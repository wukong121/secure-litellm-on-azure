import json
import os
import subprocess
import time
import unittest
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from scripts.database_roles import grant_roles, verify_grants


@unittest.skipUnless(os.environ.get("RUN_DATABASE_ROLES_CONTAINER_TESTS") == "1", "Explicit isolated PostgreSQL test opt-in required")
class DatabaseRoleContainerTests(unittest.TestCase):
    def docker(self, *arguments):
        return subprocess.run(["docker", *arguments], capture_output=True, text=True, check=True, timeout=120).stdout.strip()

    def test_postgresql16_migrator_ddl_and_application_dml_are_separate(self):
        network = "llmgw-db-test-" + uuid4().hex
        container = "llmgw-db-test-" + uuid4().hex
        self.docker("network", "create", "--internal", network)
        try:
            image = "postgres@sha256:e17e86066e5ef83e0952a9347f5c792b7ece00972e2aa787a6986f471b3dd3d5"
            self.docker("run", "--detach", "--name", container, "--network", network, "--env", "POSTGRES_HOST_AUTH_METHOD=trust", image)
            try:
                details = json.loads(self.docker("inspect", container))[0]
                self.assertFalse(details["HostConfig"]["PortBindings"])
                address = details["NetworkSettings"]["Networks"][network]["IPAddress"]
                deadline = time.monotonic() + 30
                while True:
                    try:
                        connection = psycopg.connect(host=address, port=5432, dbname="postgres", user="postgres", sslmode="disable", connect_timeout=1, autocommit=True)
                        break
                    except psycopg.OperationalError:
                        if time.monotonic() >= deadline:
                            self.fail("Isolated PostgreSQL did not become ready")
                with connection:
                    connection.execute("CREATE DATABASE litellm")
                    connection.execute("CREATE ROLE llmgw_migrator LOGIN")
                    connection.execute("CREATE ROLE llmgw_app LOGIN")
                roles = [{"name": "llmgw_migrator"}, {"name": "llmgw_app"}]
                with psycopg.connect(host=address, dbname="litellm", user="postgres", sslmode="disable", row_factory=dict_row) as admin:
                    grant_roles(admin, roles, "litellm")
                    verify_grants(admin)
                with psycopg.connect(host=address, dbname="litellm", user="llmgw_migrator", sslmode="disable") as migrator:
                    migrator.execute("CREATE TABLE public.workflow_probe (id bigserial PRIMARY KEY, value text NOT NULL)")
                with psycopg.connect(host=address, dbname="litellm", user="llmgw_app", sslmode="disable", autocommit=True) as application:
                    application.execute("INSERT INTO public.workflow_probe (value) VALUES (%s)", ("synthetic",))
                    self.assertEqual(application.execute("SELECT value FROM public.workflow_probe").fetchone()[0], "synthetic")
                    for statement in ("CREATE TABLE public.forbidden (id int)", "DROP TABLE public.workflow_probe"):
                        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                            application.execute(statement)
            finally:
                self.docker("rm", "--force", "--volumes", container)
        finally:
            self.docker("network", "rm", network)