import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from LiteLLM.runtime.azure_postgresql import AzureDatabaseTokens, DATABASE_SCOPE, DatabaseAuthError, azure_wrapper_type, database_url_template, verify_prisma_source, workload_identity_tokens


TEMPLATE = urlunsplit((
    "postgresql", "@".join(("llmgw_app", "synthetic.postgres.database.azure.com:5432")), "/litellm",
    "sslmode=require&sslaccept=strict&sslcert=%2Fetc%2Fssl%2Fcerts%2Fca-certificates.crt&schema=public&connection_limit=5", "",
))


class AzurePostgresqlTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=1000)
        self.credential = Mock()
        self.tokens = AzureDatabaseTokens(TEMPLATE, self.credential, self.clock)

    def test_refresh_changes_only_password_and_uses_sdk_expiry(self):
        self.credential.get_token.side_effect = [SimpleNamespace(token="synthetic:@/?first", expires_on=4600), SimpleNamespace(token="synthetic-second", expires_on=8200)]
        first = self.tokens.new_url()
        self.clock.return_value = 4400
        second = self.tokens.new_url()
        for value in (first, second):
            actual = urlsplit(value)
            self.assertEqual(actual.hostname, urlsplit(TEMPLATE).hostname)
            self.assertEqual(actual.path, "/litellm")
            self.assertEqual(parse_qs(actual.query), parse_qs(urlsplit(TEMPLATE).query))
        self.assertEqual(unquote(urlsplit(first).password), "synthetic:@/?first")
        self.assertNotEqual(first, second)
        self.assertEqual(self.tokens.expires_at("synthetic-second"), datetime.fromtimestamp(8200, timezone.utc).replace(tzinfo=None))
        self.assertIsNone(self.tokens.expires_at("unknown-token"))
        self.credential.get_token.assert_called_with(DATABASE_SCOPE)
        self.assertNotIn("synthetic-second", str(self.tokens._expirations))

    def test_invalid_or_failed_tokens_never_include_credentials_in_error(self):
        for response in (SimpleNamespace(token="private-value", expires_on=1200), SimpleNamespace(token="", expires_on=9000)):
            self.credential.get_token.return_value = response
            with self.assertRaises(DatabaseAuthError) as caught:
                self.tokens.new_url()
            self.assertNotIn("private-value", str(caught.exception))
        self.credential.get_token.side_effect = RuntimeError("sensitive-provider-error")
        with self.assertRaises(DatabaseAuthError) as caught:
            self.tokens.new_url()
        self.assertNotIn("sensitive-provider-error", str(caught.exception))

    def test_rejects_insecure_redirecting_and_password_templates(self):
        for value in (TEMPLATE.replace("sslaccept=strict", "sslaccept=accept_invalid_certs"), TEMPLATE.replace("sslmode=require", "sslmode=disable"), TEMPLATE.replace("llmgw_app@", "llmgw_app:stored-secret@"), TEMPLATE.replace("synthetic.postgres.database.azure.com", "localhost"), TEMPLATE.replace("llmgw_app", "postgres"), TEMPLATE + "&sslmode=disable", TEMPLATE + "#fragment"):
            with self.subTest(value=value), self.assertRaises(DatabaseAuthError):
                database_url_template(value)

    def test_wrapper_updates_credentials_without_replacing_pool_logic(self):
        class Base:
            iam_token_db_auth = True
            _db_url_env_var = "DATABASE_URL"
            _iam_endpoint = None

            def retained_pool_method(self):
                return "upstream"

        wrapper = azure_wrapper_type(Base, self.tokens)()
        self.credential.get_token.return_value = SimpleNamespace(token="synthetic", expires_on=4600)
        with patch.dict(os.environ, {}, clear=True):
            value = wrapper.get_rds_iam_token()
            self.assertEqual(os.environ["DATABASE_URL"], value)
            self.assertEqual(wrapper.retained_pool_method(), "upstream")
            self.assertIsNotNone(wrapper._parse_token_expiration("synthetic"))
            wrapper._db_url_env_var = "DATABASE_URL_READ_REPLICA"
            with self.assertRaises(DatabaseAuthError):
                wrapper.get_rds_iam_token()

    def test_workload_identity_has_no_developer_credential_fallback(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(DatabaseAuthError):
            workload_identity_tokens(TEMPLATE)

    def test_upstream_source_drift_requires_adapter_revalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "prisma_client.py"
            source.write_text("unreviewed source\n")
            with self.assertRaisesRegex(DatabaseAuthError, "reviewed image"):
                verify_prisma_source(SimpleNamespace(__file__=str(source)))