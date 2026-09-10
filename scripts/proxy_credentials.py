"""Provision recoverable LiteLLM credentials only for backend administrators."""

import hashlib
import hmac
import secrets

from scripts.customer_migration import fingerprint, require
from scripts.proxy_config import credential_name, object_id, proxy_settings


def binding_contract(config, binding):
    require(binding["plane"] == "admin" and binding["role"] in {"proxy_admin", "proxy_admin_viewer"}, "Only backend administrators receive managed proxy credentials")
    name = credential_name(config["azure"]["tenantId"], binding)
    contract = {"tenantId": object_id(config["azure"]["tenantId"]), "oid": object_id(binding["oid"]), "plane": binding["plane"], "role": binding["role"], "models": sorted(binding["models"])}
    return {**contract, "secretName": name, "userId": "llmgw-" + binding["plane"] + "-" + name[4:], "bindingSha256": fingerprint(contract)}


def user_payload(contract):
    return {"user_id": contract["userId"], "user_role": contract["role"], "models": contract["models"], "auto_create_key": False, "send_invite_email": False, "metadata": {"llmgw_binding": contract["bindingSha256"]}}


def key_payload(contract, value):
    require(contract["plane"] == "admin", "API vkeys are managed directly in LiteLLM")
    routes = ["/model/info", "/team/info", "/key/info"]
    if contract["role"] == "proxy_admin":
        routes.extend(["/key/block", "/key/unblock"])
    return {"key": value, "key_alias": contract["secretName"], "user_id": contract["userId"], "models": contract["models"], "metadata": {"llmgw_binding": contract["bindingSha256"]}, "key_type": "default", "allowed_routes": routes, "auto_rotate": False}


def validate_user(contract, user):
    if user is None:
        return
    require(user.get("user_id") == contract["userId"] and user.get("user_role") == contract["role"] and sorted(user.get("models", [])) == contract["models"] and (user.get("metadata") or {}).get("llmgw_binding") == contract["bindingSha256"], "Existing backend user does not match the approved identity/role/models")
    require(not user.get("teams") and not user.get("organization_memberships"), "Managed proxy user must not inherit unexpected team or organization privileges")


def validate_key(contract, info, value):
    if info is None:
        return
    expected = key_payload(contract, value)
    require(info.get("user_id") == contract["userId"] and sorted(info.get("models", [])) == contract["models"] and info.get("key_alias") == contract["secretName"] and (info.get("metadata") or {}).get("llmgw_binding") == contract["bindingSha256"], "Existing backend key does not match the approved identity/models")
    require(not info.get("blocked") and not info.get("team_id") and not info.get("organization_id") and not info.get("expires") and not info.get("auto_rotate"), "Backend key is blocked, expires or belongs to an unexpected scope; no automatic override")
    require(sorted(info.get("allowed_routes") or []) == sorted(expected["allowed_routes"]), "Backend key route permissions differ from the approved plane")


def inspect_binding(config, binding, vault, backend):
    from azure.core.exceptions import ResourceNotFoundError

    contract = binding_contract(config, binding)
    try:
        secret = vault.get_secret(contract["secretName"])
    except ResourceNotFoundError:
        secret = None
    key = None
    if secret:
        tags = secret.properties.tags or {}
        require(secret.properties.enabled is True and secret.properties.expires_on is None and tags.get("llmgw-binding") == contract["bindingSha256"] and tags.get("llmgw-state") in {"pending", "ready"}, "Proxy key secret is unmanaged, expired or bound to different privileges")
        require(isinstance(secret.value, str) and secret.value.startswith("sk-") and len(secret.value) >= 40, "Invalid stored proxy key")
        key = backend.key_info(hashlib.sha256(secret.value.encode()).hexdigest())
        validate_key(contract, key, secret.value)
        require(tags["llmgw-state"] != "ready" or key is not None, "Previously initialized backend key is missing; do not recreate a revoked key")
    user = backend.user_info(contract["userId"])
    validate_user(contract, user)
    require(key is None or user is not None, "Backend credential has no matching user")
    summary = {"contract": contract, "secret": {"id": secret.properties.id, "version": secret.properties.version, "state": secret.properties.tags["llmgw-state"]} if secret else None, "userExists": user is not None, "keyExists": key is not None}
    return contract, secret, summary


def provision_binding(config, binding, vault, backend):
    contract, item, before = inspect_binding(config, binding, vault, backend)
    if item is None:
        value = "sk-" + secrets.token_urlsafe(48)
        item = vault.set_secret(contract["secretName"], value, enabled=True, tags={"llmgw-binding": contract["bindingSha256"], "llmgw-state": "pending"})
        require(hmac.compare_digest(item.value, value), "Proxy key storage verification failed")
    value = item.value
    if not before["userExists"]:
        backend.create_user(user_payload(contract))
    validate_user(contract, backend.user_info(contract["userId"]))
    info = backend.key_info(hashlib.sha256(value.encode()).hexdigest())
    if info is None:
        backend.create_key(key_payload(contract, value))
    info = backend.key_info(hashlib.sha256(value.encode()).hexdigest())
    require(info is not None, "Backend did not confirm the credential; pending Vault value retained for replan")
    validate_key(contract, info, value)
    if (item.properties.tags or {}).get("llmgw-state") != "ready":
        vault.update_secret_properties(contract["secretName"], item.properties.version, tags={"llmgw-binding": contract["bindingSha256"], "llmgw-state": "ready"})
    _contract, verified, summary = inspect_binding(config, binding, vault, backend)
    require(summary["secret"]["state"] == "ready" and verified.properties.version == item.properties.version and hmac.compare_digest(verified.value, value), "Proxy credential final verification failed")
    return summary


def credential_bindings(config):
    return [item for item in proxy_settings(config)["bindings"] if item["plane"] == "admin" and item["role"] != "audit_reader" and not item.get("disabled", False)]