"""Plan, deploy and verify the managed private gateways on a private runner."""

import base64
import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlsplit

import yaml

from scripts.customer_migration import MigrationError, fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id
from scripts.migration_runtime import connect_cluster, run_command
from scripts.private_ingress import certificate_material, ingress_image, ingress_settings, render_ingress
from scripts.render_stage7_domain import domain_hosts


def read_certificate(config, secret_id, host):
    result = subprocess.run(["az", "keyvault", "secret", "show", "--id", secret_id, "--subscription", config["azure"]["subscriptionId"], "--only-show-errors", "--output", "json"], capture_output=True, text=True, check=False, timeout=120)
    require(result.returncode == 0, "Cannot read the configured TLS secret; check private access and Key Vault authorization")
    secret = json.loads(result.stdout)
    require(secret.get("attributes", {}).get("enabled") is True, "TLS secret is disabled")
    requested, returned = urlsplit(secret_id), urlsplit(secret["id"])
    require(returned.scheme == requested.scheme and returned.netloc == requested.netloc and not returned.query and not returned.fragment, "TLS secret returned from an unexpected Vault")
    expected_path = requested.path.split("/")
    actual_path = returned.path.split("/")
    require(len(actual_path) == 4 and actual_path[:3] == expected_path[:3] and len(actual_path[3]) == 32 and all(character in "0123456789abcdef" for character in actual_path[3]), "TLS secret must resolve to the configured secret and a concrete version")
    require(len(expected_path) == 3 or actual_path == expected_path, "TLS secret version differs from the requested version")
    try:
        material = certificate_material(secret["value"], host)
    except Exception:
        raise MigrationError("TLS PEM failed certificate, key, hostname or validity checks") from None
    material["secretId"] = secret["id"]
    return material


def promote_image(config, directory, azure, lock):
    source = f"{lock['source']}@{lock['digest']}"
    registry = config["parameters"]["platform"]["containerRegistryName"]
    target = f"{registry}.azurecr.io/{lock['repository']}"
    run_command(["trivy", "image", "--exit-code", "1", "--severity", "CRITICAL", "--platform", "linux/amd64", source], directory, "ingress-image-scan")
    run_command(["syft", source, "--platform", "linux/amd64", "--output", f"spdx-json={directory / 'ingress-sbom.spdx.json'}"], directory, "ingress-image-sbom")
    token = subprocess.run(["az", "acr", "login", "--name", registry, "--expose-token", "--subscription", config["azure"]["subscriptionId"], "--only-show-errors", "--output", "json"], capture_output=True, text=True, check=False, timeout=120)
    require(token.returncode == 0, "Unable to obtain scoped ACR login token")
    credentials = json.loads(token.stdout)
    require(credentials["loginServer"].lower() == f"{registry}.azurecr.io".lower(), "Unexpected ACR login server")
    authfile = directory / "registry-auth.json"
    encoded = base64.b64encode(("00000000-0000-0000-0000-000000000000:" + credentials["accessToken"]).encode()).decode()
    private_write(authfile, json.dumps({"auths": {credentials["loginServer"]: {"auth": encoded}}}))
    try:
        run_command(["skopeo", "copy", "--all", "--preserve-digests", "--authfile", str(authfile), "docker://" + source, "docker://" + target + ":sha256-" + lock["digest"].split(":")[1]], directory, "ingress-image-copy")
        digest = azure.scoped(["acr", "repository", "show", "--name", registry, "--image", lock["repository"] + "@" + lock["digest"], "--query", "digest"])
        require(digest == lock["digest"], "Promoted ingress image digest mismatch")
    finally:
        authfile.unlink(missing_ok=True)


def verify_endpoint(address, host, other_host, expected_sha256):
    context = ssl.create_default_context()
    with socket.create_connection((address, 443), timeout=15) as connection:
        with context.wrap_socket(connection, server_hostname=host) as secured:
            require(hashlib.sha256(secured.getpeercert(binary_form=True)).hexdigest() == expected_sha256, "Ingress served an unexpected TLS certificate")
            secured.sendall(f"GET / HTTP/1.1\r\nHost: {other_host}\r\nConnection: close\r\n\r\n".encode("ascii"))
            response = http.client.HTTPResponse(secured)
            response.begin()
            require(response.status in {404, 421}, "Private ingress did not reject the other plane's host")


