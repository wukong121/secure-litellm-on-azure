import copy
import json
from pathlib import Path
import subprocess
import unittest

import yaml

from scripts.validate_stage9 import validate_manifests, validate_templates

ROOT = Path(__file__).resolve().parents[1]


class Stage9TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def compile_template(path):
            return json.loads(subprocess.run(["az", "bicep", "build", "--file", path, "--stdout"], cwd=ROOT, capture_output=True, text=True, check=True).stdout)
        cls.edge = compile_template("infra/edge/main.bicep")
        cls.origin = compile_template("infra/edge-origin/main.bicep")
        cls.documents = list(yaml.safe_load_all(subprocess.run(["kubectl", "kustomize", "deploy/validation/stage9"], cwd=ROOT, capture_output=True, text=True, check=True).stdout))

    def test_defaults_and_composed_manifests(self):
        validate_templates(self.edge, self.origin)
        validate_manifests(self.documents)

    def test_public_default_domain_cache_and_wildcard_routes_are_rejected(self):
        for property_name, value in (("linkToDefaultDomain", "Enabled"), ("patternsToMatch", ["/*"]), ("cacheConfiguration", {}), ("forwardingProtocol", "HttpOnly")):
            with self.subTest(property_name=property_name):
                edge = copy.deepcopy(self.edge)
                resources = edge["resources"]
                route = next(item for item in (resources.values() if isinstance(resources, dict) else resources) if item["type"].endswith("/routes"))
                route["properties"][property_name] = value
                with self.assertRaises(AssertionError):
                    validate_templates(edge, self.origin)

    def test_ingress_wildcard_is_rejected(self):
        documents = copy.deepcopy(self.documents)
        ingress = next(item for item in documents if item["kind"] == "Ingress" and item["metadata"]["name"] == "llm-api")
        ingress["spec"]["rules"][0]["http"]["paths"][0]["path"] = "/"
        with self.assertRaises(AssertionError):
            validate_manifests(documents)

    def test_new_resources_cannot_be_enabled_by_default(self):
        edge = copy.deepcopy(self.edge)
        edge["parameters"]["deployEdge"]["defaultValue"] = True
        with self.assertRaises(AssertionError):
            validate_templates(edge, self.origin)