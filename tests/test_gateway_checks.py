import unittest
from types import SimpleNamespace

from scripts.gateway_checks import client_certificate_required, gateway_checks
from tests.test_customer_migration import customer_config


class GatewayCheckTests(unittest.TestCase):
    def test_only_explicit_client_certificate_tls_alert_is_accepted(self):
        self.assertTrue(client_certificate_required(SimpleNamespace(reason="TLSV13_ALERT_CERTIFICATE_REQUIRED")))
        self.assertFalse(client_certificate_required(SimpleNamespace(reason="SSLV3_ALERT_HANDSHAKE_FAILURE")))

    def test_all_addresses_checked_and_partial_report_never_certifies_stage(self):
        calls = []
        def request(host, address, path, host_header, method):
            calls.append((host, address, method))
            return {"status": "tls_client_certificate_required" if host.startswith("llm-admin") else 403, "certificateSha256": "a" * 64}
        def resolve(host):
            return ["203.0.113.10", "203.0.113.11"] if host.startswith("llm-admin") else ["10.30.4.10", "10.30.4.11"]
        result = gateway_checks(customer_config(), "a" * 40, resolve, request)
        self.assertTrue(all(check["status"] == "passed" for check in result["checks"].values()))
        self.assertFalse(result["stageAccepted"])
        self.assertEqual(len(calls), 6)
        self.assertIn("object_ownership", result["notCovered"])

    def test_private_admin_or_successful_unauthenticated_request_fails(self):
        result = gateway_checks(customer_config(), "a" * 40, lambda _: ["10.30.4.10"], lambda *_: {"status": 200})
        self.assertTrue(all(check["status"] == "failed" for check in result["checks"].values()))
        self.assertNotIn("10.30.4.10", str(result))