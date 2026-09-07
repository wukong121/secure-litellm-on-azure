import unittest

from scripts.validate_public_config import violations


class PublicConfigTests(unittest.TestCase):
    def test_documentation_examples_and_role_ids_are_allowed(self):
        self.assertEqual(violations("owner@example.com admin@contoso.com owner@customer.invalid 7f951dda-4ed3-4680-a7ca-43fe172d538d"), set())

    def test_customer_email_and_private_identifiers_are_blocked(self):
        self.assertIn("non-example-email", violations("owner@" + "customer-business.test"))
        self.assertIn("literal-subscription-id", violations("/subscriptions/" + "12345678-1234-1234-1234-123456789abc"))

    def test_credential_signatures_are_reported_without_values(self):
        self.assertEqual(violations("AccountKey=" + "A" * 40), {"storage-key"})
        self.assertEqual(violations("sk-" + "B" * 30), {"api-key"})

    def test_synthetic_subscription_is_only_allowed_in_tests(self):
        value = "/subscriptions/" + "11111111-1111-4111-8111-111111111111"
        self.assertEqual(violations(value, synthetic_tests=True), set())
        self.assertEqual(violations(value), {"literal-subscription-id"})