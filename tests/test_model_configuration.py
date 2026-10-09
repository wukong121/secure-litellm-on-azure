import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts.backend_manifest import application_settings, render_backend_manifest
from scripts.customer_migration import ROOT, MigrationError, stage_fingerprint
from scripts.model_configuration import (
    COST_FIELDS, PARAM_FIELDS, INFO_FIELDS, normalize_openai_endpoint, pricing_warnings, validate_options,
)
from scripts.model_sync_catalog import discover, model_list, parse_catalog, reconcile
from scripts import model_sync_runtime as runtime
from scripts.runtime_secrets import backend_secrets
from tests.test_model_sync import CatalogAzure, SUB, OTHER_SUB, catalog, customer, current_baseline, runtime_documents


def resource_model(document, subscription=0, resource=0, model=0):
    return document["subscriptions"][subscription]["resources"][resource]["models"][model]


def reconciled(config, document, fallback=None, hostnames=None):
    accounts = parse_catalog(document, fallback)
    azure = CatalogAzure(accounts)
    azure.hostnames = {} if hostnames is None else hostnames
    observations, matches = discover(config, accounts, azure)
    return reconcile(config, matches), observations


class NestedCatalogTests(unittest.TestCase):
    def test_example_is_truly_nested_multisubscription_distinct_models(self):
        document = json.loads((ROOT / "local_execution/azure-openai.catalog.example.json").read_text())
        accounts = parse_catalog(document)
        self.assertEqual(len({a["subscriptionId"] for a in accounts}), 2)
        self.assertEqual(len(accounts), 3)
        desired, observations = reconciled(customer(), document, hostnames={"foundry-team-east": "aoai-team-east"})
        self.assertEqual(len(observations), 3)
        self.assertEqual(len(desired["application"]["models"]), 5)
        chat = [m for m in desired["application"]["models"] if m["modelGroup"] == "customer-chat"]
        self.assertEqual({m["deploymentName"] for m in chat}, {"existing-chat-east", "existing-chat-west"})
        self.assertEqual({m["apiVersion"] for m in chat}, {"2024-10-21"})
        embedding = next(m for m in desired["application"]["models"] if m["modelGroup"] == "customer-embedding")
        self.assertEqual(embedding["baseModel"], "azure/text-embedding-3-large")
        self.assertEqual(embedding["litellmParams"]["dimensions"], 1536)
        self.assertEqual(embedding["modelInfo"]["mode"], "embedding")
        self.assertEqual(desired["application"]["models"][0], customer()["application"]["models"][0])

    def test_each_model_has_its_own_api_version_and_cli_is_only_fallback(self):
        document = catalog()
        resource_model(document)["litellm_params"] = {"api_version": "2024-10-21"}
        resource_model(document)["model_info"] = {"mode": "chat"}
        resource = document["subscriptions"][0]["resources"][0]
        resource["models"].append({"model_name": "embed", "deployment_name": "existing-embed"})
        accounts = parse_catalog(document, "v1")
        self.assertEqual([m["apiVersion"] for m in accounts[0]["models"]], ["2024-10-21", "v1"])
        with self.assertRaisesRegex(MigrationError, "explicit --api-version"):
            parse_catalog(document)
        resource["models"][1]["litellm_params"] = {"api_version": "2025-04-01-preview"}
        self.assertEqual(parse_catalog(document)[0]["models"][1]["apiVersion"], "2025-04-01-preview")

    def test_rejects_legacy_not_silently_ignoring_apim_or_region(self):
        for legacy in ("apim_name", "apim_resource_group", "region", "azure-openai-list", "deployment_list"):
            document = catalog()
            document[legacy] = "obsolete"
            with self.subTest(field=legacy), self.assertRaisesRegex(MigrationError, "migrate"):
                parse_catalog(document, "v1")
        with self.assertRaisesRegex(MigrationError, "migrate"):
            parse_catalog({"azure-openai-list": [], "deployment_list": []}, "v1")

    def test_duplicate_subscriptions_resources_exact_models_and_bool_schema_rejected(self):
        for category in ("subscription", "resource", "mapping", "schema"):
            document = catalog()
            if category == "subscription":
                document["subscriptions"].append(copy.deepcopy(document["subscriptions"][0]))
            elif category == "resource":
                document["subscriptions"][0]["resources"].append(copy.deepcopy(document["subscriptions"][0]["resources"][0]))
            elif category == "mapping":
                document["subscriptions"][0]["resources"][0]["models"].append(copy.deepcopy(resource_model(document)))
            else:
                document["schema_version"] = True
            with self.subTest(category=category), self.assertRaises(MigrationError):
                parse_catalog(document, "v1")

    def test_mapped_deployment_cannot_be_satisfied_by_other_account(self):
        document = catalog(("synthetic-chat", "synthetic-west"), [SUB, OTHER_SUB])
        resource_model(document, subscription=1)["deployment_name"] = "west-only"
        accounts = parse_catalog(document, "v1")
        azure = CatalogAzure(accounts)
        azure.missing_deployments = {("synthetic-west", "west-only")}
        with self.assertRaisesRegex(MigrationError, "synthetic-west/deployments/west-only"):
            discover(customer(), accounts, azure)

    def test_foundry_endpoint_comes_from_live_arm_and_not_account_name_guess(self):
        accounts = parse_catalog(catalog(), "v1")
        azure = CatalogAzure(accounts)
        azure.kind = "AIServices"
        azure.hostname = "actual-openai-host"
        _, matches = discover(customer(), accounts, azure)
        desired = reconcile(customer(), matches)
        connection = desired["parameters"]["platform"]["azureOpenAIConnections"][-1]
        self.assertEqual(connection["accountName"], "synthetic-chat")
        self.assertEqual(connection["endpoint"], "https://actual-openai-host.openai.azure.com")
        self.assertEqual(model_list(desired)[-1]["litellm_params"]["api_base"], connection["endpoint"])
        with patch.object(runtime, "command", return_value="private-dns-tls-ok\n") as command:
            runtime.network_probe(["kubectl"], matches[0][0], ["10.30.8.5"])
        self.assertIn("actual-openai-host.openai.azure.com", command.call_args.args[1])

    def test_optional_expected_endpoint_normalizes_and_verifies_custom_hostname(self):
        document = catalog()
        resource = document["subscriptions"][0]["resources"][0]
        resource["name"] = "foundry-resource-name"
        resource["endpoint"] = "HTTPS://Custom-OpenAI-Host.OPENAI.AZURE.COM:443/"
        accounts = parse_catalog(document, "v1")
        self.assertEqual(accounts[0]["accountName"], "foundry-resource-name")
        self.assertEqual(accounts[0]["expectedEndpoint"], "https://custom-openai-host.openai.azure.com")
        azure = CatalogAzure(accounts)
        azure.hostname = "Custom-OpenAI-Host"
        observations, matches = discover(customer(), accounts, azure)
        self.assertEqual(observations[0]["expectedEndpoint"], accounts[0]["expectedEndpoint"])
        self.assertEqual(observations[0]["account"]["endpoint"], accounts[0]["expectedEndpoint"])
        self.assertNotIn("expectedEndpoint", matches[0][0])
        desired = reconcile(customer(), matches)
        self.assertEqual(desired["parameters"]["platform"]["azureOpenAIConnections"][-1]["accountName"], "foundry-resource-name")
        self.assertEqual(model_list(desired)[-1]["litellm_params"]["api_base"], "https://custom-openai-host.openai.azure.com")
        del resource["endpoint"]
        self.assertNotIn("expectedEndpoint", parse_catalog(document, "v1")[0])

    def test_wrong_resource_endpoint_stops_before_deployment_reads_or_probes(self):
        document = catalog(("synthetic-chat", "synthetic-west"), [SUB, OTHER_SUB])
        document["subscriptions"][0]["resources"][0]["endpoint"] = "https://synthetic-west.openai.azure.com"
        accounts = parse_catalog(document, "v1")
        azure = CatalogAzure(accounts)
        with patch.object(azure, "run", wraps=azure.run) as commands, \
                patch.object(runtime, "network_probe") as probe, \
                self.assertRaisesRegex(MigrationError, "authoritative ARM OpenAI endpoint"):
            discover(customer(), accounts, azure)
        self.assertFalse(any(call.args[0][0] == "rest" for call in commands.call_args_list))
        self.assertFalse(any("create" in call.args[0] for call in commands.call_args_list))
        probe.assert_not_called()

    def test_provided_endpoint_cannot_replace_unavailable_or_ambiguous_arm_metadata(self):
        for case in ("missing-endpoint", "null-endpoint", "missing-custom-name", "ambiguous-foundry", "missing-foundry"):
            document = catalog()
            document["subscriptions"][0]["resources"][0]["endpoint"] = "https://synthetic-chat.openai.azure.com"
            accounts = parse_catalog(document, "v1")
            azure = CatalogAzure(accounts)
            run = azure.run
            def response(args):
                result = run(args)
                if args[:2] == ["resource", "show"]:
                    if case == "missing-endpoint":
                        result["properties"].pop("endpoint")
                    elif case == "null-endpoint":
                        result["properties"]["endpoint"] = None
                    elif case == "missing-custom-name":
                        result["properties"].pop("customSubDomainName")
                    else:
                        result["kind"] = "AIServices"
                        result["properties"]["endpoints"] = (
                            {"OpenAI": "https://synthetic-chat.openai.azure.com",
                             "OpenAI Language Model Instance API": "https://different-host.openai.azure.com"} if case == "ambiguous-foundry" else {})
                return result
            with self.subTest(case=case), patch.object(azure, "run", side_effect=response) as commands, self.assertRaises(MigrationError):
                discover(customer(), accounts, azure)
            self.assertFalse(any(call.args[0][0] == "rest" for call in commands.call_args_list))

    def test_name_required_and_account_name_is_clear_migration_error(self):
        for both in (False, True):
            document = catalog()
            resource = document["subscriptions"][0]["resources"][0]
            resource["account_name"] = resource["name"]
            if not both:
                resource.pop("name")
            with self.subTest(both=both), self.assertRaisesRegex(MigrationError, "migrate to name"):
                parse_catalog(document, "v1")
        document = catalog()
        document["subscriptions"][0]["resources"][0].pop("name")
        with self.assertRaisesRegex(MigrationError, "requires resource_group, name"):
            parse_catalog(document, "v1")

    def test_authoritative_foundry_endpoints_allow_equivalent_case_port_and_slash(self):
        accounts = parse_catalog(catalog(), "v1")
        azure = CatalogAzure(accounts)
        run = azure.run
        def response(args):
            result = run(args)
            if args[:2] == ["resource", "show"]:
                result["kind"] = "AIServices"
                result["properties"]["customSubDomainName"] = "SYNTHETIC-CHAT"
                result["properties"]["endpoints"] = {
                    "OpenAI": "HTTPS://SYNTHETIC-CHAT.OPENAI.AZURE.COM:443/",
                    "OpenAI Language Model Instance API": "https://synthetic-chat.openai.azure.com"}
            return result
        with patch.object(azure, "run", side_effect=response):
            _, matches = discover(customer(), accounts, azure)
        self.assertEqual(matches[0][0]["endpoint"], "https://synthetic-chat.openai.azure.com")

    def test_expected_endpoint_rejects_unsafe_shapes_and_non_string_types(self):
        invalid = [None, True, 443, {}, [], "", "http://synthetic-chat.openai.azure.com",
                   "https://synthetic-chat.openai.azure.com:444", "https://synthetic-chat.openai.azure.com:bad",
                   "https://user@synthetic-chat.openai.azure.com", "https://user:password@synthetic-chat.openai.azure.com",
                   "https://synthetic-chat.openai.azure.com/path", "https://synthetic-chat.openai.azure.com//",
                   "https://synthetic-chat.openai.azure.com?", "https://synthetic-chat.openai.azure.com?api-version=v1",
                   "https://synthetic-chat.openai.azure.com#", "https://synthetic-chat.openai.azure.com#fragment",
                   "https://synthetic-chat.privatelink.openai.azure.com", "https://synthetic-chat.services.ai.azure.com",
                   "https://synthetic-chat.openai.azure.com.evil", "https://synthetic-chat.openai.azure.com.",
                   "https://synthetic-chat-.openai.azure.com", " https://synthetic-chat.openai.azure.com",
                   "https://synthetic-chat.openai.azure.com/\n", "https://synthetic-chat.openai.azure.com\t",
                   "https://[invalid", "https://synthetic-chat.openai.azure.com:0443",
                   "https://synthetic%2Dchat.openai.azure.com", "https://%73ynthetic-chat.openai.azure.com",
                   "https://synthetic-chat.openai.azure.com%3A443", "https://synthetic-chat.openai.azure.com/%2F",
                   "https://%75ser@synthetic-chat.openai.azure.com", "https://synthetic-chat.openai.azure.com@evil.invalid",
                   "https://synthetic-chat.openai.azure.com:", "https://synthetic-chat.openai.azure.com:-443",
                   "https://synthetic-chat.openai.azure.com:65536", "https://synthetic-chat.openai.azure.com:443:444"]
        for value in invalid:
            document = catalog()
            document["subscriptions"][0]["resources"][0]["endpoint"] = value
            with self.subTest(value=value), self.assertRaises(MigrationError):
                parse_catalog(document, "v1")
        self.assertEqual(normalize_openai_endpoint("HTTPS://SYNTHETIC-CHAT.OPENAI.AZURE.COM:443/"),
                         "https://synthetic-chat.openai.azure.com")


