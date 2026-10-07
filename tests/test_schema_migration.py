import hashlib
import json
import unittest
from unittest.mock import patch

from LiteLLM.runtime.azure_postgresql import DatabaseAuthError
from LiteLLM.runtime.schema_migration import check_history, state_fingerprint, unapproved_schema_diff


class SchemaMigrationTests(unittest.TestCase):
    def test_known_source_allows_interleaved_new_migrations_without_rewriting_history(self):
        assets = {"migrations": [{"name": name, "sha256": digest * 64} for name, digest in (("first", "a"), ("inserted", "b"), ("last", "c"))]}
        old = [{"name": "first", "sha256": "d" * 64}, {"name": "last", "sha256": "e" * 64}]
        digest = hashlib.sha256(json.dumps(old, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        history = [{"name": item["name"], "checksum": item["sha256"], "finished": True, "rolledBack": False} for item in old]
        with self.assertRaises(DatabaseAuthError):
            check_history(assets, history, ["existing"], "migration")
        with patch.dict("LiteLLM.runtime.schema_migration.SUPPORTED_SOURCE_HISTORIES", {digest: 2}, clear=True):
            self.assertEqual(check_history(assets, history, ["existing"], "migration"), ["inserted"])
            complete = history + [{"name": "inserted", "checksum": "b" * 64, "finished": True, "rolledBack": False}]
            self.assertEqual(check_history(assets, complete, ["existing"], "migration"), [])
            self.assertEqual([item["name"] for item in history], ["first", "last"])
            for invalid in (list(reversed(history)), [{**history[0], "checksum": "f" * 64}, history[1]], [{**complete[0]}, {**complete[1]}, {**complete[2], "checksum": "f" * 64}], complete + [complete[-1]]):
                with self.assertRaises(DatabaseAuthError):
                    check_history(assets, invalid, ["existing"], "migration")
            for invalid in ([history[0], history[0]], [history[0], {**history[1], "name": "unknown"}]):
                source = [{"name": item["name"], "sha256": item["checksum"]} for item in invalid]
                invalid_digest = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                with patch.dict("LiteLLM.runtime.schema_migration.SUPPORTED_SOURCE_HISTORIES", {invalid_digest: 2}, clear=True), self.assertRaises(DatabaseAuthError):
                    check_history(assets, invalid, ["existing"], "migration")
            with self.assertRaises(DatabaseAuthError):
                check_history(assets, history, ["existing"], "greenfield")

    def setUp(self):
        self.assets = {"migrations": [{"name": "first", "sha256": "a" * 64}, {"name": "second", "sha256": "b" * 64}]}
        self.record = {"name": "first", "checksum": "a" * 64, "finished": True, "rolledBack": False}

    def test_empty_new_database_and_compatible_restore(self):
        self.assertEqual(check_history(self.assets, [], [], "greenfield"), ["first", "second"])
        self.assertEqual(check_history(self.assets, [self.record], ["existing"], "migration"), ["second"])

    def test_unfinished_unknown_or_modified_history_fails_closed(self):
        for updates in ({"finished": False}, {"checksum": "c" * 64}, {"name": "other"}):
            with self.subTest(updates=updates), self.assertRaises(DatabaseAuthError):
                check_history(self.assets, [{**self.record, **updates}], ["existing"], "migration")
        with self.assertRaises(DatabaseAuthError):
            check_history(self.assets, [], ["existing"], "migration")
        with self.assertRaises(DatabaseAuthError):
            check_history(self.assets, [], [], "migration")
        with self.assertRaises(DatabaseAuthError):
            check_history(self.assets, [self.record, self.record], ["existing"], "migration")

    def test_state_fingerprint_binds_history_and_assets(self):
        first = state_fingerprint(self.assets, {"history": []})
        second = state_fingerprint(self.assets, {"history": [self.record]})
        self.assertNotEqual(first, second)

    def test_only_operator_managed_spend_log_indexes_are_allowed_drift(self):
        approved = '''
-- CreateIndex
CREATE INDEX "LiteLLM_SpendLogs_litellm_call_id_idx" ON "LiteLLM_SpendLogs"("litellm_call_id");
CREATE INDEX "LiteLLM_SpendLogs_api_key_startTime_idx" ON "public"."LiteLLM_SpendLogs"("api_key", "startTime");
'''
        self.assertEqual(unapproved_schema_diff(approved), "")
        for rejected in (
            'CREATE UNIQUE INDEX "LiteLLM_SpendLogs_litellm_call_id_idx" ON "LiteLLM_SpendLogs"("litellm_call_id");',
            'CREATE INDEX CONCURRENTLY "LiteLLM_SpendLogs_litellm_call_id_idx" ON "LiteLLM_SpendLogs"("litellm_call_id");',
            'create index "LiteLLM_SpendLogs_litellm_call_id_idx" on "LiteLLM_SpendLogs"("litellm_call_id");',
            'CREATE INDEX "LiteLLM_SpendLogs_litellm_call_id_idx" ON "LiteLLM_SpendLogs"("request_id");',
            'CREATE INDEX "LiteLLM_SpendLogs_litellm_call_id_idx" ON "litellm_spendlogs"("litellm_call_id");',
            'CREATE INDEX "other_idx" ON "LiteLLM_SpendLogs"("litellm_call_id");',
            'CREATE INDEX "LiteLLM_SpendLogs_litellm_call_id_idx" ON "LiteLLM_UserTable"("litellm_call_id");',
            'ALTER TABLE "LiteLLM_SpendLogs" ADD COLUMN "unexpected" TEXT;',
        ):
            with self.subTest(rejected=rejected):
                self.assertEqual(unapproved_schema_diff(rejected), rejected.removesuffix(";"))