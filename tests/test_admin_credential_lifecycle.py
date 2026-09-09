import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.admin_credentials import CREDENTIAL_NAME, OIDC_SECRET, SESSION_SECRET, manage_credential_lifecycle
from scripts.customer_migration import ROOT
from tests.test_proxy_config import APPS, proxy_customer


class AdminLifecycleTests(unittest.TestCase):
    def test_session_rotation_preserves_oidc_and_requires_rollout(self):
        config = proxy_customer()
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        application = {"id": APPS["admin"]["appId"], "appId": APPS["admin"]["appId"]}
        state = {"current": {"version": "oidc"}, "session": {"version": "old-session"}, "orphaned": [], "credentials": [], "versions": []}
        client, graph, azure = Mock(), Mock(), Mock()
        client.set_secret.side_effect = lambda name, value, **kwargs: SimpleNamespace(value=value)
        metadata = {"oidc": state["current"], "session": {"version": "new-session"}}
        with tempfile.TemporaryDirectory() as directory, patch("scripts.admin_credentials.lifecycle_state", return_value=state), patch("scripts.admin_credentials.inspect_credentials", return_value=({}, metadata)), patch("scripts.admin_credentials.save_credential_receipt") as receipt:
            path = Path(directory)
            args = (config, "plan", "admin-credentials-session-rotate", "a" * 40, path, "", graph, client, application, "vault", azure)
            plan = manage_credential_lifecycle(*args)
            client.set_secret.assert_not_called()
            result = manage_credential_lifecycle(config, "execute", "admin-credentials-session-rotate", "a" * 40, path, plan["planSha256"], graph, client, application, "vault", azure)
            self.assertTrue(result["adminRolloutRequired"])
            self.assertFalse(result["activeCookiesInvalidated"])
            self.assertEqual(client.set_secret.call_args.args[0], SESSION_SECRET)
            graph.request.assert_not_called()
            receipt.assert_called_once()
            value = client.set_secret.call_args.args[1]
            self.assertNotIn(value, (path / "runtime-summary.json").read_text())

    def test_retirement_only_removes_expired_noncurrent_keys(self):
        config = proxy_customer()
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        application = {"id": APPS["admin"]["appId"]}
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        state = {"current": {"keyId": "current"}, "session": {"version": "session"}, "orphaned": [], "versions": [{"version": "old", "keyId": "expired"}], "credentials": [
            {"keyId": "expired", "endDateTime": "2026-09-01T00:00:00Z"}, {"keyId": "valid", "endDateTime": "2026-12-01T00:00:00Z"}, {"keyId": "current", "endDateTime": "2026-12-01T00:00:00Z"}]}
        graph, client, azure = Mock(), Mock(), Mock()
        def remove(method, path, body):
            self.assertEqual(body, {"keyId": "expired"})
            state["credentials"] = [entry for entry in state["credentials"] if entry["keyId"] != body["keyId"]]
        graph.request.side_effect = remove
        with tempfile.TemporaryDirectory() as directory, patch("scripts.admin_credentials.lifecycle_state", side_effect=lambda *args: copy.deepcopy(state)), patch("scripts.admin_credentials.inspect_credentials", return_value=({}, {})):
            path = Path(directory)
            plan = manage_credential_lifecycle(config, "plan", "admin-credentials-retire-expired", "a" * 40, path, "", graph, client, application, "vault", azure, now)
            result = manage_credential_lifecycle(config, "execute", "admin-credentials-retire-expired", "a" * 40, path, plan["planSha256"], graph, client, application, "vault", azure, now)
            self.assertEqual(result["retiredExpiredKeyIds"], ["expired"])
            self.assertTrue(result["vaultVersionsRetained"])
            client.set_secret.assert_not_called()
            self.assertEqual(len(state["credentials"]), 2)

    def test_orphan_recovery_stops_if_a_reference_appears_after_approval(self):
        config = proxy_customer()
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        application = {"id": APPS["admin"]["appId"], "appId": APPS["admin"]["appId"]}
        orphan = {"keyId": "33333333-1111-4111-8111-111111111111", "displayName": CREDENTIAL_NAME, "endDateTime": "2099-01-01T00:00:00Z"}
        state = {"current": None, "session": None, "versions": [], "credentials": [orphan], "orphaned": [orphan]}
        protected = {**state, "versions": [{"id": "new-reference", "version": "new", "keyId": orphan["keyId"]}], "orphaned": []}
        graph, client, azure = Mock(), Mock(), Mock()
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.admin_credentials.lifecycle_state", return_value=state) as inspect:
            path = Path(directory)
            plan = manage_credential_lifecycle(config, "plan", "admin-credentials-recover", "a" * 40, path, "", graph, client, application, "vault", azure, now)
            graph.request.assert_not_called()
            inspect.side_effect = [state, state, protected]
            with self.assertRaisesRegex(ValueError, "gained a Vault reference"):
                manage_credential_lifecycle(config, "execute", "admin-credentials-recover", "a" * 40, path, plan["planSha256"], graph, client, application, "vault", azure, now)
            graph.request.assert_not_called()
            inspect.side_effect = [state, state, state, {**state, "credentials": [], "orphaned": []}]
            result = manage_credential_lifecycle(config, "execute", "admin-credentials-recover", "a" * 40, path, plan["planSha256"], graph, client, application, "vault", azure, now)
            self.assertTrue(result["recovered"])
            graph.request.assert_called_once_with("POST", "/applications/" + application["id"] + "/removePassword", {"keyId": orphan["keyId"]})
            client.set_secret.assert_not_called()

    def test_rotation_retains_old_key_and_recovery_only_removes_orphans(self):
        config = proxy_customer()
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        application = {"id": APPS["admin"]["appId"], "appId": APPS["admin"]["appId"]}
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        old_id, new_id, orphan_id = [f"{number}" * 8 + "-1111-4111-8111-111111111111" for number in (1, 2, 3)]
        old = SimpleNamespace(value="synthetic-old-oidc", properties=SimpleNamespace(id="old-secret-id", version="a" * 32, enabled=True, not_before=None, expires_on=now + timedelta(days=1), tags={"llmgw-purpose": OIDC_SECRET, "llmgw-application-id": application["id"], "llmgw-key-id": old_id}))
        session = SimpleNamespace(value="YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=", properties=SimpleNamespace(id="session-id", version="s", enabled=True, not_before=None, expires_on=None, tags={"llmgw-purpose": SESSION_SECRET, "llmgw-application-id": application["id"]}))
        versions, stored = [old.properties], {OIDC_SECRET: old, SESSION_SECRET: session}
        keys = [{"keyId": old_id, "displayName": CREDENTIAL_NAME, "endDateTime": old.properties.expires_on.isoformat()}, {"keyId": "unmanaged", "displayName": "customer-unrelated", "endDateTime": "2099-01-01T00:00:00Z"}]
        graph, client, azure = Mock(), Mock(), Mock()
        removed = []

        def request(method, path, body=None):
            if method == "GET":
                return {"passwordCredentials": copy.deepcopy(keys)}
            if path.endswith("addPassword"):
                entry = {"keyId": new_id, "displayName": CREDENTIAL_NAME, "endDateTime": body["passwordCredential"]["endDateTime"]}
                keys.append(entry)
                return {**entry, "secretText": "synthetic-new-oidc"}
            removed.append(body["keyId"])
            keys[:] = [key for key in keys if key["keyId"] != body["keyId"]]
            return {}

        def write(name, value, **kwargs):
            self.assertEqual(name, OIDC_SECRET)
            item = SimpleNamespace(value=value, properties=SimpleNamespace(id="new-secret-id", version="b" * 32, enabled=True, not_before=None, expires_on=kwargs["expires_on"], tags=kwargs["tags"]))
            versions.append(item.properties)
            stored[name] = item
            return item

        graph.request.side_effect = request
        client.get_secret.side_effect = lambda name: stored[name]
        client.list_properties_of_secret_versions.side_effect = lambda name: list(versions)
        client.set_secret.side_effect = write
        azure.scoped.return_value = {"properties": {"provisioningState": "Succeeded"}}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            path = Path(directory)
            arguments = (config, "plan", "admin-credentials-rotate", "a" * 40, path, "", graph, client, application, "vault", azure, now)
            plan = manage_credential_lifecycle(*arguments)
            client.set_secret.assert_not_called()
            with self.assertRaisesRegex(ValueError, "plan changed"):
                manage_credential_lifecycle(config, "execute", "admin-credentials-rotate", "a" * 40, path, "f" * 64, graph, client, application, "vault", azure, now)
            result = manage_credential_lifecycle(config, "execute", "admin-credentials-rotate", "a" * 40, path, plan["planSha256"], graph, client, application, "vault", azure, now)
            self.assertTrue(result["rotated"])
            self.assertEqual(result["previousVersionRetained"], old.properties.version)
            self.assertFalse(removed)
            self.assertIs(stored[SESSION_SECRET], session)
            keys.append({"keyId": orphan_id, "displayName": CREDENTIAL_NAME, "endDateTime": "2099-01-01T00:00:00Z"})
            with self.assertRaisesRegex(ValueError, "recover"):
                manage_credential_lifecycle(*arguments)
            plan = manage_credential_lifecycle(config, "plan", "admin-credentials-recover", "a" * 40, path, "", graph, client, application, "vault", azure, now)
            manage_credential_lifecycle(config, "execute", "admin-credentials-recover", "a" * 40, path, plan["planSha256"], graph, client, application, "vault", azure, now)
            self.assertEqual(removed, [orphan_id])
            self.assertIn(old_id, [entry["keyId"] for entry in keys])
            self.assertIn("unmanaged", [entry["keyId"] for entry in keys])
            for artifact in path.glob("*.json"):
                self.assertNotIn("synthetic-new-oidc", artifact.read_text())
                self.assertNotIn(session.value, artifact.read_text())