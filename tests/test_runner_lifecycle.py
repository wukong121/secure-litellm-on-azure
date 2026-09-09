import base64
import copy
import json
import unittest
from unittest.mock import Mock

from scripts.customer_migration import fingerprint
from scripts.runner_lifecycle import enroll_runner, retire_runner, runner_snapshot


class RunnerLifecycleTests(unittest.TestCase):
    def snapshot(self):
        tags = {"managedBy": "llmgw-runner", "repository": "synthetic/gateway", "environment": "test"}
        image = "/subscriptions/synthetic/resourceGroups/image/providers/Microsoft.Compute/galleries/tools/images/runner/versions/1.0.0"
        machine = {"id": "/synthetic/vm", "tags": tags, "properties": {"vmId": "synthetic-uid", "securityProfile": {"securityType": "TrustedLaunch", "encryptionAtHost": True, "uefiSettings": {"secureBootEnabled": True}}, "storageProfile": {"imageReference": {"id": image}}, "networkProfile": {"networkInterfaces": [{"id": "/synthetic/nic"}]}}}
        network = {"id": "/synthetic/nic", "tags": tags, "properties": {"ipConfigurations": [{"properties": {"subnet": {"id": "/synthetic/subnet"}}}]}}
        subnet = {"id": "/synthetic/subnet", "properties": {"routeTable": {"id": "/synthetic/firewall-route"}}}
        return runner_snapshot(machine, network, subnet, "synthetic/gateway", "test", image)

    def test_jit_only_uses_protected_parameter_and_never_enters_checkpoint(self):
        snapshot = self.snapshot()
        registration = Mock()
        registration.is_private_admin.return_value = True
        registration.find.return_value = []
        private = base64.b64encode(b"synthetic-registration-secret").decode()
        registration.generate.side_effect = lambda repo, body: {"runner": {"id": 123, "name": body["name"], "ephemeral": True}, "encoded_jit_config": private}
        command = Mock()
        saved = []
        result = enroll_runner(snapshot, fingerprint(snapshot), lambda: snapshot, registration, command, lambda record: saved.append(copy.deepcopy(record)))
        self.assertEqual(result["phase"], "started")
        self.assertNotIn(private, json.dumps(saved))
        body = command.call_args.args[1]["properties"]
        self.assertEqual(body["protectedParameters"], [{"name": "LLMGW_JIT_CONFIG", "value": private}])
        self.assertNotIn(private, body["source"]["script"])
        self.assertEqual(body["runAsUser"], "runner")

    def test_busy_or_replaced_runner_is_not_deleted(self):
        snapshot = self.snapshot()
        record = {"snapshot": snapshot, "name": "synthetic", "runnerId": 123}
        registration, delete, save = Mock(), Mock(), Mock()
        registration.find.return_value = [{"id": 123, "busy": True}]
        with self.assertRaisesRegex(ValueError, "busy"):
            retire_runner(record, lambda: snapshot, registration, delete, save)
        delete.assert_not_called()
        registration.find.return_value = []
        changed = {**snapshot, "vmId": "replacement"}
        with self.assertRaisesRegex(ValueError, "replaced"):
            retire_runner(record, lambda: changed, registration, delete, save)
        delete.assert_not_called()

    def test_unknown_jit_result_leaves_recoverable_checkpoint_not_a_second_runner(self):
        snapshot = self.snapshot()
        registration = Mock()
        registration.find.return_value = []
        registration.generate.side_effect = ValueError("synthetic acknowledgement loss")
        saved = []
        with self.assertRaises(ValueError):
            enroll_runner(snapshot, fingerprint(snapshot), lambda: snapshot, registration, Mock(), lambda record: saved.append(copy.deepcopy(record)))
        self.assertEqual(saved[0]["phase"], "registering")