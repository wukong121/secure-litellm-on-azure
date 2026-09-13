"""Evaluate installation observations and run bounded read-only runner probes."""

import argparse
from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess
import sys

from scripts.customer_migration import ROOT, deployment_mode, fingerprint, private_write, require, validate_config

INSTALLATION_CHECKS = (
    "repository_admin", "protected_default_branch", "protected_environments",
    "workflow_federations", "scoped_deployment_permissions", "graph_permissions",
    "database_admin_identity", "kubernetes_roles", "private_runner_toolchain",
    "runner_registration_authority", "private_dns_routes", "dns_certificate_permissions",
)


def installation_readiness(observations):
    require(isinstance(observations, dict) and not set(observations) - set(INSTALLATION_CHECKS), "Unknown installation observation")
    checks = {}
    for name in INSTALLATION_CHECKS:
        evidence = observations.get(name)
        if evidence is None:
            checks[name] = {"status": "not-verified"}
        else:
            require(isinstance(evidence, dict) and set(evidence) == {"passed", "source", "scope"}, "Installation observations need a result, actual source and scope")
            require(type(evidence["passed"]) is bool and isinstance(evidence["source"], str) and evidence["source"] and isinstance(evidence["scope"], str) and evidence["scope"], "Invalid installation evidence")
            checks[name] = {"status": "passed" if evidence["passed"] else "failed", "source": evidence["source"], "scope": evidence["scope"]}
    return {"checks": checks, "readyForDeployment": all(check["status"] == "passed" for check in checks.values()), "stageAccepted": False}


def inspect_runner(config, directory, check_target=False, run=subprocess.run):
    from scripts.migration_runtime import connect_cluster
    from scripts.runner_lifecycle import TOOLS

    results = []
    def execute(arguments):
        result = run(arguments, capture_output=True, text=True, check=False, timeout=120)
        require(result.returncode == 0, "Read-only probe failed")
        return result.stdout

    def probe(name, operation):
        try:
            details = operation()
            results.append({"name": name, "status": "passed", "details": details})
            return True
        except (ValueError, KeyError, OSError, subprocess.SubprocessError):
            results.append({"name": name, "status": "failed", "details": "Check tool installation, private connectivity and the selected workflow identity permissions; raw diagnostics are not published"})
            return False

    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    results.append({"name": "runner-tools", "status": "failed" if missing else "passed", "missing": missing})
    def runtime_versions():
        python = execute([sys.executable, "--version"]).strip()
        node = execute(["node", "--version"]).strip()
        require(re.fullmatch(r"Python 3\.13\.\d+", python) and re.fullmatch(r"v24\.\d+\.\d+", node), "Use Python3.13 and Node24")
        return {"python": python, "node": node}
    probe("runtime-versions", runtime_versions)
    def docker_available():
        require(bool(execute(["docker", "version", "--format", "{{.Server.Version}}"]).strip()), "Docker daemon unavailable")
        return "Docker daemon responded"
    probe("docker-daemon", docker_available)

    def account_scope():
        account = json.loads(execute(["az", "account", "show", "--query", "{tenantId:tenantId,id:id}", "--output", "json", "--only-show-errors"]))
        require(account == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure scope differs from configured environment")
        return "OIDC account matches the configured tenant and subscription"
    if probe("azure-scope", account_scope):
        targets = ([True] if deployment_mode(config) == "migration" else []) + ([False] if check_target else [])
        for legacy in targets:
            def cluster_access():
                folder = directory / ("legacy" if legacy else "target")
                folder.mkdir(mode=0o700, exist_ok=True)
                command = connect_cluster(config, folder, legacy)
                execute([*command, "get", "deployments", "-o", "name"])
                return "Authenticated deployment listing succeeded; no workload was changed"
            probe("legacy-cluster-read" if legacy else "target-cluster-read", cluster_access)
    if not check_target:
        results.append({"name": "target-cluster-read", "status": "not-selected", "details": "Select check_target after target AKS is deployed"})
    return {"version": 1, "environment": config["environment"], "revision": os.environ.get("GITHUB_SHA", ""), "configSha256": fingerprint(config),
            "observedAt": datetime.now(timezone.utc).isoformat(), "status": "failed" if any(item["status"] == "failed" for item in results) else "passed",
            "checks": results, "stageAccepted": False, "readyForDeployment": False,
            "notCovered": ["write permissions", "Vault/Blob/ACR/PostgreSQL/Graph data access", "database size and restore time", "private target resources not yet deployed", "client and model authentication"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("dev", "test", "prod"), required=True)
    parser.add_argument("--check-target", action="store_true")
    args = parser.parse_args()
    directory = ROOT / "temp/runner-readiness"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = validate_config(json.loads(os.environ["CUSTOMER_CONFIG_JSON"]), args.environment)
    report = inspect_runner(config, directory, args.check_target)
    private_write(directory / "runner-readiness.json", json.dumps(report, indent=2))
    lines = ["## 私网 Runner 只读检查", "", "| 检查 | 结果 |", "| --- | --- |"]
    lines.extend(f"| {item['name']} | {item['status']} |" for item in report["checks"])
    lines.extend(["", "这里只验证工具、账户范围及所选集群的只读访问，不代表迁移或全部权限就绪。", ""])
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines))
    print(json.dumps({"status": report["status"], "stageAccepted": False}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit("Runner readiness failed before completion; check configuration and the preceding login step. No resource changes were performed.") from None