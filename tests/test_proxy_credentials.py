import copy
import hashlib
import unittest
from types import SimpleNamespace

from azure.core.exceptions import ResourceNotFoundError

from scripts.proxy_credentials import binding_contract, credential_bindings, inspect_binding, key_payload, provision_binding
from tests.test_proxy_config import proxy_customer


class CredentialFixture:
    def __init__(self):
        self.values, self.users, self.keys = {}, {}, {}
        self.creates = 0
        self.interrupt = False

    def get_secret(self, name):
        if name not in self.values:
            raise ResourceNotFoundError()
        return self.values[name]

    def set_secret(self, name, value, **kwargs):
        self.values[name] = SimpleNamespace(value=value, properties=SimpleNamespace(id=name, version="a" * 32, enabled=True, expires_on=None, tags=kwargs["tags"]))
        return self.values[name]

    def update_secret_properties(self, name, version, tags):
        self.values[name].properties.tags = tags

    def user_info(self, user_id):
        return self.users.get(user_id)

    def key_info(self, digest):
        return self.keys.get(digest)

    def create_user(self, payload):
        self.users[payload["user_id"]] = copy.deepcopy(payload)

    def create_key(self, payload):
        self.creates += 1
        digest = hashlib.sha256(payload["key"].encode()).hexdigest()
        self.keys[digest] = {key: value for key, value in payload.items() if key != "key"}
        if self.interrupt:
            self.interrupt = False
            raise RuntimeError("synthetic lost response")


class ProxyCredentialTests(unittest.TestCase):
    def test_pending_secret_recovers_unknown_create_result_without_duplicate_keys(self):
        config = proxy_customer()
        binding = config["proxy"]["bindings"][0]
        store = CredentialFixture()
        _contract, secret, summary = inspect_binding(config, binding, store, store)
        self.assertIsNone(secret)
        self.assertFalse(summary["keyExists"])
        self.assertFalse(store.values)
        store.interrupt = True
        with self.assertRaises(RuntimeError):
            provision_binding(config, binding, store, store)
        self.assertEqual(next(iter(store.values.values())).properties.tags["llmgw-state"], "pending")
        result = provision_binding(config, binding, store, store)
        self.assertEqual(store.creates, 1)
        self.assertEqual(result["secret"]["state"], "ready")
        provision_binding(config, binding, store, store)
        self.assertEqual(store.creates, 1)
        store.keys.clear()
        with self.assertRaisesRegex(ValueError, "revoked key"):
            provision_binding(config, binding, store, store)

    def test_key_scopes_follow_plane_and_admin_role(self):
        config = proxy_customer()
        api, admin = config["proxy"]["bindings"]
        api_request = key_payload(binding_contract(config, api), "synthetic")
        admin_request = key_payload(binding_contract(config, admin), "synthetic")
        self.assertEqual(api_request["key_type"], "default")
        self.assertNotIn("/key/block", api_request["allowed_routes"])
        self.assertEqual(admin_request["key_type"], "default")
        self.assertNotIn("/key/block", admin_request["allowed_routes"])
        config["proxy"]["bindings"].append({"oid": "99999999-9999-4999-8999-999999999999", "plane": "admin", "role": "audit_reader"})
        self.assertEqual(len(credential_bindings(config)), 2)

    def test_existing_role_or_key_permissions_cannot_be_overwritten(self):
        config = proxy_customer()
        binding = config["proxy"]["bindings"][0]
        store = CredentialFixture()
        provision_binding(config, binding, store, store)
        next(iter(store.users.values()))["user_role"] = "proxy_admin"
        with self.assertRaisesRegex(ValueError, "role/models"):
            provision_binding(config, binding, store, store)