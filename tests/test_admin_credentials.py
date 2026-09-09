import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from azure.core.exceptions import ResourceNotFoundError

from scripts.admin_credentials import CREDENTIAL_NAME, OIDC_SECRET, SESSION_SECRET, initialize_credentials
from scripts.customer_migration import ROOT
from tests.test_proxy_config import APPS, proxy_customer


class AdminCredentialTests(unittest.TestCase):
    def test_initialization_reuses_keys_and_does_not_publish_values(self):
        config = proxy_customer()
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        application = {"id": APPS["admin"]["appId"], "appId": APPS["admin"]["appId"]}
        entries, stored, writes = [], {}, []
        graph, client, azure = Mock(), Mock(), Mock()
        azure.scoped.return_value = {"properties": {"provisioningState": "Succeeded"}}

        def graph_request(method, path, data=None):
            if method == "GET":
                return {"passwordCredentials": copy.deepcopy(entries)}
            entry = {"displayName": CREDENTIAL_NAME, "keyId": "99999999-9999-4999-8999-999999999999", "endDateTime": data["passwordCredential"]["endDateTime"]}
            entries.append(entry)
            return {**entry, "secretText": "synthetic-oidc-secret"}

        def get_secret(name):
            if name not in stored:
                raise ResourceNotFoundError()
            return stored[name]

        def set_secret(name, value, **kwargs):
            writes.append(name)
            item = SimpleNamespace(value=value, properties=SimpleNamespace(id="https://synthetic.vault.azure.net/secrets/" + name + "/" + "a" * 32, version="a" * 32, not_before=None, **{key: kwargs.get(key) for key in ("enabled", "expires_on", "tags")}))
            stored[name] = item
            return item

        graph.request.side_effect = graph_request
        client.get_secret.side_effect = get_secret
        client.set_secret.side_effect = set_secret
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.admin_credentials.secrets.token_bytes", return_value=b"a" * 32) as random:
            path = Path(directory)
            plan = initialize_credentials(config, "plan", "a" * 40, path, "", graph, client, application, "vault-id", azure, now)
            self.assertFalse(writes)
            random.assert_not_called()
            with self.assertRaisesRegex(ValueError, "plan changed"):
                initialize_credentials(config, "execute", "a" * 40, path, "f" * 64, graph, client, application, "vault-id", azure, now)
            result = initialize_credentials(config, "execute", "a" * 40, path, plan["planSha256"], graph, client, application, "vault-id", azure, now)
            self.assertTrue(result["initialized"])
            self.assertEqual(writes, [OIDC_SECRET, SESSION_SECRET])
            for artifact in path.glob("*.json"):
                self.assertNotIn("synthetic-oidc-secret", artifact.read_text())
                self.assertNotIn(stored[SESSION_SECRET].value, artifact.read_text())
            plan = initialize_credentials(config, "plan", "a" * 40, path, "", graph, client, application, "vault-id", azure, now)
            initialize_credentials(config, "execute", "a" * 40, path, plan["planSha256"], graph, client, application, "vault-id", azure, now)
            self.assertEqual(len(writes), 2)
            stored.pop(OIDC_SECRET)
            with self.assertRaisesRegex(ValueError, "unrecorded Entra"):
                initialize_credentials(config, "plan", "a" * 40, path, "", graph, client, application, "vault-id", azure, now)