#!/usr/bin/env python3
"""
LiteLLM Managed Identity AKS Deployment Script

This script deploys LiteLLM Proxy to Azure Kubernetes Service (AKS) with
Managed Identity authentication for Azure OpenAI resources.

Usage:
    python deploy_mi_aks_litellm.py [config.json]

Requirements:
    pip install -r requirements.txt
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests
import yaml
from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.models import RoleAssignmentCreateParameters
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.compute.models import VirtualMachineScaleSetIdentity, VirtualMachineScaleSetUpdate
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.msi import ManagedServiceIdentityClient
try:
    from azure.mgmt.resource import ResourceManagementClient
except ImportError:
    # azure-mgmt-resource 26.x moved the sync client under .resources.
    from azure.mgmt.resource.resources import ResourceManagementClient
from azure.mgmt.containerservice.models import (
    ManagedCluster,
    ManagedClusterAgentPoolProfile,
    ManagedClusterIdentity,
    ManagedClusterProperties,
    ManagedClusterSKU,
)
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.utils.quantity import parse_quantity


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

SUPPORTED_AFFINITY_CHECKS = {
    "responses_api_deployment_check",
    "deployment_affinity",
    "session_affinity",
}


def parse_affinity_checks(value: str) -> list[str]:
    """Parse and validate comma-separated router affinity checks."""
    checks = list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    unsupported = sorted(set(checks) - SUPPORTED_AFFINITY_CHECKS)
    if unsupported:
        raise ValueError(
            "Unsupported LITELLM_AFFINITY_CHECKS value(s): "
            f"{', '.join(unsupported)}. Supported values: "
            f"{', '.join(sorted(SUPPORTED_AFFINITY_CHECKS))}"
        )
    return checks


DEFAULT_CONFIG = {
    "mi_name": os.environ.get("MI_NAME", "litellm-managed-identity"),
    "aks_name": os.environ.get("AKS_NAME", "litellm-mi-aks"),
    "aks_node_count": int(os.environ.get("AKS_NODE_COUNT", "1")),
    "aks_vm_size": os.environ.get("AKS_VM_SIZE", "Standard_D2s_v3"),
    "aks_namespace": os.environ.get("AKS_NAMESPACE", "litellm"),
    "litellm_image": os.environ.get(
        "LITELLM_IMAGE", "docker.litellm.ai/berriai/litellm:1.95.0"
    ),
    "litellm_master_key": os.environ.get("LITELLM_MASTER_KEY", "").strip(),
    "auto_generate_master_key": os.environ.get("AUTO_GENERATE_MASTER_KEY", "true").lower() == "true",
    "store_model_in_db": os.environ.get("STORE_MODEL_IN_DB", "false").lower() == "true",
    "disable_spend_logs": os.environ.get("DISABLE_SPEND_LOGS", "false").lower() == "true",
    "store_prompts_in_spend_logs": os.environ.get(
        "STORE_PROMPTS_IN_SPEND_LOGS", "false"
    ).lower() == "true",
    "spend_logs_retention_period": os.environ.get(
        "MAXIMUM_SPEND_LOGS_RETENTION_PERIOD", "7d"
    ).strip(),
    "spend_logs_retention_interval": os.environ.get(
        "MAXIMUM_SPEND_LOGS_RETENTION_INTERVAL", "1d"
    ).strip(),
    "affinity_checks": parse_affinity_checks(
        os.environ.get(
            "LITELLM_AFFINITY_CHECKS",
            "responses_api_deployment_check,deployment_affinity,session_affinity",
        )
    ),
    "deployment_affinity_ttl_seconds": int(
        os.environ.get("DEPLOYMENT_AFFINITY_TTL_SECONDS", "3600")
    ),
    "litellm_startup_wait_seconds": int(os.environ.get("LITELLM_STARTUP_WAIT_SECONDS", "180")),
    "ingress_proxy_body_size": os.environ.get("INGRESS_PROXY_BODY_SIZE", "100m").strip() or "100m",
    "ingress_proxy_buffering": os.environ.get("INGRESS_PROXY_BUFFERING", "off").strip() or "off",
    "ingress_proxy_read_timeout": os.environ.get("INGRESS_PROXY_READ_TIMEOUT", "600").strip() or "600",
    "ingress_proxy_send_timeout": os.environ.get("INGRESS_PROXY_SEND_TIMEOUT", "600").strip() or "600",
    "azure_scope": os.environ.get("AZURE_SCOPE", "https://cognitiveservices.azure.com/.default"),
    "azure_api_version": os.environ.get("AZURE_API_VERSION", ""),
    "openai_role_name": os.environ.get("OPENAI_ROLE_NAME", "Cognitive Services OpenAI User"),
    "run_smoke_test": os.environ.get("RUN_SMOKE_TEST", "true").lower() == "true",
    "pg_user": os.environ.get("PG_USER", "litellm"),
    "pg_password": os.environ.get("PG_PASSWORD", "litellm-local-dev"),
    "pg_db": os.environ.get("PG_DB", "litellm"),
    "pg_storage": os.environ.get("PG_STORAGE", "20Gi"),
    "expand_existing_pg_pvc": os.environ.get(
        "EXPAND_EXISTING_PG_PVC", "false"
    ).lower() == "true",
    "litellm_hostname": os.environ.get("LITELLM_HOSTNAME", "").strip(),
    "letsencrypt_email": os.environ.get("LETSENCRYPT_EMAIL", "").strip(),
}

OPENAI_USER_ROLE_ID = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"  # Cognitive Services OpenAI User


# ═══════════════════════════════════════════════════════════════════════════════
# Utility Functions
# ═══════════════════════════════════════════════════════════════════════════════

def log(message: str, level: str = "INFO") -> None:
    """Print a log message with timestamp."""
    print(f"[{level}] {message}")


def generate_master_key() -> str:
    """Generate a strong URL-safe LiteLLM master key."""
    return f"sk-{secrets.token_urlsafe(48)}"


def load_config(config_path: str) -> dict[str, Any]:
    """Load and validate the configuration JSON file."""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    required_fields = ["region", "apim_resource_group", "azure-openai-list", "deployment_list"]
    for field in required_fields:
        if field not in cfg or not cfg[field]:
            raise ValueError(f"Config must include non-empty '{field}'")

    return cfg


def extract_resource_name_from_endpoint(endpoint: str) -> Optional[str]:
    """Extract Azure resource name from endpoint URL."""
    match = re.match(r"^https://([^.]+)\.openai\.azure\.com", endpoint)
    return match.group(1) if match else None


def get_subscription_id() -> str:
    """Resolve the Azure subscription used for the AKS deployment."""
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    if subscription_id:
        return subscription_id

    az_command = _resolve_tool("az") or "az"
    try:
        subscription_id = subprocess.check_output(
            [az_command, "account", "show", "--query", "id", "-o", "tsv"],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Cannot determine Azure subscription. "
            "Set AZURE_SUBSCRIPTION_ID or run az login."
        ) from exc

    if subscription_id:
        return subscription_id

    raise RuntimeError(
        "Cannot determine Azure subscription. "
        "Set AZURE_SUBSCRIPTION_ID or run az login."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Helpers (Helm-based add-ons)
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_tool(name: str) -> Optional[str]:
    """Locate a CLI tool across common Windows/Unix executable names."""
    return (
        shutil.which(name)
        or shutil.which(f"{name}.cmd")
        or shutil.which(f"{name}.exe")
    )


def run_cli(args: list[str], description: str) -> str:
    """Run a CLI command, raising with captured output on failure."""
    log(description)
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        if result.stdout:
            log(result.stdout.strip(), "ERROR")
        if result.stderr:
            log(result.stderr.strip(), "ERROR")
        raise RuntimeError(f"Command failed: {' '.join(args)}")
    return result.stdout


def _helm() -> str:
    """Return the Helm executable path or raise with an install hint."""
    helm = _resolve_tool("helm")
    if not helm:
        raise RuntimeError(
            "helm not found. Install Helm first: https://helm.sh/docs/intro/install/"
        )
    return helm


def install_ingress_nginx() -> None:
    """Install or upgrade the ingress-nginx controller via Helm."""
    helm = _helm()
    run_cli(
        [helm, "repo", "add", "ingress-nginx",
         "https://kubernetes.github.io/ingress-nginx", "--force-update"],
        "Adding ingress-nginx Helm repo",
    )
    run_cli([helm, "repo", "update"], "Updating Helm repos")
    # The AKS cloud provider derives the Azure Load Balancer health probe from the
    # Service ports' appProtocol (http/https). By default it probes path "/", but
    # ingress-nginx returns HTTP 404 on "/", which Azure treats as unhealthy and
    # removes the backend -> external 80/443 time out. Point the probe at "/healthz"
    # (ingress-nginx answers 200) so the LB keeps the node in rotation.
    probe_path_annotation = (
        "controller.service.annotations."
        "service\\.beta\\.kubernetes\\.io/azure-load-balancer-health-probe-request-path"
        "=/healthz"
    )
    run_cli(
        [helm, "upgrade", "--install", "ingress-nginx", "ingress-nginx/ingress-nginx",
         "--namespace", "ingress-nginx", "--create-namespace",
         "--set", "controller.service.type=LoadBalancer",
         "--set", probe_path_annotation,
         "--wait", "--timeout", "10m"],
        "Installing/upgrading ingress-nginx (may take a few minutes)",
    )


def install_cert_manager() -> None:
    """Install or upgrade cert-manager via Helm."""
    helm = _helm()
    run_cli(
        [helm, "repo", "add", "jetstack", "https://charts.jetstack.io", "--force-update"],
        "Adding jetstack Helm repo",
    )
    run_cli([helm, "repo", "update"], "Updating Helm repos")
    upgrade_args = [
        helm, "upgrade", "--install", "cert-manager", "jetstack/cert-manager",
        "--namespace", "cert-manager", "--create-namespace",
        "--set", "crds.enabled=true",
    ]
    upgrade_help = subprocess.run(
        [helm, "upgrade", "--help"], capture_output=True, text=True
    )
    if "--server-side" in upgrade_help.stdout:
        upgrade_args.append("--server-side=false")
    upgrade_args.extend(["--wait", "--timeout", "10m"])
    run_cli(
        upgrade_args,
        "Installing/upgrading cert-manager (may take a few minutes)",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Azure Resource Management
# ═══════════════════════════════════════════════════════════════════════════════

class AzureResourceManager:
    """Manages Azure resources using Azure SDK."""

    def __init__(self, subscription_id: str):
        self.subscription_id = subscription_id
        self.credential = DefaultAzureCredential()
        self.resource_client = ResourceManagementClient(self.credential, subscription_id)
        self.msi_client = ManagedServiceIdentityClient(self.credential, subscription_id)
        self.aks_client = ContainerServiceClient(self.credential, subscription_id)
        self.compute_client = ComputeManagementClient(self.credential, subscription_id)
        self.auth_client = AuthorizationManagementClient(self.credential, subscription_id)

    def ensure_resource_group(self, name: str, location: str) -> None:
        """Create resource group if it doesn't exist."""
        log(f"Ensuring resource group exists: {name} ({location})")
        self.resource_client.resource_groups.create_or_update(
            name,
            {"location": location}
        )

    def ensure_managed_identity(self, name: str, resource_group: str, location: str) -> dict:
        """Create or get managed identity."""
        log(f"Ensuring managed identity exists: {name}")
        try:
            identity = self.msi_client.user_assigned_identities.get(resource_group, name)
            log(f"Managed identity already exists: {name}")
        except Exception:
            identity = self.msi_client.user_assigned_identities.create_or_update(
                resource_group,
                name,
                {"location": location}
            )
            log(f"Created managed identity: {name}")

        return {
            "client_id": identity.client_id,
            "principal_id": identity.principal_id,
            "resource_id": identity.id,
        }

    def ensure_aks_cluster(
        self,
        name: str,
        resource_group: str,
        location: str,
        node_count: int,
        vm_size: str,
    ) -> dict:
        """Create or get AKS cluster."""
        log(f"Ensuring AKS exists: {name}")
        try:
            cluster = self.aks_client.managed_clusters.get(resource_group, name)
            log(f"AKS already exists: {name}")
        except Exception:
            log(f"Creating AKS cluster: {name} (this may take 5-10 minutes)")
            poller = self.aks_client.managed_clusters.begin_create_or_update(
                resource_group,
                name,
                ManagedCluster(
                    location=location,
                    properties=ManagedClusterProperties(
                        dns_prefix=f"{name}-dns",
                        agent_pool_profiles=[
                            ManagedClusterAgentPoolProfile(
                                name="nodepool1",
                                count=node_count,
                                vm_size=vm_size,
                                mode="System",
                            )
                        ],
                    ),
                    identity=ManagedClusterIdentity(type="SystemAssigned"),
                    sku=ManagedClusterSKU(name="Base", tier="Standard"),
                )
            )
            cluster = poller.result()
            log(f"Created AKS cluster: {name}")

        return {
            "node_resource_group": cluster.node_resource_group,
            "fqdn": cluster.fqdn,
        }

    def get_aks_credentials(self, name: str, resource_group: str) -> None:
        """Fetch AKS credentials and configure kubectl."""
        log("Fetching AKS credentials")
        # Use az CLI for kubeconfig merge (SDK doesn't directly support this)
        az_command = shutil.which("az.cmd") or shutil.which("az") or "az"
        subprocess.run(
            [az_command, "aks", "get-credentials", "--name", name, "--resource-group", resource_group, "--overwrite-existing"],
            check=True,
            capture_output=True,
        )

    def get_vmss_in_resource_group(self, resource_group: str) -> str:
        """Get the first VMSS name in a resource group."""
        vmss_list = list(self.compute_client.virtual_machine_scale_sets.list(resource_group))
        if not vmss_list:
            raise RuntimeError(f"No VMSS found in resource group: {resource_group}")
        return vmss_list[0].name

    def assign_identity_to_vmss(self, vmss_name: str, resource_group: str, identity_resource_id: str) -> None:
        """Assign user-assigned managed identity to VMSS."""
        log(f"Assigning user-assigned MI to AKS node VMSS: {vmss_name}")
        vmss = self.compute_client.virtual_machine_scale_sets.get(resource_group, vmss_name)

        user_identities = vmss.identity.user_assigned_identities or {} if vmss.identity else {}
        normalized_identity_id = identity_resource_id.rstrip("/").lower()
        attached_identity_ids = {
            resource_id.rstrip("/").lower() for resource_id in user_identities
        }
        if normalized_identity_id in attached_identity_ids:
            log(f"Managed identity is already attached to VMSS: {vmss_name}")
            return

        user_identities[identity_resource_id] = {}

        identity_type = "SystemAssigned, UserAssigned" if vmss.identity and vmss.identity.type == "SystemAssigned" else "UserAssigned"

        poller = self.compute_client.virtual_machine_scale_sets.begin_update(
            resource_group,
            vmss_name,
            VirtualMachineScaleSetUpdate(
                identity=VirtualMachineScaleSetIdentity(
                    type=identity_type,
                    user_assigned_identities=user_identities,
                )
            ),
        )
        poller.result()
        log(f"Verified: MI is attached to VMSS.")

    def assign_role_on_scope(
        self,
        principal_id: str,
        scope: str,
        role_name: str,
        subscription_id: str,
    ) -> bool:
        """Assign a role to a principal on a specific scope."""
        auth_client = AuthorizationManagementClient(self.credential, subscription_id)

        # Check if role already assigned
        existing = list(auth_client.role_assignments.list_for_scope(
            scope,
            filter=f"principalId eq '{principal_id}'"
        ))

        for assignment in existing:
            if role_name.lower() in assignment.role_definition_id.lower() or OPENAI_USER_ROLE_ID in assignment.role_definition_id:
                log(f"Role already assigned on scope")
                return False

        # Get role definition ID
        role_defs = list(auth_client.role_definitions.list(
            scope,
            filter=f"roleName eq '{role_name}'"
        ))
        if not role_defs:
            log(f"WARNING: Role '{role_name}' not found", "WARN")
            return False

        role_def_id = role_defs[0].id

        # Create role assignment
        import uuid
        assignment_name = str(uuid.uuid4())
        auth_client.role_assignments.create(
            scope,
            assignment_name,
            RoleAssignmentCreateParameters(
                role_definition_id=role_def_id,
                principal_id=principal_id,
                principal_type="ServicePrincipal",
            )
        )
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# Kubernetes Operations
# ═══════════════════════════════════════════════════════════════════════════════

