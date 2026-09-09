"""Read approved audit plans from same-revision protected workflow artifacts."""

from scripts.customer_migration import fingerprint, require
from scripts.workflow_artifacts import github_api, read_artifact


def approved_audit_plan(config, action, revision, run_id, api=github_api):
    values = read_artifact(revision, run_id, "customer-runtime.yml", f"runtime-{config['environment']}-8-{run_id}", ("runtime-summary.json", "runtime-review.json"), api)
    summary, plan = values["runtime-summary.json"], values["runtime-review.json"]
    require(summary.get("stage") == 8 and summary.get("action") == action and summary.get("revision") == revision and summary.get("applied") is False, "Select a matching read-only plan run, not an execute run")
    require(summary.get("planSha256") == fingerprint(plan), "Approved plan artifact hash mismatch")
    return plan, summary["planSha256"]