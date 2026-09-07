import json
import unittest

from LiteLLM.observability.audit_envelope import build_envelope


class AuditEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.kwargs = {
            "call_type": "aresponses", "litellm_call_id": "synthetic-call", "response_cost": 0.001,
            "standard_logging_object": {"messages": [{"role": "user", "content": "synthetic model input"}]},
            "litellm_params": {"model_info": {"id": "synthetic-deployment"}, "api_key": "PRIVATE_INTERNAL_KEY", "metadata": {"user_api_key": "PRIVATE_HASH"}, "proxy_server_request": {
                "headers": {"authorization": "PRIVATE_TOKEN", "cookie": "PRIVATE_COOKIE", "traceparent": "00-11111111111111111111111111111111-2222222222222222-01"},
                "body": {"model": "synthetic", "input": "synthetic source", "stream": True, "metadata": {"tenant": "forged"}, "api_key": "PRIVATE_OVERRIDE"},
            }},
        }
        self.response = {"output": [{"type": "function_call", "name": "synthetic_tool", "arguments": "{}"}], "usage": {"input_tokens": 5}, "status": "completed", "_hidden_params": {"key": "PRIVATE_HIDDEN"}}

    def test_explicit_envelope_keeps_content_but_never_copies_headers_or_provider_credentials(self):
        result = build_envelope("success", self.kwargs, self.response)
        self.assertEqual(result["traceId"], "1" * 32)
        self.assertEqual(result["deploymentId"], "synthetic-deployment")
        self.assertEqual(result["content"]["proxyRequest"]["input"], "synthetic source")
        self.assertEqual(result["content"]["modelResponse"]["output"][0]["name"], "synthetic_tool")
        self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertNotIn("forged", json.dumps(result))
        self.assertEqual(result["transportCompleteness"], "not-verified")

    def test_failure_does_not_export_exception_text(self):
        self.kwargs["exception"] = RuntimeError("PRIVATE_EXCEPTION_URL_AND_KEY")
        result = build_envelope("failure", self.kwargs, None)
        self.assertEqual(result["errorType"], "RuntimeError")
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_missing_optional_fields_and_invalid_trace_are_safe(self):
        result = build_envelope("success", {"litellm_params": None}, None)
        self.assertIsNone(result["deploymentId"])
        self.assertIsNone(result["traceId"])
        self.kwargs["litellm_params"]["proxy_server_request"]["headers"]["traceparent"] = "00-" + "0" * 32 + "-" + "0" * 16 + "-01"
        self.assertIsNone(build_envelope("success", self.kwargs, self.response)["traceId"])

    def test_content_budget_omits_oversized_payload_with_explicit_marker(self):
        result = build_envelope("success", self.kwargs, self.response, max_content_bytes=1)
        self.assertTrue(result["contentTruncated"])
        self.assertIsNone(result["content"])
        self.assertGreater(result["contentBytes"], 1)

    def test_content_is_detached_and_wrong_event_is_rejected(self):
        result = build_envelope("success", self.kwargs, self.response)
        self.response["output"].clear()
        self.assertEqual(len(result["content"]["modelResponse"]["output"]), 1)
        with self.assertRaises(ValueError):
            build_envelope("stream", self.kwargs, self.response)