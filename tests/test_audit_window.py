import copy
import json
import unittest

from scripts.audit_window import WINDOW_NAME, assert_paused, execute_window, plan_window
from scripts.customer_migration import fingerprint


class WindowClient:
    def __init__(self):
        self.resources = {
            ("deployment", "llm-api-proxy"): {"metadata": {"uid": "writer-uid"}, "spec": {"replicas": 2, "template": {"spec": {"serviceAccountName": "llm-api-proxy"}}}},
            ("cronjob", "l3-retention"): {"metadata": {"uid": "retention-uid"}, "spec": {"suspend": False, "jobTemplate": {"spec": {"template": {"spec": {"serviceAccountName": "l3-retention"}}}}}},
        }
        self.fail = None
        self.active_jobs = False
        self.quiet = True

    def get(self, kind, name, optional=False):
        return copy.deepcopy(self.resources.get((kind, name)))

    def create(self, value):
        key = (value["kind"].lower(), value["metadata"]["name"])
        assert key not in self.resources
        self.resources[key] = copy.deepcopy(value)
        self.resources[key]["metadata"]["uid"] = "window-uid"

    def patch(self, kind, name, operations):
        if self.fail == kind:
            raise ValueError("synthetic patch failure")
        resource = self.resources[(kind, name)]
        for operation in operations:
            keys = operation["path"].strip("/").split("/")
            owner = resource
            for key in keys[:-1]:
                owner = owner[key]
            if operation["op"] == "test":
                assert owner[keys[-1]] == operation["value"]
            else:
                owner[keys[-1]] = copy.deepcopy(operation["value"])

    def delete(self, kind, name, uid):
        assert self.resources[(kind, name)]["metadata"]["uid"] == uid
        del self.resources[(kind, name)]

    def assert_no_recovery_jobs(self):
        if self.active_jobs:
            raise ValueError("Recovery job is still active")

    def assert_quiet(self):
        if not self.quiet:
            raise ValueError("Writer is still active")

    def wait_quiet(self):
        self.assert_quiet()

    def wait_ready(self):
        pass


class AuditWindowTests(unittest.TestCase):
    scope = {"tenantId": "synthetic", "clusterId": "synthetic", "configSha256": "a" * 64}

    def execute(self, client, action):
        return execute_window(client, self.scope, action, fingerprint(plan_window(client, self.scope, action)))

    def test_plan_is_read_only_and_pause_resume_preserves_original_fields(self):
        client = WindowClient()
        client.resources[("cronjob", "l3-retention")]["spec"].pop("suspend")
        original = copy.deepcopy(client.resources)
        plan_window(client, self.scope, "audit-pause")
        self.assertEqual(client.resources, original)
        with self.assertRaisesRegex(ValueError, "approved"):
            execute_window(client, self.scope, "audit-pause", "f" * 64)
        self.execute(client, "audit-pause")
        self.assertEqual(client.get("deployment", "llm-api-proxy")["spec"]["replicas"], 0)
        self.assertTrue(client.get("cronjob", "l3-retention")["spec"]["suspend"])
        self.assertIn("windowId", assert_paused(client, self.scope))
        self.execute(client, "audit-resume")
        self.assertEqual(client.resources, original)

    def test_partial_pause_survives_restart_and_can_be_resumed(self):
        client = WindowClient()
        original = copy.deepcopy(client.resources)
        client.fail = "deployment"
        with self.assertRaisesRegex(ValueError, "synthetic"):
            self.execute(client, "audit-pause")
        self.assertIsNotNone(client.get("configmap", WINDOW_NAME))
        self.assertTrue(client.get("cronjob", "l3-retention")["spec"]["suspend"])
        with self.assertRaisesRegex(ValueError, "audit-pause"):
            assert_paused(client, self.scope)
        client.fail = None
        self.execute(client, "audit-resume")
        self.assertEqual(client.resources, original)

    def test_unquiet_writer_drift_replaced_workload_and_active_job_block(self):
        client = WindowClient()
        self.execute(client, "audit-pause")
        client.quiet = False
        with self.assertRaisesRegex(ValueError, "active"):
            assert_paused(client, self.scope)
        client.quiet = True
        writer = client.resources[("deployment", "llm-api-proxy")]
        writer["spec"]["replicas"] = 3
        with self.assertRaisesRegex(ValueError, "changed outside"):
            self.execute(client, "audit-resume")
        writer["spec"]["replicas"] = 0
        writer["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(ValueError, "replaced"):
            assert_paused(client, self.scope)
        writer["metadata"]["uid"] = "writer-uid"
        client.active_jobs = True
        with self.assertRaisesRegex(ValueError, "still active"):
            self.execute(client, "audit-resume")

    def test_interrupted_resume_retains_checkpoint_and_replans(self):
        client = WindowClient()
        original = copy.deepcopy(client.resources)
        self.execute(client, "audit-pause")
        client.fail = "cronjob"
        with self.assertRaises(ValueError):
            self.execute(client, "audit-resume")
        state = json.loads(client.get("configmap", WINDOW_NAME)["data"]["window.json"])
        self.assertEqual(state["phase"], "resuming")
        with self.assertRaisesRegex(ValueError, "audit-pause"):
            assert_paused(client, self.scope)
        client.fail = None
        self.execute(client, "audit-resume")
        self.assertEqual(client.resources, original)