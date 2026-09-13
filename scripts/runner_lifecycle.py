"""Scoped one-job runner enrollment; never persist registration material in reports."""

import base64
import json
import re

from scripts.customer_migration import fingerprint, require

TOOLS = ("python3", "node", "az", "kubectl", "kubelogin", "docker", "gh", "cosign", "skopeo", "openssl", "psql", "pg_restore", "trivy", "syft")


class GitHubRunnerRegistration:
    def __init__(self, repository, token, session=None):
        import requests

        require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "Invalid runner repository")
        require(isinstance(token, str) and bool(token.strip()) and not any(value.isspace() for value in token), "Runner registration requires an installation token")
        self.repository = repository
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def close(self):
        self.headers.clear()
        self.session.close()

    def request(self, repository, method, suffix="", body=None):
        require(repository == self.repository, "Runner registration cannot cross repository scope")
        try:
            response = self.session.request(method, "https://api.github.com/repos/" + repository + suffix,
                                            headers=self.headers, json=body, timeout=60, allow_redirects=False)
            expected = {"GET": 200, "POST": 201, "DELETE": 204}[method]
            require(response.status_code == expected, "GitHub runner operation failed; recheck registration state before retrying")
            if method == "DELETE":
                return None
            document = response.json()
            require(isinstance(document, dict), "Invalid GitHub runner response")
            return document
        except Exception:
            raise ValueError("GitHub runner operation failed; response and credentials are suppressed") from None

    def is_private_admin(self, repository):
        document = self.request(repository, "GET")
        return document.get("full_name", "").lower() == repository.lower() and document.get("private") is True and document.get("permissions", {}).get("admin") is True

    def find(self, repository, name):
        require(re.fullmatch(r"llmgw-[a-f0-9]{24}", name), "Unmanaged runner name")
        matched = []
        seen = set()
        for page in range(1, 11):
            document = self.request(repository, "GET", f"/actions/runners?per_page=100&page={page}")
            require(type(document.get("total_count")) is int and 0 <= document["total_count"] <= 1000 and isinstance(document.get("runners"), list), "Runner enumeration exceeds its bound")
            runners = document["runners"]
            require(len(runners) <= 100, "Unexpected runner page size")
            for runner in runners:
                require(isinstance(runner, dict) and type(runner.get("id")) is int and runner["id"] > 0 and runner["id"] not in seen, "Invalid or repeated runner ID")
                seen.add(runner["id"])
                if runner.get("name") == name:
                    matched.append(runner)
            if len(seen) >= document["total_count"]:
                require(len(seen) == document["total_count"] and len(matched) <= 1, "Runner inventory changed; retry discovery")
                return matched
            require(len(runners) == 100, "Incomplete runner enumeration")
        raise ValueError("Runner enumeration did not finish")

    def generate(self, repository, body):
        require(isinstance(body, dict) and set(body) == {"name", "runner_group_id", "work_folder", "labels"}, "Unexpected JIT registration fields")
        require(re.fullmatch(r"llmgw-[a-f0-9]{24}", body["name"]) and body["work_folder"] == "_work", "Unapproved JIT runner identity or work folder")
        require(type(body["runner_group_id"]) is int and body["runner_group_id"] > 0, "Invalid approved runner group")
        require(isinstance(body["labels"], list) and body["name"] in body["labels"] and all(isinstance(label, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", label) for label in body["labels"]), "Invalid JIT labels")
        return self.request(repository, "POST", "/actions/runners/generate-jitconfig", body)

    def remove(self, repository, runner_id):
        require(type(runner_id) is int and runner_id > 0, "Invalid runner ID")
        self.request(repository, "DELETE", "/actions/runners/" + str(runner_id))


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


def enroll_runner(snapshot, approved, current_snapshot, registration, managed_command, save, runner_group_id=1):
    require(fingerprint(snapshot) == approved and current_snapshot() == snapshot, "Runner resources changed or were not approved")
    require(type(runner_group_id) is int and runner_group_id > 0, "Approve an explicit runner group")
    require(registration.is_private_admin(snapshot["repository"]), "Runner enrollment requires the approved private repository")
    name = "llmgw-" + fingerprint({"vmId": snapshot["vmId"], "repo": snapshot["repository"]})[:24]
    require(not registration.find(snapshot["repository"], name), "Runner already registered; inspect it instead of replacing credentials")
    record = {"snapshot": snapshot, "name": name, "runnerGroupId": runner_group_id, "phase": "registering"}
    save(record)
    generated = registration.generate(snapshot["repository"], {"name": name, "runner_group_id": runner_group_id, "work_folder": "_work", "labels": ["self-hosted", "linux", "x64", "llmgw-" + snapshot["environment"], name]})
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
    current = current_snapshot()
    resuming = record.get("phase") == "retiring"
    require(current == snapshot or (resuming and current is None), "Runner VM was replaced or drifted; refuse cleanup")
    runners = registration.find(snapshot["repository"], record["name"])
    require(len(runners) <= 1, "Ambiguous runner cleanup target")
    if runners:
        runner = runners[0]
        require(runner["id"] == record.get("runnerId") and runner.get("busy") is False, "Runner is busy or was replaced; do not delete")
        registration.remove(snapshot["repository"], runner["id"])
    current = current_snapshot()
    require(current == snapshot or (resuming and current is None), "Runner changed before VM cleanup")
    save({**record, "phase": "retiring"})
    delete_machine(snapshot["machineId"], snapshot["vmId"])
    saved = {**record, "phase": "retired"}
    save(saved)
    return saved