import copy
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.validate_stage8 import validate


class Stage8ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        rendered = subprocess.run(["kubectl", "kustomize", "deploy/validation/stage8"], cwd=root, capture_output=True, text=True, check=True).stdout
        cls.documents = list(yaml.safe_load_all(rendered))

    def check_documents(self, documents):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stage8.yaml"
            path.write_text(yaml.safe_dump_all(documents), encoding="utf-8")
            validate(path)

    def test_valid_template(self):
        self.check_documents(self.documents)

    def test_accidental_l3_enable_is_rejected(self):
        documents = copy.deepcopy(self.documents)
        config = next(item for item in documents if "config.json" in item.get("data", {}))
        config["data"]["config.json"] = config["data"]["config.json"].replace('"enabled": false', '"enabled": true', 1)
        with self.assertRaises(AssertionError):
            self.check_documents(documents)

    def test_automatic_retention_enable_is_rejected(self):
        documents = copy.deepcopy(self.documents)
        next(item for item in documents if item["kind"] == "CronJob")["spec"]["suspend"] = False
        with self.assertRaises(AssertionError):
            self.check_documents(documents)