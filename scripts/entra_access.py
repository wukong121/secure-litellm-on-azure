"""Generate and apply explicit user consent and application role assignments."""

import json
import os
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

from scripts.customer_migration import MigrationError, fingerprint, private_write, require, stage_fingerprint
from scripts.entra_apps import GraphApplications, discover, entra_settings, managed_definitions
from scripts.migration_deploy import AzureCommands, deployment_name
from scripts.proxy_config import entra_documents, object_id, proxy_settings


def access_intent(config, applications, subjects, clients):
    settings = proxy_settings(config)
    definitions = entra_documents(config)
    roles, grants = [], []
    resources = {plane: object_id(applications[plane]["servicePrincipalId"]) for plane in ("api", "admin")}
    require(resources["api"] != resources["admin"], "Entra resources must be distinct")
    for binding in settings["bindings"]:
        if binding.get("disabled", False):
            continue
        principal_id = object_id(binding["oid"])
        subject = subjects[principal_id]
        kind = binding.get("principalType", "User")
        require(subject["type"] == kind and object_id(subject["id"]) == principal_id and subject["accountEnabled"] is True, "Subject type, ID or enabled state differs from the approved binding")
        plane = binding["plane"]
        if plane == "admin":
            role_value = binding["role"]
        elif kind == "ServicePrincipal":
            app_id = object_id(subject["appId"])
            require(app_id in clients and object_id(clients[app_id]["id"]) == principal_id, "API application subject must match an approved calling client")
            role_value = "Llm.Invoke"
        else:
            role_value = "Llm.User"
            for client_id in binding.get("clientIds", settings["apiClientIds"]):
                client = clients[object_id(client_id)]
                require(client["accountEnabled"] is True and object_id(client["appId"]) == object_id(client_id), "Delegated client is disabled or has mismatched ID")
                grants.append({"clientId": object_id(client["id"]), "consentType": "Principal", "principalId": principal_id, "resourceId": resources["api"], "scope": "llm.invoke"})
        role = next(item for item in definitions[plane]["appRoles"] if item["value"] == role_value)
        require(("Application" if kind == "ServicePrincipal" else "User") in role["allowedMemberTypes"], "Role cannot be assigned to this principal type")
        roles.append({"principalId": principal_id, "resourceId": resources[plane], "appRoleId": role["id"]})
    return {"roles": sorted(roles, key=lambda item: (item["resourceId"], item["principalId"])), "grants": sorted(grants, key=lambda item: (item["clientId"], item["principalId"]))}


class GraphAccess(GraphApplications):
    def __init__(self, credential):
        super().__init__(credential)
        self.approved = set()

    def request(self, method, path, document=None):
        if method == "GET":
            require(path.startswith(("/applications?", "/applications/", "/servicePrincipals?", "/servicePrincipals/", "/users/", "/oauth2PermissionGrants?")) and ".." not in path and "#" not in path, "Graph read outside access review scope")
        else:
            require(method == "POST" and (path, fingerprint(document)) in self.approved, "Graph write is not in the approved access plan")
        return self._send(method, path, document)

    def approve(self, intent):
        require(all(item.get("consentType") == "Principal" and item.get("scope") == "llm.invoke" and item.get("principalId") for item in intent["grants"]), "Access transport refuses tenant-wide or unrelated consent even in an approval payload")
        self.approved = {(f"/servicePrincipals/{item['resourceId']}/appRoleAssignedTo", fingerprint(item)) for item in intent["roles"]}
        self.approved.update(("/oauth2PermissionGrants", fingerprint(item)) for item in intent["grants"])

    def collection(self, path):
        original_path = urlsplit(path).path
        visited, items = set(), []
        while path:
            require(path not in visited and len(visited) < 100, "Graph pagination repeated or exceeded its safe bound")
            visited.add(path)
            page = self.request("GET", path)
            require(isinstance(page.get("value"), list), "Invalid Graph collection")
            items.extend(page["value"])
            require(len(items) <= 10000, "Graph collection exceeds the supported access review size")
            next_link = page.get("@odata.nextLink")
            path = None
            if next_link:
                parsed = urlsplit(next_link)
                require(parsed.scheme == "https" and parsed.netloc == "graph.microsoft.com" and parsed.path == "/v1.0" + original_path and not parsed.fragment, "Graph pagination changed authority or collection")
                path = parsed.path.removeprefix("/v1.0") + "?" + parsed.query
        return items


def directory_inputs(config, graph):
    clients, subjects = {}, {}
    settings = proxy_settings(config)
    for client_id in settings["apiClientIds"]:
        identifier = object_id(client_id)
        matches = graph.collection("/servicePrincipals?" + urlencode({"$filter": "appId eq '" + identifier + "'", "$select": "id,appId,accountEnabled"}))
        require(len(matches) == 1 and matches[0].get("accountEnabled") is True, "Approved calling client needs one enabled service principal in this tenant")
        require(object_id(matches[0]["appId"]) == identifier, "Calling client lookup returned a different application")
        clients[identifier] = matches[0]
    for binding in settings["bindings"]:
        if binding.get("disabled", False):
            continue
        oid = object_id(binding["oid"])
        kind = binding.get("principalType", "User")
        path = "/users/" if kind == "User" else "/servicePrincipals/"
        selected = "id,accountEnabled" if kind == "User" else "id,appId,accountEnabled"
        item = graph.request("GET", path + oid + "?$select=" + selected)
        require(item.get("@odata.type", "#microsoft.graph.user" if kind == "User" else "#microsoft.graph.servicePrincipal") == ("#microsoft.graph.user" if kind == "User" else "#microsoft.graph.servicePrincipal"), "Directory subject type mismatch")
        subjects[oid] = {**item, "type": kind}
    return subjects, clients


