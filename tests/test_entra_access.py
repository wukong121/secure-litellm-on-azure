import unittest
import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from scripts.customer_migration import ROOT, fingerprint
from scripts.entra_access import GraphAccess, access_intent, grant_access, missing_access
from scripts.entra_apps import managed_definitions
from tests.test_proxy_config import APPS, proxy_customer


def access_fixture():
    config = proxy_customer()
    applications = {plane: {**value, "id": value["appId"], "servicePrincipalId": value["appId"]} for plane, value in APPS.items()}
    subjects = {item["oid"]: {"id": item["oid"], "type": "User", "accountEnabled": True} for item in config["proxy"]["bindings"]}
    client_id = config["proxy"]["apiClientIds"][0]
    clients = {client_id: {"id": "99999999-9999-4999-8999-999999999999", "appId": client_id, "accountEnabled": True}}
    return config, applications, subjects, clients


class MemoryAccessGraph(GraphAccess):
    def __init__(self, config, applications, subjects, clients):
        super().__init__(Mock())
        self.config = config
        self.subjects = subjects
        self.roles, self.grants, self.writes = [], [], []
        definitions = managed_definitions(config)
        self.apps = {plane: {**definitions[plane], "id": application["id"], "appId": application["appId"], "identifierUris": ["api://" + application["appId"]] if plane == "api" else []} for plane, application in applications.items()}
        self.principals = [{"id": application["servicePrincipalId"], "appId": application["appId"], "appOwnerOrganizationId": config["azure"]["tenantId"], "appRoleAssignmentRequired": True, "accountEnabled": True, "tags": definitions[plane]["tags"]} for plane, application in applications.items()] + list(clients.values())

    def _send(self, method, path, document=None):
        url = urlsplit(path)
        if method == "POST":
            self.writes.append((path, copy.deepcopy(document)))
            (self.grants if path == "/oauth2PermissionGrants" else self.roles).append(copy.deepcopy(document))
            return document
        if url.path.startswith("/users/"):
            return self.subjects[url.path.split("/")[2]]
        if url.path.endswith("/owners"):
            return {"value": [{"id": self.config["entra"]["bootstrapPrincipalId"]}]}
        if url.path.endswith("/appRoleAssignedTo"):
            return {"value": [item for item in self.roles if item["resourceId"] == url.path.split("/")[2]]}
        if url.path == "/oauth2PermissionGrants":
            resource = parse_qs(url.query)["$filter"][0].split("'")[1]
            return {"value": [item for item in self.grants if item["resourceId"] == resource]}
        target = parse_qs(url.query)["$filter"][0].split("'")[1]
        if url.path == "/applications":
            return {"value": [item for item in self.apps.values() if item["displayName"] == target]}
        return {"value": [item for item in self.principals if item["appId"] == target]}


