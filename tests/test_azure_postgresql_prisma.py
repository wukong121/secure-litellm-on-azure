"""Contract tests run inside the locked LiteLLM image, without cloud or database access."""

import asyncio
import os
import time
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

from LiteLLM.runtime.azure_postgresql import AzureDatabaseTokens, DatabaseAuthError, azure_wrapper_type, install_prisma_adapter, verify_prisma_source
from tests.test_azure_postgresql import TEMPLATE


@unittest.skipUnless(os.environ.get("RUN_AZURE_PRISMA_CONTRACT_TESTS") == "1", "Run inside the approved LiteLLM image")
class AzurePrismaContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from litellm.proxy.db import prisma_client

        verify_prisma_source(prisma_client)
        self.credential = Mock()
        self.tokens = AzureDatabaseTokens(TEMPLATE, self.credential)
        self.original = SimpleNamespace()
        self.wrapper = azure_wrapper_type(prisma_client.PrismaWrapper, self.tokens)(self.original, True)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_real_wrapper_schedules_refresh_from_azure_sdk_expiry(self):
        expires = int(time.time()) + 3600
        self.credential.get_token.return_value = SimpleNamespace(token="synthetic-token", expires_on=expires)
        current = self.wrapper.get_rds_iam_token()
        self.assertFalse(self.wrapper.is_token_expired(current))
        delay = self.wrapper._calculate_seconds_until_refresh()
        self.assertGreater(delay, 3400)
        self.assertLessEqual(delay, 3420)
        self.assertIsNone(self.wrapper._parse_token_expiration("unissued-token"))
        self.assertEqual(self.wrapper._parse_token_expiration("synthetic-token"), datetime.fromtimestamp(expires, timezone.utc).replace(tzinfo=None))
        self.assertTrue(self.wrapper.is_token_expired(TEMPLATE))

    async def test_concurrent_refresh_coalesces_and_keeps_tls(self):
        self.credential.get_token.return_value = SimpleNamespace(token="synthetic-new-token", expires_on=int(time.time()) + 3600)
        self.wrapper._replace_prisma_client_for_token_refresh_locked = AsyncMock()
        os.environ["DATABASE_URL"] = TEMPLATE
        await asyncio.gather(*(self.wrapper._safe_refresh_token() for _attempt in range(8)))
        self.wrapper._replace_prisma_client_for_token_refresh_locked.assert_awaited_once()
        self.credential.get_token.assert_called_once()
        passed = self.wrapper._replace_prisma_client_for_token_refresh_locked.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(passed).query), parse_qs(urlsplit(TEMPLATE).query))

    async def test_failed_connection_keeps_previous_url_and_original_client(self):
        self.credential.get_token.return_value = SimpleNamespace(token="synthetic-new-token", expires_on=int(time.time()) + 3600)
        self.wrapper._replace_prisma_client_for_token_refresh_locked = AsyncMock(side_effect=ConnectionError("synthetic unavailable database"))
        os.environ["DATABASE_URL"] = TEMPLATE
        with self.assertRaises(ConnectionError):
            await self.wrapper._safe_refresh_token()
        self.assertEqual(os.environ["DATABASE_URL"], TEMPLATE)
        self.assertIs(self.wrapper._original_prisma, self.original)
        self.assertEqual(self.wrapper.engine_generation, 0)

    async def test_background_task_can_start_and_stop_without_busy_refresh(self):
        self.credential.get_token.return_value = SimpleNamespace(token="synthetic-new-token", expires_on=int(time.time()) + 3600)
        self.wrapper.get_rds_iam_token()
        self.wrapper._replace_prisma_client_for_token_refresh_locked = AsyncMock()
        await self.wrapper.start_token_refresh_task()
        self.assertIsNotNone(self.wrapper._token_refresh_task)
        await self.wrapper.stop_token_refresh_task()
        self.assertIsNone(self.wrapper._token_refresh_task)
        self.wrapper._replace_prisma_client_for_token_refresh_locked.assert_not_called()

    async def test_token_acquisition_does_not_block_event_loop(self):
        loop_thread = threading.get_ident()
        acquisition_threads = []

        def token(_scope):
            acquisition_threads.append(threading.get_ident())
            return SimpleNamespace(token="synthetic-off-loop", expires_on=int(time.time()) + 3600)

        self.credential.get_token.side_effect = token
        self.wrapper._replace_prisma_client_for_token_refresh_locked = AsyncMock()
        os.environ["DATABASE_URL"] = TEMPLATE
        await self.wrapper._safe_refresh_token()
        self.assertTrue(acquisition_threads)
        self.assertNotIn(loop_thread, acquisition_threads)

    async def test_cancelled_connection_swap_restores_previous_url(self):
        self.credential.get_token.return_value = SimpleNamespace(token="synthetic-new-token", expires_on=int(time.time()) + 3600)
        self.wrapper._replace_prisma_client_for_token_refresh_locked = AsyncMock(side_effect=asyncio.CancelledError())
        os.environ["DATABASE_URL"] = TEMPLATE
        with self.assertRaises(asyncio.CancelledError):
            await self.wrapper._safe_refresh_token()
        self.assertEqual(os.environ["DATABASE_URL"], TEMPLATE)
        self.assertIs(self.wrapper._original_prisma, self.original)

    async def test_installed_startup_settings_never_call_aws_or_drop_tls(self):
        from litellm.proxy.db import db_url_settings, prisma_client
        from litellm.proxy.auth import rds_iam_token

        self.credential.get_token.return_value = SimpleNamespace(token="synthetic-startup", expires_on=int(time.time()) + 3600)
        with patch.object(prisma_client, "PrismaWrapper", prisma_client.PrismaWrapper), patch.object(prisma_client, "_llmgw_azure_installed", False, create=True), patch.object(db_url_settings.DatabaseURLSettings, "build_writer_url", db_url_settings.DatabaseURLSettings.build_writer_url), patch.object(db_url_settings.DatabaseURLSettings, "build_reader_url", db_url_settings.DatabaseURLSettings.build_reader_url), patch.object(rds_iam_token, "generate_iam_auth_token") as aws:
            install_prisma_adapter(self.tokens)
            settings = db_url_settings.DatabaseURLSettings()
            self.assertTrue(settings.apply_to_env())
            self.assertEqual(parse_qs(urlsplit(os.environ["DATABASE_URL"]).query), parse_qs(urlsplit(TEMPLATE).query))
            self.assertEqual(os.environ["IAM_TOKEN_DB_AUTH"], "True")
            installed = prisma_client.PrismaWrapper(self.original, True)
            self.assertIsNotNone(installed._parse_token_expiration("synthetic-startup"))
            aws.assert_not_called()
            with self.assertRaises(DatabaseAuthError):
                install_prisma_adapter(self.tokens)

    async def test_install_rejects_alternate_database_and_late_import(self):
        import sys

        os.environ["DIRECT_URL"] = "unapproved"
        with self.assertRaisesRegex(DatabaseAuthError, "alternate database"):
            install_prisma_adapter(self.tokens)
        os.environ.pop("DIRECT_URL")
        with patch.dict(sys.modules, {"litellm.proxy.proxy_server": Mock()}), self.assertRaisesRegex(DatabaseAuthError, "before loading"):
            install_prisma_adapter(self.tokens)