"""Render proxy trust and per-subject credential contracts from customer decisions."""

import hashlib
import json
import re
from uuid import UUID

from scripts.customer_migration import ROOT, require
from scripts.render_stage7_domain import domain_hosts


def object_id(value):
    require(isinstance(value, str), "Entra identity must be a UUID string")
    parsed = UUID(value)
    require(parsed.int != 0, "Entra identity cannot be zero")
    return str(parsed)


def proxy_settings(config):
    settings = config.get("proxy", {})
    require(isinstance(settings, dict) and {"apiClientIds", "bindings"}.issubset(settings) and not set(settings) - {"apiClientIds", "bindings", "image"}, "proxy requires apiClientIds and bindings, with an optional approved image")
    if "image" in settings:
        registry = config["parameters"]["platform"]["containerRegistryName"].lower()
        require(isinstance(settings["image"], str) and re.fullmatch(re.escape(registry) + r"\.azurecr\.io/[a-z0-9][a-z0-9/._-]*@sha256:[0-9a-f]{64}", settings["image"]) is not None, "Proxy image must pin a digest in the approved ACR")
    clients = settings["apiClientIds"]
    require(isinstance(clients, list) and clients and len(clients) == len({object_id(value) for value in clients}), "API client IDs must be nonempty and unique")
    bindings = settings["bindings"]
    require(isinstance(bindings, list) and bindings, "Explicit proxy identity bindings are required")
    model_groups = {item["modelGroup"] for item in config.get("application", {}).get("models", [])}
    identities = set()
    for binding in bindings:
        require(isinstance(binding, dict) and not set(binding) - {"oid", "plane", "role", "models", "disabled", "auditTeamId", "principalType", "clientIds"}, "Unexpected proxy binding fields")
        identity = (binding.get("plane"), object_id(binding.get("oid")))
        require(identity[0] in {"api", "admin"} and identity not in identities, "Invalid plane or duplicate identity binding")
        identities.add(identity)
        principal_type = binding.get("principalType", "User")
        require(principal_type in {"User", "ServicePrincipal"} and (identity[0] != "admin" or principal_type == "User"), "Admin bindings require users; API bindings require User or ServicePrincipal")
        if "clientIds" in binding:
            selected = binding["clientIds"]
            require(identity[0] == "api" and principal_type == "User" and isinstance(selected, list) and selected and len({object_id(value) for value in selected}) == len(selected) and {object_id(value) for value in selected}.issubset({object_id(value) for value in clients}), "Delegated binding clientIds must be a nonempty subset of approved API clients")
        require(type(binding.get("disabled", False)) is bool, "Binding disabled flag must be boolean")
        if binding.get("role") == "audit_reader":
            require(identity[0] == "admin" and not binding.get("models") and "auditTeamId" not in binding, "Audit readers must not carry backend model privileges")
            continue
        require(binding.get("role") in {"internal_user", "proxy_admin_viewer", "proxy_admin"} and (identity[0] == "api") == (binding["role"] == "internal_user"), "Proxy role does not match its plane")
        models = binding.get("models")
        require(isinstance(models, list) and models and all(isinstance(model, str) for model in models) and len(set(models)) == len(models) and set(models).issubset(model_groups), "Binding models must explicitly reference approved model groups")
        if "auditTeamId" in binding:
            require(identity[0] == "api" and isinstance(binding["auditTeamId"], str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}", binding["auditTeamId"]) is not None, "Invalid trusted audit team mapping")
    require(any(item["plane"] == "api" for item in bindings), "At least one explicit API binding is required; all may be disabled for emergency isolation")
    require(any(item["plane"] == "admin" and not item.get("disabled") for item in bindings), "At least one enabled admin binding is required")
    return settings


def credential_name(tenant, binding):
    identity = ":".join((object_id(tenant), binding["plane"], object_id(binding["oid"])))
    return "key-" + hashlib.sha256(identity.encode()).hexdigest()[:48]


def proxy_policy(config, applications):
    settings = proxy_settings(config)
    api_client = object_id(applications["api"]["appId"])
    admin_client = object_id(applications["admin"]["appId"])
    require(api_client != admin_client, "API and admin Entra applications must be distinct")
    hosts = domain_hosts(config["baseDomain"])
    bindings = []
    for item in settings["bindings"]:
        binding = {"oid": object_id(item["oid"]), "plane": item["plane"], "role": item["role"], "disabled": item.get("disabled", False), "principalType": item.get("principalType", "User")}
        if "clientIds" in item:
            binding["clientIds"] = [object_id(value) for value in item["clientIds"]]
        if item["role"] != "audit_reader":
            binding.update(models=list(item["models"]), keyFile=credential_name(config["azure"]["tenantId"], item))
        if "auditTeamId" in item:
            binding["audit"] = {"capture": True, "teamId": item["auditTeamId"]}
        bindings.append(binding)
    return {"tenantId": object_id(config["azure"]["tenantId"]), "apiHost": hosts["api"], "adminHost": hosts["admin"], "apiAudience": api_client, "apiClientIds": [object_id(value) for value in settings["apiClientIds"]], "adminClientId": admin_client, "bindings": bindings}


def entra_documents(config, api_app_id=None):
    proxy_settings(config)
    result = {plane: json.loads((ROOT / f"auth-proxy/entra/{plane}-application.json").read_text()) for plane in ("api", "admin")}
    for plane, document in result.items():
        document["displayName"] = f"llmgw-{plane}-{config['environment']}"
    result["admin"]["web"]["redirectUris"] = ["https://" + domain_hosts(config["baseDomain"])["admin"] + "/auth/callback"]
    if api_app_id:
        result["api"]["identifierUris"] = ["api://" + object_id(api_app_id)]
    return result