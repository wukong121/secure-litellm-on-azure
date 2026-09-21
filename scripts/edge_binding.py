"""Bind a managed API proxy to its deployed Front Door before traffic release."""

import copy
import json
import re
from uuid import UUID

import yaml

from scripts.customer_migration import fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.private_ingress import native_api_rule, native_health_rule, source_ranges


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


def native_ingress_state(config, client, identifier=None):
    config_map = client.get("configmap", "llm-api-ingress")
    deployment = client.get("deployment", "llm-api-ingress")
    service = client.get("service", "llm-api-ingress")
    return native_ingress_document_state(config, config_map, deployment, service, identifier)


def native_ingress_document_state(config, config_map, deployment, service, identifier=None):
    for item in (config_map, deployment, service):
        require(item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/managed-by") == "llmgw-workflow", "Native API ingress must remain workflow managed")
    dynamic = yaml.safe_load(config_map.get("data", {}).get("routes.yaml", ""))
    require(isinstance(dynamic, dict), "Native API ingress route configuration is missing")
    http = dynamic.get("http", {})
    routers = http.get("routers", {})
    require(set(routers) == {"api", "api-health"}, "Native API ingress contains an unexpected router")
    api = routers["api"]
    health = routers["api-health"]
    rule = api.get("rule", "")
    bound = re.findall(r"HeaderRegexp\(`X-Azure-FDID`,\s*`\(\?i\)\^([a-fA-F0-9-]+)\$`\)", rule)
    require(len(bound) <= 1 and (not bound or UUID(bound[0]).int != 0), "Native API ingress has an invalid Front Door header binding")
    host = "llm-api." + config["baseDomain"]
    require(api == {"rule": native_api_rule(host, bound[0] if bound else None), "entryPoints": ["websecure"], "service": "api", "tls": {}}, "Native API ingress does not enforce the exact reviewed inference routes")
    require(health == {"rule": native_health_rule(host), "entryPoints": ["websecure"], "service": "api", "middlewares": ["api-health-path"], "tls": {}}, "Native API ingress health router differs from the reviewed contract")
    if identifier is not None:
        require(bound and bound[0].lower() == identifier.lower(), "Native API ingress trusts another or no Front Door")
    require(http.get("middlewares") == {"api-health-path": {"replacePath": {"path": "/health/readiness"}}}, "Native API ingress middleware differs from the reviewed contract")
    require(http.get("services") == {"api": {"loadBalancer": {"servers": [{"url": "http://litellm.litellm.svc.cluster.local:4000"}], "passHostHeader": True}}}, "Native API ingress targets an unexpected service")
    service_spec = service.get("spec", {})
    expected_sources = source_ranges([*config["privateIngress"]["api"]["allowedCidrs"], config["parameters"]["platform"]["stage4Network"]["ingressSubnetPrefix"]])
    expected_labels = {"app.kubernetes.io/name": "llmgw-ingress", "app.kubernetes.io/component": "controller", "app.kubernetes.io/managed-by": "llmgw-workflow", "plane": "api"}
    annotations = service.get("metadata", {}).get("annotations", {})
    ports = service_spec.get("ports", [])
    port_contract = len(ports) == 1 and all(ports[0].get(key) == value for key, value in {"name": "https", "port": 443, "targetPort": "https", "protocol": "TCP"}.items())
    service_contract = (
        service_spec.get("type") == "LoadBalancer"
        and service_spec.get("selector") == expected_labels
        and service_spec.get("externalTrafficPolicy") == "Local"
        and service_spec.get("loadBalancerSourceRanges") == expected_sources
        and not service_spec.get("externalIPs")
        and not service_spec.get("loadBalancerIP")
        and not service_spec.get("loadBalancerClass")
        and port_contract
        and annotations.get("service.beta.kubernetes.io/azure-load-balancer-internal") == "true"
        and annotations.get("service.beta.kubernetes.io/azure-load-balancer-internal-subnet") == config["parameters"]["platform"]["stage4Network"]["ingressSubnetName"]
    )
    require(service_contract, "Native API ingress Service differs from the reviewed private routing contract")
    labels = deployment.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
    require(labels == expected_labels, "Native API ingress deployment labels changed")
    pod = deployment["spec"]["template"]["spec"]
    dynamic_volumes = [item for item in pod.get("volumes", []) if item.get("name") == "dynamic"]
    require(dynamic_volumes == [{"name": "dynamic", "configMap": {"name": "llm-api-ingress"}}], "Native API ingress does not mount the reviewed route ConfigMap")
    containers = pod.get("containers", [])
    require(len(containers) == 1 and containers[0].get("name") == "traefik" and {"name": "dynamic", "mountPath": "/dynamic", "readOnly": True} in containers[0].get("volumeMounts", []), "Native API ingress controller does not read the reviewed route ConfigMap")
    return {
        "bindingMode": "native-private-ingress",
        "configMapUid": config_map["metadata"]["uid"],
        "deploymentUid": deployment["metadata"]["uid"],
        "serviceUid": service["metadata"]["uid"],
        "serviceSpecSha256": fingerprint(service["spec"]),
        "podTemplateSha256": fingerprint(deployment["spec"]["template"]),
        "routeConfigSha256": fingerprint(dynamic),
        "frontDoorHeaderBound": bound[0] if bound else None,
    }


def bind_native_ingress(config, client, identifier):
    require(isinstance(identifier, str) and UUID(identifier).int != 0, "Invalid deployed Front Door identity")
    config_map = client.get("configmap", "llm-api-ingress")
    deployment = client.get("deployment", "llm-api-ingress")
    service = client.get("service", "llm-api-ingress")
    current = native_ingress_document_state(config, config_map, deployment, service)
    require(current["frontDoorHeaderBound"] is None or current["frontDoorHeaderBound"].lower() == identifier.lower(), "Existing native API ingress trusts another Front Door")
    desired_config_map = copy.deepcopy(config_map)
    desired_deployment = copy.deepcopy(deployment)
    dynamic = yaml.safe_load(desired_config_map["data"]["routes.yaml"])
    rule = dynamic["http"]["routers"]["api"]["rule"]
    if current["frontDoorHeaderBound"] is None:
        dynamic["http"]["routers"]["api"]["rule"] = native_api_rule("llm-api." + config["baseDomain"], identifier)
    desired_config_map["data"]["routes.yaml"] = yaml.safe_dump(dynamic, sort_keys=False)
    annotations = desired_deployment["spec"]["template"]["metadata"].setdefault("annotations", {})
    annotations["llmgw/front-door-id"] = identifier
    annotations["llmgw/routes-sha256"] = fingerprint(dynamic)
    desired = native_ingress_document_state(config, desired_config_map, desired_deployment, service, identifier)
    return config_map, deployment, desired_config_map, desired_deployment, current, desired


def bind_edge(config, operation, revision, directory, approved, azure=None, client=None):
    from scripts.audit_runtime import AuditCluster
    from scripts.migration_runtime import connect_cluster, validate_action

    require(operation in {"plan", "execute"}, "Invalid edge binding operation")
    validate_action(config, 9, "edge-bind")
    azure = azure or AzureCommands(config, directory)
    edge = deployed_edge(config, azure)
    from scripts.backend_manifest import application_authentication
    native = application_authentication(config)["mode"] == "native"
    if client is None:
        kube = connect_cluster(config, directory, legacy=False)
        if native and kube[-2:] == ["--namespace", "litellm"]:
            kube = kube[:-2]
        client = AuditCluster([*kube, "--namespace", "llm-api-ingress"] if native else kube, directory)
    if native:
        current_config_map, current_deployment, desired_config_map, desired_deployment, current_state, desired_state = bind_native_ingress(config, client, edge["frontDoorId"])
        scope = {"stage": 9, "action": "edge-bind", "revision": revision, "configSha256": stage_fingerprint(config, 9),
                 "clusterResourceId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"], **edge}
        plan = {**scope, "bindingMode": "native-private-ingress", "configMapUid": current_config_map["metadata"]["uid"], "deploymentUid": current_deployment["metadata"]["uid"], "serviceUid": desired_state["serviceUid"], "currentRouteConfigSha256": current_state["routeConfigSha256"], "currentPodTemplateSha256": current_state["podTemplateSha256"], "desiredRouteConfigSha256": desired_state["routeConfigSha256"], "desiredPodTemplateSha256": desired_state["podTemplateSha256"]}
        digest = fingerprint(plan)
        summary = {**scope, "bindingMode": "native-private-ingress", "planSha256": digest, "applied": False, "stageAccepted": False, "trafficVerified": False}
        private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
        if operation == "plan":
            return summary
        require(digest == approved, "Edge binding plan changed or was not approved")
        observed_config_map = client.get("configmap", "llm-api-ingress")
        observed_deployment = client.get("deployment", "llm-api-ingress")
        require(observed_config_map["metadata"]["uid"] == plan["configMapUid"] and observed_config_map["data"] == current_config_map["data"] and observed_deployment["metadata"]["uid"] == plan["deploymentUid"] and observed_deployment["spec"] == current_deployment["spec"], "Native API ingress changed during approval")
        if observed_config_map["data"] != desired_config_map["data"]:
            client.patch("configmap", "llm-api-ingress", [{"op": "test", "path": "/metadata/uid", "value": plan["configMapUid"]}, {"op": "test", "path": "/data", "value": observed_config_map["data"]}, {"op": "replace", "path": "/data", "value": desired_config_map["data"]}])
        if observed_deployment["spec"] != desired_deployment["spec"]:
            client.patch("deployment", "llm-api-ingress", [{"op": "test", "path": "/metadata/uid", "value": plan["deploymentUid"]}, {"op": "test", "path": "/spec", "value": observed_deployment["spec"]}, {"op": "replace", "path": "/spec", "value": desired_deployment["spec"]}])
        client.run(["rollout", "status", "deployment/llm-api-ingress", "--timeout=15m"])
        after = native_ingress_state(config, client, edge["frontDoorId"])
        require(after["deploymentUid"] == plan["deploymentUid"] and after["serviceUid"] == plan["serviceUid"] and after["routeConfigSha256"] == plan["desiredRouteConfigSha256"] and after["podTemplateSha256"] == plan["desiredPodTemplateSha256"], "Native API ingress changed during rollout verification")
        receipt = {**scope, **after, "configMapUid": plan["configMapUid"], "rolloutVerified": True}
        template = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"edgeBinding": {"type": "object", "value": receipt}}}
        path = directory / "edge-binding-receipt.json"
        private_write(path, json.dumps(template))
        saved = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge-bind"), "--mode", "Incremental", "--template-file", str(path)])
        require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Native API ingress verified but saving the binding receipt failed")
        summary.update(applied=True, rolloutVerified=True)
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
        return summary
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


