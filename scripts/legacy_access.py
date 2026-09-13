"""Restrict the known legacy gateway source ranges without changing its backend."""

import copy
import ipaddress
import json

from scripts.customer_migration import deployment_mode, fingerprint, private_write, require, stage_fingerprint

ANNOTATION = "nginx.ingress.kubernetes.io/whitelist-source-range"
SERVICE = "litellm-mi-proxy"
INGRESS = "litellm-ingress"
CHECKPOINT = "llmgw-legacy-access"


def source_ranges(values):
    require(isinstance(values, list) and 1 <= len(values) <= 64, "Provide 1 to 64 explicit source CIDRs")
    result = []
    for value in values:
        require(isinstance(value, str), "Source CIDRs must be strings")
        network = ipaddress.ip_network(value, strict=True)
        require(network.version == 4 and network.prefixlen > 0 and not network.is_loopback and not network.is_link_local and not network.is_multicast and not network.is_unspecified, "Use explicit IPv4 client or egress CIDRs, never unrestricted or special ranges")
        result.append(str(network))
    require(len(set(result)) == len(result), "Duplicate source CIDRs are not allowed")
    return sorted(result)


def access_settings(config):
    value = config.get("legacyAccess")
    require(deployment_mode(config) == "migration", "Legacy access changes do not apply to greenfield")
    require(isinstance(value, dict) and set(value) == {"mode", "allowedCidrs", "accessImpactAccepted"}, "legacyAccess requires mode, allowedCidrs and accessImpactAccepted")
    require(value["mode"] in {"load-balancer", "nginx-ingress"} and value["accessImpactAccepted"] is True, "Approve the legacy entry mode and client access impact")
    return {**value, "allowedCidrs": source_ranges(value["allowedCidrs"])}


def access_view(resource, kind):
    metadata = resource["metadata"]
    spec = copy.deepcopy(resource["spec"])
    annotations = dict(metadata.get("annotations") or {})
    annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if kind == "service":
        present = "loadBalancerSourceRanges" in spec
        value = spec.pop("loadBalancerSourceRanges", None)
    else:
        present = ANNOTATION in annotations
        value = annotations.pop(ANNOTATION, None)
    return {"uid": metadata["uid"], "stableSha256": fingerprint({"spec": spec, "annotations": annotations}), "present": present, "value": value}


def access_target(settings, services, ingresses):
    selected = [item for item in services if item["metadata"]["name"] == SERVICE]
    require(len(selected) == 1, "Expected the known legacy litellm-mi-proxy Service")
    service = selected[0]
    spec = service["spec"]
    require(spec.get("selector") == {"app": SERVICE}, "Legacy service selector differs from the reviewed deployment")
    ports = spec.get("ports", [])
    require(len(ports) == 1 and ports[0].get("port") == ports[0].get("targetPort") == 4000 and ports[0].get("protocol", "TCP") == "TCP", "Legacy service ports differ from the reviewed deployment")
    require(not spec.get("externalIPs"), "Review legacy external IP bypass before restricting access")
    require(not any(item["metadata"]["name"] != SERVICE and item["spec"].get("selector") == spec["selector"] for item in services), "Additional Services target the legacy application; review them separately")
    related = []
    for item in ingresses:
        ingress_spec = item["spec"]
        backends = [ingress_spec.get("defaultBackend", {})]
        backends += [path.get("backend", {}) for rule in ingress_spec.get("rules", []) for path in rule.get("http", {}).get("paths", [])]
        if any(backend.get("service", {}).get("name") == SERVICE for backend in backends):
            related.append(item)
    if settings["mode"] == "load-balancer":
        require(spec.get("type") == "LoadBalancer" and not related, "LoadBalancer mode cannot coexist with an ingress to the same backend")
        return "service", service
    require(spec.get("type", "ClusterIP") == "ClusterIP", "Ingress mode requires a private backend Service; close direct LoadBalancer or NodePort exposure separately")
    require(len(related) == 1 and related[0]["metadata"]["name"] == INGRESS, "Expected only the known litellm-ingress route")
    ingress = related[0]
    require(ingress["spec"].get("ingressClassName") == "nginx" and not ingress["spec"].get("defaultBackend"), "Only the reviewed NGINX ingress is supported")
    rules = ingress["spec"].get("rules", [])
    require(len(rules) == 1 and rules[0].get("host"), "Expected one explicit legacy hostname")
    paths = rules[0].get("http", {}).get("paths", [])
    require(len(paths) == 1 and paths[0].get("path") == "/" and paths[0].get("pathType") == "Prefix" and paths[0].get("backend") == {"service": {"name": SERVICE, "port": {"number": 4000}}}, "Legacy ingress route differs from the reviewed deployment")
    annotations = ingress["metadata"].get("annotations") or {}
    require(not any("snippet" in name or name == "nginx.ingress.kubernetes.io/denylist-source-range" for name in annotations), "Custom NGINX policy requires separate review")
    return "ingress", ingress


def restricted_value(settings, kind, view):
    requested = settings["allowedCidrs"]
    previous = view["value"]
    if previous:
        old = [part.strip() for part in previous.split(",")] if kind == "ingress" else previous
        require(isinstance(old, list), "Existing source ranges have an unexpected format")
        networks = [ipaddress.ip_network(value, strict=True) for value in old]
        require(all(any(ipaddress.ip_network(value).version == network.version and ipaddress.ip_network(value).subnet_of(network) for network in networks) for value in requested), "New source ranges would broaden the existing policy")
    return ",".join(requested) if kind == "ingress" else requested


