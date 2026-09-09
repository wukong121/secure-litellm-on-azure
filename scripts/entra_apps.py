"""Create owned Entra application definitions without granting client access."""

import copy
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from scripts.customer_migration import MigrationError, fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.proxy_config import entra_documents, object_id, proxy_policy


APP_FIELDS = "id,appId,displayName,tags,signInAudience,api,appRoles,web,identifierUris,optionalClaims,requiredResourceAccess,publicClient,spa,isFallbackPublicClient"
OWNERS_FIELD = "owners" + "@odata.bind"


class GraphApplications:
    def __init__(self, credential):
        self.credential = credential
        self.session = requests.Session()

    def request(self, method, path, document=None):
        require(method in {"GET", "POST", "PATCH"} and path.startswith(("/applications", "/servicePrincipals")) and ".." not in path and "#" not in path, "Graph operation outside application provisioning scope")
        return self._send(method, path, document)

    def _send(self, method, path, document=None):
        try:
            token = self.credential.get_token("https://graph.microsoft.com/.default").token
            response = self.session.request(method, "https://graph.microsoft.com/v1.0" + path, headers={"Authorization": "Bearer " + token}, json=document, timeout=60, allow_redirects=False)
            if response.status_code not in {200, 201, 204}:
                raise MigrationError(f"Graph application operation failed (HTTP {response.status_code}); review granted permissions and retry with a new plan")
            return response.json() if response.content else {}
        except requests.RequestException:
            raise MigrationError("Graph transport failed; the write result may be unknown. Replan without blindly repeating create operations") from None

    def collection(self, path):
        result = self.request("GET", path)
        require(isinstance(result.get("value"), list) and not result.get("@odata.nextLink"), "Unexpected or ambiguous paginated application lookup")
        return result["value"]

    def close(self):
        self.session.close()


def entra_settings(config):
    settings = config.get("entra", {})
    require(isinstance(settings, dict) and not set(settings) - {"bootstrapPrincipalId", "credentialLifetimeDays", "accessPrincipalId"}, "Invalid Entra bootstrap fields")
    object_id(settings.get("bootstrapPrincipalId"))
    if "databaseAccess" in config:
        require(object_id(settings["bootstrapPrincipalId"]) != object_id(config["databaseAccess"]["migrationPrincipalId"]), "Entra bootstrap and database migration identities must be distinct")
    if "accessPrincipalId" in settings:
        access = object_id(settings["accessPrincipalId"])
        require(access != object_id(settings["bootstrapPrincipalId"]) and access != config.get("databaseAccess", {}).get("migrationPrincipalId", "").lower(), "Entra access grant identity must be separate from app bootstrap and database migration")
    days = settings.get("credentialLifetimeDays", 90)
    require(type(days) is int and 30 <= days <= 180, "Entra credential lifetime must be 30-180 days")
    return settings


def managed_definitions(config):
    owner = object_id(entra_settings(config)["bootstrapPrincipalId"])
    marker = "llmgw-" + fingerprint({"scope": group_id(config).lower(), "tenant": config["azure"]["tenantId"], "environment": config["environment"]})[:24]
    result = entra_documents(config)
    for plane, document in result.items():
        document["displayName"] += "-" + marker[-12:]
        document["tags"] = [marker, "llmgw-plane-" + plane]
        document["isFallbackPublicClient"] = False
        document["requiredResourceAccess"] = []
        document[OWNERS_FIELD] = ["https://graph.microsoft.com/v1.0/directoryObjects/" + owner]
    return result


def project(expected, actual):
    if isinstance(expected, dict):
        return {key: project(value, (actual or {}).get(key)) for key, value in expected.items()}
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return actual
        if expected and isinstance(expected[0], dict) and "id" in expected[0]:
            by_id = {item.get("id"): item for item in actual}
            if set(by_id) != {item["id"] for item in expected}:
                return actual
            return [project(item, by_id[item["id"]]) for item in expected]
        if expected and isinstance(expected[0], dict):
            return [project(expected[index], item) for index, item in enumerate(actual)] if len(expected) == len(actual) else actual
        return sorted(actual)
    return actual


def verify_application(config, graph, actual, desired, plane, *, allow_user_role_upgrade=False):
    object_id(actual["id"])
    object_id(actual["appId"])
    contract = {key: value for key, value in desired.items() if key != OWNERS_FIELD}
    if allow_user_role_upgrade and plane == "api":
        current_ids = {item.get("id") for item in actual.get("appRoles", [])}
        legacy_roles = [item for item in desired["appRoles"] if item["value"] != "Llm.User"]
        if current_ids == {item["id"] for item in legacy_roles}:
            contract["appRoles"] = legacy_roles
    require(project(contract, actual) == project(contract, contract), "Existing Entra application differs from the approved trust configuration; automatic overwrite is forbidden")
    require(not (actual.get("publicClient") or {}).get("redirectUris") and not (actual.get("spa") or {}).get("redirectUris"), "Public client or SPA redirects are not allowed")
    api = actual.get("api") or {}
    require(not api.get("preAuthorizedApplications") and not api.get("knownClientApplications") and not api.get("acceptMappedClaims"), "Unexpected preauthorization or claims mapping")
    if plane == "admin":
        require(not api.get("oauth2PermissionScopes") and not actual.get("identifierUris"), "Admin application must not expose an API audience")
    else:
        web = actual.get("web") or {}
        require(not web.get("redirectUris") and not any((web.get("implicitGrantSettings") or {}).values()), "API application must not enable browser implicit grants")
        require(actual.get("identifierUris", []) in ([], ["api://" + actual["appId"]]), "API identifier URI differs from its generated application ID")
    owners = graph.collection("/applications/" + actual["id"] + "/owners?$select=id")
    require({object_id(config["entra"]["bootstrapPrincipalId"])} == {object_id(item["id"]) for item in owners}, "Application owners differ from the approved Entra bootstrap identity")


