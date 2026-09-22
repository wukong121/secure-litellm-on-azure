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
    for parameter in ("deployEdge", "enableApiTraffic", "enableAdminTraffic"):
        assert edge["parameters"][parameter]["defaultValue"] is False
    assert edge["parameters"]["wafMode"]["defaultValue"] == "Detection"
    assert origin["parameters"]["deployPrivateOrigin"]["defaultValue"] is False
    managed = [item for item in resources(edge) if item.get("condition") == "[parameters('deployEdge')]" ]
    by_type = {}
    for item in managed:
        by_type.setdefault(item["type"], []).append(item)
    assert {kind: len(items) for kind, items in by_type.items()} == {
        "Microsoft.Cdn/profiles": 1,
        "Microsoft.Cdn/profiles/secrets": 1,
        "Microsoft.Cdn/profiles/afdEndpoints": 2,
        "Microsoft.Cdn/profiles/customDomains": 2,
        "Microsoft.Cdn/profiles/originGroups": 2,
        "Microsoft.Cdn/profiles/originGroups/origins": 2,
        "Microsoft.Network/FrontDoorWebApplicationFirewallPolicies": 2,
        "Microsoft.Cdn/profiles/securityPolicies": 2,
        "Microsoft.Cdn/profiles/afdEndpoints/routes": 2,
        "Microsoft.Insights/diagnosticSettings": 1,
        "Microsoft.Resources/deployments": 1,
    }
    profile = by_type["Microsoft.Cdn/profiles"][0]
    assert profile["sku"]["name"] == "Premium_AzureFrontDoor"
    assert profile["identity"] == {"type": "SystemAssigned"}
    ca_secret = by_type["Microsoft.Cdn/profiles/secrets"][0]
    assert ca_secret["apiVersion"] == "2026-08-01-preview"
    assert ca_secret["copy"]["count"] == "[length(parameters('adminMtls').trustedClientCaSecrets)]"
    assert ca_secret["properties"]["parameters"] == {
        "type": "MtlsCertificateChain",
        "secretSource": {"id": "[format('{0}/secrets/{1}', variables('adminMtlsVaultId'), parameters('adminMtls').trustedClientCaSecrets[copyIndex()].secretName)]"},
        "secretVersion": "[parameters('adminMtls').trustedClientCaSecrets[copyIndex()].secretVersion]",
    }
    endpoints = {"admin" if "llm-admin" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/afdEndpoints"]}
    assert set(endpoints) == {"api", "admin"}
    assert endpoints["api"]["properties"]["enabledState"] == "[variables('apiTrafficState')]"
    assert endpoints["admin"]["properties"]["enabledState"] == "[variables('adminTrafficState')]"
    domains = {"admin" if "llm-admin" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/customDomains"]}
    assert set(domains) == {"api", "admin"}
    assert domains["api"]["properties"]["hostName"] == "[variables('apiHost')]" and "mtlsSettings" not in domains["api"]["properties"]
    admin_mtls = domains["admin"]["properties"]["mtlsSettings"]
    assert domains["admin"]["apiVersion"] == "2026-08-01-preview" and domains["admin"]["properties"]["hostName"] == "[variables('adminHost')]"
    assert admin_mtls["scenario"] == "ClientCertificateRequiredAndValidated"
    assert admin_mtls["certificateRevocationCheck"] == "Enabled"
    assert admin_mtls["allowedFqdns"] == "[parameters('adminMtls').allowedCertificateFqdns]"
    assert len(admin_mtls["copy"]) == 1 and admin_mtls["copy"][0]["name"] == "secrets"
    assert edge["variables"]["apiHost"] == "[format('llm-api.{0}', parameters('baseDomain'))]"
    assert edge["variables"]["adminHost"] == "[format('llm-admin.{0}', parameters('baseDomain'))]"
    origin_groups = {"admin" if "private-admin" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/originGroups"]}
    origins = {"admin" if "private-admin" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/originGroups/origins"]}
    assert set(origin_groups) == set(origins) == {"api", "admin"}
    for plane in ("api", "admin"):
        properties = origins[plane]["properties"]
        assert properties["enforceCertificateNameCheck"] is True
        assert properties["originHostHeader"] == properties["hostName"] == f"[variables('{plane}Host')]"
        origin_parameter = "privateOrigin" if plane == "api" else "adminPrivateOrigin"
        assert properties["sharedPrivateLinkResource"]["privateLink"]["id"] == f"[parameters('{origin_parameter}').privateLinkServiceId]"
        assert "status" not in properties["sharedPrivateLinkResource"]
        assert origin_groups[plane]["properties"]["healthProbeSettings"]["probePath"] == "/readyz"
    wafs = {"admin" if "wafllmadmin" in item["name"] else "api": item for item in by_type["Microsoft.Network/FrontDoorWebApplicationFirewallPolicies"]}
    assert set(wafs) == {"api", "admin"}
    for waf_resource in wafs.values():
        waf = waf_resource["properties"]
        assert waf["policySettings"]["requestBodyCheck"] == "Enabled"
        assert waf["policySettings"]["logScrubbing"]["state"] == "Enabled"
        assert {item["ruleSetType"] for item in waf["managedRules"]["managedRuleSets"]} == {"Microsoft_DefaultRuleSet", "Microsoft_BotManagerRuleSet"}
        assert not waf["managedRules"].get("exclusions")
        assert all(rule["action"] != "Allow" for rule in waf["customRules"]["rules"])
    block_non_post = next(rule for rule in wafs["api"]["properties"]["customRules"]["rules"] if rule["name"] == "BlockNonPost")
    assert block_non_post["matchConditions"] == [{"matchVariable": "RequestMethod", "operator": "Equal", "negateCondition": True, "matchValue": ["POST"]}]
    assert {rule["name"] for rule in wafs["admin"]["properties"]["customRules"]["rules"]} == {"BlockUnsafeMethods", "RateLimitAdmin"}
    policies = {"admin" if "admin-waf" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/securityPolicies"]}
    routes = {"admin" if "admin-mtls-only" in item["name"] else "api": item for item in by_type["Microsoft.Cdn/profiles/afdEndpoints/routes"]}
    assert set(policies) == set(routes) == {"api", "admin"}
    for plane in ("api", "admin"):
        association = policies[plane]["properties"]["parameters"]["associations"]
        assert len(association) == 1 and len(association[0]["domains"]) == 1 and association[0]["patternsToMatch"] == ["/*"]
        route = routes[plane]["properties"]
        assert route["supportedProtocols"] == ["Https"] and route["forwardingProtocol"] == "HttpsOnly"
        assert route["linkToDefaultDomain"] == "Disabled" and "cacheConfiguration" not in route and route["ruleSets"] == []
        assert len(route["customDomains"]) == 1
    assert set(routes["api"]["properties"]["patternsToMatch"]) == API_PATHS
    assert routes["api"]["properties"]["enabledState"] == "[variables('apiTrafficState')]"
    assert routes["admin"]["properties"]["patternsToMatch"] == ["/*"]
    assert routes["admin"]["properties"]["enabledState"] == "[variables('adminTrafficState')]"
    output = edge["outputs"]["edge"]["value"]
    assert output["privateOrigin"] == "[parameters('privateOrigin')]"
    assert output["adminPrivateOrigin"] == "[parameters('adminPrivateOrigin')]"
    assert output["adminMtls"] == "[parameters('adminMtls')]"
    assert output["adminMtlsMode"] == "ClientCertificateRequiredAndValidated"
    assert output["endpointHost"] != output["adminEndpointHost"] and output["routeId"] != output["adminRouteId"]
    origin_resources = resources(origin)
    assert len(origin_resources) == 2 and all(item["type"] == "Microsoft.Network/privateLinkServices" for item in origin_resources)
    private_links = {"admin" if "adminPrivateLinkServiceName" in item["name"] else "api": item for item in origin_resources}
    assert set(private_links) == {"api", "admin"}
    for plane, pls in private_links.items():
        assert pls["condition"] == "[parameters('deployPrivateOrigin')]"
        assert pls["properties"]["autoApproval"]["subscriptions"] == []
        assert pls["properties"]["enableProxyProtocol"] is False
        frontend = pls["properties"]["loadBalancerFrontendIpConfigurations"][0]["id"]
        assert f"parameters('{plane}LoadBalancer')" in frontend


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
        assert edge_values == [{"name": "FRONT_DOOR_ID", "value": "REPLACE_EXPECTED_FRONT_DOOR_ID"}]
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