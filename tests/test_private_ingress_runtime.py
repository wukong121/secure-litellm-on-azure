import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.customer_migration import ROOT, stage_fingerprint
from scripts.migration_deploy import group_id, resolve_origin
from scripts.migration_runtime import validate_action
from scripts.private_ingress_runtime import deploy_private_ingress, read_certificate
from tests.test_customer_migration import customer_config


def ingress_config():
    config = customer_config()
    config["privateIngress"] = {plane: {"tlsSecretId": f"https://synthetic.vault.azure.net/secrets/{plane}-tls", "allowedCidrs": ["10.30.0.0/16"]} for plane in ("api", "admin")}
    config["parameters"]["platform"]["stage4Network"].update(ingressSubnetName="snet-ingress", ingressSubnetPrefix="10.30.4.0/24")
    return config


class PrivateIngressRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = ingress_config()
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / "temp")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("scripts.private_ingress_runtime.connect_cluster", return_value=["kubectl", "--namespace", "litellm"]))
        self.azure = Mock()
        self.azure.scoped.side_effect = self.cloud
        self.stack.enter_context(patch("scripts.private_ingress_runtime.AzureCommands", return_value=self.azure))
        self.command = self.stack.enter_context(patch("scripts.private_ingress_runtime.run_command", side_effect=self.commands))
        self.cert = self.stack.enter_context(patch("scripts.private_ingress_runtime.read_certificate", side_effect=self.certificate))
        self.promote = self.stack.enter_context(patch("scripts.private_ingress_runtime.promote_image"))
        self.verify = self.stack.enter_context(patch("scripts.private_ingress_runtime.verify_endpoint"))

    def certificate(self, config, secret_id, host):
        return {"certificate": "synthetic-public-chain", "key": "synthetic-private-material", "sha256": ("a" if "api" in host else "b") * 64, "expiresAt": "2099-01-01T00:00:00Z", "secretId": secret_id + "/" + "c" * 32}

    def commands(self, arguments, directory, label, **kwargs):
        if label.startswith("before-"):
            return ""
        if label.startswith("service-"):
            return json.dumps({"status": {"loadBalancer": {"ingress": [{"ip": "10.30.4.10" if label.endswith("api") else "10.30.4.11"}]}}})
        return "verified"

    def cloud(self, arguments):
        if arguments[:2] == ["aks", "show"]:
            return {"id": group_id(self.config) + "/providers/Microsoft.ContainerService/managedClusters/new-aks", "nodeResourceGroup": "rg-nodes", "private": True}
        if arguments[:4] == ["network", "vnet", "subnet", "show"]:
            return {"prefix": "10.30.4.0/24", "plsPolicy": "Disabled"}
        if arguments[:3] == ["network", "lb", "list"]:
            subnet = group_id(self.config) + "/providers/Microsoft.Network/virtualNetworks/target-vnet/subnets/snet-ingress"
            return [{"name": "kubernetes-internal", "sku": {"name": "Standard"}, "frontendIPConfigurations": [{"name": plane, "privateIPAddress": address, "subnet": {"id": subnet}} for plane, address in (("api", "10.30.4.10"), ("admin", "10.30.4.11"))]}]
        return {"properties": {"provisioningState": "Succeeded"}}

    def plan(self):
        return deploy_private_ingress(self.config, "plan", "a" * 40, self.path, "")

    def test_plan_contains_no_private_key_and_never_promotes_or_applies(self):
        result = self.plan()
        self.assertFalse(result["stageAccepted"])
        review = (self.path / "runtime-review.json").read_text()
        self.assertNotIn("synthetic-private-material", review)
        self.assertNotIn("synthetic-public-chain", review)
        self.promote.assert_not_called()
        self.verify.assert_not_called()
        for call in self.command.call_args_list:
            if "apply" in call.args[0]:
                self.assertIn("--dry-run=server", call.args[0])
        self.assertFalse(any("create" in call.args[0] for call in self.azure.scoped.call_args_list))

    def test_unapproved_or_changed_certificate_never_deploys(self):
        result = self.plan()
        with self.assertRaisesRegex(ValueError, "plan changed"):
            deploy_private_ingress(self.config, "execute", "a" * 40, self.path, "f" * 64)
        original = self.certificate(self.config, self.config["privateIngress"]["api"]["tlsSecretId"], "llm-api.customer.invalid")
        self.cert.side_effect = None
        self.cert.return_value = {**original, "sha256": "d" * 64}
        with self.assertRaisesRegex(ValueError, "plan changed"):
            deploy_private_ingress(self.config, "execute", "a" * 40, self.path, result["planSha256"])
        self.promote.assert_not_called()

    def test_execute_verifies_both_planes_then_records_outputs(self):
        result = self.plan()
        outcome = deploy_private_ingress(self.config, "execute", "a" * 40, self.path, result["planSha256"])
        self.assertTrue(outcome["verified"])
        self.assertFalse(outcome["stageAccepted"])
        self.assertEqual(self.verify.call_count, 2)
        self.promote.assert_called_once()
        receipt = json.loads((self.path / "ingress-receipt-template.json").read_text())["outputs"]["privateIngress"]["value"]
        self.assertEqual(receipt["api"]["frontendName"], "api")
        self.assertEqual(receipt["admin"]["frontendName"], "admin")
        self.assertNotIn("synthetic-private-material", json.dumps(receipt))
        self.assertFalse((self.path / "ingress-tls.json").exists())

    def test_failed_tls_verification_never_records_success(self):
        result = self.plan()
        self.verify.side_effect = ValueError("TLS mismatch")
        with self.assertRaisesRegex(ValueError, "TLS mismatch"):
            deploy_private_ingress(self.config, "execute", "a" * 40, self.path, result["planSha256"])
        self.assertFalse((self.path / "ingress-receipt-template.json").exists())
        self.assertFalse(any("create" in call.args[0] for call in self.azure.scoped.call_args_list))

    def test_stage9_resolves_only_the_verified_api_frontend(self):
        self.config["parameters"]["origin"] = {"virtualNetworkName": "target-vnet", "ingressSubnetName": "snet-ingress", "apiLoadBalancer": {"resourceGroupName": "auto", "name": "auto", "frontendName": "auto"}}
        ingress = {"configSha256": stage_fingerprint(self.config, 4), "api": {"resourceGroupName": "rg-nodes", "name": "kubernetes-internal", "frontendName": "api", "privateIpAddress": "10.30.4.10"}, "admin": {"privateIpAddress": "10.30.4.11"}}

        def resolve(arguments):
            if arguments[:2] == ["aks", "show"]:
                return "rg-nodes"
            if arguments[:3] == ["deployment", "group", "show"]:
                return {"state": "Succeeded", "ingress": ingress}
            return self.cloud(arguments)

        self.azure.scoped.side_effect = resolve
        resolved = resolve_origin(self.config, "origin", self.azure)
        self.assertEqual(resolved["parameters"]["origin"]["apiLoadBalancer"]["frontendName"], "api")
        self.assertEqual(self.config["parameters"]["origin"]["apiLoadBalancer"]["name"], "auto")
        ingress["api"]["privateIpAddress"] = "10.30.4.12"
        with self.assertRaisesRegex(ValueError, "exactly one"):
            resolve_origin(self.config, "origin", self.azure)
        ingress["configSha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "Stage 4 configuration"):
            resolve_origin(self.config, "origin", self.azure)

    def test_private_ingress_only_runs_at_stage4_in_both_modes(self):
        validate_action(self.config, 4, "private-ingress")
        self.config["deploymentMode"] = "greenfield"
        self.config.pop("legacy")
        validate_action(self.config, 4, "private-ingress")
        with self.assertRaises(ValueError):
            validate_action(self.config, 3, "private-ingress")


class CertificateReadTests(unittest.TestCase):
    def test_read_binds_version_and_suppresses_secret_parser_errors(self):
        config = ingress_config()
        secret_id = config["privateIngress"]["api"]["tlsSecretId"]
        secret = {"id": secret_id + "/" + "a" * 32, "attributes": {"enabled": True}, "value": "synthetic-sensitive-value"}
        with patch("scripts.private_ingress_runtime.subprocess.run") as command, patch("scripts.private_ingress_runtime.certificate_material") as parse:
            command.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(secret))
            parse.return_value = {"sha256": "b" * 64}
            result = read_certificate(config, secret_id, "llm-api.customer.invalid")
            self.assertEqual(result["secretId"], secret["id"])
            self.assertNotIn(secret["value"], str(command.call_args))
            with self.assertRaisesRegex(ValueError, "version differs"):
                read_certificate(config, secret_id + "/" + "c" * 32, "llm-api.customer.invalid")
            parse.side_effect = ValueError(secret["value"])
            with self.assertRaisesRegex(ValueError, "TLS PEM failed") as caught:
                read_certificate(config, secret_id, "llm-api.customer.invalid")
            self.assertNotIn(secret["value"], str(caught.exception))