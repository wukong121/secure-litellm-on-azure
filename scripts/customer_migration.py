#!/usr/bin/env python3
"""Customer migration guidance and read-only previews; never deploy or cut over."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID

from scripts.render_stage7_domain import domain_hosts, render

ROOT = Path(__file__).resolve().parents[1]
STAGES = (
    ("Baseline and recoverable backup", ("inventory", "backup_restore", "key_salt_recovery", "protocol_baseline")),
    ("Legacy minimum hardening", ("legacy_health", "alerts_received", "rollback_snapshot")),
    ("Customer architecture decisions", ("network_capacity", "identity_owners", "pg_auth_ha", "l3_policy", "protocol_scope")),
    ("Supply chain and isolated target", ("oidc_scope", "image_signature_sbom", "target_isolation")),
    ("Private network and AKS", ("private_dns_egress", "private_runner", "workload_identity", "private_ingress")),
    ("Data platform and restore rehearsal", ("pg_migration_restore", "key_salt_decryption", "redis_entra", "csi_rotation")),
    ("HA and routing", ("replica_failure", "load_affinity", "capacity_limits", "no_legacy_db_writes")),
    ("Entra and split domains", ("tenant_negative_tests", "object_ownership", "admin_private", "required_protocols")),
    ("L3 and observability", ("durable_audit_recovery", "audit_governance", "telemetry_received", "guardrail_scope")),
    ("Approved pilot and cutover", ("enabled_what_if", "origin_tls_private_link", "pilot_regression", "rollback_rehearsal", "dual_owner_release")),
)
COMPONENTS = {
    "backup": (0, "backup-storage", {"storageAccountName", "virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix", "logAnalyticsWorkspaceName", "backupOwnerPrincipalId", "storageSku", "softDeleteRetentionDays"}),
    "monitoring": (1, "monitoring", {"logAnalyticsWorkspaceName"}),
    "platform": (3, "environments", {"containerRegistryName", "logAnalyticsWorkspaceName", "stage4Network", "stage4Aks", "stage5Data", "approvedHttpsFqdns", "azureOpenAIConnections", "createStage5KeyVaultPrivateDnsZone", "createStage5PostgresqlPrivateDnsZone", "createStage5ManagedRedisPrivateDnsZone"}),
    "audit": (8, "audit-storage", {"storageAccountName", "virtualNetworkName", "privateEndpointSubnetName", "logAnalyticsWorkspaceName", "cmkVaultName", "cmkKeyName", "writerPrincipalId", "readerPrincipalId", "retentionPrincipalId"}),
    "origin": (9, "edge-origin", {"privateLinkServiceName", "virtualNetworkName", "ingressSubnetName", "apiLoadBalancer"}),
    "edge": (9, "edge", {"privateOrigin", "logAnalyticsWorkspaceName", "rateLimitPerMinute"}),
}
REQUIRED = {
    "backup": {"logAnalyticsWorkspaceName", "backupOwnerPrincipalId", "virtualNetworkName", "virtualNetworkAddressPrefix", "privateEndpointSubnetName", "privateEndpointSubnetPrefix"},
    "monitoring": {"logAnalyticsWorkspaceName"},
    "platform": {"containerRegistryName", "logAnalyticsWorkspaceName", "stage4Network", "stage4Aks"},
    "audit": {"virtualNetworkName", "privateEndpointSubnetName", "logAnalyticsWorkspaceName", "cmkVaultName", "cmkKeyName", "writerPrincipalId", "readerPrincipalId", "retentionPrincipalId"},
    "origin": {"virtualNetworkName", "ingressSubnetName", "apiLoadBalancer"},
    "edge": {"privateOrigin", "logAnalyticsWorkspaceName"},
}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class MigrationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise MigrationError(message)


def configured(value):
    if isinstance(value, dict):
        return all(configured(item) for item in value.values())
    if isinstance(value, list):
        return all(configured(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip()) and not re.search(r"REPLACE|example\.(com|net|org)|example-workspace|stage3check|<|>", value, re.I)
    return value is not None


def validate_config(config, environment):
    require(isinstance(config, dict) and config.get("schemaVersion") == 1, "Unsupported customer configuration")
    require(set(config) == {"schemaVersion", "environment", "azure", "location", "baseDomain", "ownerEmail", "legacy", "target", "parameters"}, "Unexpected or missing customer configuration fields")
    require(environment in {"dev", "test", "prod"} and config["environment"] == environment, "Environment mismatch")
    require(set(config["azure"]) == {"tenantId", "subscriptionId"}, "Invalid Azure scope fields")
    require(all(UUID(value).int for value in config["azure"].values()), "Nonzero Azure scope identifiers are required")
    require(configured({key: config[key] for key in ("location", "baseDomain", "ownerEmail", "legacy", "target")}), "Replace customer configuration placeholders")
    domain_hosts(config["baseDomain"])
    require(re.fullmatch(r"[a-z0-9]+", config["location"]) is not None, "Invalid location")
    require(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", config["ownerEmail"]) is not None, "Invalid ownerEmail")
    require(set(config["legacy"]) == {"resourceGroup", "aksClusterName", "namespace", "postgresPvc"}, "Invalid legacy inventory fields")
    require(set(config["target"]) == {"resourceGroup"}, "Invalid target fields")
    require(config["legacy"]["resourceGroup"].lower() != config["target"]["resourceGroup"].lower(), "Target must use a separate resource group; in-place migration is not supported")
    for group in (config["legacy"]["resourceGroup"], config["target"]["resourceGroup"]):
        require(re.fullmatch(r"[a-zA-Z0-9_().-]{1,90}", group) is not None and not group.endswith("."), "Invalid resource group name")
    require(isinstance(config["parameters"], dict) and not set(config["parameters"]) - COMPONENTS.keys(), "Unknown parameter component")
    for component, parameters in config["parameters"].items():
        require(isinstance(parameters, dict) and not set(parameters) - COMPONENTS[component][2], "Unknown or reserved component parameter")
    return config


def validate_evidence(evidence, stage, config, revision, now=None):
    require(re.fullmatch(r"[0-9a-f]{40}", revision or "") is not None, "A full reviewed Git revision is required")
    require(isinstance(evidence, list), "Evidence must be an array")
    now = now or datetime.now(timezone.utc)
    completed = set()
    for item in evidence:
        require(isinstance(item, dict), "Invalid evidence record")
        previous = item.get("stage")
        require(type(previous) is int and 0 <= previous <= 9 and previous not in completed, "Invalid or duplicate evidence stage")
        require(item.get("environment") == config["environment"] and item.get("configSha256") == fingerprint(config) and item.get("revision") == revision, "Evidence configuration or revision mismatch")
        require(item.get("status") == "passed" and set(item.get("checks", [])) == set(STAGES[previous][1]), "Stage checks are incomplete")
        approvers = item.get("approvedBy", [])
        require(isinstance(approvers, list) and len(approvers) == 2, "Two distinct approver object IDs are required")
        try:
            identities = {UUID(value) for value in approvers}
            observed = datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            raise ValueError("Invalid evidence approval or timestamp") from None
        require(len(identities) == 2 and all(identity.int for identity in identities), "Two distinct nonzero approver object IDs are required")
        require(observed.tzinfo is not None and now - timedelta(days=7) <= observed <= now, "Evidence must be from the last seven days")
        report = urlsplit(item.get("reportUrl", ""))
        require(report.scheme == "https" and bool(report.hostname) and not report.username and not report.password and not report.query, "Report must be a private HTTPS reference without embedded credentials or query tokens")
        require(re.fullmatch(r"[0-9a-f]{64}", item.get("reportSha256", "")) is not None, "Report hash is required")
        completed.add(previous)
    require(set(range(stage)).issubset(completed), "Prior stage evidence is missing; do not skip migration gates")
    return completed


def parameters_for(config, stage, component):
    require(component in COMPONENTS, "Select an IaC component")
    first_stage, template, _allowed = COMPONENTS[component]
    require(stage in ({3, 4, 5} if component == "platform" else {first_stage}), "Component does not belong to the selected stage")
    parameters = dict(config["parameters"].get(component, {}))
    if component == "platform" and stage < 5:
        parameters.pop("stage5Data", None)
    if component == "platform" and stage < 4:
        parameters.pop("azureOpenAIConnections", None)
    require(REQUIRED[component].issubset(parameters) and configured(parameters), "Component configuration is missing or contains placeholders")
    if component == "platform":
        if stage >= 5:
            require("stage5Data" in parameters, "Stage 5 database configuration is required")
        require(parameters["stage4Aks"]["name"] != config["legacy"]["aksClusterName"], "New AKS must not reuse the legacy name")
        parameters.update(environmentName=config["environment"], deployContainerRegistry=True, deployStage4=stage >= 4, deployStage5=stage >= 5, containerRegistryPublicNetworkAccess="Disabled")
    elif component == "backup":
        parameters["backupOwnerUpn"] = config["ownerEmail"]
    elif component == "monitoring":
        require(config["legacy"]["namespace"] == "litellm" and config["legacy"]["postgresPvc"] == "pg-data", "Monitoring queries currently require litellm namespace and pg-data PVC; adapt and test queries first")
        parameters.update(aksClusterName=config["legacy"]["aksClusterName"], ownerEmail=config["ownerEmail"])
    elif component == "audit":
        parameters["deployAuditStorage"] = True
    elif component == "origin":
        parameters["deployPrivateOrigin"] = True
    elif component == "edge":
        parameters.update(deployEdge=True, enableApiTraffic=False, environmentName=config["environment"], baseDomain=config["baseDomain"], wafMode="Detection")
    if component != "edge":
        parameters["location"] = config["location"]
    parameters["tags"] = {"owner": config["ownerEmail"], "environment": config["environment"], "workload": "litellm", "managedBy": "bicep"}
    return ROOT / f"infra/{template}/main.bicep", {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in parameters.items()},
    }


def private_write(path, content):
    require(not path.is_symlink(), "Refusing symlink output")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def prepare(config, stage, component, destination):
    destination = destination.resolve()
    require(destination.is_relative_to((ROOT / "temp").resolve()) and destination != (ROOT / "temp").resolve(), "Output must stay under ignored temp/")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    template, parameters = parameters_for(config, stage, component)
    path = destination / "parameters.json"
    private_write(path, json.dumps(parameters, indent=2) + "\n")
    return template, path


def preview(config, template, path, component):
    group = config["legacy" if component == "monitoring" else "target"]["resourceGroup"]
    result = subprocess.run(["az", "deployment", "group", "what-if", "--resource-group", group, "--template-file", str(template), "--parameters", f"@{path}", "--result-format", "FullResourcePayloads", "--no-pretty-print", "--output", "json"], capture_output=True, text=True, check=False)
    private_write(path.parent / "what-if.json", result.stdout)
    private_write(path.parent / "diagnostics.txt", result.stderr)
    require(result.returncode == 0, "What-if failed; detailed diagnostics are private local output")
    result = json.loads(result.stdout)
    require(result.get("status") == "Succeeded" and isinstance(result.get("changes"), list), "Unrecognized What-if response")
    counts = {}
    for change in result["changes"]:
        kind = change.get("changeType", "Unknown")
        counts[kind] = counts.get(kind, 0) + 1
    print(json.dumps({"changeCounts": counts, "deploymentPerformed": False}))
    require(not set(counts) - {"Create", "NoChange", "Ignore"}, "Review required: Modify/Delete/Unsupported/unknown changes block this gate; no deployment performed")
    require(counts.get("Create", 0) > 0, "No creation detected; a disabled or already-deployed preview is not a new-stage deployment approval")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=range(10), required=True)
    parser.add_argument("--mode", choices=("guide", "preflight", "what-if"), default="guide")
    parser.add_argument("--component", choices=tuple(COMPONENTS) + ("none",), default="none")
    parser.add_argument("--environment", choices=("dev", "test", "prod"), required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/customer-migration")
    args = parser.parse_args()
    title, checks = STAGES[args.stage]
    print(f"Stage {args.stage}: {title}\nRequired exit evidence: {', '.join(checks)}")
    print("Runbook: docs/customer-migration-guide-zh.md; no automatic deployment, DNS change or retirement.")
    if args.mode == "guide":
        return
    config = validate_config(json.loads(os.environ["CUSTOMER_CONFIG_JSON"]), args.environment)
    require(os.environ.get("AZURE_TENANT_ID") == config["azure"]["tenantId"] and os.environ.get("AZURE_SUBSCRIPTION_ID") == config["azure"]["subscriptionId"], "OIDC scope does not match customer configuration")
    print(json.dumps({"configSha256": fingerprint(config), "stage": args.stage, "environment": args.environment}))
    completed = validate_evidence(json.loads(os.environ.get("MIGRATION_EVIDENCE_JSON", "[]")), args.stage, config, os.environ.get("GITHUB_SHA", os.environ.get("MIGRATION_REVISION", "")))
    require(not (args.component == "backup" and any(stage >= 4 for stage in completed)), "Do not reapply the bootstrap VNet after Stage 4")
    if args.component != "none":
        template, path = prepare(config, args.stage, args.component, args.output_dir)
        if args.mode == "what-if":
            preview(config, template, path, args.component)
    else:
        require(args.mode != "what-if", "What-if needs an IaC component")
        if args.stage in (7, 8, 9):
            render(config["baseDomain"], args.output_dir, args.stage)
            print("Domain overlay generated; identity, image and secret placeholders still require integration. Not deployable as-is.")
    print("Readiness metadata checked, not an independent audit of evidence or cloud acceptance.")


if __name__ == "__main__":
    try:
        main()
    except MigrationError as error:
        raise SystemExit(str(error)) from None
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        raise SystemExit("Migration check failed. Verify configuration, stage evidence and private local diagnostics; input values are not logged.") from None