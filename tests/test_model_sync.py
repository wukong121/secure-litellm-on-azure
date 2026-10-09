import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from local_execution import model_sync
from scripts.backend_manifest import render_backend_manifest
from scripts.customer_migration import MigrationError, ROOT, fingerprint, stage_fingerprint
from scripts.model_sync_catalog import account_id, discover, model_list, parse_catalog, reconcile, ROLE
from scripts.model_sync_infra import assert_what_if, endpoint_state, role_state, infrastructure_plan, execute_infrastructure
from scripts import model_sync_runtime as runtime
from scripts.runtime_secrets import backend_secrets
from tests.test_backend_manifest import backend_customer


SUB = "22222222-2222-4222-8222-222222222222"
OTHER_SUB = "66666666-6666-4666-8666-666666666666"
TENANT = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "44444444-4444-4444-8444-444444444444"
CLIENT = "33333333-3333-4333-8333-333333333333"
APPROVERS = [TENANT, SUB]


def customer():
    config = backend_customer()
    config["parameters"]["platform"]["azureOpenAIConnections"][0].update(
        subscriptionId=SUB, resourceGroupName="rg-model",
        accountResourceId=account_id(SUB, "rg-model", "synthetic-model"))
    config["parameters"]["platform"]["stage4Network"]["privateEndpointSubnetName"] = "snet-pe"
    return config


def catalog(names=("synthetic-chat",), subscriptions=None):
    entries = {}
    for i, name in enumerate(names):
        subscription = (subscriptions or [SUB] * len(names))[i]
        entries.setdefault(subscription, []).append({"name": name, "resource_group": "rg-model",
                                                     "models": [{"model_name": "chat", "deployment_name": "existing-chat"}]})
    return {"schema_version": 1, "subscriptions": [{"subscription_id": key, "resources": resources}
                                                  for key, resources in entries.items()]}


def live_deployment(account, name="existing-chat", model="gpt-4o"):
    return {"id": account["accountResourceId"] + "/deployments/" + name, "name": name,
            "properties": {"provisioningState": "Succeeded", "model": {"format": "OpenAI", "name": model, "version": "2024-08-06"},
                           "versionUpgradeOption": "NoAutoUpgrade"}, "sku": {"name": "GlobalStandard", "capacity": 10}}


class CatalogAzure:
    def __init__(self, accounts):
        self.accounts = accounts
        self.tenant = TENANT
        self.missing = set()
        self.poison = False
        self.state = "Succeeded"
        self.version = "2024-08-06"
        self.hostname = None
        self.hostnames = {}
        self.kind = "OpenAI"
        self.missing_deployments = set()

    def run(self, arguments):
        if arguments[:2] == ["account", "show"]:
            return {"id": arguments[arguments.index("--subscription") + 1], "tenantId": self.tenant}
        if arguments[:2] == ["resource", "show"]:
            account = next(a for a in self.accounts if a["accountResourceId"] == arguments[arguments.index("--ids") + 1])
            hostname = self.hostnames.get(account["accountName"], self.hostname or account["accountName"])
            endpoint = "https://poison.invalid" if self.poison else "https://" + hostname + ".openai.azure.com/"
            return {"id": account["accountResourceId"], "name": account["accountName"], "kind": self.kind,
                    "properties": {"provisioningState": "Succeeded", "customSubDomainName": hostname,
                                   "endpoint": endpoint if self.kind == "OpenAI" else "https://" + hostname + ".services.ai.azure.com/",
                                   "endpoints": {"OpenAI": endpoint}}}
        account = next(a for a in self.accounts if a["accountResourceId"] in arguments[-1])
        deployments = []
        for model in account["models"]:
            if (account["accountName"], model["deploymentName"]) in self.missing_deployments:
                continue
            family = "text-embedding-3-large" if "embed" in model["deploymentName"] else (
                "gpt-4o-mini" if "mini" in model["deploymentName"] else "gpt-4o")
            deployment = live_deployment(account, model["deploymentName"], family)
            deployment["properties"]["provisioningState"] = self.state
            deployment["properties"]["model"]["version"] = self.version
            if deployment["name"] not in {item["name"] for item in deployments}:
                deployments.append(deployment)
        return {"value": [] if account["accountName"] in self.missing else deployments}


def runtime_documents(config):
    source = yaml.safe_load((ROOT / "deploy/components/stage6-ha/config-patch.yaml").read_text())
    runtime_yaml = yaml.safe_load(source["data"]["config.yaml"])
    runtime_yaml["model_list"] = model_list(config)
    runtime_yaml["general_settings"].update(store_model_in_db=False, master_key="os.environ/LITELLM_MASTER_KEY")
    runtime_yaml["router_settings"]["model_group_affinity_config"] = {"coding": ["region"]}
    runtime_yaml["non_model"] = {"audit": True}
    deployment = {"metadata": {"uid": "deployment-uid", "resourceVersion": "42"},
                  "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "litellm"}},
                           "template": {"metadata": {"labels": {"azure.workload.identity/use": "true"}},
                                        "spec": {"serviceAccountName": "litellm",
                                                 "containers": [{"name": "litellm", "image": config["application"]["backendImage"],
                                                                 "args": ["--config", "/app/config/config.yaml"],
                                                                 "env": [{"name": "STORE_MODEL_IN_DB", "value": "false"}],
                                                                 "volumeMounts": [{"name": "config", "mountPath": "/app/config/config.yaml",
                                                                                  "subPath": "config.yaml", "readOnly": True}]}],
                                                 "volumes": [{"name": "config", "configMap": {"name": "old-config"}},
                                                             {"name": "csi", "csi": {"driver": "secrets-store.csi.k8s.io"}}]}}}}
    service = {"metadata": {"uid": "sa-uid", "annotations": {"azure.workload.identity/client-id": CLIENT}}}
    cm = {"metadata": {"uid": "cm-uid", "name": "old-config"}, "data": {"config.yaml": yaml.safe_dump(runtime_yaml)}}
    return deployment, service, cm


def autoscaler_document():
    return {"metadata": {"name": "litellm", "namespace": "litellm", "uid": "hpa-uid"},
            "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "litellm"},
                     "minReplicas": 2, "maxReplicas": 6,
                     "metrics": [{"type": "Resource", "resource": {"name": "cpu",
                                 "target": {"type": "Utilization", "averageUtilization": 65}}}]}}


def current_baseline(config):
    deployment, service, cm = runtime_documents(config)
    with patch.object(runtime, "command", side_effect=[
            json.dumps(deployment), json.dumps(service), json.dumps(cm),
            json.dumps({"items": [autoscaler_document()]})]):
        return runtime.baseline(config, {"properties": {"clientId": CLIENT}}, ["kubectl"])