class ModelOptionsTests(unittest.TestCase):
    def test_all_accepted_fields_round_trip_with_exact_types(self):
        params = {"api_version": "v1", "seed": -42, "max_completion_tokens": 2048, "n": 1,
                  "rpm": 120, "tpm": 120000, "dimensions": 1536, "top_logprobs": 20, "num_retries": 0,
                  "temperature": 0.7, "top_p": 0.95, "presence_penalty": -1, "frequency_penalty": 1.5,
                  "timeout": 60, "stream": True, "logprobs": False, "stop": ["<END>", "done"],
                  "reasoning_effort": "high", "encoding_format": "float", "response_format": {"type": "json_object"}}
        info = {field: 0.000001 for field in COST_FIELDS}
        info.update(base_model="gpt-4o-2024-08-06", mode="chat", max_tokens=128000,
                    max_input_tokens=126000, max_output_tokens=16384)
        validate_options(params, info, catalog=True)
        self.assertEqual(set(params) | {"max_tokens"}, PARAM_FIELDS)
        self.assertEqual(set(info), INFO_FIELDS)
        document = catalog()
        resource_model(document).update(litellm_params=params, model_info=info)
        desired, _ = reconciled(customer(), document)
        rendered = model_list(desired)[-1]
        self.assertEqual(rendered["model_info"]["base_model"], "azure/gpt-4o-2024-08-06")
        self.assertEqual(rendered["litellm_params"]["stop"], ["<END>", "done"])
        self.assertEqual(rendered["model_info"]["cache_creation_input_token_cost"], 0.000001)
        self.assertEqual(rendered["litellm_params"]["max_completion_tokens"], 2048)

    def test_costs_are_finite_nonnegative_literal_numbers_not_zero_defaults(self):
        for value in (True, False, "0.1", "os.environ/COST", None, float("nan"), float("inf"), -float("inf"), -0.1, 10**1000):
            with self.subTest(value=str(value)[:20]), self.assertRaises(MigrationError):
                validate_options({}, {"input_cost_per_token": value}, catalog=True)
        validate_options({}, {"input_cost_per_token": 0, "output_cost_per_token": 0.0001}, catalog=True)
        desired, _ = reconciled(customer(), catalog(), "v1")
        self.assertNotIn("input_cost_per_token", model_list(desired)[-1]["model_info"])
        self.assertNotIn("output_cost_per_token", model_list(desired)[-1]["model_info"])
        self.assertEqual(pricing_warnings(desired), [])
        desired["application"]["models"][-1]["modelInfo"] = {"input_cost_per_token": 0, "output_cost_per_token": 0}
        self.assertEqual(len(pricing_warnings(desired)), 1)

    def test_invalid_parameter_types_and_ranges_rejected(self):
        invalid = {"max_tokens": [0, -1, True, 1.5, 2**31], "seed": [True, "42", 2**63],
                   "top_logprobs": [-1, 21, True], "num_retries": [-1, True],
                   "temperature": [-0.1, 2.1, True, float("nan")], "top_p": [-1, 1.1],
                   "presence_penalty": [-3, 3], "frequency_penalty": [-3, 3],
                   "timeout": [0, -1, float("inf"), "60"], "stream": [0, "true"],
                   "stop": ["", [], [""]], "reasoning_effort": ["arbitrary", None],
                   "response_format": [{"type": "json_schema"}, {"type": "text", "extra": True}, {"type": {}}],
                   "encoding_format": ["json"], "api_version": ["", "os.environ/VERSION"]}
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(MigrationError):
                    validate_options({field: value}, {}, catalog=True)
        with self.assertRaisesRegex(MigrationError, "only one"):
            validate_options({"max_tokens": 10, "max_completion_tokens": 10}, {}, catalog=True)

    def test_reserved_insecure_and_version_gated_fields_are_rejected(self):
        prohibited = {"model": "openai/other", "api_base": "https://other.invalid", "api_key": "os.environ/KEY",
                      "azure_ad_token": "literal", "azure_ad_token_provider": "callable", "token_provider": "callable",
                      "extra_headers": {"Authorization": "unsafe"}, "headers": {}, "custom_llm_provider": "openai",
                      "client_id": "override", "verify_ssl": False, "ssl_verify": False, "extra_body": {}, "tools": [],
                      "litellm_settings": {}, "general_settings": {}}
        for field, value in prohibited.items():
            with self.subTest(field=field), self.assertRaisesRegex(MigrationError, "Unsupported/reserved"):
                validate_options({field: value}, {}, catalog=True)
        for field in ("id", "api_base", "model", "secret", "supports_unknown", "tiered_pricing"):
            with self.subTest(field=field), self.assertRaises(MigrationError):
                validate_options({}, {field: 1}, catalog=True)
        for source in ("params", "info"):
            with self.subTest(source=source), self.assertRaisesRegex(MigrationError, ">=1.105"):
                validate_options({"cost_per_second": 1} if source == "params" else {},
                                 {"cost_per_second": 1} if source == "info" else {}, catalog=True)

    def test_no_null_objects_or_id_overrides_or_ambiguous_managed_fields(self):
        for field in ("litellm_params", "model_info"):
            document = catalog()
            resource_model(document)[field] = None
            with self.assertRaises(MigrationError):
                parse_catalog(document, "v1")
        for key, value in (("api_version", "v1"), ("base_model", "azure/gpt-4o")):
            config = customer()
            config["application"]["models"][0]["litellmParams"] = {key: value}
            with self.assertRaises(MigrationError):
                application_settings(config)
        config = customer()
        config["application"]["models"][0]["modelInfo"] = {"base_model": "azure/gpt-4o"}
        with self.assertRaises(MigrationError):
            application_settings(config)