def current_access(graph, applications):
    roles, grants = [], []
    for plane in ("api", "admin"):
        resource = object_id(applications[plane]["servicePrincipalId"])
        assigned = graph.collection("/servicePrincipals/" + resource + "/appRoleAssignedTo?$select=id,principalId,resourceId,appRoleId")
        for item in assigned:
            require(object_id(item["resourceId"]) == resource, "Role assignment targets an unexpected resource")
            roles.append({key: item[key] for key in ("principalId", "resourceId", "appRoleId")})
        consent = graph.collection("/oauth2PermissionGrants?" + urlencode({"$filter": "resourceId eq '" + resource + "'"}))
        for item in consent:
            require(object_id(item["resourceId"]) == resource, "Consent lookup returned an unexpected resource")
            grants.append({key: item.get(key) for key in ("clientId", "consentType", "principalId", "resourceId", "scope")})
    return {"roles": sorted(roles, key=fingerprint), "grants": sorted(grants, key=fingerprint)}


def missing_access(intent, before):
    missing = {}
    for kind in ("roles", "grants"):
        desired = {fingerprint(item): item for item in intent[kind]}
        actual = {fingerprint(item): item for item in before[kind]}
        require(len(actual) == len(before[kind]) and set(actual).issubset(desired), "Existing gateway access is broader, duplicated or different; no automatic overwrite or deletion")
        missing[kind] = [desired[key] for key in sorted(set(desired) - set(actual))]
    return missing


def grant_access(config, operation, revision, directory, approved, graph, azure):
    require(operation in {"plan", "execute"}, "Invalid Entra access operation")
    found = discover(config, graph, managed_definitions(config))
    require(all(item and item["servicePrincipal"] for item in found.values()), "Initialize both Entra applications before access grants")
    applications = {plane: {"id": item["application"]["id"], "appId": item["application"]["appId"], "servicePrincipalId": item["servicePrincipal"]["id"]} for plane, item in found.items()}
    subjects, clients = directory_inputs(config, graph)
    intent = access_intent(config, applications, subjects, clients)
    before = current_access(graph, applications)
    missing = missing_access(intent, before)
    plan = {"stage": 7, "action": "entra-access", "revision": revision, "configSha256": stage_fingerprint(config, 7), "applications": applications, "subjects": subjects, "clients": clients, "before": before, "intent": intent, "create": missing}
    digest = fingerprint(plan)
    summary = {"stage": 7, "action": "entra-access", "planSha256": digest, "stageAccepted": False, "rolesToCreate": len(missing["roles"]), "consentsToCreate": len(missing["grants"])}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2) + "\n")
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    if operation == "execute":
        require(approved == digest, "Entra access plan changed or was not approved")
        require(current_access(graph, applications) == before, "Gateway grants changed during approval")
        require(directory_inputs(config, graph) == (subjects, clients), "Subject or calling-client state changed during approval")
        graph.approve(missing)
        for assignment in missing["roles"]:
            graph.request("POST", "/servicePrincipals/" + assignment["resourceId"] + "/appRoleAssignedTo", assignment)
        for consent in missing["grants"]:
            require(consent["consentType"] == "Principal" and consent["scope"] == "llm.invoke" and consent["principalId"], "Only specific-user invocation consent is permitted")
            graph.request("POST", "/oauth2PermissionGrants", consent)
        after = current_access(graph, applications)
        remaining = missing_access(intent, after)
        require(not any(remaining.values()), "New grants are not yet observable; replan to inspect partial results, no acceptance issued")
        receipt = {"revision": revision, "configSha256": stage_fingerprint(config, 7), "applications": applications, "intent": intent, "verifiedAt": datetime.now(timezone.utc).isoformat(), "directoryVerified": True, "loginVerified": False}
        document = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"entraAccess": {"type": "object", "value": receipt}}}
        path = directory / "entra-access-receipt.json"
        private_write(path, json.dumps(document))
        saved = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 7, "entra-access"), "--mode", "Incremental", "--template-file", str(path)])
        require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Access granted but receipt storage failed; replan without duplicating grants")
        summary.update(applied=True, directoryVerified=True, loginVerified=False)
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    return summary


def initialize_entra_access(config, operation, revision, directory, approved):
    from azure.identity import AzureCliCredential
    from azure.core.exceptions import AzureError

    access_principal = object_id(entra_settings(config).get("accessPrincipalId"))
    client_id = object_id(os.environ.get("AZURE_ENTRA_ACCESS_CLIENT_ID"))
    azure = AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope mismatch")
    credential = AzureCliCredential(tenant_id=config["azure"]["tenantId"])
    graph = GraphAccess(credential)
    try:
        principals = graph.collection("/servicePrincipals?" + urlencode({"$filter": "appId eq '" + client_id + "'", "$select": "id,appId"}))
        require(len(principals) == 1 and object_id(principals[0]["id"]) == access_principal, "Entra access Client ID differs from approved principal Object ID")
        return grant_access(config, operation, revision, directory, approved, graph, azure)
    except AzureError:
        raise MigrationError("Unable to authenticate the separately authorized Entra access identity") from None
    finally:
        graph.close()
        credential.close()