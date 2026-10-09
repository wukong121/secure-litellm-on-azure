import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from local_execution import model_sync_evidence as evidence
from scripts.customer_migration import ROOT, MigrationError, fingerprint, private_write, stage_fingerprint
from tests.test_model_sync import APPROVERS, CLIENT, current_baseline, customer


def completed_sync(directory):
    previous = customer()
    current = copy.deepcopy(previous)
    current["application"]["models"][0]["apiVersion"] = "2024-10-21"
    settings = {"preserved": "customer-local-settings"}
    old_document, new_document = {**previous, "localExecution": settings}, {**current, "localExecution": settings}
    proof = {"desiredConfigSha256": fingerprint(new_document), "application": {"runtimeSha256": fingerprint(current_baseline(current)["runtime"])},
             "infrastructure": {"accounts": []}, "revision": "a" * 40}
    digest = fingerprint(proof)
    sync_output = directory / "sync"
    sync_output.mkdir(mode=0o700, exist_ok=True)
    for name, value in (("previous-customer.json", old_document), ("desired-customer.json", new_document),
                        ("model-sync-review.json", {**proof, "planSha256": digest}),
                        ("model-sync-state.json", {"status": "completed", "rolloutVerified": True, "planSha256": digest})):
        private_write(sync_output / name, json.dumps(value))
    config_path = directory / "customer.json"
    private_write(config_path, json.dumps(new_document))
    return previous, current, proof, config_path, sync_output


