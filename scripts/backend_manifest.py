"""Generate the Stage 6 backend manifest from customer decisions and verified outputs."""

import copy
import hashlib
import ipaddress
import json
import re
import os
import base64
import subprocess
from urllib.parse import urlencode, urlunsplit

import yaml

from scripts.backend_access import render_backend_access
from scripts.customer_migration import ROOT, configured, private_write, require, stage_fingerprint


def application_settings(config):
    settings = config.get("application", {})
    require(isinstance(settings, dict) and set(settings) == {"backendImage", "models"}, "application requires backendImage and models")
    registry = config["parameters"]["platform"]["containerRegistryName"].lower()
    require(isinstance(settings["backendImage"], str) and re.fullmatch(re.escape(registry) + r"\.azurecr\.io/[a-z0-9][a-z0-9/._-]*@sha256:[0-9a-f]{64}", settings["backendImage"]) is not None, "Backend image must pin a digest in the approved private ACR")
    require(isinstance(settings["models"], list) and bool(settings["models"]), "At least one approved model deployment is required")
    connections = config["parameters"]["platform"].get("azureOpenAIConnections", [])
    accounts = {item["alias"]: item for item in connections}
    require(len(accounts) == len(connections), "Model connection aliases must be unique")
    identities = set()
    for model in settings["models"]:
        require(isinstance(model, dict) and set(model) == {"modelGroup", "connectionAlias", "deploymentName", "id", "apiVersion"} and configured(model), "Invalid model deployment mapping")
        for key in ("modelGroup", "deploymentName", "id", "apiVersion"):
            require(isinstance(model[key], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model[key]) is not None, "Model mapping must contain plain identifiers")
        require(model["connectionAlias"] in accounts and model["id"] not in identities, "Model account is unapproved or deployment ID is duplicated")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{1,62}", accounts[model["connectionAlias"]]["accountName"]) is not None, "Invalid model account name")
        identities.add(model["id"])
    return settings


def render_backend_manifest(config, platform, versions, host, endpoint_subnet):
    settings = application_settings(config)
    access = render_backend_access(config, platform, versions)
    database = config["parameters"]["platform"]["stage5Data"]["postgresqlDatabaseName"]
    require(re.fullmatch(r"[a-z0-9-]+\.postgres\.database\.azure\.com", host) is not None and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", database) is not None, "Invalid deployed database host or name")
    redis = platform["managedRedisHostName"]
    require(re.fullmatch(r"[a-z0-9.-]+\.redis\.azure\.net", redis) is not None, "Unexpected managed Redis hostname")
    require(ipaddress.ip_network(endpoint_subnet).version == 4, "Private endpoint subnet must be IPv4")
    query = urlencode({"schema": "public", "sslmode": "require", "sslaccept": "strict", "sslcert": "/etc/ssl/certs/ca-certificates.crt", "connection_limit": "5", "pool_timeout": "10"})
    database_template = urlunsplit(("postgresql", "llmgw_app" + "@" + host + ":5432", "/" + database, query, ""))
    router = yaml.safe_load((ROOT / "deploy/components/stage6-ha/config-patch.yaml").read_text())["data"]["config.yaml"]
    runtime = yaml.safe_load(router)
    accounts = {item["alias"]: item for item in config["parameters"]["platform"]["azureOpenAIConnections"]}
    runtime["model_list"] = [{"model_name": model["modelGroup"], "litellm_params": {"model": "azure/" + model["deploymentName"], "api_base": "https://" + accounts[model["connectionAlias"]]["accountName"] + ".openai.azure.com", "api_version": model["apiVersion"]}, "model_info": {"id": model["id"]}} for model in settings["models"]]
    affinities = next(iter(runtime["router_settings"]["model_group_affinity_config"].values()))
    runtime["router_settings"]["model_group_affinity_config"] = {name: list(affinities) for name in sorted({model["modelGroup"] for model in settings["models"]})}
    runtime["general_settings"].update(disable_prisma_schema_update=True, store_model_in_db=False)
    config_text = yaml.safe_dump(runtime, sort_keys=False)
    config_name = "litellm-config-" + hashlib.sha256(config_text.encode()).hexdigest()[:12]
    config_map = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": config_name, "namespace": "litellm"}, "data": {"config.yaml": config_text}}
    deployment = yaml.safe_load((ROOT / "deploy/base/deployment.yaml").read_text())
    deployment["spec"].update(minReadySeconds=30, progressDeadlineSeconds=900)
    pod = deployment["spec"]["template"]["spec"]
    pod["terminationGracePeriodSeconds"] = 600
    pod["serviceAccountName"] = "litellm"
    deployment["spec"]["template"]["metadata"]["labels"]["azure.workload.identity/use"] = "true"
    container = pod["containers"][0]
    container["image"] = settings["backendImage"]
    container["env"] = [{"name": name, "value": value} for name, value in {"LLMGW_BACKEND_SECRETS_DIR": "/mnt/backend-secrets", "AZURE_DATABASE_URL_TEMPLATE": database_template, "REDIS_HOST": redis, "REDIS_PORT": "10000", "REDIS_USERNAME": platform["workloadIdentityPrincipalId"], "STORE_MODEL_IN_DB": "false"}.items()]
    container["lifecycle"]["preStop"]["exec"]["command"] = ["/bin/sh", "-c", "sleep 30"]
    container["volumeMounts"].extend(copy.deepcopy(access["deploymentPatch"]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]))
    pod["volumes"].extend(copy.deepcopy(access["deploymentPatch"]["spec"]["template"]["spec"]["volumes"]))
    pod["volumes"][0]["configMap"]["name"] = config_name
    policies = list(yaml.safe_load_all((ROOT / "deploy/components/stage4-network/networkpolicy.yaml").read_text()))
    traffic = policies[1]["spec"]
    traffic["ingress"] = [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "entra-auth-proxy", "plane": plane}}} for plane in ("api", "admin")], "ports": [{"protocol": "TCP", "port": 4000}]}]
    traffic["egress"][1]["to"][0]["ipBlock"]["cidr"] = endpoint_subnet
    documents = [yaml.safe_load((ROOT / "deploy/base" / name).read_text()) for name in ("namespace.yaml", "service.yaml", "pdb.yaml")]
    documents.extend([*access["resources"], config_map, deployment, *policies, yaml.safe_load((ROOT / "deploy/components/stage6-ha/hpa.yaml").read_text())])
    for document in documents:
        if document["kind"] != "Namespace":
            document["metadata"]["namespace"] = "litellm"
    return documents


