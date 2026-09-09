import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
from uuid import uuid4

import yaml

from scripts.audit_manifest import render_audit_documents
from scripts.customer_migration import validate_config, stage_fingerprint
from scripts.observability import COLLECTOR_DIGEST, COLLECTOR_SOURCE, collector_config, connection_fields, render_observability
from tests.test_audit_manifest import fixture


class ObservabilityTests(unittest.TestCase):
    def test_collector_configuration_and_proxy_identity_separation(self):
        config, source, foundation, storage = fixture()
        config["observability"] = {"collectorImage": config["parameters"]["platform"]["containerRegistryName"] + ".azurecr.io/collector@" + COLLECTOR_DIGEST}
        before_telemetry = dict(config)
        before_telemetry.pop("observability")
        validate_config(config, config["environment"])
        self.assertEqual(stage_fingerprint(config, 7), stage_fingerprint(before_telemetry, 7))
        audited = render_audit_documents(config, source, foundation, storage, "10.30.8.0/24")
        connection = "InstrumentationKey=55555555-5555-4555-8555-555555555555;IngestionEndpoint=https://westus-0.in.applicationinsights.azure.com/"
        receipt = {"identity": {"clientId": "88888888-8888-4888-8888-888888888888", "serviceAccountName": "otel-collector", "kubernetesNamespace": "litellm"}, "connectionString": connection}
        result = render_observability(config, audited, receipt, "10.30.8.0/24")
        collector = next(item for item in result if item["kind"] == "Deployment" and item["metadata"]["name"] == "otel-collector")
        self.assertEqual(collector["spec"]["replicas"], 2)
        self.assertFalse(collector["spec"]["template"]["spec"]["automountServiceAccountToken"])
        for item in result:
            if item["kind"] == "ConfigMap" and item["metadata"]["name"].startswith(("stage8-api-", "stage8-admin-")):
                self.assertTrue(json.loads(item["data"]["config.json"])["telemetry"]["enabled"])
        for wrong in (connection.replace("https://", "http://"), connection + ";Password=synthetic", connection.replace("applicationinsights.azure.com", "untrusted.invalid")):
            with self.assertRaises(ValueError):
                connection_fields(wrong)

    @unittest.skipUnless(os.environ.get("RUN_COLLECTOR_CONTAINER_TESTS") == "1", "Opt-in local collector binary validation")
    def test_locked_collector_accepts_actual_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.yaml"
            path.write_text(yaml.safe_dump(collector_config(), sort_keys=False))
            result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--mount", f"type=bind,src={path},dst=/etc/otel/config.yaml,readonly",
                                     "-e", "AZURE_CLIENT_ID=11111111-1111-4111-8111-111111111111", "-e", "AZURE_TENANT_ID=22222222-2222-4222-8222-222222222222", "-e", "AZURE_FEDERATED_TOKEN_FILE=/synthetic/token",
                                     "-e", "APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=55555555-5555-4555-8555-555555555555;IngestionEndpoint=https://westus-0.in.applicationinsights.azure.com/",
                                     COLLECTOR_SOURCE, "validate", "--config=/etc/otel/config.yaml"], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.environ.get("RUN_COLLECTOR_CONTAINER_TESTS") == "1", "Opt-in local collector data-flow test")
    def test_locked_collector_removes_sensitive_content_before_export(self):
        name = "llmgw-otel-test-" + uuid4().hex[:12]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            output = root / "output"
            output.mkdir(mode=0o777)
            output.chmod(0o777)
            config = collector_config()
            config["extensions"] = {"health_check": {"endpoint": "0.0.0.0:13133"}}
            config["exporters"] = {"file": {"path": "/output/traces.json"}}
            config["service"]["extensions"] = ["health_check"]
            config["service"]["pipelines"]["traces"]["exporters"] = ["file"]
            path = root / "config.yaml"
            path.write_text(yaml.safe_dump(config, sort_keys=False))
            subprocess.run(["docker", "network", "create", "--internal", name], check=True, capture_output=True)
            try:
                subprocess.run(["docker", "run", "--detach", "--name", name, "--network", name, "--read-only", "--mount", f"type=bind,src={path},dst=/etc/otel/config.yaml,readonly", "--mount", f"type=bind,src={output},dst=/output", COLLECTOR_SOURCE, "--config=/etc/otel/config.yaml"], check=True, capture_output=True)
                address = subprocess.run(["docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name], check=True, capture_output=True, text=True).stdout.strip()
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urlopen(f"http://{address}:13133/", timeout=1) as response:
                            self.assertEqual(response.status, 200)
                        break
                    except (URLError, TimeoutError):
                        self.assertLess(time.monotonic(), deadline, "Collector did not become ready")
                        time.sleep(0.1)
                span = {"traceId": "a" * 32, "spanId": "b" * 16, "name": "SYNTHETIC_PRIVATE_NAME", "traceState": "private=SYNTHETIC_PRIVATE_STATE", "startTimeUnixNano": "1788912000000000000", "endTimeUnixNano": "1788912000010000000", "kind": 2,
                        "attributes": [{"key": "gateway.plane", "value": {"stringValue": "api"}}, {"key": "request.body", "value": {"stringValue": "SYNTHETIC_PRIVATE_BODY"}}],
                        "events": [{"timeUnixNano": "1788912000000000000", "name": "SYNTHETIC_PRIVATE_EVENT"}],
                        "links": [{"traceId": "c" * 32, "spanId": "d" * 16, "attributes": [{"key": "private", "value": {"stringValue": "SYNTHETIC_PRIVATE_LINK"}}]}],
                        "status": {"code": 2, "message": "SYNTHETIC_PRIVATE_ERROR"}}
                payload = {"resourceSpans": [{"schemaUrl": "SYNTHETIC_PRIVATE_RESOURCE_SCHEMA", "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "llm-api-proxy"}}, {"key": "private", "value": {"stringValue": "SYNTHETIC_PRIVATE_RESOURCE"}}]}, "scopeSpans": [{"schemaUrl": "SYNTHETIC_PRIVATE_SCOPE_SCHEMA", "scope": {"name": "SYNTHETIC_PRIVATE_SCOPE_NAME", "version": "SYNTHETIC_PRIVATE_VERSION", "attributes": [{"key": "private", "value": {"stringValue": "SYNTHETIC_PRIVATE_SCOPE_ATTRIBUTE"}}]}, "spans": [span]}]}]}
                with urlopen(Request(f"http://{address}:4318/v1/traces", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}), timeout=5) as response:
                    self.assertEqual(response.status, 200)
                subprocess.run(["docker", "stop", "--time", "10", name], check=True, capture_output=True)
                exported = (output / "traces.json").read_text()
                self.assertNotIn("SYNTHETIC_PRIVATE", exported)
                self.assertIn("gateway.request", exported)
                self.assertIn("gateway.plane", exported)
                trace = json.loads(exported)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
                self.assertFalse(trace.get("events"))
                self.assertFalse(trace.get("links"))
            finally:
                subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)
                subprocess.run(["docker", "network", "rm", name], check=False, capture_output=True)