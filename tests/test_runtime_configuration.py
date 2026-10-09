import copy
import unittest

from scripts.customer_migration import MigrationError
from scripts.runtime_configuration import (
    ROUTING_STRATEGIES, SUPPORTED_FIELDS, apply_runtime_settings, parse_patch, runtime_overrides,
    validate_runtime_settings,
)


VALID_SETTINGS = {
    "general_settings": {"disable_env_credential_login": True, "ui_access_mode": "admin_only"},
    "litellm_settings": {
        "request_timeout": 120.5, "drop_params": True, "modify_params": False,
        "force_ipv4": True, "set_verbose": False,
    },
    "router_settings": {
        "routing_strategy": "simple-shuffle", "num_retries": 0, "allowed_fails": 3,
        "cooldown_time": 0, "timeout": 60, "stream_timeout": 90.5, "retry_after": 0.5,
        "enable_pre_call_checks": True,
    },
}
BOOLEAN_FIELDS = {
    "general_settings": {"disable_env_credential_login"},
    "litellm_settings": {"drop_params", "modify_params", "force_ipv4"},
    "router_settings": {"enable_pre_call_checks"},
}
POSITIVE_DURATIONS = {
    "litellm_settings": {"request_timeout"},
    "router_settings": {"timeout", "stream_timeout"},
}
NONNEGATIVE_DURATIONS = {"router_settings": {"cooldown_time", "retry_after"}}
SECRET = "SECRET_SENTINEL_DO_NOT_DISCLOSE"


