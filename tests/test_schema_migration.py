import unittest

from LiteLLM.runtime.azure_postgresql import DatabaseAuthError
from LiteLLM.runtime.schema_migration import check_history, state_fingerprint


class SchemaMigrationTests(unittest.TestCase):
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