class ModelSyncCatalogTests(unittest.TestCase):
    def test_replace_uses_only_catalog_mappings_across_the_entire_gateway(self):
        config = customer()
        original = copy.deepcopy(config)
        document = catalog(("synthetic-astra", "synthetic-terra", "synthetic-luna"))
        for index, resource in enumerate(document["subscriptions"][0]["resources"]):
            resource["models"][0]["model_name"] = "model-" + str(index)
        accounts = parse_catalog(document, "v1")
        _, matches = discover(config, accounts, CatalogAzure(accounts))
        desired = reconcile(config, matches)
        self.assertEqual(desired, reconcile(config, matches, "replace"))
        self.assertEqual(len(desired["application"]["models"]), 3)
        self.assertEqual({m["modelGroup"] for m in desired["application"]["models"]}, {"model-0", "model-1", "model-2"})
        self.assertNotIn("coding", {m["modelGroup"] for m in desired["application"]["models"]})
        self.assertIn(config["parameters"]["platform"]["azureOpenAIConnections"][0],
                      desired["parameters"]["platform"]["azureOpenAIConnections"])
        self.assertEqual(config, original)
        self.assertEqual(reconcile(desired, matches, "replace"), desired)
        current = current_baseline(config)
        rendered = yaml.safe_load(runtime.render(desired, current)["configMap"]["data"]["config.yaml"])
        self.assertEqual(list(rendered["router_settings"]["model_group_affinity_config"]),
                         ["model-0", "model-1", "model-2"])

    def test_replace_removes_old_alias_for_the_same_account_and_deployment(self):
        config = customer()
        document = catalog()
        resource = document["subscriptions"][0]["resources"][0]
        resource["name"] = config["parameters"]["platform"]["azureOpenAIConnections"][0]["accountName"]
        resource["models"][0]["deployment_name"] = config["application"]["models"][0]["deploymentName"]
        resource["models"][0]["model_name"] = "new-alias"
        accounts = parse_catalog(document, "v1")
        _, matches = discover(config, accounts, CatalogAzure(accounts))
        merged = reconcile(config, matches, "merge")
        replaced = reconcile(merged, matches, "replace")
        self.assertEqual([m["modelGroup"] for m in merged["application"]["models"]], ["coding", "new-alias"])
        self.assertEqual(replaced["application"]["models"], [merged["application"]["models"][1]])
        self.assertEqual(len(replaced["parameters"]["platform"]["azureOpenAIConnections"]), 1)

    def test_replace_retains_exact_ids_and_omitted_settings_but_removes_unlisted_backends(self):
        accounts = parse_catalog(catalog(), "v1")
        _, matches = discover(customer(), accounts, CatalogAzure(accounts))
        config = reconcile(customer(), matches, "merge")
        retained = config["application"]["models"][1]
        retained["litellmParams"] = {"rpm": 100}
        retained["modelInfo"] = {"input_cost_per_token": 0.000003}
        other = copy.deepcopy(retained)
        other.update(id="other-backend", deploymentName="unlisted")
        config["application"]["models"].append(other)
        desired = reconcile(config, matches, "replace")
        self.assertEqual(desired["application"]["models"], [retained])
        self.assertEqual(desired["application"]["models"][0]["id"], retained["id"])
        self.assertEqual(desired["application"]["models"][0]["litellmParams"], {"rpm": 100})
        self.assertEqual(desired["application"]["models"][0]["modelInfo"], {"input_cost_per_token": 0.000003})

    def test_unknown_model_policy_and_empty_discovery_cannot_delete_models(self):
        for policy in ("replace", "merge"):
            with self.subTest(policy=policy), self.assertRaisesRegex(MigrationError, "at least one"):
                reconcile(customer(), [], policy)
        with self.assertRaisesRegex(MigrationError, "Model policy"):
            reconcile(customer(), [], "unknown")

    def test_multiple_subscriptions_balance_same_group_keep_coding(self):
        config = customer()
        accounts = parse_catalog(catalog(("synthetic-chat", "synthetic-west"), [SUB, OTHER_SUB]), "2024-10-21")
        observations, matches = discover(config, accounts, CatalogAzure(accounts))
        desired = reconcile(config, matches, "merge")
        self.assertEqual(desired["application"]["models"][0], config["application"]["models"][0])
        self.assertEqual([m["modelGroup"] for m in desired["application"]["models"]], ["coding", "chat", "chat"])
        self.assertEqual({m["baseModel"] for m in desired["application"]["models"][1:]}, {"azure/gpt-4o"})
        self.assertEqual(len(observations), 2)
        self.assertEqual(reconcile(desired, matches, "merge"), desired)
        self.assertTrue(all(stage_fingerprint(config, s) != stage_fingerprint(desired, s) for s in range(4, 10)))

    def test_missing_specific_account_fails_even_if_target_exists_elsewhere(self):
        accounts = parse_catalog(catalog(("synthetic-chat", "synthetic-unused")), "v1")
        azure = CatalogAzure(accounts)
        azure.missing = {"synthetic-unused"}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            azure.directory = Path(destination)
            with self.assertRaisesRegex(MigrationError, "explicitly mapped resource"):
                discover(customer(), accounts, azure)
            observations = json.loads((azure.directory / "discovery.json").read_text())
            self.assertEqual(observations[1]["status"], "missing-explicit-deployment")
            self.assertEqual(observations[1]["missingDeployments"], ["existing-chat"])

    def test_missing_target_is_explicit_failure(self):
        accounts = parse_catalog(catalog(), "v1")
        azure = CatalogAzure(accounts)
        azure.missing = {"synthetic-chat"}
        with self.assertRaisesRegex(MigrationError, "explicitly mapped resource"):
            discover(customer(), accounts, azure)

    def test_live_endpoint_tenant_state_metadata_rejected(self):
        accounts = parse_catalog(catalog(), "v1")
        for field, value in (("tenant", OTHER_SUB), ("poison", True), ("state", "Creating"), ("version", "")):
            azure = CatalogAzure(accounts)
            setattr(azure, field, value)
            with self.subTest(field=field), self.assertRaises(MigrationError):
                discover(customer(), accounts, azure)

    def test_poisoned_catalog_and_unknown_shapes_rejected(self):
        for endpoint in ("http://synthetic-chat.openai.azure.com", "https://synthetic-chat.openai.azure.com.evil/",
                         "https://synthetic-chat.openai.azure.com/path", "https://user@synthetic-chat.openai.azure.com/",
                         "https://synthetic-chat.services.ai.azure.com/"):
            doc = catalog()
            doc["subscriptions"][0]["resources"][0]["endpoint"] = endpoint
            with self.subTest(endpoint=endpoint), self.assertRaises(MigrationError):
                parse_catalog(doc, "v1")
        for mutate in (lambda d: d.update(api_key="not-allowed"),
                       lambda d: d["subscriptions"][0]["resources"][0].update(key="not-allowed"),
                       lambda d: d["subscriptions"][0]["resources"][0]["models"][0].update(baseModel="fabricated"),
                       lambda d: d["subscriptions"][0]["resources"][0]["models"].append(d["subscriptions"][0]["resources"][0]["models"][0]),
                       lambda d: d["subscriptions"][0]["resources"].append(d["subscriptions"][0]["resources"][0])):
            doc = catalog()
            mutate(doc)
            with self.assertRaises(MigrationError):
                parse_catalog(doc, "v1")

    def test_exact_mapping_update_keeps_identity_and_unlisted_backends_in_same_group(self):
        accounts = parse_catalog(catalog(), "v1")
        _, matches = discover(customer(), accounts, CatalogAzure(accounts))
        desired = reconcile(customer(), matches, "merge")
        mapping = desired["application"]["models"][1]
        mapping["apiVersion"] = "old"
        mapping["baseModel"] = "azure/old-family"
        identifier = mapping["id"]
        desired["application"]["models"].append({**desired["application"]["models"][0], "modelGroup": "chat", "id": "old-chat"})
        updated = reconcile(desired, matches, "merge")
        self.assertEqual(len(updated["application"]["models"]), 3)
        self.assertEqual(updated["application"]["models"][2]["id"], "old-chat")
        self.assertEqual(updated["application"]["models"][1]["id"], identifier)
        self.assertEqual(updated["application"]["models"][1]["apiVersion"], "v1")

    def test_duplicate_existing_alias_id_identity_rejected(self):
        for duplicate in ("alias", "id", "mapping"):
            config = customer()
            if duplicate == "alias":
                config["parameters"]["platform"]["azureOpenAIConnections"].append(
                    copy.deepcopy(config["parameters"]["platform"]["azureOpenAIConnections"][0]))
            else:
                item = copy.deepcopy(config["application"]["models"][0])
                if duplicate == "mapping":
                    item["id"] = "another-id"
                config["application"]["models"].append(item)
            with self.subTest(duplicate=duplicate), self.assertRaises(MigrationError):
                reconcile(config, [])


