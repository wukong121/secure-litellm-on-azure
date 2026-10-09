"""Focused private-network and workload data-plane prerequisite plans."""

import ipaddress
import json
from uuid import UUID, uuid5

from scripts.customer_migration import fingerprint, private_write, require
from scripts.migration_deploy import group_id
from scripts.model_sync_catalog import DNS_API, NETWORK_API, ROLE
from scripts.model_configuration import connection_endpoint


def get(azure, resource, api=NETWORK_API):
    return azure.run(["resource", "show", "--ids", resource, "--api-version", api])


def collection(azure, resource, api):
    document = azure.run(["rest", "--method", "get", "--url",
                          "https://management.azure.com" + resource + "?api-version=" + api])
    require(isinstance(document.get("value"), list) and not document.get("nextLink"),
            "Incomplete Azure collection; paginated responses are not accepted")
    return document["value"]


def network_context(config, azure):
    group = group_id(config)
    network = config["parameters"]["platform"]["stage4Network"]
    vnet_id = group + "/providers/Microsoft.Network/virtualNetworks/" + network["virtualNetworkName"]
    subnet_id = vnet_id + "/subnets/" + network["privateEndpointSubnetName"]
    zone_id = group + "/providers/Microsoft.Network/privateDnsZones/privatelink.openai.azure.com"
    subnet, zone = get(azure, subnet_id), get(azure, zone_id, DNS_API)
    require(subnet.get("id", "").lower() == subnet_id.lower()
            and zone.get("id", "").lower() == zone_id.lower(), "Target subnet/DNS zone identity mismatch")
    props = subnet["properties"]
    prefixes = props.get("addressPrefixes") or [props.get("addressPrefix")]
    networks = [ipaddress.ip_network(value) for value in prefixes]
    require(networks and all(value.is_private for value in networks), "Private Endpoint subnet must be private")
    links = collection(azure, zone_id + "/virtualNetworkLinks", DNS_API)
    matching = [item for item in links if item.get("properties", {}).get("virtualNetwork", {}).get("id", "").lower() == vnet_id.lower()]
    require(len(matching) <= 1, "Ambiguous private DNS VNet links")
    if matching:
        require(matching[0]["properties"].get("provisioningState") == "Succeeded"
                and matching[0]["properties"].get("virtualNetworkLinkState") == "Completed"
                and matching[0]["properties"].get("registrationEnabled") is False,
                "Private DNS VNet link must be completed with registration disabled")
    expected_link = zone_id + "/virtualNetworkLinks/model-sync-" + network["virtualNetworkName"]
    require(matching or not any(item.get("id", "").lower() == expected_link.lower() for item in links),
            "DNS link name collides with another VNet")
    endpoints = collection(azure, group + "/providers/Microsoft.Network/privateEndpoints", NETWORK_API)
    return {"subnet": subnet, "zone": zone, "links": matching, "endpoints": endpoints,
            "vnetId": vnet_id, "subnetId": subnet_id, "zoneId": zone_id,
            "linkId": expected_link, "prefixes": prefixes}


