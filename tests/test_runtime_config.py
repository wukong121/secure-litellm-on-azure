import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from local_execution import runtime_config as flow
from scripts import model_sync_runtime as runtime
from scripts import runtime_config_probe as probe
from scripts.backend_manifest import application_settings, render_backend_manifest
from scripts.customer_migration import MigrationError, ROOT, fingerprint
from scripts.runtime_secrets import backend_secrets
from scripts.runtime_configuration import SUPPORTED_FIELDS
from tests.test_model_sync import APPROVERS, CLIENT, current_baseline, customer, runtime_documents


ADMIN = "77777777-7777-4777-8777-777777777777"


def native_customer():
    config = customer()
    config["application"]["authentication"] = {"mode": "native", "adminUsername": "gateway-admin"}
    config["contentAudit"] = {"mode": "native", "retentionDays": 7, "contentPolicyAccepted": True}
    return config


class RuntimeConfigIntegrationTests(unittest.TestCase):
    def test_patch_preserves_models_protected_settings_and_input_objects(self):
        config = native_customer()
        current = current_baseline(config)
        original = copy.deepcopy((config, current))
        settings = {"general_settings": {"disable_env_credential_login": True},
                    "router_settings": {"num_retries": 4}}
        desired, payload = flow.desired_configuration(config, current, settings)
        changed = yaml.safe_load(payload["configMap"]["data"]["config.yaml"])
        expected = copy.deepcopy(current["runtime"])
        expected["general_settings"]["disable_env_credential_login"] = True
        expected["router_settings"]["num_retries"] = 4
        self.assertEqual(changed, expected)
        self.assertEqual((config, current), original)
        self.assertEqual(desired["application"]["models"], config["application"]["models"])
        self.assertTrue(payload["configMap"]["immutable"])
        self.assertEqual(payload["patch"][-1]["path"],
                         "/spec/template/spec/volumes/0/configMap/name")
        self.assertEqual(desired["application"]["runtimeSettings"], settings)

    def test_existing_overrides_merge_without_deleting_omitted_fields(self):
        config = native_customer()
        config["application"]["runtimeSettings"] = {"router_settings": {"num_retries": 2}}
        current = current_baseline(config)
        desired, _ = flow.desired_configuration(config, current, {"litellm_settings": {"drop_params": True}})
        self.assertEqual(desired["application"]["runtimeSettings"], {
            "router_settings": {"num_retries": 2}, "litellm_settings": {"drop_params": True}})

    def test_full_render_preserves_runtime_overrides_and_protected_fields(self):
        config = native_customer()
        config["application"]["runtimeSettings"] = {
            "general_settings": {"disable_env_credential_login": True, "ui_access_mode": "admin_only"},
            "litellm_settings": {"drop_params": True},
            "router_settings": {"num_retries": 4}}
        platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": CLIENT,
                    "workloadIdentityPrincipalId": APPROVERS[0],
                    "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32,
                           "version": "a" * 32} for name in backend_secrets(config)}
        documents = render_backend_manifest(config, platform, versions,
                                            "synthetic.postgres.database.azure.com", "10.30.8.0/24")
        text = next(item for item in documents if item["kind"] == "ConfigMap")["data"]["config.yaml"]
        settings = yaml.safe_load(text)
        self.assertTrue(settings["general_settings"]["disable_env_credential_login"])
        self.assertFalse(settings["general_settings"]["store_model_in_db"])
        self.assertTrue(settings["general_settings"]["disable_prisma_schema_update"])
        self.assertTrue(settings["litellm_settings"]["enable_azure_ad_token_refresh"])
        self.assertEqual(settings["router_settings"]["num_retries"], 4)
        self.assertEqual(len(settings["model_list"]), len(config["application"]["models"]))

    def test_environment_login_override_rejected_for_entra(self):
        config = customer()
        config["application"]["runtimeSettings"] = {"general_settings": {"disable_env_credential_login": True}}
        with self.assertRaisesRegex(MigrationError, "native"):
            application_settings(config)

    def test_model_publication_preserves_declared_runtime_overrides(self):
        config = native_customer()
        current = current_baseline(config)
        config["application"]["runtimeSettings"] = {"general_settings": {"disable_env_credential_login": True}}
        payload = runtime.render(config, current)
        self.assertTrue(yaml.safe_load(payload["configMap"]["data"]["config.yaml"])
                        ["general_settings"]["disable_env_credential_login"])

    def test_baseline_rejects_runtime_override_drift(self):
        config = native_customer()
        config["application"]["runtimeSettings"] = {"general_settings": {"disable_env_credential_login": True}}
        deployment, account, cm = runtime_documents(config)
        with patch.object(runtime, "command", side_effect=[json.dumps(deployment), json.dumps(account), json.dumps(cm)]):
            with self.assertRaisesRegex(MigrationError, "runtime setting"):
                runtime.baseline(config, {"properties": {"clientId": CLIENT}}, ["kubectl"])

    def test_boolean_and_numeric_values_are_not_equivalent_configuration(self):
        config = native_customer()
        current = current_baseline(config)
        current["runtime"]["general_settings"]["disable_env_credential_login"] = 1
        desired, payload = flow.desired_configuration(
            config, current, {"general_settings": {"disable_env_credential_login": True}})
        self.assertTrue(payload["modelChanged"])
        deployment, account, cm = runtime_documents(config)
        settings = yaml.safe_load(cm["data"]["config.yaml"])
        settings["general_settings"]["disable_env_credential_login"] = 1
        cm["data"]["config.yaml"] = yaml.safe_dump(settings)
        with patch.object(runtime, "command", side_effect=[
                json.dumps(deployment), json.dumps(account), json.dumps(cm)]):
            with self.assertRaisesRegex(MigrationError, "runtime setting"):
                runtime.baseline(desired, {"properties": {"clientId": CLIENT}}, ["kubectl"])

    def test_source_fingerprint_covers_runtime_probe_and_schema(self):
        digest = flow.source_fingerprint()
        self.assertEqual(len(digest), 64)
        with patch.object(flow, "source_fingerprints", return_value={"scripts/runtime_configuration.py": "a"}):
            first = flow.source_fingerprint()
        with patch.object(flow, "source_fingerprints", return_value={"scripts/runtime_configuration.py": "b"}):
            self.assertNotEqual(first, flow.source_fingerprint())

    def test_malformed_live_yaml_error_withholds_configuration_values(self):
        with self.assertRaises(MigrationError) as raised:
            runtime.load_runtime_yaml("password: [synthetic-sensitive-value")
        self.assertNotIn("synthetic-sensitive-value", str(raised.exception))


class RuntimeConfigWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "temp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "customer.local.json"
        self.patch_file = self.directory / "settings.local.json"
        self.output = self.directory / "operations"
        self.config = native_customer()
        self.raw = copy.deepcopy(self.config)
        self.raw["localExecution"] = {"preserveThis": "synthetic-local-profile"}
        self.write_source(self.raw)
        self.write_patch({"router_settings": {"num_retries": 4}})
        self.current = current_baseline(self.config)
        self.installed = copy.deepcopy(self.current)

        self.load = self.start_patch("load_config", return_value=(self.config, {}))
        self.auth = self.start_patch("authenticate_azure")
        self.start_patch("AzureCommands")
        self.start_patch("reviewed_revision", return_value="a" * 40)
        self.start_patch("source_fingerprint", return_value="b" * 64)
        self.start_patch("effective_checks", return_value=[{"environmentLoginEnabled": True}])
        self.start_runtime_patch("identity_context", return_value={"identity": {"properties": {"clientId": CLIENT}}})
        self.start_runtime_patch("connect_cluster", return_value=["kubectl", "-n", "litellm"])
        self.baseline = self.start_runtime_patch("baseline", side_effect=self.live_baseline)
        self.dry_run = self.start_runtime_patch("dry_run")
        self.apply = self.start_runtime_patch("apply", side_effect=self.apply_payload)

    def start_patch(self, name, **kwargs):
        context = patch.object(flow, name, **kwargs)
        result = context.start()
        self.addCleanup(context.stop)
        return result

    def start_runtime_patch(self, name, **kwargs):
        context = patch.object(runtime, name, **kwargs)
        result = context.start()
        self.addCleanup(context.stop)
        return result

    def live_baseline(self, *_args):
        return self.installed

    def apply_payload(self, _kube, payload, _current, _directory, state, save_state):
        self.installed = copy.deepcopy(self.current)
        self.installed["runtime"] = yaml.safe_load(payload["configMap"]["data"]["config.yaml"])
        state.update(applicationPatched=payload["modelChanged"], rolloutVerified=True)
        save_state()

    def write_source(self, document):
        self.source.write_text(json.dumps(document), encoding="utf-8")
        self.source.chmod(0o600)

    def write_patch(self, document):
        self.patch_file.write_text(json.dumps({"schema_version": 1, **document}), encoding="utf-8")
        self.patch_file.chmod(0o600)

    def run_flow(self, operation="plan", approved_sha=None, **kwargs):
        return flow.sync(self.source, self.patch_file, operation, "synthetic-change", APPROVERS,
                         approved_sha, self.output, **kwargs)

    def plan_then_execute(self, **kwargs):
        plan = self.run_flow(**kwargs)
        return self.run_flow("execute", plan["planSha256"], **kwargs)

    def test_plan_is_read_only_and_preserves_customer_bytes(self):
        before = self.source.read_bytes()
        result = self.run_flow()
        self.assertEqual(result["status"], "planned")
        self.apply.assert_not_called()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(result["azureDeploymentPerformed"])
        self.assertFalse(result["applicationPatchAttempted"])
        directory = Path(result["outputDirectory"])
        review = json.loads((directory / "plan.json").read_text())
        self.assertEqual(review["settings"]["router_settings.num_retries"]["after"], 4)
        self.assertEqual(flow.sha((directory / "plan.json").read_bytes()), result["planSha256"])
        self.assertEqual((directory / "plan.json").stat().st_mode & 0o077, 0)

    def test_execute_persists_only_overrides_preserving_local_configuration(self):
        result = self.plan_then_execute()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["rolloutVerified"])
        self.assertTrue(result["effectiveAuthenticationVerified"])
        self.assertTrue(result["configInstalled"])
        self.assertFalse(result["inferenceVerified"])
        installed = json.loads(self.source.read_text())
        expected = copy.deepcopy(self.raw)
        expected["application"]["runtimeSettings"] = {"router_settings": {"num_retries": 4}}
        self.assertEqual(installed, expected)
        self.assertEqual(self.source.stat().st_mode & 0o077, 0)
        self.assertEqual(json.loads((Path(result["outputDirectory"]) / "customer-config.before.json").read_text()),
                         self.raw)

    def test_disabling_requires_independent_password_login_confirmation(self):
        self.write_patch({"general_settings": {"disable_env_credential_login": True}})
        for kwargs in ({}, {"verified_admin_user_id": ADMIN}, {"admin_password_login_verified": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MigrationError):
                self.run_flow(**kwargs)
        self.auth.assert_not_called()
        self.apply.assert_not_called()

    def test_disabling_binds_independent_admin_id_and_attestation(self):
        self.write_patch({"general_settings": {"disable_env_credential_login": True}})
        arguments = {"verified_admin_user_id": ADMIN, "admin_password_login_verified": True}
        result = self.plan_then_execute(**arguments)
        self.assertTrue(result["effectiveAuthenticationVerified"])
        settings = json.loads(self.source.read_text())["application"]["runtimeSettings"]
        self.assertTrue(settings["general_settings"]["disable_env_credential_login"])
        review = json.loads((Path(result["outputDirectory"]) / "plan.json").read_text())
        self.assertEqual(review["verifiedAdminUserId"], ADMIN)
        self.assertTrue(review["adminPasswordLoginVerified"])

    def test_changed_admin_identity_invalidates_approval(self):
        self.write_patch({"general_settings": {"disable_env_credential_login": True}})
        result = self.run_flow(verified_admin_user_id=ADMIN, admin_password_login_verified=True)
        with self.assertRaisesRegex(MigrationError, "differs from approval"):
            self.run_flow("execute", result["planSha256"], verified_admin_user_id="another-admin",
                          admin_password_login_verified=True)
        self.apply.assert_not_called()

    def test_changed_patch_invalidates_approval_before_writes(self):
        plan = self.run_flow()
        self.write_patch({"router_settings": {"num_retries": 5}})
        with self.assertRaisesRegex(MigrationError, "differs from approval"):
            self.run_flow("execute", plan["planSha256"])
        self.apply.assert_not_called()

    def test_changed_source_invalidates_approval_before_writes(self):
        plan = self.run_flow()
        with patch.object(flow, "source_fingerprint", return_value="c" * 64):
            with self.assertRaisesRegex(MigrationError, "differs from approval"):
                self.run_flow("execute", plan["planSha256"])
        self.apply.assert_not_called()

    def test_changed_live_baseline_invalidates_approval(self):
        plan = self.run_flow()
        self.installed = copy.deepcopy(self.current)
        self.installed["deploymentResourceVersion"] = "new-version"
        with self.assertRaisesRegex(MigrationError, "differs from approval"):
            self.run_flow("execute", plan["planSha256"])
        self.apply.assert_not_called()

    def test_missing_or_invalid_approval_performs_no_azure_reads(self):
        for digest in (None, "bad", "0" * 64):
            with self.subTest(digest=digest), self.assertRaises(MigrationError):
                self.run_flow("execute", digest)
        self.auth.assert_not_called()

    def test_only_latest_successful_plan_is_accepted(self):
        first = self.run_flow()
        self.write_patch({"router_settings": {"num_retries": 5}})
        second = self.run_flow()
        with self.assertRaisesRegex(MigrationError, "latest"):
            self.run_flow("execute", first["planSha256"])
        state_path = Path(second["outputDirectory"]) / "state.json"
        state = json.loads(state_path.read_text())
        state["status"] = "failed"
        state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(MigrationError, "finish successfully"):
            self.run_flow("execute", second["planSha256"])
        self.apply.assert_not_called()

    def test_effective_verification_failure_does_not_install_customer_config(self):
        plan = self.run_flow()
        before = self.source.read_bytes()
        with patch.object(flow, "effective_checks", side_effect=[
                [{"environmentLoginEnabled": True}], MigrationError("Effective login setting did not change")]):
            with self.assertRaisesRegex(MigrationError, "Effective login"):
                self.run_flow("execute", plan["planSha256"])
        self.assertEqual(self.source.read_bytes(), before)
        failed = list(self.output.glob("*-runtime-config-execute-*/state.json"))[0]
        state = json.loads(failed.read_text())
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["applicationPatched"])
        self.assertTrue(state["rolloutVerified"])
        self.assertFalse(state["configInstalled"])

    def test_partial_rollout_failure_records_attempt_and_preserves_customer_config(self):
        plan = self.run_flow()
        before = self.source.read_bytes()

        def fail(_kube, _payload, _current, _directory, state, save_state):
            state.update(applicationPatched=True, phase="rollout")
            save_state()
            raise MigrationError("Rollout failed")

        self.apply.side_effect = fail
        with self.assertRaisesRegex(MigrationError, "Rollout failed"):
            self.run_flow("execute", plan["planSha256"])
        self.assertEqual(self.source.read_bytes(), before)
        state = json.loads(next(self.output.glob("*-runtime-config-execute-*/state.json")).read_text())
        self.assertTrue(state["applicationPatchAttempted"])
        self.assertTrue(state["applicationPatched"])
        self.assertFalse(state["rolloutVerified"])
        self.assertFalse(state["configInstalled"])

    def test_invalid_patch_never_prints_embedded_secret(self):
        self.patch_file.write_text("schema_version: 1\npassword: [synthetic-sensitive-value\n")
        with self.assertRaises(MigrationError) as raised:
            self.run_flow()
        self.assertNotIn("synthetic-sensitive-value", str(raised.exception))
        self.auth.assert_not_called()

    def test_public_or_tracked_patch_is_rejected(self):
        self.patch_file.chmod(0o644)
        with self.assertRaisesRegex(MigrationError, "chmod 600"):
            self.run_flow()
        self.auth.assert_not_called()

    def test_input_change_during_rollout_prevents_overwrite(self):
        plan = self.run_flow()
        original_apply = self.apply_payload

        def mutate(*args, **kwargs):
            original_apply(*args, **kwargs)
            self.write_source({**self.raw, "unexpectedConcurrentChange": True})

        self.apply.side_effect = mutate
        with self.assertRaisesRegex(MigrationError, "changed during rollout"):
            self.run_flow("execute", plan["planSha256"])
        self.assertTrue(json.loads(self.source.read_text())["unexpectedConcurrentChange"])

    def test_noop_setting_is_verified_and_persisted_without_application_patch(self):
        value = self.current["runtime"]["router_settings"]["num_retries"]
        self.write_patch({"router_settings": {"num_retries": value}})
        result = self.plan_then_execute()
        self.assertFalse(result["applicationPatchAttempted"])
        self.assertFalse(result["applicationPatched"])
        self.assertTrue(result["rolloutVerified"])
        self.assertTrue(result["configInstalled"])

    def test_post_rollout_identity_drift_blocks_config_install(self):
        plan = self.run_flow()
        original = self.apply_payload

        def drift(*args, **kwargs):
            original(*args, **kwargs)
            self.installed["serviceAccountUid"] = "replacement-sa"

        self.apply.side_effect = drift
        with self.assertRaisesRegex(MigrationError, "identity or runtime"):
            self.run_flow("execute", plan["planSha256"])
        self.assertEqual(json.loads(self.source.read_text()), self.raw)