class KubernetesManager:
    """Manages Kubernetes resources."""

    def __init__(self, namespace: str):
        self.namespace = namespace
        k8s_config.load_kube_config()
        self.core_v1 = k8s_client.CoreV1Api()
        self.apps_v1 = k8s_client.AppsV1Api()
        self.networking_v1 = k8s_client.NetworkingV1Api()
        self.custom = k8s_client.CustomObjectsApi()

    def ensure_namespace(self) -> None:
        """Create namespace if it doesn't exist."""
        try:
            self.core_v1.read_namespace(self.namespace)
        except ApiException as e:
            if e.status == 404:
                self.core_v1.create_namespace(
                    k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=self.namespace))
                )

    def apply_configmap(self, name: str, data: dict[str, str]) -> None:
        """Create or update a ConfigMap."""
        configmap = k8s_client.V1ConfigMap(
            metadata=k8s_client.V1ObjectMeta(name=name, namespace=self.namespace),
            data=data,
        )
        try:
            self.core_v1.read_namespaced_config_map(name, self.namespace)
            self.core_v1.replace_namespaced_config_map(name, self.namespace, configmap)
        except ApiException as e:
            if e.status == 404:
                self.core_v1.create_namespaced_config_map(self.namespace, configmap)
            else:
                raise

    def apply_secret(self, name: str, data: dict[str, str]) -> None:
        """Create or update a Secret without dropping an existing permanent Salt."""
        secret = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(name=name, namespace=self.namespace),
            string_data=data,
        )
        try:
            existing = self.core_v1.read_namespaced_secret(name, self.namespace)
            secret.metadata.resource_version = existing.metadata.resource_version
            existing_data = existing.data or {}
            secret.data = (
                {"LITELLM_SALT_KEY": existing_data["LITELLM_SALT_KEY"]}
                if "LITELLM_SALT_KEY" in existing_data
                else {}
            )
            self.core_v1.replace_namespaced_secret(name, self.namespace, secret)
        except ApiException as e:
            if e.status == 404:
                self.core_v1.create_namespaced_secret(self.namespace, secret)
            else:
                raise

    def apply_pvc(
        self, name: str, storage: str, expand_existing: bool = False
    ) -> None:
        """Create a PVC, or explicitly expand an existing one."""
        pvc = k8s_client.V1PersistentVolumeClaim(
            metadata=k8s_client.V1ObjectMeta(name=name, namespace=self.namespace),
            spec=k8s_client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=k8s_client.V1ResourceRequirements(requests={"storage": storage}),
            ),
        )
        try:
            existing = self.core_v1.read_namespaced_persistent_volume_claim(
                name, self.namespace
            )
            current_storage = existing.spec.resources.requests.get("storage")
            if current_storage is None or parse_quantity(storage) == parse_quantity(
                current_storage
            ):
                return
            if parse_quantity(storage) < parse_quantity(current_storage):
                raise ValueError(
                    f"Refusing to shrink PVC {name} from {current_storage} to {storage}"
                )
            if not expand_existing:
                log(
                    f"PVC {name} remains at {current_storage}; set "
                    "EXPAND_EXISTING_PG_PVC=true to expand it to "
                    f"{storage}.",
                    "WARN",
                )
                return
            self.core_v1.patch_namespaced_persistent_volume_claim(
                name,
                self.namespace,
                {"spec": {"resources": {"requests": {"storage": storage}}}},
            )
            log(f"Requested PVC expansion: {name} {current_storage} -> {storage}")
        except ApiException as e:
            if e.status == 404:
                self.core_v1.create_namespaced_persistent_volume_claim(self.namespace, pvc)
            else:
                raise

    def apply_deployment(self, deployment: k8s_client.V1Deployment) -> None:
        """Create or update a Deployment."""
        name = deployment.metadata.name
        try:
            self.apps_v1.read_namespaced_deployment(name, self.namespace)
            self.apps_v1.replace_namespaced_deployment(name, self.namespace, deployment)
        except ApiException as e:
            if e.status == 404:
                self.apps_v1.create_namespaced_deployment(self.namespace, deployment)
            else:
                raise

    def apply_service(self, service: k8s_client.V1Service) -> None:
        """Create or update a Service."""
        name = service.metadata.name
        try:
            existing = self.core_v1.read_namespaced_service(name, self.namespace)
            # Preserve clusterIP for update
            service.spec.cluster_ip = existing.spec.cluster_ip
            self.core_v1.replace_namespaced_service(name, self.namespace, service)
        except ApiException as e:
            if e.status == 404:
                self.core_v1.create_namespaced_service(self.namespace, service)
            else:
                raise

    def wait_for_deployment(self, name: str, timeout: int = 600) -> bool:
        """Wait for a deployment to be ready."""
        log(f"Waiting for deployment rollout: {name}")
        start = time.time()
        while time.time() - start < timeout:
            try:
                deployment = self.apps_v1.read_namespaced_deployment(name, self.namespace)
                if deployment.status.ready_replicas == deployment.spec.replicas:
                    return True
            except ApiException:
                pass
            time.sleep(5)
        return False

    def get_pod_logs(self, deployment_name: str, tail_lines: int = 10) -> str:
        """Get logs from a deployment's pod."""
        try:
            pods = self.core_v1.list_namespaced_pod(
                self.namespace,
                label_selector=f"app={deployment_name}"
            )
            if pods.items:
                # Prefer the newest running pod during rolling updates.
                selected = sorted(
                    pods.items,
                    key=lambda p: p.metadata.creation_timestamp or 0,
                    reverse=True,
                )[0]
                for pod in pods.items:
                    phase = (pod.status.phase or "").lower()
                    if phase == "running":
                        selected = pod
                        break
                return self.core_v1.read_namespaced_pod_log(
                    selected.metadata.name,
                    self.namespace,
                    tail_lines=tail_lines,
                )
        except ApiException:
            pass
        return ""

    def get_service_external_ip(
        self, name: str, timeout: int = 150, namespace: Optional[str] = None
    ) -> Optional[str]:
        """Get the external IP (or hostname) of a LoadBalancer service."""
        target_ns = namespace or self.namespace
        start = time.time()
        while time.time() - start < timeout:
            try:
                svc = self.core_v1.read_namespaced_service(name, target_ns)
                if svc.status.load_balancer.ingress:
                    lb = svc.status.load_balancer.ingress[0]
                    return lb.ip or lb.hostname
            except ApiException:
                pass
            time.sleep(5)
        return None

    def apply_ingress(self, ingress: k8s_client.V1Ingress) -> None:
        """Create or update an Ingress."""
        name = ingress.metadata.name
        try:
            self.networking_v1.read_namespaced_ingress(name, self.namespace)
            self.networking_v1.replace_namespaced_ingress(name, self.namespace, ingress)
        except ApiException as e:
            if e.status == 404:
                self.networking_v1.create_namespaced_ingress(self.namespace, ingress)
            else:
                raise

    def apply_cluster_issuer(self, name: str, email: str) -> None:
        """Create or update a cert-manager ClusterIssuer, waiting for the webhook."""
        body = build_cluster_issuer(name, email)
        last_error: Optional[Exception] = None
        for _ in range(24):
            try:
                self.custom.create_cluster_custom_object(
                    "cert-manager.io", "v1", "clusterissuers", body
                )
                return
            except ApiException as e:
                if e.status == 409:
                    self.custom.patch_cluster_custom_object(
                        "cert-manager.io", "v1", "clusterissuers", name, body
                    )
                    return
                # cert-manager webhook may not be ready immediately after install
                last_error = e
                time.sleep(5)
        raise RuntimeError(f"Failed to create ClusterIssuer: {last_error}")