def endpoint_state(account, alias, context, azure):
    candidates = []
    for endpoint in context["endpoints"]:
        props = endpoint.get("properties", {})
        connections = props.get("privateLinkServiceConnections", []) + props.get("manualPrivateLinkServiceConnections", [])
        if any(c.get("properties", {}).get("privateLinkServiceId", "").lower() == account["accountResourceId"].lower()
               for c in connections) and props.get("subnet", {}).get("id", "").lower() == context["subnetId"].lower():
            candidates.append(endpoint)
    require(len(candidates) <= 1, "Multiple Private Endpoints for this account/subnet require explicit remediation")
    endpoint_id = context["zoneId"].split("/providers/")[0] + "/providers/Microsoft.Network/privateEndpoints/pe-" + alias + "-account"
    if not candidates:
        require(not any(item["id"].lower() == endpoint_id.lower() for item in context["endpoints"]),
                "Private Endpoint name is occupied by an unrelated resource")
        return {"createEndpoint": True, "createDnsBinding": False, "endpointId": endpoint_id,
                "endpointName": "pe-" + alias + "-account", "ips": [], "live": None, "dnsGroups": []}
    endpoint = candidates[0]
    props = endpoint["properties"]
    connections = props.get("privateLinkServiceConnections", []) + props.get("manualPrivateLinkServiceConnections", [])
    require(props.get("provisioningState") == "Succeeded" and len(connections) == 1,
            "Existing Private Endpoint must be Succeeded with one reviewed connection")
    connection = connections[0]["properties"]
    require(connection.get("privateLinkServiceId", "").lower() == account["accountResourceId"].lower()
            and connection.get("groupIds") == ["account"]
            and connection.get("privateLinkServiceConnectionState", {}).get("status") == "Approved",
            "Private Endpoint must target this account and be Approved; pending requests require the account owner")
    hostname = connection_endpoint(account).removeprefix("https://")
    private_hostname = hostname.removesuffix(".openai.azure.com") + ".privatelink.openai.azure.com"
    nic_ips, ips, nics = [], [], []
    has_fqdns = False
    for reference in props.get("networkInterfaces", []):
        nic = get(azure, reference["id"])
        nics.append(nic)
        for item in nic.get("properties", {}).get("ipConfigurations", []):
            properties = item.get("properties", {})
            ip = properties.get("privateIPAddress")
            require(ip and any(ipaddress.ip_address(ip) in ipaddress.ip_network(prefix) for prefix in context["prefixes"]),
                    "Private Endpoint NIC address is outside the approved subnet")
            nic_ips.append(ip)
            mapping = properties.get("privateLinkConnectionProperties", {})
            require(isinstance(mapping, dict), "Private Endpoint NIC requires valid hostname mapping metadata")
            if mapping:
                require(mapping.get("groupId") == "account",
                        "Private Endpoint NIC mapping must belong to the approved account group")
            fqdns = mapping.get("fqdns", [])
            require(isinstance(fqdns, list) and all(isinstance(fqdn, str) for fqdn in fqdns),
                    "Private Endpoint NIC FQDN mappings must be strings")
            has_fqdns = has_fqdns or bool(fqdns)
            if any(fqdn.rstrip(".").lower() in {hostname, private_hostname} for fqdn in fqdns):
                ips.append(ip)
    require(nic_ips, "Private Endpoint requires private NIC addresses")
    if not ips:
        # Older single-address NIC responses are unambiguous; multi-service NICs require FQDN metadata.
        require(not has_fqdns and len(set(nic_ips)) == 1,
                "Private Endpoint NIC metadata cannot identify the OpenAI hostname: " + hostname)
        ips = nic_ips
    ips = sorted(set(ips))
    groups = collection(azure, endpoint["id"] + "/privateDnsZoneGroups", NETWORK_API)
    require(len(groups) <= 1, "Multiple Private DNS groups require explicit remediation")
    if groups:
        configs = groups[0].get("properties", {}).get("privateDnsZoneConfigs", [])
        require(groups[0]["properties"].get("provisioningState") == "Succeeded" and len(configs) == 1
                and configs[0].get("properties", {}).get("privateDnsZoneId", "").lower() == context["zoneId"].lower(),
                "Private Endpoint DNS group differs from the approved zone")
    records = collection(azure, context["zoneId"] + "/A", DNS_API)
    record_name = hostname.split(".")[0]
    record = next((r for r in records if r.get("name", "").lower() == record_name), None)
    actual = {item.get("ipv4Address") for item in (record or {}).get("properties", {}).get("aRecords", [])}
    if groups:
        require(actual == set(ips), "Private DNS A record does not match the approved Private Endpoint for "
                + hostname + "; expected " + json.dumps(ips) + ", actual " + json.dumps(sorted(actual, key=str)))
    else:
        require(not record or actual == set(ips), "Existing DNS A record conflicts with missing binding")
    return {"createEndpoint": False, "createDnsBinding": not groups, "endpointId": endpoint["id"],
            "endpointName": endpoint["name"], "ips": ips, "nicIps": sorted(set(nic_ips)), "live": endpoint,
            "dnsGroups": groups, "nics": nics, "record": record}


