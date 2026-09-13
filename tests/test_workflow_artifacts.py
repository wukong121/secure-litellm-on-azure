import io
import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.workflow_artifacts import approved_operation, load_evidence, read_artifact, write_operation_receipt
from tests.test_customer_migration import customer_config
from scripts.workflow_security import SEALED_FILE, open_values, run_private, seal_directory, seal_values


class WorkflowArtifactTests(unittest.TestCase):
    def test_real_draft_subprocess_and_encrypted_review_need_no_cloud_or_plaintext_upload(self):
        from scripts.customer_migration import ROOT
        config = customer_config()
        environment = {"CUSTOMER_CONFIG_JSON": json.dumps(config), "CUSTOMER_ENVIRONMENT": "test", "MIGRATION_STAGE": "0", "GITHUB_SHA": "a" * 40,
                       "GITHUB_REPOSITORY": "synthetic/gateway", "GITHUB_RUN_ID": "123", "WORKFLOW_ARTIFACT_KEY": base64.b64encode(b"k" * 32).decode()}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as folder, patch.dict("os.environ", environment, clear=True), patch("scripts.workflow_security.ROOT", Path(folder)), patch("sys.stdout", new_callable=io.StringIO) as output:
            destination = Path(folder) / "temp/evidence"
            code = run_private("scripts.migration_evidence", ["--operation", "draft", "--stage", "0", "--environment", "test", "--output-dir", str(destination)])
            self.assertEqual(code, 0)
            self.assertNotIn(config["baseDomain"], output.getvalue())
            seal_directory(destination, "acceptance-draft-test-0-123", ["acceptance-report.json"])
            payload = (destination / SEALED_FILE).read_text()
            self.assertNotIn("checks", payload)
            restored = open_values(json.loads(payload), "synthetic/gateway", "123", "acceptance-draft-test-0-123")
            self.assertTrue(all(item["status"] == "pending" for item in restored["acceptance-report.json"]["checks"].values()))

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "synthetic/gateway", "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY_VISIBILITY": "public", "WORKFLOW_ARTIFACT_KEY": base64.b64encode(b"k" * 32).decode()})
    def test_public_artifact_round_trip_requires_ciphertext_and_rejects_mixed_archive(self):
        name = "acceptance-draft-test-0-123"
        values = {"acceptance-report.json": {"scope": "PRIVATE_SCOPE"}}
        envelope = seal_values(values, "synthetic/gateway", "123", name)
        entries = {SEALED_FILE: envelope}
        def api(route):
            if route.endswith("/zip"):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w") as archive:
                    for filename, value in entries.items():
                        archive.writestr(filename, json.dumps(value))
                return stream.getvalue()
            if "artifacts?" in route:
                return json.dumps({"total_count": 1, "artifacts": [{"id": 456, "name": name, "expired": False, "size_in_bytes": 1024}]}).encode()
            return json.dumps({"event": "workflow_dispatch", "conclusion": "success", "head_sha": "a" * 40, "head_branch": "main", "path": ".github/workflows/customer-acceptance.yml", "repository": {"full_name": "synthetic/gateway"}}).encode()
        self.assertEqual(read_artifact("a" * 40, "123", "customer-acceptance.yml", name, tuple(values), api), values)
        entries.update(values)
        with self.assertRaisesRegex(ValueError, "mix"):
            read_artifact("a" * 40, "123", "customer-acceptance.yml", name, tuple(values), api)
        entries.pop(SEALED_FILE)
        with self.assertRaisesRegex(ValueError, "encrypted"):
            read_artifact("a" * 40, "123", "customer-acceptance.yml", name, tuple(values), api)

    @patch.dict("os.environ", {"WORKFLOW_ARTIFACT_KEY": base64.b64encode(b"k" * 32).decode()}, clear=True)
    def test_private_runner_output_does_not_reach_console_or_public_summary(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder, patch("scripts.workflow_security.ROOT", Path(folder)), patch("sys.stdout", new_callable=io.StringIO) as output:
            summary = Path(folder) / "public-summary"
            def execute(arguments, **kwargs):
                kwargs["stdout"].write("PRIVATE_STDOUT")
                kwargs["stderr"].write("PRIVATE_STDERR\nPrior stage evidence is missing")
                Path(kwargs["env"]["GITHUB_STEP_SUMMARY"]).write_text("PRIVATE_SUMMARY")
                return SimpleNamespace(returncode=1)
            with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(summary)}), patch("scripts.workflow_security.subprocess.run", side_effect=execute):
                self.assertEqual(run_private("scripts.migration_runtime", []), 1)
            self.assertNotIn("PRIVATE_", output.getvalue() + summary.read_text())
            self.assertIn("prior-stage-not-confirmed", output.getvalue())

    @patch.dict("os.environ", {"WORKFLOW_ARTIFACT_KEY": base64.b64encode(b"k" * 32).decode()})
    def test_artifacts_hide_payload_and_authenticate_repository_run_and_name(self):
        data = {"runtime-review.json": {"synthetic": "PRIVATE_CUSTOMER_CONFIGURATION"}}
        envelope = seal_values(data, "synthetic/gateway", "123", "runtime-test-1-123")
        self.assertNotIn("PRIVATE_CUSTOMER_CONFIGURATION", json.dumps(envelope))
        self.assertEqual(open_values(envelope, "synthetic/gateway", "123", "runtime-test-1-123"), data)
        for repo, run, name in (("other/gateway", "123", "runtime-test-1-123"), ("synthetic/gateway", "124", "runtime-test-1-123"), ("synthetic/gateway", "123", "runtime-prod-1-123")):
            with self.assertRaises(ValueError):
                open_values(envelope, repo, run, name)
        with patch.dict("os.environ", {"WORKFLOW_ARTIFACT_KEY": base64.b64encode(b"x" * 32).decode()}), self.assertRaises(ValueError):
            open_values(envelope, "synthetic/gateway", "123", "runtime-test-1-123")

    @patch.dict("os.environ", {"WORKFLOW_ARTIFACT_KEY": base64.b64encode(b"k" * 32).decode()})
    def test_large_sbom_is_compressed_but_decompression_stays_bounded(self):
        values = {"sbom.json": {"packages": [{"name": "synthetic", "metadata": "x" * 100}] * 35000}}
        sealed = seal_values(values, "synthetic/gateway", "123", "sbom-123")
        self.assertLess(len(json.dumps(sealed)), 3 * 1024 * 1024)
        self.assertEqual(open_values(sealed, "synthetic/gateway", "123", "sbom-123"), values)
        with patch("scripts.workflow_security.MAX_PLAINTEXT", 1024), self.assertRaises(ValueError):
            open_values(sealed, "synthetic/gateway", "123", "sbom-123")

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "synthetic/gateway", "GITHUB_REF": "refs/heads/main"})
    def test_any_runtime_plan_is_bound_to_operation_and_configuration(self):
        config = customer_config()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name, value in (("runtime-summary.json", {"planSha256": "a" * 64}), ("runtime-review.json", {"desiredObjects": []})):
                (path / name).write_text(json.dumps(value))
            receipt = write_operation_receipt(path, config, "b" * 40, 7, "application", "plan", "runtime")
            def api(route):
                if route.endswith("/zip"):
                    archive = io.BytesIO()
                    with zipfile.ZipFile(archive, "w") as bundle:
                        for file in path.iterdir():
                            bundle.writestr(file.name, file.read_bytes())
                    return archive.getvalue()
                if "artifacts?" in route:
                    return json.dumps({"total_count": 1, "artifacts": [{"id": 456, "name": "runtime-test-7-123", "expired": False, "size_in_bytes": 1024}]}).encode()
                return json.dumps({"id": 123, "event": "workflow_dispatch", "conclusion": "success", "head_sha": "b" * 40, "head_branch": "main", "path": ".github/workflows/customer-runtime.yml", "repository": {"full_name": "synthetic/gateway"}}).encode()
            self.assertEqual(approved_operation(config, "b" * 40, 7, "application", "123", "runtime", api), "a" * 64)
            with self.assertRaises(ValueError):
                approved_operation(config, "b" * 40, 7, "proxy-credentials", "123", "runtime", api)
            receipt["operation"] = "execute"
            (path / "operation-receipt.json").write_text(json.dumps(receipt))
            with self.assertRaises(ValueError):
                approved_operation(config, "b" * 40, 7, "application", "123", "runtime", api)

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "synthetic/gateway", "GITHUB_REF": "refs/heads/main"})
    def test_missing_evidence_only_allowed_for_initial_stage(self):
        api = lambda route: b'{"workflow_runs":[]}'
        self.assertEqual(load_evidence(customer_config(), 0, "a" * 40, api=api), [])
        with self.assertRaisesRegex(ValueError, "Prior stage"):
            load_evidence(customer_config(), 5, "a" * 40, api=api)

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "synthetic/gateway", "GITHUB_REF": "refs/heads/main"})
    def test_rerecord_initial_stage_drops_obsolete_later_entries_only_for_recording(self):
        def api(route):
            if "workflows/" in route:
                return b'{"workflow_runs":[{"id":123}]}'
            return b'{"total_count":1,"artifacts":[{"name":"acceptance-record-test-8-123"}]}'
        invalid = [{"stage": 8, "configSha256": "obsolete"}]
        with patch("scripts.workflow_artifacts.read_artifact", return_value={"migration-evidence.json": invalid}):
            self.assertEqual(load_evidence(customer_config(), 0, "a" * 40, api=api, predecessors_only=True), [])
            with self.assertRaises(ValueError):
                load_evidence(customer_config(), 0, "a" * 40, api=api)