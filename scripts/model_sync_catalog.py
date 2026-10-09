"""Resource-explicit nested catalog parsing and additive deployment reconciliation."""

import copy
import hashlib
import json
import re
from uuid import UUID

from scripts.backend_manifest import application_settings
from scripts.customer_migration import MigrationError, private_write, require
from scripts.model_configuration import (
    azure_base_model, connection_endpoint, identifier, normalize_openai_endpoint, rendered_models, validate_options,
)


ROLE = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
ACCOUNT_API = "2024-10-01"
NETWORK_API = "2024-07-01"
DNS_API = "2024-06-01"
IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"


def plain(value, expression=IDENTIFIER):
    require(isinstance(value, str) and re.fullmatch(expression, value) is not None,
            "Catalog identifiers must be plain, non-empty identifiers")
    return value


def uuid(value):
    require(isinstance(value, str), "Subscription must be a UUID")
    try:
        parsed = UUID(value)
    except ValueError:
        raise MigrationError("Subscription must be a valid UUID") from None
    require(parsed.int != 0, "Subscription must be a nonzero UUID")
    return str(parsed)


def account_id(subscription, group, name):
    return f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.CognitiveServices/accounts/{name}"


def parse_catalog(document, api_version=None):
    require(isinstance(document, dict), "Catalog must be a JSON object")
    require(not document.keys() & {"azure-openai-list", "deployment_list", "apim_name", "apim_resource_group", "region"},
            "Legacy catalog is no longer supported; migrate to schema_version/subscriptions/resources/models")
    require(set(document) == {"schema_version", "subscriptions"} and type(document["schema_version"]) is int
            and document["schema_version"] == 1, "Catalog requires exactly schema_version=1 and subscriptions")
    if api_version is not None:
        identifier(api_version)
    subscriptions = document["subscriptions"]
    require(isinstance(subscriptions, list) and subscriptions, "Catalog requires nonempty subscriptions")
    accounts, subscription_ids, resource_ids = [], set(), set()
    for subscription in subscriptions:
        require(isinstance(subscription, dict) and set(subscription) == {"subscription_id", "resources"},
                "Subscription requires exactly subscription_id and resources")
        subscription_id = uuid(subscription["subscription_id"])
        require(subscription_id not in subscription_ids, "Duplicate subscription_id")
        subscription_ids.add(subscription_id)
        resources = subscription["resources"]
        require(isinstance(resources, list) and resources, "Subscription resources must be nonempty")
        for resource in resources:
            require(isinstance(resource, dict), "Catalog resource must be an object")
            require("account_name" not in resource,
                    "Catalog account_name is no longer supported; migrate to name (the Azure ARM resource name)")
            require({"resource_group", "name", "models"} <= resource.keys()
                    and not resource.keys() - {"resource_group", "name", "models", "endpoint"},
                    "Resource requires resource_group, name and models with optional expected endpoint only")
            name = plain(resource["name"], r"[A-Za-z0-9][A-Za-z0-9-]{1,62}").lower()
            group = plain(resource["resource_group"], r"[A-Za-z0-9][A-Za-z0-9._()-]{0,89}")
            identity = account_id(subscription_id, group, name)
            require(identity.lower() not in resource_ids, "Duplicate resource identity")
            resource_ids.add(identity.lower())
            models = resource["models"]
            require(isinstance(models, list) and models, "Each resource must explicitly map at least one model")
            targets, exact = [], set()
            for model in models:
                require(isinstance(model, dict) and {"model_name", "deployment_name"} <= model.keys()
                        and not model.keys() - {"model_name", "deployment_name", "litellm_params", "model_info"},
                        "Model requires model_name/deployment_name with optional litellm_params/model_info only")
                alias, deployment = plain(model["model_name"]), plain(model["deployment_name"])
                require((alias, deployment) not in exact, "Duplicate exact model mapping in resource")
                exact.add((alias, deployment))
                params, info = copy.deepcopy(model.get("litellm_params", {})), copy.deepcopy(model.get("model_info", {}))
                validate_options(params, info, catalog=True)
                version = params.pop("api_version", api_version)
                require(version is not None, "Each model requires litellm_params.api_version or explicit --api-version fallback")
                target = {"modelGroup": alias, "deploymentName": deployment, "apiVersion": identifier(version),
                          "litellmParams": params, "modelInfo": info}
                if "base_model" in info:
                    target["baseModel"] = azure_base_model(info.pop("base_model"))
                targets.append(target)
            account = {"accountResourceId": identity, "accountName": name, "subscriptionId": subscription_id,
                       "resourceGroupName": group, "models": targets}
            if "endpoint" in resource:
                account["expectedEndpoint"] = normalize_openai_endpoint(resource["endpoint"])
            accounts.append(account)
    return accounts