def discover(config, graph, definitions, *, allow_user_role_upgrade=False):
    found = {}
    for plane, desired in definitions.items():
        query = urlencode({"$filter": "displayName eq '" + desired["displayName"] + "'", "$select": APP_FIELDS})
        matches = graph.collection("/applications?" + query)
        require(len(matches) <= 1, "Duplicate managed application names require explicit investigation")
        if not matches:
            found[plane] = None
            continue
        app = matches[0]
        verify_application(config, graph, app, desired, plane, allow_user_role_upgrade=allow_user_role_upgrade)
        principals = graph.collection("/servicePrincipals?" + urlencode({"$filter": "appId eq '" + app["appId"] + "'", "$select": "id,appId,appOwnerOrganizationId,appRoleAssignmentRequired,accountEnabled,tags"}))
        require(len(principals) <= 1, "Ambiguous application service principal")
        principal = principals[0] if principals else None
        if principal:
            require(object_id(principal["appOwnerOrganizationId"]) == object_id(config["azure"]["tenantId"]) and principal["appRoleAssignmentRequired"] is True and principal["accountEnabled"] is True and set(desired["tags"]).issubset(principal.get("tags", [])), "Service principal ownership or assignment policy differs from the approved configuration")
        found[plane] = {"application": app, "servicePrincipal": principal}
    return found


def provision_applications(config, operation, revision, directory, approved, graph, azure):
    require(operation in {"plan", "execute"}, "Invalid Entra application operation")
    definitions = managed_definitions(config)
    before = discover(config, graph, definitions, allow_user_role_upgrade=True)
    plan = {"stage": 7, "action": "entra-apps", "revision": revision, "configSha256": stage_fingerprint(config, 7), "definitions": definitions, "before": before, "grantsClientAccess": False}
    digest = fingerprint(plan)
    summary = {"stage": 7, "action": "entra-apps", "planSha256": digest, "stageAccepted": False}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2) + "\n")
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    if operation == "execute":
        require(approved == digest, "Entra application plan changed or was not approved")
        for plane, desired in definitions.items():
            current = before[plane]
            app = current["application"] if current else graph.request("POST", "/applications", copy.deepcopy(desired))
            object_id(app["id"])
            object_id(app["appId"])
            if plane == "api" and current and {item["id"] for item in app["appRoles"]} != {item["id"] for item in desired["appRoles"]}:
                graph.request("PATCH", "/applications/" + app["id"], {"appRoles": desired["appRoles"]})
            if plane == "api" and not app.get("identifierUris"):
                graph.request("PATCH", "/applications/" + app["id"], {"identifierUris": ["api://" + app["appId"]]})
            if not current or not current["servicePrincipal"]:
                graph.request("POST", "/servicePrincipals", {"appId": app["appId"], "appRoleAssignmentRequired": True, "accountEnabled": True, "tags": desired["tags"]})
        verified = discover(config, graph, definitions)
        require(all(item and item["servicePrincipal"] for item in verified.values()), "Entra applications are not yet observable; replan to resume, do not assume success")
        require(verified["api"]["application"].get("identifierUris") == ["api://" + verified["api"]["application"]["appId"]], "API identifier URI is not yet verified; replan before continuing")
        applications = {plane: {"id": item["application"]["id"], "appId": item["application"]["appId"], "servicePrincipalId": item["servicePrincipal"]["id"]} for plane, item in verified.items()}
        policy = proxy_policy(config, applications)
        receipt = {"revision": revision, "configSha256": stage_fingerprint(config, 7), "applications": applications, "verifiedAt": datetime.now(timezone.utc).isoformat(), "clientAccessGranted": False}
        template = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"entraApplications": {"type": "object", "value": receipt}}}
        path = directory / "entra-receipt-template.json"
        private_write(path, json.dumps(template))
        saved = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 7, "entra-apps"), "--mode", "Incremental", "--template-file", str(path)])
        require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Entra apps created but receipt storage failed; replan without duplicating apps")
        private_write(directory / "proxy-policy.json", json.dumps(policy, indent=2) + "\n")
        summary.update(initialized=True, clientAccessGranted=False)
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    return summary


def initialize_entra_apps(config, operation, revision, directory, approved):
    from azure.identity import AzureCliCredential
    from azure.core.exceptions import AzureError

    azure = AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope mismatch")
    credential = AzureCliCredential(tenant_id=config["azure"]["tenantId"])
    graph = GraphApplications(credential)
    try:
        verify_bootstrap_identity(config, graph)
        return provision_applications(config, operation, revision, directory, approved, graph, azure)
    except AzureError:
        raise MigrationError("Unable to authenticate the approved Entra bootstrap identity; token details suppressed") from None
    finally:
        graph.close()
        credential.close()


def verify_bootstrap_identity(config, graph):
    client_id = object_id(os.environ.get("AZURE_ENTRA_CLIENT_ID"))
    principals = graph.collection("/servicePrincipals?" + urlencode({"$filter": "appId eq '" + client_id + "'", "$select": "id,appId"}))
    require(len(principals) == 1 and object_id(principals[0]["id"]) == object_id(entra_settings(config)["bootstrapPrincipalId"]), "Entra bootstrap Client ID does not match the approved service principal Object ID")