def access_patch(resource, kind, present, value):
    path = "/spec/loadBalancerSourceRanges" if kind == "service" else "/metadata/annotations/" + ANNOTATION.replace("/", "~1")
    operations = [{"op": "test", "path": "/metadata/uid", "value": resource["metadata"]["uid"]},
                  {"op": "test", "path": "/metadata/resourceVersion", "value": resource["metadata"]["resourceVersion"]}]
    if kind == "ingress" and not isinstance(resource["metadata"].get("annotations"), dict):
        require(present, "Missing ingress annotations cannot be removed")
        operations.append({"op": "add", "path": "/metadata/annotations", "value": {ANNOTATION: value}})
    else:
        operations.append({"op": "add" if present else "remove", "path": path, **({"value": value} if present else {})})
    return operations


def access_plan(config, revision, action, client):
    require(action in {"legacy-access-restrict", "legacy-access-restore"}, "Unknown legacy access action")
    settings = access_settings(config)
    kind, target = access_target(settings, client.items("services"), client.items("ingresses"))
    view = access_view(target, kind)
    scope = {"azure": config["azure"], "legacy": config["legacy"], "kind": kind, "name": target["metadata"]["name"]}
    checkpoint = client.get("configmap", CHECKPOINT, optional=True)
    state = None
    if checkpoint:
        require(checkpoint.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/managed-by") == "llmgw-legacy-access", "Existing access checkpoint is not managed by this operation")
        state = json.loads(checkpoint.get("data", {}).get("access.json", "null"))
        require(isinstance(state, dict) and state.get("version") == 1 and state.get("scope") == scope and state.get("phase") in {"restricting", "restricted", "restoring", "restored"}, "Legacy access checkpoint scope or format differs")
        require(view in (state["original"], state["restricted"]), "Legacy entry was replaced or changed outside this operation; no automatic overwrite")
    if action == "legacy-access-restrict":
        require(not state or state["phase"] != "restoring", "Finish interrupted access restoration before restricting again")
        if state is None or state["phase"] == "restored":
            require(state is None or view == state["original"], "Restored checkpoint differs from the live entry")
            state = {"version": 1, "scope": scope, "phase": "restricting", "original": view,
                     "restricted": {**view, "present": True, "value": restricted_value(settings, kind, view)}}
        require(state["restricted"]["value"] == (",".join(settings["allowedCidrs"]) if kind == "ingress" else settings["allowedCidrs"]), "Source policy changed during this access cycle; restore or review it separately")
        desired = state["restricted"]
    else:
        require(state is not None, "No legacy access checkpoint exists to restore")
        desired = state["original"]
    plan = {"stage": 1, "action": action, "revision": revision, "configSha256": stage_fingerprint(config, 1), "scope": scope,
            "current": view, "desired": desired, "checkpointSha256": fingerprint(checkpoint["data"]) if checkpoint else None,
            "checkpointUid": checkpoint["metadata"]["uid"] if checkpoint else None}
    return plan, state, checkpoint, target


def access_operation(config, action, operation, revision, directory, approved, client=None):
    from scripts.migration_runtime import connect_cluster, validate_action
    from scripts.audit_runtime import AuditCluster

    require(operation in {"plan", "execute"}, "Unknown legacy access operation")
    validate_action(config, 1, action)
    client = client or AuditCluster(connect_cluster(config, directory, legacy=True), directory)
    plan, state, checkpoint, target = access_plan(config, revision, action, client)
    digest = fingerprint(plan)
    summary = {"stage": 1, "action": action, "revision": revision, "planSha256": digest, "applied": False, "stageAccepted": False,
               "scope": plan["scope"], "before": plan["current"]["value"], "after": plan["desired"]["value"],
               "verification": "configuration-only", "trafficVerified": False,
               "notCovered": ["external source and bypass tests", "private admin isolation", "shared ingress controller and cross-namespace routes", "credentials and actual client availability"]}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    if operation == "plan":
        return summary
    require(digest == approved, "Legacy access plan changed or was not approved")
    state["phase"] = "restricting" if action == "legacy-access-restrict" else "restoring"
    data = {"access.json": json.dumps(state, sort_keys=True)}
    if checkpoint is None:
        client.create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": CHECKPOINT, "labels": {"app.kubernetes.io/managed-by": "llmgw-legacy-access"}}, "data": data})
    else:
        client.patch("configmap", CHECKPOINT, [{"op": "test", "path": "/metadata/uid", "value": checkpoint["metadata"]["uid"]}, {"op": "test", "path": "/data", "value": checkpoint["data"]}, {"op": "replace", "path": "/data", "value": data}])
    checkpoint = client.get("configmap", CHECKPOINT)
    require(checkpoint["data"] == data, "Access checkpoint changed before patching the entry")
    kind = plan["scope"]["kind"]
    name = plan["scope"]["name"]
    desired = plan["desired"]
    if plan["current"] != desired:
        client.patch(kind, name, access_patch(target, kind, desired["present"], desired["value"]))
    require(access_view(client.get(kind, name), kind) == desired, "Legacy access patch could not be confirmed; preserve checkpoint and replan")
    state["phase"] = "restricted" if action == "legacy-access-restrict" else "restored"
    client.patch("configmap", CHECKPOINT, [{"op": "test", "path": "/metadata/uid", "value": checkpoint["metadata"]["uid"]}, {"op": "test", "path": "/data", "value": data}, {"op": "replace", "path": "/data", "value": {"access.json": json.dumps(state, sort_keys=True)}}])
    summary.update(applied=True, checkpoint=CHECKPOINT, phase=state["phase"])
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    return summary