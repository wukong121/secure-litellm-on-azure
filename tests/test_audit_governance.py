from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from scripts.audit_governance import apply_governance, governance_plan
from tests.test_proxy_config import proxy_customer


class AuditGovernanceTests(unittest.TestCase):
    def test_actual_independent_approvals_and_registry_compare_before_write(self):
        config = proxy_customer()
        actor = "11111111-1111-4111-8111-111111111111"
        config["proxy"]["bindings"].append({"oid": actor, "plane": "admin", "role": "audit_reader", "models": []})
        config["auditGovernance"] = {"reviewers": {"reviewer-one": "22222222-2222-4222-8222-222222222222", "reviewer-two": "33333333-3333-4333-8333-333333333333"}}
        now = datetime.now(timezone.utc)
        request = {"action": "grant", "id": "44444444-4444-4444-8444-444444444444", "actorOid": actor, "ticketId": "SYNTHETIC", "reason": "Synthetic approved investigation", "teamIds": ["coding-team"], "from": (now - timedelta(hours=1)).isoformat(), "to": now.isoformat(), "validUntil": (now + timedelta(hours=1)).isoformat()}
        resource = {"metadata": {"uid": "registry"}, "data": {"approvals.json": "[]", "holds.json": "[]"}}
        plan = governance_plan(config, request, resource, "a" * 40, now)
        approvals = [{"state": "approved", "user": {"login": login}, "environments": [{"name": config["environment"] + "-audit-approval-" + str(index)}]} for index, login in enumerate(("reviewer-one", "reviewer-two"), 1)]
        desired, reviewers = apply_governance(config, plan, resource, approvals, now)
        self.assertEqual(json.loads(desired["approvals.json"])[0]["approvedBy"], reviewers)
        self.assertEqual(resource["data"]["approvals.json"], "[]")
        with patch.dict("os.environ", {"GITHUB_ACTOR": "reviewer-one"}), self.assertRaisesRegex(ValueError, "requester"):
            apply_governance(config, plan, resource, approvals, now)
        with self.assertRaises(ValueError):
            apply_governance(config, plan, resource, approvals[:1], now)
        approvals[1]["user"]["login"] = "reviewer-one"
        with self.assertRaisesRegex(ValueError, "same person"):
            apply_governance(config, plan, resource, approvals, now)
        resource["data"]["holds.json"] = '[{"id":"different"}]'
        with self.assertRaisesRegex(ValueError, "changed"):
            apply_governance(config, plan, resource, approvals, now)

    def test_hold_scope_cannot_be_wildcard_or_unbounded(self):
        config = proxy_customer()
        config["auditGovernance"] = {"reviewers": {"first": "22222222-2222-4222-8222-222222222222", "second": "33333333-3333-4333-8333-333333333333"}}
        resource = {"metadata": {"uid": "registry"}, "data": {"approvals.json": "[]", "holds.json": "[]"}}
        request = {"action": "hold", "id": "*", "actorOid": "11111111-1111-4111-8111-111111111111", "ticketId": "synthetic", "reason": "Synthetic investigation", "until": "2099-01-01T00:00:00Z"}
        with self.assertRaises(ValueError):
            governance_plan(config, request, resource, "a" * 40)