def role_state(account, identity, source, azure):
    scope = account["accountResourceId"]
    roles = azure.run(["role", "assignment", "list", "--scope", scope,
                      "--subscription", account["subscriptionId"],
                      "--fill-principal-name", "false", "--fill-role-definition-name", "false"])
    require(isinstance(roles, list), "Unexpected role assignment response")
    matching = [role for role in roles if role.get("scope", "").lower() == scope.lower()
                and role.get("principalId", "").lower() == identity["properties"]["principalId"].lower()
                and role.get("roleDefinitionId", "").lower().endswith("/" + ROLE)]
    require(all(not role.get("condition") and not role.get("conditionVersion")
                and role.get("principalType") == "ServicePrincipal" for role in matching),
            "Existing model role has unsupported conditions/principal type")
    role_definition = f"/subscriptions/{account['subscriptionId']}/providers/Microsoft.Authorization/roleDefinitions/{ROLE}"
    name = str(uuid5(UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830"),
                     "-".join([scope, source or identity["properties"]["principalId"], role_definition])))
    expected = scope + "/providers/Microsoft.Authorization/roleAssignments/" + name
    require(matching or not any(role.get("id", "").lower() == expected.lower() for role in roles),
            "Role assignment name conflicts with an existing assignment")
    return {"createRole": not matching, "roleId": expected, "existingRoles": matching}


def assert_what_if(document, allowed):
    require(document.get("status") == "Succeeded" and isinstance(document.get("changes"), list),
            "What-if did not complete; verify cross-subscription deployment and account role-write permissions")
    seen = set()

    def check(changes):
        for change in changes:
            resource = change.get("resourceId", "").lower()
            kind = change.get("changeType")
            require(resource in allowed, "What-if resource is outside the exact model-sync allowlist")
            require(kind in {"Create", "Modify", "NoChange", "Ignore"}, "What-if Delete/Unsupported/unknown change blocked")
            require(kind != "Modify" or allowed[resource] == "deployment",
                    "What-if cannot modify an existing PE, NIC, DNS record/link or role")
            seen.add(resource)
            children = change.get("resourceChanges", [])
            if children:
                check(children)
    check(document["changes"])
    # Do not silently accept an opaque nested deployment or an unresolved What-if.
    require(all(resource in seen for resource, kind in allowed.items() if kind == "required"),
            "What-if did not expand all planned prerequisite resources")


