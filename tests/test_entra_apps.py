import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from scripts.customer_migration import ROOT
from scripts.entra_apps import GraphApplications, entra_settings, managed_definitions, provision_applications, verify_bootstrap_identity
from tests.test_proxy_config import APPS, proxy_customer


class FakeGraph:
    def __init__(self, config):
        self.config = config
        self.apps = {}
        self.principals = {}
        self.writes = []

    def collection(self, path):
        if "/owners?" in path:
            return [{"id": self.config["entra"]["bootstrapPrincipalId"]}]
        query = parse_qs(urlsplit(path).query)["$filter"][0]
        selected = query.split("'")[1]
        if path.startswith("/applications?"):
            return [copy.deepcopy(item) for item in self.apps.values() if item["displayName"] == selected]
        return [copy.deepcopy(item) for item in self.principals.values() if item["appId"] == selected]

    def request(self, method, path, document=None):
        self.writes.append((method, path, document))
        if path == "/applications":
            plane = "api" if "llmgw-plane-api" in document["tags"] else "admin"
            app = {**document, "id": APPS[plane]["appId"], "appId": APPS[plane]["appId"], "identifierUris": []}
            self.apps[app["id"]] = copy.deepcopy(app)
            return app
        if method == "PATCH":
            self.apps[path.split("/")[2]].update(document)
            return {}
        item = {**document, "id": document["appId"], "appOwnerOrganizationId": self.config["azure"]["tenantId"]}
        self.principals[item["id"]] = item
        return item


class EntraApplicationTests(unittest.TestCase):
    def test_graph_transport_rejects_redirects_without_revealing_body(self):
        credential = Mock()
        credential.get_token.return_value.token = "synthetic-access-token"
        graph = GraphApplications(credential)
        try:
            graph.session.request = Mock(return_value=Mock(status_code=302, content=b"synthetic-secret"))
            with self.assertRaisesRegex(ValueError, "HTTP 302") as caught:
                graph.request("POST", "/applications", {"displayName": "synthetic"})
            self.assertNotIn("synthetic-secret", str(caught.exception))
            self.assertFalse(graph.session.request.call_args.kwargs["allow_redirects"])
            with self.assertRaises(ValueError):
                graph.request("POST", "/oauth2PermissionGrants", {})
        finally:
            graph.close()

    def test_bootstrap_client_and_principal_must_match(self):
        config = proxy_customer()
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        graph = Mock()
        graph.collection.return_value = [{"id": config["entra"]["bootstrapPrincipalId"]}]
        with patch.dict("os.environ", {"AZURE_ENTRA_CLIENT_ID": "99999999-9999-4999-8999-999999999999"}):
            verify_bootstrap_identity(config, graph)
            graph.collection.return_value = [{"id": "88888888-8888-4888-8888-888888888888"}]
            with self.assertRaisesRegex(ValueError, "does not match"):
                verify_bootstrap_identity(config, graph)
        config["databaseAccess"] = {"migrationPrincipalId": config["entra"]["bootstrapPrincipalId"]}
        with self.assertRaisesRegex(ValueError, "must be distinct"):
            entra_settings(config)

    def test_plan_create_resume_and_no_implicit_consent(self):
        config = proxy_customer()
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        graph = FakeGraph(config)
        azure = Mock()
        azure.scoped.return_value = {"properties": {"provisioningState": "Succeeded"}}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
            path = Path(directory)
            plan = provision_applications(config, "plan", "a" * 40, path, "", graph, azure)
            self.assertFalse(graph.writes)
            with self.assertRaisesRegex(ValueError, "plan changed"):
                provision_applications(config, "execute", "a" * 40, path, "f" * 64, graph, azure)
            self.assertFalse(graph.writes)
            result = provision_applications(config, "execute", "a" * 40, path, plan["planSha256"], graph, azure)
            self.assertTrue(result["initialized"])
            self.assertFalse(result["clientAccessGranted"])
            self.assertTrue((path / "proxy-policy.json").exists())
            self.assertFalse(any("oauth2PermissionGrants" in call[1] or "appRoleAssignments" in call[1] for call in graph.writes))
            graph.writes.clear()
            plan = provision_applications(config, "plan", "a" * 40, path, "", graph, azure)
            provision_applications(config, "execute", "a" * 40, path, plan["planSha256"], graph, azure)
            self.assertFalse(graph.writes)
            graph.apps[APPS["api"]["appId"]]["appRoles"] = [item for item in graph.apps[APPS["api"]["appId"]]["appRoles"] if item["value"] != "Llm.User"]
            plan = provision_applications(config, "plan", "a" * 40, path, "", graph, azure)
            self.assertFalse(graph.writes)
            provision_applications(config, "execute", "a" * 40, path, plan["planSha256"], graph, azure)
            self.assertEqual(len(graph.writes), 1)
            self.assertEqual(graph.writes[0][0], "PATCH")
            self.assertEqual(set(graph.writes[0][2]), {"appRoles"})
            graph.apps[APPS["admin"]["appId"]]["web"]["redirectUris"] = ["https://unapproved.invalid/callback"]
            with self.assertRaisesRegex(ValueError, "automatic overwrite"):
                provision_applications(config, "plan", "a" * 40, path, "", graph, azure)

    def test_resource_group_separates_application_names(self):
        config = proxy_customer()
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        first = managed_definitions(config)
        config["target"]["resourceGroup"] = "different"
        self.assertNotEqual(first["api"]["displayName"], managed_definitions(config)["api"]["displayName"])