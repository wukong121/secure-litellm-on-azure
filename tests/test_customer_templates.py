import json
import copy
from pathlib import Path
import subprocess
import unittest

from scripts.customer_migration import COMPONENTS, ROOT, parameters_for, validate_config
from tests.test_customer_migration import certificate_config


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
    config["parameters"]["certificate-vault"] = certificate_config()["parameters"]["certificate-vault"]
    config["parameters"]["runner-connectivity"]["runnerVirtualNetworkId"] = config["parameters"]["certificate-vault"]["runnerVirtualNetworkId"]
    return validate_config(config, "test")


class CustomerTemplateTests(unittest.TestCase):
    def test_generated_parameters_match_compiled_bicep_contracts(self):
        config = example_customer()
        config["databaseAccess"] = {"migrationPrincipalId": "33333333-3333-4333-8333-333333333333"}
        for component, (first_stage, directory, _allowed) in COMPONENTS.items():
            selected = copy.deepcopy(config)
            if component == "network":
                selected["parameters"]["network"] = {key: selected["parameters"]["backup"][key] for key in COMPONENTS["network"][2]}
                selected["parameters"].pop("backup")
                validate_config(selected, "test")
            result = subprocess.run(["az", "bicep", "build", "--file", str(ROOT / f"infra/{directory}/main.bicep"), "--stdout"], capture_output=True, text=True, check=True)
            compiled = json.loads(result.stdout)
            if component == "certificate-vault":
                self.assert_certificate_vault_contract(compiled)
            declarations = compiled["parameters"]
            for stage in ((3, 4, 5) if component == "platform" else (first_stage,)):
                with self.subTest(component=component, stage=stage):
                    _template, document = parameters_for(selected, stage, component)
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

    def assert_certificate_vault_contract(self, compiled):
        resources = compiled["resources"]
        resources = list(resources.values()) if isinstance(resources, dict) else resources
        vaults = [item for item in resources if item["type"] == "Microsoft.KeyVault/vaults"]
        self.assertEqual(len(vaults), 1)
        properties = vaults[0]["properties"]
        self.assertEqual(properties["publicNetworkAccess"], "Disabled")
        self.assertEqual(properties["networkAcls"]["bypass"], "None")
        self.assertEqual(properties["networkAcls"]["defaultAction"], "Deny")
        self.assertTrue(properties["enableRbacAuthorization"])
        self.assertTrue(properties["enablePurgeProtection"])
        self.assertTrue(properties["enableSoftDelete"])
        self.assertEqual(properties["softDeleteRetentionInDays"], 90)
        self.assertEqual(properties["sku"]["name"], "standard")
        roles = [item for item in resources if item["type"] == "Microsoft.Authorization/roleAssignments"]
        self.assertEqual(len(roles), 2)
        for assignment, principal, role in zip(roles, ("ingressReaderPrincipalId", "certificateImporterPrincipalId"), ("4633458b-17de-408a-b874-0445c86b69e6", "b86a8fe4-44ce-4948-aee5-eccb2c155cd7")):
            self.assertEqual(assignment["properties"]["principalId"], f"[parameters('{principal}')]")
            self.assertIn(role, assignment["properties"]["roleDefinitionId"])
            self.assertIn("Microsoft.KeyVault/vaults", assignment["scope"])
        self.assertFalse(any(item["type"] == "Microsoft.KeyVault/vaults/secrets" for item in resources))
        outputs = compiled["outputs"]["certificateVault"]["value"]
        self.assertFalse(outputs["certificateMaterialsImported"])
        self.assertIn("secrets/api-tls", outputs["apiTlsSecretId"])
        self.assertIn("secrets/admin-tls", outputs["adminTlsSecretId"])