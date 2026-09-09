import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

from scripts.dns_runtime import DnsClient, change_dns, dns_settings
from tests.test_customer_migration import customer_config


class DnsTests(unittest.TestCase):
    def test_wrong_tenant_is_rejected_before_dns_reads_or_writes(self):
        azure = Mock()
        azure.run.return_value = {"tenantId": "other", "id": "other"}
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "scope mismatch"):
            DnsClient(customer_config(), Path(directory), azure)
        azure.scoped.assert_not_called()
    def test_checkpoint_precedes_write_and_rollback_restores_exact_record(self):
        config = customer_config()
        config["dns"] = {"zoneResourceId": "/subscriptions/" + config["azure"]["subscriptionId"] + "/resourceGroups/dns/providers/Microsoft.Network/dnsZones/" + config["baseDomain"], "ttl": 300}
        original = {"etag": "before", "properties": {"TTL": 600, "CNAMERecord": {"cname": "legacy.synthetic.invalid"}, "metadata": {"owner": "customer"}}}
        class Client:
            record = copy.deepcopy(original)
            saved = None
            def read(self): return copy.deepcopy(self.record)
            def checkpoint(self): return copy.deepcopy(self.saved)
            def save(self, value): self.saved = copy.deepcopy(value)
            def write(self, desired, previous):
                assert self.saved and self.record == previous
                self.record = {"etag": "next", "properties": copy.deepcopy(desired)} if desired else None
        client = Client()
        edge = {"apiHost": "llm-api." + config["baseDomain"], "trafficEnabled": True, "endpointHost": "synthetic.azurefd.net"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            plan = change_dns(config, "dns-publish", "plan", "a" * 40, path, "", client=client, edge=edge)
            self.assertIsNone(client.saved)
            change_dns(config, "dns-publish", "execute", "a" * 40, path, plan["planSha256"], client=client, edge=edge)
            rollback = change_dns(config, "dns-rollback", "plan", "a" * 40, path, "", client=client, edge=edge)
            change_dns(config, "dns-rollback", "execute", "a" * 40, path, rollback["planSha256"], client=client, edge=edge)
            self.assertEqual(client.record["properties"], original["properties"])
            self.assertEqual(client.saved["phase"], "rolled-back")
            retry = change_dns(config, "dns-rollback", "plan", "a" * 40, path, "", client=client, edge=edge)
            change_dns(config, "dns-rollback", "execute", "a" * 40, path, retry["planSha256"], client=client, edge=edge)
            client.record["properties"]["TTL"] = 99
            with self.assertRaisesRegex(ValueError, "modified externally"):
                change_dns(config, "dns-rollback", "plan", "a" * 40, path, "", client=client, edge=edge)

    def test_dns_scope_never_targets_admin_or_another_subscription(self):
        config = customer_config()
        config["dns"] = {"zoneResourceId": "/subscriptions/" + config["azure"]["subscriptionId"] + "/resourceGroups/dns/providers/Microsoft.Network/dnsZones/" + config["baseDomain"], "ttl": 300}
        self.assertEqual(dns_settings(config)["record"], "llm-api")
        config["dns"]["zoneResourceId"] += ".untrusted.invalid"
        with self.assertRaises(ValueError): dns_settings(config)