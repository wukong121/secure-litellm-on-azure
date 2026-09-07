import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, mock_open
from unittest.mock import patch

import yaml

from LiteLLM.deploy_mi_aks_litellm import (
    AzureResourceManager,
    KubernetesManager,
    build_litellm_deployment,
    build_postgres_deployment,
    generate_litellm_config,
    get_subscription_id,
    load_config,
    parse_affinity_checks,
)


class LegacyConfigCompatibilityTests(unittest.TestCase):
    def load(self, fields):
        config = {"region": "westus", "azure-openai-list": [{"name": "synthetic"}], "deployment_list": [{"model": "synthetic"}], **fields}
        with patch("builtins.open", mock_open(read_data=json.dumps(config))):
            return load_config("synthetic.json")

    def test_new_and_legacy_resource_group_fields_are_normalized(self):
        for fields in ({"resource_group": "rg-synthetic"}, {"apim_resource_group": "rg-synthetic"}, {"resource_group": "rg-synthetic", "apim_resource_group": "rg-synthetic"}):
            with self.subTest(fields=fields):
                config = self.load(fields)
                self.assertEqual(config["resource_group"], "rg-synthetic")
                self.assertNotIn("apim_resource_group", config)

    def test_conflicting_resource_groups_fail_before_deployment(self):
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.load({"resource_group": "rg-new", "apim_resource_group": "rg-old"})

    def test_missing_or_invalid_resource_group_is_rejected(self):
        for fields in ({}, {"resource_group": " "}, {"resource_group": None}, {"resource_group": 42}, {"resource_group": "", "apim_resource_group": "rg-old"}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, "resource_group"):
                self.load(fields)


