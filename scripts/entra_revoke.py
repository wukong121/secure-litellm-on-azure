"""Explicit disabled-subject directory revocation; never tenant-wide cleanup."""

import json
import os
import re
from urllib.parse import urlencode

from scripts.customer_migration import fingerprint, private_write, require, stage_fingerprint
from scripts.entra_access import GraphAccess
from scripts.entra_apps import discover, entra_settings, managed_definitions
from scripts.migration_deploy import AzureCommands
from scripts.proxy_config import entra_documents, object_id, proxy_settings


class GraphRevocation(GraphAccess):
    def __init__(self, credential):
        super().__init__(credential)
        self.delete_paths = set()

    def request(self, method, path, document=None):
        if method == "DELETE":
            require(document is None and path in self.delete_paths, "Graph deletion is outside the approved revocation plan")
            return self._send(method, path)
        require(method == "GET", "Revocation cannot create or modify grants")
        return super().request(method, path, document)


def revocation_state(config, applications, graph):
    disabled = {(binding["plane"], object_id(binding["oid"])) for binding in proxy_settings(config)["bindings"] if binding.get("disabled") is True}
    require(disabled, "Explicitly disable the affected binding before planning directory revocation")
    definitions = entra_documents(config)
    removals = []
    for plane in ("api", "admin"):
        resource = object_id(applications[plane]["servicePrincipalId"])
        role_ids = {role["id"] for role in definitions[plane]["appRoles"]}
        for item in graph.collection(f"/servicePrincipals/{resource}/appRoleAssignedTo?$select=id,principalId,resourceId,appRoleId"):
            require(object_id(item["resourceId"]) == resource, "Unexpected role resource")
            if (plane, object_id(item["principalId"])) in disabled:
                require(item["appRoleId"] in role_ids, "Disabled subject has an unrecognized role; separate review required")
                require(re.fullmatch(r"[A-Za-z0-9_-]{1,200}", item["id"]), "Invalid assignment ID")
                removals.append({"path": f"/servicePrincipals/{resource}/appRoleAssignedTo/{item['id']}", "before": item})
        for item in graph.collection("/oauth2PermissionGrants?" + urlencode({"$filter": "resourceId eq '" + resource + "'"})):
            require(item.get("resourceId") == resource, "Unexpected delegated consent resource")
            require(item.get("consentType") == "Principal" and item.get("principalId"), "Tenant-wide consent requires separate remediation")
            if (plane, object_id(item["principalId"])) in disabled:
                require(plane == "api" and item.get("scope") == "llm.invoke", "Broader consent cannot be silently removed")
                require(re.fullmatch(r"[A-Za-z0-9_-]{1,200}", item["id"]), "Invalid consent ID")
                removals.append({"path": "/oauth2PermissionGrants/" + item["id"], "before": item})
    return sorted(removals, key=lambda item: item["path"])


def revoke_access(config, operation, revision, directory, approved, graph):
    require(operation in {"plan", "execute"}, "Invalid revocation operation")
    found = discover(config, graph, managed_definitions(config))
    require(all(item and item["servicePrincipal"] for item in found.values()), "Initialize gateway applications first")
    applications = {plane: {"servicePrincipalId": item["servicePrincipal"]["id"]} for plane, item in found.items()}
    before = revocation_state(config, applications, graph)
    plan = {"stage": 7, "action": "entra-revoke", "revision": revision, "configSha256": stage_fingerprint(config, 7), "applications": applications, "remove": before}
    summary = {"stage": 7, "action": "entra-revoke", "planSha256": fingerprint(plan), "applied": False, "stageAccepted": False, "assignmentsToRemove": len(before), "existingTokensRevoked": False}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    if operation == "execute":
        require(approved == summary["planSha256"], "Revocation plan changed or was not approved")
        require(revocation_state(config, applications, graph) == before, "Directory grants changed before deletion")
        graph.delete_paths = {item["path"] for item in before}
        for item in before:
            remaining = revocation_state(config, applications, graph)
            require(item in remaining, "Revocation target changed; replan partial results")
            graph.request("DELETE", item["path"])
        require(not revocation_state(config, applications, graph), "Directory revocation is not yet observable; replan")
        summary["applied"] = True
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    print(json.dumps(summary))
    return summary


def initialize_entra_revoke(config, operation, revision, directory, approved):
    from azure.identity import AzureCliCredential
    azure = AzureCommands(config, directory)
    require(azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"]) == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope mismatch")
    credential = AzureCliCredential(tenant_id=config["azure"]["tenantId"])
    graph = GraphRevocation(credential)
    try:
        client = object_id(os.environ.get("AZURE_ENTRA_ACCESS_CLIENT_ID"))
        principals = graph.collection("/servicePrincipals?" + urlencode({"$filter": "appId eq '" + client + "'", "$select": "id,appId"}))
        require(len(principals) == 1 and object_id(principals[0]["id"]) == object_id(entra_settings(config).get("accessPrincipalId")), "Revocation identity differs from approved access principal")
        return revoke_access(config, operation, revision, directory, approved, graph)
    finally:
        graph.close()
        credential.close()