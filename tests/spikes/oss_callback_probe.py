"""Synthetic-only callback probe, never a production content logger."""

import asyncio
import json
import os
from pathlib import Path

from LiteLLM.observability.oss_audit_callback import OSSAuditCallback

INPUT_MARKER = "SYNTHETIC_CALLBACK_INPUT"
OUTPUT_MARKER = "SYNTHETIC_CALLBACK_OUTPUT"
TOOL_MARKER = "synthetic_lookup"


def normalized(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): normalized(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalized(child) for child in value]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return type(value).__name__


class ProbeLogger(OSSAuditCallback):
    def __init__(self):
        super().__init__(select_request=lambda _kwargs: True, emit=self.capture_envelope, signal=self.capture_failure)
        self.events = []
        self.envelopes = {}
        self.terminal = asyncio.Event()
        self.output_path = os.environ.get("OSS_CALLBACK_PROBE_FILE")

    async def capture_envelope(self, envelope):
        if "SYNTHETIC_SINK_FAIL" in json.dumps(envelope):
            raise OSError("Synthetic-only delivery failure")
        self.envelopes[envelope["litellmCallId"]] = envelope

    def capture_failure(self, _event):
        if self.output_path:
            with Path(self.output_path).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": "delivery_failure", "case": "synthetic-sink-failure"}) + "\n")

    def record(self, event, kwargs, response):
        payload = normalized(kwargs.get("standard_logging_object") or {})
        response = normalized(response)
        combined = json.dumps({"kwargs": normalized(kwargs), "response": response})
        params = kwargs.get("litellm_params") or {}
        metadata = params.get("metadata") or {}
        proxy_request = params.get("proxy_server_request") or {}
        headers = proxy_request.get("headers") or {}
        case = headers.get("x-spike-case", "")
        if not isinstance(case, str) or not case.startswith("synthetic-"):
            case = ""
        envelope = self.envelopes.get(kwargs.get("litellm_call_id"), {})
        evidence = {
            "event": event,
            "case": case,
            "call_type": str(kwargs.get("call_type", "")),
            "input_present": INPUT_MARKER in combined,
            "output_present": OUTPUT_MARKER in combined,
            "standard_payload_present": bool(payload),
            "standard_input_present": INPUT_MARKER in json.dumps(payload.get("messages")),
            "standard_output_present": OUTPUT_MARKER in json.dumps(payload.get("response")),
            "assembled_output_present": OUTPUT_MARKER in json.dumps(normalized(kwargs.get("complete_streaming_response"))),
            "usage_present": isinstance(response, dict) and bool(response.get("usage")),
            "call_id_present": bool(kwargs.get("litellm_call_id")),
            "model_id_present": bool((metadata.get("model_info") or {}).get("id") or (params.get("model_info") or {}).get("id")),
            "traceparent_present": headers.get("traceparent") == "00-11111111111111111111111111111111-2222222222222222-01",
            "exception_present": kwargs.get("exception") is not None,
            "response_type": type(response).__name__,
            "embedding_present": isinstance(response, dict) and bool(response.get("data")),
            "tool_present": TOOL_MARKER in json.dumps(response),
            "adapter_input_present": INPUT_MARKER in json.dumps(envelope.get("content")),
            "adapter_output_present": OUTPUT_MARKER in json.dumps(envelope.get("content", {}).get("modelResponse")),
            "adapter_transport_completeness": envelope.get("transportCompleteness"),
        }
        self.events.append(evidence)
        if self.output_path:
            with Path(self.output_path).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(evidence) + "\n")
        if event in ("success", "failure"):
            self.terminal.set()

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        await self.publish("success", kwargs, response_obj)
        self.record("success", kwargs, response_obj)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        await self.publish("failure", kwargs, response_obj)
        self.record("failure", kwargs, response_obj)

    async def async_log_stream_event(self, kwargs, response_obj, start_time, end_time):
        self.record("stream", kwargs, response_obj)


probe = ProbeLogger()