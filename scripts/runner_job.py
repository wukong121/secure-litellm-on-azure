"""Approved one-job runner plans and orchestration, independent of the private runner."""

import hashlib
import re
import time

from scripts.customer_migration import ROOT, fingerprint, require
from scripts.migration_deploy import group_id
from scripts.runner_lifecycle import enroll_runner, retire_runner


def runner_intent(config, settings, repository, run_id, attempt):
    require(isinstance(settings, dict) and set(settings) == {"imageVersionResourceId", "subnetResourceId", "virtualMachineSize", "sshPublicKey", "runnerGroupId"}, "Runner configuration requires approved image, subnet, size, SSH public key and group")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "Invalid runner repository")
    require(re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)) and re.fullmatch(r"[1-9][0-9]{0,4}", str(attempt)), "Runner lease requires an actual workflow run and attempt")
    base = r"/subscriptions/[a-fA-F0-9-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/providers/"
    require(re.fullmatch(base + r"Microsoft.Compute/galleries/[A-Za-z0-9_.-]+/images/[A-Za-z0-9_.-]+/versions/[0-9]+\.[0-9]+\.[0-9]+", settings["imageVersionResourceId"]), "Runner image must pin an approved gallery version")
    require(re.fullmatch(base + r"Microsoft.Network/virtualNetworks/[A-Za-z0-9_.-]+/subnets/[A-Za-z0-9_.-]+", settings["subnetResourceId"]), "Runner subnet must be an explicit resource ID")
    require(settings["subnetResourceId"].lower().startswith("/subscriptions/" + config["azure"]["subscriptionId"].lower() + "/"), "Runner subnet must belong to the approved subscription")
    require(re.fullmatch(r"Standard_[A-Za-z0-9_]+", settings["virtualMachineSize"]), "Invalid approved VM size")
    require(type(settings["runnerGroupId"]) is int and settings["runnerGroupId"] > 0, "An explicit approved runner group is required")
    from cryptography.hazmat.primitives.serialization import load_ssh_public_key
    from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
    try:
        key = load_ssh_public_key(settings["sshPublicKey"].encode())
        require(isinstance(key, ed25519.Ed25519PublicKey) or (isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048), "Use an approved SSH public key algorithm")
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Runner requires a valid SSH public key, never a private key") from None
    lease = {"repository": repository, "environment": config["environment"], "runId": str(run_id), "attempt": str(attempt), "group": group_id(config)}
    name = "llmgw-runner-" + fingerprint(lease)[:20]
    parameters = {"location": config["location"], "runnerName": name, **{key: value for key, value in settings.items() if key != "runnerGroupId"}, "repository": repository, "environmentName": config["environment"], "tags": {"runId": str(run_id), "runAttempt": str(attempt)}}
    contract = {"lease": lease, "parameters": parameters, "runnerGroupId": settings["runnerGroupId"], "templateSha256": hashlib.sha256((ROOT / "infra/private-runner/main.bicep").read_bytes()).hexdigest()}
    return {**contract, "planSha256": fingerprint(contract), "machineId": group_id(config) + "/providers/Microsoft.Compute/virtualMachines/" + name}


def wait_runner_online(registration, record, deadline_seconds=300, clock=time.monotonic, wait=time.sleep):
    deadline = clock() + deadline_seconds
    while True:
        runners = registration.find(record["snapshot"]["repository"], record["name"])
        require(len(runners) <= 1, "Ambiguous online runner")
        if runners:
            runner = runners[0]
            require(runner.get("id") == record["runnerId"] and runner.get("ephemeral") is True, "Runner was replaced or is not ephemeral")
            require(runner.get("busy") is False, "Runner accepted an unexpected job before handoff")
            if runner.get("status") == "online":
                return ["self-hosted", "linux", "x64", record["name"]]
        require(clock() < deadline, "Runner did not become online; preserve checkpoint and clean up the unused lease")
        wait(2)


def start_runner(intent, approved, provision, arm, registration, save, previous=None, ready=wait_runner_online):
    require(intent["planSha256"] == approved, "Runner creation plan changed or was not approved")
    contract = {key: value for key, value in intent.items() if key not in {"planSha256", "machineId"}}
    require(fingerprint(contract) == approved, "Runner plan content changed after approval")
    require(intent["templateSha256"] == hashlib.sha256((ROOT / "infra/private-runner/main.bicep").read_bytes()).hexdigest(), "Runner template changed after approval")
    expected_machine = intent["lease"]["group"] + "/providers/Microsoft.Compute/virtualMachines/" + intent["parameters"]["runnerName"]
    require(intent["machineId"] == expected_machine, "Runner machine differs from its creation parameters")
    require(previous is None, "Runner start cannot replay a prior lease; use its checkpoint for cleanup")
    require(arm.machine_id == intent["machineId"] and arm.repository == intent["lease"]["repository"], "Runner adapters have a different scope")
    require(arm.snapshot(optional=True) is None, "Runner VM already exists; inspect prior lease before creating or enrolling")
    require(registration.is_private_admin(intent["lease"]["repository"]), "Runner registration authority is missing")
    state = {"intent": intent, "phase": "creating", "runner": None, "ready": False, "stageAccepted": False}
    save(state.copy())
    provision(intent)
    snapshot = arm.snapshot()
    state.update(phase="created", snapshot=snapshot)
    save(state.copy())

    def checkpoint(record):
        state.update(phase=record["phase"], runner=record)
        save(state.copy())

    record = enroll_runner(snapshot, fingerprint(snapshot), arm.snapshot, registration, arm.managed_command, checkpoint, intent["runnerGroupId"])
    labels = ready(registration, record)
    state.update(phase="online", ready=True, labels=labels)
    save(state.copy())
    return state


def cleanup_runner(state, arm, registration, save):
    require(state["intent"]["machineId"] == arm.machine_id, "Runner cleanup checkpoint targets another VM")
    require(state.get("runner") is not None, "Pre-enrollment failure requires resource reconciliation, not guessed cleanup")
    record = state["runner"]
    require(record.get("phase") in {"registered", "started", "retiring", "retired"}, "Uncertain registration needs explicit reconciliation before cleanup")
    if record["phase"] == "retired":
        require(arm.snapshot(optional=True) is None, "Retired runner VM unexpectedly exists")
        return state

    def checkpoint(value):
        state.update(phase=value["phase"], runner=value, ready=False)
        save(state.copy())

    retire_runner(record, lambda: arm.snapshot(optional=True), registration, arm.delete_machine, checkpoint)
    return state