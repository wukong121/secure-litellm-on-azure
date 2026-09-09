"""One-time administrator installation of scoped passwordless workflow identities."""

import argparse
import json
from pathlib import Path
import re
import subprocess
from uuid import UUID

from scripts.customer_migration import ROOT, fingerprint, private_write, require, validate_config
from scripts.migration_deploy import AzureCommands, group_id
from scripts.installation_readiness import installation_readiness

IDENTITIES = {
    "deploy": "AZURE_CLIENT_ID", "runtime": "AZURE_RUNTIME_CLIENT_ID", "database": "AZURE_DATABASE_CLIENT_ID",
    "entra-bootstrap": "AZURE_ENTRA_CLIENT_ID", "entra-access": "AZURE_ENTRA_ACCESS_CLIENT_ID",
    "audit-governance": "AZURE_AUDIT_GOVERNANCE_CLIENT_ID", "certificate": "AZURE_CERTIFICATE_CLIENT_ID",
}
READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
AKS_USER = "4abbcc35-e782-43d8-92c5-2d3f1bd2253f"


def install_intent(config, repository):
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "Use an explicit private GitHub repository")
    suffix = fingerprint({"repository": repository, "group": group_id(config), "environment": config["environment"]})[:12]
    trust = {"issuer": "https://token.actions.githubusercontent.com", "subject": f"repo:{repository}:environment:{config['environment']}", "audiences": ["api://AzureADTokenExchange"]}
    identities = {role: {"name": f"id-llmgw-{role}-{suffix}", "variable": variable, "trust": trust} for role, variable in IDENTITIES.items()}
    return {"repository": repository, "environment": config["environment"], "scope": group_id(config), "location": config["location"], "identities": identities,
            "defaultRoles": {"deploy": [CONTRIBUTOR], "runtime": [READER, AKS_USER], "database": [READER], "entra-bootstrap": [READER], "entra-access": [READER], "audit-governance": [READER, AKS_USER], "certificate": [READER]},
            "requiresAdditionalApproval": ["Scoped RBAC assignment administration", "Entra Graph application permissions", "Database Entra administrator assignment", "Kubernetes fixed-resource roles", "Private runner registration", "DNS and API certificate Vault scope"]}


