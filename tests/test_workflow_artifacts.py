import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.workflow_artifacts import approved_operation, load_evidence, write_operation_receipt
from tests.test_customer_migration import customer_config


class WorkflowArtifactTests(unittest.TestCase):
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