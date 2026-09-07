#!/usr/bin/env python3
"""Check compiled Stage 9 ARM and rendered manifests, without deployment."""

import json
import sys
from pathlib import Path

import yaml

API_PATHS = {"/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses", "/embeddings", "/v1/embeddings"}


def resources(template):
    value = template["resources"]
    return [item for item in (value.values() if isinstance(value, dict) else value) if not item.get("existing")]


def validate_templates(edge, origin):
    for parameter in ("deployEdge", "enableApiTraffic"):
        assert edge["parameters"][parameter]["defaultValue"] is False
    assert edge["parameters"]["wafMode"]["defaultValue"] == "Detection"
    assert origin["parameters"]["deployPrivateOrigin"]["defaultValue"] is False
    edge_resources = resources(edge)
    by_type = {item["type"]: item for item in edge_resources}
    expected_types = {
        "Microsoft.Cdn/profiles", "Microsoft.Cdn/profiles/afdEndpoints", "Microsoft.Cdn/profiles/customDomains",
        "Microsoft.Cdn/profiles/originGroups", "Microsoft.Cdn/profiles/originGroups/origins",
        "Microsoft.Network/FrontDoorWebApplicationFirewallPolicies", "Microsoft.Cdn/profiles/securityPolicies",
        "Microsoft.Cdn/profiles/afdEndpoints/routes", "Microsoft.Insights/diagnosticSettings",
    }
    assert len(edge_resources) == len(expected_types) and set(by_type) == expected_types
    assert all(item.get("condition") == "[parameters('deployEdge')]" for item in edge_resources)
    assert by_type["Microsoft.Cdn/profiles"]["sku"]["name"] == "Premium_AzureFrontDoor"
    route = by_type["Microsoft.Cdn/profiles/afdEndpoints/routes"]["properties"]
    assert set(route["patternsToMatch"]) == API_PATHS
    assert route["supportedProtocols"] == ["Https"]
    assert route["forwardingProtocol"] == "HttpsOnly"
    assert route["linkToDefaultDomain"] == "Disabled"
    assert route["enabledState"] == "[variables('trafficState')]"
    assert "cacheConfiguration" not in route and route["ruleSets"] == []
    assert len(route["customDomains"]) == 1
    assert by_type["Microsoft.Cdn/profiles/customDomains"]["properties"]["hostName"] == "[variables('apiHost')]"
    assert edge["variables"]["apiHost"] == "[format('llm-api.{0}', parameters('baseDomain'))]"
    origin_properties = by_type["Microsoft.Cdn/profiles/originGroups/origins"]["properties"]
    assert origin_properties["enforceCertificateNameCheck"] is True
    assert origin_properties["originHostHeader"] == origin_properties["hostName"] == "[variables('apiHost')]"
    assert "privateLinkServiceId" in origin_properties["sharedPrivateLinkResource"]["privateLink"]["id"]
    assert "status" not in origin_properties["sharedPrivateLinkResource"]
    assert by_type["Microsoft.Cdn/profiles/originGroups"]["properties"]["healthProbeSettings"]["probePath"] == "/readyz"
    waf = by_type["Microsoft.Network/FrontDoorWebApplicationFirewallPolicies"]["properties"]
    assert waf["policySettings"]["requestBodyCheck"] == "Enabled"
    assert waf["policySettings"]["logScrubbing"]["state"] == "Enabled"
    assert {item["ruleSetType"] for item in waf["managedRules"]["managedRuleSets"]} == {"Microsoft_DefaultRuleSet", "Microsoft_BotManagerRuleSet"}
    assert not waf["managedRules"].get("exclusions")
    assert all(rule["action"] != "Allow" for rule in waf["customRules"]["rules"])
    association = by_type["Microsoft.Cdn/profiles/securityPolicies"]["properties"]["parameters"]["associations"]
    assert len(association) == 1 and len(association[0]["domains"]) == 1
    assert association[0]["patternsToMatch"] == ["/*"]
    origin_resources = resources(origin)
    assert len(origin_resources) == 1
    pls = origin_resources[0]
    assert pls["type"] == "Microsoft.Network/privateLinkServices"
    assert pls["condition"] == "[parameters('deployPrivateOrigin')]"
    assert pls["properties"]["autoApproval"]["subscriptions"] == []
    assert pls["properties"]["enableProxyProtocol"] is False


def validate_manifests(documents):
    objects = {(item["kind"], item["metadata"]["name"]): item for item in documents}
    ingresses = [item for item in documents if item["kind"] == "Ingress"]
    assert len(ingresses) == 2
    api = objects[("Ingress", "llm-api")]["spec"]
    admin = objects[("Ingress", "llm-admin")]["spec"]
    assert api["ingressClassName"] == "REPLACE_PRIVATE_API_INGRESS_CLASS"
    assert admin["ingressClassName"] == "REPLACE_PRIVATE_ADMIN_INGRESS_CLASS"
    assert api["rules"][0]["host"] == "llm-api.example.com"
    assert admin["rules"][0]["host"] == "llm-admin.example.com"
    paths = api["rules"][0]["http"]["paths"]
    assert {item["path"] for item in paths} == API_PATHS | {"/readyz"}
    assert all(item["pathType"] == "Exact" and item["backend"]["service"]["name"] == "llm-api-proxy" for item in paths)
    for plane in ("api", "admin"):
        ingress = objects[("Ingress", f"llm-{plane}")]["spec"]
        assert ingress["tls"][0]["hosts"] == [f"llm-{plane}.example.com"]
        env = objects[("Deployment", f"llm-{plane}-proxy")]["spec"]["template"]["spec"]["containers"][0]["env"]
        edge_values = [item for item in env if item["name"] == "FRONT_DOOR_ID"]
        assert edge_values == ([{"name": "FRONT_DOOR_ID", "value": "REPLACE_EXPECTED_FRONT_DOOR_ID"}] if plane == "api" else [])
        policy = objects[("NetworkPolicy", f"llm-{plane}-proxy-ingress")]["spec"]
        assert policy["ingress"][0]["from"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": f"llm-{plane}-ingress"}}, "podSelector": {"matchLabels": {"app.kubernetes.io/component": "controller"}}}]
    assert objects[("NetworkPolicy", "allow-litellm-required-traffic")]["spec"]["ingress"] == [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "entra-auth-proxy"}}}], "ports": [{"protocol": "TCP", "port": 4000}]}]
    assert all(item["spec"]["type"] == "ClusterIP" for item in documents if item["kind"] == "Service")
    settings = next(item["data"]["config.json"] for item in documents if item["kind"] == "ConfigMap" and "config.json" in item.get("data", {}))
    assert json.loads(settings)["l3"]["enabled"] is False


if __name__ == "__main__":
    validate_templates(json.loads(Path(sys.argv[1]).read_text()), json.loads(Path(sys.argv[2]).read_text()))
    validate_manifests(list(yaml.safe_load_all(Path(sys.argv[3]).read_text())))
    print("Stage 9 compiled edge/PLS templates and isolated API/admin manifests passed.")