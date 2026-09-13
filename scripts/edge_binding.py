"""Bind a managed API proxy to its deployed Front Door before traffic release."""

import copy
import json
import re
from uuid import UUID

from scripts.customer_migration import fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id


def front_door_id(deployment):
    if not deployment:
        return None
    values = [item for container in deployment["spec"]["template"]["spec"]["containers"] for item in container.get("env", []) if item.get("name") == "FRONT_DOOR_ID"]
    require(len(values) <= 1, "Duplicate Front Door identity configuration")
    if not values:
        return None
    value = values[0].get("value")
    require(set(values[0]) == {"name", "value"} and isinstance(value, str) and UUID(value).int != 0, "Front Door ID must be an explicit nonzero UUID")
    return value


def bind_deployment(deployment, identifier):
    require(isinstance(identifier, str) and UUID(identifier).int != 0, "Invalid deployed Front Door identity")
    require(deployment["metadata"]["name"] == "llm-api-proxy", "Only the API proxy can bind Front Door")
    existing = front_door_id(deployment)
    require(existing is None or existing.lower() == identifier.lower(), "Existing API proxy trusts another Front Door; review that change separately")
    desired = copy.deepcopy(deployment)
    containers = desired["spec"]["template"]["spec"]["containers"]
    require(len(containers) == 1 and containers[0]["name"] == "auth-proxy", "Unexpected managed API proxy container set")
    if existing is None:
        containers[0].setdefault("env", []).append({"name": "FRONT_DOOR_ID", "value": identifier})
    return desired


def preserve_binding(documents, live):
    identifier = front_door_id(live)
    result = copy.deepcopy(documents)
    for index, item in enumerate(result):
        if item["kind"] != "Deployment":
            continue
        if item["metadata"]["name"] == "llm-admin-proxy":
            require(front_door_id(item) is None, "Admin proxy must not be bound to a public Front Door")
        elif item["metadata"]["name"] == "llm-api-proxy" and identifier:
            result[index] = bind_deployment(item, identifier)
    return result


def deployed_edge(config, azure):
    output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge"), "--query", "{state:properties.provisioningState,edge:properties.outputs.edge.value}"])
    edge = output.get("edge", {})
    require(output.get("state") == "Succeeded" and edge.get("provisioned") is True and edge.get("apiHost") == "llm-api." + config["baseDomain"], "Provision the matching disabled Front Door before binding")
    route = edge.get("routeId", "")
    prefix = group_id(config) + "/providers/Microsoft.Cdn/profiles/"
    match = re.fullmatch(re.escape(prefix) + r"([A-Za-z0-9-]+)/afdEndpoints/[A-Za-z0-9-]+/routes/[A-Za-z0-9-]+", route, re.I)
    require(match is not None, "Front Door route belongs to another target scope")
    resource_id = prefix + match[1]
    profile = azure.scoped(["resource", "show", "--ids", resource_id, "--api-version", "2025-04-15"])
    identifier = edge.get("profileId")
    require(isinstance(identifier, str) and UUID(identifier).int != 0 and profile.get("id", "").lower() == resource_id.lower() and profile.get("properties", {}).get("frontDoorId") == identifier, "Actual Front Door identity differs from deployment outputs")
    return {"profileResourceId": resource_id, "frontDoorId": identifier, "apiHost": edge["apiHost"]}


def bind_edge(config, operation, revision, directory, approved, azure=None, client=None):
    from scripts.audit_runtime import AuditCluster
    from scripts.migration_runtime import connect_cluster, validate_action

    require(operation in {"plan", "execute"}, "Invalid edge binding operation")
    validate_action(config, 9, "edge-bind")
    azure = azure or AzureCommands(config, directory)
    edge = deployed_edge(config, azure)
    client = client or AuditCluster(connect_cluster(config, directory, legacy=False), directory)
    current = client.get("deployment", "llm-api-proxy")
    admin = client.get("deployment", "llm-admin-proxy")
    require(front_door_id(admin) is None, "Admin proxy must not trust Front Door")
    desired = bind_deployment(current, edge["frontDoorId"])
    pod = desired["spec"]["template"]["spec"]
    require(pod.get("serviceAccountName") == "llm-api-proxy" and pod["containers"][0].get("image") == config.get("proxy", {}).get("image"), "Bind only the approved managed API workload and image")
    scope = {"stage": 9, "action": "edge-bind", "revision": revision, "configSha256": stage_fingerprint(config, 9),
             "clusterResourceId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"], **edge}
    plan = {**scope, "deploymentUid": current["metadata"]["uid"], "currentSpecSha256": fingerprint(current["spec"]), "desiredSpecSha256": fingerprint(desired["spec"])}
    digest = fingerprint(plan)
    summary = {**scope, "planSha256": digest, "applied": False, "stageAccepted": False, "trafficVerified": False}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    if operation == "plan":
        return summary
    require(digest == approved, "Edge binding plan changed or was not approved")
    if current["spec"] != desired["spec"]:
        client.patch("deployment", "llm-api-proxy", [{"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]}, {"op": "test", "path": "/spec", "value": current["spec"]}, {"op": "replace", "path": "/spec", "value": desired["spec"]}])
    client.run(["rollout", "status", "deployment/llm-api-proxy", "--timeout=15m"])
    after = client.get("deployment", "llm-api-proxy")
    require(after["metadata"]["uid"] == plan["deploymentUid"] and after["spec"] == desired["spec"] and front_door_id(after) == edge["frontDoorId"], "API binding changed during rollout; do not release traffic")
    receipt = {**scope, "deploymentUid": after["metadata"]["uid"], "podTemplateSha256": fingerprint(after["spec"]["template"]), "rolloutVerified": True}
    template = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"edgeBinding": {"type": "object", "value": receipt}}}
    path = directory / "edge-binding-receipt.json"
    private_write(path, json.dumps(template))
    saved = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge-bind"), "--mode", "Incremental", "--template-file", str(path)])
    require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "API binding applied but receipt storage failed; replan before release")
    summary.update(applied=True, rolloutVerified=True)
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    return summary


def require_edge_binding(config, revision, identifier, azure):
    result = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge-bind"), "--query", "{state:properties.provisioningState,binding:properties.outputs.edgeBinding.value}"])
    receipt = result.get("binding", {})
    require(result.get("state") == "Succeeded" and receipt.get("rolloutVerified") is True, "Run edge-bind and verify its rollout before enabling traffic")
    expected = {"revision": revision, "configSha256": stage_fingerprint(config, 9), "frontDoorId": identifier,
                "apiHost": "llm-api." + config["baseDomain"], "clusterResourceId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"]}
    require(all(receipt.get(key) == value for key, value in expected.items()), "Edge binding receipt is stale or belongs to a different environment")
    require(isinstance(receipt.get("deploymentUid"), str) and bool(receipt["deploymentUid"]) and re.fullmatch(r"[a-f0-9]{64}", receipt.get("podTemplateSha256", "")), "Edge binding receipt lacks the verified API workload")