class ModelSyncInfrastructureTests(unittest.TestCase):
    def endpoint_fixture(self):
        group = "/subscriptions/" + SUB + "/resourceGroups/rg-secure"
        account = parse_catalog(catalog(), "v1")[0]
        context = {"subnetId": group + "/providers/Microsoft.Network/virtualNetworks/target/subnets/pe",
                   "zoneId": group + "/providers/Microsoft.Network/privateDnsZones/privatelink.openai.azure.com",
                   "prefixes": ["10.30.8.0/24"]}
        pe = {"id": group + "/providers/Microsoft.Network/privateEndpoints/existing-customer-pe",
              "name": "existing-customer-pe", "properties": {"provisioningState": "Succeeded",
                  "subnet": {"id": context["subnetId"]},
                  "privateLinkServiceConnections": [{"properties": {"privateLinkServiceId": account["accountResourceId"],
                      "groupIds": ["account"], "privateLinkServiceConnectionState": {"status": "Approved"}}}],
                  "networkInterfaces": [{"id": group + "/providers/Microsoft.Network/networkInterfaces/existing-nic"}]}}
        context["endpoints"] = [pe]
        group_dns = {"id": pe["id"] + "/privateDnsZoneGroups/default", "properties": {"provisioningState": "Succeeded",
                     "privateDnsZoneConfigs": [{"properties": {"privateDnsZoneId": context["zoneId"]}}]}}
        record = {"name": account["accountName"], "properties": {"aRecords": [{"ipv4Address": "10.30.8.5"}]}}
        nic = {"properties": {"ipConfigurations": [{"properties": {
            "privateIPAddress": "10.30.8.5",
            "privateLinkConnectionProperties": {
                "groupId": "account", "requiredMemberName": "secondary",
                "fqdns": [account["accountName"] + ".openai.azure.com"],
            },
        }}]}}
        def run(args):
            if args[:2] == ["resource", "show"]:
                return nic
            return {"value": [group_dns] if "privateDnsZoneGroups" in args[-1] else [record]}
        azure = Mock(run=Mock(side_effect=run))
        azure.nic, azure.record = nic, record
        return account, context, pe, azure

    def test_approved_existing_customer_pe_reused_exactly(self):
        account, context, _, azure = self.endpoint_fixture()
        state = endpoint_state(account, "new-alias", context, azure)
        self.assertFalse(state["createEndpoint"])
        self.assertFalse(state["createDnsBinding"])
        self.assertEqual(state["endpointName"], "existing-customer-pe")
        self.assertEqual(state["ips"], ["10.30.8.5"])

    def multiservice_endpoint_fixture(self):
        account, context, pe, azure = self.endpoint_fixture()
        for suffix, address, member in (
            ("cognitiveservices.azure.com", "10.30.8.4", "default"),
            ("services.ai.azure.com", "10.30.8.6", "third"),
        ):
            azure.nic["properties"]["ipConfigurations"].append({"properties": {
                "privateIPAddress": address, "privateLinkConnectionProperties": {
                    "groupId": "account", "requiredMemberName": member,
                    "fqdns": [account["accountName"] + "." + suffix],
                },
            }})
        return account, context, pe, azure

    def test_foundry_multiservice_pe_reuses_only_openai_addresses_for_dns_and_probe(self):
        account, context, _, azure = self.multiservice_endpoint_fixture()
        state = endpoint_state(account, "alias", context, azure)
        self.assertFalse(state["createEndpoint"])
        self.assertFalse(state["createDnsBinding"])
        self.assertEqual(state["ips"], ["10.30.8.5"])
        self.assertEqual(state["nicIps"], ["10.30.8.4", "10.30.8.5", "10.30.8.6"])
        with patch.object(runtime, "command", return_value="private-dns-tls-ok\n") as command:
            runtime.network_probe(["kubectl"], account, state["ips"])
        self.assertEqual(json.loads(command.call_args.args[1][-1]), ["10.30.8.5"])

    def test_multiservice_dns_must_match_the_complete_openai_address_set(self):
        for addresses in ([], ["10.30.8.4"], ["10.30.8.4", "10.30.8.5"], ["10.30.8.5", "10.30.8.6"]):
            account, context, _, azure = self.multiservice_endpoint_fixture()
            azure.record["properties"]["aRecords"] = [{"ipv4Address": address} for address in addresses]
            with self.subTest(addresses=addresses), self.assertRaisesRegex(MigrationError, "expected.*10.30.8.5"):
                endpoint_state(account, "alias", context, azure)

    def test_multiple_openai_addresses_are_all_required(self):
        account, context, _, azure = self.multiservice_endpoint_fixture()
        second = copy.deepcopy(azure.nic["properties"]["ipConfigurations"][0])
        second["properties"]["privateIPAddress"] = "10.30.8.7"
        azure.nic["properties"]["ipConfigurations"].append(second)
        with self.assertRaisesRegex(MigrationError, "does not match"):
            endpoint_state(account, "alias", context, azure)
        azure.record["properties"]["aRecords"].append({"ipv4Address": "10.30.8.7"})
        self.assertEqual(endpoint_state(account, "alias", context, azure)["ips"], ["10.30.8.5", "10.30.8.7"])

    def test_multiservice_missing_or_foreign_hostname_metadata_is_not_guessed_from_dns(self):
        for mode in ("missing", "foreign"):
            account, context, _, azure = self.multiservice_endpoint_fixture()
            for item in azure.nic["properties"]["ipConfigurations"]:
                if mode == "missing":
                    item["properties"].pop("privateLinkConnectionProperties")
                else:
                    item["properties"]["privateLinkConnectionProperties"]["fqdns"] = ["foreign.openai.azure.com"]
            with self.subTest(mode=mode), self.assertRaisesRegex(MigrationError, "cannot identify"):
                endpoint_state(account, "alias", context, azure)

    def test_single_address_legacy_metadata_is_unambiguous_but_wrong_hostname_is_rejected(self):
        account, context, _, azure = self.endpoint_fixture()
        properties = azure.nic["properties"]["ipConfigurations"][0]["properties"]
        mapping = properties.pop("privateLinkConnectionProperties")
        self.assertEqual(endpoint_state(account, "alias", context, azure)["ips"], ["10.30.8.5"])
        mapping["fqdns"] = ["foreign.openai.azure.com"]
        properties["privateLinkConnectionProperties"] = mapping
        with self.assertRaisesRegex(MigrationError, "cannot identify"):
            endpoint_state(account, "alias", context, azure)

    def test_custom_openai_hostname_case_and_private_alias_are_supported(self):
        for fqdn in ("CUSTOM-HOST.OPENAI.AZURE.COM.", "custom-host.privatelink.openai.azure.com"):
            account, context, _, azure = self.multiservice_endpoint_fixture()
            account["endpoint"] = "https://custom-host.openai.azure.com"
            azure.record["name"] = "custom-host"
            azure.nic["properties"]["ipConfigurations"][0]["properties"]["privateLinkConnectionProperties"]["fqdns"] = [fqdn]
            with self.subTest(fqdn=fqdn):
                self.assertEqual(endpoint_state(account, "alias", context, azure)["ips"], ["10.30.8.5"])

    def test_unselected_member_subnet_and_selected_group_are_still_checked(self):
        account, context, _, azure = self.multiservice_endpoint_fixture()
        azure.nic["properties"]["ipConfigurations"][-1]["properties"]["privateIPAddress"] = "10.40.8.6"
        with self.assertRaisesRegex(MigrationError, "outside the approved subnet"):
            endpoint_state(account, "alias", context, azure)
        account, context, _, azure = self.multiservice_endpoint_fixture()
        azure.nic["properties"]["ipConfigurations"][0]["properties"]["privateLinkConnectionProperties"]["groupId"] = "foreign"
        with self.assertRaisesRegex(MigrationError, "approved account group"):
            endpoint_state(account, "alias", context, azure)

    def test_invalid_nic_hostname_metadata_is_rejected(self):
        for mapping in (None, [], {"groupId": "account", "fqdns": "synthetic-chat.openai.azure.com"},
                        {"groupId": "account", "fqdns": [None]}, {"groupId": "foreign", "fqdns": []}):
            account, context, _, azure = self.endpoint_fixture()
            azure.nic["properties"]["ipConfigurations"][0]["properties"]["privateLinkConnectionProperties"] = mapping
            with self.subTest(mapping=mapping), self.assertRaises(MigrationError):
                endpoint_state(account, "alias", context, azure)

    def test_pending_foreign_private_endpoint_blocks(self):
        account, context, pe, azure = self.endpoint_fixture()
        pe["properties"]["privateLinkServiceConnections"][0]["properties"]["privateLinkServiceConnectionState"]["status"] = "Pending"
        with self.assertRaisesRegex(MigrationError, "pending requests"):
            endpoint_state(account, "alias", context, azure)
        azure.run.assert_not_called()

    def test_missing_dns_binding_planned_but_conflicting_binding_rejected(self):
        account, context, _, azure = self.endpoint_fixture()
        previous = azure.run.side_effect
        azure.run.side_effect = lambda args: {"value": []} if args[0] == "rest" else previous(args)
        self.assertTrue(endpoint_state(account, "alias", context, azure)["createDnsBinding"])
        azure.run.side_effect = lambda args: {"value": [{"properties": {"provisioningState": "Succeeded", "privateDnsZoneConfigs": []}}]} if args[0] == "rest" else previous(args)
        with self.assertRaises(MigrationError):
            endpoint_state(account, "alias", context, azure)

    def test_missing_pe_has_exact_target_scope(self):
        account, context, _, azure = self.endpoint_fixture()
        context["endpoints"] = []
        state = endpoint_state(account, "alias", context, azure)
        self.assertTrue(state["createEndpoint"])
        self.assertTrue(state["endpointId"].endswith("/pe-alias-account"))
        azure.run.assert_not_called()

    def test_matching_existing_role_reused_and_conditional_role_rejected(self):
        account = parse_catalog(catalog(), "v1")[0]
        assignment = {"id": account["accountResourceId"] + "/providers/Microsoft.Authorization/roleAssignments/customer-existing",
                      "scope": account["accountResourceId"], "principalId": PRINCIPAL,
                      "principalType": "ServicePrincipal", "roleDefinitionId": "/subscriptions/" + SUB + "/providers/Microsoft.Authorization/roleDefinitions/" + ROLE}
        azure = Mock(run=Mock(return_value=[assignment]))
        self.assertFalse(role_state(account, {"properties": {"principalId": PRINCIPAL}}, "", azure)["createRole"])
        azure.run.assert_called_once_with([
            "role", "assignment", "list", "--scope", account["accountResourceId"],
            "--subscription", account["subscriptionId"],
            "--fill-principal-name", "false", "--fill-role-definition-name", "false",
        ])
        assignment["condition"] = "restricted"
        with self.assertRaisesRegex(MigrationError, "conditions"):
            role_state(account, {"properties": {"principalId": PRINCIPAL}}, "", azure)

    def test_role_query_does_not_reuse_other_principals_or_parent_scopes(self):
        account = parse_catalog(catalog(), "v1")[0]
        assignment = {
            "scope": account["accountResourceId"], "principalId": PRINCIPAL,
            "principalType": "ServicePrincipal",
            "roleDefinitionId": "/subscriptions/" + SUB + "/providers/Microsoft.Authorization/roleDefinitions/" + ROLE,
        }
        for change in ({"principalId": CLIENT}, {"scope": "/subscriptions/" + SUB}):
            azure = Mock(run=Mock(return_value=[{**assignment, **change}]))
            with self.subTest(change=change):
                self.assertTrue(role_state(account, {"properties": {"principalId": PRINCIPAL}}, "", azure)["createRole"])

    def test_scoped_role_query_still_detects_foreign_assignment_name_collision(self):
        account = parse_catalog(catalog(), "v1")[0]
        azure = Mock(run=Mock(return_value=[]))
        expected = role_state(account, {"properties": {"principalId": PRINCIPAL}}, "", azure)["roleId"]
        azure.run.return_value = [{"id": expected, "principalId": CLIENT, "scope": account["accountResourceId"]}]
        with self.assertRaisesRegex(MigrationError, "name conflicts"):
            role_state(account, {"properties": {"principalId": PRINCIPAL}}, "", azure)

    def test_role_list_failure_is_not_treated_as_a_missing_assignment(self):
        account = parse_catalog(catalog(), "v1")[0]
        azure = Mock(run=Mock(side_effect=MigrationError("AuthorizationFailed")))
        with self.assertRaisesRegex(MigrationError, "AuthorizationFailed"):
            role_state(account, {"properties": {"principalId": PRINCIPAL}}, "", azure)

    def test_nested_full_payload_what_if_strict_allowlist(self):
        wrapper, role = "/scope/deployments/role", "/scope/roleAssignments/approved"
        document = {"status": "Succeeded", "changes": [{"resourceId": wrapper, "changeType": "Modify",
                     "after": {"properties": {"mode": "Incremental"}},
                     "resourceChanges": [{"resourceId": role, "changeType": "Create", "after": {"properties": {"principalId": PRINCIPAL}}}]}]}
        allowed = {wrapper.lower(): "deployment", role.lower(): "required"}
        assert_what_if(document, allowed)
        for kind in ("Delete", "Modify", "Unsupported"):
            bad = copy.deepcopy(document)
            bad["changes"][0]["resourceChanges"][0]["changeType"] = kind
            with self.assertRaises(MigrationError):
                assert_what_if(bad, allowed)
        bad = copy.deepcopy(document)
        bad["changes"][0]["resourceChanges"][0]["resourceId"] = "/scope/accounts/unapproved"
        with self.assertRaisesRegex(MigrationError, "allowlist"):
            assert_what_if(bad, allowed)
        with self.assertRaisesRegex(MigrationError, "expand"):
            assert_what_if({"status": "Succeeded", "changes": document["changes"][:1][0:0]}, allowed)

    def test_failed_what_if_cannot_mask_missing_rights(self):
        with self.assertRaisesRegex(MigrationError, "permissions"):
            assert_what_if({"status": "Failed", "error": {"code": "AuthorizationFailed"}}, {})

    def test_incremental_what_if_accepts_untouched_resources_outside_template(self):
        role = "/scope/roleAssignments/approved"
        allowed = {role.lower(): "required"}
        document = {"status": "Succeeded", "changes": [
            {"resourceId": role, "changeType": "Create"},
            {"resourceId": "/scope/managedClusters/existing", "changeType": "Ignore"},
            {"resourceId": "/scope/privateDnsZones/existing", "changeType": "Ignore"},
        ]}
        assert_what_if(document, allowed)
        for kind in ("Create", "Modify", "NoChange", "Delete", "Unsupported", "unexpected"):
            bad = copy.deepcopy(document)
            bad["changes"][1]["changeType"] = kind
            with self.subTest(kind=kind), self.assertRaises(MigrationError):
                assert_what_if(bad, allowed)

    def test_ignored_resource_cannot_satisfy_required_expansion(self):
        role = "/scope/roleAssignments/approved"
        with self.assertRaisesRegex(MigrationError, "expand"):
            assert_what_if({"status": "Succeeded", "changes": [
                {"resourceId": role, "changeType": "Ignore"},
            ]}, {role.lower(): "required"})

    def test_ignored_resource_still_requires_valid_resource_identity(self):
        for resource in (None, "", " ", 42):
            with self.subTest(resource=resource), self.assertRaisesRegex(MigrationError, "resourceId"):
                assert_what_if({"status": "Succeeded", "changes": [
                    {"resourceId": resource, "changeType": "Ignore"},
                ]}, {})

    def test_ignored_parent_cannot_hide_unapproved_nested_changes(self):
        role = "/scope/roleAssignments/approved"
        for kind in ("Create", "Modify", "Delete", "Unsupported"):
            document = {"status": "Succeeded", "changes": [
                {"resourceId": role, "changeType": "Create"},
                {"resourceId": "/scope/deployments/ignored", "changeType": "Ignore",
                 "resourceChanges": [{"resourceId": "/scope/accounts/unapproved", "changeType": kind}]},
            ]}
            with self.subTest(kind=kind), self.assertRaises(MigrationError):
                assert_what_if(document, {role.lower(): "required"})
        document["changes"][1]["resourceChanges"] = {"unexpected": "shape"}
        with self.assertRaisesRegex(MigrationError, "resourceChanges"):
            assert_what_if(document, {role.lower(): "required"})

    def test_cross_subscription_focused_plan_exact_resources_and_one_account_once(self):
        config = customer()
        accounts = parse_catalog(catalog(("synthetic-cross",), [OTHER_SUB]), "v1")
        _, matches = discover(config, accounts, CatalogAzure(accounts))
        desired = reconcile(config, matches)
        account = accounts[0]
        group = "/subscriptions/" + SUB + "/resourceGroups/rg-secure"
        zone = group + "/providers/Microsoft.Network/privateDnsZones/privatelink.openai.azure.com"
        alias = desired["parameters"]["platform"]["azureOpenAIConnections"][-1]["alias"]
        endpoint = group + "/providers/Microsoft.Network/privateEndpoints/pe-" + alias + "-account"
        role = role_state(account, {"properties": {"principalId": PRINCIPAL}}, "", Mock(run=Mock(return_value=[])))
        context = {"endpoints": [], "links": [], "zoneId": zone,
                   "linkId": zone + "/virtualNetworkLinks/model-sync-target-vnet"}
        role_wrapper = "/subscriptions/" + OTHER_SUB + "/resourceGroups/rg-model/providers/Microsoft.Resources/deployments/model-sync-role-" + alias
        pe_wrapper = group + "/providers/Microsoft.Resources/deployments/model-sync-pe-" + alias
        what_if = {"status": "Succeeded", "changes": [
            {"resourceId": pe_wrapper, "changeType": "Create", "resourceChanges": [
                {"resourceId": endpoint, "changeType": "Create", "after": {"properties": {"subnet": {"id": "/approved/subnet"}}}},
                {"resourceId": endpoint + "/privateDnsZoneGroups/default", "changeType": "Create"}]},
            {"resourceId": role_wrapper, "changeType": "Modify", "resourceChanges": [
                {"resourceId": role["roleId"], "changeType": "Create", "after": {"properties": {"principalId": PRINCIPAL}}}]},
            {"resourceId": context["linkId"], "changeType": "Create"}]}
        azure = Mock(run=Mock(return_value=what_if))
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, \
                patch("scripts.model_sync_infra.network_context", return_value=context), \
                patch("scripts.model_sync_infra.role_state", return_value=role):
            plan = infrastructure_plan(config, desired, matches + matches,
                                       {"id": "/identity", "properties": {"principalId": PRINCIPAL}},
                                       Path(destination) / "template.json", Path(destination), azure)
        self.assertEqual(len(plan["accounts"]), 1)
        self.assertEqual(plan["accounts"][0]["parameters"]["accountSubscriptionId"], OTHER_SUB)
        self.assertEqual(plan["accounts"][0]["parameters"]["principalId"], PRINCIPAL)
        self.assertTrue(plan["accounts"][0]["parameters"]["createEndpoint"])
        self.assertNotIn(account["accountResourceId"].lower(), plan["accounts"][0]["allowedResources"])
        self.assertIn("--result-format", azure.run.call_args.args[0])
        self.assertIn("FullResourcePayloads", azure.run.call_args.args[0])
        arguments = azure.run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--mode") + 1], "Incremental")

    def test_execute_records_partial_infra_before_pending_pe_blocks_application(self):
        account = parse_catalog(catalog(), "v1")[0]
        plan = {"accounts": [{"account": account, "allowedResources": {"approved": "required"},
                             "parameters": {"accountAlias": "alias", "principalId": PRINCIPAL,
                                            "principalSourceResourceId": ""}}]}
        record = {"infrastructureCompleted": []}
        azure = Mock(run=Mock(return_value={"properties": {"provisioningState": "Succeeded"}}))
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, \
                patch("scripts.model_sync_infra.network_context", return_value={"links": [True]}), \
                patch("scripts.model_sync_infra.endpoint_state", side_effect=MigrationError("Pending approval")), \
                self.assertRaisesRegex(MigrationError, "Pending"):
            execute_infrastructure(customer(), plan, Path(destination) / "template.json", Path(destination), azure, record)
            self.fail("Pending PE must stop")
        self.assertEqual(record["infrastructureAttempted"], ["alias"])
        self.assertEqual(record["infrastructureCompleted"], ["alias"])

    def test_execute_azure_auth_failure_records_attempt_without_false_completion(self):
        plan = {"accounts": [{"allowedResources": {"approved": "required"}, "parameters": {"accountAlias": "alias"}}]}
        record = {"infrastructureCompleted": []}
        azure = Mock(run=Mock(side_effect=MigrationError("AuthorizationFailed: roleAssignments/write")))
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, self.assertRaisesRegex(MigrationError, "roleAssignments"):
            execute_infrastructure(customer(), plan, Path(destination) / "template.json", Path(destination), azure, record)
            self.fail("Rights failure must stop")
        self.assertEqual(record["infrastructureAttempted"], ["alias"])
        self.assertEqual(record["infrastructureCompleted"], [])

    def test_failed_arm_receipt_is_not_recorded_as_completed(self):
        plan = {"accounts": [{"allowedResources": {"approved": "required"}, "parameters": {"accountAlias": "alias"}}]}
        record = {"infrastructureCompleted": []}
        azure = Mock(run=Mock(return_value={"properties": {"provisioningState": "Failed"}}))
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, self.assertRaisesRegex(MigrationError, "did not succeed"):
            execute_infrastructure(customer(), plan, Path(destination) / "template.json", Path(destination), azure, record)
        self.assertEqual(record["infrastructureReceipts"], {"alias": "Failed"})
        self.assertEqual(record["infrastructureCompleted"], [])