def discover(config, accounts, azure):
    observations, matches, missing = [], [], []
    for subscription in sorted({item["subscriptionId"] for item in accounts}):
        context = azure.run(["account", "show", "--subscription", subscription, "--query", "{id:id,tenantId:tenantId}"])
        require(context.get("id", "").lower() == subscription.lower()
                and context.get("tenantId", "").lower() == config["azure"]["tenantId"].lower(),
                "Cross-tenant Azure OpenAI onboarding is forbidden")
    for requested in accounts:
        account = {key: value for key, value in requested.items() if key not in {"models", "expectedEndpoint"}}
        live = azure.run(["resource", "show", "--ids", account["accountResourceId"], "--api-version", ACCOUNT_API])
        properties = live.get("properties", {})
        require(live.get("id", "").lower() == account["accountResourceId"].lower()
                and live.get("kind") in {"OpenAI", "AIServices"} and properties.get("provisioningState") == "Succeeded",
                "Account must be an existing Succeeded Azure OpenAI/Foundry AI Services resource")
        hostname = plain(properties.get("customSubDomainName"), r"[A-Za-z0-9][A-Za-z0-9-]{1,62}").lower()
        if live["kind"] == "OpenAI":
            endpoint = properties.get("endpoint")
        else:
            endpoints = properties.get("endpoints", {})
            require(isinstance(endpoints, dict), "Foundry account must publish an explicit OpenAI endpoint")
            candidates = {normalize_openai_endpoint(endpoints[key])
                          for key in ("OpenAI", "OpenAI Language Model Instance API") if key in endpoints}
            require(len(candidates) == 1, "Foundry account requires one unambiguous explicit OpenAI endpoint")
            endpoint = candidates.pop()
        account["endpoint"] = connection_endpoint({**account, "endpoint": endpoint})
        require(account["endpoint"] == f"https://{hostname}.openai.azure.com",
                "Live OpenAI endpoint must exactly match the account customSubDomainName")
        if "expectedEndpoint" in requested:
            require(requested["expectedEndpoint"] == account["endpoint"],
                    "Provided endpoint differs from the authoritative ARM OpenAI endpoint for " + account["accountResourceId"])
        deployments = azure.run(["rest", "--method", "get", "--url",
                                 "https://management.azure.com" + account["accountResourceId"]
                                 + "/deployments?api-version=" + ACCOUNT_API])
        require(isinstance(deployments.get("value"), list) and not deployments.get("nextLink"),
                "Unexpected or paginated deployment response; complete discovery required")
        names = {}
        for deployment in deployments["value"]:
            name = deployment.get("name")
            require(isinstance(name, str) and name not in names, "Duplicate or invalid live deployment")
            names[name] = deployment
        selected, unmatched = [], []
        for target in requested["models"]:
            name = target["deploymentName"]
            if name not in names:
                missing.append(account["accountResourceId"] + "/deployments/" + name)
                unmatched.append(name)
                continue
            deployment = names[name]
            props, metadata = deployment.get("properties", {}), deployment.get("properties", {}).get("model", {})
            require(props.get("provisioningState") == "Succeeded" and metadata.get("format") == "OpenAI",
                    "Requested deployment must be Succeeded and use the OpenAI model format")
            actual = azure_base_model(metadata.get("name"))
            require(isinstance(metadata.get("version"), str) and bool(metadata["version"]),
                    "Requested deployment must expose its actual model version")
            require(deployment.get("id", "").lower() == (account["accountResourceId"] + "/deployments/" + name).lower(),
                    "Live deployment ID does not match the explicitly mapped resource")
            selected.append(deployment)
            matches.append((account, {**target, "actualBaseModel": actual}))
        observations.append({"account": account, "live": live, "deployments": selected,
                             **({"expectedEndpoint": requested["expectedEndpoint"]} if "expectedEndpoint" in requested else {}),
                             "matchedDeployments": [item["name"] for item in selected], "missingDeployments": unmatched,
                             "status": "missing-explicit-deployment" if unmatched else "matched"})
    if getattr(azure, "directory", None) is not None:
        private_write(azure.directory / "discovery.json", json.dumps(observations, indent=2))
    require(not missing, "Requested deployment missing from its explicitly mapped resource: " + ", ".join(missing))
    for group in {target["modelGroup"] for _, target in matches}:
        require(len({m["actualBaseModel"] for _, m in matches if m["modelGroup"] == group}) == 1,
                "Load-balanced deployments must have the same actual base model family")
    return observations, matches


