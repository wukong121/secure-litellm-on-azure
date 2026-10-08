import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from scripts.customer_migration import ROOT, MigrationError, stage_fingerprint
from scripts.migration_deploy import deploy_component
from scripts.private_ingress_runtime import verify_endpoint
from tests.test_customer_migration import customer_config


class PrivateIngressProbeTests(unittest.TestCase):
    identifier = "11111111-1111-4111-8111-111111111111"

    def probe(self, plane, statuses, identifier=None, certificate=b"public-certificate"):
        context = MagicMock()
        secured = context.wrap_socket.return_value.__enter__.return_value
        secured.getpeercert.return_value = certificate
        with patch("scripts.private_ingress_runtime.ssl.create_default_context", return_value=context), \
                patch("scripts.private_ingress_runtime.socket.create_connection"), \
                patch("scripts.private_ingress_runtime.http.client.HTTPResponse",
                      side_effect=[Mock(status=status) for status in statuses]):
            verify_endpoint("10.30.4.11", f"llm-{plane}.customer.invalid",
                            f"llm-{'api' if plane == 'admin' else 'admin'}.customer.invalid",
                            hashlib.sha256(b"public-certificate").hexdigest(),
                            native=True, plane=plane, front_door_id=identifier)
        return [call.args[0].decode("ascii") for call in secured.sendall.call_args_list]

    def test_unbound_admin_keeps_existing_login_check(self):
        requests = self.probe("admin", [404, 200])
        self.assertEqual(len(requests), 2)
        self.assertTrue(all("X-Azure-FDID" not in request for request in requests))

    def test_unbound_api_keeps_health_and_admin_isolation(self):
        requests = self.probe("api", [421, 200, 404])
        self.assertIn("GET /readyz", requests[1])
        self.assertIn("GET /fallback/login", requests[2])

    def test_bound_admin_requires_rejections_and_correct_header_success(self):
        requests = self.probe("admin", [404, 404, 404, 200], self.identifier)
        self.assertIn(f"X-Azure-FDID: {self.identifier}", requests[0])
        self.assertIn("Host: llm-api.customer.invalid", requests[0])
        self.assertNotIn("X-Azure-FDID", requests[1])
        self.assertIn("X-Azure-FDID: 00000000-0000-0000-0000-000000000001", requests[2])
        self.assertIn(f"X-Azure-FDID: {self.identifier}", requests[3])
        self.assertIn("GET /fallback/login", requests[3])

    def test_bound_api_tests_header_gate_and_unauthenticated_backend(self):
        for status in (401, 403):
            with self.subTest(status=status):
                requests = self.probe("api", [404, 200, 404, 404, 404, status], self.identifier)
                self.assertNotIn("X-Azure-FDID", requests[1])
                self.assertIn(f"X-Azure-FDID: {self.identifier}", requests[2])
                self.assertIn("POST /v1/responses", requests[-1])
                self.assertTrue(requests[-1].endswith("\r\n\r\n{}"))
                self.assertNotIn("Authorization:", requests[-1])

    def test_bound_admin_404_is_not_treated_as_success(self):
        with self.assertRaisesRegex(MigrationError, r"bound route.*HTTP 404.*200"):
            self.probe("admin", [404, 404, 404, 404], self.identifier)

    def test_missing_and_wrong_header_must_be_rejected(self):
        for statuses in ([404, 200], [404, 404, 200]):
            with self.subTest(statuses=statuses), self.assertRaisesRegex(MigrationError, "FDID rejection"):
                self.probe("admin", statuses, self.identifier)

    def test_api_cannot_bypass_authentication_or_return_not_found(self):
        for status in (200, 404, 500):
            with self.subTest(status=status), self.assertRaisesRegex(MigrationError, "bound route"):
                self.probe("api", [404, 200, 404, 404, 404, status], self.identifier)

    def test_wrong_header_is_distinct_even_for_sentinel_id(self):
        identifier = "00000000-0000-0000-0000-000000000001"
        requests = self.probe("admin", [404, 404, 404, 200], identifier)
        self.assertIn("X-Azure-FDID: 00000000-0000-0000-0000-000000000002", requests[2])

    def test_rejects_invalid_identity_before_network_access(self):
        for identifier in ("invalid", "00000000-0000-0000-0000-000000000000",
                           self.identifier + "\r\nInjected: true"):
            with patch("scripts.private_ingress_runtime.socket.create_connection") as connection:
                with self.subTest(identifier=identifier), self.assertRaises(MigrationError):
                    self.probe("admin", [], identifier)
                connection.assert_not_called()

    def test_certificate_pin_remains_enforced(self):
        with self.assertRaisesRegex(MigrationError, "unexpected TLS certificate"):
            self.probe("admin", [404], self.identifier, certificate=b"different-certificate")


class NativeReleaseProbeOrderingTests(unittest.TestCase):
    def test_live_binding_is_verified_before_probing_with_release_identity(self):
        config = customer_config()
        config["application"] = {"authentication": {"mode": "native", "adminUsername": "gateway-admin"}}
        config["contentAudit"] = {}
        edge = config["parameters"]["edge"]
        identifier = PrivateIngressProbeTests.identifier
        revision = "a" * 40
        report = {
            **edge, "phase": "canary", "environmentName": "test", "baseDomain": config["baseDomain"],
            "authenticationMode": "native", "auditMode": "native", "telemetryEnabled": False,
            "rateLimitPerMinute": 600, "adminRateLimitPerMinute": 120, "wafMode": "Detection",
            "frontDoorId": identifier, "changeTicket": "approved-test-change",
            "approvedBy": [identifier, "22222222-2222-4222-8222-222222222222"],
            "revision": revision, "configSha256": stage_fingerprint(config, 9),
        }
        azure = Mock()
        azure.run.return_value = {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}
        azure.scoped.return_value = {"state": "Succeeded", "edge": {"profileId": identifier}}
        order = []

        def binding(*args):
            order.append("binding")

        def backends(*args, **kwargs):
            order.append("probe")
            self.assertEqual(kwargs, {"front_door_id": identifier})
            raise MigrationError("stop before what-if")

        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, \
                patch("scripts.migration_deploy.validate_config"), \
                patch("scripts.migration_deploy.resolve_origin", return_value=config), \
                patch("scripts.migration_deploy.prepare", return_value=(Path("template"), Path("parameters"))), \
                patch("scripts.migration_runtime.connect_cluster", return_value=["kubectl", "--namespace", "litellm"]), \
                patch("scripts.audit_runtime.AuditCluster"), \
                patch("scripts.edge_binding.require_edge_binding", side_effect=binding) as verify_binding, \
                patch("scripts.private_ingress_runtime.require_private_ingress_backends", side_effect=backends) as probe:
            with self.assertRaisesRegex(MigrationError, "stop before what-if"):
                deploy_component(config, 9, "edge", revision, "plan", [], directory, azure=azure, release=report)
            self.assertEqual(order, ["binding", "probe"])
            probe.reset_mock()
            verify_binding.side_effect = MigrationError("stale binding")
            with self.assertRaisesRegex(MigrationError, "stale binding"):
                deploy_component(config, 9, "edge", revision, "plan", [], directory, azure=azure, release=report)
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
