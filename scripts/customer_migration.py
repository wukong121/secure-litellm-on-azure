#!/usr/bin/env python3
"""Customer migration guidance and read-only previews; never deploy or cut over."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID

from scripts.render_stage7_domain import domain_hosts, render

ROOT = Path(__file__).resolve().parents[1]
STAGES = (
    ("Baseline and recoverable backup", ("inventory", "backup_restore", "key_salt_recovery", "protocol_baseline")),
    ("Legacy minimum hardening", ("legacy_health", "alerts_received", "rollback_snapshot")),
    ("Customer architecture decisions", ("network_capacity", "identity_owners", "pg_auth_ha", "l3_policy", "protocol_scope")),
    ("Supply chain and isolated target", ("oidc_scope", "image_signature_sbom", "target_isolation")),
    ("Private network and AKS", ("private_dns_egress", "private_runner", "workload_identity", "private_ingress")),
    ("Data platform and restore rehearsal", ("pg_migration_restore", "key_salt_decryption", "redis_entra", "csi_rotation")),
    ("HA and routing", ("replica_failure", "load_affinity", "capacity_limits", "no_legacy_db_writes")),
    ("Entra and split domains", ("tenant_negative_tests", "object_ownership", "admin_private", "required_protocols")),
    ("L3 and observability", ("durable_audit_recovery", "audit_governance", "telemetry_received", "guardrail_scope")),
    ("Approved pilot and cutover", ("enabled_what_if", "origin_tls_private_link", "pilot_regression", "rollback_rehearsal", "dual_owner_release")),
)
COMPONENTS = {
    "bootstrap": (0, "bootstrap", {"workspaceMode", "logRetentionDays"}),
    "network": (0, "network-bootstrap", {"virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix"}),
    "backup": (0, "backup-storage", {"storageAccountName", "virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix", "logAnalyticsWorkspaceName", "backupOwnerPrincipalId", "backupAutomationPrincipalId", "storageSku", "softDeleteRetentionDays"}),
    "legacy-logging": (1, "legacy-logging", {"workspaceMode"}),
    "monitoring": (1, "monitoring", {"logAnalyticsWorkspaceName"}),
    "platform": (3, "environments", {"containerRegistryName", "logAnalyticsWorkspaceName", "stage4Network", "stage4Aks", "stage5Data", "approvedHttpsFqdns", "azureOpenAIConnections", "createStage5KeyVaultPrivateDnsZone", "createStage5PostgresqlPrivateDnsZone", "createStage5ManagedRedisPrivateDnsZone"}),
    "audit-foundation": (8, "audit-foundation", set()),
    "observability": (8, "observability", set()),
    "proxy-foundation": (7, "proxy-foundation", set()),
    "audit": (8, "audit-storage", {"storageAccountName", "virtualNetworkName", "privateEndpointSubnetName", "logAnalyticsWorkspaceName", "cmkVaultName", "cmkKeyName", "writerPrincipalId", "readerPrincipalId", "retentionPrincipalId", "recoveryPrincipalId"}),
    "origin": (9, "edge-origin", {"privateLinkServiceName", "virtualNetworkName", "ingressSubnetName", "apiLoadBalancer"}),
    "edge": (9, "edge", {"privateOrigin", "logAnalyticsWorkspaceName", "rateLimitPerMinute"}),
}
REQUIRED = {
    "bootstrap": set(),
    "network": {"virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix"},
    "legacy-logging": set(),
    "backup": {"logAnalyticsWorkspaceName", "backupOwnerPrincipalId", "virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix"},
    "monitoring": {"logAnalyticsWorkspaceName"},
    "platform": {"containerRegistryName", "logAnalyticsWorkspaceName", "stage4Network", "stage4Aks"},
    "audit-foundation": set(),
    "observability": set(),
    "proxy-foundation": set(),
    "audit": {"virtualNetworkName", "privateEndpointSubnetName", "logAnalyticsWorkspaceName", "cmkVaultName", "cmkKeyName", "writerPrincipalId", "readerPrincipalId", "retentionPrincipalId"},
    "origin": {"virtualNetworkName", "ingressSubnetName", "apiLoadBalancer"},
    "edge": {"privateOrigin", "logAnalyticsWorkspaceName"},
}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def approval_policy(config):
    policy = config.get("governance", {})
    require(isinstance(policy, dict) and not set(policy) - {"approvalMode", "approverObjectIds", "singleOperatorRiskAccepted"}, "Invalid governance fields")
    mode = policy.get("approvalMode", "dual")
    require(mode in {"dual", "single-operator"}, "Invalid approvalMode")
    allowed = policy.get("approverObjectIds", [])
    require(isinstance(allowed, list), "approverObjectIds must be an array")
    identities = {UUID(value) for value in allowed}
    require(len(identities) == len(allowed) and all(identity.int for identity in identities), "Invalid or duplicate approved operator IDs")
    if mode == "single-operator":
        require(policy.get("singleOperatorRiskAccepted") is True and len(identities) == 1, "Single-operator mode requires one named operator and explicit risk acceptance")
    elif allowed:
        require(len(identities) >= 2, "Dual approval requires at least two eligible approvers")
    return mode, identities


def deployment_mode(config):
    mode = config.get("deploymentMode", "migration")
    require(mode in {"migration", "greenfield"}, "deploymentMode must be migration or greenfield")
    return mode


def active_stages(config):
    return tuple(stage for stage in range(10) if deployment_mode(config) != "greenfield" or stage != 1)


def stage_title(stage, config):
    require(stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    if deployment_mode(config) == "greenfield":
        return {0: "New environment prerequisites", 5: "Data platform and database initialization", 9: "Approved pilot and first release"}.get(stage, STAGES[stage][0])
    return STAGES[stage][0]


def stage_checks(stage, config):
    require(stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    checks = list(STAGES[stage][1])
    if deployment_mode(config) == "greenfield":
        replacements = {
            0: ("subscription_scope", "deployment_permissions", "private_runner", "domain_ownership"),
            5: ("database_schema", "application_secret_recovery", "redis_entra", "csi_rotation"),
            6: ("replica_failure", "load_affinity", "capacity_limits", "target_database_scope"),
        }
        checks = list(replacements.get(stage, checks))
    if stage == 9 and approval_policy(config)[0] == "single-operator":
        checks[-1] = "single_operator_release"
    return checks


def stage_fingerprint(config, stage):
    scoped = {key: value for key, value in config.items() if key != "parameters"}
    if stage < 9:
        scoped.pop("dns", None)
    if stage < 8:
        scoped.pop("auditRuntime", None)
        scoped.pop("observability", None)
        scoped.pop("auditGovernance", None)
    if stage < 7:
        scoped.pop("proxy", None)
        scoped.pop("entra", None)
    if stage < 6:
        scoped.pop("application", None)
    if stage < 5:
        scoped.pop("databaseAccess", None)
    if stage < 4:
        scoped.pop("privateIngress", None)
        scoped.pop("certificates", None)
    scoped["bootstrapWorkspaceName"] = config["parameters"].get("backup", config["parameters"].get("platform", {})).get("logAnalyticsWorkspaceName")
    scoped["parameters"] = {}
    for component, values in config["parameters"].items():
        if COMPONENTS[component][0] > stage:
            continue
        selected = dict(values)
        if component == "platform" and stage < 5:
            selected.pop("stage5Data", None)
        if component == "platform" and stage < 4:
            selected.pop("azureOpenAIConnections", None)
        scoped["parameters"][component] = selected
    return fingerprint(scoped)


class MigrationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise MigrationError(message)


def configured(value):
    if isinstance(value, dict):
        return all(configured(item) for item in value.values())
    if isinstance(value, list):
        return all(configured(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip()) and not re.search(r"REPLACE|example\.(com|net|org)|example-workspace|stage3check|<|>", value, re.I)
    return value is not None


def validate_config(config, environment):
    require(isinstance(config, dict) and config.get("schemaVersion") == 1, "Unsupported customer configuration")
    mode = deployment_mode(config)
    required_fields = {"schemaVersion", "environment", "azure", "location", "baseDomain", "ownerEmail", "target", "parameters"}
    if mode == "migration":
        required_fields.add("legacy")
    require(set(config) - {"governance", "deploymentMode", "privateIngress", "databaseAccess", "application", "proxy", "entra", "auditRuntime", "observability", "auditGovernance", "dns", "certificates"} == required_fields, "Unexpected or missing customer configuration fields; greenfield must omit legacy")
    if "certificates" in config:
        from scripts.certificate_runtime import certificate_settings
        certificate_settings(config)
    if "dns" in config:
        from scripts.dns_runtime import dns_settings
        dns_settings(config)
    if "auditGovernance" in config:
        from scripts.audit_governance import governance_settings
        governance_settings(config)
    if "observability" in config:
        from scripts.observability import telemetry_settings
        require("auditRuntime" in config, "Managed observability requires auditRuntime")
        telemetry_settings(config)
    if "auditRuntime" in config:
        from scripts.audit_manifest import audit_settings
        audit_settings(config)
    if "proxy" in config:
        from scripts.proxy_config import proxy_settings
        proxy_settings(config)
    if "entra" in config:
        from scripts.entra_apps import entra_settings
        entra_settings(config)
    if "application" in config:
        from scripts.backend_manifest import application_settings
        application_settings(config)
    if "databaseAccess" in config:
        access = config["databaseAccess"]
        require(isinstance(access, dict) and set(access) == {"migrationPrincipalId"} and isinstance(access["migrationPrincipalId"], str) and UUID(access["migrationPrincipalId"]).int != 0, "databaseAccess requires a nonzero migration service principal Object ID")
    if "privateIngress" in config:
        from scripts.private_ingress import ingress_settings
        ingress_settings(config)
    approval_policy(config)
    require(environment in {"dev", "test", "prod"} and config["environment"] == environment, "Environment mismatch")
    require(set(config["azure"]) == {"tenantId", "subscriptionId"}, "Invalid Azure scope fields")
    require(all(UUID(value).int for value in config["azure"].values()), "Nonzero Azure scope identifiers are required")
    require(configured({key: config[key] for key in ("location", "baseDomain", "ownerEmail", "target")}), "Replace customer configuration placeholders")
    domain_hosts(config["baseDomain"])
    require(re.fullmatch(r"[a-z0-9]+", config["location"]) is not None, "Invalid location")
    require(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", config["ownerEmail"]) is not None, "Invalid ownerEmail")
    require(set(config["target"]) == {"resourceGroup"}, "Invalid target fields")
    groups = [config["target"]["resourceGroup"]]
    if mode == "migration":
        require(isinstance(config["legacy"], dict) and set(config["legacy"]) == {"resourceGroup", "aksClusterName", "namespace", "postgresPvc"}, "Invalid legacy inventory fields")
        require(configured(config["legacy"]), "Replace legacy inventory placeholders")
        require(config["legacy"]["resourceGroup"].lower() != config["target"]["resourceGroup"].lower(), "Target must use a separate resource group; in-place migration is not supported")
        groups.append(config["legacy"]["resourceGroup"])
    for group in groups:
        require(re.fullmatch(r"[a-zA-Z0-9_().-]{1,90}", group) is not None and not group.endswith("."), "Invalid resource group name")
    require(isinstance(config["parameters"], dict) and not set(config["parameters"]) - COMPONENTS.keys(), "Unknown parameter component")
    require(not {"network", "backup"}.issubset(config["parameters"]), "Choose one bootstrap network owner: network or backup, never both")
    for component, parameters in config["parameters"].items():
        require(isinstance(parameters, dict) and not set(parameters) - COMPONENTS[component][2], "Unknown or reserved component parameter")
        require(mode != "greenfield" or component not in {"monitoring", "legacy-logging"}, "Legacy monitoring components do not apply to greenfield")
    network = config["parameters"].get("network")
    if network is not None:
        require(REQUIRED["network"].issubset(network), "Network bootstrap configuration is incomplete")
        if configured(network):
            address = ipaddress.ip_network(network["virtualNetworkAddressPrefix"])
            endpoint = ipaddress.ip_network(network["privateEndpointSubnetPrefix"])
            require(address.version == endpoint.version == 4 and endpoint.subnet_of(address), "Private Endpoint subnet must be inside the IPv4 target VNet")
        platform_network = config["parameters"].get("platform", {}).get("stage4Network", {})
        for key in ("virtualNetworkName", "privateEndpointSubnetName"):
            require(key not in platform_network or platform_network[key] == network[key], "Platform network names must match network bootstrap")
    return config


def validate_evidence(evidence, stage, config, revision, now=None):
    require(stage == 10 or stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    require(re.fullmatch(r"[0-9a-f]{40}", revision or "") is not None, "A full reviewed Git revision is required")
    require(isinstance(evidence, list), "Evidence must be an array")
    now = now or datetime.now(timezone.utc)
    mode, eligible = approval_policy(config)
    required_approvers = 1 if mode == "single-operator" else 2
    completed = set()
    for item in evidence:
        require(isinstance(item, dict), "Invalid evidence record")
        previous = item.get("stage")
        require(type(previous) is int and 0 <= previous <= 9 and previous not in completed, "Invalid or duplicate evidence stage")
        binding = item.get("binding", "full-config")
        require(binding in {"full-config", "stage-config"}, "Invalid evidence binding")
        expected_hash = stage_fingerprint(config, previous) if binding == "stage-config" else fingerprint(config)
        require(item.get("environment") == config["environment"] and item.get("configSha256") == expected_hash and item.get("revision") == revision, "Evidence configuration or revision mismatch")
        require(item.get("approvalMode", "dual") == mode, "Evidence approval policy mismatch")
        require(item.get("status") == "passed" and set(item.get("checks", [])) == set(stage_checks(previous, config)), "Stage checks are incomplete")
        approvers = item.get("approvedBy", [])
        require(isinstance(approvers, list) and len(approvers) == required_approvers, "Approval count does not match configured policy")
        try:
            identities = {UUID(value) for value in approvers}
            observed = datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            raise ValueError("Invalid evidence approval or timestamp") from None
        require(len(identities) == required_approvers and all(identity.int for identity in identities), "Distinct nonzero approver object IDs are required")
        require(not eligible or identities.issubset(eligible), "Evidence contains an ineligible approver")
        require(observed.tzinfo is not None and now - timedelta(days=7) <= observed <= now, "Evidence must be from the last seven days")
        report = urlsplit(item.get("reportUrl", ""))
        require(report.scheme == "https" and bool(report.hostname) and not report.username and not report.password and not report.query, "Report must be a private HTTPS reference without embedded credentials or query tokens")
        require(re.fullmatch(r"[0-9a-f]{64}", item.get("reportSha256", "")) is not None, "Report hash is required")
        completed.add(previous)
    require({previous for previous in active_stages(config) if previous < stage}.issubset(completed), "Prior stage evidence is missing; do not skip migration gates")
    return completed


def parameters_for(config, stage, component):
    require(stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    require(component in COMPONENTS, "Select an IaC component")
    first_stage, template, _allowed = COMPONENTS[component]
    require(stage in ({3, 4, 5} if component == "platform" else {first_stage}), "Component does not belong to the selected stage")
    parameters = dict(config["parameters"].get(component, {}))
    if component == "platform" and stage < 5:
        parameters.pop("stage5Data", None)
    if component == "platform" and stage < 4:
        parameters.pop("azureOpenAIConnections", None)
    require(REQUIRED[component].issubset(parameters) and configured(parameters), "Component configuration is missing or contains placeholders")
    if component == "bootstrap":
        logging = config["parameters"].get("backup", config["parameters"].get("platform", {}))
        workspace_name = logging.get("logAnalyticsWorkspaceName", "")
        require(configured(workspace_name), "Target Log Analytics workspace name is required for bootstrap")
        parameters.update(resourceGroupName=config["target"]["resourceGroup"], logAnalyticsWorkspaceName=workspace_name)
    elif component == "legacy-logging":
        workspace_name = config["parameters"].get("monitoring", {}).get("logAnalyticsWorkspaceName", "")
        require(configured(workspace_name), "Legacy monitoring workspace name is required")
        parameters["logAnalyticsWorkspaceName"] = workspace_name
    elif component == "platform":
        if stage >= 5:
            require("stage5Data" in parameters, "Stage 5 database configuration is required")
            if "databaseAccess" in config:
                parameters["bootstrapPrincipalId"] = config["databaseAccess"]["migrationPrincipalId"]
        if deployment_mode(config) == "migration":
            require(parameters["stage4Aks"]["name"] != config["legacy"]["aksClusterName"], "New AKS must not reuse the legacy name")
        parameters.update(environmentName=config["environment"], deployContainerRegistry=True, deployStage4=stage >= 4, deployStage5=stage >= 5, containerRegistryPublicNetworkAccess="Disabled")
    elif component == "backup":
        parameters["backupOwnerUpn"] = config["ownerEmail"]
    elif component == "monitoring":
        require(config["legacy"]["namespace"] == "litellm" and config["legacy"]["postgresPvc"] == "pg-data", "Monitoring queries currently require litellm namespace and pg-data PVC; adapt and test queries first")
        parameters.update(aksClusterName=config["legacy"]["aksClusterName"], ownerEmail=config["ownerEmail"])
    elif component == "audit-foundation":
        audit = config["parameters"].get("audit", {})
        keys = ("cmkVaultName", "cmkKeyName", "virtualNetworkName", "privateEndpointSubnetName")
        require(all(configured(audit.get(key, "")) for key in keys), "Audit Vault/key and network names must be configured")
        parameters.update({key: audit[key] for key in keys})
        parameters["aksClusterName"] = config["parameters"]["platform"]["stage4Aks"]["name"]
    elif component == "observability":
        platform = config["parameters"]["platform"]
        network = platform["stage4Network"]
        parameters.update(aksClusterName=platform["stage4Aks"]["name"], logAnalyticsWorkspaceName=platform["logAnalyticsWorkspaceName"], virtualNetworkName=network["virtualNetworkName"], privateEndpointSubnetName=network["privateEndpointSubnetName"])
    elif component == "proxy-foundation":
        platform = config["parameters"]["platform"]
        require("databaseAccess" in config, "Proxy foundation requires an explicitly approved bootstrap identity")
        network = platform["stage4Network"]
        parameters.update(environmentName=config["environment"], aksClusterName=platform["stage4Aks"]["name"], virtualNetworkName=network["virtualNetworkName"], privateEndpointSubnetName=network["privateEndpointSubnetName"], logAnalyticsWorkspaceName=platform["logAnalyticsWorkspaceName"], bootstrapPrincipalId=config["databaseAccess"]["migrationPrincipalId"])
        if "entra" in config:
            parameters["entraBootstrapPrincipalId"] = config["entra"]["bootstrapPrincipalId"]
    elif component == "audit":
        parameters["deployAuditStorage"] = True
    elif component == "origin":
        parameters["deployPrivateOrigin"] = True
    elif component == "edge":
        parameters.update(deployEdge=True, enableApiTraffic=False, environmentName=config["environment"], baseDomain=config["baseDomain"], wafMode="Detection")
    if component != "edge":
        parameters["location"] = config["location"]
    parameters["tags"] = {"owner": config["ownerEmail"], "environment": config["environment"], "workload": "litellm", "managedBy": "bicep"}
    return ROOT / f"infra/{template}/main.bicep", {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in parameters.items()},
    }


def private_write(path, content):
    require(not path.is_symlink(), "Refusing symlink output")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def prepare(config, stage, component, destination):
    destination = destination.resolve()
    require(destination.is_relative_to((ROOT / "temp").resolve()) and destination != (ROOT / "temp").resolve(), "Output must stay under ignored temp/")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    template, parameters = parameters_for(config, stage, component)
    path = destination / "parameters.json"
    private_write(path, json.dumps(parameters, indent=2) + "\n")
    return template, path


def preview(config, template, path, component):
    group = config["legacy" if component in {"monitoring", "legacy-logging"} else "target"]["resourceGroup"]
    scope = ["sub", "what-if", "--location", config["location"]] if component == "bootstrap" else ["group", "what-if", "--resource-group", group]
    result = subprocess.run(["az", "deployment", *scope, "--subscription", config["azure"]["subscriptionId"], "--template-file", str(template), "--parameters", f"@{path}", "--result-format", "FullResourcePayloads", "--no-pretty-print", "--output", "json"], capture_output=True, text=True, check=False)
    private_write(path.parent / "what-if.json", result.stdout)
    private_write(path.parent / "diagnostics.txt", result.stderr)
    require(result.returncode == 0, "What-if failed; detailed diagnostics are private local output")
    result = json.loads(result.stdout)
    require(result.get("status") == "Succeeded" and isinstance(result.get("changes"), list), "Unrecognized What-if response")
    counts = {}
    for change in result["changes"]:
        kind = change.get("changeType", "Unknown")
        counts[kind] = counts.get(kind, 0) + 1
    print(json.dumps({"changeCounts": counts, "deploymentPerformed": False}))
    require(not set(counts) - {"Create", "NoChange", "Ignore"}, "Review required: Modify/Delete/Unsupported/unknown changes block this gate; no deployment performed")
    require(counts.get("Create", 0) > 0, "No creation detected; a disabled or already-deployed preview is not a new-stage deployment approval")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=range(10), required=True)
    parser.add_argument("--mode", choices=("guide", "config-check", "preflight", "what-if"), default="guide")
    parser.add_argument("--component", choices=tuple(COMPONENTS) + ("none",), default="none")
    parser.add_argument("--environment", choices=("dev", "test", "prod"), required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/customer-migration")
    args = parser.parse_args()
    supplied = os.environ.get("CUSTOMER_CONFIG_JSON", "")
    config = json.loads(supplied) if supplied else {}
    title, checks = stage_title(args.stage, config), stage_checks(args.stage, config)
    print(f"Stage {args.stage}: {title}\nRequired exit evidence: {', '.join(checks)}")
    print(f"Deployment mode: {deployment_mode(config)}; applicable stages: {', '.join(map(str, active_stages(config)))}")
    print("Runbook: docs/customer-migration-guide-zh.md; no automatic deployment, DNS change or retirement.")
    if args.mode == "guide":
        return
    config = validate_config(config, args.environment)
    require(os.environ.get("AZURE_TENANT_ID") == config["azure"]["tenantId"] and os.environ.get("AZURE_SUBSCRIPTION_ID") == config["azure"]["subscriptionId"], "OIDC scope does not match customer configuration")
    print(json.dumps({"configSha256": fingerprint(config), "stageConfigSha256": stage_fingerprint(config, args.stage), "stage": args.stage, "environment": args.environment}))
    if args.mode == "config-check":
        if args.component != "none":
            parameters_for(config, args.stage, args.component)
        print("Configuration syntax checked only; evidence, cloud state and deployment readiness were not checked.")
        return
    revision = os.environ.get("GITHUB_SHA", os.environ.get("MIGRATION_REVISION", ""))
    if os.environ.get("MIGRATION_AUTO_EVIDENCE") == "true":
        from scripts.workflow_artifacts import load_evidence
        previous = load_evidence(config, args.stage, revision)
    else:
        previous = json.loads(os.environ.get("MIGRATION_EVIDENCE_JSON") or "[]")
    completed = validate_evidence(previous, args.stage, config, revision)
    require(not (args.component in {"backup", "network"} and any(stage >= 4 for stage in completed)), "Do not reapply the bootstrap VNet after Stage 4")
    if args.component != "none":
        template, path = prepare(config, args.stage, args.component, args.output_dir)
        if args.mode == "what-if":
            preview(config, template, path, args.component)
    else:
        require(args.mode != "what-if", "What-if needs an IaC component")
        if args.stage in (7, 8, 9):
            render(config["baseDomain"], args.output_dir, args.stage)
            print("Domain overlay generated; identity, image and secret placeholders still require integration. Not deployable as-is.")
    print("Readiness metadata checked, not an independent audit of evidence or cloud acceptance.")


if __name__ == "__main__":
    try:
        main()
    except MigrationError as error:
        raise SystemExit(str(error)) from None
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        raise SystemExit("Migration check failed. Verify configuration, stage evidence and private local diagnostics; input values are not logged.") from None