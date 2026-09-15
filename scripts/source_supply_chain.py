"""Check the locked public source image without Azure or target registry access."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone

from scripts.customer_migration import ROOT, MigrationError, private_write, require
from scripts.workflow_diagnostics import exception_diagnostic, format_diagnostic


def source_image():
    dockerfile = (ROOT / "LiteLLM/runtime/Dockerfile").read_text()
    matches = re.findall(r"^FROM (docker\.litellm\.ai/berriai/litellm@sha256:[0-9a-f]{64})$", dockerfile, re.M)
    require(len(matches) == 1, "Expected one digest-locked public LiteLLM source")
    return matches[0]


def check_source(directory, revision, run=subprocess.run):
    directory = Path(directory).resolve()
    require(directory.is_relative_to((ROOT / "temp").resolve()) and directory != (ROOT / "temp").resolve(), "Source evidence must stay under ignored temp/")
    require(re.fullmatch(r"[0-9a-f]{40}", revision or ""), "A full source revision is required")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    image = source_image()
    report = {"schemaVersion": 1, "check": "source_image_sbom_scan", "revision": revision, "sourceImage": image,
              "observedAt": datetime.now(timezone.utc).isoformat(), "status": "failed", "stageAccepted": False,
              "policy": {"severity": ["CRITICAL"], "ignoreUnfixed": True}, "results": [],
              "notCovered": ["upstream publisher identity", "target ACR signature and private pull", "customer stage approval"]}
    operations = (
        ("sbom", ["syft", "registry:" + image, "--output", "spdx-json"], "source-sbom.spdx.json"),
        ("scan", ["trivy", "image", "--exit-code", "1", "--severity", "CRITICAL", "--ignore-unfixed", "--format", "json", image], "source-scan.json"),
    )
    for name, command, filename in operations:
        path = directory / filename
        path.unlink(missing_ok=True)
        entry = {"name": name, "status": "failed"}
        try:
            result = run(command, capture_output=True, text=True, check=False, timeout=1200)
            document = json.loads(result.stdout)
            require(isinstance(document, dict), "Tool output must be a JSON object")
            if name == "sbom":
                require(document.get("spdxVersion") and isinstance(document.get("packages"), list) and document["packages"], "Incomplete SPDX inventory")
            else:
                require(document.get("ArtifactName") == image and isinstance(document.get("Results"), list) and document["Results"], "Scan output is not bound to the locked source")
            content = json.dumps(document, sort_keys=True)
            private_write(path, content)
            entry.update(status="passed" if result.returncode == 0 else "failed", exitCode=result.returncode,
                         artifact=filename, sha256=hashlib.sha256(content.encode()).hexdigest())
            if result.returncode:
                entry["reason"] = "Fixable CRITICAL vulnerabilities matched policy" if name == "scan" else "SBOM command returned a nonzero exit code"
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            entry["reason"] = "Tool unavailable, timed out, or returned invalid evidence"
            entry["diagnostic"] = exception_diagnostic(error, "source-supply-chain")
        report["results"].append(entry)
    if all(item["status"] == "passed" for item in report["results"]):
        report["status"] = "passed"
    private_write(directory / "source-summary.json", json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/source-supply-chain")
    args = parser.parse_args()
    report = check_source(args.output_dir, args.revision)
    print(json.dumps({"check": report["check"], "status": report["status"], "results": {item["name"]: item["status"] for item in report["results"]}, "stageAccepted": False}))
    if report["status"] == "failed":
        failures = []
        for result in report["results"]:
            if result["status"] != "failed":
                continue
            diagnostic = result.get("diagnostic", {})
            detail = diagnostic.get("message") if isinstance(diagnostic, dict) and isinstance(diagnostic.get("message"), str) else result.get("reason", "failed")
            failures.append(f"{result['name']}: {detail}")
        raise MigrationError("Source supply-chain checks failed: " + "; ".join(failures))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        diagnostic = exception_diagnostic(error, "source-supply-chain", "Source supply-chain checks failed")
        raise SystemExit(format_diagnostic(diagnostic)) from None