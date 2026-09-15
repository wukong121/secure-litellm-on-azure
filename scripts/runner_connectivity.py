"""Explicit resource identities for the private runner's backup connectivity."""

import hashlib
import ipaddress
import re
import subprocess
from urllib.parse import urlsplit

from scripts.customer_migration import configured, require
from scripts.workflow_diagnostics import exception_diagnostic


def connectivity_settings(config):
    settings = config["parameters"].get("runner-connectivity", {})
    require(isinstance(settings, dict) and not set(settings) - {"runnerVirtualNetworkId", "managePeering", "manageBlobDnsLink"}, "Invalid runner-connectivity fields")
    identifier = settings.get("runnerVirtualNetworkId", "")
    require(isinstance(identifier, str) and configured(identifier), "Configure runnerVirtualNetworkId before checking backup connectivity")
    match = re.fullmatch(r"/subscriptions/([0-9a-f-]{36})/resourceGroups/([a-zA-Z0-9_().-]{1,90})/providers/Microsoft.Network/virtualNetworks/([a-zA-Z0-9_.-]{1,64})", identifier, re.I)
    require(match is not None, "runnerVirtualNetworkId must identify a VNet, not a subnet or VM")
    subscription, group, name = match.groups()
    require(subscription.lower() == config["azure"]["subscriptionId"].lower(), "Runner connectivity currently requires the same subscription")
    backup = config["parameters"].get("backup", {})
    backup_name = backup.get("virtualNetworkName", "")
    require(isinstance(backup_name, str) and configured(backup_name) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", backup_name), "Runner connectivity requires the configured backup VNet")
    target = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{config['target']['resourceGroup']}/providers/Microsoft.Network/virtualNetworks/{backup_name}"
    require(identifier.lower() != target.lower(), "Runner and backup VNet must be separate; reuse an existing same-VNet path without this component")
    manage_peering = settings.get("managePeering", True)
    manage_dns = settings.get("manageBlobDnsLink", True)
    require(type(manage_peering) is bool and type(manage_dns) is bool, "Connectivity management switches must be booleans")
    suffix = hashlib.sha256((identifier.lower() + "|" + target.lower()).encode()).hexdigest()[:16]
    return {
        "runnerVirtualNetworkId": identifier, "runnerResourceGroupName": group,
        "runnerVirtualNetworkName": name, "backupVirtualNetworkId": target,
        "backupVirtualNetworkName": backup_name, "managePeering": manage_peering,
        "manageBlobDnsLink": manage_dns, "connectionName": "llmgw-backup-" + suffix,
    }


def connectivity_parameters(config):
    settings = connectivity_settings(config)
    return {key: settings[key] for key in (
        "runnerResourceGroupName", "runnerVirtualNetworkName", "backupVirtualNetworkName",
        "managePeering", "manageBlobDnsLink", "connectionName",
    )}


def backup_target(config, azure):
    group = config["target"]["resourceGroup"]
    prefix = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{group}/providers/"
    deployment = azure.scoped([
        "deployment", "group", "show", "--resource-group", group,
        "--name", f"llmgw-{config['environment']}-s0-backup",
        "--query", "{state:properties.provisioningState,backup:properties.outputs.backupStorage.value}",
    ])
    require(deployment.get("state") == "Succeeded", "Deploy the backup component before checking connectivity")
    backup = deployment.get("backup", {})
    for field in ("storageAccountName", "virtualNetworkName", "privateEndpointName", "privateEndpointSubnetName", "privateDnsZoneName", "containerName"):
        require(isinstance(backup.get(field), str) and re.fullmatch(r"[a-zA-Z0-9_.-]+", backup[field]), "Invalid backup deployment outputs")
    require(backup["virtualNetworkName"] == config["parameters"]["backup"]["virtualNetworkName"], "Backup output VNet differs from customer configuration")
    require(backup["containerName"] == "litellm-postgresql", "Backup container differs from the managed contract")
    suffix = azure.run(["cloud", "show", "--query", "suffixes.storageEndpoint"])
    require(isinstance(suffix, str) and re.fullmatch(r"[a-z0-9.-]+", suffix), "Invalid Azure Storage DNS suffix")
    require(backup["privateDnsZoneName"] == "privatelink.blob." + suffix, "Backup DNS zone differs from the selected Azure cloud")
    account_id = prefix + "Microsoft.Storage/storageAccounts/" + backup["storageAccountName"]
    account = azure.scoped(["storage", "account", "show", "--ids", account_id,
                            "--query", "{id:id,publicNetworkAccess:publicNetworkAccess,blob:primaryEndpoints.blob}"])
    hostname = backup["storageAccountName"] + ".blob." + suffix
    endpoint = urlsplit(account.get("blob", ""))
    require(account.get("id", "").lower() == account_id.lower() and account.get("publicNetworkAccess") == "Disabled", "Backup storage must remain private in the approved target group")
    require(endpoint.scheme == "https" and endpoint.netloc == hostname and endpoint.path in {"", "/"} and not endpoint.query and not endpoint.fragment, "Unexpected backup Blob endpoint")
    pe_id = prefix + "Microsoft.Network/privateEndpoints/" + backup["privateEndpointName"]
    pe = azure.scoped(["network", "private-endpoint", "show", "--ids", pe_id])
    expected_subnet = prefix + "Microsoft.Network/virtualNetworks/" + backup["virtualNetworkName"] + "/subnets/" + backup["privateEndpointSubnetName"]
    require(pe.get("subnet", {}).get("id", "").lower() == expected_subnet.lower(), "Backup private endpoint belongs to another subnet")
    connections = pe.get("privateLinkServiceConnections", []) + pe.get("manualPrivateLinkServiceConnections", [])
    require(len(connections) == 1 and connections[0].get("privateLinkServiceId", "").lower() == account_id.lower() and connections[0].get("groupIds") == ["blob"] and connections[0].get("privateLinkServiceConnectionState", {}).get("status") == "Approved", "Backup Blob private endpoint is not approved for this account")
    interfaces = pe.get("networkInterfaces", [])
    require(len(interfaces) == 1, "Expected one backup private endpoint NIC")
    nic_id = interfaces[0].get("id", "")
    require(nic_id.lower().startswith((prefix + "Microsoft.Network/networkInterfaces/").lower()) and len(nic_id.split("/")) == 9, "Backup NIC is outside the approved group")
    addresses = azure.scoped(["network", "nic", "show", "--ids", nic_id, "--query", "ipConfigurations[].privateIPAddress"])
    require(isinstance(addresses, list) and addresses and all(ipaddress.ip_address(address).is_private for address in addresses), "Expected private addresses on the backup NIC")
    return {"storageAccountName": backup["storageAccountName"], "containerName": backup["containerName"],
            "blobHost": hostname, "privateEndpointIps": sorted(set(addresses)),
            "privateDnsZoneName": backup["privateDnsZoneName"], "storageAccountId": account_id}


def connectivity_resource_ids(config, dns_zone):
    settings = connectivity_settings(config)
    target_group = settings["backupVirtualNetworkId"].split("/providers/")[0]
    runner_group = settings["runnerVirtualNetworkId"].lower().split("/providers/")[0]
    name = settings["connectionName"]
    identifiers = set()
    if settings["managePeering"]:
        identifiers.update(network + "/virtualNetworkPeerings/" + name for network in (settings["runnerVirtualNetworkId"], settings["backupVirtualNetworkId"]))
        identifiers.add(runner_group + "/providers/Microsoft.Resources/deployments/" + name)
    if settings["manageBlobDnsLink"]:
        identifiers.add(target_group + "/providers/Microsoft.Network/privateDnsZones/" + dns_zone + "/virtualNetworkLinks/" + name)
    return {identifier.lower() for identifier in identifiers}


def inspect_connectivity(config, azure):
    settings = connectivity_settings(config)
    target = backup_target(config, azure)
    networks = [azure.scoped(["network", "vnet", "show", "--ids", settings[key]]) for key in ("runnerVirtualNetworkId", "backupVirtualNetworkId")]
    spaces = []
    for network, identifier in zip(networks, (settings["runnerVirtualNetworkId"], settings["backupVirtualNetworkId"])):
        require(network.get("id", "").lower() == identifier.lower(), "Connectivity VNet resource mismatch")
        prefixes = network.get("addressSpace", {}).get("addressPrefixes", [])
        require(prefixes, "Connectivity VNet has no address space")
        spaces.append([ipaddress.ip_network(prefix) for prefix in prefixes])
    require(not any(source.overlaps(destination) for source in spaces[0] for destination in spaces[1]), "Runner and backup VNet address spaces overlap")
    if settings["managePeering"]:
        for network, remote in zip(networks, (settings["backupVirtualNetworkId"], settings["runnerVirtualNetworkId"])):
            for peer in network.get("virtualNetworkPeerings", []):
                same_name = peer.get("name", "").lower() == settings["connectionName"].lower()
                same_remote = peer.get("remoteVirtualNetwork", {}).get("id", "").lower() == remote.lower()
                require(not same_remote or same_name, "Existing peering uses another name; select managePeering=false to reuse it")
                if same_name:
                    require(same_remote and not any(peer.get(flag, False) for flag in ("allowForwardedTraffic", "allowGatewayTransit", "useRemoteGateways")), "Existing peering conflicts with managed direct connectivity")
    if settings["manageBlobDnsLink"]:
        require(not networks[0].get("dhcpOptions", {}).get("dnsServers"), "Runner uses custom DNS; select manageBlobDnsLink=false and verify approved forwarding")
        links = azure.scoped(["network", "private-dns", "link", "vnet", "list", "--resource-group", config["target"]["resourceGroup"], "--zone-name", target["privateDnsZoneName"]])
        for link in links:
            same_name = link.get("name", "").lower() == settings["connectionName"].lower()
            same_network = link.get("virtualNetwork", {}).get("id", "").lower() == settings["runnerVirtualNetworkId"].lower()
            require(not same_network or same_name, "Existing DNS link uses another name; select manageBlobDnsLink=false to reuse it")
            if same_name:
                require(same_network and link.get("registrationEnabled") is False, "Existing DNS link conflicts with managed backup connectivity")
    return {**target, "runnerAddressSpaces": [str(prefix) for prefix in spaces[0]],
            "backupAddressSpaces": [str(prefix) for prefix in spaces[1]],
            "allowedResourceIds": sorted(connectivity_resource_ids(config, target["privateDnsZoneName"]))}


BACKUP_PROBES = ("backup-resources", "backup-private-dns", "backup-private-tls", "backup-blob-read")


def check_backup_access(config, azure, execute):
    results = []
    target = None
    for name in BACKUP_PROBES:
        if results and results[-1]["status"] != "passed":
            results.append({"name": name, "status": "skipped", "details": "A preceding backup prerequisite failed"})
            continue
        try:
            if name == "backup-resources":
                connectivity_settings(config)
                target = backup_target(config, azure)
                details = target
            elif name == "backup-private-dns":
                output = execute(["getent", "ahostsv4", target["blobHost"]])
                addresses = {line.split()[0] for line in output.splitlines() if line.split()}
                require(addresses and addresses.issubset(set(target["privateEndpointIps"])), "Blob DNS does not resolve exclusively to the approved private endpoint")
                details = "Runner DNS matches the approved private endpoint addresses"
            elif name == "backup-private-tls":
                output = execute(["curl", "--noproxy", "*", "--connect-timeout", "5", "--max-time", "15", "--silent", "--show-error", "--output", "/dev/null", "--write-out", "%{remote_ip} %{http_code}", "https://" + target["blobHost"] + "/"])
                response = output.split()
                require(len(response) == 2 and response[0] in target["privateEndpointIps"] and re.fullmatch(r"[1-5][0-9]{2}", response[1]), "Blob TLS did not reach the approved private endpoint")
                details = "TLS verified at the approved private endpoint; anonymous HTTP response is not data authorization"
            else:
                output = execute(["az", "storage", "blob", "list", "--subscription", config["azure"]["subscriptionId"], "--account-name", target["storageAccountName"], "--container-name", target["containerName"], "--auth-mode", "login", "--num-results", "1", "--query", "length(@)", "--output", "json", "--only-show-errors"])
                require(output.strip() in {"0", "1"}, "Unexpected Blob listing result")
                details = "Runtime identity can list the backup container; upload and download are not verified"
            results.append({"name": name, "status": "passed", "details": details})
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
            results.append({"name": name, "status": "failed", "details": "Check this prerequisite's configuration, routing or runtime identity permissions; no raw response is published", "diagnostic": exception_diagnostic(error, "runner-readiness")})
    return results