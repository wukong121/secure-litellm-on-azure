"""Run migration Stage 0 and Stage 1 from a local customer configuration."""

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import uuid4

from scripts.customer_migration import (
    ROOT,
    MigrationError,
    deployment_mode,
    fingerprint,
    parameters_for,
    private_write,
    require,
    validate_config,
)
from scripts.migration_deploy import AzureCommands, deploy_component, deployment_name
from scripts.workflow_diagnostics import command_failure_summary, diagnostic_exit, exception_diagnostic


INFRASTRUCTURE_STEPS = {
    "bootstrap": (0, "bootstrap"),
    "backup": (0, "backup"),
    "execution-host-connectivity": (0, "runner-connectivity"),
    "legacy-logging": (1, "legacy-logging"),
    "monitoring": (1, "monitoring"),
}
RUNTIME_STEPS = {
    "monitoring-onboard": (1, "monitoring-onboard"),
    "legacy-hardening": (1, "legacy-hardening"),
    "legacy-access-restrict": (1, "legacy-access-restrict"),
    "legacy-access-restore": (1, "legacy-access-restore"),
}
STEPS = (
    "config-check",
    "bootstrap",
    "backup",
    "execution-host-connectivity",
    "connectivity-check",
    "backup-restore",
    "legacy-logging",
    "monitoring-onboard",
    "monitoring",
    "legacy-hardening",
    "legacy-access-restrict",
    "legacy-access-restore",
)
LOCAL_TOOLS = ("az", "kubectl", "kubelogin", "docker", "curl", "getent")


def reviewed_revision(run=subprocess.run):
    result = run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False)
    revision = result.stdout.strip()
    require(result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "Run local Stage0/1 from a Git checkout with a full revision")
    return revision


def local_settings(document):
    settings = document.get("localExecution")
    require(isinstance(settings, dict) and set(settings) == {"postgresRestoreImage", "executionHost"}, "localExecution requires postgresRestoreImage and executionHost")
    image = settings["postgresRestoreImage"]
    require(isinstance(image, str) and re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image) is not None, "localExecution.postgresRestoreImage must pin a full image digest")
    host = settings["executionHost"]
    require(isinstance(host, dict) and set(host) == {"virtualNetworkId", "managePeering", "manageBlobDnsLink"}, "localExecution.executionHost has unexpected or missing fields")
    require(isinstance(host["virtualNetworkId"], str) and host["virtualNetworkId"], "Configure the execution host VNet resource ID")
    require(type(host["managePeering"]) is bool and type(host["manageBlobDnsLink"]) is bool, "Execution host connectivity switches must be booleans")
    return settings


def load_config(path):
    source = path.resolve()
    require(source.is_file() and not source.is_symlink(), "Local customer config must be a regular JSON file")
    try:
        document = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError):
        raise MigrationError("Unable to read the local customer JSON configuration") from None
    require(isinstance(document, dict), "Local customer configuration must be a JSON object")
    settings = local_settings(document)
    customer = copy.deepcopy(document)
    customer.pop("localExecution")
    config = validate_config(customer, customer.get("environment"))
    require(deployment_mode(config) == "migration", "Local Stage0/1 applies only to migration mode")
    require("runner-connectivity" not in config["parameters"], "Move execution host connectivity from parameters into localExecution.executionHost")
    host = settings["executionHost"]
    config["parameters"]["runner-connectivity"] = {
        "runnerVirtualNetworkId": host["virtualNetworkId"],
        "managePeering": host["managePeering"],
        "manageBlobDnsLink": host["manageBlobDnsLink"],
    }
    validate_config(config, config["environment"])
    return config, settings


def operation_root(base, step):
    base = base.resolve()
    require(base.is_relative_to((ROOT / "temp").resolve()) and base != (ROOT / "temp").resolve(), "Local execution output must stay under temp/")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = base / f"{stamp}-{step}-{uuid4().hex[:8]}"
    destination.mkdir(parents=True, mode=0o700)
    destination.chmod(0o700)
    return destination


