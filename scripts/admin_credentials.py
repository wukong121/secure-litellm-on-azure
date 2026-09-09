"""Initialize the admin OIDC and session secrets without overwriting existing values."""

import base64
from datetime import datetime, timedelta, timezone
import hmac
import json
import secrets

from scripts.customer_migration import fingerprint, private_write, require, stage_fingerprint
from scripts.entra_apps import GraphApplications, discover, entra_settings, managed_definitions, verify_bootstrap_identity
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.proxy_config import object_id


OIDC_SECRET = "llm-admin-oidc-client-secret"
SESSION_SECRET = "llm-admin-session-key"
CREDENTIAL_NAME = "llmgw-admin-bootstrap"


def read_admin_secret(client, name, application_id, now, *, require_fresh=True):
    from azure.core.exceptions import ResourceNotFoundError

    try:
        item = client.get_secret(name)
    except ResourceNotFoundError:
        return None
    properties = item.properties
    tags = properties.tags or {}
    require(properties.enabled is True and item.value and tags.get("llmgw-purpose") == name and tags.get("llmgw-application-id") == application_id, "Admin secret is unmanaged, disabled or bound to a different application")
    require(not properties.not_before or properties.not_before <= now, "Admin credential is not yet valid")
    if name == OIDC_SECRET:
        require(properties.expires_on and (not require_fresh or properties.expires_on > now + timedelta(days=7)), "OIDC credential requires separately approved rotation before expiry")
    else:
        require(properties.expires_on is None and len(base64.b64decode(item.value, validate=True)) == 32, "Admin session key must be a valid 32-byte base64 key without implicit expiry")
    return item


def secret_metadata(item):
    if item is None:
        return None
    return {"id": item.properties.id, "version": item.properties.version, "keyId": (item.properties.tags or {}).get("llmgw-key-id"), "expiresAt": item.properties.expires_on.isoformat() if item.properties.expires_on else None}


def inspect_credentials(graph, client, application, now):
    oidc = read_admin_secret(client, OIDC_SECRET, application["id"], now)
    session = read_admin_secret(client, SESSION_SECRET, application["id"], now)
    credentials = graph.request("GET", "/applications/" + application["id"] + "?$select=passwordCredentials").get("passwordCredentials", [])
    if oidc:
        key_id = (oidc.properties.tags or {}).get("llmgw-key-id")
        matches = [entry for entry in credentials if entry.get("keyId") == key_id and entry.get("displayName") == CREDENTIAL_NAME]
        require(len(matches) == 1, "Stored OIDC secret does not match a managed Entra credential")
        end = datetime.fromisoformat(matches[0]["endDateTime"].replace("Z", "+00:00"))
        require(end > now + timedelta(days=7) and end == oidc.properties.expires_on, "Vault and Entra credential expiry mismatch")
    else:
        require(not any(entry.get("displayName") == CREDENTIAL_NAME for entry in credentials), "An unrecorded Entra bootstrap credential exists; investigate the previous partial failure before retrying")
    return {"oidc": oidc, "session": session}, {"oidc": secret_metadata(oidc), "session": secret_metadata(session), "credentials": [{key: entry.get(key) for key in ("keyId", "displayName", "startDateTime", "endDateTime")} for entry in credentials]}


def save_credential_receipt(config, revision, directory, application, vault_id, metadata, azure, now):
    receipt = {"revision": revision, "configSha256": stage_fingerprint(config, 7), "application": application, "vaultId": vault_id, "secrets": {OIDC_SECRET: metadata["oidc"], SESSION_SECRET: metadata["session"]}, "verifiedAt": now.isoformat()}
    document = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"adminCredentials": {"type": "object", "value": receipt}}}
    path = directory / "admin-credential-receipt.json"
    private_write(path, json.dumps(document))
    saved = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 7, "admin-credentials"), "--mode", "Incremental", "--template-file", str(path)])
    require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Credentials stored but receipt failed; replan without rotating existing values")