def infrastructure_plan(config, desired, matches, identity, template, directory, azure):
    context = network_context(config, azure)
    plans = []
    source = identity["id"] if config["parameters"]["platform"].get("stage4RoleAssignmentNaming", "principal-id") == "resource-id" else ""
    unique = {account["accountResourceId"].lower(): account for account, _ in matches}
    for account in unique.values():
        alias = next(c["alias"] for c in desired["parameters"]["platform"]["azureOpenAIConnections"]
                     if c["accountResourceId"].lower() == account["accountResourceId"].lower())
        endpoint = endpoint_state(account, alias, context, azure)
        role = role_state(account, identity, source, azure)
        create_link = not context["links"] and not any(p["parameters"]["createDnsLink"] for p in plans)
        network = config["parameters"]["platform"]["stage4Network"]
        params = {"location": config["location"], "virtualNetworkName": network["virtualNetworkName"],
                  "privateEndpointSubnetName": network["privateEndpointSubnetName"], "accountResourceId": account["accountResourceId"],
                  "accountAlias": alias, "accountName": account["accountName"], "accountSubscriptionId": account["subscriptionId"],
                  "accountResourceGroupName": account["resourceGroupName"], "principalId": identity["properties"]["principalId"],
                  "principalSourceResourceId": source, "createEndpoint": endpoint["createEndpoint"],
                  "createDnsBinding": endpoint["createDnsBinding"], "endpointName": endpoint["endpointName"],
                  "createDnsLink": create_link, "createRole": role["createRole"]}
        allowed = {}
        def allow(resource, kind="required"):
            allowed[resource.lower()] = kind
        group = group_id(config)
        if endpoint["createEndpoint"]:
            allow(endpoint["endpointId"])
            allow(endpoint["endpointId"] + "/privateDnsZoneGroups/default")
            allow(group + "/providers/Microsoft.Network/networkInterfaces/nic-pe-" + alias + "-account", "optional")
            allow(group + "/providers/Microsoft.Resources/deployments/model-sync-pe-" + alias, "deployment")
        elif endpoint["createDnsBinding"]:
            allow(endpoint["endpointId"] + "/privateDnsZoneGroups/default")
        if role["createRole"]:
            allow(role["roleId"])
            allow(f"/subscriptions/{account['subscriptionId']}/resourceGroups/{account['resourceGroupName']}"
                  + "/providers/Microsoft.Resources/deployments/model-sync-role-" + alias, "deployment")
        if create_link:
            allow(context["linkId"])
        plan = {"account": account, "parameters": params, "endpoint": endpoint, "role": role, "allowedResources": allowed}
        if allowed:
            path = directory / ("parameters-" + alias + ".json")
            private_write(path, json.dumps({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                                          "contentVersion": "1.0.0.0", "parameters": {k: {"value": v} for k, v in params.items()}}, indent=2))
            arguments = ["deployment", "group", "what-if", "--subscription", config["azure"]["subscriptionId"],
                         "--resource-group", config["target"]["resourceGroup"], "--name", "model-sync-" + alias,
                         "--template-file", str(template), "--parameters", "@" + str(path),
                         "--result-format", "FullResourcePayloads", "--no-pretty-print"]
            what_if = azure.run(arguments)
            private_write(directory / ("what-if-" + alias + ".json"), json.dumps(what_if, indent=2))
            assert_what_if(what_if, allowed)
            # Bind logical resource changes, not provider-generated What-if diagnostics/timestamps.
            plan["whatIfSha256"] = fingerprint(what_if["changes"])
        plans.append(plan)
    return {"context": context, "accounts": plans}


def execute_infrastructure(config, plan, template, directory, azure, record):
    for item in plan["accounts"]:
        if not item["allowedResources"]:
            continue
        alias = item["parameters"]["accountAlias"]
        path = directory / ("parameters-" + alias + ".json")
        record.setdefault("infrastructureAttempted", []).append(alias)
        private_write(directory / "model-sync-state.json", json.dumps(record, indent=2))
        result = azure.run(["deployment", "group", "create", "--subscription", config["azure"]["subscriptionId"],
                            "--resource-group", config["target"]["resourceGroup"], "--name", "model-sync-" + alias,
                            "--template-file", str(template), "--parameters", "@" + str(path), "--mode", "Incremental"])
        private_write(directory / ("receipt-" + alias + ".json"), json.dumps(result, indent=2))
        record.setdefault("infrastructureReceipts", {})[alias] = result.get("properties", {}).get("provisioningState", "unknown")
        private_write(directory / "model-sync-state.json", json.dumps(record, indent=2))
        require(result.get("properties", {}).get("provisioningState") == "Succeeded",
                "Prerequisite deployment did not succeed; inspect receipt before retrying")
        record["infrastructureCompleted"].append(alias)
        record["phase"] = "infrastructure"
        private_write(directory / "model-sync-state.json", json.dumps(record, indent=2))
    # Re-read every prerequisite; an unapproved PE must stop before any application update.
    context = network_context(config, azure)
    require(context["links"], "Private DNS link was not completed")
    for item in plan["accounts"]:
        endpoint = endpoint_state(item["account"], item["parameters"]["accountAlias"], context, azure)
        require(not endpoint["createEndpoint"] and not endpoint["createDnsBinding"],
                "Private Endpoint/DNS prerequisite remains missing; no application update performed")
        role = role_state(item["account"], {"id": "", "properties": {"principalId": item["parameters"]["principalId"]}},
                          item["parameters"]["principalSourceResourceId"], azure)
        require(not role["createRole"], "Workload role assignment remains missing")
        item["verifiedIps"] = endpoint["ips"]
