#!/usr/bin/env python3
"""Validate release attestations and render preview-only Stage 9 artifacts."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re

import yaml

try:
    from scripts.render_stage7_domain import ROOT, domain_hosts, render
except ModuleNotFoundError:
    from render_stage7_domain import ROOT, domain_hosts, render

PREPARE_CHECKS = {
    "private_origin_tls", "origin_bypass_denied", "admin_private_isolation",
    "waf_diagnostics_privacy", "private_link_approval", "rollback_plan",
}
CANARY_CHECKS = PREPARE_CHECKS | {
    "entra_backend_acl", "stage8_capture_architecture", "l3_governance_and_recovery",
    "required_protocol_matrix", "database_restore", "telemetry_alerts",
    "waf_detection_review", "approved_pilot_clients",
}
PRODUCTION_CHECKS = CANARY_CHECKS | {"canary_slo", "waf_prevention_review", "dns_cutover_and_rollback"}
PLS_ID = re.compile(r"^/subscriptions/[a-fA-F0-9-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/providers/Microsoft.Network/privateLinkServices/[A-Za-z0-9_.-]+$")
UUID = re.compile(r"^[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$")


def validate_release(config: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    phase = config.get("phase")
    if phase not in {"prepare", "canary", "production"}:
        raise ValueError("phase must be prepare, canary or production")
    domain_hosts(config["baseDomain"])
    if config.get("environmentName") not in {"dev", "test", "prod"}:
        raise ValueError("Invalid environment")
    if not PLS_ID.fullmatch(config.get("privateOrigin", {}).get("privateLinkServiceId", "")):
        raise ValueError("An explicit API Private Link Service resource ID is required")
    if not re.fullmatch(r"[a-z0-9]+", config["privateOrigin"].get("privateLinkLocation", "")):
        raise ValueError("An approved Private Link location is required")
    if not config.get("logAnalyticsWorkspaceName") or "REPLACE" in config["logAnalyticsWorkspaceName"]:
        raise ValueError("An explicit diagnostics workspace is required")
    if type(config.get("rateLimitPerMinute")) is not int or not 1 <= config["rateLimitPerMinute"] <= 100000:
        raise ValueError("Invalid rate limit")
    if config.get("wafMode") != ("Prevention" if phase == "production" else "Detection"):
        raise ValueError("Prepare/canary use Detection; production requires reviewed Prevention")
    if phase != "prepare" and not UUID.fullmatch(config.get("frontDoorId", "")):
        raise ValueError("Traffic requires the actual Front Door ID")
    if not config.get("changeTicket") or "REPLACE" in config["changeTicket"]:
        raise ValueError("Approved change ticket required")
    owners = config.get("approvedBy", [])
    if len(owners) < 2 or len(set(owners)) != len(owners) or any(not UUID.fullmatch(owner) for owner in owners):
        raise ValueError("Two distinct approval owners are required")
    required = {"prepare": PREPARE_CHECKS, "canary": CANARY_CHECKS, "production": PRODUCTION_CHECKS}[phase]
    for name in sorted(required):
        evidence = config.get("checks", {}).get(name, {})
        if evidence.get("passed") is not True or not evidence.get("report") or "REPLACE" in evidence["report"]:
            raise ValueError(f"Missing passing evidence: {name}")
        observed = datetime.fromisoformat(evidence.get("observedAt", "").replace("Z", "+00:00"))
        if observed.tzinfo is None or not now - timedelta(days=7) <= observed <= now:
            raise ValueError(f"Expired or invalid evidence timestamp: {name}")


def validate_origin_snapshot(load_balancer: dict, frontend_id: str, subnet: dict) -> None:
    if load_balancer.get("sku", {}).get("name") != "Standard":
        raise ValueError("Private Link requires a Standard load balancer")
    frontends = load_balancer.get("properties", {}).get("frontendIPConfigurations", [])
    selected = next((item for item in frontends if item.get("id", "").lower() == frontend_id.lower()), None)
    properties = (selected or {}).get("properties", {})
    address = ipaddress.ip_address(properties.get("privateIPAddress", ""))
    if properties.get("publicIPAddress") or not address.is_private or address.is_loopback or address.is_link_local or not properties.get("subnet", {}).get("id"):
        raise ValueError("API frontend must have a private address and subnet, with no public IP")
    if subnet.get("properties", {}).get("privateLinkServiceNetworkPolicies") != "Disabled":
        raise ValueError("PLS NAT subnet requires privateLinkServiceNetworkPolicies=Disabled")


def validate_what_if(result: dict) -> dict[str, int]:
    if result.get("status") != "Succeeded" or not isinstance(result.get("changes"), list):
        raise ValueError("What-if did not complete successfully")
    counts = {}
    for change in result["changes"]:
        kind = change.get("changeType")
        counts[kind] = counts.get(kind, 0) + 1
        if kind not in {"Create", "Ignore", "NoChange"}:
            raise ValueError(f"What-if requires review: {kind}; Modify/Delete/unsupported analysis cannot pass automatically")
    return counts


def generate(config: dict, output_dir: Path) -> None:
    validate_release(config)
    render(config["baseDomain"], output_dir, stage=9)
    overlay_path = output_dir / "kustomization.yaml"
    overlay = yaml.safe_load(overlay_path.read_text())
    if UUID.fullmatch(config.get("frontDoorId", "")):
        overlay["patches"].append({"patch": yaml.safe_dump({
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "llm-api-proxy"},
            "spec": {"template": {"spec": {"containers": [{"name": "auth-proxy", "env": [{"name": "FRONT_DOOR_ID", "value": config["frontDoorId"]}]}]}}},
        })})
    overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")
    parameters = {name: {"value": config[name]} for name in ("environmentName", "privateOrigin", "logAnalyticsWorkspaceName", "wafMode", "rateLimitPerMinute")}
    parameters.update({"baseDomain": {"value": domain_hosts(config["baseDomain"])["api"].removeprefix("llm-api.")}, "deployEdge": {"value": True}, "enableApiTraffic": {"value": config["phase"] != "prepare"}})
    (output_dir / "edge.parameters.json").write_text(json.dumps({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#", "contentVersion": "1.0.0.0", "parameters": parameters}, indent=2) + "\n", encoding="utf-8")
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    (output_dir / "release-summary.json").write_text(json.dumps({"phase": config["phase"], "configurationSha256": fingerprint, "attestationsChecked": True, "deploymentPerformed": False}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    release = commands.add_parser("render")
    release.add_argument("--config", type=Path, required=True)
    release.add_argument("--output-dir", type=Path, default=ROOT / "temp/stage9-release")
    what_if = commands.add_parser("check-what-if")
    what_if.add_argument("result", type=Path)
    origin = commands.add_parser("check-origin")
    origin.add_argument("--load-balancer", type=Path, required=True)
    origin.add_argument("--frontend-id", required=True)
    origin.add_argument("--subnet", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "render":
        generate(json.loads(args.config.read_text()), args.output_dir)
        print("Stage 9 preview artifacts generated. Attestations are not independent proof; no deployment was performed.")
    elif args.command == "check-origin":
        validate_origin_snapshot(json.loads(args.load_balancer.read_text()), args.frontend_id, json.loads(args.subnet.read_text()))
        print("Origin ARM snapshots meet private frontend requirements; controller ownership still requires review.")
    else:
        print(json.dumps(validate_what_if(json.loads(args.result.read_text()))))