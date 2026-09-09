import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from LiteLLM.runtime.application import REQUIRED_TABLES, REQUIRED_VIEWS, application_config, check_application_role, check_schema
from LiteLLM.runtime.azure_postgresql import DatabaseAuthError
from scripts.customer_migration import ROOT


class AzureApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_role_cannot_write_migration_history(self):
        client = AsyncMock()
        permitted = {"name": "llmgw_app", "ddl": False, "migration_write": False}
        client.query_raw.return_value = [permitted]
        await check_application_role(client)
        for changes in ({"name": "llmgw_migrator"}, {"ddl": True}, {"migration_write": True}):
            client.query_raw.return_value = [{**permitted, **changes}]
            with self.assertRaises(DatabaseAuthError):
                await check_application_role(client)

    async def test_image_uses_dedicated_nonroot_no_migration_entrypoint(self):
        recipe = (ROOT / "LiteLLM/runtime/Dockerfile").read_text()
        self.assertIn("USER 10001:10001", recipe)
        self.assertIn('"LiteLLM.runtime.application"', recipe)
        self.assertNotIn("prisma_migration.py", recipe)
        self.assertTrue((ROOT / "LiteLLM/runtime/Dockerfile.dockerignore").read_text().startswith("**\n"))

    async def test_schema_check_is_read_only_and_rejects_missing_or_failed_migrations(self):
        tables = [{"name": name} for name in REQUIRED_TABLES]
        views = [{"name": name} for name in REQUIRED_VIEWS]
        client = AsyncMock()
        client.query_raw.side_effect = [tables, views, [{"count": 0}]]
        await check_schema(client)
        self.assertTrue(all(call.args[0].startswith("SELECT ") for call in client.query_raw.call_args_list))
        for response in ([[], views], [tables, []], [tables, views, [{"count": 1}]]):
            client.query_raw.side_effect = response
            with self.assertRaises(DatabaseAuthError):
                await check_schema(client)

    async def test_config_requires_no_ddl_and_no_database_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("general_settings:\n  disable_prisma_schema_update: true\n")
            application_config(config)
            for text in ("general_settings: {}", "general_settings:\n  disable_prisma_schema_update: false\n", "environment_variables: {}\ngeneral_settings:\n  disable_prisma_schema_update: true\n", "general_settings:\n  disable_prisma_schema_update: true\n  database_url: other\n"):
                config.write_text(text)
                with self.assertRaises(DatabaseAuthError):
                    application_config(config)