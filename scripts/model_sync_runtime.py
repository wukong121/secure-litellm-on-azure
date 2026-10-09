"""Model-only optimistic Kubernetes updates; never reapplies application manifests."""

import copy
import hashlib
import json
import re
import subprocess
from urllib.parse import urlsplit

import yaml

from scripts.customer_migration import fingerprint, private_write, require
from scripts.migration_runtime import connect_cluster
from scripts.model_sync_catalog import model_list
from scripts.model_configuration import COST_FIELDS, connection_endpoint, finite_number


def command(kube, arguments, timeout=120):
    result = subprocess.run([*kube, *arguments], capture_output=True, text=True, check=False, timeout=timeout)
    # Do not persist generic kubectl output: admission errors and Pod env can contain credentials.
    require(result.returncode == 0, "Model-sync Kubernetes operation failed (" + arguments[0]
            + "); check private connectivity, Kubernetes RBAC, admission policy or rollout events")
    return result.stdout


IDENTITY_SWITCH_PATHS = frozenset({
    ("litellm_settings", "enable_azure_ad_token_refresh"),
    ("router_settings", "cache_kwargs", "azure_redis_ad_token"),
})


def safe_settings(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            location = (*path, str(key))
            sensitive = re.search(r"(?:^|_)(?:api_key|master_key|password|token|secret)(?:$|_)", str(key).lower())
            if location in IDENTITY_SWITCH_PATHS:
                require(item is True, "Live runtime identity authentication switch must be true: "
                        + ".".join(location))
            elif key in COST_FIELDS:
                finite_number(item)
            elif sensitive:
                require(isinstance(item, str) and item.startswith("os.environ/"),
                        "Live runtime configuration contains a credential literal or unsupported credential source")
            else:
                safe_settings(item, location)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            safe_settings(item, (*path, str(index)))
    elif isinstance(value, str) and "://" in value:
        require(urlsplit(value).password is None, "Live runtime configuration contains a credential-bearing URL")


def identity_context(config, azure):
    from scripts.migration_deploy import group_id
    from scripts.model_sync_infra import collection, get
    identity_id = group_id(config) + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-litellm-workload-" + config["environment"]
    identity = get(azure, identity_id, "2023-01-31")
    require(identity.get("id", "").lower() == identity_id.lower()
            and identity.get("properties", {}).get("tenantId", "").lower() == config["azure"]["tenantId"].lower(),
            "Existing workload identity must belong to the customer tenant")
    aks_id = group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"]
    aks = get(azure, aks_id, "2024-10-01")
    issuer = aks.get("properties", {}).get("oidcIssuerProfile", {}).get("issuerURL")
    require(isinstance(issuer, str) and issuer.startswith("https://")
            and aks.get("properties", {}).get("securityProfile", {}).get("workloadIdentity", {}).get("enabled") is True,
            "Target AKS must retain OIDC workload identity")
    federations = collection(azure, identity_id + "/federatedIdentityCredentials", "2023-01-31")
    valid = [item for item in federations if item.get("properties", {}).get("issuer") == issuer
             and item.get("properties", {}).get("subject") == "system:serviceaccount:litellm:litellm"
             and item.get("properties", {}).get("audiences") == ["api://AzureADTokenExchange"]]
    require(len(valid) == 1, "Workload identity federation must match the existing target service account")
    return {"identity": identity, "aks": {"id": aks["id"], "issuer": issuer}, "federation": valid[0]}


def baseline(config, identity, kube):
    deployment = json.loads(command(kube, ["get", "deployment", "litellm", "-o", "json"]))
    pod = deployment["spec"]["template"]
    require(pod["metadata"].get("labels", {}).get("azure.workload.identity/use") == "true"
            and pod["spec"].get("serviceAccountName") == "litellm", "Live backend must retain workload identity")
    account = json.loads(command(kube, ["get", "serviceaccount", "litellm", "-o", "json"]))
    require(account["metadata"].get("annotations", {}).get("azure.workload.identity/client-id") ==
            identity["properties"]["clientId"], "Live service account selects another workload identity")
    containers = pod["spec"]["containers"]
    selected = [c for c in containers if c["name"] == "litellm"]
    require(len(selected) == 1 and selected[0].get("image") == config["application"]["backendImage"],
            "Live backend image differs from approved customer config")
    container = selected[0]
    environment = {item["name"]: item for item in container.get("env", [])}
    require(environment.get("STORE_MODEL_IN_DB", {}).get("value") == "false",
            "Model-sync requires file-backed models, not database-backed model storage")
    for name, item in environment.items():
        if any(word in name for word in ("KEY", "TOKEN", "PASSWORD", "SECRET")) and "value" in item:
            require(name.endswith("_DIR"), "Live Deployment contains unsupported inline credential environment")
    mounts = [mount for mount in container.get("volumeMounts", []) if mount.get("mountPath") == "/app/config/config.yaml"]
    require(len(mounts) == 1 and mounts[0].get("subPath") == "config.yaml" and mounts[0].get("readOnly") is True,
            "Model config must be the existing read-only config.yaml mount")
    require(container.get("args", [])[:2] == ["--config", "/app/config/config.yaml"],
            "Backend must use the reviewed configuration file")
    volume_index = next((i for i, volume in enumerate(pod["spec"].get("volumes", []))
                         if volume["name"] == mounts[0]["name"]), None)
    require(volume_index is not None, "Live model config volume is missing")
    volume = pod["spec"]["volumes"][volume_index]
    require("configMap" in volume, "Live model config must come from a ConfigMap")
    config_map = json.loads(command(kube, ["get", "configmap", volume["configMap"]["name"], "-o", "json"]))
    require(set(config_map.get("data", {})) == {"config.yaml"} and not config_map.get("binaryData"),
            "Model ConfigMap must contain only config.yaml")
    runtime = yaml.safe_load(config_map["data"]["config.yaml"])
    require(isinstance(runtime, dict), "Live runtime YAML must be an object")
    safe_settings(runtime)
    require(runtime.get("general_settings", {}).get("store_model_in_db") is False
            and runtime.get("litellm_settings", {}).get("enable_azure_ad_token_refresh") is True,
            "Live runtime must retain file-backed models and Azure AD token refresh")
    expected = model_list(config)
    require(sorted(runtime.get("model_list", []), key=lambda item: item["model_info"]["id"]) ==
            sorted(expected, key=lambda item: item["model_info"]["id"]),
            "Live model YAML differs from customer mapping; reconcile baseline drift before planning")
    return {"deploymentUid": deployment["metadata"]["uid"],
            "deploymentResourceVersion": deployment["metadata"]["resourceVersion"],
            "deploymentSpecSha256": fingerprint(deployment["spec"]),
            "serviceAccountUid": account["metadata"]["uid"],
            "serviceAccountSha256": fingerprint({"annotations": account["metadata"].get("annotations"), "spec": account.get("spec")}),
            "configMapUid": config_map["metadata"]["uid"], "configMapName": config_map["metadata"]["name"],
            "configMapSha256": fingerprint(config_map["data"]), "volumeIndex": volume_index,
            "runtime": runtime, "deployment": deployment}


def render(desired, current):
    runtime = copy.deepcopy(current["runtime"])
    runtime["model_list"] = model_list(desired)
    affinities = runtime.get("router_settings", {}).get("model_group_affinity_config")
    if affinities is not None:
        require(isinstance(affinities, dict) and bool(affinities), "Existing model affinity template is required")
        prototype = copy.deepcopy(next(iter(affinities.values())))
        for name in {m["modelGroup"] for m in desired["application"]["models"]}:
            affinities.setdefault(name, copy.deepcopy(prototype))
    text = yaml.safe_dump(runtime, sort_keys=False)
    name = "litellm-config-" + hashlib.sha256(text.encode()).hexdigest()[:12]
    config_map = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": "litellm"},
                  "immutable": True, "data": {"config.yaml": text}}
    pointer = f"/spec/template/spec/volumes/{current['volumeIndex']}/configMap/name"
    patch = [{"op": "test", "path": "/metadata/uid", "value": current["deploymentUid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": current["deploymentResourceVersion"]},
             {"op": "test", "path": pointer, "value": current["configMapName"]},
             {"op": "replace", "path": pointer, "value": name}]
    model_changed = runtime != current["runtime"]
    return {"configMap": config_map, "patch": patch, "modelChanged": model_changed,
            "runtimeSha256": fingerprint(runtime)}


def dry_run(kube, payload, directory):
    private_write(directory / "configmap.json", json.dumps(payload["configMap"], indent=2))
    private_write(directory / "deployment-patch.json", json.dumps(payload["patch"], indent=2))
    if not payload["modelChanged"]:
        return
    command(kube, ["apply", "--server-side", "--field-manager=llmgw-migration", "--dry-run=server",
                   "-f", str(directory / "configmap.json"), "-o", "json"])
    output = json.loads(command(kube, ["patch", "deployment", "litellm", "--type=json",
                                      "--patch-file", str(directory / "deployment-patch.json"), "--dry-run=server", "-o", "json"]))
    expected = copy.deepcopy(payload["_baselineSpec"])
    expected["template"]["spec"]["volumes"][payload["_volumeIndex"]]["configMap"]["name"] = payload["configMap"]["metadata"]["name"]
    require(output.get("spec") == expected, "Server dry-run changes more than the model config volume")


def network_probe(kube, account, ips):
    # Uses only the already running, reviewed backend image, without tokens or HTTP inference.
    code = (
        "import json,socket,ssl,sys; "
        "host=sys.argv[1]; expected=set(json.loads(sys.argv[2])); "
        "actual={r[4][0] for r in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)}; "
        "assert actual and actual<=expected,'private DNS mismatch'; "
        "s=socket.create_connection((host,443),timeout=10); "
        "tls=ssl.create_default_context().wrap_socket(s,server_hostname=host); tls.close(); print('private-dns-tls-ok')"
    )
    output = command(kube, ["exec", "deployment/litellm", "-c", "litellm", "--", "python", "-c", code,
                            connection_endpoint(account).removeprefix("https://"), json.dumps(ips)], timeout=45)
    require(output.strip() == "private-dns-tls-ok", "In-cluster private DNS/TLS check did not complete")


def apply(kube, payload, current, directory, record):
    if payload["modelChanged"]:
        fresh = json.loads(command(kube, ["get", "deployment", "litellm", "-o", "json"]))
        require(fresh["metadata"]["uid"] == current["deploymentUid"]
                and fresh["metadata"]["resourceVersion"] == current["deploymentResourceVersion"]
                and fingerprint(fresh["spec"]) == current["deploymentSpecSha256"],
                "Deployment drifted after approval; no application writes performed")
        command(kube, ["apply", "--server-side", "--field-manager=llmgw-migration",
                       "-f", str(directory / "configmap.json")])
        record["phase"] = "configmap-created"
        private_write(directory / "model-sync-state.json", json.dumps(record, indent=2))
        command(kube, ["patch", "deployment", "litellm", "--type=json",
                       "--patch-file", str(directory / "deployment-patch.json")])
        record["applicationPatched"] = True
        record["phase"] = "rollout"
        private_write(directory / "model-sync-state.json", json.dumps(record, indent=2))
    command(kube, ["rollout", "status", "deployment/litellm", "--timeout=900s"], timeout=930)
    deployment = json.loads(command(kube, ["get", "deployment", "litellm", "-o", "json"]))
    expected = copy.deepcopy(current["deployment"]["spec"])
    wanted_name = payload["configMap"]["metadata"]["name"] if payload["modelChanged"] else current["configMapName"]
    expected["template"]["spec"]["volumes"][current["volumeIndex"]]["configMap"]["name"] = wanted_name
    require(deployment["metadata"]["uid"] == current["deploymentUid"] and deployment["spec"] == expected,
            "Post-rollout Deployment differs from the exact model-only patch")
    config_map = json.loads(command(kube, ["get", "configmap", wanted_name, "-o", "json"]))
    require(fingerprint(yaml.safe_load(config_map["data"]["config.yaml"])) == payload["runtimeSha256"],
            "Post-rollout ConfigMap differs from desired exact model mapping")
    # Check each ready backend Pod actually mounted this YAML, not just the control-plane object.
    selector = ",".join(k + "=" + v for k, v in deployment["spec"]["selector"]["matchLabels"].items())
    pods = json.loads(command(kube, ["get", "pods", "-l", selector, "-o", "json"]))["items"]
    ready = [p for p in pods if not p["metadata"].get("deletionTimestamp")
             and any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", []))]
    require(len(ready) >= deployment["spec"].get("replicas", 1), "Not all required backend replicas are ready")
    code = "import hashlib; print(hashlib.sha256(open('/app/config/config.yaml','rb').read()).hexdigest())"
    text_hash = hashlib.sha256(config_map["data"]["config.yaml"].encode()).hexdigest()
    for pod in ready:
        actual = command(kube, ["exec", pod["metadata"]["name"], "-c", "litellm", "--", "python", "-c", code], timeout=45)
        require(actual.strip() == text_hash, "Ready backend Pod mounted stale model configuration")
    record["rolloutVerified"] = True
    record["inferenceVerified"] = False
