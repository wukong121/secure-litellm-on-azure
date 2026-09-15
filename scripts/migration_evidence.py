"""Generate pending acceptance reports and record reviewed results, never invent passes."""

import argparse
import json
import os
from pathlib import Path
import re
from datetime import datetime, timedelta, timezone

from scripts.customer_migration import (
    ROOT, MigrationError, active_stages, approval_policy, fingerprint, private_write,
    require, stage_checks, stage_fingerprint, stage_title, validate_config, validate_evidence,
)
from scripts.workflow_diagnostics import diagnostic_exit


def draft_report(config, stage, revision):
    return {
        "stage": stage,
        "environment": config["environment"],
        "configSha256": stage_fingerprint(config, stage),
        "revision": revision,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "observedAt": "REPLACE_ACTUAL_UTC_ACCEPTANCE_TIME",
        "checks": {name: {"status": "pending", "evidence": "REPLACE_ACTUAL_TEST_RESULT_AND_PRIVATE_REFERENCE"}
                   for name in stage_checks(stage, config)},
    }


def confirm_report(config, stage, revision, reviewed, checked_items, notes, actor, now=None):
    now = now or datetime.now(timezone.utc)
    mode, identities = approval_policy(config)
    login = config.get("governance", {}).get("githubLogin", "")
    require(mode == "single-operator" and len(identities) == 1 and login, "Manual confirmation requires the approved single-operator githubLogin")
    require(actor.get("login", "").lower() == login.lower() and actor.get("triggeringLogin", "").lower() == login.lower(), "Only the configured GitHub operator may confirm or rerun confirmation")
    require(all(re.fullmatch(r"[1-9][0-9]{0,19}", actor.get(key, "")) for key in ("id", "runId", "runAttempt")), "GitHub actor and execution identifiers are required")
    expected = {"stage": stage, "environment": config["environment"], "revision": revision, "configSha256": stage_fingerprint(config, stage)}
    require(isinstance(reviewed, dict) and all(reviewed.get(key) == value for key, value in expected.items()), "Reviewed checklist scope or revision changed")
    generated = datetime.fromisoformat(reviewed.get("generatedAt", "").replace("Z", "+00:00"))
    require(generated.tzinfo is not None and now - timedelta(days=7) <= generated <= now, "Reviewed checklist is expired or has an invalid timestamp")
    required = stage_checks(stage, config)
    checks = reviewed.get("checks", {})
    require(isinstance(checks, dict) and set(checks) == set(required) and all(isinstance(value, dict) and value.get("status") == "pending" for value in checks.values()), "Select a pending checklist generated for this stage")
    require(isinstance(checked_items, str), "Provide the IDs of each manually checked item")
    selected = re.split(r"[,\s]+", checked_items.strip())
    require(len(selected) == len(set(selected)) and set(selected) == set(required), "Explicitly confirm every required check ID once; missing or unknown checks are rejected")
    require(isinstance(notes, str) and 12 <= len(notes.strip()) <= 4000 and "REPLACE" not in notes.upper(), "Provide concise actual observations and evidence references, without secrets")
    return {**expected, "generatedAt": reviewed["generatedAt"], "observedAt": now.isoformat(),
            "checks": {name: {"status": "passed", "evidence": notes.strip(), "verification": "operator-confirmed"} for name in required},
            "confirmation": {"method": "manual-workflow", "githubLogin": actor["login"], "githubActorId": actor["id"],
                             "runId": actor["runId"], "runAttempt": actor["runAttempt"], "independentlyVerified": False}}


def record_evidence(config, stage, revision, previous, report, report_url, approvers, now=None):
    require(isinstance(previous, list) and all(isinstance(entry, dict) and type(entry.get("stage")) is int for entry in previous), "Invalid prior evidence ledger")
    predecessors = [entry for entry in previous if entry["stage"] < stage]
    validate_evidence(predecessors, stage, config, revision, now)
    require(isinstance(report, dict), "Acceptance report must be an object")
    for key, expected in (("stage", stage), ("environment", config["environment"]),
                          ("configSha256", stage_fingerprint(config, stage)), ("revision", revision)):
        require(report.get(key) == expected, "Acceptance report scope or revision mismatch")
    checks = report.get("checks", {})
    require(isinstance(checks, dict) and set(checks) == set(stage_checks(stage, config)), "Acceptance report check set is incomplete")
    for result in checks.values():
        require(isinstance(result, dict) and result.get("status") == "passed", "Acceptance report contains a check that has not passed")
        detail = result.get("evidence", "")
        require(isinstance(detail, str) and len(detail.strip()) >= 12 and "REPLACE" not in detail.upper(), "Actual test results and evidence references are required")
    record = {
        "stage": stage, "environment": config["environment"], "binding": "stage-config",
        "configSha256": stage_fingerprint(config, stage), "revision": revision,
        "approvalMode": approval_policy(config)[0], "status": "passed",
        "checks": stage_checks(stage, config), "approvedBy": approvers,
        "observedAt": report.get("observedAt"), "reportUrl": report_url,
        "reportSha256": fingerprint(report),
    }
    if "confirmation" in report:
        record["confirmation"] = report["confirmation"]
    records = predecessors + [record]
    records.sort(key=lambda entry: entry["stage"])
    next_stage = next((candidate for candidate in active_stages(config) if candidate > stage), 10)
    validate_evidence(records, next_stage, config, revision, now)
    return records


