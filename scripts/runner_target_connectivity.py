"""Approved Runner access to an existing private AKS DNS zone, without AKS changes."""

import hashlib
import ipaddress
import re

from scripts.customer_migration import COMPONENTS, REQUIRED, configured, require


def target_connectivity_settings(config):
    settings = dict(config["parameters"].get("runner-target-connectivity", {}))
    require(REQUIRED["runner-target-connectivity"].issubset(settings) and not set(settings) - COMPONENTS["runner-target-connectivity"][2] and configured(settings), "Runner target connectivity configuration is incomplete")
    identifier = settings["runnerVirtualNetworkId"]
    match = re.fullmatch(r"/subscriptions/([0-9a-f-]{36})/resourceGroups/([a-zA-Z0-9_().-]{1,90})/providers/Microsoft.Network/virtualNetworks/([a-zA-Z0-9_.-]{1,64})", identifier, re.I) if isinstance(identifier, str) else None
    require(match is not None and match[1].lower() == config["azure"]["subscriptionId"].lower(), "Runner target connectivity requires a same-subscription VNet resource ID")
    settings.setdefault("manageDnsLink", True)
    require(type(settings["manageDnsLink"]) is bool, "manageDnsLink must be a boolean")
    platform = config["parameters"].get("platform", {})
    cluster = platform.get("stage4Aks", {}).get("name", "")
    network = platform.get("stage4Network", {}).get("virtualNetworkName", "")
    require(all(isinstance(name, str) and configured(name) and re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}", name) for name in (cluster, network)), "Runner target connectivity requires the configured target AKS and VNet names")
    for component in ("runner-connectivity", "certificate-vault"):
        previous = config["parameters"].get(component, {}).get("runnerVirtualNetworkId")
        require(not previous or not configured(previous) or previous.lower() == identifier.lower(), "Runner VNet differs between connectivity components")
    prefix = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{config['target']['resourceGroup']}/providers/"
    cluster_id = prefix + "Microsoft.ContainerService/managedClusters/" + cluster
    suffix = hashlib.sha256((cluster_id.lower() + "|" + identifier.lower()).encode()).hexdigest()[:16]
    return {
        **settings,
        "aksName": cluster,
        "aksResourceId": cluster_id,
        "targetVirtualNetworkId": prefix + "Microsoft.Network/virtualNetworks/" + network,
        "linkName": "llmgw-aks-runner-" + suffix,
    }


def inspect_target_connectivity(config, azure, require_link=False):
    settings = target_connectivity_settings(config)
    group = config["target"]["resourceGroup"]
    deployment = azure.scoped([
        "deployment", "group", "show", "--resource-group", group,
        "--name", f"llmgw-{config['environment']}-s4-platform",
        "--query", "properties.provisioningState",
    ])
    require(deployment == "Succeeded", "Complete the Stage 4 platform deployment before target connectivity")
    cluster = azure.scoped([
        "aks", "show", "--resource-group", group, "--name", settings["aksName"],
        "--query", "{id:id,provisioningState:provisioningState,powerState:powerState,nodeResourceGroup:nodeResourceGroup,privateFqdn:privateFqdn,apiServerAccessProfile:apiServerAccessProfile,agentPoolProfiles:agentPoolProfiles}",
    ])
    require(cluster.get("id", "").lower() == settings["aksResourceId"].lower() and cluster.get("provisioningState") == "Succeeded" and cluster.get("powerState", {}).get("code") == "Running", "Target AKS must match the approved running cluster")
    profile = cluster.get("apiServerAccessProfile") or {}
    require(profile.get("enablePrivateCluster") is True, "Target connectivity requires a private AKS cluster")
    pools = cluster.get("agentPoolProfiles", [])
    expected_subnets = settings["targetVirtualNetworkId"].lower() + "/subnets/"
    require(pools and all(pool.get("vnetSubnetId", "").lower().startswith(expected_subnets) for pool in pools), "AKS node pools differ from the configured target VNet")
    node_group = cluster.get("nodeResourceGroup", "")
    require(isinstance(node_group, str) and re.fullmatch(r"[a-zA-Z0-9_().-]{1,90}", node_group) and not node_group.endswith("."), "Invalid AKS node resource group")
    configured_group = config["parameters"]["platform"]["stage4Aks"].get("nodeResourceGroupName")
    require(not configured_group or configured_group.lower() == node_group.lower(), "AKS node resource group differs from configuration")
    host = cluster.get("privateFqdn", "").lower()
    require(re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.privatelink\.[a-z0-9-]+\.azmk8s\.io", host), "Unsupported AKS private FQDN; verify the Azure cloud and DNS configuration")
    subscription = config["azure"]["subscriptionId"]
    node_prefix = f"/subscriptions/{subscription}/resourceGroups/{node_group}/providers/"
    dns_mode = profile.get("privateDnsZone")
    if isinstance(dns_mode, str) and dns_mode.lower() == "system":
        zone_name = host.split(".", 1)[1]
        dns_group = node_group
    else:
        match = re.fullmatch(r"/subscriptions/([0-9a-f-]{36})/resourceGroups/([a-zA-Z0-9_().-]{1,90})/providers/Microsoft.Network/privateDnsZones/([a-z0-9.-]+)", dns_mode, re.I) if isinstance(dns_mode, str) else None
        require(match is not None and match[1].lower() == subscription.lower(), "AKS must report a system or same-subscription private DNS zone")
        dns_group, zone_name = match[2], match[3].lower()
    require(host.endswith("." + zone_name), "AKS private FQDN does not belong to the reported zone")
    zone_id = f"/subscriptions/{subscription}/resourceGroups/{dns_group}/providers/Microsoft.Network/privateDnsZones/{zone_name}"
    zone = azure.scoped(["network", "private-dns", "zone", "show", "--resource-group", dns_group, "--name", zone_name])
    require(zone.get("id", "").lower() == zone_id.lower(), "AKS private DNS zone identity mismatch")
    endpoints = azure.scoped(["network", "private-endpoint", "list", "--resource-group", node_group])
    matches = []
    for endpoint in endpoints:
        connections = endpoint.get("privateLinkServiceConnections", []) + endpoint.get("manualPrivateLinkServiceConnections", [])
        if any(connection.get("privateLinkServiceId", "").lower() == settings["aksResourceId"].lower() and connection.get("privateLinkServiceConnectionState", {}).get("status") == "Approved" for connection in connections):
            matches.append(endpoint)
    require(len(matches) == 1, "Expected one approved AKS API private endpoint in its node resource group")
    endpoint = matches[0]
    require(endpoint.get("provisioningState") == "Succeeded" and endpoint.get("subnet", {}).get("id", "").lower().startswith(expected_subnets), "AKS private endpoint is not ready in the approved target VNet")
    interfaces = endpoint.get("networkInterfaces", [])
    require(len(interfaces) == 1, "Expected one AKS API private endpoint NIC")
    nic_id = interfaces[0].get("id", "")
    require(re.fullmatch(re.escape(node_prefix + "Microsoft.Network/networkInterfaces/") + r"[^/]+", nic_id, re.I), "AKS private endpoint NIC is outside its node resource group")
    addresses = azure.scoped(["network", "nic", "show", "--ids", nic_id, "--query", "ipConfigurations[].privateIPAddress"])
    require(isinstance(addresses, list) and addresses and all(ipaddress.ip_address(address).version == 4 and ipaddress.ip_address(address).is_private for address in addresses), "AKS private endpoint requires private IPv4 addresses")
    record_name = host[:-(len(zone_name) + 1)]
    record = azure.scoped(["network", "private-dns", "record-set", "a", "show", "--resource-group", dns_group, "--zone-name", zone_name, "--name", record_name])
    require({item.get("ipv4Address") for item in record.get("aRecords", [])} == set(addresses), "AKS DNS record differs from the actual API private endpoint; investigate restart or DNS drift")
    runner = azure.scoped(["network", "vnet", "show", "--ids", settings["runnerVirtualNetworkId"]])
    require(runner.get("id", "").lower() == settings["runnerVirtualNetworkId"].lower(), "Runner VNet identity mismatch")
    require(not settings["manageDnsLink"] or not (runner.get("dhcpOptions") or {}).get("dnsServers"), "Custom Runner DNS requires manageDnsLink=false and approved forwarding")
    links = azure.scoped(["network", "private-dns", "link", "vnet", "list", "--resource-group", dns_group, "--zone-name", zone_name])
    require(isinstance(links, list), "Invalid AKS DNS link inventory")
    existing = []
    for link in links:
        same_network = link.get("virtualNetwork", {}).get("id", "").lower() == settings["runnerVirtualNetworkId"].lower()
        same_name = link.get("name", "").lower() == settings["linkName"].lower()
        require(not same_name or same_network, "Managed AKS DNS link name points to another VNet")
        if same_network:
            require(link.get("registrationEnabled") is False and link.get("provisioningState") == "Succeeded" and link.get("virtualNetworkLinkState") == "Completed", "Existing AKS DNS link conflicts with the approved settings or is not ready")
            require(not same_name or link.get("resolutionPolicy", "Default") == "Default", "Managed AKS DNS link has an unexpected resolution policy")
            existing.append(link)
    require(len(existing) <= 1, "Multiple AKS DNS links target the Runner VNet")
    require(not require_link or not settings["manageDnsLink"] or existing, "Runner AKS DNS link is missing after deployment")
    create_link = settings["manageDnsLink"] and (not existing or existing[0]["name"].lower() == settings["linkName"].lower())
    link_name = existing[0]["name"] if existing else settings["linkName"]
    return {
        "aksResourceId": settings["aksResourceId"], "apiHostname": host,
        "runnerVirtualNetworkId": settings["runnerVirtualNetworkId"],
        "dnsResourceGroupName": dns_group, "privateDnsZoneName": zone_name,
        "privateDnsZoneId": zone_id, "privateEndpointIps": sorted(addresses),
        "linkName": link_name, "createDnsLink": create_link,
        "management": "managed" if create_link else "reused" if settings["manageDnsLink"] else "external",
    }


def target_connectivity_resource_ids(config, context):
    require(context is not None, "Target connectivity requires resolved AKS DNS resources")
    if not context["createDnsLink"]:
        return set()
    prefix = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{context['dnsResourceGroupName']}/providers/"
    return {
        (context["privateDnsZoneId"] + "/virtualNetworkLinks/" + context["linkName"]).lower(),
        (prefix + "Microsoft.Resources/deployments/" + context["linkName"]).lower(),
    }