import unittest

from scripts.entra_revoke import GraphRevocation, revocation_state
from scripts.proxy_config import entra_documents
from tests.test_proxy_config import proxy_customer


class RevocationTests(unittest.TestCase):
    def test_only_disabled_principal_exact_grants_are_selected(self):
        config = proxy_customer()
        config["proxy"]["bindings"][0]["disabled"] = True
        oid = config["proxy"]["bindings"][0]["oid"]
        applications = {"api": {"servicePrincipalId": "11111111-1111-4111-8111-111111111111"}, "admin": {"servicePrincipalId": "22222222-2222-4222-8222-222222222222"}}
        resource = applications["api"]["servicePrincipalId"]
        role = next(item["id"] for item in entra_documents(config)["api"]["appRoles"] if item["value"] == "Llm.User")
        consent = {"id": "synthetic-consent", "resourceId": resource, "principalId": oid, "consentType": "Principal", "scope": "llm.invoke"}
        class Graph:
            def collection(self, path):
                if applications["admin"]["servicePrincipalId"] in path:
                    return []
                if "appRoleAssignedTo" in path:
                    return [{"id": "synthetic-role", "principalId": oid, "resourceId": resource, "appRoleId": role}, {"id": "other-user", "principalId": "99999999-9999-4999-8999-999999999999", "resourceId": resource, "appRoleId": role}]
                return [consent]
        selected = revocation_state(config, applications, Graph())
        self.assertEqual(len(selected), 2)
        self.assertNotIn("other-user", str(selected))
        consent["consentType"] = "AllPrincipals"
        with self.assertRaisesRegex(ValueError, "Tenant-wide"):
            revocation_state(config, applications, Graph())

    def test_revocation_transport_cannot_write_outside_plan(self):
        graph = GraphRevocation(None)
        try:
            for method, path in (("POST", "/oauth2PermissionGrants"), ("DELETE", "/oauth2PermissionGrants/unapproved")):
                with self.assertRaises(ValueError):
                    graph.request(method, path)
        finally:
            graph.close()