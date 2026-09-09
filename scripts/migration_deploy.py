"""Explicit, plan-bound ARM deployments. Never backs up data, cuts DNS, or certifies acceptance."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from scripts.customer_migration import (
    COMPONENTS, ROOT, MigrationError, active_stages, approval_policy, configured, fingerprint, prepare,
    private_write, require, stage_fingerprint, validate_config, validate_evidence,
)


def deployment_name(config, stage, component):
    return f"llmgw-{config['environment']}-s{stage}-{component}"


def group_id(config, legacy=False):
    return f"/subscriptions/{config['azure']['subscriptionId']}/resourceGroups/{config['legacy' if legacy else 'target']['resourceGroup']}"


def assert_change_scope(config, component, changes):
    allowed_types = {"Create", "Modify", "NoChange", "Ignore"}
    allowed_group = group_id(config, component in {"monitoring", "legacy-logging"}).lower()
    external_accounts = {
        connection["accountResourceId"].rstrip("/").lower()
        for connection in config["parameters"].get("platform", {}).get("azureOpenAIConnections", [])
    }
    for change in changes:
        require(change.get("changeType") in allowed_types, "Delete, Unsupported or unknown changes require separate remediation; deployment blocked")
        if change["changeType"] in {"NoChange", "Ignore"}:
            continue
        resource = change.get("resourceId", "").lower()
        local = resource.startswith(allowed_group + "/") or (component == "bootstrap" and resource == allowed_group)
        external_role = component == "platform" and any(
            resource.startswith(account + "/providers/microsoft.authorization/roleassignments/")
            for account in external_accounts
        )
        require(local or external_role, "Plan attempts to modify a resource outside the approved component scope")
        if component in {"monitoring", "legacy-logging"}:
            kinds = ("microsoft.operationalinsights/workspaces", "microsoft.resources/deployments") if component == "legacy-logging" else (
                "microsoft.insights/actiongroups", "microsoft.insights/scheduledqueryrules",
                "microsoft.insights/activitylogalerts", "microsoft.resources/deployments",
            )
            require(any(resource.startswith(allowed_group + "/providers/" + kind + "/") for kind in kinds), "Monitoring deployment cannot replace legacy application or data resources")


class AzureCommands:
    def __init__(self, config, directory):
        self.config = config
        self.directory = directory
        self.counter = 0

    def run(self, arguments):
        self.counter += 1
        result = subprocess.run(["az", *arguments, "--only-show-errors", "--output", "json"], capture_output=True, text=True, check=False)
        private_write(self.directory / f"command-{self.counter}.stdout.json", result.stdout)
        private_write(self.directory / f"command-{self.counter}.stderr.txt", result.stderr)
        if result.returncode:
            known = ("ResourceGroupNotFound", "ResourceNotFound", "AuthorizationFailed", "LinkedAuthorizationFailed", "RequestDisallowedByPolicy", "InvalidTemplateDeployment", "InvalidTemplate", "MissingSubscriptionRegistration")
            codes = [code for code in known if code in result.stderr or code in result.stdout]
            raise MigrationError("Azure command failed: " + (", ".join(codes) or "see private runner diagnostics"))
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise MigrationError("Azure returned invalid JSON; see private runner diagnostics") from None

    def scoped(self, arguments):
        return self.run([*arguments, "--subscription", self.config["azure"]["subscriptionId"]])


def resolve_origin(config, component, azure):
    resolved = copy.deepcopy(config)
    if component == "audit":
        audit = resolved["parameters"]["audit"]
        roles = ("writer", "reader", "retention")
        if "recoveryPrincipalId" in audit:
            roles += ("recovery",)
        if any(audit.get(role + "PrincipalId") == "auto" or not configured(audit.get(role + "PrincipalId", "")) for role in roles):
            output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 8, "audit-foundation"), "--query", "{state:properties.provisioningState,foundation:properties.outputs.auditFoundation.value}"])
            require(output.get("state") == "Succeeded", "Deploy audit-foundation before resolving audit identities")
            foundation = output["foundation"]
            require(all(audit[key] == foundation[key] for key in ("cmkVaultName", "cmkKeyName")), "Audit foundation Vault/key differs from configured names")
            for role in roles:
                key = role + "PrincipalId"
                if audit.get(key) == "auto" or not configured(audit.get(key, "")):
                    require(isinstance(foundation.get(role), dict), "Redeploy audit-foundation to initialize the requested identity")
                    audit[key] = foundation[role]["principalId"]
        require(len({audit[role + "PrincipalId"].lower() for role in roles}) == len(roles), "Audit roles must be distinct service principals")
    elif component == "edge":
        origin = resolved["parameters"]["edge"]["privateOrigin"]
        if not configured(origin["privateLinkServiceId"]) or origin["privateLinkServiceId"] == "auto":
            output = azure.scoped([
                "deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"],
                "--name", deployment_name(config, 9, "origin"),
                "--query", "{state:properties.provisioningState,origin:properties.outputs.privateOrigin.value}",
            ])
            require(output.get("state") == "Succeeded", "Deploy the origin component before resolving edge")
            origin.update(output["origin"])
        require(origin["privateLinkServiceId"].lower().startswith(group_id(config).lower() + "/providers/microsoft.network/privatelinkservices/"), "PLS must belong to the approved target resource group")
    elif component == "origin":
        cluster_name = config["parameters"]["platform"]["stage4Aks"]["name"]
        node_group = azure.scoped(["aks", "show", "--resource-group", config["target"]["resourceGroup"], "--name", cluster_name, "--query", "nodeResourceGroup"])
        load_balancer = resolved["parameters"]["origin"]["apiLoadBalancer"]
        managed_address = None
        if "privateIngress" in config:
            output = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 4, "private-ingress"), "--query", "{state:properties.provisioningState,ingress:properties.outputs.privateIngress.value}"])
            require(output.get("state") == "Succeeded", "Deploy and verify private-ingress before creating the origin")
            ingress = output["ingress"]
            require(ingress.get("configSha256") == stage_fingerprint(config, 4), "Private ingress outputs differ from the approved Stage 4 configuration")
            managed = ingress["api"]
            managed_address = managed["privateIpAddress"]
            require(managed_address != ingress["admin"]["privateIpAddress"], "API/admin private frontends must be separate")
            for key in ("resourceGroupName", "name", "frontendName"):
                require(load_balancer[key] == "auto" or not configured(load_balancer[key]) or load_balancer[key] == managed[key], "Configured origin differs from the verified API ingress")
                load_balancer[key] = managed[key]
        supplied_group = load_balancer["resourceGroupName"]
        require(supplied_group == "auto" or not configured(supplied_group) or supplied_group.lower() == node_group.lower(), "Configured load balancer group differs from actual AKS node resource group")
        candidates = azure.scoped(["network", "lb", "list", "--resource-group", node_group])
        matches = []
        for candidate in candidates:
            if candidate.get("sku", {}).get("name") != "Standard":
                continue
            if configured(load_balancer["name"]) and load_balancer["name"] != "auto" and candidate["name"] != load_balancer["name"]:
                continue
            for frontend in candidate.get("frontendIPConfigurations", []):
                if frontend.get("publicIPAddress") or not frontend.get("privateIPAddress"):
                    continue
                if managed_address and frontend["privateIPAddress"] != managed_address:
                    continue
                if configured(load_balancer["frontendName"]) and load_balancer["frontendName"] != "auto" and frontend["name"] != load_balancer["frontendName"]:
                    continue
                subnet = frontend.get("subnet", {}).get("id", "")
                expected = group_id(config) + "/providers/Microsoft.Network/virtualNetworks/" + resolved["parameters"]["origin"]["virtualNetworkName"] + "/subnets/" + resolved["parameters"]["origin"]["ingressSubnetName"]
                if subnet.lower() == expected.lower():
                    matches.append({"resourceGroupName": node_group, "name": candidate["name"], "frontendName": frontend["name"]})
        require(len(matches) == 1, "Expected exactly one reviewed private API frontend; specify actual LB/frontend names to resolve ambiguity")
        resolved["parameters"]["origin"]["apiLoadBalancer"] = matches[0]
    return resolved


def build_plan(config, stage, component, revision, template_hash, parameters, changes):
    assert_change_scope(config, component, changes)
    relevant = sorted((change for change in changes if change["changeType"] != "Ignore"), key=lambda change: change.get("resourceId", ""))
    contract = {
        "stage": stage, "component": component, "environment": config["environment"],
        "revision": revision, "configSha256": stage_fingerprint(config, stage),
        "templateSha256": template_hash, "parametersSha256": fingerprint(parameters),
        "changes": relevant,
    }
    return {
        "planSha256": fingerprint(contract), "stage": stage, "component": component,
        "environment": config["environment"], "revision": revision,
        "configSha256": contract["configSha256"], "templateSha256": template_hash,
        "parametersSha256": contract["parametersSha256"],
        "changes": [{"resourceId": change.get("resourceId"), "changeType": change["changeType"],
                     "changedProperties": [delta.get("path") for delta in change.get("delta", [])]}
                    for change in relevant],
    }


def deploy_component(config, stage, component, revision, operation, previous, directory, approved_plan="", azure=None, release=None):
    require(operation in {"plan", "deploy"}, "Invalid deployment operation")
    require(component in COMPONENTS, "Unknown deployment component")
    require(re.fullmatch(r"[0-9a-f]{40}", revision or "") is not None, "Full reviewed Git revision required")
    validate_config(config, config["environment"])
    require(stage in active_stages(config), "Stage does not apply to the selected deployment mode")
    completed = set()
    if operation == "deploy":
        completed = validate_evidence(previous, stage, config, revision)
        require(re.fullmatch(r"[0-9a-f]{64}", approved_plan or "") is not None, "Deploy requires an approved plan SHA256")
        require(not (component in {"backup", "network"} and any(value >= 4 for value in completed)), "Do not reapply the bootstrap VNet after Stage 4")
        require(not (component == "platform" and any(value > stage for value in completed)), "Do not apply an earlier platform stage over a later accepted stage")
    directory = Path(directory).resolve()
    require(directory.is_relative_to((ROOT / "temp").resolve()) and directory != (ROOT / "temp").resolve(), "Deployment output must stay under temp/")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    azure = azure or AzureCommands(config, directory)
    context = azure.run(["account", "show", "--query", "{tenantId:tenantId,id:id}"])
    require(context == {"tenantId": config["azure"]["tenantId"], "id": config["azure"]["subscriptionId"]}, "Azure login scope does not match customer configuration")
    resolved = resolve_origin(config, component, azure)
    template, path = prepare(resolved, stage, component, directory)
    if release is not None:
        from scripts.stage9_release import validate_release
        require(stage == 9 and component == "edge", "Release operation only applies to Stage 9 edge")
        mode, eligible = approval_policy(config)
        validate_release(release, required_approvers=1 if mode == "single-operator" else 2, eligible_approvers=eligible)
        require(len(release["approvedBy"]) == (1 if mode == "single-operator" else 2), "Release approval count differs from configured policy")
        require(release.get("revision") == revision and release.get("configSha256") == stage_fingerprint(config, 9), "Release must bind the reviewed code and stage configuration")
        require(release["environmentName"] == config["environment"] and release["baseDomain"] == config["baseDomain"], "Release environment/domain mismatch")
        require(release["privateOrigin"] == resolved["parameters"]["edge"]["privateOrigin"], "Release PLS differs from deployed origin")
        require(release["logAnalyticsWorkspaceName"] == resolved["parameters"]["edge"]["logAnalyticsWorkspaceName"] and release["rateLimitPerMinute"] == resolved["parameters"]["edge"].get("rateLimitPerMinute", 600), "Release logging/rate configuration mismatch")
        previous_edge = azure.scoped(["deployment", "group", "show", "--resource-group", config["target"]["resourceGroup"], "--name", deployment_name(config, 9, "edge"), "--query", "{state:properties.provisioningState,edge:properties.outputs.edge.value}"])
        require(previous_edge.get("state") == "Succeeded", "Provision the disabled edge before releasing traffic")
        require(release.get("frontDoorId") == previous_edge.get("edge", {}).get("profileId"), "Release Front Door identity differs from the provisioned edge")
        document = json.loads(path.read_text())
        document["parameters"]["enableApiTraffic"] = {"value": release["phase"] != "prepare"}
        document["parameters"]["wafMode"] = {"value": release["wafMode"]}
        private_write(path, json.dumps(document, indent=2) + "\n")
    compiled = directory / "template.json"
    result = subprocess.run(["az", "bicep", "build", "--file", str(template), "--outfile", str(compiled)], capture_output=True, text=True, check=False)
    private_write(directory / "bicep-diagnostics.txt", result.stderr)
    require(result.returncode == 0, "Bicep compilation failed; see private diagnostics")
    template_hash = hashlib.sha256(compiled.read_bytes()).hexdigest()
    scope = "sub" if component == "bootstrap" else "group"
    scope_args = ["--location", config["location"]] if scope == "sub" else ["--resource-group", config["legacy" if component in {"monitoring", "legacy-logging"} else "target"]["resourceGroup"]]
    common = [*scope_args, "--name", deployment_name(config, stage, component), "--template-file", str(compiled), "--parameters", f"@{path}"]
    response = azure.scoped(["deployment", scope, "what-if", *common, "--result-format", "FullResourcePayloads", "--no-pretty-print"])
    require(response.get("status") == "Succeeded" and isinstance(response.get("changes"), list), "What-if did not produce a successful change list")
    plan = build_plan(config, stage, component, revision, template_hash, {"parameters": json.loads(path.read_text()), "release": release}, response["changes"])
    private_write(directory / "reviewed-plan.json", json.dumps({"planSha256": plan["planSha256"], "revision": revision, "changes": [change for change in response["changes"] if change["changeType"] != "Ignore"]}, indent=2) + "\n")
    private_write(directory / "plan-summary.json", json.dumps(plan, indent=2) + "\n")
    print(json.dumps({"planSha256": plan["planSha256"], "stage": stage, "component": component, "deploymentPerformed": False}))
    if operation == "plan":
        return plan
    require(plan["planSha256"] == approved_plan, "Plan changed since approval; review the new plan before deploying")
    create_args = ["deployment", scope, "create", *common]
    if scope == "group":
        create_args.extend(["--mode", "Incremental"])
    deployed = azure.scoped(create_args)
    require(deployed.get("properties", {}).get("provisioningState") == "Succeeded", "Deployment did not succeed; no acceptance evidence created")
    receipt = {key: plan[key] for key in ("stage", "component", "environment", "revision", "configSha256", "planSha256")}
    receipt.update(deploymentId=deployed.get("id"), provisioningState="Succeeded", stageAccepted=False)
    private_write(directory / "deployment-outputs.json", json.dumps(deployed.get("properties", {}).get("outputs", {}), indent=2) + "\n")
    private_write(directory / "deployment-receipt.json", json.dumps(receipt, indent=2) + "\n")
    print("ARM deployment succeeded. Complete runtime checks and record acceptance separately.")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("plan", "deploy"), required=True)
    parser.add_argument("--stage", type=int, choices=range(10), required=True)
    parser.add_argument("--component", choices=tuple(COMPONENTS), required=True)
    parser.add_argument("--environment", choices=("dev", "test", "prod"), required=True)
    parser.add_argument("--release", action="store_true", help="Use separately approved Stage 9 release attestations")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "temp/migration-deploy")
    args = parser.parse_args()
    config = validate_config(json.loads(os.environ["CUSTOMER_CONFIG_JSON"]), args.environment)
    require(os.environ.get("AZURE_TENANT_ID") == config["azure"]["tenantId"] and os.environ.get("AZURE_SUBSCRIPTION_ID") == config["azure"]["subscriptionId"], "Configured Azure scope mismatch")
    if args.operation == "deploy":
        require(os.environ.get("MIGRATION_CONFIRM_ENVIRONMENT") == args.environment, "Explicit environment confirmation required")
    from scripts.workflow_artifacts import approved_operation, load_evidence, write_operation_receipt
    revision = os.environ.get("GITHUB_SHA", os.environ.get("MIGRATION_REVISION", ""))
    approved = os.environ.get("MIGRATION_APPROVED_PLAN_SHA256", "")
    previous = json.loads(os.environ.get("MIGRATION_EVIDENCE_JSON") or "[]")
    if args.operation == "deploy":
        if os.environ.get("MIGRATION_APPROVED_RUN_ID"):
            approved = approved_operation(config, revision, args.stage, args.component, os.environ["MIGRATION_APPROVED_RUN_ID"], "infrastructure")
        if os.environ.get("MIGRATION_AUTO_EVIDENCE") == "true":
            previous = load_evidence(config, args.stage, revision)
    deploy_component(config, args.stage, args.component,
                     revision, args.operation, previous, args.output_dir, approved,
                     release=json.loads(os.environ["MIGRATION_RELEASE_JSON"]) if args.release else None)
    write_operation_receipt(args.output_dir, config, revision, args.stage, args.component, args.operation, "infrastructure")


if __name__ == "__main__":
    try:
        main()
    except MigrationError as error:
        raise SystemExit(str(error)) from None
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        raise SystemExit("Deployment controller failed; inspect private runner diagnostics. Input values are not logged.") from None