class OverlayAndRenderingTests(unittest.TestCase):
    def existing_document(self):
        document = catalog(("synthetic-model",))
        resource_model(document).update(model_name="coding", deployment_name="gpt-deployment",
                                        litellm_params={"api_version": "v1"})
        return document

    def test_cost_only_overlay_preserves_id_unlisted_settings_and_other_models(self):
        config = customer()
        original = config["application"]["models"][0]
        original["litellmParams"] = {"temperature": 0.5, "max_tokens": 2048}
        original["modelInfo"] = {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002, "max_tokens": 128000}
        document = self.existing_document()
        resource_model(document)["model_info"] = {"input_cost_per_token": 0.000003}
        desired, _ = reconciled(config, document)
        mapping = desired["application"]["models"][0]
        self.assertEqual(mapping["id"], original["id"])
        self.assertEqual(mapping["litellmParams"], original["litellmParams"])
        self.assertEqual(mapping["modelInfo"], {**original["modelInfo"], "input_cost_per_token": 0.000003})
        self.assertEqual(mapping["baseModel"], original["baseModel"])
        self.assertEqual(desired["parameters"]["platform"], config["parameters"]["platform"])
        self.assertEqual(stage_fingerprint(desired, 4), stage_fingerprint(config, 4))
        self.assertEqual(stage_fingerprint(desired, 5), stage_fingerprint(config, 5))
        self.assertTrue(all(stage_fingerprint(desired, s) != stage_fingerprint(config, s) for s in range(6, 10)))
        current = current_baseline(config)
        payload = runtime.render(desired, current)
        self.assertTrue(payload["modelChanged"])
        self.assertNotEqual(payload["runtimeSha256"], runtime.render(config, current)["runtimeSha256"])
        self.assertNotEqual(payload["configMap"]["metadata"]["name"], runtime.render(config, current)["configMap"]["metadata"]["name"])
        self.assertEqual(payload["patch"][-1]["path"], "/spec/template/spec/volumes/0/configMap/name")
        self.assertEqual(reconciled(desired, document)[0], desired)

    def test_settings_only_overlay_no_implicit_clear_or_competing_token_limits(self):
        config = customer()
        config["application"]["models"][0]["litellmParams"] = {"temperature": 0.5, "max_tokens": 2048}
        document = self.existing_document()
        resource_model(document)["litellm_params"]["temperature"] = 1.2
        desired, _ = reconciled(config, document)
        self.assertEqual(desired["application"]["models"][0]["litellmParams"], {"temperature": 1.2, "max_tokens": 2048})
        resource_model(document)["litellm_params"]["max_completion_tokens"] = 3000
        with self.assertRaisesRegex(MigrationError, "only one"):
            reconciled(config, document)
        resource_model(document)["litellm_params"] = {"api_version": "v1", "max_tokens": None}
        with self.assertRaisesRegex(MigrationError, "null"):
            reconciled(config, document)

    def test_backend_and_model_only_renderer_match_exact_pricing_and_params(self):
        config = customer()
        document = self.existing_document()
        resource_model(document)["model_info"] = {"input_cost_per_token": 0.000003, "output_cost_per_token": 0.000004,
                                                 "base_model": "gpt-4o-2024-08-06", "mode": "chat"}
        resource_model(document)["litellm_params"].update(temperature=0.5, seed=42, stop="<END>")
        desired, _ = reconciled(config, document)
        platform = {"keyVaultName": "synthetic-backend", "workloadIdentityClientId": "33333333-3333-4333-8333-333333333333",
                    "workloadIdentityPrincipalId": "44444444-4444-4444-8444-444444444444", "managedRedisHostName": "synthetic.westus.redis.azure.net"}
        versions = {name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32,
                           "version": "a" * 32} for name in backend_secrets(desired)}
        documents = render_backend_manifest(desired, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24")
        full_config = yaml.safe_load(next(d for d in documents if d["kind"] == "ConfigMap")["data"]["config.yaml"])
        payload = runtime.render(desired, current_baseline(config))
        focused_config = yaml.safe_load(payload["configMap"]["data"]["config.yaml"])
        self.assertEqual(full_config["model_list"], focused_config["model_list"])
        mapping = focused_config["model_list"][0]
        self.assertEqual(mapping["litellm_params"]["base_model"], mapping["model_info"]["base_model"])
        self.assertEqual(mapping["litellm_params"]["stop"], "<END>")
        self.assertEqual(mapping["model_info"]["input_cost_per_token"], 0.000003)
        self.assertEqual(mapping["model_info"]["id"], config["application"]["models"][0]["id"])

    def test_baseline_drift_detects_cost_only_difference(self):
        config = customer()
        config["application"]["models"][0]["modelInfo"] = {"input_cost_per_token": 0.000001}
        deployment, service, config_map = runtime_documents(config)
        live = yaml.safe_load(config_map["data"]["config.yaml"])
        live["model_list"][0]["model_info"]["input_cost_per_token"] = 0.000009
        config_map["data"]["config.yaml"] = yaml.safe_dump(live)
        with patch.object(runtime, "command", side_effect=[json.dumps(deployment), json.dumps(service), json.dumps(config_map)]), \
                self.assertRaisesRegex(MigrationError, "differs from customer mapping"):
            runtime.baseline(config, {"properties": {"clientId": "33333333-3333-4333-8333-333333333333"}}, ["kubectl"])


if __name__ == "__main__":
    unittest.main()
