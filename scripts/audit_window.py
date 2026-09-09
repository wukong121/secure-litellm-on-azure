"""Plan-bound, restartable audit maintenance windows; no automatic failure resume."""

import copy
import json
from uuid import uuid4

from scripts.customer_migration import fingerprint, require

WINDOW_NAME = "llmgw-audit-recovery-window"
TARGETS = {"deployment": "llm-api-proxy", "cronjob": "l3-retention"}


def load_window(client, scope):
    resource = client.get("configmap", WINDOW_NAME, optional=True)
    if resource is None:
        return None, None
    state = json.loads(resource.get("data", {}).get("window.json", "null"))
    require(isinstance(state, dict) and state.get("version") == 1 and state.get("scope") == scope, "Audit maintenance window belongs to another configuration or target")
    require(state.get("phase") in {"pausing", "paused", "resuming"}, "Invalid audit maintenance phase")
    require(set(state.get("resources", {})) == set(TARGETS), "Invalid audit maintenance targets")
    return resource, state


def target_state(client):
    resources = {kind: client.get(kind, name) for kind, name in TARGETS.items()}
    require(resources["deployment"]["spec"]["template"]["spec"].get("serviceAccountName") == "llm-api-proxy", "Unexpected audit writer service account")
    require(resources["cronjob"]["spec"]["jobTemplate"]["spec"]["template"]["spec"].get("serviceAccountName") == "l3-retention", "Unexpected audit retention service account")
    return resources


def spec_hash(resource):
    return fingerprint(resource["spec"])


def snapshot(resources):
    replicas = resources["deployment"]["spec"].get("replicas", 1)
    require(type(replicas) is int and 1 <= replicas <= 32, "Initial audit writer replicas must be between 1 and 32")
    result = {}
    for kind, resource in resources.items():
        desired = copy.deepcopy(resource)
        desired["spec"]["replicas" if kind == "deployment" else "suspend"] = 0 if kind == "deployment" else True
        field = "replicas" if kind == "deployment" else "suspend"
        result[kind] = {
            "uid": resource["metadata"]["uid"], "originalSha256": spec_hash(resource),
            "pausedSha256": spec_hash(desired), "fieldPresent": field in resource["spec"],
            "originalValue": resource["spec"].get(field, 1 if kind == "deployment" else False),
        }
    return result


def verify_targets(resources, state):
    for kind, resource in resources.items():
        saved = state["resources"][kind]
        require(resource["metadata"]["uid"] == saved["uid"], "Audit workload was replaced during maintenance")
        require(spec_hash(resource) in {saved["originalSha256"], saved["pausedSha256"]}, "Audit workload changed outside the approved maintenance plan")


def save_window(client, resource, state):
    client.patch("configmap", WINDOW_NAME, [
        {"op": "test", "path": "/metadata/uid", "value": resource["metadata"]["uid"]},
        {"op": "test", "path": "/data", "value": resource["data"]},
        {"op": "replace", "path": "/data", "value": {"window.json": json.dumps(state, sort_keys=True)}},
    ])


def plan_window(client, scope, action):
    require(action in {"audit-pause", "audit-resume"}, "Invalid audit window action")
    resource, state = load_window(client, scope)
    resources = target_state(client)
    client.assert_no_recovery_jobs()
    if state is None:
        require(action == "audit-pause", "No audit window exists to resume")
        state = {"version": 1, "scope": scope, "phase": "pausing", "resources": snapshot(resources)}
    else:
        require(action != "audit-pause" or state["phase"] != "resuming", "Finish the interrupted resume before pausing again")
        verify_targets(resources, state)
    plan = {"action": action, "window": state, "checkpointUid": resource["metadata"]["uid"] if resource else None,
            "observed": {kind: spec_hash(value) for kind, value in resources.items()}}
    return plan


def patch_target(client, kind, resource, desired):
    if resource["spec"] == desired:
        return
    client.patch(kind, TARGETS[kind], [
        {"op": "test", "path": "/metadata/uid", "value": resource["metadata"]["uid"]},
        {"op": "test", "path": "/spec", "value": resource["spec"]},
        {"op": "replace", "path": "/spec", "value": desired},
    ])


def execute_window(client, scope, action, approved):
    plan = plan_window(client, scope, action)
    require(fingerprint(plan) == approved, "Audit window plan changed or was not approved")
    resource, state = load_window(client, scope)
    if state is None:
        state = {**plan["window"], "id": str(uuid4())}
        client.create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": WINDOW_NAME},
                       "data": {"window.json": json.dumps(state, sort_keys=True)}})
    resource, state = load_window(client, scope)
    if action == "audit-resume":
        state["phase"] = "resuming"
        save_window(client, resource, state)
    order = ("cronjob", "deployment") if action == "audit-pause" else ("deployment", "cronjob")
    for kind in order:
        resources = target_state(client)
        verify_targets(resources, state)
        current = resources[kind]
        desired = copy.deepcopy(current["spec"])
        field = "replicas" if kind == "deployment" else "suspend"
        if action == "audit-pause":
            desired[field] = 0 if kind == "deployment" else True
        elif state["resources"][kind]["fieldPresent"]:
            desired[field] = state["resources"][kind]["originalValue"]
        else:
            desired.pop(field, None)
        patch_target(client, kind, current, desired)
    if action == "audit-pause":
        client.wait_quiet()
        resources = target_state(client)
        require(all(spec_hash(value) == state["resources"][kind]["pausedSha256"] for kind, value in resources.items()), "Audit writers did not remain paused")
        resource, state = load_window(client, scope)
        state["phase"] = "paused"
        save_window(client, resource, state)
    else:
        client.wait_ready()
        resource, _ = load_window(client, scope)
        client.delete("configmap", WINDOW_NAME, resource["metadata"]["uid"])
    return {"windowId": state["id"], "phase": "paused" if action == "audit-pause" else "resumed", "stageAccepted": False}


def assert_paused(client, scope):
    resource, state = load_window(client, scope)
    require(state is not None and state["phase"] == "paused", "Run the approved audit-pause workflow before recovery")
    resources = target_state(client)
    verify_targets(resources, state)
    require(all(spec_hash(value) == state["resources"][kind]["pausedSha256"] for kind, value in resources.items()), "Audit workload is not paused")
    client.assert_quiet()
    return {"uid": resource["metadata"]["uid"], "stateSha256": fingerprint(state), "windowId": state["id"]}