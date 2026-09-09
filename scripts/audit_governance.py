"""Audit access and hold registry changes backed by two actual workflow reviewers."""

import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
import os
import re
from uuid import UUID, uuid4

from scripts.audit_runtime import AuditCluster
from scripts.customer_migration import ROOT, fingerprint, private_write, require, stage_fingerprint, validate_config
from scripts.migration_runtime import connect_cluster
from scripts.workflow_artifacts import github_api, read_artifact


def governance_settings(config):
    settings = config.get("auditGovernance")
    require(isinstance(settings, dict) and set(settings) == {"reviewers"}, "auditGovernance requires a reviewer identity mapping")
    reviewers = settings["reviewers"]
    require(isinstance(reviewers, dict) and 2 <= len(reviewers) <= 20, "At least two independent audit reviewers are required")
    require(all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login) and UUID(oid).int for login, oid in reviewers.items()), "Invalid audit reviewer mapping")
    require(len({login.lower() for login in reviewers}) == len(reviewers) and len({UUID(oid) for oid in reviewers.values()}) == len(reviewers), "Audit reviewers must have distinct GitHub and Entra identities")
    return {login.lower(): str(UUID(oid)) for login, oid in reviewers.items()}


def reviewers_for_run(config, approvals, actor_oid):
    mapping = governance_settings(config)
    selected = []
    for suffix in ("audit-approval-1", "audit-approval-2"):
        name = config["environment"] + "-" + suffix
        decisions = [item for item in approvals if any(environment.get("name") == name for environment in item.get("environments", []))]
        require(len(decisions) == 1 and decisions[0].get("state") == "approved", "Two unambiguous protected-environment approvals are required")
        login = decisions[0].get("user", {}).get("login", "").lower()
        require(login in mapping and mapping[login] != actor_oid, "Audit reviewer is unapproved or is the requester")
        require(login != os.environ.get("GITHUB_ACTOR", "").lower(), "Workflow requester cannot approve their own governance change")
        selected.append(mapping[login])
    require(len(set(selected)) == 2, "Audit approval cannot be supplied twice by the same person")
    return selected


def timestamp(value):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(instant.tzinfo is not None, "Audit timestamps must include UTC offset")
    return instant


