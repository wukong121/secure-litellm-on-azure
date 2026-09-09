import copy
import json
import subprocess
import unittest

from scripts.customer_migration import ROOT, stage_fingerprint, validate_config
from scripts.migration_runtime import validate_action
from scripts.proxy_config import credential_name, entra_documents, proxy_policy, proxy_settings
from tests.test_backend_manifest import backend_customer


def proxy_customer():
    config = backend_customer()
    config["proxy"] = {"apiClientIds": ["66666666-6666-4666-8666-666666666666"], "bindings": [
        {"oid": "77777777-7777-4777-8777-777777777777", "plane": "api", "role": "internal_user", "models": ["coding"], "auditTeamId": "coding-team"},
        {"oid": "88888888-8888-4888-8888-888888888888", "plane": "admin", "role": "proxy_admin_viewer", "models": ["coding"]},
    ]}
    return config


APPS = {"api": {"appId": "33333333-3333-4333-8333-333333333333"}, "admin": {"appId": "44444444-4444-4444-8444-444444444444"}}


class ProxyConfigurationTests(unittest.TestCase):
    def test_stage7_decisions_do_not_invalidate_earlier_receipts(self):
        config = proxy_customer()
        plain = copy.deepcopy(config)
        plain.pop("proxy")
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        validate_config(config, "test")
        self.assertEqual(stage_fingerprint(config, 6), stage_fingerprint(plain, 6))
        self.assertNotEqual(stage_fingerprint(config, 7), stage_fingerprint(plain, 7))
        for action in ("entra-apps", "entra-access", "admin-credentials", "admin-credentials-rotate", "admin-credentials-recover"):
            validate_action(config, 7, action)
            with self.assertRaises(ValueError):
                validate_action(config, 6, action)

    def test_generated_policy_passes_existing_node_validator(self):
        config = proxy_customer()
        policy = proxy_policy(config, APPS)
        result = subprocess.run(["node", "--input-type=module", "-e", "import {validateConfig} from './auth-proxy/policy.mjs'; let input=''; for await (const chunk of process.stdin) input+=chunk; validateConfig(JSON.parse(input));"], input=json.dumps(policy), text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(policy["apiAudience"], APPS["api"]["appId"])
        self.assertNotEqual(policy["bindings"][0]["keyFile"], policy["bindings"][1]["keyFile"])
        self.assertEqual(policy["bindings"][0]["audit"]["teamId"], "coding-team")
        documents = entra_documents(config, APPS["api"]["appId"])
        self.assertEqual(documents["admin"]["web"]["redirectUris"], ["https://llm-admin.customer.invalid/auth/callback"])
        self.assertEqual(documents["api"]["signInAudience"], "AzureADMyOrg")

    def test_keys_are_stable_and_separated_by_plane_and_tenant(self):
        binding = proxy_customer()["proxy"]["bindings"][0]
        tenant = proxy_customer()["azure"]["tenantId"]
        first = credential_name(tenant, binding)
        self.assertEqual(first, credential_name(tenant, binding))
        self.assertNotEqual(first, credential_name(tenant, {**binding, "plane": "admin"}))
        self.assertNotEqual(first, credential_name("99999999-9999-4999-8999-999999999999", binding))

    def test_privilege_and_unknown_model_overrides_are_rejected(self):
        for updates in ({"role": "proxy_admin"}, {"models": ["*"]}, {"models": ["unknown"]}, {"keyFile": "master-key"}, {"oid": "00000000-0000-0000-0000-000000000000"}):
            config = proxy_customer()
            config["proxy"]["bindings"][0].update(updates)
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                proxy_settings(config)
        config = proxy_customer()
        config["proxy"]["bindings"].append(copy.deepcopy(config["proxy"]["bindings"][0]))
        with self.assertRaises(ValueError):
            proxy_settings(config)