import json
from pathlib import Path
import subprocess
import unittest

from scripts.customer_migration import COMPONENTS, ROOT, parameters_for, validate_config


def example_customer():
    config = json.loads((ROOT / "config/customer.example.json").read_text())

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str) and value.startswith("REPLACE_"):
            return "synthetic-value"
        return value

    config = replace(config)
    config.update(location="westus", baseDomain="customer.invalid", ownerEmail="owner@customer.invalid")
    config["azure"] = {"tenantId": "11111111-1111-4111-8111-111111111111", "subscriptionId": "22222222-2222-4222-8222-222222222222"}
    config["legacy"]["resourceGroup"] = "rg-legacy"
    config["target"]["resourceGroup"] = "rg-target"
    config["parameters"]["platform"]["stage4Aks"]["name"] = "synthetic-new-aks"
    return validate_config(config, "test")


class CustomerTemplateTests(unittest.TestCase):
    def test_generated_parameters_match_compiled_bicep_contracts(self):
        config = example_customer()
        for component, (first_stage, directory, _allowed) in COMPONENTS.items():
            result = subprocess.run(["az", "bicep", "build", "--file", str(ROOT / f"infra/{directory}/main.bicep"), "--stdout"], capture_output=True, text=True, check=True)
            compiled = json.loads(result.stdout)
            declarations = compiled["parameters"]
            for stage in ((3, 4, 5) if component == "platform" else (first_stage,)):
                with self.subTest(component=component, stage=stage):
                    _template, document = parameters_for(config, stage, component)
                    parameters = document["parameters"]
                    self.assertFalse(set(parameters) - set(declarations))
                    self.assertFalse({name for name, declaration in declarations.items() if "defaultValue" not in declaration} - set(parameters))
                    for name, item in parameters.items():
                        declaration = declarations[name]
                        if "$ref" in declaration:
                            declaration = compiled["definitions"][declaration["$ref"].removeprefix("#/definitions/")]
                        expected = declaration["type"]
                        types = {"string": str, "object": dict, "array": list, "bool": bool, "int": int}
                        self.assertIsInstance(item["value"], types[expected], name)