def checklist_summary(config, stage, revision, confirmed=False):
    checks = stage_checks(stage, config)
    lines = [f"## Stage {stage}: {stage_title(stage, config)}", "",
             "人工确认已记录；不是自动技术验收。" if confirmed else "这是待检查清单，不是阶段通过报告。请先运行相应操作并核对实际结果。", "",
             f"Environment: `{config['environment']}`", f"Revision: `{revision}`", "",
             "| Check ID | 人工核对 |", "| --- | --- |"]
    lines.extend(f"| `{name}` | {'已确认' if confirmed else '待检查'} |" for name in checks)
    lines.extend(["", "全部项目实际检查完成后，在confirm的checked_items中填写：", "", "```text", ",".join(checks), "```", "",
                  "reviewed_run_id填写本次draft运行编号；evidence_notes填写实际结果及证据引用，不含凭据或正文。",
                  "任何未检查或失败项目都不能确认；需改代码/配置时重新生成清单。", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("draft", "record", "confirm"), required=True)
    parser.add_argument("--stage", type=int, choices=range(10), required=True)
    parser.add_argument("--environment", choices=("dev", "test", "prod"), required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/migration-evidence")
    args = parser.parse_args()
    config = validate_config(json.loads(os.environ["CUSTOMER_CONFIG_JSON"]), args.environment)
    revision = os.environ.get("GITHUB_SHA", os.environ.get("MIGRATION_REVISION", ""))
    require(len(revision) == 40 and all(character in "0123456789abcdef" for character in revision), "Reviewed Git revision required")
    destination = args.output_dir.resolve()
    require(destination.is_relative_to((ROOT / "temp").resolve()) and destination != (ROOT / "temp").resolve(), "Evidence output must stay under temp/")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    if args.operation == "draft":
        report = draft_report(config, args.stage, revision)
        private_write(destination / "acceptance-report.json", json.dumps(report, indent=2) + "\n")
        print("Pending report generated. This is not approval or acceptance evidence.")
    else:
        from scripts.workflow_artifacts import load_evidence, read_artifact
        previous = load_evidence(config, args.stage, revision, predecessors_only=True) if os.environ.get("MIGRATION_AUTO_EVIDENCE") == "true" else json.loads(os.environ.get("MIGRATION_EVIDENCE_JSON") or "[]")
        if args.operation == "confirm":
            require(os.environ.get("MIGRATION_CONFIRM_ENVIRONMENT") == args.environment, "Type the target environment to confirm these checks")
            selected = os.environ.get("MIGRATION_REVIEWED_RUN_ID", "")
            values = read_artifact(revision, selected, "customer-acceptance.yml", f"acceptance-draft-{args.environment}-{args.stage}-{selected}", ("acceptance-report.json",))
            actor = {"login": os.environ.get("GITHUB_ACTOR", ""), "triggeringLogin": os.environ.get("GITHUB_TRIGGERING_ACTOR", ""),
                     "id": os.environ.get("GITHUB_ACTOR_ID", ""), "runId": os.environ.get("GITHUB_RUN_ID", ""), "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", "")}
            report = confirm_report(config, args.stage, revision, values["acceptance-report.json"], os.environ.get("MIGRATION_CHECKED_ITEMS", ""), os.environ.get("MIGRATION_EVIDENCE_NOTES", ""), actor)
            repository = os.environ.get("GITHUB_REPOSITORY", "")
            require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "Confirmation requires its GitHub repository context")
            report_url = f"https://github.com/{repository}/actions/runs/{actor['runId']}"
            approvers = [str(identity) for identity in approval_policy(config)[1]]
            report["confirmation"]["reviewedRunId"] = selected
            private_write(destination / "acceptance-report.json", json.dumps(report, indent=2) + "\n")
        else:
            report = json.loads(os.environ["MIGRATION_REPORT_JSON"])
            report_url = os.environ["MIGRATION_REPORT_URL"]
            approvers = json.loads(os.environ["MIGRATION_APPROVERS_JSON"])
        records = record_evidence(
            config, args.stage, revision,
            previous, report, report_url, approvers,
        )
        private_write(destination / "migration-evidence.json", json.dumps(records, indent=2) + "\n")
        print("Evidence ledger generated from submitted acceptance results. Report truth and approver authority remain the customer's responsibility.")
    summary = checklist_summary(config, args.stage, revision, confirmed=args.operation != "draft")
    private_write(destination / "checklist.md", summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(summary)


if __name__ == "__main__":
    try:
        main()
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "migration-evidence")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "migration-evidence", "Evidence generation failed")) from None