def require_edge_binding(config, revision, identifier, azure, client=None):
    result = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge-bind"), "--query", "{state:properties.provisioningState,binding:properties.outputs.edgeBinding.value}"])
    receipt = result.get("binding", {})
    require(result.get("state") == "Succeeded" and receipt.get("rolloutVerified") is True, "Run edge-bind and verify its rollout before enabling traffic")
    expected = {"revision": revision, "configSha256": stage_fingerprint(config, 9), "frontDoorId": identifier,
                "apiHost": "llm-api." + config["baseDomain"], "clusterResourceId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"]}
    require(all(receipt.get(key) == value for key, value in expected.items()), "Edge binding receipt is stale or belongs to a different environment")
    from scripts.backend_manifest import application_authentication
    if application_authentication(config)["mode"] == "native":
        require(receipt.get("bindingMode") == "native-private-ingress" and isinstance(receipt.get("serviceUid"), str) and re.fullmatch(r"[a-f0-9]{64}", receipt.get("routeConfigSha256", "")) and re.fullmatch(r"[a-f0-9]{64}", receipt.get("serviceSpecSha256", "")), "Native edge binding receipt lacks the verified private API ingress")
        require(client is not None, "Native traffic release requires live access to the private API ingress")
        live = native_ingress_state(config, client, identifier)
        for key in ("configMapUid", "deploymentUid", "serviceUid", "serviceSpecSha256", "podTemplateSha256", "routeConfigSha256", "frontDoorHeaderBound"):
            require(receipt.get(key) == live.get(key), "Native API ingress differs from the verified edge binding receipt")
    require(isinstance(receipt.get("deploymentUid"), str) and bool(receipt["deploymentUid"]) and re.fullmatch(r"[a-f0-9]{64}", receipt.get("podTemplateSha256", "")), "Edge binding receipt lacks the verified API workload")