def prepare_backend_documents(config, revision, directory, azure):
    from scripts.migration_deploy import deployment_name, group_id

    application_settings(config)
    group = config["target"]["resourceGroup"]
    platform_output = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 5, "platform"), "--query", "{state:properties.provisioningState,platform:properties.outputs.platform.value}"])
    require(platform_output.get("state") == "Succeeded" and platform_output.get("platform", {}).get("stage5Deployed") is True, "Deploy Stage5 platform before generating the backend")
    platform = platform_output["platform"]
    receipts = {}
    for component, output_name in (("schema-migrate", "databaseSchema"), ("backend-secrets", "backendSecrets")):
        result = azure.scoped(["deployment", "group", "show", "--resource-group", group, "--name", deployment_name(config, 5, component), "--query", "{state:properties.provisioningState,receipt:properties.outputs." + output_name + ".value}"])
        require(result.get("state") == "Succeeded", "Complete schema and backend secret initialization before generating the application")
        receipt = result["receipt"]
        require(receipt.get("revision") == revision and receipt.get("configSha256") == stage_fingerprint(config, 5), "Stage5 receipt is stale or belongs to different code/configuration")
        receipts[component] = receipt
    expected_server = group_id(config) + "/providers/Microsoft.DBforPostgreSQL/flexibleServers/" + platform["postgresqlServerName"]
    require(receipts["schema-migrate"]["serverId"].lower() == expected_server.lower() and receipts["schema-migrate"]["database"] == config["parameters"]["platform"]["stage5Data"]["postgresqlDatabaseName"], "Schema receipt targets a different database")
    require(receipts["backend-secrets"]["vaultId"].lower() == (group_id(config) + "/providers/Microsoft.KeyVault/vaults/" + platform["keyVaultName"]).lower(), "Secret receipt targets a different Vault")
    server = azure.scoped(["postgres", "flexible-server", "show", "--resource-group", group, "--name", platform["postgresqlServerName"], "--query", "{host:fullyQualifiedDomainName,auth:authConfig,network:network}"])
    require(server["auth"].get("activeDirectoryAuth") == "Enabled" and server["auth"].get("passwordAuth") == "Disabled" and server["network"].get("publicNetworkAccess") == "Disabled", "Backend requires private Entra-only PostgreSQL")
    network = config["parameters"]["platform"]["stage4Network"]
    subnet = azure.scoped(["network", "vnet", "subnet", "show", "--resource-group", group, "--vnet-name", network["virtualNetworkName"], "--name", network["privateEndpointSubnetName"], "--query", "addressPrefix"])
    foundation = config["parameters"].get("network", config["parameters"].get("backup", {}))
    if "privateEndpointSubnetPrefix" in foundation:
        require(subnet == foundation["privateEndpointSubnetPrefix"], "Deployed private endpoint subnet differs from approved configuration")
    verify_runtime_image(config, revision, directory, config["application"]["backendImage"], "azure")
    return render_backend_manifest(config, platform, receipts["backend-secrets"]["secrets"], server["host"], subnet)


def verify_runtime_image(config, revision, directory, image, runtime):
    from scripts.migration_runtime import run_command

    require(runtime in {"azure", "auth-proxy"}, "Unapproved runtime signature type")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF", "")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is not None and branch.startswith("refs/heads/"), "Managed backend publishing requires its protected GitHub repository identity")
    identity = "https://github.com/" + repository + "/.github/workflows/promote-litellm-image.yml@" + branch
    registry = config["parameters"]["platform"]["containerRegistryName"]
    token = subprocess.run(["az", "acr", "login", "--name", registry, "--expose-token", "--subscription", config["azure"]["subscriptionId"], "--output", "json", "--only-show-errors"], capture_output=True, text=True, check=False, timeout=120)
    require(token.returncode == 0, "Cannot obtain scoped ACR authentication for image verification")
    credentials = json.loads(token.stdout)
    require(credentials["loginServer"].lower() == (registry + ".azurecr.io").lower(), "Unexpected ACR login server")
    auth_directory = directory / "backend-acr-auth"
    auth_directory.mkdir(mode=0o700, exist_ok=True)
    auth_directory.chmod(0o700)
    auth_file = auth_directory / "config.json"
    encoded = base64.b64encode(("00000000-0000-0000-0000-000000000000:" + credentials["accessToken"]).encode()).decode()
    private_write(auth_file, json.dumps({"auths": {credentials["loginServer"]: {"auth": encoded}}}))
    try:
        run_command(["cosign", "verify", "--certificate-identity", identity, "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com", "-a", "llmgw.runtime=" + runtime, "-a", "llmgw.revision=" + revision, "-a", "llmgw.environment=" + config["environment"], image], directory, runtime + "-image-signature", environment={**os.environ, "DOCKER_CONFIG": str(auth_directory)})
    finally:
        auth_file.unlink(missing_ok=True)