def initialize_credentials(config, operation, revision, directory, approved, graph, client, application, vault_id, azure, now=None):
    now = now or datetime.now(timezone.utc)
    require(operation in {"plan", "execute"}, "Invalid admin credential operation")
    values, before = inspect_credentials(graph, client, application, now)
    plan = {"stage": 7, "action": "admin-credentials", "revision": revision, "configSha256": stage_fingerprint(config, 7), "application": application, "vaultId": vault_id, "before": before, "credentialLifetimeDays": entra_settings(config).get("credentialLifetimeDays", 90)}
    digest = fingerprint(plan)
    summary = {"stage": 7, "action": "admin-credentials", "planSha256": digest, "stageAccepted": False}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2) + "\n")
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    if operation == "execute":
        require(approved == digest, "Admin credential plan changed or was not approved")
        _values, rechecked = inspect_credentials(graph, client, application, now)
        require(before == rechecked, "Admin credential state changed during approval")
        if values["oidc"] is None:
            end = (now + timedelta(days=plan["credentialLifetimeDays"])).replace(microsecond=0)
            generated = graph.request("POST", "/applications/" + application["id"] + "/addPassword", {"passwordCredential": {"displayName": CREDENTIAL_NAME, "endDateTime": end.isoformat()}})
            require(isinstance(generated.get("secretText"), str) and generated["secretText"], "Entra did not return the new credential; do not blindly retry")
            key_id = object_id(generated["keyId"])
            expires = datetime.fromisoformat(generated["endDateTime"].replace("Z", "+00:00"))
            require(expires == end, "Entra returned an unexpected credential lifetime")
            item = client.set_secret(OIDC_SECRET, generated["secretText"], enabled=True, expires_on=expires, tags={"llmgw-purpose": OIDC_SECRET, "llmgw-application-id": application["id"], "llmgw-key-id": key_id})
            require(hmac.compare_digest(item.value, generated["secretText"]), "Stored OIDC credential differs from generated value")
        if values["session"] is None:
            value = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
            session = client.set_secret(SESSION_SECRET, value, enabled=True, tags={"llmgw-purpose": SESSION_SECRET, "llmgw-application-id": application["id"]})
            require(hmac.compare_digest(session.value, value), "Session key storage verification failed")
        _verified, metadata = inspect_credentials(graph, client, application, now)
        require(metadata["oidc"] and metadata["session"], "Admin credentials are incomplete")
        save_credential_receipt(config, revision, directory, application, vault_id, metadata, azure, now)
        summary["initialized"] = True
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    return summary


def initialize_admin_credentials(config, operation, revision, directory, approved, *, action="admin-credentials"):
    from azure.identity import AzureCliCredential
    from azure.keyvault.secrets import SecretClient
    from azure.core.exceptions import AzureError

    azure = AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope mismatch")
    output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 7, "proxy-foundation"), "--query", "{state:properties.provisioningState,foundation:properties.outputs.proxyFoundation.value}"])
    require(output.get("state") == "Succeeded", "Deploy proxy-foundation before admin credential initialization")
    admin = output["foundation"]["admin"]
    vault = azure.scoped(["keyvault", "show", "--resource-group", config["target"]["resourceGroup"], "--name", admin["vault"]["name"], "--query", "{id:id,uri:properties.vaultUri,public:properties.publicNetworkAccess,rbac:properties.enableRbacAuthorization,purge:properties.enablePurgeProtection}"])
    require(vault["id"].lower() == (group_id(config) + "/providers/Microsoft.KeyVault/vaults/" + admin["vault"]["name"]).lower() and vault["id"].lower() == admin["vault"]["id"].lower(), "Admin Vault is outside approved scope")
    require(vault["uri"].rstrip("/").lower() == f"https://{admin['vault']['name']}.vault.azure.net".lower() and vault["public"] == "Disabled" and vault["rbac"] is True and vault["purge"] is True, "Admin Vault must be private, RBAC and purge protected")
    credential = AzureCliCredential(tenant_id=config["azure"]["tenantId"])
    graph = GraphApplications(credential)
    try:
        verify_bootstrap_identity(config, graph)
        applications = discover(config, graph, managed_definitions(config))
        require(applications["admin"] and applications["admin"]["servicePrincipal"], "Initialize Entra applications before admin credentials")
        actual = applications["admin"]["application"]
        application = {"id": actual["id"], "appId": actual["appId"]}
        with SecretClient(vault_url=vault["uri"], credential=credential) as client:
            if action != "admin-credentials":
                return manage_credential_lifecycle(config, operation, action, revision, directory, approved, graph, client, application, vault["id"], azure)
            return initialize_credentials(config, operation, revision, directory, approved, graph, client, application, vault["id"], azure)
    except AzureError:
        raise ValueError("Admin credential initialization failed; an Entra credential may remain unrecorded. Review private access and partial state without blindly recreating credentials") from None
    finally:
        graph.close()
        credential.close()


