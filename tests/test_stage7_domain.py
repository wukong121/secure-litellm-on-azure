import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.render_stage7_domain import ROOT, domain_artifacts, domain_hosts, render


class Stage7DomainTests(unittest.TestCase):
    def test_hosts_and_callback_share_one_parameter(self):
        policy = {"apiHost": "llm-api.example.com", "adminHost": "llm-admin.example.com", "bindings": []}
        admin = {"web": {"redirectUris": ["https://llm-admin.example.com/auth/callback"]}, "appRoles": []}
        originals = copy.deepcopy((policy, admin))
        for domain in ("demo.test.invalid", "customer.other.invalid"):
            with self.subTest(domain=domain):
                result_policy, result_admin, patches = domain_artifacts(domain, policy, admin)
                self.assertEqual(result_policy["apiHost"], f"llm-api.{domain}")
                self.assertEqual(result_policy["adminHost"], f"llm-admin.{domain}")
                self.assertEqual(result_admin["web"]["redirectUris"], [f"https://llm-admin.{domain}/auth/callback"])
                for patch in patches:
                    plane = patch["target"]["name"]
                    self.assertEqual([entry["value"] for entry in json.loads(patch["patch"])], [f"{plane}.{domain}"] * 2)
        self.assertEqual((policy, admin), originals)

    def test_invalid_domains_are_rejected(self):
        for domain in ("https://tenant.test", "tenant.test:443", "tenant.test/path", "*.tenant.test", "localhost", "127.0.0.1", "bad..test", "-bad.test", "example.com", "x.example.com"):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                domain_hosts(domain)
        self.assertEqual(domain_hosts(" Tenant.TEST. ")["api"], "llm-api.tenant.test")

    def test_generated_overlay_renders_consistently_and_changes_config_hash(self):
        (ROOT / "temp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            previous_name = None
            for domain in ("demo.test.invalid", "customer.other.invalid"):
                render(domain, Path(directory))
                result = subprocess.run(["kubectl", "kustomize", directory], capture_output=True, text=True, check=True)
                documents = list(yaml.safe_load_all(result.stdout))
                config = next(item for item in documents if item["kind"] == "ConfigMap" and "policy.json" in item.get("data", {}))
                self.assertEqual(json.loads(config["data"]["policy.json"])["adminHost"], f"llm-admin.{domain}")
                self.assertNotEqual(config["metadata"]["name"], previous_name)
                previous_name = config["metadata"]["name"]
                for ingress in (item for item in documents if item["kind"] == "Ingress"):
                    host = f"{ingress['metadata']['name']}.{domain}"
                    self.assertEqual(ingress["spec"]["rules"][0]["host"], host)
                    self.assertEqual(ingress["spec"]["tls"][0]["hosts"], [host])
                for proxy in (item for item in documents if item["kind"] == "Deployment" and item["metadata"]["name"].endswith("-proxy")):
                    volume = next(item for item in proxy["spec"]["template"]["spec"]["volumes"] if item["name"] == "config")
                    self.assertEqual(volume["configMap"]["name"], config["metadata"]["name"])

    def test_output_outside_ignored_temp_is_rejected(self):
        with self.assertRaises(ValueError):
            render("tenant.test", ROOT / "deploy/overlays/prod")

    def test_stage8_domain_overlay_keeps_audit_disabled(self):
        (ROOT / "temp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            render("customer.test.invalid", Path(directory), stage=8)
            result = subprocess.run(["kubectl", "kustomize", directory], capture_output=True, text=True, check=True)
            documents = list(yaml.safe_load_all(result.stdout))
            settings = next(item["data"]["config.json"] for item in documents if item["kind"] == "ConfigMap" and "config.json" in item.get("data", {}))
            self.assertFalse(json.loads(settings)["l3"]["enabled"])

    def test_stage9_domain_overlay_preserves_private_admin_and_exact_api_paths(self):
        (ROOT / "temp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            render("customer.test.invalid", Path(directory), stage=9)
            result = subprocess.run(["kubectl", "kustomize", directory], capture_output=True, text=True, check=True)
            documents = list(yaml.safe_load_all(result.stdout))
            api = next(item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == "llm-api")
            admin = next(item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == "llm-admin")
            self.assertEqual(api["spec"]["rules"][0]["host"], "llm-api.customer.test.invalid")
            self.assertTrue(all(item["pathType"] == "Exact" for item in api["spec"]["rules"][0]["http"]["paths"]))
            self.assertEqual(admin["spec"]["ingressClassName"], "REPLACE_PRIVATE_ADMIN_INGRESS_CLASS")