# ═══════════════════════════════════════════════════════════════════════════════
# LiteLLM Config Generation
# ═══════════════════════════════════════════════════════════════════════════════

def generate_litellm_config(
    cfg: dict[str, Any], settings: Optional[dict[str, Any]] = None
) -> str:
    """Generate LiteLLM configuration YAML."""
    settings = settings or DEFAULT_CONFIG
    resources = cfg["azure-openai-list"]
    deployments = cfg["deployment_list"]

    model_list = []
    for deployment in deployments:
        model = deployment["model"]
        deployment_name = deployment["deployment_name"]
        alias = deployment_name

        # Decide API version based on model name
        model_low = model.lower()
        if model_low.startswith(("gpt-image-", "dall-e", "sora")):
            resolved_api_version = "2025-04-01-preview"
        else:
            # Let's try matching the test script's expectation of standard endpoints vs responses api
            resolved_api_version = "2025-04-01-preview"

        for resource in resources:
            endpoint = resource.get("endpoint", "")
            if not endpoint:
                name = resource["name"]
                endpoint = f"https://{name}.openai.azure.com/"

            model_list.append({
                "model_name": alias,
                "litellm_params": {
                    "model": f"azure/{model}",
                    "base_model": model,
                    "deployment_id": deployment_name,
                    "api_base": endpoint,
                    "api_version": resolved_api_version,
                },
            })

    general_settings = {
        "disable_spend_logs": settings.get(
            "disable_spend_logs", DEFAULT_CONFIG["disable_spend_logs"]
        ),
        "store_prompts_in_spend_logs": settings.get(
            "store_prompts_in_spend_logs",
            DEFAULT_CONFIG["store_prompts_in_spend_logs"],
        ),
    }
    retention_period = settings.get(
        "spend_logs_retention_period",
        DEFAULT_CONFIG["spend_logs_retention_period"],
    )
    if retention_period:
        general_settings["maximum_spend_logs_retention_period"] = retention_period
        general_settings["maximum_spend_logs_retention_interval"] = settings.get(
            "spend_logs_retention_interval",
            DEFAULT_CONFIG["spend_logs_retention_interval"],
        )

    config = {
        "model_list": model_list,
        "litellm_settings": {
            "enable_azure_ad_token_refresh": True,
        },
        "general_settings": general_settings,
        "router_settings": {
            "routing_strategy": "simple-shuffle",
            "num_retries": 2,
            "optional_pre_call_checks": settings["affinity_checks"],
            "deployment_affinity_ttl_seconds": settings[
                "deployment_affinity_ttl_seconds"
            ],
        },
    }

    return yaml.dump(config, default_flow_style=False, sort_keys=False)