class EvidenceProofTests(unittest.TestCase):
    def test_requires_completed_verified_exact_model_only_delta(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            previous, current, _, config_path, sync_output = completed_sync(Path(destination))
            with patch.object(evidence, "load_config", return_value=(previous, {})):
                old, _, digest = evidence.sync_proof(config_path, sync_output)
                self.assertEqual(old, previous)
                self.assertEqual(len(digest), 64)
                document = json.loads(config_path.read_text())
                document["location"] = "eastus"
                private_write(config_path, json.dumps(document))
                with self.assertRaisesRegex(MigrationError, "exact models/connections"):
                    evidence.sync_proof(config_path, sync_output)

    def test_rejects_failed_or_unverified_sync_and_tampered_review(self):
        for case in ("failed", "unverified", "tampered"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
                _, _, _, config_path, sync_output = completed_sync(Path(destination))
                state = json.loads((sync_output / "model-sync-state.json").read_text())
                if case == "failed":
                    state["status"] = "failed"
                elif case == "unverified":
                    state["rolloutVerified"] = False
                else:
                    review = json.loads((sync_output / "model-sync-review.json").read_text())
                    review["revision"] = "b" * 40
                    private_write(sync_output / "model-sync-review.json", json.dumps(review))
                private_write(sync_output / "model-sync-state.json", json.dumps(state))
                with self.assertRaisesRegex(MigrationError, "genuine completed"):
                    evidence.sync_proof(config_path, sync_output)


class EvidenceObservationTests(unittest.TestCase):
    def observe(self, directory, mutate=None):
        previous, config, proof, _, _ = completed_sync(directory)
        config["application"]["authentication"] = {"mode": "native", "adminUsername": "gateway-admin"}
        previous["application"]["authentication"] = config["application"]["authentication"]
        current = current_baseline(config)
        proof["application"]["runtimeSha256"] = fingerprint(current["runtime"])
        proof["completedSyncPlanSha256"] = "c" * 64
        ingress = {"configSha256": stage_fingerprint(previous, 4), "revision": "a" * 40,
                   "image": "trusted-ingress-image", "verifiedAt": "historical-original-observation",
                   "authenticationMode": "native", "backendRoutesVerified": False,
                   "api": {"privateIpAddress": "10.30.2.5"}, "admin": {"privateIpAddress": "10.30.2.6"},
                   "certificates": {"api": {"sha256": "a" * 64}, "admin": {"sha256": "b" * 64}}}
        backend = {"configSha256": stage_fingerprint(previous, 6), "revision": "a" * 40,
                   "backendRoutesVerified": True, "authenticationMode": "native"}
        states = {"api": {"podTemplateSha256": "api-stable"}, "admin": {"podTemplateSha256": "admin-stable"}}
        binding = {"configSha256": stage_fingerprint(previous, 9), "rolloutVerified": True,
                   "bindingMode": "native-private-ingress", "frontDoorId": CLIENT, "planes": copy.deepcopy(states)}
        if mutate:
            mutate(ingress, backend, binding)
        def objects(_kind, _name):
            if _kind == "deployment":
                return {"spec": {"template": {"spec": {"containers": [{"image": "trusted-ingress-image"}]}}}}
            if _kind == "service":
                plane = "api" if "api" in _name else "admin"
                return {"status": {"loadBalancer": {"ingress": [{"ip": ingress[plane]["privateIpAddress"], "ipMode": "VIP"}]}}}
            return {}
        client = Mock(get=Mock(side_effect=objects))
        azure = Mock(scoped=Mock(return_value={"nodeResourceGroup": "aks-nodes"}))
        with patch.object(evidence.runtime, "identity_context", return_value={"identity": {"properties": {"clientId": CLIENT}}}), \
                patch.object(evidence.runtime, "baseline", return_value=current), \
                patch.object(evidence.runtime, "apply") as mounted, \
                patch.object(evidence, "network_context", return_value={"links": [True]}), \
                patch.object(evidence, "receipt", side_effect=[ingress, backend, binding]), \
                patch.object(evidence, "deployed_edge", return_value={"frontDoorId": CLIENT}), \
                patch.object(evidence, "AuditCluster", return_value=client), \
                patch.object(evidence, "native_ingress_document_state", side_effect=[states["api"], states["admin"]]), \
                patch.object(evidence, "frontend_for", side_effect=[ingress["api"], ingress["admin"]]), \
                patch.object(evidence, "verify_native_ingress_routes") as probes:
            result = evidence.observe(config, previous, proof, "b" * 40, {"actualApproval": True},
                                      ["kubectl", "--namespace", "litellm"], directory, azure)
            mounted.assert_called_once()
            self.assertFalse(mounted.call_args.args[1]["modelChanged"])
            probes.assert_called_once_with(config, ingress, front_door_id=CLIENT)
            return result, config

    def test_revalidates_current_routes_mounts_and_preserves_original_claims_in_before(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            result, config = self.observe(Path(destination))
        self.assertEqual(result["before"]["privateIngress"]["verifiedAt"], "historical-original-observation")
        self.assertNotIn("verifiedAt", result["after"]["privateIngress"])
        self.assertEqual(result["after"]["privateIngressBackend"]["configSha256"], stage_fingerprint(config, 6))
        self.assertFalse(result["after"]["privateIngress"]["revalidation"]["inferenceVerified"])
        self.assertFalse(result["after"]["privateIngress"]["revalidation"]["stageAccepted"])
        self.assertEqual(result["after"]["privateIngress"]["revalidation"]["approval"], {"actualApproval": True})

    def test_unrelated_stale_stage6_or_stage4_receipt_rejected(self):
        for component in ("privateIngress", "privateIngressBackend"):
            def mutate(ingress, backend, _binding):
                (ingress if component == "privateIngress" else backend)["configSha256"] = "unrelated"
            with self.subTest(component=component), tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, \
                    self.assertRaisesRegex(MigrationError, "unrelated stale"):
                self.observe(Path(destination), mutate)

    def test_ingress_drift_from_previously_verified_binding_rejected(self):
        def mutate(_ingress, _backend, binding):
            binding["planes"]["api"]["podTemplateSha256"] = "drifted"
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, self.assertRaisesRegex(MigrationError, "previously verified binding"):
            self.observe(Path(destination), mutate)

    def test_entra_mode_is_explicit_blocker_not_implicit_receipt_acceptance(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, self.assertRaisesRegex(MigrationError, "native private ingress only"):
            evidence.observe(customer(), customer(), {}, "a" * 40, {}, [], Path(destination), Mock())


class EvidenceExecutionTests(unittest.TestCase):
    def run_refresh(self, directory, operation, approved="", fail_second=False, drift=False, no_op=False):
        previous, current, _proof, config_path, sync_output = completed_sync(directory / "inputs")
        observed = {"before": {"privateIngress": {"old": 4}, "privateIngressBackend": {"old": 6}},
                    "after": {"privateIngress": {"fresh": 4}, "privateIngressBackend": {"fresh": 6}}}
        if no_op:
            observed["after"] = copy.deepcopy(observed["before"])
        live = copy.deepcopy(observed["before"])
        count = 0
        def read_receipt(_config, _stage, _component, output, _azure):
            if drift:
                return {"unexpected": True}
            return live[output]
        def deploy(args):
            nonlocal count
            count += 1
            if fail_second and count == 2:
                raise MigrationError("receipt write permission denied")
            output = "privateIngress" if args[args.index("--name") + 1].endswith("private-ingress") else "privateIngressBackend"
            live[output] = copy.deepcopy(observed["after"][output])
            return {"properties": {"provisioningState": "Succeeded"}}
        azure = Mock(scoped=Mock(side_effect=deploy))
        with patch.object(evidence, "load_config", side_effect=lambda path: (previous if path.name == "previous-customer.json" else current, {})), \
                patch.object(evidence, "reviewed_revision", return_value="a" * 40), \
                patch.object(evidence, "source_fingerprints", return_value={"source": "fixed"}), \
                patch.object(evidence, "operation_root", return_value=directory), \
                patch.object(evidence, "authenticate_azure"), patch.object(evidence.runtime, "connect_cluster", return_value=["kubectl"]), \
                patch.object(evidence, "AzureCommands", return_value=azure), \
                patch.object(evidence, "observe", return_value=observed), \
                patch.object(evidence, "unchanged_workloads"), \
                patch.object(evidence, "receipt", side_effect=read_receipt):
            result = evidence.refresh(config_path, sync_output, operation, approved, "CHG-real", APPROVERS)
            return result, azure

    def test_plan_is_read_only_and_receipt_templates_have_no_resources(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            (path / "inputs").mkdir()
            result, azure = self.run_refresh(path, "plan")
            azure.scoped.assert_not_called()
            self.assertEqual(result["status"], "planned")
            for output in ("privateIngress", "privateIngressBackend"):
                template = json.loads((path / (output + ".json")).read_text())
                self.assertEqual(template["resources"], [])
                self.assertEqual((path / (output + ".json")).stat().st_mode & 0o777, 0o600)

    def test_fresh_approval_writes_only_two_receipts_and_postreads(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            (path / "inputs").mkdir()
            result, _ = self.run_refresh(path, "plan")
            finished, azure = self.run_refresh(path, "execute", result["planSha256"])
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(finished["receiptsWritten"], ["privateIngress", "privateIngressBackend"])
            self.assertEqual(azure.scoped.call_count, 2)
            self.assertFalse(finished["inferenceVerified"])

    def test_stale_approval_performs_zero_receipt_writes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            (path / "inputs").mkdir()
            with self.assertRaisesRegex(MigrationError, "no receipt writes"):
                self.run_refresh(path, "execute", "0" * 64)
            state = json.loads((path / "evidence-state.json").read_text())
            self.assertEqual(state["receiptsAttempted"], [])
            self.assertEqual(state["status"], "failed")

    def test_partial_receipt_failure_explicitly_records_first_write(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            (path / "inputs").mkdir()
            result, _ = self.run_refresh(path, "plan")
            with self.assertRaisesRegex(MigrationError, "permission"):
                self.run_refresh(path, "execute", result["planSha256"], fail_second=True)
            state = json.loads((path / "evidence-state.json").read_text())
            self.assertEqual(state["receiptsWritten"], ["privateIngress"])
            self.assertEqual(state["receiptsAttempted"], ["privateIngress", "privateIngressBackend"])
            self.assertEqual(state["status"], "failed")

    def test_receipt_drift_blocks_writes_after_approval(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            (path / "inputs").mkdir()
            result, _ = self.run_refresh(path, "plan")
            with self.assertRaisesRegex(MigrationError, "receipt drifted"):
                self.run_refresh(path, "execute", result["planSha256"], drift=True)
            self.assertEqual(json.loads((path / "evidence-state.json").read_text())["receiptsAttempted"], [])

    def test_exact_noop_revalidates_but_does_not_write_receipts(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination:
            path = Path(destination)
            (path / "inputs").mkdir()
            result, _ = self.run_refresh(path, "plan", no_op=True)
            finished, azure = self.run_refresh(path, "execute", result["planSha256"], no_op=True)
            self.assertTrue(finished["noOp"])
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(finished["receiptsWritten"], [])
            azure.scoped.assert_not_called()

    def test_workload_drift_cannot_be_certified_by_refresh(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "temp") as destination, \
                patch.object(evidence.runtime, "baseline", return_value={"changed": True}), \
                self.assertRaisesRegex(MigrationError, "Backend changed"):
            evidence.unchanged_workloads(customer(), {"identity": {"identity": {}}, "backend": {"expected": True}},
                                         ["kubectl"], Path(destination))


if __name__ == "__main__":
    unittest.main()