class RuntimeConfigProbeTests(unittest.TestCase):
    def test_admin_evidence_is_redacted_and_excludes_bootstrap_identity(self):
        document = {"user_id": ADMIN, "user_email": "synthetic@example.invalid", "user_role": "proxy_admin",
                    "password": "synthetic-password", "metadata": {"private": "synthetic-data"}}
        facts = probe.admin_facts(document, ADMIN, {"litellm-proxy-admin"})
        self.assertTrue(all(facts.values()))
        self.assertNotIn("synthetic-password", json.dumps(facts))
        self.assertNotIn("synthetic@example.invalid", json.dumps(facts))
        self.assertFalse(probe.admin_facts(document, ADMIN, {ADMIN})["independentIdentity"])

    def test_non_admin_missing_email_and_wrong_identity_rejected(self):
        for document in ({"user_id": ADMIN, "user_role": "internal_user", "user_email": "a@b.invalid"},
                         {"user_id": ADMIN, "user_role": "proxy_admin", "user_email": ""},
                         {"user_id": "different-user", "user_role": "proxy_admin", "user_email": "a@b.invalid"}):
            with self.subTest(document=document):
                self.assertFalse(all(probe.admin_facts(document, ADMIN, set()).values()))

    def test_loopback_probe_uses_csi_key_and_only_get_requests(self):
        opener = Mock()
        opener.open.side_effect = [
            io.StringIO(json.dumps({"show_env_credential_login_warning": False})),
            io.StringIO(json.dumps({"user_id": ADMIN, "user_email": "a@b.invalid", "user_role": "proxy_admin"}))]
        with patch.object(probe.Path, "read_text", return_value="synthetic-master-key\n"), \
                patch.object(probe, "build_opener", return_value=opener), \
                patch.dict(probe.os.environ, {}, clear=True):
            result = probe.probe(ADMIN)
        self.assertFalse(result["environmentLoginEnabled"])
        self.assertTrue(all(result["admin"].values()))
        self.assertNotIn("synthetic-master-key", json.dumps(result))
        for call in opener.open.call_args_list:
            request = call.args[0]
            self.assertTrue(request.full_url.startswith("http://127.0.0.1:4000/"))
            self.assertEqual(request.get_method(), "GET")
        self.assertIn("/v2/user/info?", opener.open.call_args_list[1].args[0].full_url)

    def test_missing_effective_flag_is_not_a_successful_fallback(self):
        opener = Mock()
        opener.open.return_value = io.StringIO("{}")
        with patch.object(probe.Path, "read_text", return_value="synthetic-key"), \
                patch.object(probe, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "effective"):
                probe.probe()

    def test_authenticated_redirects_are_prohibited(self):
        with self.assertRaisesRegex(ValueError, "redirects"):
            probe.NoRedirect().redirect_request(None, None, 302, "", {}, "https://unapproved.invalid")

    def test_effective_check_rejects_wrong_flag_and_unverified_admin(self):
        config = native_customer()
        current = current_baseline(config)
        wanted = copy.deepcopy(current["runtime"])
        wanted["general_settings"]["disable_env_credential_login"] = True
        for document in ({"environmentLoginEnabled": True},
                         {"environmentLoginEnabled": False, "admin": {"proxyAdmin": False}},
                         {"environmentLoginEnabled": "false"}):
            with self.subTest(document=document), patch.object(flow, "ready_pods", return_value=["pod-1"]), \
                    patch.object(runtime, "command", return_value=json.dumps(document)):
                with self.assertRaises(MigrationError):
                    flow.effective_checks(["kubectl"], current, wanted, "desired", ADMIN)

    def test_effective_check_covers_every_ready_pod(self):
        current = current_baseline(native_customer())
        with patch.object(flow, "ready_pods", return_value=["pod-1", "pod-2"]), \
                patch.object(runtime, "command", return_value='{"environmentLoginEnabled":true}') as command:
            facts = flow.effective_checks(["kubectl"], current, current["runtime"], "old-config")
        self.assertEqual([item["pod"] for item in facts], ["pod-1", "pod-2"])
        self.assertEqual(command.call_count, 2)

    def test_ready_pods_requires_exact_ready_owned_replicas(self):
        current = current_baseline(native_customer())
        deployment = copy.deepcopy(current["deployment"])
        replicasets = {"items": [{"metadata": {"uid": "rs-uid", "ownerReferences": [
            {"kind": "Deployment", "controller": True, "uid": current["deploymentUid"]}]}}]}

        def pod(name):
            return {"metadata": {"name": name, "labels": {"app": "litellm"}, "ownerReferences": [
                {"kind": "ReplicaSet", "controller": True, "uid": "rs-uid"}]},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]}}

        pods = {"items": [pod("pod-1"), pod("pod-2")]}
        for broken in ("none", "unready", "terminating", "unowned", "missing"):
            selected = copy.deepcopy(pods)
            if broken == "unready":
                selected["items"][0]["status"]["conditions"][0]["status"] = "False"
            elif broken == "terminating":
                selected["items"][0]["metadata"]["deletionTimestamp"] = "synthetic-time"
            elif broken == "unowned":
                selected["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "other-rs"
            elif broken == "missing":
                selected["items"].pop()
            with self.subTest(broken=broken), patch.object(runtime, "verify_deployment"), \
                    patch.object(runtime, "command", side_effect=[
                        json.dumps(deployment), json.dumps(replicasets), json.dumps(selected)]):
                if broken == "none":
                    self.assertEqual(flow.ready_pods(["kubectl"], current, "old-config"), ["pod-1", "pod-2"])
                else:
                    with self.assertRaises(MigrationError):
                        flow.ready_pods(["kubectl"], current, "old-config")
        terminating = pod("old-terminating-pod")
        terminating["metadata"]["deletionTimestamp"] = "synthetic-time"
        pods["items"].append(terminating)
        with patch.object(runtime, "verify_deployment"), patch.object(runtime, "command", side_effect=[
                json.dumps(deployment), json.dumps(replicasets), json.dumps(pods)]):
            self.assertEqual(flow.ready_pods(["kubectl"], current, "old-config"), ["pod-1", "pod-2"])


class RuntimeConfigDocumentationTests(unittest.TestCase):
    def test_runbook_documents_all_supported_fields_and_approval_and_persistence(self):
        document = ROOT / "docs/litellm-runtime-config-runbook-zh.md"
        text = document.read_text(encoding="utf-8")
        for section, fields in SUPPORTED_FIELDS.items():
            self.assertIn("`" + section + "`", text)
            for field in fields:
                self.assertIn("`" + field + "`", text)
        for required in ("--admin-password-login-verified", "--verified-admin-user-id",
                         "--approved-plan-sha256", "application.runtimeSettings",
                         "/app/config/config.yaml", "immutable", "configInstalled",
                         "show_env_credential_login_warning=false"):
            self.assertIn(required, text)
        for readme in ("README.md", "README_ZH.md"):
            self.assertIn("docs/litellm-runtime-config-runbook-zh.md",
                          (ROOT / readme).read_text(encoding="utf-8"))
        self.assertIn("!docs/litellm-runtime-config-runbook-zh.md", (ROOT / ".gitignore").read_text())


if __name__ == "__main__":
    unittest.main()
