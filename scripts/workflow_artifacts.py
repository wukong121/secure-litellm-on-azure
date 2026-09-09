"""Bounded readers for reviewed artifacts from the current protected revision."""

import io
import json
import os
import re
import subprocess
import zipfile

from scripts.customer_migration import fingerprint, private_write, require, stage_fingerprint, validate_evidence


def github_api(path):
    result = subprocess.run(["gh", "api", path], capture_output=True, check=False, timeout=120)
    require(result.returncode == 0, "Cannot read the approved private workflow artifact")
    require(len(result.stdout) <= 8 * 1024 * 1024, "Approved artifact response exceeded size limit")
    return result.stdout


def read_artifact(revision, run_id, workflow, artifact_name, filenames, api=github_api, *, current_run=False):
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF", "")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) and branch.startswith("refs/heads/"), "Artifact approval requires the protected repository context")
    require(re.fullmatch(r"[1-9][0-9]{0,19}", run_id or ""), "Select the successful plan workflow run ID")
    require(re.fullmatch(r"[a-f0-9]{40}", revision or ""), "Reviewed revision required")
    run = json.loads(api(f"repos/{repository}/actions/runs/{run_id}"))
    approved_state = run.get("conclusion") == "success"
    if current_run:
        require(run_id == os.environ.get("GITHUB_RUN_ID"), "Only the executing workflow can read its in-progress plan")
        approved_state = run.get("status") == "in_progress" and run.get("conclusion") is None
    require(run.get("event") == "workflow_dispatch" and approved_state and run.get("head_sha") == revision and run.get("head_branch") == branch.removeprefix("refs/heads/"), "Approved run is not successful on the current protected revision")
    require(run.get("path") == ".github/workflows/" + workflow and run.get("repository", {}).get("full_name") == repository, "Approved run came from a different workflow or repository")
    artifacts = json.loads(api(f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"))
    require(artifacts.get("total_count", 0) <= 100, "Unexpected artifact pagination")
    matches = [item for item in artifacts["artifacts"] if item.get("name") == artifact_name and not item.get("expired")]
    require(len(matches) == 1 and matches[0].get("size_in_bytes", 0) <= 4 * 1024 * 1024, "Approved artifact is missing, expired or too large")
    archive = api(f"repos/{repository}/actions/artifacts/{int(matches[0]['id'])}/zip")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        require(len(bundle.infolist()) <= 20 and sum(item.file_size for item in bundle.infolist()) <= 4 * 1024 * 1024, "Approved archive exceeded bounds")
        result = {}
        for filename in filenames:
            entries = [entry for entry in bundle.infolist() if entry.filename == filename]
            require(len(entries) == 1, "Approved artifact lacks an unambiguous required file")
            result[filename] = json.loads(bundle.read(entries[0]))
        return result


def write_operation_receipt(directory, config, revision, stage, action, operation, kind):
    filenames = ("runtime-summary.json", "runtime-review.json") if kind == "runtime" else ("plan-summary.json", "reviewed-plan.json")
    values = {name: json.loads((directory / name).read_text()) for name in filenames}
    digest = values[filenames[0]].get("planSha256")
    require(re.fullmatch(r"[a-f0-9]{64}", digest or ""), "Operation did not produce a valid plan receipt")
    receipt = {"version": 1, "environment": config["environment"], "revision": revision, "stage": stage,
               "action": action, "operation": operation, "kind": kind, "configSha256": stage_fingerprint(config, stage),
               "planSha256": digest, "files": {name: fingerprint(value) for name, value in values.items()}}
    private_write(directory / "operation-receipt.json", json.dumps(receipt, indent=2) + "\n")
    return receipt


def approved_operation(config, revision, stage, action, run_id, kind, api=github_api):
    require(kind in {"runtime", "infrastructure"}, "Unknown operation artifact kind")
    workflow = "customer-runtime.yml" if kind == "runtime" else "customer-deploy.yml"
    suffix = "" if kind == "runtime" else f"-{action}"
    name = f"{kind}-{config['environment']}-{stage}{suffix}-{run_id}"
    filenames = ("runtime-summary.json", "runtime-review.json") if kind == "runtime" else ("plan-summary.json", "reviewed-plan.json")
    values = read_artifact(revision, run_id, workflow, name, ("operation-receipt.json", *filenames), api)
    receipt = values["operation-receipt.json"]
    expected = {"version": 1, "environment": config["environment"], "revision": revision, "stage": stage,
                "action": action, "operation": "plan", "kind": kind, "configSha256": stage_fingerprint(config, stage)}
    require(all(receipt.get(key) == value for key, value in expected.items()), "Select a matching plan run for this environment, configuration and action")
    require(receipt.get("files") == {name: fingerprint(values[name]) for name in filenames}, "Approved operation files changed")
    digest = receipt.get("planSha256")
    require(re.fullmatch(r"[a-f0-9]{64}", digest or "") and values[filenames[0]].get("planSha256") == digest, "Approved operation plan hash mismatch")
    return digest


def load_evidence(config, stage, revision, run_id=None, api=github_api, *, predecessors_only=False):
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF", "").removeprefix("refs/heads/")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "Evidence loading requires the current private repository")
    from urllib.parse import quote
    if run_id:
        candidates = [{"id": run_id}]
    else:
        response = json.loads(api(f"repos/{repository}/actions/workflows/customer-acceptance.yml/runs?event=workflow_dispatch&status=success&branch={quote(branch, safe='')}&head_sha={revision}&per_page=30"))
        candidates = response.get("workflow_runs", [])
    for run in candidates[:30]:
        selected = str(run["id"])
        listing = json.loads(api(f"repos/{repository}/actions/runs/{selected}/artifacts?per_page=100"))
        require(listing.get("total_count", 0) <= 100, "Evidence artifact listing exceeds its bound")
        names = [item["name"] for item in listing.get("artifacts", []) if re.fullmatch(r"acceptance-record-" + re.escape(config["environment"]) + r"-[0-9]-" + re.escape(selected), item.get("name", ""))]
        if not names:
            continue
        require(len(names) == 1, "Ambiguous acceptance record artifact")
        values = read_artifact(revision, selected, "customer-acceptance.yml", names[0], ("migration-evidence.json",), api)
        records = values["migration-evidence.json"]
        if predecessors_only:
            require(isinstance(records, list) and all(isinstance(record, dict) and type(record.get("stage")) is int for record in records), "Malformed recorded evidence ledger")
            records = [record for record in records if record["stage"] < stage]
        validate_evidence(records, stage, config, revision)
        return records
    require(not run_id, "Selected run does not contain a recorded evidence ledger")
    validate_evidence([], stage, config, revision)
    return []