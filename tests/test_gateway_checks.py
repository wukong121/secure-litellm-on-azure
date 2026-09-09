import unittest

from scripts.gateway_checks import gateway_checks
from tests.test_customer_migration import customer_config


class GatewayCheckTests(unittest.TestCase):
    def test_all_addresses_checked_and_partial_report_never_certifies_stage(self):
        calls = []
        def request(host, address, path, host_header, method):
            calls.append((host, address, method))
            return {"status": 403, "certificateSha256": "a" * 64}
        result = gateway_checks(customer_config(), "a" * 40, lambda _: ["10.30.4.10", "10.30.4.11"], request)
        self.assertTrue(all(check["status"] == "passed" for check in result["checks"].values()))
        self.assertFalse(result["stageAccepted"])
        self.assertEqual(len(calls), 6)
        self.assertIn("object_ownership", result["notCovered"])

    def test_public_admin_or_successful_unauthenticated_request_fails(self):
        result = gateway_checks(customer_config(), "a" * 40, lambda _: ["8.8.8.8"], lambda *_: {"status": 200})
        self.assertTrue(all(check["status"] == "failed" for check in result["checks"].values()))
        self.assertNotIn("8.8.8.8", str(result))