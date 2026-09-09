"""Managed Stage8 audit manifests; deployment never implies observability acceptance."""

import copy
import ipaddress
import json
from uuid import UUID

import yaml

from scripts.customer_migration import ROOT, fingerprint, require
from scripts.migration_deploy import deployment_name, group_id


def audit_settings(config):
    settings = config.get("auditRuntime")
    require(isinstance(settings, dict) and set(settings) == {"retentionDays", "captureEnabled", "retentionEnabled", "deliveryPolicyAccepted"}, "auditRuntime requires retentionDays, captureEnabled, retentionEnabled and deliveryPolicyAccepted")
    require(type(settings["retentionDays"]) is int and 1 <= settings["retentionDays"] <= 30, "Audit retention must be 1 to 30 days")
    require(type(settings["captureEnabled"]) is bool and type(settings["retentionEnabled"]) is bool, "Audit enable decisions must be booleans")
    require(settings["deliveryPolicyAccepted"] is True, "Approve the audit delivery limits, latency and content policy before publishing")
    require("application" in config and "proxy" in config, "Managed audit requires managed backend and proxy configuration")
    return settings


def render_audit_documents(config, source, foundation, storage, subnet, registry=None):
    settings = audit_settings(config)
    require(ipaddress.ip_network(subnet).is_private, "Audit egress must use a private subnet")
    identities = [foundation[role] for role in ("writer", "reader", "retention", "recovery")]
    for field in ("clientId", "principalId"):
        values = [UUID(identity[field]) for identity in identities]
        require(all(value.int for value in values) and len(set(values)) == 4, "Audit identities must be nonzero and distinct")
    require(set(registry or {"approvals.json": "[]", "holds.json": "[]"}) == {"approvals.json", "holds.json"}, "Unexpected audit governance registry fields")
    registry = registry if registry is not None else {"approvals.json": "[]", "holds.json": "[]"}
    require(all(isinstance(json.loads(value), list) for value in registry.values()), "Audit governance registries must be arrays")
    documents = copy.deepcopy(source)
    metadata = lambda name: {"name": name, "namespace": "litellm"}
    policy_maps = [item for item in documents if "policy.json" in item.get("data", {})]
    require(len(policy_maps) == 1, "Exactly one generated proxy policy is required")
    policy_map = policy_maps[0]
    policy = json.loads(policy_map["data"]["policy.json"])
    if settings["captureEnabled"]:
        require(any(binding.get("plane") == "api" and binding.get("audit", {}).get("capture") is True and not binding.get("disabled") for binding in policy["bindings"]), "Declare auditTeamId for an enabled API binding before enabling capture")
    for plane, role in (("api", "writer"), ("admin", "reader")):
        account = "llm-" + plane + "-proxy"
        identity = foundation[role]
        require(identity.get("serviceAccountName") == account and identity.get("kubernetesNamespace") == "litellm", "Audit identity federation belongs to another plane")
        service_account = next(item for item in documents if item["kind"] == "ServiceAccount" and item["metadata"]["name"] == account)
        proxy_id = service_account["metadata"]["annotations"]["azure.workload.identity/client-id"]
        require(UUID(proxy_id) not in {UUID(value["clientId"]) for value in identities}, "Audit identities cannot replace proxy credential identities")
        stage8 = {"l3": {"enabled": settings["captureEnabled"] if plane == "api" else True, "deliveryMode": "persist-before-forward", "clientId": identity["clientId"], "retentionDays": settings["retentionDays"], **storage}, "telemetry": {"enabled": False}, "guardrail": {"enabled": False}}
        name = "stage8-" + plane + "-" + fingerprint(stage8)[:12]
        documents.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(name), "data": {"config.json": json.dumps(stage8, sort_keys=True)}})
        deployment = next(item for item in documents if item["kind"] == "Deployment" and item["metadata"]["name"] == account)
        pod = deployment["spec"]["template"]["spec"]
        container = next(item for item in pod["containers"] if item["name"] == "auth-proxy")
        require(not any(item["name"] == "stage8" for item in pod.get("volumes", [])), "Stage8 input already contains audit configuration")
        pod.setdefault("volumes", []).append({"name": "stage8", "configMap": {"name": name}})
        container.setdefault("env", []).append({"name": "STAGE8_CONFIG", "value": "/etc/stage8/config.json"})
        container.setdefault("volumeMounts", []).append({"name": "stage8", "mountPath": "/etc/stage8", "readOnly": True})
        if plane == "api":
            container["resources"]["requests"]["memory"] = "256Mi"
            container["resources"]["limits"]["memory"] = "1Gi"
        else:
            pod["volumes"].append({"name": "approvals", "configMap": {"name": "audit-approvals"}})
            container["volumeMounts"].append({"name": "approvals", "mountPath": "/etc/audit-approvals", "readOnly": True})
    documents.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata("audit-approvals"), "data": copy.deepcopy(registry)})
    retention_config = {"l3": {"enabled": True, "deliveryMode": "persist-before-forward", "retentionDays": settings["retentionDays"], **storage}}
    retention_name = "stage8-retention-" + fingerprint(retention_config)[:12]
    documents.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(retention_name), "data": {"config.json": json.dumps(retention_config, sort_keys=True)}})
    for item in yaml.safe_load_all((ROOT / "deploy/components/stage8-audit/maintenance.yaml").read_text()):
        item["metadata"]["namespace"] = "litellm"
        if item["kind"] == "ServiceAccount":
            identity = foundation["retention"]
            require(identity.get("serviceAccountName") == "l3-retention" and identity.get("kubernetesNamespace") == "litellm", "Retention federation does not match")
            item["metadata"]["annotations"]["azure.workload.identity/client-id"] = identity["clientId"]
        else:
            item["spec"]["suspend"] = not settings["retentionEnabled"]
            pod = item["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            pod["securityContext"]["fsGroup"] = 10001
            pod["containers"][0]["image"] = config["proxy"]["image"]
            next(volume for volume in pod["volumes"] if volume["name"] == "stage8")["configMap"]["name"] = retention_name
        documents.append(item)
    network = list(yaml.safe_load_all((ROOT / "deploy/components/stage8-audit/networkpolicy.yaml").read_text()))
    for item in network:
        if item["metadata"]["name"] == "stage8-collector":
            continue
        item["metadata"]["namespace"] = "litellm"
        if item["metadata"]["name"] == "stage8-proxy-egress":
            item["spec"]["egress"] = [{"to": [{"ipBlock": {"cidr": subnet}}], "ports": [{"protocol": "TCP", "port": 443}]}]
        else:
            for rule in item["spec"]["egress"]:
                for target in rule.get("to", []):
                    if target.get("ipBlock", {}).get("cidr") == "10.30.8.0/24":
                        target["ipBlock"]["cidr"] = subnet
        documents.append(item)
    return documents


def prepare_audit_documents(config, revision, directory, azure, source, kube):
    from scripts.audit_runtime import AuditCluster

    audit_settings(config)
    group = config["target"]["resourceGroup"]
    base = group_id(config)
    foundation_output = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 8, "audit-foundation"), "--query", "{state:properties.provisioningState,foundation:properties.outputs.auditFoundation.value}"])
    require(foundation_output.get("state") == "Succeeded", "Deploy audit-foundation before Stage8 publishing")
    foundation = foundation_output["foundation"]
    roles = {"writer": "llm-api-proxy", "reader": "llm-admin-proxy", "retention": "l3-retention", "recovery": "l3-recovery"}
    require(set(roles).issubset(foundation), "Redeploy audit-foundation to provide four separate audit identities")
    cluster = azure.scoped(["aks", "show", "--resource-group", group, "--name", config["parameters"]["platform"]["stage4Aks"]["name"]])
    require(cluster.get("apiServerAccessProfile", {}).get("enablePrivateCluster") is True, "Stage8 requires private AKS")
    issuer = cluster["oidcIssuerProfile"]["issuerUrl"]
    for role, account in roles.items():
        identity = foundation[role]
        require(identity.get("serviceAccountName") == account and identity.get("kubernetesNamespace") == "litellm", "Audit federation account differs from its approved role")
        require(identity["id"].lower() == (base + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/" + identity["name"]).lower(), "Audit identity is outside the target resource group")
        live = azure.scoped(["identity", "show", "--ids", identity["id"]])
        require(all(live.get(key) == identity[key] for key in ("clientId", "principalId")), "Audit identity changed since foundation deployment")
        federation = azure.scoped(["identity", "federated-credential", "list", "--identity-name", identity["name"], "--resource-group", group])
        require(len(federation) == 1 and federation[0].get("issuer") == issuer and federation[0].get("subject") == "system:serviceaccount:litellm:" + account and federation[0].get("audiences") == ["api://AzureADTokenExchange"], "Audit identity federation changed or trusts additional subjects")
    output = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 8, "audit"), "--query", "{state:properties.provisioningState,storageId:properties.outputs.storageResourceId.value,storageUrl:properties.outputs.storageUrl.value,parameters:properties.parameters}"])
    require(output.get("state") == "Succeeded", "Deploy audit storage before Stage8 publishing")
    for role in roles:
        require(output.get("parameters", {}).get(role + "PrincipalId", {}).get("value", "").lower() == foundation[role]["principalId"].lower(), "Audit storage permissions were not deployed for the current four identities")
    storage_id = output["storageId"]
    require(storage_id.lower().startswith(base.lower() + "/providers/microsoft.storage/storageaccounts/"), "Audit storage is outside the approved group")
    storage = azure.scoped(["storage", "account", "show", "--ids", storage_id])
    storage_url = output["storageUrl"].rstrip("/")
    require(storage.get("publicNetworkAccess") == "Disabled" and storage.get("allowSharedKeyAccess") is False and storage.get("allowBlobPublicAccess") is False, "Audit storage must be private and Entra-only")
    require(storage["primaryEndpoints"]["blob"].rstrip("/") == storage_url and storage_url == "https://" + storage_id.split("/")[-1] + ".blob.core.windows.net", "Audit storage URL does not match its resource")
    service = azure.scoped(["rest", "--method", "get", "--url", "https://management.azure.com" + storage_id + "/blobServices/default?api-version=2025-06-01"])["properties"]
    require(service.get("isVersioningEnabled") is False and service.get("deleteRetentionPolicy", {}).get("enabled") is False and service.get("containerDeleteRetentionPolicy", {}).get("enabled") is False, "Audit deletion policy must explicitly exclude hidden retained copies")
    audit = config["parameters"]["audit"]
    subnet_id = base + "/providers/Microsoft.Network/virtualNetworks/" + audit["virtualNetworkName"] + "/subnets/" + audit["privateEndpointSubnetName"]
    endpoints = azure.scoped(["network", "private-endpoint", "list", "--resource-group", group])
    matches = []
    for endpoint in endpoints:
        properties = endpoint.get("properties", endpoint)
        for connection in properties.get("privateLinkServiceConnections", []):
            link = connection.get("properties", connection)
            if link.get("privateLinkServiceId", "").lower() == storage_id.lower() and "blob" in link.get("groupIds", []):
                require(link.get("privateLinkServiceConnectionState", {}).get("status") == "Approved" and properties["subnet"]["id"].lower() == subnet_id.lower(), "Audit endpoint is unapproved or uses another subnet")
                matches.append(endpoint)
    require(len(matches) == 1, "Expected one approved Blob endpoint in the target group")
    subnet = azure.scoped(["network", "vnet", "subnet", "show", "--ids", subnet_id])
    cidr = subnet.get("addressPrefix") or subnet["addressPrefixes"][0]
    client = AuditCluster(kube, directory)
    for account in ("llm-api-proxy", "llm-admin-proxy"):
        existing = client.get("deployment", account, optional=True)
        if existing:
            for volume in existing["spec"]["template"]["spec"].get("volumes", []):
                if volume.get("name") == "stage8" and "configMap" in volume:
                    current = json.loads(client.get("configmap", volume["configMap"]["name"])["data"]["config.json"])
                    require((current.get("telemetry", {}).get("enabled") is not True or "observability" in config) and current.get("guardrail", {}).get("enabled") is not True, "Audit-only generation cannot disable existing telemetry or guardrails")
    registry = client.get("configmap", "audit-approvals", optional=True)
    documents = render_audit_documents(config, source, foundation, {"storageResourceId": storage_id, "storageUrl": storage_url}, cidr, registry["data"] if registry else None)
    if registry:
        documents = [document for document in documents if not (document["kind"] == "ConfigMap" and document["metadata"]["name"] == "audit-approvals")]
    if "observability" in config:
        from scripts.observability import prepare_observability
        documents = prepare_observability(config, documents, azure, cidr)
    return documents