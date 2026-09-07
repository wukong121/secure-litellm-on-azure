import copy
from datetime import datetime, timezone
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import yaml

from scripts.stage9_release import ROOT, PRODUCTION_CHECKS, generate, validate_origin_snapshot, validate_release, validate_what_if
from scripts.preview_stage9 import preview


def evidence_config(phase="canary"):
    return {
        "phase": phase, "baseDomain": "customer.test.invalid", "environmentName": "test",
        "privateOrigin": {"privateLinkServiceId": "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/synthetic/providers/Microsoft.Network/privateLinkServices/api", "privateLinkLocation": "westus"},
        "logAnalyticsWorkspaceName": "synthetic-logs", "frontDoorId": "22222222-2222-4222-8222-222222222222",
        "wafMode": "Prevention" if phase == "production" else "Detection", "rateLimitPerMinute": 600,
        "changeTicket": "SYNTHETIC-ONLY", "approvedBy": ["33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"],
        "checks": {name: {"passed": True, "observedAt": datetime.now(timezone.utc).isoformat(), "report": "synthetic://offline-test"} for name in PRODUCTION_CHECKS},
    }


class Stage9ReleaseTests(unittest.TestCase):
    def test_template_cannot_authorize_a_release(self):
        config = json.loads((ROOT / "infra/edge/release.example.json").read_text())
        with self.assertRaises(ValueError):
            validate_release(config)

    def test_canary_requires_stage8_and_protocol_evidence(self):
        config = evidence_config()
        validate_release(config)
        for key in ("stage8_capture_architecture", "l3_governance_and_recovery", "required_protocol_matrix"):
            invalid = copy.deepcopy(config)
            del invalid["checks"][key]
            with self.assertRaisesRegex(ValueError, key):
                validate_release(invalid)

    def test_production_requires_prevention_and_recent_distinct_approvals(self):
        config = evidence_config("production")
        validate_release(config)
        for patch in ({"wafMode": "Detection"}, {"approvedBy": [config["approvedBy"][0]]}, {"frontDoorId": "REPLACE_ID"}):
            with self.assertRaises(ValueError):
                validate_release({**config, **patch})
        config["checks"]["canary_slo"]["observedAt"] = "2000-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            validate_release(config)

    def test_what_if_does_not_silently_accept_changes_or_unsupported_analysis(self):
        self.assertEqual(validate_what_if({"status": "Succeeded", "changes": [{"changeType": "Create"}]}), {"Create": 1})
        for kind in ("Modify", "Delete", "Unsupported", "Deploy", None):
            with self.assertRaises(ValueError):
                validate_what_if({"status": "Succeeded", "changes": [{"changeType": kind}]})
        with self.assertRaises(ValueError):
            validate_what_if({"status": "Failed", "changes": []})

    def test_private_origin_rejects_public_frontend_and_wrong_subnet_policy(self):
        frontend_id = "/synthetic/frontend"
        load_balancer = {"sku": {"name": "Standard"}, "properties": {"frontendIPConfigurations": [{"id": frontend_id, "properties": {"privateIPAddress": "10.30.4.10", "subnet": {"id": "/synthetic/subnet"}}}]}}
        subnet = {"properties": {"privateLinkServiceNetworkPolicies": "Disabled"}}
        validate_origin_snapshot(load_balancer, frontend_id, subnet)
        load_balancer["properties"]["frontendIPConfigurations"][0]["properties"]["publicIPAddress"] = {"id": "/synthetic/public-ip"}
        with self.assertRaises(ValueError):
            validate_origin_snapshot(load_balancer, frontend_id, subnet)

    def test_render_has_no_dns_or_deployment_side_effect_and_prepare_disables_traffic(self):
        (ROOT / "temp").mkdir(exist_ok=True)
        for phase in ("prepare", "canary", "production"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
                generate(evidence_config(phase), Path(directory))
                parameters = json.loads((Path(directory) / "edge.parameters.json").read_text())["parameters"]
                self.assertEqual(parameters["enableApiTraffic"]["value"], phase != "prepare")
                self.assertEqual(parameters["baseDomain"]["value"], "customer.test.invalid")
                self.assertFalse(json.loads((Path(directory) / "release-summary.json").read_text())["deploymentPerformed"])

    def test_preview_uses_only_what_if_and_keeps_payloads_ignored(self):
        (ROOT / "temp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.preview_stage9.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"status":"Succeeded","changes":[]}'
            run.return_value.stderr = ''
            result = preview("synthetic-rg", "edge", Path(directory))
            command = run.call_args.args[0]
            self.assertEqual(command[:4], ["az", "deployment", "group", "what-if"])
            self.assertNotIn("create", command)
            self.assertFalse(result["deploymentPerformed"])
            self.assertEqual((Path(directory) / "edge.what-if.json").stat().st_mode & 0o777, 0o600)

    def test_approved_preview_binds_edge_domain_and_id_to_only_api_proxy(self):
        config = evidence_config()
        (ROOT / "temp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            generate(config, Path(directory))
            result = subprocess.run(["kubectl", "kustomize", directory], capture_output=True, text=True, check=True)
            documents = list(yaml.safe_load_all(result.stdout))
            for plane in ("api", "admin"):
                deployment = next(item for item in documents if item["kind"] == "Deployment" and item["metadata"]["name"] == f"llm-{plane}-proxy")
                env = {item["name"]: item.get("value") for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(env.get("FRONT_DOOR_ID"), config["frontDoorId"] if plane == "api" else None)
                ingress = next(item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == f"llm-{plane}")
                self.assertEqual(ingress["spec"]["rules"][0]["host"], f"llm-{plane}.{config['baseDomain']}")