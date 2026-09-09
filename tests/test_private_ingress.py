import unittest
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import yaml

from scripts.private_ingress import certificate_material, ingress_settings, render_ingress, source_ranges
from tests.test_customer_migration import customer_config


class PrivateIngressTests(unittest.TestCase):
    def setUp(self):
        self.config = customer_config()
        self.config["parameters"]["platform"]["stage4Network"]["ingressSubnetName"] = "snet-ingress"
        self.image = "registry.invalid/traefik@sha256:" + "a" * 64

    def test_private_planes_have_no_api_credentials_or_cross_plane_backend(self):
        for plane in ("api", "admin"):
            documents = render_ingress(self.config, plane, self.image, ["10.30.0.0/16"])
            objects = {document["kind"]: document for document in documents}
            self.assertFalse({"Secret", "Role", "ClusterRole"} & objects.keys())
            service = objects["Service"]
            self.assertEqual(service["metadata"]["annotations"]["service.beta.kubernetes.io/azure-load-balancer-internal"], "true")
            self.assertEqual([port["port"] for port in service["spec"]["ports"]], [443])
            self.assertEqual(service["spec"]["externalTrafficPolicy"], "Local")
            pod = objects["Deployment"]["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["containers"][0]["image"], self.image)
            self.assertNotIn("providers.kubernetes", str(pod))
            dynamic = yaml.safe_load(objects["ConfigMap"]["data"]["routes.yaml"])
            self.assertEqual(dynamic["http"]["services"][plane]["loadBalancer"]["servers"], [{"url": f"http://llm-{plane}-proxy.litellm.svc.cluster.local:8080"}])
            self.assertTrue(dynamic["tls"]["options"]["default"]["sniStrict"])
            backend = objects["NetworkPolicy"]["spec"]["egress"][1]["to"][0]
            self.assertEqual(backend["podSelector"]["matchLabels"]["plane"], plane)

    def test_public_and_missing_source_ranges_are_rejected(self):
        for ranges in ([], ["0.0.0.0/0"], ["8.8.8.0/24"], ["::/0"], ["10.30.1.1/24"]):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                source_ranges(ranges)

    def test_unpinned_image_is_rejected(self):
        with self.assertRaises(ValueError):
            render_ingress(self.config, "api", "traefik:latest", ["10.0.0.0/8"])

    def test_settings_reject_arbitrary_secret_hosts_and_public_sources(self):
        self.config["privateIngress"] = {plane: {"tlsSecretId": f"https://synthetic.vault.azure.net/secrets/{plane}-tls", "allowedCidrs": ["10.30.0.0/16"]} for plane in ("api", "admin")}
        ingress_settings(self.config)
        for url in ("http://synthetic.vault.azure.net/secrets/tls", "https://attacker.invalid/secrets/tls", "https://synthetic.vault.azure.net/secrets/tls?sig=secret"):
            self.config["privateIngress"]["api"]["tlsSecretId"] = url
            with self.assertRaises(ValueError):
                ingress_settings(self.config)

    def test_certificate_domain_key_and_expiry_are_validated(self):
        now = datetime.now(timezone.utc)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "llm-api.customer.invalid")])
        cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30)).add_extension(x509.SubjectAlternativeName([x509.DNSName("llm-api.customer.invalid")]), critical=False).sign(key, hashes.SHA256())
        pem = cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        material = certificate_material(pem.decode(), "llm-api.customer.invalid", now)
        self.assertEqual(len(material["sha256"]), 64)
        with self.assertRaises(ValueError):
            certificate_material(pem.decode(), "llm-api.customer.invalid", now + timedelta(days=25))
        with self.assertRaises(Exception):
            certificate_material(pem.decode(), "llm-admin.customer.invalid", now)