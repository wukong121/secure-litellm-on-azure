#!/usr/bin/env python3
"""Generate an ignored, domain-specific Stage 7 overlay without deploying it."""

import argparse
import copy
import ipaddress
import json
import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def domain_hosts(base_domain: str) -> dict[str, str]:
    domain = base_domain.strip().lower().rstrip(".")
    labels = domain.split(".")
    if len(labels) < 2 or len(f"llm-admin.{domain}") > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("baseDomain must be a DNS name without scheme, port, path or wildcard")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("baseDomain cannot be an IP address")
    if domain == "example.com" or domain.endswith(".example.com"):
        raise ValueError("Replace the example.com placeholder with an environment domain")
    return {"api": f"llm-api.{domain}", "admin": f"llm-admin.{domain}"}


def domain_artifacts(base_domain: str, policy: dict, admin_application: dict) -> tuple[dict, dict, list]:
    hosts = domain_hosts(base_domain)
    policy = copy.deepcopy(policy)
    admin_application = copy.deepcopy(admin_application)
    policy.update(apiHost=hosts["api"], adminHost=hosts["admin"])
    admin_application["web"]["redirectUris"] = [f"https://{hosts['admin']}/auth/callback"]
    patches = []
    for plane, host in hosts.items():
        patches.append({
            "target": {"group": "networking.k8s.io", "version": "v1", "kind": "Ingress", "name": f"llm-{plane}"},
            "patch": json.dumps([
                {"op": "replace", "path": "/spec/rules/0/host", "value": host},
                {"op": "replace", "path": "/spec/tls/0/hosts/0", "value": host},
            ]),
        })
    return policy, admin_application, patches


def render(base_domain: str, output_dir: Path, stage: int = 7) -> None:
    if stage not in (7, 8, 9):
        raise ValueError("Only Stage 7, Stage 8 and Stage 9 domain overlays are supported")
    output_dir = output_dir.resolve()
    temp_root = (ROOT / "temp").resolve()
    if not output_dir.is_relative_to(temp_root) or output_dir == temp_root:
        raise ValueError("Generated environment artifacts must stay in an ignored temp/ subdirectory")
    policy, admin_application, patches = domain_artifacts(
        base_domain,
        json.loads((ROOT / "deploy/components/stage7-identity/policy.json").read_text(encoding="utf-8")),
        json.loads((ROOT / "auth-proxy/entra/admin-application.json").read_text(encoding="utf-8")),
    )
    overlay = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": [os.path.relpath(ROOT / f"deploy/validation/stage{stage}", output_dir)],
        "configMapGenerator": [{"name": "llm-auth-policy", "behavior": "merge", "files": ["policy.json"]}],
        "patches": patches,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, artifact in (("policy.json", policy), ("admin-application.json", admin_application)):
        (output_dir / name).write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    (output_dir / "kustomization.yaml").write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--base-domain")
    source.add_argument("--config", type=Path)
    parser.add_argument("--stage", type=int, choices=(7, 8, 9), default=7)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    base_domain = args.base_domain
    if base_domain is None:
        config_path = args.config or ROOT / "auth-proxy/domain.local.json"
        base_domain = json.loads(config_path.read_text(encoding="utf-8"))["baseDomain"]
    render(base_domain, args.output_dir or ROOT / f"temp/stage{args.stage}-domain", args.stage)
    print("Domain overlay and admin application template generated. No deployment was performed.")