import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from scripts.certificate_runtime import AcmeEngine, TxtChallenge, certificate_settings, issue_api_certificate, new_key_and_csr, ACME_DIRECTORY
from scripts.customer_migration import stage_fingerprint
from tests.test_customer_migration import customer_config


class CertificateRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("acme"), "ACME library installed in isolated certificate test environment")
    def test_real_acme_order_serialization_and_completed_certificate_recovery(self):
        from acme import messages
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID
        host = "llm-api.synthetic.invalid"
        key_pem, csr = new_key_and_csr(host)
        authority = ACME_DIRECTORY.removesuffix("/directory")
        authorization = messages.AuthorizationResource(uri=authority + "/synthetic/auth", body=messages.Authorization(identifier=messages.Identifier(typ=messages.IDENTIFIER_FQDN, value=host), status=messages.STATUS_VALID, challenges=()))
        order = messages.OrderResource(uri=authority + "/synthetic/order", body=messages.Order(status=messages.STATUS_READY, authorizations=(authorization.uri,), identifiers=(authorization.body.identifier,), finalize=authority + "/synthetic/finalize"), authorizations=(authorization,), csr_pem=csr.encode())
        state = {"version": 1, "host": host, "directory": ACME_DIRECTORY, "key": key_pem, "csr": csr, "phase": "ordered", "order": json.loads(order.json_dumps())}
        restored = AcmeEngine.restore(None, state["order"])
        self.assertEqual(restored.csr_pem, csr.encode())
        self.assertEqual(restored.authorizations[0].body.identifier.value, host)
        key = serialization.load_pem_private_key(key_pem.encode(), None)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
        now = datetime.now(timezone.utc)
        certificate = x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=90)).add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False).sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()
        engine = Mock()
        engine.restore.return_value = restored
        engine.poll_and_finalize.return_value = SimpleNamespace(fullchain_pem=certificate)
        states = []
        challenge = Mock()
        pem = issue_api_certificate({"host": host}, state, lambda value: states.append(copy.deepcopy(value)), engine, challenge)
        self.assertEqual(pem, certificate + key_pem)
        engine.new_order.assert_not_called()
        challenge.change.assert_not_called()
        self.assertEqual(states[-1]["phase"], "issued")
        self.assertEqual(issue_api_certificate({"host": host}, states[-1], lambda *_: None, engine, challenge), pem)
        self.assertEqual(engine.poll_and_finalize.call_count, 1)
        engine.restore.return_value = restored.update(uri="https://untrusted.synthetic.invalid/order")
        with self.assertRaisesRegex(ValueError, "authority"):
            issue_api_certificate({"host": host}, state, lambda *_: None, engine, challenge)
        self.assertEqual(engine.poll_and_finalize.call_count, 1)

    def test_only_api_versionless_secret_and_explicit_ca_acceptance(self):
        config = customer_config()
        config["privateIngress"] = {"api": {"tlsSecretId": "https://synthetic.vault.azure.net/secrets/api-cert"}, "admin": {"tlsSecretId": "https://synthetic.vault.azure.net/secrets/admin-cert"}}
        config["certificates"] = {"zoneResourceId": "/subscriptions/" + config["azure"]["subscriptionId"] + "/resourceGroups/dns/providers/Microsoft.Network/dnsZones/" + config["baseDomain"], "termsAccepted": True, "publicApiHostnameAccepted": True}
        self.assertEqual(certificate_settings(config)["host"], "llm-api." + config["baseDomain"])
        previous = copy.deepcopy(config)
        previous.pop("certificates")
        self.assertEqual(stage_fingerprint(config, 3), stage_fingerprint(previous, 3))
        self.assertNotEqual(stage_fingerprint(config, 4), stage_fingerprint(previous, 4))
        config["certificates"]["termsAccepted"] = False
        with self.assertRaises(ValueError): certificate_settings(config)

    def test_txt_challenge_preserves_other_values_and_uses_etag(self):
        record = {"etag": "initial", "properties": {"TTL": 600, "TXTRecords": [{"value": ["unrelated"]}], "metadata": {"owner": "customer"}}}
        calls = []
        def arm(method, path, body, headers):
            calls.append((method, headers))
            if method == "GET": return copy.deepcopy(record)
            self.assertEqual(headers, {"If-Match": record["etag"]})
            record["properties"] = copy.deepcopy(body["properties"])
            record["etag"] = "updated"
        challenge = TxtChallenge(arm, "/synthetic/TXT/_acme-challenge", lambda *_: None)
        challenge.change("a" * 43)
        challenge.change("a" * 43, remove=True)
        self.assertEqual(record["properties"], {"TTL": 600, "TXTRecords": [{"value": ["unrelated"]}], "metadata": {"owner": "customer"}})

    def test_prepared_key_is_durable_before_order_and_uncertain_order_is_not_retried(self):
        states = []
        engine = Mock()
        engine.new_order.side_effect = ValueError("synthetic acknowledgement loss")
        with self.assertRaises(ValueError):
            issue_api_certificate({"host": "llm-api.synthetic.invalid"}, None, lambda value: states.append(copy.deepcopy(value)), engine, Mock())
        self.assertEqual([state["phase"] for state in states], ["prepared", "ordering"])
        with self.assertRaisesRegex(ValueError, "uncertain"):
            issue_api_certificate({"host": "llm-api.synthetic.invalid"}, states[-1], lambda *_: None, engine, Mock())
        self.assertEqual(engine.new_order.call_count, 1)

    def test_generated_csr_contains_only_api_name_and_valid_signature(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        key, csr = new_key_and_csr("llm-api.synthetic.invalid")
        request = x509.load_pem_x509_csr(csr.encode())
        self.assertTrue(request.is_signature_valid)
        self.assertEqual(request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName), ["llm-api.synthetic.invalid"])
        self.assertEqual(request.public_key().public_numbers(), serialization.load_pem_private_key(key.encode(), None).public_key().public_numbers())