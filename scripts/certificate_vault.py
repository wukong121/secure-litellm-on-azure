"""Stage 4 certificate-only Vault parameters and bounded infrastructure changes."""

import re
from uuid import UUID, uuid5

from scripts.customer_migration import COMPONENTS, REQUIRED, configured, require

ZONE_NAME = "privatelink.vaultcore.azure.net"
ARM_GUID_NAMESPACE = UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830")


def certificate_vault_parameters(config):
    settings = dict(config["parameters"].get("certificate-vault", {}))
    require(REQUIRED["certificate-vault"].issubset(settings) and not set(settings) - COMPONENTS["certificate-vault"][2] and configured(settings), "Certificate Vault configuration is incomplete")
    name = settings["vaultName"]
    require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9-]{1,22}[a-z0-9]", name) and "--" not in name, "Invalid certificate Vault name")
    require(not name.startswith("kv-lt-"), "Certificate Vault must be separate from the backend Vault")
    for key in ("ingressReaderPrincipalId", "certificateImporterPrincipalId"):
        require(isinstance(settings[key], str) and re.fullmatch(r"[0-9a-fA-F-]{36}", settings[key]) and UUID(settings[key]).int, "Certificate access requires nonzero principal Object IDs")
    require(settings["ingressReaderPrincipalId"].lower() != settings["certificateImporterPrincipalId"].lower(), "Certificate reader and manual importer must be distinct principals")
    require(isinstance(settings["certificateImporterPrincipalType"], str) and settings["certificateImporterPrincipalType"] in {"User", "Group"}, "Manual certificate importer must be a User or Group")
    settings.setdefault("manageTargetDnsLink", True)
    require(all(type(settings[key]) is bool for key in ("createPrivateDnsZone", "manageRunnerDnsLink", "manageTargetDnsLink")), "Certificate DNS switches must be booleans")
    identifier = settings["runnerVirtualNetworkId"]
    match = re.fullmatch(r"/subscriptions/([0-9a-f-]{36})/resourceGroups/([a-zA-Z0-9_().-]{1,90})/providers/Microsoft.Network/virtualNetworks/([a-zA-Z0-9_.-]{1,64})", identifier, re.I) if isinstance(identifier, str) else None
    require(match is not None and match[1].lower() == config["azure"]["subscriptionId"].lower(), "Certificate Runner VNet must be a same-subscription VNet resource ID")
    platform = config["parameters"].get("platform", {})
    network = platform.get("stage4Network", {})
    require(all(isinstance(network.get(key), str) and configured(network[key]) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", network[key]) for key in ("virtualNetworkName", "privateEndpointSubnetName")) and configured(platform.get("logAnalyticsWorkspaceName", "")), "Certificate Vault requires the target platform network and workspace")
    settings.update(virtualNetworkName=network["virtualNetworkName"], privateEndpointSubnetName=network["privateEndpointSubnetName"], logAnalyticsWorkspaceName=platform["logAnalyticsWorkspaceName"])
    if "privateIngress" in config:
        for plane in ("api", "admin"):
            identifier = config["privateIngress"].get(plane, {}).get("tlsSecretId", "")
            expected = f"https://{name}.vault.azure.net/secrets/{plane}-tls"
            require(isinstance(identifier, str) and re.fullmatch(re.escape(expected) + r"(?:/[a-fA-F0-9]{32})?", identifier), "privateIngress must use the corresponding certificate Vault api-tls/admin-tls Secret")
    return settings


def certificate_resource_ids(config):
    settings = certificate_vault_parameters(config)
    prefix = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{config['target']['resourceGroup']}/providers/"
    vault = prefix + "Microsoft.KeyVault/vaults/" + settings["vaultName"]
    zone = prefix + "Microsoft.Network/privateDnsZones/" + ZONE_NAME
    endpoint = prefix + "Microsoft.Network/privateEndpoints/pe-" + settings["vaultName"] + "-vault"
    target = prefix + "Microsoft.Network/virtualNetworks/" + settings["virtualNetworkName"]
    resources = {
        vault, vault + "/providers/Microsoft.Authorization/locks/protect-key-vault-from-deletion",
        vault + "/providers/Microsoft.Insights/diagnosticSettings/send-key-vault-audit-to-log-analytics",
        endpoint, endpoint + "/privateDnsZoneGroups/default",
        prefix + "Microsoft.Network/networkInterfaces/nic-pe-" + settings["vaultName"] + "-vault",
        prefix + "Microsoft.Resources/deployments/certificate-vault-private-endpoint",
    }
    for principal, purpose in ((settings["ingressReaderPrincipalId"], "certificate-reader"), (settings["certificateImporterPrincipalId"], "certificate-importer")):
        assignment = uuid5(ARM_GUID_NAMESPACE, "-".join((vault.lower(), principal.lower(), purpose)))
        resources.add(vault + "/providers/Microsoft.Authorization/roleAssignments/" + str(assignment))
    if settings["createPrivateDnsZone"]:
        resources.add(zone)
    if settings["manageTargetDnsLink"]:
        resources.add(zone + "/virtualNetworkLinks/" + settings["virtualNetworkName"] + "-link")
    if settings["manageRunnerDnsLink"] and target.lower() != settings["runnerVirtualNetworkId"].lower():
        resources.add(zone + "/virtualNetworkLinks/certificate-runner-link")
    return {resource.lower() for resource in resources}


def inspect_certificate_infrastructure(config, azure):
    settings = certificate_vault_parameters(config)
    group = config["target"]["resourceGroup"]
    prefix = f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{group}/providers/"
    target = prefix + "Microsoft.Network/virtualNetworks/" + settings["virtualNetworkName"]
    vaults = azure.scoped(["keyvault", "list", "--resource-group", group])
    require(isinstance(vaults, list), "Invalid Vault inventory")
    for vault in vaults:
        if vault.get("name", "").lower() == settings["vaultName"]:
            require((vault.get("tags") or {}).get("purpose") == "ingress-certificates", "Refusing to adopt a Vault not marked for ingress certificates")
    zones = azure.scoped(["network", "private-dns", "zone", "list", "--resource-group", group])
    require(isinstance(zones, list), "Invalid private DNS inventory")
    exists = any(zone.get("name", "").lower() == ZONE_NAME for zone in zones)
    require(exists or settings["createPrivateDnsZone"], "Create or provide the target-RG Key Vault private DNS zone first")
    links = azure.scoped(["network", "private-dns", "link", "vnet", "list", "--resource-group", group, "--zone-name", ZONE_NAME]) if exists else []
    require(isinstance(links, list), "Invalid private DNS link inventory")
    networks = [(target, settings["virtualNetworkName"] + "-link", settings["manageTargetDnsLink"])]
    if target.lower() != settings["runnerVirtualNetworkId"].lower():
        networks.append((settings["runnerVirtualNetworkId"], "certificate-runner-link", settings["manageRunnerDnsLink"]))
    for identifier, name, managed in networks:
        network = azure.scoped(["network", "vnet", "show", "--ids", identifier])
        require(network.get("id", "").lower() == identifier.lower(), "Certificate VNet differs from the approved scope")
        if not managed:
            continue
        require(not network.get("dhcpOptions", {}).get("dnsServers"), "Custom DNS requires unmanaged certificate DNS links and approved forwarding")
        for link in links:
            same_name = link.get("name", "").lower() == name.lower()
            same_network = link.get("virtualNetwork", {}).get("id", "").lower() == identifier.lower()
            require(not same_network or same_name, "Existing certificate DNS link uses another name; disable management to reuse it")
            if same_name:
                require(same_network and link.get("registrationEnabled") is False, "Existing certificate DNS link conflicts with the approved link")