def governance_plan(config, request, resource, revision, now=None):
    now = now or datetime.now(timezone.utc)
    governance_settings(config)
    common = {"action", "id", "actorOid", "ticketId", "reason"}
    action = request.get("action")
    allowed = common | ({"teamIds", "from", "to", "validUntil"} if action == "grant" else {"until"} if action == "hold" else set())
    require(action in {"grant", "revoke", "hold", "release-hold"} and set(request) == allowed, "Invalid audit governance request fields")
    require(UUID(request["id"]).int and UUID(request["actorOid"]).int, "Nonzero subject and record identifiers required")
    require(1 <= len(request["ticketId"]) <= 100 and 12 <= len(request["reason"]) <= 500, "A bounded ticket and meaningful reason are required")
    require(resource and set(resource.get("data", {})) == {"approvals.json", "holds.json"}, "Initialize the audit registry before governance changes")
    registry = {name: json.loads(value) for name, value in resource["data"].items()}
    require(all(isinstance(value, list) and len(value) <= 1000 for value in registry.values()), "Audit registry exceeded its bound")
    require(not resource.get("immutable", False), "Audit registry is immutable")
    if action == "grant":
        teams = request["teamIds"]
        require(isinstance(teams, list) and 1 <= len(teams) <= 20 and len(set(teams)) == len(teams) and all(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", team) for team in teams), "Access requires exact non-wildcard teams")
        require(timestamp(request["from"]) < timestamp(request["to"]) <= now and timestamp(request["to"]) - timestamp(request["from"]) <= timedelta(days=7), "Audit query window must be past and at most seven days")
        require(now < timestamp(request["validUntil"]) <= now + timedelta(hours=24), "Audit access approval may last at most 24 hours")
        require(any(binding.get("oid") == request["actorOid"] and binding.get("plane") == "admin" and binding.get("role") == "audit_reader" and not binding.get("disabled") for binding in config.get("proxy", {}).get("bindings", [])), "Requested subject is not an enabled audit reader")
    elif action == "hold":
        require(now < timestamp(request["until"]) <= now + timedelta(days=365), "Hold expiry must be within one year")
    field = "approvals.json" if action in {"grant", "revoke"} else "holds.json"
    matches = [entry for entry in registry[field] if entry.get("id") == request["id"]]
    require(len(matches) <= 1, "Duplicate governance record ID")
    require(all(entry.get("tenantId") == config["azure"]["tenantId"] for entry in matches), "Governance record belongs to a different tenant")
    require((not matches) if action in {"grant", "hold"} else bool(matches), "Governance record already exists or cannot be removed")
    return {"revision": revision, "configSha256": stage_fingerprint(config, 8), "environment": config["environment"], "request": request,
            "registryUid": resource["metadata"]["uid"], "beforeSha256": fingerprint(resource["data"]), "validFrom": now.isoformat()}


def apply_governance(config, plan, resource, approvals, now=None):
    now = now or datetime.now(timezone.utc)
    require(plan.get("configSha256") == stage_fingerprint(config, 8) and plan.get("environment") == config["environment"], "Governance plan scope changed")
    require(resource["metadata"]["uid"] == plan["registryUid"] and fingerprint(resource["data"]) == plan["beforeSha256"], "Governance registry changed; replan")
    require(timestamp(plan["validFrom"]) <= now <= timestamp(plan["validFrom"]) + timedelta(hours=1), "Governance plan expired")
    request = plan["request"]
    governance_plan(config, request, resource, plan["revision"], now)
    reviewers = reviewers_for_run(config, approvals, request["actorOid"])
    data = copy.deepcopy(resource["data"])
    field = "approvals.json" if request["action"] in {"grant", "revoke"} else "holds.json"
    records = json.loads(data[field])
    if request["action"] == "grant":
        records.append({key: request[key] for key in ("id", "actorOid", "ticketId", "reason", "teamIds", "from", "to", "validUntil")} | {"tenantId": config["azure"]["tenantId"], "approvedBy": reviewers, "validFrom": now.isoformat()})
    elif request["action"] == "revoke":
        for entry in records:
            if entry["id"] == request["id"]:
                entry["disabled"] = True
    elif request["action"] == "hold":
        records.append({"id": request["id"], "tenantId": config["azure"]["tenantId"], "until": request["until"], "caseId": request["ticketId"]})
    else:
        records = [entry for entry in records if entry["id"] != request["id"]]
    data[field] = json.dumps(records, sort_keys=True)
    return data, reviewers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("plan", "execute"), required=True)
    args = parser.parse_args()
    config = validate_config(json.loads(os.environ["CUSTOMER_CONFIG_JSON"]), os.environ["CUSTOMER_ENVIRONMENT"])
    revision = os.environ["GITHUB_SHA"]
    directory = ROOT / "temp/audit-governance"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    client = AuditCluster(connect_cluster(config, directory, False), directory)
    require(client.get("configmap", "llmgw-audit-recovery-window", optional=True) is not None, "Pause audit writers and retention before governance changes")
    from scripts.audit_window import assert_paused
    from scripts.migration_deploy import group_id
    scope = {"tenantId": config["azure"]["tenantId"], "clusterId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"], "configSha256": stage_fingerprint(config, 8)}
    assert_paused(client, scope)
    client.assert_no_recovery_jobs()
    resource = client.get("configmap", "audit-approvals")
    if args.operation == "plan":
        if os.environ.get("AUDIT_REQUEST_JSON"):
            request = json.loads(os.environ["AUDIT_REQUEST_JSON"])
        else:
            action = os.environ["AUDIT_ACTION"]
            request = {"action": action, "id": os.environ.get("AUDIT_RECORD_ID") or (str(uuid4()) if action == "grant" else ""), "actorOid": os.environ["AUDIT_ACTOR_ID"], "ticketId": os.environ["AUDIT_TICKET"], "reason": os.environ["AUDIT_REASON"]}
            if action == "grant":
                request.update(teamIds=[team.strip() for team in os.environ["AUDIT_TEAMS"].split(",")], **{"from": os.environ["AUDIT_FROM"], "to": os.environ["AUDIT_TO"], "validUntil": os.environ["AUDIT_UNTIL"]})
            if action == "hold":
                request["until"] = os.environ["AUDIT_UNTIL"]
        plan = governance_plan(config, request, resource, revision)
        private_write(directory / "governance-plan.json", json.dumps(plan, indent=2))
        print(json.dumps({"action": request["action"], "planSha256": fingerprint(plan), "status": "awaiting-independent-approvals"}))
    else:
        run_id = os.environ["GITHUB_RUN_ID"]
        repository = os.environ["GITHUB_REPOSITORY"]
        approvals = json.loads(github_api(f"repos/{repository}/actions/runs/{run_id}/approvals"))
        plan = read_artifact(revision, run_id, "customer-audit-governance.yml", f"audit-governance-plan-{config['environment']}-{run_id}", ("governance-plan.json",), current_run=True)["governance-plan.json"]
        require(plan["revision"] == revision, "Governance plan revision mismatch")
        desired, reviewers = apply_governance(config, plan, resource, approvals)
        client.patch("configmap", "audit-approvals", [{"op": "test", "path": "/metadata/uid", "value": resource["metadata"]["uid"]}, {"op": "test", "path": "/data", "value": resource["data"]}, {"op": "replace", "path": "/data", "value": desired}])
        require(client.get("configmap", "audit-approvals")["data"] == desired, "Governance write could not be confirmed")
        receipt = {"revision": revision, "configSha256": stage_fingerprint(config, 8), "action": plan["request"]["action"], "id": plan["request"]["id"], "approvedBy": reviewers, "beforeSha256": plan["beforeSha256"], "afterSha256": fingerprint(desired), "stageAccepted": False}
        private_write(directory / "governance-receipt.json", json.dumps(receipt, indent=2))
        print(json.dumps(receipt))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit("Audit governance failed; keep maintenance paused. No values or raw contents are logged.") from None