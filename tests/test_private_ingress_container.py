import json
import os
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import yaml

from scripts.customer_migration import ROOT, private_write
from scripts.private_ingress import certificate_material, render_ingress
from scripts.private_ingress_runtime import verify_endpoint
from tests.test_private_ingress_runtime import ingress_config


@unittest.skipUnless(os.environ.get("RUN_PRIVATE_INGRESS_CONTAINER_TESTS") == "1", "Explicit local Docker test opt-in required")
class PrivateIngressContainerTests(unittest.TestCase):
    def docker(self, *arguments):
        result = subprocess.run(["docker", *arguments], check=True, capture_output=True, text=True, timeout=120)
        return result.stdout.strip()

    def test_locked_image_loads_rendered_config_and_isolates_tls_hosts(self):
        config = ingress_config()
        lock = json.loads((ROOT / "deploy/private-ingress-image.json").read_text())
        image = lock["source"] + "@" + lock["digest"]
        network = "llmgw-test-" + uuid4().hex
        self.docker("network", "create", "--internal", network)
        self.addCleanup(self.docker, "network", "rm", network)
        for plane, other in (("api", "admin"), ("admin", "api")):
            with self.subTest(plane=plane), tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
                root = Path(directory)
                root.chmod(0o755)
                (root / "dynamic").mkdir(mode=0o755)
                (root / "certs").mkdir(mode=0o755)
                host = f"llm-{plane}.customer.invalid"
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
                now = datetime.now(timezone.utc)
                certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30)).add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False).sign(key, hashes.SHA256())
                certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
                private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
                material = certificate_material(certificate_pem + private_pem, host)
                objects = {document["kind"]: document for document in render_ingress(config, plane, image, ["10.30.0.0/16"])}
                files = {root / "dynamic/routes.yaml": objects["ConfigMap"]["data"]["routes.yaml"], root / "certs/tls.crt": certificate_pem, root / "certs/tls.key": private_pem}
                for path, value in files.items():
                    private_write(path, value)
                    path.chmod(0o644)
                container = "llmgw-test-" + uuid4().hex
                arguments = objects["Deployment"]["spec"]["template"]["spec"]["containers"][0]["args"]
                self.docker("run", "--detach", "--name", container, "--network", network, "--user", "65532:65532", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--mount", f"type=bind,src={root / 'dynamic'},dst=/dynamic,readonly", "--mount", f"type=bind,src={root / 'certs'},dst=/certs,readonly", image, *arguments)
                try:
                    inspected = json.loads(self.docker("inspect", container))[0]
                    self.assertFalse(inspected["HostConfig"]["PortBindings"])
                    address = (inspected["NetworkSettings"]["Networks"][network]["IPAddress"], 8443)
                    context = ssl.create_default_context(cadata=certificate_pem)
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            with socket.create_connection(address, timeout=1) as connection, context.wrap_socket(connection, server_hostname=host):
                                break
                        except (OSError, ssl.SSLError):
                            if time.monotonic() >= deadline:
                                self.fail("Traefik did not become TLS-ready: " + self.docker("logs", container))
                    original_connection = socket.create_connection
                    with patch("scripts.private_ingress_runtime.socket.create_connection", side_effect=lambda _address, timeout: original_connection(address, timeout=timeout)), patch("scripts.private_ingress_runtime.ssl.create_default_context", return_value=context):
                        verify_endpoint(address[0], host, f"llm-{other}.customer.invalid", material["sha256"])
                    with socket.create_connection(address, timeout=5) as connection, self.assertRaises(ssl.SSLError):
                        context.wrap_socket(connection, server_hostname=f"llm-{other}.customer.invalid")
                    routes = yaml.safe_load(files[root / "dynamic/routes.yaml"])
                    self.assertEqual(set(routes["http"]["routers"]), {plane})
                finally:
                    self.docker("rm", "--force", container)