class SubscriptionSelectionTests(unittest.TestCase):
    def test_environment_variable_wins(self):
        with patch.dict(os.environ, {"AZURE_SUBSCRIPTION_ID": "env-sub"}, clear=False), \
             patch("LiteLLM.deploy_mi_aks_litellm.subprocess.check_output") as check_output:
            self.assertEqual(get_subscription_id(), "env-sub")
            check_output.assert_not_called()

    def test_falls_back_to_active_azure_cli_subscription(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch(
                 "LiteLLM.deploy_mi_aks_litellm._resolve_tool",
                 return_value="az",
             ), \
             patch(
                 "LiteLLM.deploy_mi_aks_litellm.subprocess.check_output",
                 return_value="cli-sub\n",
             ) as check_output:
            self.assertEqual(get_subscription_id(), "cli-sub")
            check_output.assert_called_once_with(
                ["az", "account", "show", "--query", "id", "-o", "tsv"],
                text=True,
            )

    def test_generated_config_enables_responses_affinity(self):
        config = {
            "azure-openai-list": [
                {
                    "name": "aoai-a",
                    "endpoint": "https://aoai-a.openai.azure.com/",
                }
            ],
            "deployment_list": [
                {"model": "gpt-test", "deployment_name": "gpt-test"}
            ],
        }
        settings = {
            "affinity_checks": [
                "responses_api_deployment_check",
                "deployment_affinity",
                "session_affinity",
            ],
            "deployment_affinity_ttl_seconds": 1800,
        }

        generated = yaml.safe_load(generate_litellm_config(config, settings))

        self.assertEqual(
            generated["router_settings"]["optional_pre_call_checks"],
            settings["affinity_checks"],
        )
        self.assertEqual(
            generated["router_settings"]["deployment_affinity_ttl_seconds"],
            1800,
        )
        self.assertEqual(
            generated["general_settings"]["maximum_spend_logs_retention_period"],
            "7d",
        )
        self.assertEqual(
            generated["general_settings"]["maximum_spend_logs_retention_interval"],
            "1d",
        )
        self.assertFalse(
            generated["general_settings"]["store_prompts_in_spend_logs"]
        )

    def test_existing_pvc_expands_only_when_enabled(self):
        manager = KubernetesManager.__new__(KubernetesManager)
        manager.namespace = "litellm"
        manager.core_v1 = Mock()
        manager.core_v1.read_namespaced_persistent_volume_claim.return_value = (
            SimpleNamespace(
                spec=SimpleNamespace(
                    resources=SimpleNamespace(requests={"storage": "1Gi"})
                )
            )
        )

        manager.apply_pvc("pg-data", "20Gi")
        manager.core_v1.patch_namespaced_persistent_volume_claim.assert_not_called()

        manager.apply_pvc("pg-data", "20Gi", expand_existing=True)
        manager.core_v1.patch_namespaced_persistent_volume_claim.assert_called_once_with(
            "pg-data",
            "litellm",
            {"spec": {"resources": {"requests": {"storage": "20Gi"}}}},
        )

    def test_litellm_deployment_rolls_when_config_hash_changes(self):
        first = build_litellm_deployment("litellm:test", "hash-a", "secret-hash")
        second = build_litellm_deployment("litellm:test", "hash-b", "secret-hash")

        self.assertEqual(first.spec.template.metadata.annotations["litellm.config-hash"], "hash-a")
        self.assertEqual(second.spec.template.metadata.annotations["litellm.config-hash"], "hash-b")
        self.assertNotEqual(
            first.spec.template.metadata.annotations["litellm.config-hash"],
            second.spec.template.metadata.annotations["litellm.config-hash"],
        )

    def test_litellm_deployment_has_stage1_resources_and_probes(self):
        deployment = build_litellm_deployment(
            "litellm@test", "config-hash", "secret-hash"
        )
        container = deployment.spec.template.spec.containers[0]

        self.assertEqual(container.resources.requests["cpu"], "250m")
        self.assertEqual(container.resources.requests["memory"], "1Gi")
        self.assertEqual(container.resources.limits["cpu"], "1000m")
        self.assertEqual(container.resources.limits["memory"], "2Gi")
        self.assertEqual(container.image_pull_policy, "IfNotPresent")
        self.assertEqual(container.startup_probe.http_get.path, "/health/liveliness")
        self.assertEqual(container.readiness_probe.http_get.path, "/health/readiness")
        self.assertEqual(container.liveness_probe.http_get.path, "/health/liveliness")

    def test_postgres_deployment_has_pg_isready_probes(self):
        deployment = build_postgres_deployment("litellm", "password", "litellm")
        container = deployment.spec.template.spec.containers[0]

        self.assertEqual(deployment.spec.strategy.type, "Recreate")
        for probe in (
            container.startup_probe,
            container.readiness_probe,
            container.liveness_probe,
        ):
            self.assertIn("pg_isready", probe._exec.command[-1])

    def test_secret_update_preserves_unmanaged_existing_keys(self):
        manager = KubernetesManager.__new__(KubernetesManager)
        manager.namespace = "litellm"
        manager.core_v1 = Mock()
        manager.core_v1.read_namespaced_secret.return_value = SimpleNamespace(
            metadata=SimpleNamespace(resource_version="42"),
            data={
                "LITELLM_SALT_KEY": "existing-base64-value",
                "UNMANAGED_STALE_KEY": "must-not-be-preserved",
            },
        )

        manager.apply_secret("litellm-env", {"LITELLM_MASTER_KEY": "new-value"})

        replacement = manager.core_v1.replace_namespaced_secret.call_args.args[2]
        self.assertEqual(replacement.metadata.resource_version, "42")
        self.assertEqual(
            replacement.data["LITELLM_SALT_KEY"], "existing-base64-value"
        )
        self.assertNotIn("UNMANAGED_STALE_KEY", replacement.data)
        self.assertEqual(replacement.string_data["LITELLM_MASTER_KEY"], "new-value")

    def test_affinity_checks_reject_unknown_values(self):
        with self.assertRaisesRegex(ValueError, "Unsupported LITELLM_AFFINITY_CHECKS"):
            parse_affinity_checks("responses_api_deployment_check,unknown-check")

    def test_existing_vmss_identity_skips_update(self):
        identity_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.ManagedIdentity/userAssignedIdentities/litellm"
        )
        manager = AzureResourceManager.__new__(AzureResourceManager)
        manager.compute_client = SimpleNamespace(
            virtual_machine_scale_sets=SimpleNamespace(
                get=Mock(
                    return_value=SimpleNamespace(
                        identity=SimpleNamespace(
                            type="UserAssigned",
                            user_assigned_identities={identity_id.lower(): {}},
                        )
                    )
                ),
                begin_update=Mock(),
            )
        )

        manager.assign_identity_to_vmss("vmss", "node-rg", identity_id)

        manager.compute_client.virtual_machine_scale_sets.begin_update.assert_not_called()

    def test_raises_when_no_subscription_can_be_resolved(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch(
                 "LiteLLM.deploy_mi_aks_litellm.subprocess.check_output",
                 side_effect=OSError("az unavailable"),
             ):
            with self.assertRaisesRegex(RuntimeError, "AZURE_SUBSCRIPTION_ID"):
                get_subscription_id()


if __name__ == "__main__":
    unittest.main()