def lifecycle_state(graph, client, application, now):
    from azure.core.exceptions import ResourceNotFoundError

    current = read_admin_secret(client, OIDC_SECRET, application["id"], now, require_fresh=False)
    session = read_admin_secret(client, SESSION_SECRET, application["id"], now)
    versions = []
    try:
        for item in client.list_properties_of_secret_versions(OIDC_SECRET):
            tags = item.tags or {}
            require(tags.get("llmgw-purpose") == OIDC_SECRET and tags.get("llmgw-application-id") == application["id"] and tags.get("llmgw-key-id"), "Unknown historical Vault credential version blocks automatic recovery")
            versions.append({"id": item.id, "version": item.version, "keyId": object_id(tags["llmgw-key-id"])})
    except ResourceNotFoundError:
        require(current is None, "Vault version listing disagrees with the current credential")
    keys = graph.request("GET", "/applications/" + application["id"] + "?$select=passwordCredentials").get("passwordCredentials", [])
    managed = [{name: item.get(name) for name in ("keyId", "displayName", "startDateTime", "endDateTime")} for item in keys if item.get("displayName") == CREDENTIAL_NAME]
    references = {item["keyId"] for item in versions}
    if current:
        key_id = (current.properties.tags or {})["llmgw-key-id"]
        require(key_id in references and any(item["keyId"] == key_id for item in managed), "Current credential no longer matches Entra; investigate before rotating")
    orphaned = [item for item in managed if item["keyId"] not in references]
    return {"current": secret_metadata(current), "session": secret_metadata(session), "versions": sorted(versions, key=lambda item: item["version"]), "credentials": sorted(managed, key=lambda item: item["keyId"]), "orphaned": sorted(orphaned, key=lambda item: item["keyId"])}


