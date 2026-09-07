"""Explicit content envelope for an approved OSS callback audit sink."""

from copy import deepcopy
import json
import math
import re

INPUT_FIELDS = {"model", "messages", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls", "stream"}
OUTPUT_FIELDS = {"id", "object", "model", "choices", "output", "data", "usage", "status"}
TRACEPARENT = re.compile(r"^00-([a-f0-9]{32})-([a-f0-9]{16})-[a-f0-9]{2}$")


def json_object(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return value if isinstance(value, dict) else {}


def build_envelope(event, kwargs, response_obj, max_content_bytes=2 * 1024 * 1024):
    if event not in ("success", "failure"):
        raise ValueError("Only terminal callback events belong in the audit envelope")
    if type(max_content_bytes) is not int or not 1 <= max_content_bytes <= 4 * 1024 * 1024:
        raise ValueError("Invalid callback content budget")
    params = json_object(kwargs.get("litellm_params"))
    request = json_object(params.get("proxy_server_request"))
    body = json_object(request.get("body"))
    payload = json_object(kwargs.get("standard_logging_object"))
    response = json_object(response_obj)
    model_info = json_object(params.get("model_info")) or json_object(json_object(params.get("metadata")).get("model_info"))
    trace = json_object(request.get("headers")).get("traceparent")
    match = TRACEPARENT.fullmatch(trace) if isinstance(trace, str) else None
    trace_id = match[1] if match and int(match[1], 16) and int(match[2], 16) else None
    cost = kwargs.get("response_cost")
    if type(cost) not in (int, float) or not math.isfinite(cost):
        cost = None
    content = {
        "proxyRequest": {key: body[key] for key in INPUT_FIELDS if key in body},
        "modelInput": payload.get("messages", kwargs.get("messages", kwargs.get("input"))),
        "modelResponse": {key: response[key] for key in OUTPUT_FIELDS if key in response},
    }
    content_size = len(json.dumps(content, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    return {
        "schemaVersion": 1,
        "source": "litellm-oss-custom-logger",
        "event": event,
        "callType": kwargs.get("call_type"),
        "litellmCallId": kwargs.get("litellm_call_id"),
        "deploymentId": model_info.get("id"),
        "traceId": trace_id,
        "identityBinding": "resolve-at-trusted-receiver",
        "stream": body.get("stream") is True or kwargs.get("stream") is True,
        "transportCompleteness": "not-verified",
        "contentTruncated": content_size > max_content_bytes,
        "contentBytes": content_size,
        "usage": deepcopy(response.get("usage")),
        "cost": cost,
        "errorType": type(kwargs["exception"]).__name__ if kwargs.get("exception") is not None else None,
        "content": deepcopy(content) if content_size <= max_content_bytes else None,
    }