def github(command, payload=None):
    result = subprocess.run(["gh", *command], input=json.dumps(payload) if payload is not None else None, text=True, capture_output=True, check=False, timeout=120)
    require(result.returncode == 0, "GitHub installation operation failed; check administrator authorization")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def installation(config, repository, operation, directory, approved, azure=None, gh=github):
    require(operation in {"plan", "execute"}, "Invalid installation operation")
    azure = azure or AzureCommands(config, directory)
    account = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(account == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Administrator Azure login has a different scope")
    repo = gh(["api", "repos/" + repository])
    require(repo.get("private") is True and repo.get("permissions", {}).get("admin") is True, "Installation requires administrator access to a private repository")
    branch = gh(["api", f"repos/{repository}/branches/{repo['default_branch']}"])
    require(branch.get("protected") is True, "Protect the default branch before installing production workflow identities")
    environments = gh(["api", f"repos/{repository}/environments?per_page=100"])
    require(environments.get("total_count", 0) <= 100, "Environment enumeration exceeds installation bound")
    matches = [item for item in environments.get("environments", []) if item.get("name") == config["environment"]]
    require(len(matches) <= 1, "Ambiguous existing GitHub environment")
    existing_environment = matches[0] if matches else None
    if existing_environment:
        require(existing_environment.get("deployment_branch_policy") == {"protected_branches": True, "custom_branch_policies": False}, "Existing environment branch policy differs; review it separately without overwriting approvals")
    prior_variables = {}
    if existing_environment:
        listed = gh(["api", f"repos/{repository}/environments/{config['environment']}/variables?per_page=100"])
        require(listed.get("total_count", 0) <= 100, "Existing environment variables exceed installation bound")
        prior_variables = {item["name"]: item["value"] for item in listed.get("variables", []) if item["name"] in set(IDENTITIES.values()) | {"AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"}}
    intent = install_intent(config, repository)
    resources = azure.scoped(["identity", "list", "--query", "[].{id:id,name:name,clientId:clientId,principalId:principalId,resourceGroup:resourceGroup}"])
    found = {}
    for role, definition in intent["identities"].items():
        matches = [resource for resource in resources if resource["name"] == definition["name"] and resource["resourceGroup"].lower() == config["target"]["resourceGroup"].lower()]
        require(len(matches) <= 1, "Ambiguous workflow identity")
        if matches:
            federation = azure.scoped(["identity", "federated-credential", "list", "--resource-group", config["target"]["resourceGroup"], "--identity-name", definition["name"]])
            require(len(federation) <= 1 and all({key: item[key] for key in definition["trust"]} == definition["trust"] for item in federation), "Existing workflow trust differs; no automatic trust expansion")
        found[role] = matches[0] if matches else None
    for name, expected in (("AZURE_TENANT_ID", config["azure"]["tenantId"]), ("AZURE_SUBSCRIPTION_ID", config["azure"]["subscriptionId"])):
        require(name not in prior_variables or prior_variables[name] == expected, "Existing GitHub Azure scope differs; do not overwrite")
    for role, definition in intent["identities"].items():
        name = definition["variable"]
        require(name not in prior_variables or (found[role] and prior_variables[name] == found[role]["clientId"]), "Existing GitHub workflow identity differs; explicit migration is required")
    plan = {"intent": intent, "configSha256": fingerprint(config), "before": found, "defaultBranch": repo["default_branch"], "environment": existing_environment, "variables": prior_variables}
    summary = {"planSha256": fingerprint(plan), "applied": False, "readyForDeployment": False}
    private_write(directory / "installation-plan.json", json.dumps(plan, indent=2))
    private_write(directory / "installation-summary.json", json.dumps(summary, indent=2))
    if operation == "plan":
        print(json.dumps(summary))
        return summary
    require(approved == summary["planSha256"], "Installation plan changed or was not approved")
    azure.scoped(["group", "create", "--name", config["target"]["resourceGroup"], "--location", config["location"]])
    variables = {"AZURE_TENANT_ID": config["azure"]["tenantId"], "AZURE_SUBSCRIPTION_ID": config["azure"]["subscriptionId"]}
    client_ids, principal_ids = set(), set()
    for role, definition in intent["identities"].items():
        identity = found[role] or azure.scoped(["identity", "create", "--name", definition["name"], "--resource-group", config["target"]["resourceGroup"], "--location", config["location"]])
        client_id, principal_id = UUID(identity["clientId"]), UUID(identity["principalId"])
        require(client_id.int and principal_id.int and client_id not in client_ids and principal_id not in principal_ids, "Workflow identities must remain distinct")
        client_ids.add(client_id)
        principal_ids.add(principal_id)
        federation = azure.scoped(["identity", "federated-credential", "list", "--resource-group", config["target"]["resourceGroup"], "--identity-name", definition["name"]])
        require(len(federation) <= 1 and all({key: item[key] for key in definition["trust"]} == definition["trust"] for item in federation), "Workflow trust changed before installation")
        if not federation:
            azure.scoped(["identity", "federated-credential", "create", "--name", "github-environment", "--identity-name", definition["name"], "--resource-group", config["target"]["resourceGroup"], "--issuer", definition["trust"]["issuer"], "--subject", definition["trust"]["subject"], "--audiences", "api://AzureADTokenExchange"])
        for role_id in intent["defaultRoles"][role]:
            azure.scoped(["role", "assignment", "create", "--assignee-object-id", identity["principalId"], "--assignee-principal-type", "ServicePrincipal", "--role", role_id, "--scope", group_id(config)])
        variables[definition["variable"]] = identity["clientId"]
    environment = config["environment"]
    if existing_environment is None:
        gh(["api", "--method", "PUT", f"repos/{repository}/environments/{environment}", "--input", "-"], {"deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False}})
    for name, value in variables.items():
        gh(["variable", "set", name, "--repo", repository, "--env", environment, "--body", value])
    observed = {name: {"passed": True, "source": source, "scope": scope} for name, source, scope in (
        ("private_repository", "GitHub repository private/admin check", repository),
        ("protected_default_branch", "GitHub protected branch flag", repo["default_branch"]),
    )}
    summary.update(applied=True, workflowClientIds=variables, pendingAuthorizations=intent["requiresAdditionalApproval"], readiness=installation_readiness(observed))
    private_write(directory / "installation-summary.json", json.dumps(summary, indent=2))
    print(json.dumps(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--operation", choices=("plan", "execute"), default="plan")
    parser.add_argument("--approved-plan", type=Path)
    args = parser.parse_args()
    source = json.loads(args.config.read_text())
    config = validate_config(source, source["environment"])
    approved = fingerprint(json.loads(args.approved_plan.read_text())) if args.approved_plan else ""
    directory = ROOT / "temp/workflow-installation"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    installation(config, args.repository, args.operation, directory, approved)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit("Workflow installation failed. Replan partial results; no passwords were created and deployment is not certified ready.") from None