def manage_credential_lifecycle(config, operation, action, revision, directory, approved, graph, client, application, vault_id, azure, now=None):
    require(operation in {"plan", "execute"} and action in {"admin-credentials-rotate", "admin-credentials-recover", "admin-credentials-session-rotate", "admin-credentials-retire-expired"}, "Invalid admin credential lifecycle operation")
    now = now or datetime.now(timezone.utc)
    state = lifecycle_state(graph, client, application, now)
    if action == "admin-credentials-rotate":
        require(state["current"] and state["session"] and not state["orphaned"], "Initialize or recover credentials before rotating")
        require(len(state["credentials"]) < 5, "Too many retained credentials; retire old versions through a separately reviewed process")
    retire = []
    if action in {"admin-credentials-session-rotate", "admin-credentials-retire-expired"}:
        require(state["current"] and state["session"] and not state["orphaned"], "Initialize and recover credentials before lifecycle changes")
        inspect_credentials(graph, client, application, now)
    if action == "admin-credentials-retire-expired":
        retire = sorted(entry["keyId"] for entry in state["credentials"] if entry["keyId"] != state["current"]["keyId"] and datetime.fromisoformat(entry["endDateTime"].replace("Z", "+00:00")) <= now - timedelta(days=1))
    plan = {"stage": 7, "action": action, "revision": revision, "configSha256": stage_fingerprint(config, 7), "application": application, "vaultId": vault_id, "before": state, "credentialLifetimeDays": entra_settings(config).get("credentialLifetimeDays", 90), "removeKeyIds": [item["keyId"] for item in state["orphaned"]] if action.endswith("recover") else []}
    if action == "admin-credentials-retire-expired":
        plan["removeKeyIds"] = retire
    digest = fingerprint(plan)
    summary = {"stage": 7, "action": action, "planSha256": digest, "stageAccepted": False}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2) + "\n")
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    if operation == "execute":
        require(approved == digest, "Credential lifecycle plan changed or was not approved")
        require(lifecycle_state(graph, client, application, now) == state, "Credential state changed during approval")
        if action == "admin-credentials-rotate":
            end = (now + timedelta(days=plan["credentialLifetimeDays"])).replace(microsecond=0)
            result = graph.request("POST", "/applications/" + application["id"] + "/addPassword", {"passwordCredential": {"displayName": CREDENTIAL_NAME, "endDateTime": end.isoformat()}})
            key_id = object_id(result["keyId"])
            expires = datetime.fromisoformat(result["endDateTime"].replace("Z", "+00:00"))
            require(result.get("secretText") and expires == end, "Invalid rotated credential response; inspect possible orphan before retrying")
            stored = client.set_secret(OIDC_SECRET, result["secretText"], enabled=True, expires_on=expires, tags={"llmgw-purpose": OIDC_SECRET, "llmgw-application-id": application["id"], "llmgw-key-id": key_id})
            require(hmac.compare_digest(stored.value, result["secretText"]), "Rotated credential storage mismatch")
            _values, verified = inspect_credentials(graph, client, application, now)
            require(verified["session"] == state["session"], "Session key changed during OIDC rotation")
            save_credential_receipt(config, revision, directory, application, vault_id, verified, azure, now)
            summary.update(rotated=True, previousVersionRetained=state["current"]["version"], newVersion=verified["oidc"]["version"])
        elif action == "admin-credentials-session-rotate":
            value = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
            stored = client.set_secret(SESSION_SECRET, value, enabled=True, tags={"llmgw-purpose": SESSION_SECRET, "llmgw-application-id": application["id"]})
            require(hmac.compare_digest(stored.value, value), "Rotated session credential storage mismatch")
            _values, verified = inspect_credentials(graph, client, application, now)
            require(verified["oidc"] == state["current"] and verified["session"]["version"] != state["session"]["version"], "Session rotation changed OIDC or did not create a new version")
            save_credential_receipt(config, revision, directory, application, vault_id, verified, azure, now)
            summary.update(rotated=True, sessionRotation=True, previousVersionRetained=state["session"]["version"], newVersion=verified["session"]["version"], adminRolloutRequired=True, activeCookiesInvalidated=False)
        elif action == "admin-credentials-retire-expired":
            for key_id in retire:
                live = lifecycle_state(graph, client, application, now)
                selected = next((entry for entry in live["credentials"] if entry["keyId"] == key_id), None)
                require(live["current"] == state["current"] and live["session"] == state["session"] and live["versions"] == state["versions"], "Credential references changed before retirement")
                require(selected and key_id != live["current"]["keyId"] and datetime.fromisoformat(selected["endDateTime"].replace("Z", "+00:00")) <= now - timedelta(days=1), "Retirement target is current, changed or not expired")
                graph.request("POST", "/applications/" + application["id"] + "/removePassword", {"keyId": key_id})
            after = lifecycle_state(graph, client, application, now)
            require(not set(retire) & {entry["keyId"] for entry in after["credentials"]} and after["current"] == state["current"] and after["session"] == state["session"] and after["versions"] == state["versions"], "Expired credential retirement was not confirmed")
            summary.update(retiredExpiredKeyIds=retire, vaultVersionsRetained=True)
        else:
            for key_id in plan["removeKeyIds"]:
                live = lifecycle_state(graph, client, application, now)
                require(key_id in {item["keyId"] for item in live["orphaned"]}, "Credential gained a Vault reference; recovery stopped")
                graph.request("POST", "/applications/" + application["id"] + "/removePassword", {"keyId": key_id})
            after = lifecycle_state(graph, client, application, now)
            require(not after["orphaned"] and after["current"] == state["current"] and after["session"] == state["session"] and after["versions"] == state["versions"], "Recovery is not yet confirmed or changed a referenced credential")
            summary.update(recovered=True, removedKeyIds=plan["removeKeyIds"])
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    return summary