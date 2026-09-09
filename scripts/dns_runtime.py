"""Azure DNS API-only cutover with pre-write checkpoint and conditional rollback."""

import json
import re

from scripts.customer_migration import fingerprint, private_write, require, stage_fingerprint
from scripts.migration_deploy import AzureCommands, deployment_name, group_id


def dns_settings(config):
    value = config.get("dns")
    require(isinstance(value, dict) and set(value) == {"zoneResourceId", "ttl"}, "dns requires zoneResourceId and ttl")
    match = re.fullmatch(r"/subscriptions/([a-fA-F0-9-]{36})/resourceGroups/([A-Za-z0-9_.()-]+)/providers/Microsoft.Network/dnsZones/([a-z0-9.-]+)", value["zoneResourceId"])
    require(match and match[1].lower() == config["azure"]["subscriptionId"].lower(), "DNS zone must be in the approved subscription")
    host = "llm-api." + config["baseDomain"]
    require(host.endswith("." + match[3]), "DNS zone must contain the API hostname; apex CNAME is not supported")
    require(type(value["ttl"]) is int and 60 <= value["ttl"] <= 3600, "DNS TTL must be 60 to 3600 seconds")
    return {**value, "zone": match[3], "group": match[2], "record": host[:-(len(match[3]) + 1)]}


class DnsClient:
    def __init__(self, config, directory, azure=None):
        self.config, self.directory = config, directory
        self.azure = azure or AzureCommands(config, directory)
        require(self.azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"]) == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "DNS Azure login scope mismatch")
        self.settings = dns_settings(config)
        self.url = "https://management.azure.com" + self.settings["zoneResourceId"] + "/CNAME/" + self.settings["record"] + "?api-version=2018-05-01"

    def read(self):
        settings = self.settings
        records = self.azure.scoped(["network", "dns", "record-set", "cname", "list", "--resource-group", settings["group"], "--zone-name", settings["zone"]])
        matches = [item for item in records if item["name"] == settings["record"]]
        require(len(matches) <= 1, "Ambiguous API DNS record")
        if not matches:
            return None
        item = matches[0]
        require(item.get("etag"), "DNS record has no concurrency token")
        record = item.get("cnameRecord", item.get("CNAMERecord"))
        require(isinstance(record, dict) and record.get("cname"), "Unexpected DNS record content")
        return {"etag": item["etag"], "properties": {"TTL": item.get("ttl", item.get("TTL")), "CNAMERecord": record, "metadata": item.get("metadata") or {}}}

    def checkpoint(self):
        name = deployment_name(self.config, 9, "dns-checkpoint")
        records = self.azure.scoped(["deployment", "group", "list", "--resource-group", self.config["target"]["resourceGroup"], "--query", f"[?name=='{name}'].properties.outputs.dnsCheckpoint.value"])
        require(len(records) <= 1, "Ambiguous DNS checkpoint")
        return records[0] if records else None

    def save(self, checkpoint):
        path = self.directory / "dns-checkpoint.json"
        private_write(path, json.dumps({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0", "resources": [], "outputs": {"dnsCheckpoint": {"type": "object", "value": checkpoint}}}))
        result = self.azure.scoped(["deployment", "group", "create", "--resource-group", self.config["target"]["resourceGroup"], "--name", deployment_name(self.config, 9, "dns-checkpoint"), "--mode", "Incremental", "--template-file", str(path)])
        require(result.get("properties", {}).get("provisioningState") == "Succeeded", "DNS checkpoint could not be saved; record was not changed")

    def write(self, desired, previous):
        headers = ["If-Match=" + previous["etag"]] if previous else ["If-None-Match=*"]
        arguments = ["rest", "--method", "put" if desired else "delete", "--url", self.url, "--headers", *headers]
        if desired:
            path = self.directory / "dns-record.json"
            private_write(path, json.dumps({"properties": desired}))
            arguments += ["--body", "@" + str(path)]
        self.azure.scoped(arguments)


def change_dns(config, action, operation, revision, directory, approved, *, client=None, edge=None):
    require(action in {"dns-publish", "dns-rollback"} and operation in {"plan", "execute"}, "Invalid DNS operation")
    client = client or DnsClient(config, directory)
    settings = dns_settings(config)
    if edge is None:
        output = client.azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge"), "--query", "{state:properties.provisioningState,edge:properties.outputs.edge.value}"])
        require(output.get("state") == "Succeeded", "Deploy and verify the edge before DNS publication")
        edge = output["edge"]
        require(edge.get("routeId", "").lower().startswith(group_id(config).lower() + "/providers/microsoft.cdn/profiles/"), "Re-deploy edge to provide its route resource ID")
        route = client.azure.scoped(["resource", "show", "--ids", edge["routeId"], "--api-version", "2025-04-15"])
        if action == "dns-publish":
            require(route.get("properties", {}).get("enabledState") == "Enabled" and route["properties"].get("linkToDefaultDomain") == "Disabled", "API route is not enabled or exposes the default domain")
    require(edge.get("apiHost") == "llm-api." + config["baseDomain"] and re.fullmatch(r"[a-z0-9-]+\.(?:[a-z0-9-]+\.)?azurefd\.net", edge.get("endpointHost", "")), "DNS target must be the deployed API Front Door")
    scope = {"configSha256": stage_fingerprint(config, 9), "record": settings["zoneResourceId"] + "/CNAME/" + settings["record"]}
    current = client.read()
    checkpoint = client.checkpoint()
    if checkpoint:
        require(checkpoint.get("scope") == scope, "DNS checkpoint belongs to another configuration; preserve it for separate recovery")
    if action == "dns-publish":
        require(edge.get("trafficEnabled") is True, "Approve and enable edge traffic before changing DNS")
        desired = {"TTL": settings["ttl"], "CNAMERecord": {"cname": edge["endpointHost"]}, "metadata": {"managedBy": "llmgw-workflow"}}
        if checkpoint:
            require(checkpoint.get("after") == desired and checkpoint.get("phase") != "rolled-back", "Existing DNS checkpoint must not be replaced")
        else:
            checkpoint = {"scope": scope, "before": current["properties"] if current else None, "after": desired, "phase": "prepared", "revision": revision}
    else:
        require(checkpoint, "No approved DNS checkpoint exists")
        require((current["properties"] if current else None) in (checkpoint["before"], checkpoint["after"]), "Current DNS was modified externally; refuse automatic rollback")
        desired = checkpoint["before"]
    plan = {"stage": 9, "action": action, "revision": revision, "scope": scope, "before": current, "checkpoint": checkpoint, "desired": desired}
    summary = {"stage": 9, "action": action, "planSha256": fingerprint(plan), "applied": False, "stageAccepted": False, "dnsPropagationVerified": False, "dataRollbackPerformed": False}
    private_write(directory / "runtime-review.json", json.dumps(plan, indent=2))
    private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    if operation == "execute":
        require(approved == summary["planSha256"], "DNS plan changed or was not approved")
        require(client.read() == current, "DNS changed since plan")
        if action == "dns-publish" and client.checkpoint() is None:
            client.save(checkpoint)
        require(client.checkpoint() == checkpoint, "DNS checkpoint changed before update")
        if (current["properties"] if current else None) != desired:
            client.write(desired, current)
        observed = client.read()
        require((observed["properties"] if observed else None) == desired, "DNS write result is uncertain; replan without overwriting checkpoint")
        client.save({**checkpoint, "phase": "published" if action == "dns-publish" else "rolled-back"})
        summary["applied"] = True
        private_write(directory / "runtime-summary.json", json.dumps(summary, indent=2))
    print(json.dumps(summary))
    return summary