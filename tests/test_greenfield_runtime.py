import tempfile
import json
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.customer_migration import ROOT, parameters_for, validate_config
from scripts.migration_runtime import backup_restore, connect_cluster, monitoring_onboard, publish, target_database_restore, validate_action
from tests.test_customer_migration import customer_config


class GreenfieldRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = customer_config()
        self.config["deploymentMode"] = "greenfield"
        self.config.pop("legacy")
        self.config["parameters"].pop("monitoring")

    def test_rejects_legacy_actions_before_any_cloud_or_cluster_call(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.migration_runtime.AzureCommands") as azure, patch("scripts.migration_runtime.run_command") as command:
            path = Path(directory)
            operations = (
                lambda: backup_restore(self.config, "a" * 40, path),
                lambda: target_database_restore(self.config, "plan", "a" * 40, path, ""),
                lambda: publish(self.config, 1, "legacy-hardening", "plan", "a" * 40, path, ""),
                lambda: monitoring_onboard(self.config, 1, "plan", "a" * 40, path, ""),
                lambda: connect_cluster(self.config, path, legacy=True),
            )
            for operation in operations:
                with self.assertRaises(ValueError):
                    operation()
            azure.assert_not_called()
            command.assert_not_called()

    def test_target_actions_are_available_in_both_modes(self):
        for config in (self.config, customer_config()):
            for stage, action in ((4, "cluster-bootstrap"), (4, "monitoring-onboard"), (6, "application"), (7, "application"), (8, "application")):
                validate_action(config, stage, action)

    def test_greenfield_network_needs_no_backup_owner_or_legacy_fields(self):
        self.config["parameters"]["network"] = {"virtualNetworkName": "target-vnet", "virtualNetworkAddressPrefix": "10.30.0.0/16", "privateEndpointSubnetName": "snet-private-endpoints", "privateEndpointSubnetPrefix": "10.30.8.0/24"}
        validate_config(self.config, "test")
        for component in ("bootstrap", "network"):
            _template, document = parameters_for(self.config, 0, component)
            self.assertNotIn("backupOwnerPrincipalId", document["parameters"])
            self.assertNotIn("legacy", document["parameters"])
        for update in ({"privateEndpointSubnetPrefix": "192.168.1.0/24"}, {"virtualNetworkName": "other-vnet"}):
            invalid = copy.deepcopy(self.config)
            invalid["parameters"]["network"].update(update)
            with self.assertRaises(ValueError):
                validate_config(invalid, "test")
        self.config["parameters"]["backup"] = {}
        with self.assertRaisesRegex(ValueError, "one bootstrap network owner"):
            validate_config(self.config, "test")

    def test_public_greenfield_example_has_no_migration_only_inputs(self):
        example = json.loads((ROOT / "config/customer.greenfield.example.json").read_text())
        self.assertEqual(example["deploymentMode"], "greenfield")
        self.assertNotIn("legacy", example)
        self.assertFalse({"backup", "monitoring", "legacy-logging"} & example["parameters"].keys())
        self.assertEqual(example["parameters"]["network"]["virtualNetworkName"], example["parameters"]["platform"]["stage4Network"]["virtualNetworkName"])
        for role in ("writer", "reader", "retention"):
            self.assertEqual(example["parameters"]["audit"][role + "PrincipalId"], "auto")