def reconcile(config, matches, model_policy="replace"):
    require(model_policy in {"merge", "replace"}, "Model policy must be merge or replace")
    require(matches, "Model synchronization requires at least one discovered model")
    application_settings(config)
    desired = copy.deepcopy(config)
    connections = desired["parameters"]["platform"]["azureOpenAIConnections"]
    aliases, identities = set(), set()
    for connection in connections:
        require(connection["alias"] not in aliases and connection["accountResourceId"].lower() not in identities,
                "Existing connection aliases/resource IDs must be unique")
        aliases.add(connection["alias"])
        identities.add(connection["accountResourceId"].lower())
        require(account_id(connection["subscriptionId"], connection["resourceGroupName"], connection["accountName"]).lower()
                == connection["accountResourceId"].lower(), "Existing connection identity is inconsistent")
    mappings = desired["application"]["models"]
    selected = set()
    require(len({(m["modelGroup"], m["connectionAlias"], m["deploymentName"]) for m in mappings}) == len(mappings),
            "Existing model mapping identities are duplicated")
    for account, target in matches:
        connection = next((c for c in connections if c["accountResourceId"].lower() == account["accountResourceId"].lower()), None)
        if connection is None:
            alias = "aoai-" + hashlib.sha256(account["accountResourceId"].lower().encode()).hexdigest()[:16]
            require(alias not in aliases, "Generated connection alias collides with existing configuration")
            connection = {**account, "alias": alias}
            connections.append(connection)
            aliases.add(alias)
        elif connection_endpoint(connection) != account["endpoint"]:
            connection["endpoint"] = account["endpoint"]
        alias = connection["alias"]
        selected.add((target["modelGroup"], alias, target["deploymentName"]))
        exact = next((m for m in mappings if (m["modelGroup"], m["connectionAlias"], m["deploymentName"]) ==
                      (target["modelGroup"], alias, target["deploymentName"])), None)
        if exact is None:
            identity = "sync-" + hashlib.sha256(
                (target["modelGroup"] + "|" + account["accountResourceId"].lower() + "|" + target["deploymentName"]).encode()
            ).hexdigest()[:24]
            require(identity not in {m["id"] for m in mappings}, "Generated model ID collides with existing configuration")
            exact = {"modelGroup": target["modelGroup"], "deploymentName": target["deploymentName"],
                     "connectionAlias": alias, "id": identity}
            mappings.append(exact)
        exact["apiVersion"] = target["apiVersion"]
        if "baseModel" in target:
            exact["baseModel"] = target["baseModel"]
        elif "baseModel" not in exact:
            exact["baseModel"] = target["actualBaseModel"]
        for key in ("litellmParams", "modelInfo"):
            if target[key]:
                exact.setdefault(key, {}).update(copy.deepcopy(target[key]))
        validate_options(exact.get("litellmParams", {}), exact.get("modelInfo", {}))
    if model_policy == "replace":
        desired["application"]["models"] = [
            mapping for mapping in mappings
            if (mapping["modelGroup"], mapping["connectionAlias"], mapping["deploymentName"]) in selected
        ]
    application_settings(desired)
    return desired


def model_list(config):
    application_settings(config)
    return rendered_models(config)
