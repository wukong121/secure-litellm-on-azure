"""Keep customer workflow payloads encrypted in artifacts and out of public logs."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scripts.customer_migration import ROOT, private_write, require

SEALED_FILE = "sealed-artifact.json"
MAX_PLAINTEXT = 32 * 1024 * 1024


def artifact_key():
    try:
        value = base64.b64decode(os.environ.get("WORKFLOW_ARTIFACT_KEY", "").strip(), validate=True)
    except ValueError:
        raise ValueError("WORKFLOW_ARTIFACT_KEY must be a base64-encoded 32-byte key") from None
    require(len(value) == 32, "Configure WORKFLOW_ARTIFACT_KEY as a base64-encoded 32-byte Environment Secret")
    return value


def artifact_context(repository, run_id, name):
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or "") and re.fullmatch(r"[1-9][0-9]{0,19}", run_id or "") and re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", name or ""), "Invalid workflow artifact context")
    return json.dumps({"version": 1, "repository": repository, "runId": run_id, "artifact": name}, sort_keys=True).encode()


def seal_values(values, repository, run_id, name):
    require(isinstance(values, dict) and bool(values), "No workflow evidence to protect")
    require(all(isinstance(filename, str) and re.fullmatch(r"[a-zA-Z0-9_.-]+\.json", filename) and not filename.startswith(".") for filename in values), "Only named JSON evidence files can be uploaded")
    content = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    require(len(content) <= MAX_PLAINTEXT, "Workflow evidence exceeds its plaintext payload bound")
    compressed = zlib.compress(content)
    require(len(compressed) <= 2 * 1024 * 1024, "Compressed workflow evidence exceeds its encrypted payload bound")
    nonce = os.urandom(12)
    encrypted = AESGCM(artifact_key()).encrypt(nonce, compressed, artifact_context(repository, run_id, name))
    return {"version": 1, "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(encrypted).decode()}


def open_values(envelope, repository, run_id, name):
    try:
        require(isinstance(envelope, dict) and set(envelope) == {"version", "nonce", "ciphertext"} and envelope["version"] == 1, "Invalid encrypted evidence format")
        require(isinstance(envelope["ciphertext"], str) and len(envelope["ciphertext"]) <= 3 * 1024 * 1024, "Encrypted evidence exceeds its bound")
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        require(len(nonce) == 12, "Invalid encrypted evidence nonce")
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        compressed = AESGCM(artifact_key()).decrypt(nonce, ciphertext, artifact_context(repository, run_id, name))
        decoder = zlib.decompressobj()
        content = decoder.decompress(compressed, MAX_PLAINTEXT + 1)
        require(len(content) <= MAX_PLAINTEXT and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail, "Invalid or oversized compressed evidence")
        result = json.loads(content)
        require(isinstance(result, dict), "Invalid decrypted evidence")
        return result
    except Exception:
        raise ValueError("Cannot authenticate workflow evidence; check its repository, run, artifact name and Environment Secret") from None


def seal_directory(directory, name, filenames):
    directory = Path(directory).resolve()
    require(directory.is_relative_to((ROOT / "temp").resolve()) and directory != (ROOT / "temp").resolve(), "Workflow evidence must remain under temp/")
    require(all(Path(filename).name == filename and filename.endswith(".json") for filename in filenames), "Use explicit JSON evidence filenames")
    values = {}
    for filename in filenames:
        path = directory / filename
        require(not path.is_symlink(), "Refusing evidence symlinks")
        if path.exists():
            values[filename] = json.loads(path.read_text())
    envelope = seal_values(values, os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""), name)
    private_write(directory / SEALED_FILE, json.dumps(envelope))
    print("Encrypted workflow evidence prepared; no customer payload is included in this log.")


def run_private(module, arguments):
    require(module in {"scripts.customer_migration", "scripts.migration_deploy", "scripts.migration_runtime", "scripts.migration_evidence", "scripts.installation_readiness", "scripts.gateway_checks"}, "Unapproved customer workflow module")
    artifact_key()
    if module == "scripts.installation_readiness":
        previous_report = ROOT / "temp/runner-readiness/runner-readiness.json"
        require(not previous_report.is_symlink(), "Refusing readiness report symlinks")
        previous_report.unlink(missing_ok=True)
    directory = ROOT / "temp/workflow-private"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    stdout, stderr, summary = (directory / name for name in ("stdout.log", "stderr.log", "summary.md"))
    for path in (stdout, stderr, summary):
        private_write(path, "")
    environment = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary)}
    with stdout.open("w") as output, stderr.open("w") as errors:
        completed = subprocess.run([sys.executable, "-m", module, *arguments], env=environment, stdout=output, stderr=errors, check=False)
    label = "completed" if completed.returncode == 0 else "failed"
    category = "none" if completed.returncode == 0 else "execution-failed"
    if completed.returncode:
        errors = stderr.read_text(errors="replace")[-8192:]
        categories = {
            "Prior stage evidence is missing": "prior-stage-not-confirmed",
            "configuration or revision mismatch": "stale-configuration-or-revision",
            "Plan changed since approval": "plan-changed-replan-required",
            "plan changed or was not approved": "plan-changed-replan-required",
            "Replace customer configuration placeholders": "configuration-placeholders",
            "Component configuration is missing or contains placeholders": "component-configuration-incomplete",
            "Unexpected or missing customer configuration fields": "configuration-fields",
            "Environment mismatch": "environment-mismatch",
            "Cannot authenticate workflow evidence": "evidence-key-or-context-mismatch",
            "Existing peering uses another name": "connectivity-existing-peering",
            "Existing peering conflicts": "connectivity-peering-conflict",
            "Existing DNS link uses another name": "connectivity-existing-dns-link",
            "Existing DNS link conflicts": "connectivity-dns-link-conflict",
            "Runner uses custom DNS": "connectivity-custom-dns",
            "VNet address spaces overlap": "connectivity-address-overlap",
            "Deploy the backup component before": "backup-resources-not-ready",
            "Connectivity plan attempts to modify": "connectivity-scope-rejected",
        }
        category = next((label for fragment, label in categories.items() if fragment.lower() in errors.lower()), category)
    message = f"Customer operation {label}. Result category: {category}. Review the encrypted artifact; raw output is retained only in the runner's private temporary directory until cleanup."
    if module == "scripts.installation_readiness":
        from scripts.runner_connectivity import BACKUP_PROBES
        report = ROOT / "temp/runner-readiness/runner-readiness.json"
        known = {"runner-tools", "runtime-versions", "docker-daemon", "azure-scope", "legacy-cluster-read", "target-cluster-read", *BACKUP_PROBES}
        try:
            if report.is_file() and not report.is_symlink() and report.stat().st_size <= 1024 * 1024:
                checks = json.loads(report.read_text()).get("checks", [])
                if isinstance(checks, list):
                    for check in checks:
                        if isinstance(check, dict) and isinstance(check.get("name"), str) and check["name"] in known and isinstance(check.get("status"), str) and check["status"] in {"passed", "failed", "skipped", "not-selected"}:
                            message += f"\nRunner check {check['name']}: {check['status']}."
        except (ValueError, AttributeError, OSError):
            pass
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(message + "\n")
            if module == "scripts.migration_evidence" and completed.returncode == 0:
                from scripts.customer_migration import stage_checks, validate_config
                config = validate_config(json.loads(os.environ["CUSTOMER_CONFIG_JSON"]), os.environ["CUSTOMER_ENVIRONMENT"])
                stage = int(os.environ["MIGRATION_STAGE"])
                stream.write("\nRequired check IDs (confirm only after inspecting actual results):\n\n```text\n" + ",".join(stage_checks(stage, config)) + "\n```\n")
    destinations = {"scripts.migration_deploy": "migration-deploy", "scripts.migration_runtime": "migration-runtime", "scripts.migration_evidence": "migration-evidence", "scripts.installation_readiness": "runner-readiness", "scripts.gateway_checks": "gateway-checks"}
    if module in destinations:
        path = ROOT / "temp" / destinations[module]
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_write(path / "operation-status.json", json.dumps({"module": module, "exitCode": completed.returncode, "status": label, "category": category}))
    return completed.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-key")
    seal = commands.add_parser("seal")
    seal.add_argument("--directory", type=Path, required=True)
    seal.add_argument("--name", required=True)
    seal.add_argument("files", nargs="+")
    decrypt = commands.add_parser("open")
    decrypt.add_argument("--file", type=Path, required=True)
    decrypt.add_argument("--repository", required=True)
    decrypt.add_argument("--run-id", required=True)
    decrypt.add_argument("--name", required=True)
    decrypt.add_argument("--output-dir", type=Path, default=ROOT / "temp/reviewed-evidence")
    run = commands.add_parser("run")
    run.add_argument("module")
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "check-key":
        artifact_key()
    elif args.command == "seal":
        seal_directory(args.directory, args.name, args.files)
    elif args.command == "open":
        directory = args.output_dir.resolve()
        require(directory.is_relative_to((ROOT / "temp").resolve()) and directory != (ROOT / "temp").resolve(), "Decrypted evidence must stay under ignored temp/")
        require(args.file.stat().st_size <= 4 * 1024 * 1024, "Encrypted file exceeds its size bound")
        values = open_values(json.loads(args.file.read_text()), args.repository, args.run_id, args.name)
        require(all(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.json", name) for name in values), "Invalid evidence filenames")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        for name, value in values.items():
            private_write(directory / name, json.dumps(value, indent=2))
        print("Evidence decrypted to the local private review directory; do not upload its plaintext contents.")
    else:
        return run_private(args.module, args.arguments)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError):
        raise SystemExit("Workflow evidence protection failed; check the Environment Secret and local evidence files. No payload was printed.") from None