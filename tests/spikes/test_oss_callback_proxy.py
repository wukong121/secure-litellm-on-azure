"""Exercise the real LiteLLM proxy with synthetic loopback upstreams only."""

from contextlib import contextmanager
import importlib.metadata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

import yaml

from oss_callback_probe import INPUT_MARKER, OUTPUT_MARKER, TOOL_MARKER

TRACE = "00-11111111111111111111111111111111-2222222222222222-01"


class Upstream(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        pass

    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_events(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            text = event if isinstance(event, str) else json.dumps(event)
            self.wfile.write(f"data: {text}\n\n".encode())
            self.wfile.flush()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append(self.path)
        if "SYNTHETIC_FAIL" in json.dumps(body):
            return self.send_json(500, {"error": {"message": "Synthetic upstream failure", "type": "server_error", "code": "synthetic"}})
        if self.path.endswith("/embeddings"):
            return self.send_json(200, {"object": "list", "model": "text-embedding-3-small", "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}], "usage": {"prompt_tokens": 5, "total_tokens": 5}})
        if self.path.endswith("/chat/completions"):
            base = {"id": "chatcmpl-synthetic", "created": 1, "model": "gpt-4o"}
            usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
            if body.get("tools"):
                return self.send_json(200, {**base, "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_synthetic", "type": "function", "function": {"name": TOOL_MARKER, "arguments": '{"query":"synthetic"}'}}]}, "finish_reason": "tool_calls"}], "usage": usage})
            if body.get("stream"):
                if "SYNTHETIC_CUT" in json.dumps(body):
                    return self.send_events([{**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": OUTPUT_MARKER}, "finish_reason": None}]}])
                return self.send_events([
                    {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": OUTPUT_MARKER}, "finish_reason": None}]},
                    {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
                    "[DONE]",
                ])
            return self.send_json(200, {**base, "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": OUTPUT_MARKER}, "finish_reason": "stop"}], "usage": usage})
        if self.path.endswith("/responses"):
            part = {"type": "output_text", "text": OUTPUT_MARKER, "annotations": []}
            message = {"type": "message", "id": "msg_synthetic", "role": "assistant", "status": "completed", "content": [part]}
            response = {
                "id": "resp_synthetic", "object": "response", "created_at": 1,
                "status": "completed", "model": "gpt-4o", "output": [message],
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
                "temperature": 1.0, "top_p": 1.0,
                "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
            }
            if body.get("stream"):
                return self.send_events([
                    {"type": "response.created", "sequence_number": 0, "response": {**response, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
                    {"type": "response.content_part.added", "sequence_number": 2, "output_index": 0, "item_id": message["id"], "content_index": 0, "part": {**part, "text": ""}},
                    {"type": "response.output_text.delta", "sequence_number": 3, "output_index": 0, "item_id": message["id"], "content_index": 0, "delta": OUTPUT_MARKER},
                    {"type": "response.output_text.done", "sequence_number": 4, "output_index": 0, "item_id": message["id"], "content_index": 0, "text": OUTPUT_MARKER},
                    {"type": "response.content_part.done", "sequence_number": 5, "output_index": 0, "item_id": message["id"], "content_index": 0, "part": part},
                    {"type": "response.output_item.done", "sequence_number": 6, "output_index": 0, "item": message},
                    {"type": "response.completed", "sequence_number": 7, "response": response},
                ])
            if body.get("tools"):
                response["output"] = [{"type": "function_call", "id": "fc_synthetic", "call_id": "call_synthetic", "name": TOOL_MARKER, "arguments": '{"query":"synthetic"}', "status": "completed"}]
            return self.send_json(200, response)
        self.send_json(404, {"error": "Synthetic route missing"})


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def request(port, path, body=None, case="", extra_headers=None):
    headers = {"Authorization": "Bearer synthetic-proxy-master", "Content-Type": "application/json", "x-spike-case": case, "traceparent": TRACE}
    headers.update(extra_headers or {})
    call = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(call, timeout=15) as response:
            return response.status, response.read(), response.headers.get("x-litellm-call-id")
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("x-litellm-call-id")


@contextmanager
def proxy_runtime():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        backend = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        worker = threading.Thread(target=backend.serve_forever, daemon=True)
        worker.start()
        port = free_port()
        config = {
            "model_list": [{"model_name": "synthetic", "litellm_params": {"model": "openai/gpt-4o", "api_key": "synthetic-upstream-key", "api_base": f"http://127.0.0.1:{backend.server_port}/v1"}, "model_info": {"id": "synthetic-deployment-a"}}],
            "litellm_settings": {"callbacks": ["oss_callback_probe.probe"], "num_retries": 0, "global_disable_no_log_param": True},
            "router_settings": {"num_retries": 0},
            "general_settings": {"master_key": "synthetic-proxy-master"},
        }
        path = root / "config.yaml"
        path.write_text(yaml.safe_dump(config))
        events = root / "events.jsonl"
        env = {**os.environ, "PYTHONPATH": "/repo:/spike", "OSS_CALLBACK_PROBE_FILE": str(events), "LITELLM_LOG": "ERROR", "LITELLM_TELEMETRY": "False"}
        for name in ("LITELLM_LICENSE", "LITELLM_LICENSE_PATH", "DATABASE_URL"):
            env.pop(name, None)
        with (root / "proxy.log").open("w") as logs:
            process = subprocess.Popen(["litellm", "--config", str(path), "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=logs, stderr=logs)
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Synthetic LiteLLM proxy failed startup; no raw logs emitted")
                    try:
                        if request(port, "/health/liveliness")[0] == 200:
                            break
                    except (OSError, TimeoutError):
                        pass
                    threading.Event().wait(0.1)
                else:
                    raise TimeoutError("Synthetic proxy startup timed out")
                yield port, events
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                backend.shutdown()
                backend.server_close()
                worker.join(timeout=5)


def main():
    assert importlib.metadata.version("litellm") == "1.98.0"
    results = []
    with proxy_runtime() as (port, events):
        cases = [
            ("chat", "/v1/chat/completions", {"messages": [{"role": "user", "content": INPUT_MARKER}]}),
            ("chat-stream", "/v1/chat/completions", {"messages": [{"role": "user", "content": INPUT_MARKER}], "stream": True}),
            ("responses", "/v1/responses", {"input": INPUT_MARKER}),
            ("responses-stream", "/v1/responses", {"input": INPUT_MARKER, "stream": True}),
            ("embeddings", "/v1/embeddings", {"input": INPUT_MARKER}),
            ("chat-failure", "/v1/chat/completions", {"messages": [{"role": "user", "content": f"{INPUT_MARKER} SYNTHETIC_FAIL"}]}),
            ("responses-failure", "/v1/responses", {"input": f"{INPUT_MARKER} SYNTHETIC_FAIL"}),
            ("chat-tools", "/v1/chat/completions", {"messages": [{"role": "user", "content": INPUT_MARKER}], "tools": [{"type": "function", "function": {"name": TOOL_MARKER, "parameters": {"type": "object", "properties": {}}}}]}),
            ("responses-tools", "/v1/responses", {"input": INPUT_MARKER, "tools": [{"type": "function", "name": TOOL_MARKER, "parameters": {"type": "object", "properties": {}}}]}),
            ("no-log-request", "/v1/chat/completions", {"messages": [{"role": "user", "content": INPUT_MARKER}], "no-log": True}),
            ("callback-disable-header", "/v1/chat/completions", {"messages": [{"role": "user", "content": INPUT_MARKER}]}),
            ("chat-cut-stream", "/v1/chat/completions", {"messages": [{"role": "user", "content": f"{INPUT_MARKER} SYNTHETIC_CUT"}], "stream": True}),
            ("sink-failure", "/v1/chat/completions", {"messages": [{"role": "user", "content": f"{INPUT_MARKER} SYNTHETIC_SINK_FAIL"}]}),
        ]
        for name, path, body in cases:
            before = len(Upstream.calls)
            case = f"synthetic-{name}"
            status, response, call_id = request(port, path, {"model": "synthetic", **body}, case, {"x-litellm-disable-callbacks": "oss_callback_probe.probe"} if name == "callback-disable-header" else None)
            deadline = time.monotonic() + 5
            captured = []
            while time.monotonic() < deadline:
                if events.exists():
                    captured = [json.loads(line) for line in events.read_text().splitlines() if line.endswith("}")]
                    captured = [event for event in captured if event["case"] == case]
                    if any(event["event"] in ("success", "failure", "delivery_failure") for event in captured):
                        break
                threading.Event().wait(0.05)
            results.append({"scenario": name, "status": status, "client_output_present": OUTPUT_MARKER.encode() in response, "client_tool_present": TOOL_MARKER.encode() in response, "client_call_id_present": bool(call_id), "upstream_attempts": len(Upstream.calls) - before, "events": captured})
    print(json.dumps({"version": "1.98.0", "proxy_protocol_evidence": results}, indent=2))
    assert all(result["events"] for result in results), "A protocol lacked a correlated callback"
    for result in results:
        failure = result["scenario"].endswith("-failure")
        if result["scenario"] == "sink-failure":
            assert result["status"] == 200 and result["client_output_present"], "Callback exceptions must not be mistaken for admission control"
            assert any(event["event"] == "delivery_failure" for event in result["events"])
            continue
        assert result["status"] == (500 if failure else 200), result["scenario"]
        terminal = [event for event in result["events"] if event["event"] == ("failure" if failure else "success")]
        assert terminal, result["scenario"]
        assert any(event["input_present"] and event["call_id_present"] and event["model_id_present"] and event["traceparent_present"] for event in terminal), result["scenario"]
        assert any(event["adapter_input_present"] and event["adapter_transport_completeness"] == "not-verified" for event in terminal), result["scenario"]
        if result["scenario"] == "embeddings":
            assert any(event["embedding_present"] for event in terminal)
        elif result["scenario"].endswith("-tools"):
            assert result["client_tool_present"] and any(event["tool_present"] for event in terminal)
        elif not failure:
            assert result["client_output_present"] and any(event["output_present"] for event in terminal)
            assert any(event["adapter_output_present"] for event in terminal), result["scenario"]


if __name__ == "__main__":
    main()