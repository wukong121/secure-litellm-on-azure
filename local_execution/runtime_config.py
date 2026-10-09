"""Approval-bound runtime settings publication; no Azure infrastructure deployment."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import yaml

from local_execution.model_sync import approvals, protected_source, source_fingerprints
from local_execution.release_report import install_report
from local_execution.runner import ROOT, authenticate_azure, load_config, operation_root, reviewed_revision
from scripts import model_sync_runtime as runtime
from scripts.customer_migration import MigrationError, fingerprint, private_write, require, stage_fingerprint, validate_config
from scripts.migration_deploy import AzureCommands
from scripts.runtime_configuration import apply_runtime_settings, parse_patch, runtime_overrides
from scripts.workflow_diagnostics import diagnostic_exit


PROBE = ROOT / "scripts/runtime_config_probe.py"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def source_fingerprint():
    sources = {path: digest for path, digest in source_fingerprints().items() if path.endswith(".py")}
    for path in ("local_execution/runtime_config.py", "scripts/runtime_config_probe.py",
                 "scripts/backend_access.py"):
        sources[path] = sha((ROOT / path).read_bytes())
    return fingerprint(sources)


def desired_configuration(config, current, patch):
    desired = copy.deepcopy(config)
    overrides = apply_runtime_settings(runtime_overrides(config), patch)
    desired["application"]["runtimeSettings"] = overrides
    validate_config(desired, desired["environment"])
    desired_runtime = apply_runtime_settings(current["runtime"], patch)
    payload = runtime.configuration_payload(desired_runtime, current)
    return desired, payload


def ready_pods(kube, current, wanted_name):
    deployment = json.loads(runtime.command(kube, ["get", "deployment", "litellm", "-o", "json"]))
    runtime.verify_deployment(kube, deployment, current, wanted_name)
    replicas = deployment["spec"].get("replicas", 1)
    require(type(replicas) is int and replicas > 0, "Runtime verification requires running replicas")
    selector = deployment["spec"]["selector"].get("matchLabels", {})
    require(bool(selector), "Backend selector cannot be empty")
    label_selector = ",".join(key + "=" + value for key, value in selector.items())
    replicasets = json.loads(runtime.command(kube, ["get", "replicasets", "-l", label_selector, "-o", "json"]))
    owned = {item["metadata"]["uid"] for item in replicasets["items"]
             if any(owner.get("kind") == "Deployment" and owner.get("controller") is True
                    and owner.get("uid") == current["deploymentUid"]
                    for owner in item["metadata"].get("ownerReferences", []))}
    items = json.loads(runtime.command(kube, ["get", "pods", "-l", label_selector, "-o", "json"]))["items"]
    result = []
    for pod in items:
        if pod.get("metadata", {}).get("deletionTimestamp"):
            continue
        owners = pod.get("metadata", {}).get("ownerReferences", [])
        require(any(owner.get("kind") == "ReplicaSet" and owner.get("controller") is True
                    and owner.get("uid") in owned for owner in owners),
                "Backend selector includes a Pod outside the approved Deployment")
        labels = pod.get("metadata", {}).get("labels", {})
        require(all(labels.get(key) == value for key, value in selector.items()),
                "Backend Pod labels differ from the approved selector")
        conditions = pod.get("status", {}).get("conditions", [])
        require(any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions),
                "Runtime verification requires every backend Pod to be Ready")
        result.append(pod["metadata"]["name"])
    require(len(result) == replicas, "Runtime verification did not find exactly the desired Ready replicas")
    return sorted(result)


def effective_checks(kube, current, desired_runtime, wanted_name, admin_user_id=""):
    facts = []
    expected_enabled = desired_runtime.get("general_settings", {}).get("disable_env_credential_login") is not True
    for name in ready_pods(kube, current, wanted_name):
        result = runtime.command(kube, ["exec", name, "-c", "litellm", "--", "python", "-c",
                                      PROBE.read_text(encoding="utf-8"), admin_user_id], timeout=75)
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            raise MigrationError("Runtime read-only API probe returned invalid evidence") from None
        require(isinstance(data, dict) and type(data.get("environmentLoginEnabled")) is bool,
                "Runtime read-only API probe lacks the effective authentication setting")
        require(data["environmentLoginEnabled"] == expected_enabled,
                "Effective environment-credential login differs from mounted configuration")
        if admin_user_id:
            admin = data.get("admin")
            require(isinstance(admin, dict) and set(admin) == {
                "userIdMatches", "proxyAdmin", "emailLoginIdentityPresent", "independentIdentity"}
                and all(value is True for value in admin.values()),
                "Verified independent password-login identity is not an available proxy admin")
        facts.append({"pod": name, "environmentLoginEnabled": data["environmentLoginEnabled"],
                      "independentAdminVerified": bool(admin_user_id)})
    return facts


def read_patch(path):
    protected_source(path, "Runtime patch")
    source = path.resolve()
    raw = source.read_bytes()
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError):
        raise MigrationError("Runtime patch must be valid UTF-8 JSON or YAML; values are withheld") from None
    return source, raw, parse_patch(document)


def latest_approval(base, digest):
    require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
            "Execute requires an approved runtime plan SHA-256")
    candidates = sorted(base.glob("*-runtime-config-plan-*/plan.json"))
    require(candidates, "No reviewed runtime configuration plan exists")
    latest = max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
    protected_source(latest, "Reviewed runtime plan")
    require(sha(latest.read_bytes()) == digest, "Approval must match the latest runtime configuration plan")
    try:
        approved = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MigrationError("Reviewed runtime configuration plan is unreadable") from None
    require(isinstance(approved, dict) and approved.get("operation") == "runtime-config",
            "Approval is not a runtime configuration plan")
    status = json.loads((latest.parent / "state.json").read_text(encoding="utf-8"))
    require(isinstance(status, dict) and status.get("status") == "planned"
            and status.get("planSha256") == digest, "Reviewed runtime plan did not finish successfully")
    return approved


def sync(config_path, patch_path, operation, change_ticket, approver_ids, approved_sha=None,
         output=Path("temp/runtime-config"), verified_admin_user_id=None,
         admin_password_login_verified=False):
    require(operation in {"plan", "execute"}, "Runtime operation must be plan or execute")
    protected_source(config_path, "Customer config")
    source = config_path.resolve()
    config_raw = source.read_bytes()
    config, settings = load_config(source)
    authorization = approvals(config, change_ticket, approver_ids)
    raw_config = json.loads(config_raw)
    patch_source, patch_raw, patch = read_patch(patch_path)
    require(source != patch_source, "Customer configuration and runtime patch must be separate files")
    native = config["application"].get("authentication", {}).get("mode", "entra") == "native"
    require(type(admin_password_login_verified) is bool, "Admin password-login attestation must be boolean")
    disabling = patch.get("general_settings", {}).get("disable_env_credential_login") is True
    configured_disabled = runtime_overrides(config).get("general_settings", {}).get("disable_env_credential_login") is True
    if disabling or configured_disabled:
        require(native, "Disabling environment credentials requires native authentication")
        require(isinstance(verified_admin_user_id, str) and verified_admin_user_id.strip()
                and verified_admin_user_id == verified_admin_user_id.strip()
                and len(verified_admin_user_id) <= 256 and "\x00" not in verified_admin_user_id,
                "Supply the independent proxy admin user ID before disabling environment login")
        require(admin_password_login_verified,
                "Confirm a successful independent password login in a fresh private browser before disabling environment login")
    else:
        require(verified_admin_user_id is None and not admin_password_login_verified,
                "Admin login confirmation applies only to an environment-login-disabled configuration")
    require(native or "disable_env_credential_login" not in patch.get("general_settings", {}),
            "Environment-credential login patches require native authentication")
    base = output.resolve()
    require(base.is_relative_to((ROOT / "temp").resolve()) and base != (ROOT / "temp").resolve(),
            "Runtime configuration output must stay under temp/")
    reviewed = latest_approval(base, approved_sha) if operation == "execute" else None
    directory = operation_root(base, "runtime-config-" + operation)
    state = {"status": "planning", "phase": "read-only", "azureDeploymentPerformed": False,
             "applicationPatchAttempted": False, "applicationPatched": False,
             "rolloutVerified": False, "effectiveAuthenticationVerified": False,
             "configInstalled": False, "inferenceVerified": False}

    def save():
        private_write(directory / "state.json", json.dumps(state, indent=2, sort_keys=True) + "\n")

    save()
    try:
        revision, sources = reviewed_revision(), source_fingerprint()
        authenticate_azure(config, settings, "deploy", directory)
        azure = AzureCommands(config, directory)
        identity = runtime.identity_context(config, azure)
        authenticate_azure(config, settings, "runtime", directory)
        kube = runtime.connect_cluster(config, directory, False)
        current = runtime.baseline(config, identity["identity"], kube)
        desired, payload = desired_configuration(config, current, patch)
        desired_document = copy.deepcopy(raw_config)
        desired_document["application"]["runtimeSettings"] = desired["application"]["runtimeSettings"]
        desired_runtime = yaml.safe_load(payload["configMap"]["data"]["config.yaml"])
        require(not disabling or native, "Native authentication is required for this login change")
        before_facts = effective_checks(kube, current, current["runtime"], current["configMapName"],
                                        verified_admin_user_id or "") if native else []
        payload["_baselineSpec"], payload["_volumeIndex"] = current["deployment"]["spec"], current["volumeIndex"]
        runtime.dry_run(kube, payload, directory)
        payload.pop("_baselineSpec")
        payload.pop("_volumeIndex")
        delta = {section + "." + key: {
            "before": current["runtime"].get(section, {}).get(key), "after": value}
            for section, values in patch.items() for key, value in values.items()}
        plan = {"schemaVersion": 1, "operation": "runtime-config",
                "customerConfigSha256": sha(config_raw), "patchSha256": sha(patch_raw),
                "sourceSha256": sources, "revision": revision, "approval": authorization,
                "identitySha256": fingerprint(identity), "settings": delta,
                "verifiedAdminUserId": verified_admin_user_id,
                "adminPasswordLoginVerified": admin_password_login_verified,
                "baselineSha256": sha(canonical(current)),
                "desiredConfigSha256": fingerprint(desired_document),
                "desiredRuntimeSha256": payload["runtimeSha256"],
                "configurationChanged": payload["modelChanged"],
                "staleStageFingerprints": [stage for stage in range(6, 10)
                                           if stage_fingerprint(config, stage) != stage_fingerprint(desired, stage)],
                "kubernetesDryRun": "verified" if payload["modelChanged"] else "not-required",
                "effectiveBaselineVerified": bool(before_facts),
                "writes": {"azureInfrastructure": False, "models": False, "credentials": False,
                          "immutableConfigMapAndRollout": payload["modelChanged"],
                          "customerConfig": desired_document != raw_config}}
        encoded = canonical(plan) + b"\n"
        digest = sha(encoded)
        private_write(directory / "plan.json", encoded.decode())
        private_write(directory / "customer-config.before.json", config_raw.decode())
        private_write(directory / "customer-config.desired.json", json.dumps(desired_document, indent=2) + "\n")
        private_write(directory / "runtime-config.before.yaml", yaml.safe_dump(current["runtime"], sort_keys=False))
        private_write(directory / "runtime-config.desired.yaml", payload["configMap"]["data"]["config.yaml"])
        private_write(directory / "baseline.json", json.dumps(current, indent=2) + "\n")
        pointer = f"/spec/template/spec/volumes/{current['volumeIndex']}/configMap/name"
        rollback = [{"op": "test", "path": "/metadata/uid", "value": current["deploymentUid"]},
                    {"op": "test", "path": pointer, "value": payload["configMap"]["metadata"]["name"]},
                    {"op": "replace", "path": pointer, "value": current["configMapName"]}]
        if payload["modelChanged"]:
            private_write(directory / "recovery-rollback-patch.json", json.dumps(rollback, indent=2) + "\n")
        state["planSha256"] = digest
        state["staleStageFingerprints"] = plan["staleStageFingerprints"]
        if operation == "plan":
            state.update(status="planned", phase="read-only")
            save()
            return {**state, "outputDirectory": str(directory)}
        require(reviewed == plan and approved_sha == digest,
                "Fresh runtime configuration plan differs from approval; create and review a new plan")
        require(source.read_bytes() == config_raw and patch_source.read_bytes() == patch_raw,
                "Runtime configuration inputs changed before publication")
        require(source_fingerprint() == sources and reviewed_revision() == revision,
                "Runtime configuration source changed before publication")
        require(runtime.baseline(config, identity["identity"], kube) == current,
                "Workload baseline changed before publication; no application writes performed")
        state.update(status="executing", phase="application")
        state["applicationPatchAttempted"] = payload["modelChanged"]
        state["recovery"] = ("Review state, mounted settings and the previous/desired customer documents before retrying. "
                             "A partial rollout may require an independently approved config-volume rollback "
                             "using recovery-rollback-patch.json with a fresh resourceVersion test. "
                             "Do not delete shared ConfigMaps, rotate secrets or rewrite failed-operation evidence.")
        save()
        runtime.apply(kube, payload, current, directory, state, save_state=save)
        state["phase"] = "effective-verification"
        save()
        if native:
            wanted_name = payload["configMap"]["metadata"]["name"] if payload["modelChanged"] else current["configMapName"]
            after_facts = effective_checks(kube, current, desired_runtime, wanted_name,
                                          verified_admin_user_id or "")
            private_write(directory / "effective-authentication.json", json.dumps(after_facts, indent=2) + "\n")
            state["effectiveAuthenticationVerified"] = True
        require(source.read_bytes() == config_raw and patch_source.read_bytes() == patch_raw,
                "Runtime configuration input changed during rollout; canonical config was not installed")
        installed = runtime.baseline(desired, identity["identity"], kube)
        require(installed["serviceAccountUid"] == current["serviceAccountUid"]
                and installed["serviceAccountSha256"] == current["serviceAccountSha256"]
                and fingerprint(installed["runtime"]) == payload["runtimeSha256"],
                "Post-rollout workload identity or runtime differs from the approved settings")
        state["phase"] = "customer-config"
        save()
        if desired_document != raw_config:
            install_report(desired_document, source, directory, replace=True)
        require(json.loads(source.read_text(encoding="utf-8")) == desired_document,
                "Installed customer configuration does not match approved settings")
        state.update(status="completed", phase="completed", configInstalled=True)
        save()
        return {**state, "outputDirectory": str(directory)}
    finally:
        if state["status"] not in {"planned", "completed"}:
            state["status"] = "failed"
        save()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", required=True, type=Path)
    result.add_argument("--patch", required=True, type=Path)
    result.add_argument("--operation", required=True, choices=("plan", "execute"))
    result.add_argument("--change-ticket", required=True)
    result.add_argument("--approved-by", action="append", required=True)
    result.add_argument("--approved-plan-sha256")
    result.add_argument("--output-dir", type=Path, default=Path("temp/runtime-config"))
    result.add_argument("--verified-admin-user-id")
    result.add_argument("--admin-password-login-verified", action="store_true",
                        help="Attest that this independent admin completed a password login in a fresh private browser")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = sync(args.config, args.patch, args.operation, args.change_ticket, args.approved_by,
                      args.approved_plan_sha256, args.output_dir, args.verified_admin_user_id,
                      args.admin_password_login_verified)
        print(json.dumps(result))
        return 0
    except (MigrationError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(diagnostic_exit(error, "local-runtime-config"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
