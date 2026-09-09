"""Scoped one-job runner enrollment; never persist registration material in reports."""

import base64
import json
import re

from scripts.customer_migration import fingerprint, require

TOOLS = ("python3", "node", "az", "kubectl", "kubelogin", "docker", "gh", "cosign", "skopeo", "openssl", "psql", "pg_restore", "trivy", "syft")


def runner_snapshot(machine, network, subnet, repository, environment, image_id):
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "Invalid runner repository")
    tags = {"managedBy": "llmgw-runner", "repository": repository, "environment": environment}
    require(all(machine.get("tags", {}).get(key) == value and network.get("tags", {}).get(key) == value for key, value in tags.items()), "Runner resources lack the approved ownership tags")
    props = machine.get("properties", machine)
    nic = network.get("properties", network)
    require(machine.get("identity", {}).get("type", "None") == "None", "Runner VM must not have persistent cloud credentials")
    security = props.get("securityProfile", {})
    require(security.get("securityType") == "TrustedLaunch" and security.get("encryptionAtHost") is True and security.get("uefiSettings", {}).get("secureBootEnabled") is True, "Runner VM security profile differs")
    require(props["storageProfile"]["imageReference"]["id"].lower() == image_id.lower() and re.search(r"/versions/[0-9]+\.[0-9]+\.[0-9]+$", image_id), "Use an exact approved gallery image version")
    require(not props["storageProfile"].get("dataDisks"), "Unexpected persistent runner data disk")
    require(len(props["networkProfile"]["networkInterfaces"]) == 1 and props["networkProfile"]["networkInterfaces"][0]["id"].lower() == network["id"].lower(), "Runner network interface mismatch")
    addresses = nic.get("ipConfigurations", [])
    require(len(addresses) == 1 and not nic.get("enableIPForwarding"), "Runner must have one non-forwarding private interface")
    address = addresses[0].get("properties", addresses[0])
    require(not address.get("publicIPAddress") and address["subnet"]["id"].lower() == subnet["id"].lower(), "Runner has a public address or unexpected subnet")
    subnet_props = subnet.get("properties", subnet)
    require(subnet_props.get("routeTable", {}).get("id") and not subnet_props.get("natGateway"), "Runner requires reviewed firewall routing, not direct NAT egress")
    return {"machineId": machine["id"], "vmId": props["vmId"], "networkId": network["id"], "subnetId": subnet["id"], "imageId": image_id, "repository": repository, "environment": environment, "tags": tags}


def start_script():
    checks = "\n".join(f"command -v {tool} >/dev/null" for tool in TOOLS)
    return "\n".join([
        "#!/usr/bin/env bash", "set -euo pipefail", "set +x", "umask 077",
        "exec >/dev/null 2>&1", checks,
        "test -x /opt/actions-runner/run.sh",
        "test -z \"$(find /opt/actions-runner -maxdepth 1 -name '.runner' -print)\"",
        "test -n \"${LLMGW_JIT_CONFIG:-}\"",
        "cd /opt/actions-runner",
        "trap 'unset LLMGW_JIT_CONFIG' EXIT",
        "timeout --signal=TERM --kill-after=30s 7500 ./run.sh --jitconfig \"$LLMGW_JIT_CONFIG\"",
    ])


def enroll_runner(snapshot, approved, current_snapshot, registration, managed_command, save):
    require(fingerprint(snapshot) == approved and current_snapshot() == snapshot, "Runner resources changed or were not approved")
    require(registration.is_private_admin(snapshot["repository"]), "Runner enrollment requires the approved private repository")
    name = "llmgw-" + fingerprint({"vmId": snapshot["vmId"], "repo": snapshot["repository"]})[:24]
    require(not registration.find(snapshot["repository"], name), "Runner already registered; inspect it instead of replacing credentials")
    record = {"snapshot": snapshot, "name": name, "phase": "registering"}
    save(record)
    generated = registration.generate(snapshot["repository"], {"name": name, "runner_group_id": 1, "work_folder": "_work", "labels": ["self-hosted", "linux", "x64", "llmgw-" + snapshot["environment"], name]})
    runner = generated["runner"]
    require(runner.get("name") == name and runner.get("ephemeral") is True and type(runner.get("id")) is int, "GitHub did not return the expected one-job runner")
    encoded = generated["encoded_jit_config"]
    require(isinstance(encoded, str) and 1 <= len(encoded) <= 64000, "Invalid JIT configuration size")
    base64.b64decode(encoded, validate=True)
    record.update(runnerId=runner["id"], phase="registered")
    save(record)
    require(current_snapshot() == snapshot, "Runner changed after enrollment; do not transmit JIT credentials")
    body = {"properties": {"source": {"script": start_script()}, "protectedParameters": [{"name": "LLMGW_JIT_CONFIG", "value": encoded}], "runAsUser": "runner", "timeoutInSeconds": 7560, "asyncExecution": True}}
    managed_command(snapshot["machineId"], body)
    record["phase"] = "started"
    save(record)
    return record


def retire_runner(record, current_snapshot, registration, delete_machine, save):
    snapshot = record["snapshot"]
    require(current_snapshot() == snapshot, "Runner VM was replaced or drifted; refuse cleanup")
    runners = registration.find(snapshot["repository"], record["name"])
    require(len(runners) <= 1, "Ambiguous runner cleanup target")
    if runners:
        runner = runners[0]
        require(runner["id"] == record.get("runnerId") and runner.get("busy") is False, "Runner is busy or was replaced; do not delete")
        registration.remove(snapshot["repository"], runner["id"])
    require(current_snapshot() == snapshot, "Runner changed before VM cleanup")
    save({**record, "phase": "retiring"})
    delete_machine(snapshot["machineId"], snapshot["vmId"])
    saved = {**record, "phase": "retired"}
    save(saved)
    return saved