"""Onboard ONLY existing Azure OpenAI deployments to the private gateway."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID

from local_execution.release_report import install_report
from local_execution.runner import authenticate_azure, load_config, operation_root, reviewed_revision
from scripts.customer_migration import ROOT, MigrationError, approval_policy, fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands
from scripts.model_sync_catalog import discover, parse_catalog, reconcile
from scripts.model_configuration import pricing_warnings
from scripts.model_sync_infra import execute_infrastructure, infrastructure_plan
from scripts import model_sync_runtime as runtime
from scripts.workflow_diagnostics import diagnostic_exit


def progress(message):
    print("Model sync: " + message, file=sys.stderr, flush=True)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected_source(path, label):
    require(not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(ROOT.resolve()),
            label + " must be a regular file inside the reviewed checkout (not a symlink)")
    result = subprocess.run(["git", "check-ignore", "--quiet", "--", str(path.resolve())], cwd=ROOT, check=False)
    require(result.returncode == 0, label + " must be Git-ignored")
    require(path.stat().st_mode & 0o077 == 0, label + " must be private (chmod 600)")


def source_fingerprints():
    paths = ["local_execution/model_sync.py", "scripts/model_sync_catalog.py", "scripts/model_configuration.py",
             "local_execution/model_sync_evidence.py",
             "scripts/model_sync_infra.py", "scripts/model_sync_runtime.py",
             "local_execution/runner.py", "local_execution/release_report.py",
             "scripts/customer_migration.py", "scripts/backend_manifest.py",
             "scripts/migration_deploy.py", "scripts/migration_runtime.py",
             "scripts/private_ingress_runtime.py", "scripts/private_ingress.py",
             "scripts/edge_binding.py", "scripts/audit_runtime.py",
             "scripts/workflow_diagnostics.py", "infra/model-sync/main.bicep",
             "infra/modules/aoai-private-endpoint/main.bicep", "infra/modules/model-access-role/main.bicep",
             "infra/environments/main.bicep"]
    return {path: sha(ROOT / path) for path in paths}


def approvals(config, ticket, approvers):
    require(isinstance(ticket, str) and bool(ticket.strip()) and len(ticket) <= 256
            and not any(ord(c) < 32 for c in ticket), "Supply the actual approved change-ticket reference")
    mode, eligible = approval_policy(config)
    identities = [UUID(value) for value in approvers]
    require(len(identities) == (1 if mode == "single-operator" else 2)
            and len(set(identities)) == len(identities) and all(value.int for value in identities),
            "Actual distinct approved-by object IDs must meet customer approval policy")
    require(not eligible or set(identities) <= eligible, "Approver is outside the customer eligible operator policy")
    return {"changeTicket": ticket, "approvedBy": [str(value) for value in identities], "approvalMode": mode}


def compile_template(directory):
    path = directory / "model-sync.template.json"
    result = subprocess.run(["az", "bicep", "build", "--file", str(ROOT / "infra/model-sync/main.bicep"),
                             "--outfile", str(path)], capture_output=True, text=True, check=False, timeout=120)
    require(result.returncode == 0 and path.is_file(), "Focused model-sync Bicep build failed")
    path.chmod(0o600)
    return path


def prepare(config, settings, desired, matches, identity, kube, template, directory, azure):
    current = runtime.baseline(config, identity["identity"], kube)
    payload = runtime.render(desired, current)
    payload["_baselineSpec"] = current["deployment"]["spec"]
    payload["_volumeIndex"] = current["volumeIndex"]
    runtime.dry_run(kube, payload, directory)
    payload.pop("_baselineSpec")
    payload.pop("_volumeIndex")
    authenticate_azure(config, settings, "deploy", directory)
    infrastructure = infrastructure_plan(config, desired, matches, identity["identity"], template, directory, azure)
    authenticate_azure(config, settings, "runtime", directory)
    for item in infrastructure["accounts"]:
        if not item["endpoint"]["createEndpoint"] and not item["endpoint"]["createDnsBinding"] and infrastructure["context"]["links"]:
            runtime.network_probe(kube, item["account"], item["endpoint"]["ips"])
    return current, payload, infrastructure


def sync(config_path, catalog_path, api_version, operation, approved="", ticket="", approvers=(), model_policy="replace"):
    require(operation in {"plan", "execute"}, "Operation must be plan or execute")
    require(model_policy in {"merge", "replace"}, "Model policy must be merge or replace")
    protected_source(config_path, "Customer config")
    protected_source(catalog_path, "Azure OpenAI catalog")
    require(config_path.resolve() != catalog_path.resolve(), "Config and catalog must be separate files")
    config, settings = load_config(config_path)
    authorization = approvals(config, ticket, approvers)
    config_bytes, catalog_bytes = config_path.read_bytes(), catalog_path.read_bytes()
    raw = json.loads(config_bytes)
    accounts = parse_catalog(json.loads(catalog_bytes), api_version)
    revision, sources = reviewed_revision(), source_fingerprints()
    directory = operation_root(ROOT / "temp/model-sync", "model-sync-" + operation)
    record = {"status": "planning", "phase": "read-only", "infrastructureCompleted": [],
              "applicationPatched": False, "rolloutVerified": False, "configInstalled": False,
              "inferenceVerified": False, "outputDirectory": str(directory.relative_to(ROOT))}
    private_write(directory / "model-sync-state.json", json.dumps(record, indent=2))
    progress("Validating existing accounts, deployments, identity and private workload baseline; no Azure writes.")
    try:
        authenticate_azure(config, settings, "deploy", directory)
        azure = AzureCommands(config, directory)
        observations, matches = discover(config, accounts, azure)
        private_write(directory / "discovery.json", json.dumps(observations, indent=2))
        desired = reconcile(config, matches, model_policy)
        retained_ids = {mapping["id"] for mapping in desired["application"]["models"]}
        removed = [{key: mapping[key] for key in ("id", "modelGroup", "connectionAlias", "deploymentName")}
                   for mapping in config["application"]["models"]
                   if mapping["id"] not in retained_ids]
        groups = {mapping["modelGroup"] for mapping in desired["application"]["models"]}
        removed_groups = sorted({mapping["modelGroup"] for mapping in removed} - groups)
        for group in removed_groups:
            progress("Planned removal of client model name " + group + "; update dependent clients/vkey permissions separately.")
        warnings = pricing_warnings(desired)
        for warning in warnings:
            progress(warning)
        desired_document = copy.deepcopy(raw)
        desired_document["parameters"]["platform"]["azureOpenAIConnections"] = desired["parameters"]["platform"]["azureOpenAIConnections"]
        desired_document["application"]["models"] = desired["application"]["models"]
        private_write(directory / "desired-customer.json", json.dumps(desired_document, indent=2) + "\n")
        identity = runtime.identity_context(config, azure)
        # Runtime identity can use a separately configured customer Azure profile.
        authenticate_azure(config, settings, "runtime", directory)
        kube = runtime.connect_cluster(config, directory, False)
        template = compile_template(directory)
        current, payload, infra = prepare(config, settings, desired, matches, identity, kube, template, directory, azure)
        stale = [stage for stage in range(4, 10) if stage_fingerprint(config, stage) != stage_fingerprint(desired, stage)]
        review = {"schemaVersion": 1, "operation": "model-sync", "revision": revision, "sourceSha256": sources,
                  "configInputSha256": hashlib.sha256(config_bytes).hexdigest(),
                  "catalogInputSha256": hashlib.sha256(catalog_bytes).hexdigest(), "apiVersion": api_version,
                  "modelPolicy": model_policy, "removedModels": removed, "removedModelGroups": removed_groups,
                  "approval": authorization, "identity": identity, "discovery": observations,
                  "baseline": current, "desiredConfigSha256": fingerprint(desired_document),
                  "templateSha256": sha(template), "infrastructure": infra, "application": payload,
                  "staleStageFingerprints": stale,
                  "catalogSchemaVersion": 1, "pricingWarnings": warnings}
        digest = fingerprint(review)
        private_write(directory / "model-sync-review.json", json.dumps({**review, "planSha256": digest}, indent=2))
        record.update(planSha256=digest, status="planned", staleStageFingerprints=stale, pricingWarnings=warnings,
                      modelPolicy=model_policy, removedModels=removed, removedModelGroups=removed_groups,
                      unusedAccounts=[o["account"]["accountName"] for o in observations if not o["matchedDeployments"]])
        private_write(directory / "model-sync-state.json", json.dumps(record, indent=2))
        if operation == "plan":
            return record
        require(approved == digest, "Fresh model-sync plan differs from approved SHA256; no writes performed, review and replan")
        require(reviewed_revision() == revision and source_fingerprints() == sources
                and config_path.read_bytes() == config_bytes and catalog_path.read_bytes() == catalog_bytes,
                "Code or input changed during planning; no writes performed")
        private_write(directory / "previous-customer.json", config_bytes.decode())
        rollback = [{"op": "test", "path": "/metadata/uid", "value": current["deploymentUid"]},
                    {"op": "test", "path": f"/spec/template/spec/volumes/{current['volumeIndex']}/configMap/name",
                     "value": payload["configMap"]["metadata"]["name"]},
                    {"op": "replace", "path": f"/spec/template/spec/volumes/{current['volumeIndex']}/configMap/name",
                     "value": current["configMapName"]}]
        private_write(directory / "recovery-rollback-patch.json", json.dumps(rollback, indent=2))
        record.update(status="executing", phase="infrastructure",
                      recovery="Inspect receipts/state and live Deployment before retrying. Do not remove shared PE/DNS/roles. "
                      "If rollout/config install failed after a patch, an independently reviewed model-only rollback may "
                      "use recovery-rollback-patch.json (add a fresh resourceVersion test), then restore previous-customer.json. "
                      "Alternatively verify the desired live mapping and install desired-customer.json before replanning.")
        private_write(directory / "model-sync-state.json", json.dumps(record, indent=2))
        # Switch back to the infrastructure profile only after the fresh approved review.
        authenticate_azure(config, settings, "deploy", directory)
        progress("Fresh approval verified. Applying only missing reviewed Private Endpoint/DNS/data-plane roles.")
        execute_infrastructure(config, infra, template, directory, azure, record)
        authenticate_azure(config, settings, "runtime", directory)
        for item in infra["accounts"]:
            runtime.network_probe(kube, item["account"], item["verifiedIps"])
        # Revalidate ConfigMap/service account/workload after prerequisite deployment.
        fresh = runtime.baseline(config, identity["identity"], kube)
        require(fresh == current, "Live workload baseline changed while applying prerequisites; application update blocked")
        require(config_path.read_bytes() == config_bytes and catalog_path.read_bytes() == catalog_bytes
                and source_fingerprints() == sources and reviewed_revision() == revision,
                "Inputs changed during prerequisite deployment; application update blocked")
        progress("Private DNS/TLS and approved prerequisites verified. Applying model-only config and checking rollout.")
        record["applicationPatchAttempted"] = payload["modelChanged"]
        private_write(directory / "model-sync-state.json", json.dumps(record, indent=2))
        runtime.apply(kube, payload, current, directory, record)
        require(config_path.read_bytes() == config_bytes, "Customer config changed before installation; recovery required")
        if raw != desired_document:
            install_report(desired_document, config_path, directory, replace=True)
            record["configInstalled"] = True
        record.update(status="completed", phase="verified",
                      noOp=not payload["modelChanged"] and raw == desired_document
                      and not any(item["allowedResources"] for item in infra["accounts"]))
        progress("Rollout and mounted mapping verified. Inference was NOT tested; downstream evidence is not refreshed.")
        return record
    except Exception:
        record["status"] = "failed"
        raise
    finally:
        private_write(directory / "model-sync-state.json", json.dumps(record, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--api-version", help="Optional explicit fallback; per-model litellm_params.api_version takes precedence")
    parser.add_argument("--model-policy", choices=("merge", "replace"), default="replace",
                        help="replace uses catalog as the complete gateway model list (default); merge preserves unlisted models")
    parser.add_argument("--operation", choices=("plan", "execute"), required=True)
    parser.add_argument("--approved-plan-sha256", default="")
    parser.add_argument("--change-ticket", required=True)
    parser.add_argument("--approved-by", action="append", required=True)
    args = parser.parse_args()
    result = sync(args.config, args.catalog, args.api_version, args.operation,
                  args.approved_plan_sha256, args.change_ticket, args.approved_by, args.model_policy)
    print(json.dumps(result), flush=True)


def cli():
    try:
        main()
    except MigrationError as error:
        raise SystemExit(diagnostic_exit(error, "local-model-sync")) from None
    except Exception as error:
        raise SystemExit(diagnostic_exit(error, "local-model-sync",
                                        "Model sync failed; inspect protected state/receipts before retrying")) from None


if __name__ == "__main__":
    cli()