def frontend_for(config, azure, node_group, address):
    network = config["parameters"]["platform"]["stage4Network"]
    subnet_id = group_id(config) + "/providers/Microsoft.Network/virtualNetworks/" + network["virtualNetworkName"] + "/subnets/" + network["ingressSubnetName"]
    matches = []
    for candidate in azure.scoped(["network", "lb", "list", "--resource-group", node_group]):
        if candidate.get("sku", {}).get("name") != "Standard":
            continue
        for frontend in candidate.get("frontendIPConfigurations", []):
            if frontend.get("privateIPAddress") == address and not frontend.get("publicIPAddress") and frontend.get("subnet", {}).get("id", "").lower() == subnet_id.lower():
                matches.append({"resourceGroupName": node_group, "name": candidate["name"], "frontendName": frontend["name"], "privateIpAddress": address})
    require(len(matches) == 1, "Private ingress IP must map to exactly one Standard LB frontend in the approved ingress subnet")
    return matches[0]


def deploy_private_ingress(config, operation, revision, directory, approved):
    require(operation in {"plan", "execute"}, "Invalid private ingress operation")
    settings = ingress_settings(config)
    image, lock = ingress_image(config)
    hosts = domain_hosts(config["baseDomain"])
    kube = connect_cluster(config, directory, legacy=False)
    azure = AzureCommands(config, directory)
    cluster = azure.scoped(["aks", "show", "--resource-group", config["target"]["resourceGroup"], "--name", config["parameters"]["platform"]["stage4Aks"]["name"], "--query", "{id:id,nodeResourceGroup:nodeResourceGroup,private:apiServerAccessProfile.enablePrivateCluster}"])
    require(cluster.get("private") is True, "Managed ingress requires the target Private AKS")
    network = config["parameters"]["platform"]["stage4Network"]
    subnet = azure.scoped(["network", "vnet", "subnet", "show", "--resource-group", config["target"]["resourceGroup"], "--vnet-name", network["virtualNetworkName"], "--name", network["ingressSubnetName"], "--query", "{prefix:addressPrefix,plsPolicy:privateLinkServiceNetworkPolicies}"])
    require(subnet.get("plsPolicy") == "Disabled", "Ingress subnet must permit Private Link Service before deployment")
    require(subnet["prefix"] == network["ingressSubnetPrefix"], "Deployed ingress subnet differs from configuration")
    materials, manifests, live = {}, {}, []
    for plane in ("api", "admin"):
        namespace = f"llm-{plane}-ingress"
        scoped = [*kube, "--namespace", namespace]
        run_command([*scoped, "get", "namespace", namespace, "-o", "name"], directory, "namespace-" + plane)
        material = read_certificate(config, settings[plane]["tlsSecretId"], hosts[plane])
        materials[plane] = material
        certificate_path = directory / f"{plane}-chain.pem"
        private_write(certificate_path, material["certificate"])
        run_command(["openssl", "verify", "-purpose", "sslserver", "-verify_hostname", hosts[plane], "-untrusted", str(certificate_path), str(certificate_path)], directory, "certificate-trust-" + plane)
        documents = [document for document in render_ingress(config, plane, image, settings[plane]["allowedCidrs"]) if document["kind"] != "Namespace"]
        for document in documents:
            if document["kind"] == "Deployment":
                pod = document["spec"]["template"]
                pod["metadata"]["annotations"] = {"llmgw/certificate-sha256": material["sha256"]}
                pod["spec"]["volumes"][1]["secret"]["secretName"] = namespace + "-tls-" + material["sha256"][:16]
            current = json.loads(run_command([*scoped, "get", document["kind"], document["metadata"]["name"], "--ignore-not-found", "-o", "json"], directory, f"before-{plane}-{document['kind']}") or "null")
            if current:
                require(current["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") == "llmgw-workflow", "Refusing to adopt an existing unmanaged ingress object")
                live.append({"uid": current["metadata"]["uid"], "spec": current.get("spec"), "data": current.get("data"), "labels": current["metadata"].get("labels"), "annotations": current["metadata"].get("annotations")})
            else:
                live.append(None)
        manifests[plane] = documents
        path = directory / f"ingress-{plane}.yaml"
        private_write(path, yaml.safe_dump_all(documents, sort_keys=False))
        run_command([*scoped, "apply", "--server-side", "--field-manager=llmgw-ingress", "--dry-run=server", "-f", str(path)], directory, "dry-run-" + plane)
    public_certificates = {plane: {key: material[key] for key in ("sha256", "expiresAt", "secretId")} for plane, material in materials.items()}
    plan = {"stage": 4, "action": "private-ingress", "revision": revision, "configSha256": stage_fingerprint(config, 4), "cluster": cluster, "subnet": subnet, "documents": manifests, "certificates": public_certificates, "before": live}
    plan_hash = fingerprint(plan)
    summary = {"stage": 4, "action": "private-ingress", "planSha256": plan_hash, "stageAccepted": False}
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    private_write(directory / "runtime-review.json", json.dumps({"planSha256": plan_hash, **plan}, indent=2) + "\n")
    print(json.dumps(summary))
    if operation == "plan":
        return summary
    require(approved == plan_hash, "Private ingress plan changed or was not approved")
    promote_image(config, directory, azure, lock)
    endpoints = {}
    for plane in ("api", "admin"):
        namespace = f"llm-{plane}-ingress"
        scoped = [*kube, "--namespace", namespace]
        material = materials[plane]
        secret = {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls", "immutable": True, "metadata": {"name": namespace + "-tls-" + material["sha256"][:16], "namespace": namespace, "labels": {"app.kubernetes.io/managed-by": "llmgw-workflow"}}, "data": {key: base64.b64encode(value.encode()).decode() for key, value in {"tls.crt": material["certificate"], "tls.key": material["key"]}.items()}}
        secret_path = directory / "ingress-tls.json"
        private_write(secret_path, json.dumps(secret))
        try:
            run_command([*scoped, "apply", "--server-side", "--field-manager=llmgw-ingress", "-f", str(secret_path), "-o", "name"], directory, "tls-apply-" + plane)
        finally:
            secret_path.unlink(missing_ok=True)
        run_command([*scoped, "apply", "--server-side", "--field-manager=llmgw-ingress", "-f", str(directory / f"ingress-{plane}.yaml")], directory, "apply-" + plane)
        run_command([*scoped, "rollout", "status", "deployment/" + namespace, "--timeout=15m"], directory, "ready-" + plane)
        run_command([*scoped, "wait", "--for=jsonpath={.status.loadBalancer.ingress[0].ip}", "service/" + namespace, "--timeout=10m"], directory, "lb-ready-" + plane)
        service = json.loads(run_command([*scoped, "get", "service", namespace, "-o", "json"], directory, "service-" + plane))
        addresses = service.get("status", {}).get("loadBalancer", {}).get("ingress", [])
        require(len(addresses) == 1 and "ip" in addresses[0], "Expected one IPv4 private ingress address")
        address = addresses[0]["ip"]
        require(ipaddress.ip_address(address) in ipaddress.ip_network(subnet["prefix"]), "Ingress address is outside the approved subnet")
        endpoints[plane] = frontend_for(config, azure, cluster["nodeResourceGroup"], address)
        verify_endpoint(address, hosts[plane], hosts["admin" if plane == "api" else "api"], material["sha256"])
    require(endpoints["api"]["privateIpAddress"] != endpoints["admin"]["privateIpAddress"], "API/admin must not share an ingress frontend")
    output = {"revision": revision, "configSha256": stage_fingerprint(config, 4), "verifiedAt": datetime.now(timezone.utc).isoformat(), "image": image, "certificates": public_certificates, **endpoints}
    receipt = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"privateIngress": {"type": "object", "value": output}}}
    receipt_path = directory / "ingress-receipt-template.json"
    private_write(receipt_path, json.dumps(receipt))
    saved = azure.scoped(["deployment", "group", "create", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 4, "private-ingress"), "--mode", "Incremental", "--template-file", str(receipt_path)])
    require(saved.get("properties", {}).get("provisioningState") == "Succeeded", "Private ingress verified, but saving deployment outputs failed")
    summary.update(applied=True, verified=True, endpoints=endpoints)
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2) + "\n")
    print("Private ingress TLS and cross-plane host rejection verified; backend authentication and full stage acceptance remain separate.")
    return summary