class EntraAccessIntentTests(unittest.TestCase):
    def test_full_graph_read_write_flow_refuses_preexisting_tenant_wide_consent(self):
        config, apps, subjects, clients = access_fixture()
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        graph = MemoryAccessGraph(config, apps, subjects, clients)
        azure = Mock()
        azure.scoped.return_value = {"properties": {"provisioningState": "Succeeded"}}
        try:
            with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory:
                path = Path(directory)
                plan = grant_access(config, "plan", "a" * 40, path, "", graph, azure)
                self.assertFalse(graph.writes)
                result = grant_access(config, "execute", "a" * 40, path, plan["planSha256"], graph, azure)
                self.assertTrue(result["directoryVerified"])
                self.assertEqual(len(graph.roles), 2)
                self.assertEqual(len(graph.grants), 1)
                self.assertEqual(graph.grants[0]["consentType"], "Principal")
                graph.grants[0].update(consentType="AllPrincipals", principalId=None)
                with self.assertRaisesRegex(ValueError, "broader"):
                    grant_access(config, "plan", "a" * 40, path, "", graph, azure)
                self.assertEqual(len(graph.writes), 3)
        finally:
            graph.close()

    def test_broad_consent_or_other_roles_cannot_be_adopted(self):
        config, apps, subjects, clients = access_fixture()
        desired = access_intent(config, apps, subjects, clients)
        self.assertEqual(missing_access(desired, {"roles": [], "grants": []}), {key: sorted(values, key=fingerprint) for key, values in desired.items()})
        for extra in ({"consentType": "AllPrincipals", "principalId": None}, {"scope": "llm.invoke other.scope"}):
            before = {"roles": [], "grants": [{**desired["grants"][0], **extra}]}
            with self.assertRaisesRegex(ValueError, "broader"):
                missing_access(desired, before)

    def test_grant_execution_is_plan_bound_repeatable_and_not_login_acceptance(self):
        config, apps, subjects, clients = access_fixture()
        config["entra"] = {"bootstrapPrincipalId": "55555555-5555-4555-8555-555555555555"}
        found = {plane: {"application": {"id": value["id"], "appId": value["appId"]}, "servicePrincipal": {"id": value["servicePrincipalId"]}} for plane, value in apps.items()}
        state = {"roles": [], "grants": []}
        graph, azure = Mock(), Mock()
        azure.scoped.return_value = {"properties": {"provisioningState": "Succeeded"}}

        def post(method, path, document):
            self.assertEqual(method, "POST")
            state["grants" if path == "/oauth2PermissionGrants" else "roles"].append(copy.deepcopy(document))
            return {}

        graph.request.side_effect = post
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as directory, patch("scripts.entra_access.discover", return_value=found), patch("scripts.entra_access.directory_inputs", return_value=(subjects, clients)), patch("scripts.entra_access.current_access", side_effect=lambda *args: {key: sorted(copy.deepcopy(value), key=fingerprint) for key, value in state.items()}):
            path = Path(directory)
            plan = grant_access(config, "plan", "a" * 40, path, "", graph, azure)
            graph.request.assert_not_called()
            with self.assertRaisesRegex(ValueError, "plan changed"):
                grant_access(config, "execute", "a" * 40, path, "f" * 64, graph, azure)
            graph.request.assert_not_called()
            result = grant_access(config, "execute", "a" * 40, path, plan["planSha256"], graph, azure)
            self.assertTrue(result["directoryVerified"])
            self.assertFalse(result["loginVerified"])
            self.assertFalse(result["stageAccepted"])
            self.assertEqual(graph.request.call_count, 3)
            plan = grant_access(config, "plan", "a" * 40, path, "", graph, azure)
            grant_access(config, "execute", "a" * 40, path, plan["planSha256"], graph, azure)
            self.assertEqual(graph.request.call_count, 3)
            self.assertFalse(json.loads((path / "entra-access-receipt.json").read_text())["outputs"]["entraAccess"]["value"]["loginVerified"])

    def test_transport_restricts_writes_to_approved_payloads_and_pagination_scope(self):
        config, apps, subjects, clients = access_fixture()
        intent = access_intent(config, apps, subjects, clients)
        graph = GraphAccess(Mock())
        try:
            with patch.object(graph, "_send", return_value={}) as send:
                with self.assertRaises(ValueError):
                    graph.request("POST", "/oauth2PermissionGrants", intent["grants"][0])
                graph.approve(intent)
                graph.request("POST", "/oauth2PermissionGrants", intent["grants"][0])
                with self.assertRaises(ValueError):
                    graph.request("POST", "/oauth2PermissionGrants", {**intent["grants"][0], "consentType": "AllPrincipals"})
                with self.assertRaises(ValueError):
                    graph.request("DELETE", "/applications/" + apps["api"]["id"])
                self.assertEqual(send.call_count, 1)
            with patch.object(graph, "request", side_effect=[{"value": [{"id": "first"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?$skiptoken=next"}, {"value": [{"id": "second"}]}]):
                self.assertEqual(len(graph.collection("/oauth2PermissionGrants?$filter=synthetic")), 2)
            with patch.object(graph, "request", return_value={"value": [], "@odata.nextLink": "https://outside.invalid/v1.0/oauth2PermissionGrants"}), self.assertRaises(ValueError):
                graph.collection("/oauth2PermissionGrants?$filter=synthetic")
        finally:
            graph.close()

    def test_users_receive_only_specific_consent_and_plane_role(self):
        config, apps, subjects, clients = access_fixture()
        intent = access_intent(config, apps, subjects, clients)
        self.assertEqual(len(intent["roles"]), 2)
        self.assertEqual(intent["grants"], [{"clientId": next(iter(clients.values()))["id"], "consentType": "Principal", "principalId": config["proxy"]["bindings"][0]["oid"], "resourceId": apps["api"]["servicePrincipalId"], "scope": "llm.invoke"}])
        self.assertEqual({item["appRoleId"] for item in intent["roles"]}, {"81800000-0000-4000-8000-000000000006", "81800000-0000-4000-8000-000000000004"})

    def test_application_subject_gets_invoke_role_without_delegated_grant(self):
        config, apps, subjects, clients = access_fixture()
        client = next(iter(clients.values()))
        binding = config["proxy"]["bindings"][0]
        binding.update(oid=client["id"], principalType="ServicePrincipal")
        subjects[client["id"]] = {**client, "type": "ServicePrincipal"}
        intent = access_intent(config, apps, subjects, clients)
        self.assertFalse(intent["grants"])
        self.assertIn("81800000-0000-4000-8000-000000000002", [item["appRoleId"] for item in intent["roles"]])
        binding["plane"] = "admin"
        with self.assertRaises(ValueError):
            access_intent(config, apps, subjects, clients)

    def test_disabled_or_mismatched_directory_subject_is_rejected(self):
        config, apps, subjects, clients = access_fixture()
        subject = subjects[config["proxy"]["bindings"][0]["oid"]]
        subject["accountEnabled"] = False
        with self.assertRaises(ValueError):
            access_intent(config, apps, subjects, clients)