# ═══════════════════════════════════════════════════════════════════════════════
# Kubernetes Resource Builders
# ═══════════════════════════════════════════════════════════════════════════════

def build_postgres_deployment(pg_user: str, pg_password: str, pg_db: str) -> k8s_client.V1Deployment:
    """Build PostgreSQL deployment."""
    return k8s_client.V1Deployment(
        metadata=k8s_client.V1ObjectMeta(name="postgres"),
        spec=k8s_client.V1DeploymentSpec(
            replicas=1,
            strategy=k8s_client.V1DeploymentStrategy(type="Recreate"),
            selector=k8s_client.V1LabelSelector(match_labels={"app": "postgres"}),
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(labels={"app": "postgres"}),
                spec=k8s_client.V1PodSpec(
                    containers=[
                        k8s_client.V1Container(
                            name="postgres",
                            image="postgres:16-alpine",
                            ports=[k8s_client.V1ContainerPort(container_port=5432)],
                            env=[
                                k8s_client.V1EnvVar(name="POSTGRES_DB", value=pg_db),
                                k8s_client.V1EnvVar(name="POSTGRES_USER", value=pg_user),
                                k8s_client.V1EnvVar(name="POSTGRES_PASSWORD", value=pg_password),
                                k8s_client.V1EnvVar(name="PGDATA", value="/var/lib/postgresql/data/pgdata"),
                            ],
                            volume_mounts=[
                                k8s_client.V1VolumeMount(name="pg-data", mount_path="/var/lib/postgresql/data")
                            ],
                            resources=k8s_client.V1ResourceRequirements(
                                requests={"cpu": "100m", "memory": "128Mi"},
                                limits={"cpu": "500m", "memory": "256Mi"},
                            ),
                            startup_probe=k8s_client.V1Probe(
                                _exec=k8s_client.V1ExecAction(
                                    command=[
                                        "sh",
                                        "-c",
                                        'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                                    ]
                                ),
                                period_seconds=10,
                                timeout_seconds=5,
                                failure_threshold=30,
                            ),
                            readiness_probe=k8s_client.V1Probe(
                                _exec=k8s_client.V1ExecAction(
                                    command=[
                                        "sh",
                                        "-c",
                                        'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                                    ]
                                ),
                                period_seconds=10,
                                timeout_seconds=5,
                                failure_threshold=3,
                            ),
                            liveness_probe=k8s_client.V1Probe(
                                _exec=k8s_client.V1ExecAction(
                                    command=[
                                        "sh",
                                        "-c",
                                        'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                                    ]
                                ),
                                period_seconds=20,
                                timeout_seconds=5,
                                failure_threshold=6,
                            ),
                        )
                    ],
                    volumes=[
                        k8s_client.V1Volume(
                            name="pg-data",
                            persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(claim_name="pg-data"),
                        )
                    ],
                ),
            ),
        ),
    )


def build_postgres_service() -> k8s_client.V1Service:
    """Build PostgreSQL service."""
    return k8s_client.V1Service(
        metadata=k8s_client.V1ObjectMeta(name="postgres"),
        spec=k8s_client.V1ServiceSpec(
            selector={"app": "postgres"},
            ports=[k8s_client.V1ServicePort(port=5432, target_port=5432)],
        ),
    )


def build_litellm_deployment(
    image: str,
    config_hash: str,
    secret_hash: str,
) -> k8s_client.V1Deployment:
    """Build LiteLLM proxy deployment."""
    return k8s_client.V1Deployment(
        metadata=k8s_client.V1ObjectMeta(name="litellm-mi-proxy"),
        spec=k8s_client.V1DeploymentSpec(
            replicas=1,
            selector=k8s_client.V1LabelSelector(match_labels={"app": "litellm-mi-proxy"}),
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(
                    labels={"app": "litellm-mi-proxy"},
                    annotations={
                        "litellm.config-hash": config_hash,
                        "litellm.secret-hash": secret_hash,
                    },
                ),
                spec=k8s_client.V1PodSpec(
                    containers=[
                        k8s_client.V1Container(
                            name="litellm",
                            image=image,
                            image_pull_policy="IfNotPresent",
                            command=["litellm"],
                            args=["--config", "/app/config/config.yaml", "--port", "4000"],
                            ports=[k8s_client.V1ContainerPort(container_port=4000)],
                            resources=k8s_client.V1ResourceRequirements(
                                requests={"cpu": "250m", "memory": "1Gi"},
                                limits={"cpu": "1000m", "memory": "2Gi"},
                            ),
                            startup_probe=k8s_client.V1Probe(
                                http_get=k8s_client.V1HTTPGetAction(
                                    path="/health/liveliness",
                                    port=4000,
                                    scheme="HTTP",
                                ),
                                period_seconds=10,
                                timeout_seconds=5,
                                failure_threshold=30,
                            ),
                            readiness_probe=k8s_client.V1Probe(
                                http_get=k8s_client.V1HTTPGetAction(
                                    path="/health/readiness",
                                    port=4000,
                                    scheme="HTTP",
                                ),
                                period_seconds=10,
                                timeout_seconds=5,
                                failure_threshold=3,
                            ),
                            liveness_probe=k8s_client.V1Probe(
                                http_get=k8s_client.V1HTTPGetAction(
                                    path="/health/liveliness",
                                    port=4000,
                                    scheme="HTTP",
                                ),
                                period_seconds=20,
                                timeout_seconds=5,
                                failure_threshold=6,
                            ),
                            env_from=[
                                k8s_client.V1EnvFromSource(secret_ref=k8s_client.V1SecretEnvSource(name="litellm-env"))
                            ],
                            volume_mounts=[
                                k8s_client.V1VolumeMount(
                                    name="litellm-config",
                                    mount_path="/app/config/config.yaml",
                                    sub_path="config.yaml",
                                )
                            ],
                        )
                    ],
                    volumes=[
                        k8s_client.V1Volume(
                            name="litellm-config",
                            config_map=k8s_client.V1ConfigMapVolumeSource(name="litellm-config"),
                        )
                    ],
                ),
            ),
        ),
    )


def build_litellm_service(service_type: str = "LoadBalancer") -> k8s_client.V1Service:
    """Build LiteLLM proxy service."""
    return k8s_client.V1Service(
        metadata=k8s_client.V1ObjectMeta(name="litellm-mi-proxy"),
        spec=k8s_client.V1ServiceSpec(
            selector={"app": "litellm-mi-proxy"},
            ports=[k8s_client.V1ServicePort(port=4000, target_port=4000, protocol="TCP")],
            type=service_type,
        ),
    )


def build_litellm_ingress(
    hostname: str,
    namespace: str,
    proxy_body_size: str,
    proxy_buffering: str,
    proxy_read_timeout: str,
    proxy_send_timeout: str,
) -> k8s_client.V1Ingress:
    """Build a TLS Ingress that routes hostname traffic to litellm-mi-proxy:4000."""
    return k8s_client.V1Ingress(
        metadata=k8s_client.V1ObjectMeta(
            name="litellm-ingress",
            namespace=namespace,
            annotations={
                "cert-manager.io/cluster-issuer": "letsencrypt-prod",
                "nginx.ingress.kubernetes.io/ssl-redirect": "true",
                "nginx.ingress.kubernetes.io/proxy-body-size": proxy_body_size,
                "nginx.ingress.kubernetes.io/proxy-buffering": proxy_buffering,
                "nginx.ingress.kubernetes.io/proxy-read-timeout": proxy_read_timeout,
                "nginx.ingress.kubernetes.io/proxy-send-timeout": proxy_send_timeout,
            },
        ),
        spec=k8s_client.V1IngressSpec(
            ingress_class_name="nginx",
            tls=[k8s_client.V1IngressTLS(hosts=[hostname], secret_name="litellm-tls")],
            rules=[
                k8s_client.V1IngressRule(
                    host=hostname,
                    http=k8s_client.V1HTTPIngressRuleValue(
                        paths=[
                            k8s_client.V1HTTPIngressPath(
                                path="/",
                                path_type="Prefix",
                                backend=k8s_client.V1IngressBackend(
                                    service=k8s_client.V1IngressServiceBackend(
                                        name="litellm-mi-proxy",
                                        port=k8s_client.V1ServiceBackendPort(number=4000),
                                    )
                                ),
                            )
                        ]
                    ),
                )
            ],
        ),
    )


def build_cluster_issuer(name: str, email: str) -> dict[str, Any]:
    """Build a Let's Encrypt (production) ClusterIssuer manifest."""
    return {
        "apiVersion": "cert-manager.io/v1",
        "kind": "ClusterIssuer",
        "metadata": {"name": name},
        "spec": {
            "acme": {
                "server": "https://acme-v02.api.letsencrypt.org/directory",
                "email": email,
                "privateKeySecretRef": {"name": name},
                "solvers": [
                    {"http01": {"ingress": {"class": "nginx"}}}
                ],
            }
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Smoke Test
# ═══════════════════════════════════════════════════════════════════════════════

def run_smoke_test(
    base_url: str,
    master_key: str,
    model_alias: str,
    azure_api_version: str,
) -> bool:
    """Run smoke tests against LiteLLM proxy."""
    log("Running smoke tests for Chat API (OpenAI + Azure OpenAI style)")

    headers = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_alias,
        "messages": [{"role": "user", "content": "reply only: ok"}],
        "max_tokens": 32,
    }

    # Test OpenAI-style endpoint
    try:
        resp = requests.post(
            f"{base_url}/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        if not resp.ok:
            log(f"OpenAI-style /v1/chat/completions failed. status={resp.status_code}", "ERROR")
            return False
    except Exception as e:
        log(f"OpenAI-style /v1/chat/completions failed: {e}", "ERROR")
        return False

    # Test Azure-style endpoint
    azure_url = f"{base_url}/openai/deployments/{model_alias}/chat/completions"
    if azure_api_version:
        azure_url += f"?api-version={azure_api_version}"

    try:
        resp = requests.post(
            azure_url,
            headers=headers,
            json=payload,
            timeout=60,
        )
        if not resp.ok:
            log(f"Azure-style chat format failed. status={resp.status_code}", "ERROR")
            return False
    except Exception as e:
        log(f"Azure-style chat format failed: {e}", "ERROR")
        return False

    log("Smoke test passed: /v1/chat/completions and Azure-style chat are both available")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Main Deployment Flow
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    default_config = Path(__file__).parent / "azure-openai.loc.json"
    if not default_config.exists():
        default_config = Path(__file__).parent / "azure-openai.json"

    parser = argparse.ArgumentParser(description="Deploy LiteLLM with Managed Identity to AKS")
    parser.add_argument(
        "config",
        nargs="?",
        default=str(default_config),
        help="Path to config file (defaults to azure-openai.loc.json, fallback azure-openai.json)",
    )
    args = parser.parse_args()

    # Load configuration
    cfg = load_config(args.config)
    region = cfg["region"]
    rg_name = cfg["apim_resource_group"]
    mi_name = cfg.get("managed_identity") or DEFAULT_CONFIG["mi_name"]

    # Merge with defaults
    settings = {**DEFAULT_CONFIG}
    if cfg.get("managed_identity"):
        settings["mi_name"] = cfg["managed_identity"]

    log(f"Configuration loaded from: {args.config}")
    log(f"Region: {region}, Resource Group: {rg_name}, MI: {mi_name}")

    # Determine exposure mode: Ingress+HTTPS when a hostname is provided
    ingress_enabled = bool(settings["litellm_hostname"])
    if ingress_enabled and not settings["letsencrypt_email"]:
        log("LITELLM_HOSTNAME is set but LETSENCRYPT_EMAIL is empty.", "ERROR")
        log("Set LETSENCRYPT_EMAIL so cert-manager can issue a Let's Encrypt certificate.", "ERROR")
        sys.exit(1)
    generated_master_key: Optional[str] = None
    if not settings["litellm_master_key"]:
        if settings["auto_generate_master_key"]:
            generated_master_key = generate_master_key()
            settings["litellm_master_key"] = generated_master_key
            log("LITELLM_MASTER_KEY is empty. Auto-generated a strong key for this deployment.", "WARN")
        else:
            log("LITELLM_MASTER_KEY is empty.", "ERROR")
            log("Set a strong key via environment variable LITELLM_MASTER_KEY (at least 24 chars), or set AUTO_GENERATE_MASTER_KEY=true.", "ERROR")
            sys.exit(1)
    if len(settings["litellm_master_key"]) < 24:
        log("LITELLM_MASTER_KEY is too short.", "ERROR")
        log("Use a strong key with at least 24 characters for production deployments.", "ERROR")
        sys.exit(1)
    if ingress_enabled:
        log(f"Ingress mode enabled: https://{settings['litellm_hostname']} (Let's Encrypt)")
    else:
        log("Ingress mode disabled: exposing LiteLLM via LoadBalancer:4000")

    # Get current subscription
    subscription_id = get_subscription_id()
    log(f"Using subscription: {subscription_id}")

    # Initialize Azure manager
    azure_mgr = AzureResourceManager(subscription_id)

    # Step 1: Ensure resource group
    azure_mgr.ensure_resource_group(rg_name, region)

    # Step 2: Ensure managed identity
    mi_info = azure_mgr.ensure_managed_identity(mi_name, rg_name, region)
    log(f"MI Client ID: {mi_info['client_id']}")

    # Step 3: Ensure AKS cluster
    aks_info = azure_mgr.ensure_aks_cluster(
        settings["aks_name"],
        rg_name,
        region,
        settings["aks_node_count"],
        settings["aks_vm_size"],
    )

    # Step 4: Get AKS credentials
    azure_mgr.get_aks_credentials(settings["aks_name"], rg_name)

    # Step 5: Assign MI to VMSS
    vmss_name = azure_mgr.get_vmss_in_resource_group(aks_info["node_resource_group"])
    azure_mgr.assign_identity_to_vmss(vmss_name, aks_info["node_resource_group"], mi_info["resource_id"])

    # Step 6: Assign RBAC roles on AOAI resources
    log(f"Granting '{settings['openai_role_name']}' on each Azure OpenAI resource")
    for aoai in cfg["azure-openai-list"]:
        endpoint = aoai.get("endpoint", "")
        aoai_name = extract_resource_name_from_endpoint(endpoint)
        if not aoai_name:
            log(f"WARN: Cannot extract resource name from endpoint for {aoai['name']}, skipping.", "WARN")
            continue

        aoai_rg = aoai["resource_group"]
        aoai_sub = aoai.get("subscription_id") or subscription_id

        log(f"Processing AOAI resource: {aoai_name} (rg={aoai_rg}, sub={aoai_sub})")

        # Verify resource exists
        try:
            cog_client = CognitiveServicesManagementClient(azure_mgr.credential, aoai_sub)
            cog_client.accounts.get(aoai_rg, aoai_name)
        except Exception:
            log(f"WARN: AOAI resource not found, skipping: {aoai_name}", "WARN")
            continue

        # Assign role
        scope = f"/subscriptions/{aoai_sub}/resourceGroups/{aoai_rg}/providers/Microsoft.CognitiveServices/accounts/{aoai_name}"
        if azure_mgr.assign_role_on_scope(
            mi_info["principal_id"],
            scope,
            settings["openai_role_name"],
            aoai_sub,
        ):
            log(f"Assigned role on: {aoai_name}")
        else:
            log(f"Role already assigned on: {aoai_name}")

    # Step 7: Generate LiteLLM config
    litellm_config_yaml = generate_litellm_config(cfg, settings)
    config_path = Path(args.config).parent / "litellm.config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(litellm_config_yaml)
    log(f"Generated LiteLLM config: {config_path}")

    # Step 8: Deploy to Kubernetes
    k8s_mgr = KubernetesManager(settings["aks_namespace"])
    k8s_mgr.ensure_namespace()

    # ConfigMap
    k8s_mgr.apply_configmap("litellm-config", {"config.yaml": litellm_config_yaml})

    # PostgreSQL
    log(f"Deploying PostgreSQL in namespace {settings['aks_namespace']}...")
    database_url = f"postgresql://{settings['pg_user']}:{settings['pg_password']}@postgres.{settings['aks_namespace']}.svc.cluster.local:5432/{settings['pg_db']}"
    k8s_mgr.apply_pvc(
        "pg-data",
        settings["pg_storage"],
        settings["expand_existing_pg_pvc"],
    )
    k8s_mgr.apply_deployment(build_postgres_deployment(settings["pg_user"], settings["pg_password"], settings["pg_db"]))
    k8s_mgr.apply_service(build_postgres_service())

    if not k8s_mgr.wait_for_deployment("postgres", timeout=120):
        log("PostgreSQL deployment failed to become ready", "ERROR")
        sys.exit(1)
    log("PostgreSQL is ready.")

    # Secret
    secret_data = {
        "AZURE_CREDENTIAL": "ManagedIdentityCredential",
        "AZURE_CLIENT_ID": mi_info["client_id"],
        "AZURE_SCOPE": settings["azure_scope"],
        "AZURE_API_VERSION": settings["azure_api_version"],
        "LITELLM_MASTER_KEY": settings["litellm_master_key"],
        "DATABASE_URL": database_url,
        "STORE_MODEL_IN_DB": "true" if settings["store_model_in_db"] else "false",
    }
    k8s_mgr.apply_secret("litellm-env", secret_data)

    # LiteLLM
    config_hash = hashlib.sha256(litellm_config_yaml.encode("utf-8")).hexdigest()
    secret_hash = hashlib.sha256(
        json.dumps(secret_data, sort_keys=True).encode("utf-8")
    ).hexdigest()
    k8s_mgr.apply_deployment(
        build_litellm_deployment(settings["litellm_image"], config_hash, secret_hash)
    )
    service_type = "ClusterIP" if ingress_enabled else "LoadBalancer"
    k8s_mgr.apply_service(build_litellm_service(service_type))

    if not k8s_mgr.wait_for_deployment("litellm-mi-proxy", timeout=600):
        log("LiteLLM deployment failed to become ready", "ERROR")
        sys.exit(1)

    # Wait for app to fully start
    log("Waiting for LiteLLM application to start (Prisma migrations + Uvicorn)...")
    startup_wait_seconds = max(0, settings["litellm_startup_wait_seconds"])
    poll_seconds = 5
    attempts = startup_wait_seconds // poll_seconds if startup_wait_seconds > 0 else 0
    uvicorn_ready = False
    for i in range(attempts):
        logs = k8s_mgr.get_pod_logs("litellm-mi-proxy", tail_lines=10)
        if "Uvicorn running" in logs:
            uvicorn_ready = True
            break
        if i % 6 == 0:
            elapsed = i * poll_seconds
            log(f"LiteLLM still starting... elapsed={elapsed}s/{startup_wait_seconds}s")
        time.sleep(poll_seconds)
    if not uvicorn_ready and startup_wait_seconds > 0:
        log(
            "Timed out waiting for 'Uvicorn running'. Continuing with ingress setup; service may still become ready shortly.",
            "WARN",
        )

    # Step 9: Expose the service (Ingress+HTTPS or LoadBalancer)
    ingress_ip: Optional[str] = None
    if ingress_enabled:
        log("Setting up ingress-nginx + cert-manager for HTTPS")
        install_ingress_nginx()
        install_cert_manager()
        k8s_mgr.apply_cluster_issuer("letsencrypt-prod", settings["letsencrypt_email"])
        k8s_mgr.apply_ingress(
            build_litellm_ingress(
                settings["litellm_hostname"],
                settings["aks_namespace"],
                settings["ingress_proxy_body_size"],
                settings["ingress_proxy_buffering"],
                settings["ingress_proxy_read_timeout"],
                settings["ingress_proxy_send_timeout"],
            )
        )
        ingress_ip = k8s_mgr.get_service_external_ip(
            "ingress-nginx-controller", timeout=300, namespace="ingress-nginx"
        )
        base_url = f"https://{settings['litellm_hostname']}"
        log("Skipping public smoke test: point DNS to the ingress IP first, "
            "then the certificate is issued automatically.")
    else:
        external_ip = k8s_mgr.get_service_external_ip("litellm-mi-proxy")
        base_url = f"http://{external_ip}:4000" if external_ip else "(pending)"

        # Step 10: Smoke test (LoadBalancer mode only)
        if settings["run_smoke_test"] and external_ip:
            first_deployment = cfg["deployment_list"][0]["deployment_name"]
            model_alias = first_deployment

            # Wait a bit for service to be reachable
            time.sleep(10)

            if not run_smoke_test(base_url, settings["litellm_master_key"], model_alias, settings["azure_api_version"]):
                log("Smoke test failed", "ERROR")
                sys.exit(1)

    # Step 11: Print summary
    print()
    print("=" * 63)
    print("  LiteLLM Proxy — Deployment Complete")
    print("=" * 63)
    print()
    print(f"  Web UI URL        : {base_url}/ui")
    print(f"  Web UI Username   : admin")
    print("  Web UI Password   : (hidden, from LITELLM_MASTER_KEY)")
    print()
    print(f"  API Base URL      : {base_url}")
    print("  API Key           : (hidden, from LITELLM_MASTER_KEY)")
    print()
    print(f"  Managed Identity  : {mi_name} (client_id: {mi_info['client_id']})")
    print(f"  AKS Cluster       : {settings['aks_name']} ({settings['aks_vm_size']} x{settings['aks_node_count']})")
    print(f"  Namespace         : {settings['aks_namespace']}")
    print(f"  Database          : PostgreSQL (in-cluster)")
    if generated_master_key:
        print(f"  Generated Master Key : {generated_master_key}")
        print("  IMPORTANT            : Save this key securely and rotate it into your secret manager.")
    if ingress_enabled:
        print()
        print("-" * 63)
        print("  Next step: point Alibaba Cloud DNS at the ingress")
        print("-" * 63)
        print(f"  Hostname          : {settings['litellm_hostname']}")
        print(f"  Ingress public IP : {ingress_ip or '(pending, re-check with kubectl)'}")
        print()
        print("  Create an A record in Alibaba Cloud DNS:")
        print(f"    A  {settings['litellm_hostname']}  ->  {ingress_ip or '<ingress IP>'}")
        print()
        print("  The certificate is issued automatically after DNS propagates. Check:")
        print(f"    kubectl get certificate -n {settings['aks_namespace']}")
    print()
    print("=" * 63)


if __name__ == "__main__":
    main()