class RuntimeConfigurationTests(unittest.TestCase):
    def assert_invalid(self, value, validator=validate_runtime_settings):
        original = copy.deepcopy(value)
        with self.assertRaises(MigrationError) as raised:
            validator(value)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(SECRET, repr(raised.exception))
        # NaN is not equal to itself; compare repr to check rejection did not mutate input.
        self.assertEqual(repr(value), repr(original))

    def test_supported_fields_are_exact_and_every_field_has_a_valid_example(self):
        expected = {
            "general_settings": {"disable_env_credential_login", "ui_access_mode"},
            "litellm_settings": {
                "request_timeout", "drop_params", "modify_params", "force_ipv4", "set_verbose",
            },
            "router_settings": {
                "routing_strategy", "num_retries", "allowed_fails", "cooldown_time", "timeout",
                "stream_timeout", "retry_after", "enable_pre_call_checks",
            },
        }
        self.assertEqual(SUPPORTED_FIELDS, expected)
        self.assertEqual({section: set(fields) for section, fields in VALID_SETTINGS.items()}, expected)
        self.assertEqual(validate_runtime_settings(VALID_SETTINGS), VALID_SETTINGS)
        for section, settings in VALID_SETTINGS.items():
            for field, value in settings.items():
                with self.subTest(section=section, field=field):
                    document = {section: {field: value}}
                    self.assertEqual(validate_runtime_settings(document), document)
                    self.assertEqual(parse_patch({"schema_version": 1, **document}), document)

    def test_validation_deep_copies_and_does_not_insert_defaults(self):
        original = copy.deepcopy(VALID_SETTINGS)
        result = validate_runtime_settings(original)
        self.assertIsNot(result, original)
        for section in result:
            self.assertIsNot(result[section], original[section])
        result["router_settings"]["num_retries"] = 20
        self.assertEqual(original, VALID_SETTINGS)
        sparse = {"router_settings": {"num_retries": 0}}
        self.assertEqual(validate_runtime_settings(sparse), sparse)
        self.assertEqual(validate_runtime_settings({}), {})
        self.assertEqual(validate_runtime_settings({"general_settings": {}}), {"general_settings": {}})

    def test_boolean_fields_are_strict(self):
        for section, fields in BOOLEAN_FIELDS.items():
            for field in fields:
                for value in (True, False):
                    with self.subTest(section=section, field=field, valid=value):
                        self.assertEqual(validate_runtime_settings({section: {field: value}}),
                                         {section: {field: value}})
                for value in (0, 1, 0.0, 1.0, "true", SECRET, [], {}):
                    with self.subTest(section=section, field=field, invalid_type=type(value)):
                        self.assert_invalid({section: {field: value}})

    def test_verbose_only_accepts_literal_false(self):
        self.assertEqual(validate_runtime_settings({"litellm_settings": {"set_verbose": False}}),
                         {"litellm_settings": {"set_verbose": False}})
        for value in (True, 0, 1, 0.0, "false", SECRET, [], {}, float("nan"), float("inf")):
            with self.subTest(value_type=type(value)):
                self.assert_invalid({"litellm_settings": {"set_verbose": value}})

    def test_ui_access_mode_enum_and_types(self):
        for value in ("all", "admin_only"):
            self.assertEqual(validate_runtime_settings({"general_settings": {"ui_access_mode": value}}),
                             {"general_settings": {"ui_access_mode": value}})
        for value in ("admin", "ALL", "", SECRET, True, 1, [], {}, ["all"]):
            with self.subTest(value_type=type(value)):
                self.assert_invalid({"general_settings": {"ui_access_mode": value}})

    def test_routing_strategy_enum_and_types(self):
        expected = {
            "simple-shuffle", "least-busy", "usage-based-routing", "usage-based-routing-v2",
            "latency-based-routing", "cost-based-routing",
        }
        self.assertEqual(ROUTING_STRATEGIES, expected)
        for value in expected:
            self.assertEqual(validate_runtime_settings({"router_settings": {"routing_strategy": value}}),
                             {"router_settings": {"routing_strategy": value}})
        for value in ("lar1", "provider-budget-routing", "priority", "custom", "", SECRET,
                      True, 1, [], {}, ["simple-shuffle"]):
            with self.subTest(value_type=type(value)):
                self.assert_invalid({"router_settings": {"routing_strategy": value}})

    def test_nonnegative_integer_counts(self):
        for field in ("num_retries", "allowed_fails"):
            for value in (0, 1, 100):
                with self.subTest(field=field, valid=value):
                    self.assertEqual(validate_runtime_settings({"router_settings": {field: value}}),
                                     {"router_settings": {field: value}})
            for value in (-1, 0.0, 1.0, 1.5, True, False, "1", SECRET, [], {},
                          float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, invalid_type=type(value)):
                    self.assert_invalid({"router_settings": {field: value}})

    def test_duration_ranges_and_numeric_types(self):
        for groups, positive in ((POSITIVE_DURATIONS, True), (NONNEGATIVE_DURATIONS, False)):
            for section, fields in groups.items():
                for field in fields:
                    valid = (1, 0.5, 1e308) if positive else (0, 0.0, -0.0, 1, 0.5, 1e308)
                    for value in valid:
                        with self.subTest(section=section, field=field, valid=value):
                            self.assertEqual(validate_runtime_settings({section: {field: value}}),
                                             {section: {field: value}})
                    invalid = [-1, -0.5, True, False, "1", SECRET, [], {},
                               float("nan"), float("inf"), float("-inf"), 10**1000]
                    if positive:
                        invalid.extend([0, 0.0, -0.0])
                    for value in invalid:
                        with self.subTest(section=section, field=field, invalid_type=type(value)):
                            self.assert_invalid({section: {field: value}})

    def test_null_is_invalid_for_every_field_and_never_means_delete(self):
        for section, fields in SUPPORTED_FIELDS.items():
            for field in fields:
                with self.subTest(section=section, field=field):
                    self.assert_invalid({section: {field: None}})
                    self.assert_invalid({"schema_version": 1, section: {field: None}}, parse_patch)

    def test_nonfinite_is_invalid_for_every_field(self):
        for section, fields in SUPPORTED_FIELDS.items():
            for field in fields:
                for value in (float("nan"), float("inf"), float("-inf")):
                    with self.subTest(section=section, field=field):
                        self.assert_invalid({section: {field: value}})

    def test_root_and_section_types(self):
        for value in (None, True, False, 1, 0.5, SECRET, [], [VALID_SETTINGS]):
            with self.subTest(root_type=type(value)):
                self.assert_invalid(value)
                self.assert_invalid(value, parse_patch)
        for section in SUPPORTED_FIELDS:
            for value in (None, True, 1, SECRET, [], ["timeout"]):
                with self.subTest(section=section, section_type=type(value)):
                    self.assert_invalid({section: value})
                    self.assert_invalid({"schema_version": 1, section: value}, parse_patch)

    def test_unknown_sections_and_fields_never_disclose_keys_or_values(self):
        for section in ("model_list", "application", "runtimeSettings", "environment", SECRET, 42, None):
            self.assert_invalid({section: SECRET})
            self.assert_invalid({"schema_version": 1, section: SECRET}, parse_patch)
        for section in SUPPORTED_FIELDS:
            for field in ("unknown_setting", SECRET, 42, None):
                self.assert_invalid({section: {field: SECRET}})

    def test_protected_fields_are_rejected_in_every_section(self):
        protected = {
            "model_list", "model", "model_group_alias", "routing_groups", "default_litellm_params",
            "fallbacks", "default_fallbacks", "context_window_fallbacks", "content_policy_fallbacks",
            "routing_strategy_args", "max_parallel_requests", "default_max_parallel_requests",
            "api_key", "master_key", "database_url", "database_connection_pool_limit",
            "api_base", "base_url", "endpoint", "redis_url", "redis_host", "redis_port",
            "redis_password", "redis_db", "cache", "cache_responses", "cache_kwargs",
            "azure_ad_token", "azure_ad_token_provider", "workload_identity", "tenant_id",
            "client_id", "client_secret", "enable_azure_ad_token_refresh", "store_model_in_db",
            "disable_db_model_loading", "use_file_backed_models", "callbacks", "success_callback",
            "failure_callback", "service_callback", "input_callback", "post_call_rules",
            "pre_call_rules", "plugins", "headers", "extra_headers", "forward_client_headers_to_llm_api",
            "passthrough_endpoints", "custom_auth", "custom_key_generate", "guardrails",
            "log_raw_request_response", "turn_off_message_logging", "disable_spend_logs",
            "maximum_spend_logs_retention_period", "set_verbose_logging",
            "s3_callback_params", "s3_audit_callback_params", "debug_level",
        }
        for section in SUPPORTED_FIELDS:
            for field in protected:
                with self.subTest(section=section, field=field):
                    self.assert_invalid({section: {field: SECRET}})
                    self.assert_invalid({"schema_version": 1, section: {field: SECRET}}, parse_patch)
        self.assert_invalid({"router_settings": {"set_verbose": False}})

    def test_schema_version_requires_integer_one(self):
        for value in (None, True, False, 0, 2, 1.0, "1", SECRET, [], {}, float("nan"), float("inf")):
            with self.subTest(schema_type=type(value)):
                self.assert_invalid({"schema_version": value, **VALID_SETTINGS}, parse_patch)
        self.assert_invalid(VALID_SETTINGS, parse_patch)
        self.assert_invalid({"schema_version": 1})

    def test_patch_requires_at_least_one_setting(self):
        for document in ({}, {"schema_version": 1},
                         {"schema_version": 1, "general_settings": {}},
                         {"schema_version": 1, **{section: {} for section in SUPPORTED_FIELDS}}):
            self.assert_invalid(document, parse_patch)
        patch = {"schema_version": 1, "general_settings": {},
                 "litellm_settings": {"set_verbose": False}}
        self.assertEqual(parse_patch(patch), {
            "general_settings": {}, "litellm_settings": {"set_verbose": False},
        })

    def test_patch_returns_detached_sections_without_schema(self):
        patch = {"schema_version": 1, **copy.deepcopy(VALID_SETTINGS)}
        result = parse_patch(patch)
        self.assertEqual(result, VALID_SETTINGS)
        self.assertNotIn("schema_version", result)
        result["general_settings"]["ui_access_mode"] = "all"
        self.assertEqual(patch["general_settings"]["ui_access_mode"], "admin_only")

    def test_runtime_overrides_defaults_and_detached_validation(self):
        for config in ({}, {"application": {}}, {"application": {"runtimeSettings": {}}},
                       {"application": {"other": SECRET}, "other": SECRET}):
            self.assertEqual(runtime_overrides(config), {})
        config = {"application": {"runtimeSettings": copy.deepcopy(VALID_SETTINGS)}}
        result = runtime_overrides(config)
        self.assertEqual(result, VALID_SETTINGS)
        result["router_settings"]["timeout"] = 1
        self.assertEqual(config["application"]["runtimeSettings"], VALID_SETTINGS)

    def test_runtime_overrides_rejects_invalid_containers_and_settings(self):
        for value in (None, True, 1, SECRET, []):
            self.assert_invalid(value, runtime_overrides)
            self.assert_invalid({"application": value}, runtime_overrides)
            self.assert_invalid({"application": {"runtimeSettings": value}}, runtime_overrides)
        for settings in ({"general_settings": None}, {"router_settings": {"num_retries": True}},
                         {"litellm_settings": {"request_timeout": float("nan")}},
                         {"router_settings": {"api_key": SECRET}}, {SECRET: SECRET}):
            self.assert_invalid({"application": {"runtimeSettings": settings}}, runtime_overrides)

    def test_valid_settings_are_not_mutated_before_an_invalid_field(self):
        settings = copy.deepcopy(VALID_SETTINGS)
        settings["router_settings"]["api_key"] = SECRET
        self.assert_invalid(settings)
        self.assert_invalid({"schema_version": 1, **settings}, parse_patch)

    def test_apply_merges_leaves_preserving_models_and_protected_settings(self):
        runtime = {
            "model_list": [{"model_name": "chat", "litellm_params": {"api_key": SECRET}}],
            "general_settings": {"master_key": SECRET, "ui_access_mode": "all"},
            "litellm_settings": {"callbacks": ["existing"], "drop_params": False},
            "router_settings": {"redis_password": SECRET, "num_retries": 5, "timeout": 30},
            "other": {"nested": [SECRET]},
        }
        original = copy.deepcopy(runtime)
        overrides = {
            "general_settings": {"ui_access_mode": "admin_only"},
            "litellm_settings": {"drop_params": True},
            "router_settings": {"num_retries": 0},
        }
        result = apply_runtime_settings(runtime, overrides)
        expected = copy.deepcopy(original)
        expected["general_settings"]["ui_access_mode"] = "admin_only"
        expected["litellm_settings"]["drop_params"] = True
        expected["router_settings"]["num_retries"] = 0
        self.assertEqual(result, expected)
        self.assertEqual(runtime, original)
        self.assertEqual(overrides["router_settings"], {"num_retries": 0})
        result["model_list"][0]["litellm_params"]["api_key"] = "changed"
        result["litellm_settings"]["callbacks"].append("new")
        result["other"]["nested"].append("new")
        result["general_settings"]["ui_access_mode"] = "all"
        self.assertEqual(runtime, original)
        self.assertEqual(overrides["general_settings"]["ui_access_mode"], "admin_only")

    def test_apply_empty_overrides_returns_independent_equal_copy(self):
        runtime = {"model_list": [{"nested": [SECRET]}], "router_settings": None}
        result = apply_runtime_settings(runtime, {})
        self.assertEqual(result, runtime)
        self.assertIsNot(result, runtime)
        self.assertIsNot(result["model_list"], runtime["model_list"])
        result["model_list"][0]["nested"].append("new")
        self.assertEqual(runtime["model_list"][0]["nested"], [SECRET])

    def test_apply_creates_only_targeted_sections(self):
        runtime = {"model_list": []}
        overrides = {"router_settings": {"num_retries": 0}}
        self.assertEqual(apply_runtime_settings(runtime, overrides),
                         {"model_list": [], "router_settings": {"num_retries": 0}})
        self.assertEqual(runtime, {"model_list": []})
        self.assertEqual(apply_runtime_settings({}, {"general_settings": {}}),
                         {"general_settings": {}})
        self.assertEqual(apply_runtime_settings({}, VALID_SETTINGS), VALID_SETTINGS)

    def test_apply_rejects_malformed_target_sections_without_mutating_input(self):
        for section in SUPPORTED_FIELDS:
            field, setting = next(iter(VALID_SETTINGS[section].items()))
            for value in (None, True, 1, SECRET, [], [SECRET]):
                runtime = {"model_list": [], section: value}
                for overrides in ({section: {field: setting}}, {section: {}}):
                    with self.subTest(section=section, value_type=type(value)):
                        self.assert_invalid(
                            runtime, lambda document: apply_runtime_settings(document, overrides))

    def test_apply_does_not_inspect_untargeted_sections_or_existing_leaves(self):
        runtime = {
            "general_settings": SECRET,
            "litellm_settings": {"request_timeout": None, "set_verbose": True},
            "router_settings": {"timeout": "preexisting", "unknown": SECRET},
        }
        result = apply_runtime_settings(runtime, {"router_settings": {"num_retries": 0}})
        self.assertEqual(result, {**runtime,
                                 "router_settings": {**runtime["router_settings"], "num_retries": 0}})
        self.assertNotIn("num_retries", runtime["router_settings"])

    def test_apply_validates_runtime_and_all_overrides(self):
        for runtime in (None, True, 1, SECRET, []):
            self.assert_invalid(runtime, lambda value: apply_runtime_settings(value, {}))
        for overrides in (None, [], {SECRET: SECRET},
                          {"router_settings": {"api_key": SECRET}},
                          {"router_settings": {"num_retries": True}},
                          {"litellm_settings": {"set_verbose": True}},
                          {"general_settings": {"ui_access_mode": None}}):
            self.assert_invalid(overrides, lambda value: apply_runtime_settings({}, value))


if __name__ == "__main__":
    unittest.main()