def execute_infrastructure_plan(config, stage, component, plan, plan_directory, execute_directory):
    execute_directory.mkdir(mode=0o700)
    azure = AzureCommands(config, execute_directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope does not match customer configuration")
    scope = "sub" if component == "bootstrap" else "group"
    if scope == "sub":
        scope_arguments = ["--location", config["location"]]
    else:
        group = config["legacy" if component in {"monitoring", "legacy-logging"} else "target"]["resourceGroup"]
        scope_arguments = ["--resource-group", group]
    common = [
        *scope_arguments,
        "--name", deployment_name(config, stage, component),
        "--template-file", str(plan_directory / "template.json"),
        "--parameters", f"@{plan_directory / 'parameters.json'}",
    ]
    command = ["deployment", scope, "create", *common]
    if scope == "group":
        command.extend(["--mode", "Incremental"])
    deployed = azure.scoped(command)
    require(deployed.get("properties", {}).get("provisioningState") == "Succeeded", "Local infrastructure deployment did not succeed")
    receipt = {
        "stage": stage,
        "component": component,
        "environment": config["environment"],
        "revision": plan["revision"],
        "configSha256": plan["configSha256"],
        "planSha256": plan["planSha256"],
        "deploymentId": deployed.get("id"),
        "provisioningState": "Succeeded",
    }
    private_write(execute_directory / "deployment-outputs.json", json.dumps(deployed.get("properties", {}).get("outputs", {}), indent=2) + "\n")
    private_write(execute_directory / "deployment-receipt.json", json.dumps(receipt, indent=2) + "\n")
    return receipt


def run_infrastructure(config, revision, stage, component, destination):
    plan_directory = destination / "plan"
    execute_directory = destination / "execute"
    plan = deploy_component(config, stage, component, revision, "plan", [], plan_directory)
    receipt = execute_infrastructure_plan(config, stage, component, plan, plan_directory, execute_directory)
    return {"planSha256": plan["planSha256"], "receipt": receipt}


def runtime_plan_sha(directory):
    summary = json.loads((directory / "runtime-summary.json").read_text())
    value = summary.get("planSha256", "")
    require(re.fullmatch(r"[0-9a-f]{64}", value) is not None, "Local runtime preview did not produce a plan hash")
    return value


def run_runtime(config, revision, stage, action, destination):
    from scripts.legacy_access import access_operation
    from scripts.migration_runtime import monitoring_onboard, publish

    plan_directory = destination / "plan"
    execute_directory = destination / "execute"
    plan_directory.mkdir(mode=0o700)
    execute_directory.mkdir(mode=0o700)
    if action == "monitoring-onboard":
        monitoring_onboard(config, stage, "plan", revision, plan_directory, "")
        plan_sha = runtime_plan_sha(plan_directory)
        monitoring_onboard(config, stage, "execute", revision, execute_directory, plan_sha)
    elif action == "legacy-hardening":
        publish(config, stage, action, "plan", revision, plan_directory, "")
        plan_sha = runtime_plan_sha(plan_directory)
        publish(config, stage, action, "execute", revision, execute_directory, plan_sha)
    else:
        summary = access_operation(config, action, "plan", revision, plan_directory, "")
        plan_sha = summary["planSha256"]
        access_operation(config, action, "execute", revision, execute_directory, plan_sha)
    return {"planSha256": plan_sha}


def require_legacy_cluster_running(config, destination, azure=None):
    azure = azure or AzureCommands(config, destination)
    cluster = azure.scoped([
        "aks", "show",
        "--resource-group", config["legacy"]["resourceGroup"],
        "--name", config["legacy"]["aksClusterName"],
        "--query", "{provisioningState:provisioningState,powerState:powerState.code,fqdn:fqdn,privateFqdn:privateFqdn}",
    ])
    require(cluster.get("provisioningState") == "Succeeded", "Legacy AKS provisioning state must be Succeeded")
    require(cluster.get("powerState") == "Running", "Legacy AKS is not Running; start it and wait for its API hostname to resolve before continuing")
    require(isinstance(cluster.get("privateFqdn") or cluster.get("fqdn"), str), "Legacy AKS did not expose an API hostname")
    return cluster


def run_connectivity_check(config, revision, destination, run=subprocess.run):
    from scripts.migration_runtime import connect_cluster
    from scripts.runner_connectivity import backup_target

    checks = []

    def execute(arguments):
        result = run(arguments, capture_output=True, text=True, check=False, timeout=120)
        require(result.returncode == 0, f"Local connectivity command failed: {command_failure_summary(result.stdout, result.stderr, result.returncode)}")
        return result.stdout

    def probe(name, operation):
        try:
            details = operation()
            checks.append({"name": name, "status": "passed", "details": details})
            return details
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            checks.append({"name": name, "status": "failed", "details": "Check the local tool, current Azure identity, and private network path", "diagnostic": exception_diagnostic(error, "local-connectivity")})
            return None

    missing = [tool for tool in LOCAL_TOOLS if shutil.which(tool) is None]
    checks.append({"name": "local-tools", "status": "failed" if missing else "passed", "missing": missing})
    checks.append({"name": "python-version", "status": "passed" if sys.version_info >= (3, 10) else "failed", "version": sys.version.split()[0]})
    probe("docker-daemon", lambda: execute(["docker", "version", "--format", "{{.Server.Version}}"] ).strip())
    account = probe("azure-scope", lambda: json.loads(execute(["az", "account", "show", "--query", "{tenantId:tenantId,id:id}", "--output", "json", "--only-show-errors"])))
    expected = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
    if account is not None and account != expected:
        checks[-1] = {"name": "azure-scope", "status": "failed", "details": "Current Azure tenant or subscription differs from customer configuration"}
        account = None
    target = None
    if account is not None:
        azure = AzureCommands(config, destination)
        cluster = probe("legacy-cluster-state", lambda: require_legacy_cluster_running(config, destination, azure))

        def cluster_read():
            kube = connect_cluster(config, destination, legacy=True)
            execute([*kube, "get", "deployments", "-o", "name"])
            return "Old cluster deployment listing succeeded"

        if cluster is not None:
            probe("legacy-cluster-read", cluster_read)
        target = probe("backup-resources", lambda: backup_target(config, azure))
    if target is not None:
        def private_dns():
            output = execute(["getent", "ahostsv4", target["blobHost"]])
            addresses = {line.split()[0] for line in output.splitlines() if line.split()}
            require(addresses and addresses.issubset(set(target["privateEndpointIps"])), "Blob DNS does not resolve exclusively to the approved private endpoint")
            return "Execution host DNS matches the backup private endpoint"

        def private_tls():
            output = execute(["curl", "--noproxy", "*", "--connect-timeout", "5", "--max-time", "15", "--silent", "--show-error", "--output", "/dev/null", "--write-out", "%{remote_ip} %{http_code}", "https://" + target["blobHost"] + "/"])
            response = output.split()
            require(len(response) == 2 and response[0] in target["privateEndpointIps"] and re.fullmatch(r"[1-5][0-9]{2}", response[1]), "Blob TLS did not reach the approved private endpoint")
            return "TLS reached the backup private endpoint"

        def blob_read():
            output = execute(["az", "storage", "blob", "list", "--subscription", config["azure"]["subscriptionId"], "--account-name", target["storageAccountName"], "--container-name", target["containerName"], "--auth-mode", "login", "--num-results", "1", "--query", "length(@)", "--output", "json", "--only-show-errors"])
            require(output.strip() in {"0", "1"}, "Unexpected Blob listing result")
            return "Current Azure identity can list the backup container"

        probe("backup-private-dns", private_dns)
        probe("backup-private-tls", private_tls)
        probe("backup-blob-read", blob_read)
    status = "failed" if any(check["status"] == "failed" for check in checks) else "passed"
    report = {
        "version": 1,
        "environment": config["environment"],
        "revision": revision,
        "configSha256": fingerprint(config),
        "observedAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "checks": checks,
        "notCovered": ["Pod exec/cp", "Blob upload and download", "backup contents", "actual restore"],
    }
    private_write(destination / "local-readiness.json", json.dumps(report, indent=2) + "\n")
    require(status == "passed", "Local execution host checks failed")
    return {"status": status}


def run_backup_restore(config, settings, revision, destination):
    from scripts.migration_runtime import backup_restore

    require_legacy_cluster_running(config, destination)
    image = settings["postgresRestoreImage"]
    previous = os.environ.get("POSTGRES_RESTORE_IMAGE")
    os.environ["POSTGRES_RESTORE_IMAGE"] = image
    try:
        backup_restore(config, revision, destination)
    finally:
        if previous is None:
            os.environ.pop("POSTGRES_RESTORE_IMAGE", None)
        else:
            os.environ["POSTGRES_RESTORE_IMAGE"] = previous
    return {"status": "completed"}


def check_configuration(config):
    for stage, component in ((0, "bootstrap"), (0, "backup"), (0, "runner-connectivity"), (1, "legacy-logging"), (1, "monitoring")):
        parameters_for(config, stage, component)
    return {"status": "valid"}


def remove_sensitive_runtime_files(destination):
    for path in destination.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.name in {"kubeconfig", "database.dump", "downloaded.dump"}:
            path.unlink()


def execute_step(config, settings, revision, step, destination):
    if step == "config-check":
        return check_configuration(config)
    if step in INFRASTRUCTURE_STEPS:
        stage, component = INFRASTRUCTURE_STEPS[step]
        return run_infrastructure(config, revision, stage, component, destination)
    if step in RUNTIME_STEPS:
        stage, action = RUNTIME_STEPS[step]
        require(action not in {"legacy-access-restrict", "legacy-access-restore"} or "legacyAccess" in config, "Configure legacyAccess before changing legacy source restrictions")
        return run_runtime(config, revision, stage, action, destination)
    if step == "connectivity-check":
        return run_connectivity_check(config, revision, destination)
    return run_backup_restore(config, settings, revision, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Path to the standalone local customer JSON file")
    parser.add_argument("--step", choices=STEPS, required=True)
    parser.add_argument("--output-root", type=Path, default=ROOT / "temp/local-stage01")
    args = parser.parse_args()
    config, settings = load_config(args.config)
    revision = reviewed_revision()
    destination = operation_root(args.output_root, args.step)
    config_sha = fingerprint({"customer": config, "localExecution": settings})
    try:
        result = execute_step(config, settings, revision, args.step, destination)
    except Exception as error:
        failure = {
            "version": 1,
            "mode": "local-direct",
            "environment": config["environment"],
            "step": args.step,
            "revision": revision,
            "configSha256": config_sha,
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "outputDirectory": str(destination.relative_to(ROOT)),
            "result": {"status": "failed", "errorType": type(error).__name__},
        }
        private_write(destination / "local-execution.json", json.dumps(failure, indent=2) + "\n")
        raise
    finally:
        remove_sensitive_runtime_files(destination)
    summary = {
        "version": 1,
        "mode": "local-direct",
        "environment": config["environment"],
        "step": args.step,
        "revision": revision,
        "configSha256": config_sha,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "outputDirectory": str(destination.relative_to(ROOT)),
        "result": result,
    }
    private_write(destination / "local-execution.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


def cli():
    try:
        main()
        return 0
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "local-stage01")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "local-stage01", "Local Stage0/1 execution failed")) from None