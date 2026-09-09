"""Real Prisma migrations over TLS on isolated PostgreSQL; no Azure authentication claim."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import psycopg
from psycopg.rows import dict_row

from scripts.customer_migration import ROOT
from scripts.database_roles import grant_roles
from tests.test_azure_postgresql import TEMPLATE


LITELLM_IMAGE = "docker.litellm.ai/berriai/litellm@sha256:20b5044b619055374061a6d5b7b08754cad75aeabbf82ddf4f69cc0cf80ddaf4"
POSTGRES_IMAGE = "postgres@sha256:e17e86066e5ef83e0952a9347f5c792b7ece00972e2aa787a6986f471b3dd3d5"


@unittest.skipUnless(os.environ.get("RUN_AZURE_SCHEMA_CONTAINER_TESTS") == "1", "Explicit isolated Prisma/PostgreSQL integration opt-in required")
class AzureSchemaContainerTests(unittest.TestCase):
    def docker(self, *arguments):
        result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=240)
        if result.returncode:
            self.fail("Container test failed: " + result.stderr[-3000:] + result.stdout[-1000:])
        return result.stdout.strip()

    def test_new_database_migration_then_application_read_only_startup(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            root = Path(directory)
            root.chmod(0o755)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.postgres.database.azure.com")])
            now = datetime.now(timezone.utc)
            cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=2)).add_extension(x509.SubjectAlternativeName([x509.DNSName("synthetic.postgres.database.azure.com")]), critical=False).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256())
            (root / "tls.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            (root / "tls.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            (root / "token").write_text("synthetic-migration-password")
            (root / "config.yaml").write_text("model_list:\n  - model_name: coding\n    litellm_params:\n      model: openai/gpt-4o-mini\n      api_base: http://127.0.0.1:9\n      api_key: synthetic-model-key\ngeneral_settings:\n  disable_prisma_schema_update: true\n  disable_spend_logs: true\n")
            (root / "backend").mkdir()
            (root / "backend/LITELLM_MASTER_KEY").write_text("synthetic-application-master-key")
            (root / "backend/LITELLM_SALT_KEY").write_text("synthetic-application-salt-key")
            network = "llmgw-schema-" + uuid4().hex
            database_container = "llmgw-schema-" + uuid4().hex
            self.docker("network", "create", "--internal", network)
            try:
                startup = "cp /input/tls.crt /tmp/tls.crt && cp /input/tls.key /tmp/tls.key && chown postgres:postgres /tmp/tls.crt /tmp/tls.key && chmod 600 /tmp/tls.key && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/tls.crt -c ssl_key_file=/tmp/tls.key"
                self.docker("run", "--detach", "--name", database_container, "--network", network, "--network-alias", "synthetic.postgres.database.azure.com", "--env", "POSTGRES_PASSWORD=synthetic-admin-password", "--mount", f"type=bind,src={root},dst=/input,readonly", "--entrypoint", "sh", POSTGRES_IMAGE, "-c", startup)
                try:
                    inspect = json.loads(self.docker("inspect", database_container))[0]
                    self.assertFalse(inspect["HostConfig"]["PortBindings"])
                    address = inspect["NetworkSettings"]["Networks"][network]["IPAddress"]
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            admin = psycopg.connect(host=address, dbname="postgres", user="postgres", password="synthetic-admin-password", sslmode="require", connect_timeout=1, autocommit=True)
                            break
                        except psycopg.OperationalError:
                            if time.monotonic() > deadline:
                                self.fail("Isolated PostgreSQL startup failed")
                    with admin:
                        admin.execute("CREATE DATABASE litellm")
                        admin.execute("CREATE ROLE llmgw_migrator LOGIN PASSWORD 'synthetic-migration-password'")
                        admin.execute("CREATE ROLE llmgw_app LOGIN PASSWORD 'synthetic-application-password'")
                    with psycopg.connect(host=address, dbname="litellm", user="postgres", password="synthetic-admin-password", sslmode="require", row_factory=dict_row) as admin:
                        grant_roles(admin, [{"name": "llmgw_migrator"}, {"name": "llmgw_app"}], "litellm")
                    common = ["run", "--rm", "--network", network, "--read-only", "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,size=256m", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "LITELLM_LOCAL_MODEL_COST_MAP=True", "--env", "HOME=/tmp", "--env", "AZURE_DATABASE_URL_TEMPLATE=" + TEMPLATE.replace("llmgw_app", "llmgw_migrator"), "--env", "LLMGW_DATABASE_TOKEN_FILE=/input/token", "--mount", f"type=bind,src={ROOT},dst=/workspace,readonly", "--mount", f"type=bind,src={root},dst=/input,readonly", "--mount", f"type=bind,src={root / 'tls.crt'},dst=/etc/ssl/certs/ca-certificates.crt,readonly", "--workdir", "/workspace", "--entrypoint", "/app/.venv/bin/python", LITELLM_IMAGE]
                    report = json.loads(self.docker(*common, "-m", "LiteLLM.runtime.schema_migration", "--operation", "inspect"))
                    self.assertTrue(report["pending"])
                    outcome = json.loads(self.docker(*common, "-m", "LiteLLM.runtime.schema_migration", "--operation", "execute", "--expected-state", report["stateSha256"]))
                    self.assertTrue(outcome["schemaVerified"])
                    self.assertFalse(outcome["stageAccepted"])
                    after = json.loads(self.docker(*common, "-m", "LiteLLM.runtime.schema_migration", "--operation", "inspect"))
                    self.assertFalse(after["pending"])
                    self.docker(*common, "-m", "tests.azure_application_probe", "--config", "/input/config.yaml")
                finally:
                    self.docker("rm", "--force", "--volumes", database_container)
            finally:
                self.docker("network", "rm", network)