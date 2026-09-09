import json
import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.customer_migration import ROOT
from scripts.proxy_credentials_runtime import BackendAPI, backend_tunnel, binding_plan
from tests.test_proxy_config import proxy_customer
from tests.test_proxy_credentials import CredentialFixture


class ProxyCredentialRuntimeTests(unittest.TestCase):
    def test_tunnel_binds_loopback_and_terminates_after_failure(self):
        process = Mock()
        process.stdout = io.StringIO("Forwarding from 127.0.0.1:43210 -> 4000\n")
        process.poll.return_value = None
        with patch("scripts.proxy_credentials_runtime.subprocess.Popen", return_value=process) as spawn:
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with backend_tunnel(["kubectl", "--kubeconfig", "/synthetic/config"]) as endpoint:
                    self.assertEqual(endpoint, "http://127.0.0.1:43210")
                    raise RuntimeError("synthetic failure")
            self.assertIn("--address=127.0.0.1", spawn.call_args.args[0])
            process.terminate.assert_called_once()
            process.wait.assert_called_once()
            self.assertTrue(process.stdout.closed)

    def test_controller_requires_plan_and_never_publishes_key_values(self):
        config = proxy_customer()
        store = CredentialFixture()
        vaults = {"api": store, "admin": store}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            path = Path(directory)
            plan = binding_plan(config, "plan", "a" * 40, path, "", vaults, store)
            self.assertFalse(store.values)
            with self.assertRaisesRegex(ValueError, "plan changed"):
                binding_plan(config, "execute", "a" * 40, path, "f" * 64, vaults, store)
            result = binding_plan(config, "execute", "a" * 40, path, plan["planSha256"], vaults, store)
            self.assertTrue(result["initialized"])
            self.assertEqual(len(store.keys), 2)
            for value in store.values.values():
                self.assertNotIn(value.value, json.dumps(result))
                self.assertNotIn(value.value, (path / "runtime-review.json").read_text())

    def test_http_client_restricts_routes_and_hash_only_lookups(self):
        with self.assertRaises(ValueError):
            BackendAPI("https://untrusted.invalid", "synthetic-master")
        client = BackendAPI("http://127.0.0.1:12345", "synthetic-master")
        try:
            self.assertFalse(client.session.trust_env)
            request = Mock(return_value=Mock(status_code=404))
            client.session.request = request
            self.assertIsNone(client.key_info("a" * 64))
            self.assertEqual(request.call_args.kwargs["params"], {"key": "a" * 64})
            self.assertFalse(request.call_args.kwargs["allow_redirects"])
            with self.assertRaises(ValueError):
                client.key_info("sk-synthetic-raw")
            with self.assertRaises(ValueError):
                client.request("POST", "/key/delete", {})
        finally:
            client.close()