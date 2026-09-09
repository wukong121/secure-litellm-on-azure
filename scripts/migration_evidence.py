"""Generate pending acceptance reports and record reviewed results, never invent passes."""

import argparse
import json
import os
from pathlib import Path

from scripts.customer_migration import (
    ROOT, MigrationError, active_stages, approval_policy, fingerprint, private_write,
    require, stage_checks, stage_fingerprint, validate_config, validate_evidence,
)


def draft_report(config, stage, revision):
    return {
        "stage": stage,
        "environment": config["environment"],
        "configSha256": stage_fingerprint(config, stage),
        "revision": revision,
        "observedAt": "REPLACE_ACTUAL_UTC_ACCEPTANCE_TIME",
        "checks": {name: {"status": "pending", "evidence": "REPLACE_ACTUAL_TEST_RESULT_AND_PRIVATE_REFERENCE"}
                   for name in stage_checks(stage, config)},
    }


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
    records = predecessors + [record]
    records.sort(key=lambda entry: entry["stage"])
    next_stage = next((candidate for candidate in active_stages(config) if candidate > stage), 10)
    validate_evidence(records, next_stage, config, revision, now)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("draft", "record"), required=True)
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
        from scripts.workflow_artifacts import load_evidence
        previous = load_evidence(config, args.stage, revision, predecessors_only=True) if os.environ.get("MIGRATION_AUTO_EVIDENCE") == "true" else json.loads(os.environ.get("MIGRATION_EVIDENCE_JSON") or "[]")
        records = record_evidence(
            config, args.stage, revision,
            previous,
            json.loads(os.environ["MIGRATION_REPORT_JSON"]), os.environ["MIGRATION_REPORT_URL"],
            json.loads(os.environ["MIGRATION_APPROVERS_JSON"]),
        )
        private_write(destination / "migration-evidence.json", json.dumps(records, indent=2) + "\n")
        print("Evidence ledger generated from submitted acceptance results. Report truth and approver authority remain the customer's responsibility.")


if __name__ == "__main__":
    try:
        main()
    except MigrationError as error:
        raise SystemExit(str(error)) from None
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit("Evidence generation failed; check private report and approval inputs. Values are not logged.") from None