class ModelSyncRuntimeTests(unittest.TestCase):
    def test_replace_removes_obsolete_model_affinity_and_preserves_unrelated_settings(self):
        config = customer()
        current = current_baseline(config)
        current["runtime"]["router_settings"]["model_group_affinity_config"].update(
            {"custom-unmapped-group": ["preserve-this"]})
        accounts = parse_catalog(catalog(), "v1")
        _, matches = discover(config, accounts, CatalogAzure(accounts))
        desired = reconcile(config, matches, "replace")
        payload = runtime.render(desired, current)
        rendered = yaml.safe_load(payload["configMap"]["data"]["config.yaml"])
        self.assertEqual([m["model_name"] for m in rendered["model_list"]], ["chat"])
        self.assertEqual(rendered["router_settings"]["model_group_affinity_config"],
                         {"chat": ["region"], "custom-unmapped-group": ["preserve-this"]})
        self.assertIn("coding", current["runtime"]["router_settings"]["model_group_affinity_config"])
        self.assertEqual(rendered["non_model"], current["runtime"]["non_model"])
        self.assertEqual(rendered["general_settings"], current["runtime"]["general_settings"])
        self.assertTrue(payload["modelChanged"])

    def test_autoscaler_binds_backend_identity_and_spec_not_volatile_status(self):
        hpa = autoscaler_document()
        unrelated = copy.deepcopy(hpa)
        unrelated["spec"]["scaleTargetRef"]["name"] = "other"
        hpa["status"] = {"currentReplicas": 2, "desiredReplicas": 3}
        with patch.object(runtime, "command", return_value=json.dumps({"items": [unrelated, hpa]})) as cmd:
            approved = runtime.autoscaler(["kubectl"])
        cmd.assert_called_once_with(["kubectl"], ["get", "hpa", "-o", "json"])
        self.assertEqual(approved, {"name": "litellm", "namespace": "litellm", "uid": "hpa-uid", "spec": hpa["spec"]})
        hpa["status"]["desiredReplicas"] = 4
        with patch.object(runtime, "command", return_value=json.dumps({"items": [hpa]})):
            self.assertEqual(runtime.autoscaler(["kubectl"]), approved)
        hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] = 80
        with patch.object(runtime, "command", return_value=json.dumps({"items": [hpa]})):
            self.assertNotEqual(runtime.autoscaler(["kubectl"]), approved)

    def test_missing_hpa_is_explicit_and_query_failures_are_not_absence(self):
        with patch.object(runtime, "command", return_value=json.dumps({"items": []})):
            self.assertIsNone(runtime.autoscaler(["kubectl"]))
        with patch.object(runtime, "command", side_effect=MigrationError("HPA read denied")), \
                self.assertRaisesRegex(MigrationError, "read denied"):
            runtime.autoscaler(["kubectl"])

    def test_invalid_or_ambiguous_autoscaler_blocks_baseline(self):
        hpa = autoscaler_document()
        documents = [{"items": [hpa, hpa]}, {"items": None}, {"items": [None]}]
        for path, value in (
            (["metadata", "uid"], ""), (["metadata", "namespace"], "other"),
            (["metadata", "deletionTimestamp"], "2026-10-09T00:00:00Z"),
            (["spec", "scaleTargetRef", "apiVersion"], "unsupported/v1"),
            (["spec", "minReplicas"], True), (["spec", "maxReplicas"], "6"),
            (["spec", "minReplicas"], 0), (["spec", "minReplicas"], 7),
        ):
            bad = copy.deepcopy(hpa)
            target = bad
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            documents.append({"items": [bad]})
        for document in documents:
            with self.subTest(document=document), \
                    patch.object(runtime, "command", return_value=json.dumps(document)), \
                    self.assertRaises(MigrationError):
                runtime.autoscaler(["kubectl"])

    def test_post_rollout_allows_only_scaling_with_unchanged_approved_hpa(self):
        current = current_baseline(customer())
        for replicas in (2, 3, 6):
            after = copy.deepcopy(current["deployment"])
            after["spec"]["replicas"] = replicas
            after["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] = "new-config"
            with self.subTest(replicas=replicas), \
                    patch.object(runtime, "command", return_value=json.dumps({"items": [autoscaler_document()]})):
                runtime.verify_deployment(["kubectl"], after, current, "new-config")
        self.assertEqual(current["deployment"]["spec"]["replicas"], 2)
        self.assertEqual(current["deployment"]["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"], "old-config")

    def test_post_rollout_hpa_removal_replacement_and_spec_changes_block(self):
        current = current_baseline(customer())
        documents = [{"items": []}]
        for path, value in (
            (["metadata", "uid"], "replacement"),
            (["metadata", "name"], "renamed"),
            (["spec", "maxReplicas"], 7),
            (["spec", "metrics", 0, "resource", "target", "averageUtilization"], 80),
            (["spec", "scaleTargetRef", "name"], "other"),
        ):
            changed = autoscaler_document()
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            documents.append({"items": [changed]})
        for document in documents:
            with self.subTest(document=document), \
                    patch.object(runtime, "command", return_value=json.dumps(document)), \
                    self.assertRaisesRegex(MigrationError, "autoscaler differs"):
                runtime.verify_deployment(["kubectl"], current["deployment"], current, "old-config")

    def test_post_rollout_without_hpa_keeps_exact_replica_comparison(self):
        current = current_baseline(customer())
        current["autoscaler"] = None
        with patch.object(runtime, "command", return_value=json.dumps({"items": []})):
            runtime.verify_deployment(["kubectl"], current["deployment"], current, "old-config")
            changed = copy.deepcopy(current["deployment"])
            changed["spec"]["replicas"] = 3
            with self.assertRaisesRegex(MigrationError, "exact model-only"):
                runtime.verify_deployment(["kubectl"], changed, current, "old-config")

    def test_post_rollout_rejects_out_of_bounds_and_noninteger_replicas(self):
        current = current_baseline(customer())
        for replicas in (1, 7, True, 3.0, "3", None):
            changed = copy.deepcopy(current["deployment"])
            changed["spec"]["replicas"] = replicas
            with self.subTest(replicas=replicas), \
                    patch.object(runtime, "command", return_value=json.dumps({"items": [autoscaler_document()]})), \
                    self.assertRaisesRegex(MigrationError, "autoscaler bounds"):
                runtime.verify_deployment(["kubectl"], changed, current, "old-config")

    def test_post_rollout_scaling_cannot_mask_any_other_deployment_drift(self):
        current = current_baseline(customer())
        sentinel = "credential-value-must-not-be-logged"
        for path, value in (
            (["template", "spec", "containers", 0, "image"], "unapproved"),
            (["template", "spec", "containers", 0, "env", 0, "value"], sentinel),
            (["template", "spec", "containers", 0, "args", 0], "--unapproved"),
            (["template", "spec", "containers", 0, "volumeMounts", 0, "readOnly"], False),
            (["template", "spec", "volumes", 0, "configMap", "name"], "unapproved"),
            (["template", "spec", "volumes", 1, "csi", "driver"], "unapproved"),
            (["template", "spec", "serviceAccountName"], "unapproved"),
            (["template", "metadata", "labels", "azure.workload.identity/use"], "false"),
            (["selector", "matchLabels", "app"], "other"),
        ):
            changed = copy.deepcopy(current["deployment"])
            changed["spec"]["replicas"] = 3
            target = changed["spec"]
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), \
                    patch.object(runtime, "command", return_value=json.dumps({"items": [autoscaler_document()]})), \
                    self.assertRaisesRegex(MigrationError, "exact model-only") as failure:
                runtime.verify_deployment(["kubectl"], changed, current, "old-config")
            self.assertNotIn(sentinel, str(failure.exception))

    def test_post_rollout_rejects_replaced_deployment_and_old_approval(self):
        current = current_baseline(customer())
        replaced = copy.deepcopy(current["deployment"])
        replaced["metadata"]["uid"] = "replacement"
        with patch.object(runtime, "command") as cmd, self.assertRaisesRegex(MigrationError, "UID"):
            runtime.verify_deployment(["kubectl"], replaced, current, "old-config")
        cmd.assert_not_called()
        del current["autoscaler"]
        with patch.object(runtime, "command") as cmd, self.assertRaisesRegex(MigrationError, "lacks autoscaler evidence"):
            runtime.verify_deployment(["kubectl"], current["deployment"], current, "old-config")
        cmd.assert_not_called()

    def test_baseline_mapping_drift_and_inline_credentials_fail(self):
        config = customer()
        for field in ("mapping", "credentials", "identity"):
            deployment, service, cm = runtime_documents(config)
            if field == "mapping":
                document = yaml.safe_load(cm["data"]["config.yaml"])
                document["model_list"][0]["litellm_params"]["api_base"] = "https://poison.invalid"
                cm["data"]["config.yaml"] = yaml.safe_dump(document)
            elif field == "credentials":
                document = yaml.safe_load(cm["data"]["config.yaml"])
                document["general_settings"]["master_key"] = "literal"
                cm["data"]["config.yaml"] = yaml.safe_dump(document)
            else:
                service["metadata"]["annotations"]["azure.workload.identity/client-id"] = OTHER_SUB
            with patch.object(runtime, "command", side_effect=[json.dumps(deployment), json.dumps(service), json.dumps(cm)]), self.assertRaises(MigrationError):
                runtime.baseline(config, {"properties": {"clientId": CLIENT}}, ["kubectl"])

    def test_model_only_render_preserves_every_non_model_field(self):
        config = customer()
        current = current_baseline(config)
        accounts = parse_catalog(catalog(), "v1")
        _, matches = discover(config, accounts, CatalogAzure(accounts))
        desired = reconcile(config, matches)
        payload = runtime.render(desired, current)
        changed = yaml.safe_load(payload["configMap"]["data"]["config.yaml"])
        self.assertEqual(changed["non_model"], current["runtime"]["non_model"])
        self.assertEqual(changed["general_settings"], current["runtime"]["general_settings"])
        self.assertEqual(changed["litellm_settings"], current["runtime"]["litellm_settings"])
        self.assertEqual(payload["patch"][-1]["path"], "/spec/template/spec/volumes/0/configMap/name")
        self.assertTrue(payload["configMap"]["immutable"])
        self.assertTrue(payload["modelChanged"])
        self.assertFalse(runtime.render(config, current)["modelChanged"])

    def test_noop_does_not_server_apply_or_patch(self):
        current = current_baseline(customer())
        payload = runtime.render(customer(), current)
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, patch.object(runtime, "command") as cmd:
            runtime.dry_run(["kubectl"], payload, Path(destination))
            cmd.assert_not_called()

    def test_server_dry_run_rejects_unrelated_pod_mutation(self):
        current = current_baseline(customer())
        config = customer()
        config["application"]["models"][0]["apiVersion"] = "2024-10-21"
        payload = runtime.render(config, current)
        payload.update(_baselineSpec=current["deployment"]["spec"], _volumeIndex=0)
        modified = copy.deepcopy(current["deployment"])
        modified["spec"]["template"]["spec"]["containers"][0]["image"] = "unapproved"
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, patch.object(runtime, "command", side_effect=["{}", json.dumps(modified)]), self.assertRaisesRegex(MigrationError, "more than"):
            runtime.dry_run(["kubectl"], payload, Path(destination))

    def test_preapply_resource_version_drift_means_no_writes(self):
        current = current_baseline(customer())
        config = customer()
        config["application"]["models"][0]["apiVersion"] = "2024-10-21"
        payload = runtime.render(config, current)
        fresh = copy.deepcopy(current["deployment"])
        fresh["metadata"]["resourceVersion"] = "43"
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, patch.object(runtime, "command", return_value=json.dumps(fresh)) as cmd, self.assertRaisesRegex(MigrationError, "drifted"):
            runtime.apply(["kubectl"], payload, current, Path(destination), {})
        self.assertEqual(cmd.call_count, 1)

    def test_preapply_hpa_drift_means_no_application_writes(self):
        config = customer()
        current = current_baseline(config)
        config["application"]["models"][0]["apiVersion"] = "2024-10-21"
        payload = runtime.render(config, current)
        hpa = autoscaler_document()
        hpa["spec"]["maxReplicas"] = 7
        outputs = [json.dumps(current["deployment"]), json.dumps({"items": [hpa]})]
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, \
                patch.object(runtime, "command", side_effect=outputs) as cmd, \
                self.assertRaisesRegex(MigrationError, "Autoscaler drifted"):
            runtime.apply(["kubectl"], payload, current, Path(destination), {})
        self.assertEqual(cmd.call_count, 2)
        self.assertTrue(all(call.args[1][0] == "get" for call in cmd.call_args_list))

    def test_network_probe_no_token_no_http_and_tls_verified(self):
        account = parse_catalog(catalog(), "v1")[0]
        with patch.object(runtime, "command", return_value="private-dns-tls-ok\n") as cmd:
            runtime.network_probe(["kubectl"], account, ["10.30.8.5"])
        args = cmd.call_args.args[1]
        code = args[args.index("-c", args.index("python")) + 1]
        self.assertIn("ssl.create_default_context()", code)
        self.assertNotIn("token", code)
        self.assertNotIn("requests", code)

    def test_cost_only_rollout_postread_and_each_ready_pod_mount_are_verified_not_inference(self):
        config = customer()
        current = current_baseline(config)
        config["application"]["models"][0]["modelInfo"] = {"input_cost_per_token": 0.000003}
        payload = runtime.render(config, current)
        after = copy.deepcopy(current["deployment"])
        after["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] = payload["configMap"]["metadata"]["name"]
        pods = {"items": [{"metadata": {"name": "backend-" + str(i)},
                           "status": {"conditions": [{"type": "Ready", "status": "True"}]}} for i in range(2)]}
        content_hash = hashlib.sha256(payload["configMap"]["data"]["config.yaml"].encode()).hexdigest()
        outputs = [json.dumps(current["deployment"]), json.dumps({"items": [autoscaler_document()]}),
                   "", "", "rollout complete", json.dumps(after),
                   json.dumps({"items": [autoscaler_document()]}),
                   json.dumps(payload["configMap"]), json.dumps(pods), content_hash, content_hash]
        record = {}
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, patch.object(runtime, "command", side_effect=outputs) as cmd:
            runtime.apply(["kubectl"], payload, current, Path(destination), record)
        self.assertTrue(record["applicationPatched"])
        self.assertTrue(record["rolloutVerified"])
        self.assertFalse(record["inferenceVerified"])
        self.assertEqual(sum(call.args[1][0] == "exec" for call in cmd.call_args_list), 2)

    def test_noop_checks_rollout_and_mount_without_apply_or_patch(self):
        config = customer()
        current = current_baseline(config)
        payload = runtime.render(config, current)
        pods = {"items": [{"metadata": {"name": "backend-" + str(i)},
                           "status": {"conditions": [{"type": "Ready", "status": "True"}]}} for i in range(2)]}
        cm = runtime_documents(config)[2]
        content_hash = hashlib.sha256(cm["data"]["config.yaml"].encode()).hexdigest()
        outputs = ["rollout complete", json.dumps(current["deployment"]),
                   json.dumps({"items": [autoscaler_document()]}), json.dumps(cm), json.dumps(pods),
                   content_hash, content_hash]
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, patch.object(runtime, "command", side_effect=outputs) as cmd:
            record = {}
            runtime.apply(["kubectl"], payload, current, Path(destination), record)
        self.assertTrue(record["rolloutVerified"])
        self.assertFalse(record["inferenceVerified"])
        self.assertFalse(any(call.args[1][0] in {"apply", "patch"} for call in cmd.call_args_list))

    def test_scaled_rollout_checks_each_required_replica_mount(self):
        config = customer()
        current = current_baseline(config)
        for model_changed in (False, True):
            for pod_count in (2, 3):
                with self.subTest(model_changed=model_changed, pod_count=pod_count):
                    desired = copy.deepcopy(config)
                    if model_changed:
                        desired["application"]["models"][0]["modelInfo"] = {"input_cost_per_token": 0.000003}
                    payload = runtime.render(desired, current)
                    after = copy.deepcopy(current["deployment"])
                    after["spec"]["replicas"] = 3
                    cm = payload["configMap"] if model_changed else runtime_documents(config)[2]
                    after["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] = cm["metadata"]["name"]
                    pods = {"items": [{"metadata": {"name": "backend-" + str(i)},
                                      "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
                                     for i in range(pod_count)]}
                    digest = hashlib.sha256(cm["data"]["config.yaml"].encode()).hexdigest()
                    outputs = ([json.dumps(current["deployment"]), json.dumps({"items": [autoscaler_document()]}),
                                "", ""] if model_changed else []) + [
                        "rollout complete", json.dumps(after), json.dumps({"items": [autoscaler_document()]}),
                        json.dumps(cm), json.dumps(pods), *([digest] * pod_count),
                    ]
                    record = {}
                    with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, \
                            patch.object(runtime, "command", side_effect=outputs) as cmd:
                        if pod_count == 3:
                            runtime.apply(["kubectl"], payload, current, Path(destination), record)
                            self.assertTrue(record["rolloutVerified"])
                            self.assertFalse(record["inferenceVerified"])
                            self.assertEqual(sum(call.args[1][0] == "exec" for call in cmd.call_args_list), 3)
                        else:
                            with self.assertRaisesRegex(MigrationError, "replicas are ready"):
                                runtime.apply(["kubectl"], payload, current, Path(destination), record)
                            self.assertNotIn("rolloutVerified", record)
                        self.assertEqual(any(call.args[1][0] in {"apply", "patch"} for call in cmd.call_args_list), model_changed)

    def test_passwords_in_urls_or_redis_fields_are_never_saved(self):
        for value in ({"redis_password": "literal"}, {"database": "postgresql://user:password@database/db"}):
            with self.assertRaises(MigrationError):
                runtime.safe_settings(value)

    def test_rendered_backend_identity_settings_pass_credential_validation(self):
        for mode in ("native", "entra"):
            with self.subTest(mode=mode):
                config = customer()
                config["application"]["authentication"] = {"mode": mode}
                if mode == "native":
                    config["application"]["authentication"]["adminUsername"] = "gateway-admin"
                platform = {
                    "keyVaultName": "synthetic-backend", "workloadIdentityClientId": CLIENT,
                    "workloadIdentityPrincipalId": PRINCIPAL,
                    "managedRedisHostName": "synthetic.westus.redis.azure.net",
                }
                versions = {
                    name: {"id": f"https://synthetic-backend.vault.azure.net/secrets/{name}/" + "a" * 32,
                           "version": "a" * 32}
                    for name in backend_secrets(config)
                }
                documents = render_backend_manifest(
                    config, platform, versions, "synthetic.postgres.database.azure.com", "10.30.8.0/24",
                )
                config_map = next(item for item in documents if item["kind"] == "ConfigMap"
                                  and "config.yaml" in item.get("data", {}))
                settings = yaml.safe_load(config_map["data"]["config.yaml"])
                self.assertIs(settings["router_settings"]["cache_kwargs"]["azure_redis_ad_token"], True)
                runtime.safe_settings(settings)

    def test_identity_switches_require_true_booleans_at_exact_paths(self):
        for path in runtime.IDENTITY_SWITCH_PATHS:
            for value in (False, None, 0, 1, "true", "literal-token", "os.environ/TOKEN", {}, []):
                with self.subTest(path=path, value=value):
                    settings = value
                    for field in reversed(path):
                        settings = {field: settings}
                    with self.assertRaisesRegex(MigrationError, "identity authentication switch must be true"):
                        runtime.safe_settings(settings)
        for settings in (
            {"azure_redis_ad_token": True},
            {"router_settings": {"azure_redis_ad_token": True}},
            {"general_settings": {"enable_azure_ad_token_refresh": True}},
            {"nested": [{"litellm_settings": {"enable_azure_ad_token_refresh": True}}]},
        ):
            with self.subTest(settings=settings), self.assertRaises(MigrationError):
                runtime.safe_settings(settings)

    def test_identity_switch_does_not_allow_inline_credentials(self):
        sentinel = "credential-value-must-not-be-logged"
        for field in ("api_key", "master_key", "password", "token", "secret", "redis_password"):
            settings = {
                "router_settings": {"cache_kwargs": {"azure_redis_ad_token": True, field: sentinel}},
                "litellm_settings": {"enable_azure_ad_token_refresh": True},
            }
            with self.subTest(field=field), self.assertRaises(MigrationError) as failure:
                runtime.safe_settings(settings)
            self.assertNotIn(sentinel, str(failure.exception))

    def test_identity_federation_must_match_actual_aks_issuer_and_serviceaccount(self):
        config = customer()
        group = "/subscriptions/" + SUB + "/resourceGroups/rg-secure"
        identity_id = group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-litellm-workload-test"
        identity = {"id": identity_id, "properties": {"tenantId": TENANT, "principalId": PRINCIPAL, "clientId": CLIENT}}
        aks = {"id": group + "/providers/Microsoft.ContainerService/managedClusters/new-aks",
               "properties": {"oidcIssuerProfile": {"issuerURL": "https://issuer.invalid/"},
                              "securityProfile": {"workloadIdentity": {"enabled": True}}}}
        federation = {"properties": {"issuer": "https://issuer.invalid/", "subject": "system:serviceaccount:litellm:litellm",
                                     "audiences": ["api://AzureADTokenExchange"]}}
        azure = Mock(run=Mock(side_effect=[identity, aks, {"value": [federation]}]))
        self.assertEqual(runtime.identity_context(config, azure)["federation"], federation)
        federation["properties"]["subject"] = "system:serviceaccount:other:litellm"
        azure.run.side_effect = [identity, aks, {"value": [federation]}]
        with self.assertRaisesRegex(MigrationError, "federation"):
            runtime.identity_context(config, azure)


class ModelSyncCliTests(unittest.TestCase):
    def run_sync(self, destination, operation, approved="", failure=None, catalog_document=None, model_policy="replace"):
        config = customer()
        config_path, catalog_path = destination / "customer.json", destination / "catalog.json"
        document = {**config, "localExecution": {"preserved": True}}
        config_path.write_text(json.dumps(document))
        document_catalog = catalog() if catalog_document is None else catalog_document
        catalog_path.write_text(json.dumps(document_catalog))
        accounts = parse_catalog(document_catalog, "v1")
        azure = CatalogAzure(accounts)
        current = current_baseline(config)
        identity = {"identity": {"id": "/identity", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL}}}
        infrastructure = {"context": {}, "accounts": []}
        def compile_template(path):
            template = path / "model-sync.template.json"
            template.write_text("{}")
            return template
        def prepare(*args):
            desired = args[2]
            payload = runtime.render(desired, current)
            return current, payload, copy.deepcopy(infrastructure)
        with patch.object(model_sync, "protected_source"), patch.object(model_sync, "load_config", return_value=(config, {})), \
                patch.object(model_sync, "operation_root", return_value=destination), \
                patch.object(model_sync, "authenticate_azure"), patch.object(model_sync, "reviewed_revision", return_value="a" * 40), \
                patch.object(model_sync, "source_fingerprints", return_value={"source": "fixed"}), \
                patch.object(model_sync, "AzureCommands", return_value=azure), patch.object(runtime, "identity_context", return_value=identity), \
                patch.object(runtime, "connect_cluster", return_value=["kubectl"]), \
                patch.object(model_sync, "compile_template", side_effect=compile_template), \
                patch.object(model_sync, "prepare", side_effect=prepare), \
                patch.object(model_sync, "execute_infrastructure", side_effect=failure if failure == "infra" else None) as infra, \
                patch.object(runtime, "baseline", return_value=current), \
                patch.object(runtime, "apply", side_effect=MigrationError("rollout failed") if failure == "rollout" else None) as apply, \
                patch.object(model_sync, "install_report", side_effect=OSError("write failed") if failure == "config" else None) as install:
            result = model_sync.sync(config_path, catalog_path, "v1", operation, approved, "CHG-real", APPROVERS,
                                     model_policy=model_policy)
            return result, infra, apply, install

    def test_replace_plan_explicitly_previews_removed_mappings_and_client_names(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            result, infra, apply, install = self.run_sync(path, "plan", model_policy="replace")
            review = json.loads((path / "model-sync-review.json").read_text())
            desired = json.loads((path / "desired-customer.json").read_text())
            self.assertEqual(result["modelPolicy"], "replace")
            self.assertEqual(review["modelPolicy"], "replace")
            self.assertEqual(result["removedModelGroups"], ["coding"])
            expected_removed = [{key: mapping[key] for key in ("id", "modelGroup", "connectionAlias", "deploymentName")}
                                for mapping in customer()["application"]["models"]]
            self.assertEqual(result["removedModels"], expected_removed)
            self.assertEqual(result["removedModels"], review["removedModels"])
            self.assertEqual([m["modelGroup"] for m in desired["application"]["models"]], ["chat"])
            rendered = yaml.safe_load(review["application"]["configMap"]["data"]["config.yaml"])
            self.assertEqual([m["model_name"] for m in rendered["model_list"]], ["chat"])
            infra.assert_not_called()
            apply.assert_not_called()
            install.assert_not_called()

    def test_changing_model_policy_invalidates_approval_before_any_writes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            planned, _, _, _ = self.run_sync(path, "plan", model_policy="merge")
            self.assertEqual(planned["modelPolicy"], "merge")
            self.assertEqual(planned["removedModels"], [])
            with self.assertRaisesRegex(MigrationError, "no writes"):
                self.run_sync(path, "execute", planned["planSha256"], model_policy="replace")
            state = json.loads((path / "model-sync-state.json").read_text())
            self.assertFalse(state["applicationPatched"])
            self.assertFalse(state["configInstalled"])
            self.assertEqual(state["infrastructureCompleted"], [])
            self.assertFalse((path / "previous-customer.json").exists())

    def test_cli_defaults_to_replace_and_merge_requires_explicit_option(self):
        base = ["model-sync", "--config", "customer.json", "--catalog", "catalog.json",
                "--operation", "plan", "--change-ticket", "CHG-real", "--approved-by", TENANT]
        for options, expected in (([], "replace"), (["--model-policy", "merge"], "merge")):
            with self.subTest(options=options), patch("sys.argv", base + options), \
                    patch.object(model_sync, "sync", return_value={"status": "planned"}) as sync:
                model_sync.main()
            self.assertEqual(sync.call_args.args[-1], expected)

    def test_replace_execute_installs_only_catalog_models_without_touching_vkeys(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            planned, _, _, _ = self.run_sync(path, "plan", model_policy="replace")
            completed, infra, apply, install = self.run_sync(path, "execute", planned["planSha256"], model_policy="replace")
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["removedModelGroups"], ["coding"])
            installed = install.call_args.args[0]
            self.assertEqual([m["modelGroup"] for m in installed["application"]["models"]], ["chat"])
            self.assertEqual(installed["localExecution"], {"preserved": True})
            infra.assert_called_once()
            apply.assert_called_once()
            self.assertEqual(apply.call_args.args[1]["patch"][-1]["op"], "replace")

    def test_plan_no_writes_stdout_result_private_review(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            result, infra, apply, install = self.run_sync(path, "plan")
            self.assertEqual(result["status"], "planned")
            infra.assert_not_called()
            apply.assert_not_called()
            install.assert_not_called()
            self.assertEqual((path / "model-sync-review.json").stat().st_mode & 0o777, 0o600)

    def test_stale_approval_no_writes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            with patch.object(model_sync, "execute_infrastructure") as writes, self.assertRaisesRegex(MigrationError, "no writes"):
                self.run_sync(path, "execute", "0" * 64)
            writes.assert_not_called()
            self.assertFalse((path / "previous-customer.json").exists())

    def test_changed_pricing_stales_approved_hash_before_any_writes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            planned, _, _, _ = self.run_sync(path, "plan")
            changed = catalog()
            changed["subscriptions"][0]["resources"][0]["models"][0]["model_info"] = {
                "input_cost_per_token": 0.000003, "output_cost_per_token": 0.000004}
            with self.assertRaisesRegex(MigrationError, "no writes"):
                self.run_sync(path, "execute", planned["planSha256"], catalog_document=changed)
            state = json.loads((path / "model-sync-state.json").read_text())
            self.assertEqual(state["infrastructureCompleted"], [])
            self.assertFalse(state["applicationPatched"])
            self.assertFalse(state["configInstalled"])
            self.assertFalse((path / "previous-customer.json").exists())

    def test_matching_optional_endpoint_input_stales_prior_approval_hash(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            planned, _, _, _ = self.run_sync(path, "plan")
            changed = catalog()
            changed["subscriptions"][0]["resources"][0]["endpoint"] = "HTTPS://SYNTHETIC-CHAT.OPENAI.AZURE.COM:443/"
            with self.assertRaisesRegex(MigrationError, "no writes"):
                self.run_sync(path, "execute", planned["planSha256"], catalog_document=changed)
            state = json.loads((path / "model-sync-state.json").read_text())
            self.assertEqual(state["infrastructureCompleted"], [])
            self.assertFalse(state["applicationPatched"])
            self.assertFalse(state["configInstalled"])
            self.assertFalse((path / "previous-customer.json").exists())

    def test_execute_backup_and_preserve_local_settings(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            planned, _, _, _ = self.run_sync(path, "plan")
            completed, infra, apply, install = self.run_sync(path, "execute", planned["planSha256"])
            self.assertEqual(completed["status"], "completed")
            self.assertTrue(completed["configInstalled"])
            infra.assert_called_once()
            apply.assert_called_once()
            self.assertEqual(install.call_args.args[0]["localExecution"], {"preserved": True})
            self.assertTrue((path / "previous-customer.json").exists())
            self.assertTrue((path / "recovery-rollback-patch.json").exists())

    def test_rollout_and_config_install_failures_persist_recovery_state(self):
        for failure in ("rollout", "config"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
                path = Path(destination)
                planned, _, _, _ = self.run_sync(path, "plan")
                with self.assertRaises((MigrationError, OSError)):
                    self.run_sync(path, "execute", planned["planSha256"], failure=failure)
                state = json.loads((path / "model-sync-state.json").read_text())
                self.assertEqual(state["status"], "failed")
                self.assertFalse(state["configInstalled"])
                self.assertTrue(state["applicationPatchAttempted"])
                self.assertIn("independently reviewed", state["recovery"])
                self.assertTrue((path / "desired-customer.json").exists())
                self.assertTrue((path / "previous-customer.json").exists())

    def test_actual_approval_policy_not_fabricated(self):
        config = customer()
        with self.assertRaises(MigrationError):
            model_sync.approvals(config, "", APPROVERS)
        with self.assertRaises(MigrationError):
            model_sync.approvals(config, "CHG-real", [TENANT, TENANT])
        config["governance"] = {"approvalMode": "single-operator", "approverObjectIds": [TENANT],
                                "singleOperatorRiskAccepted": True}
        self.assertEqual(model_sync.approvals(config, "CHG-real", [TENANT])["approvedBy"], [TENANT])
        with self.assertRaises(MigrationError):
            model_sync.approvals(config, "CHG-real", [SUB])


if __name__ == "__main__":
    unittest.main()
