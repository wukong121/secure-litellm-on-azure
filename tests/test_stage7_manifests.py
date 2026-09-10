import copy
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.validate_stage7 import validate


class Stage7ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        rendered = subprocess.run(
            ["kubectl", "kustomize", "deploy/validation/stage7"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
        cls.documents = list(yaml.safe_load_all(rendered))

    def check_documents(self, documents):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stage7.yaml"
            path.write_text(yaml.safe_dump_all(documents), encoding="utf-8")
            validate(path)

    def test_valid_template(self):
        self.check_documents(self.documents)

    def test_direct_ingress_bypass_is_rejected(self):
        documents = copy.deepcopy(self.documents)
        policy = next(item for item in documents if item["metadata"]["name"] == "allow-litellm-required-traffic")
        policy["spec"]["ingress"].append({"from": [{"namespaceSelector": {}}]})
        with self.assertRaises(AssertionError):
            self.check_documents(documents)

    def test_shared_admin_ingress_is_rejected(self):
        documents = copy.deepcopy(self.documents)
        ingress = next(item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == "llm-admin")
        ingress["spec"]["ingressClassName"] = "REPLACE_PRIVATE_API_INGRESS_CLASS"
        with self.assertRaises(AssertionError):
            self.check_documents(documents)

    def test_public_service_is_rejected(self):
        documents = copy.deepcopy(self.documents)
        service = next(item for item in documents if item["kind"] == "Service")
        service["spec"]["type"] = "LoadBalancer"
        with self.assertRaises(AssertionError):
            self.check_documents(documents)

    def test_proxy_secret_sync_is_rejected(self):
        documents = copy.deepcopy(self.documents)
        provider = next(item for item in documents if item["kind"] == "SecretProviderClass" and item["metadata"]["name"] == "llm-admin-auth")
        provider["spec"]["secretObjects"] = []
        with self.assertRaises(AssertionError):
            self.check_documents(documents)

    def test_api_internal_credential_mount_is_rejected(self):
        documents = copy.deepcopy(self.documents)
        api = next(item for item in documents if item["kind"] == "Deployment" and item["metadata"]["name"] == "llm-api-proxy")
        api["spec"]["template"]["spec"]["volumes"].append({"name": "legacy-key", "secret": {"secretName": "legacy-key"}})
        with self.assertRaises(AssertionError):
            self.check_documents(documents)