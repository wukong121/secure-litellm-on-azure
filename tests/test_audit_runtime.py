import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts.audit_runtime import AuditCluster, audit_operation, recovery_documents, install_recovery_support, prepare_recovery
from scripts.audit_window import assert_paused
from scripts.customer_migration import ROOT, fingerprint, stage_fingerprint
from scripts.migration_deploy import group_id
from tests.test_audit_window import WindowClient
from tests.test_customer_migration import customer_config


class AuditRuntimeTests(unittest.TestCase):
    def test_prepare_validates_live_federation_private_storage_and_cli_endpoint_shape(self):
        config = customer_config()
        config["parameters"]["audit"] = {"recoveryPrincipalId": "auto", "virtualNetworkName": "synthetic-vnet", "privateEndpointSubnetName": "snet-pe"}
        registry = config["parameters"]["platform"]["containerRegistryName"]
        config["proxy"] = {"image": registry + ".azurecr.io/proxy@sha256:" + "a" * 64}
        group = group_id(config)
        identity = {"id": group + "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/recovery", "name": "recovery", "clientId": "recovery-client", "principalId": "recovery-principal", "serviceAccountName": "l3-recovery", "kubernetesNamespace": "litellm"}
        storage_id = group + "/providers/Microsoft.Storage/storageAccounts/synthetic"
        storage_url = "https://synthetic.blob.core.windows.net"
        federation = [{"subject": "system:serviceaccount:litellm:l3-recovery", "issuer": "https://synthetic.invalid/issuer", "audiences": ["api://AzureADTokenExchange"]}]
        endpoint = {"subnet": {"id": group + "/providers/Microsoft.Network/virtualNetworks/synthetic-vnet/subnets/snet-pe"}, "privateLinkServiceConnections": [{"privateLinkServiceId": storage_id, "groupIds": ["blob"], "privateLinkServiceConnectionState": {"status": "Approved"}}]}
        class Azure:
            def scoped(self, command):
                if command[:2] == ["deployment", "group"]:
                    if "audit-foundation" in command[command.index("--name") + 1]:
                        return {"state": "Succeeded", "foundation": {"recovery": identity, **{role: {"principalId": role} for role in ("writer", "reader", "retention")}}}
                    return {"state": "Succeeded", "storageId": storage_id, "storageUrl": storage_url}
                if command[:2] == ["identity", "show"]:
                    return identity
                if command[:2] == ["identity", "federated-credential"]:
                    return federation
                if command[0] == "storage":
                    return {"publicNetworkAccess": "Disabled", "allowSharedKeyAccess": False, "primaryEndpoints": {"blob": storage_url}}
                if command[0] == "aks":
                    return {"apiServerAccessProfile": {"enablePrivateCluster": True}, "privateFqdn": "synthetic.invalid", "oidcIssuerProfile": {"issuerUrl": "https://synthetic.invalid/issuer"}}
                if command[:2] == ["network", "private-endpoint"]:
                    return [endpoint]
                return {"addressPrefix": "10.30.8.0/24"}
        client = WindowClient()
        client.assert_managed_writers = lambda: None
        for kind, name, template in (("deployment", "llm-api-proxy", ["template"]), ("cronjob", "l3-retention", ["jobTemplate", "spec", "template"])):
            pod = client.resources[(kind, name)]["spec"]
            for key in template:
                pod = pod[key]
            pod["spec"]["volumes"] = [{"name": "stage8", "configMap": {"name": "synthetic-stage8"}}]
        client.resources[("configmap", "synthetic-stage8")] = {"data": {"config.json": json.dumps({"l3": {"enabled": True, "deliveryMode": "persist-before-forward", "storageUrl": storage_url}})}}
        with tempfile.TemporaryDirectory() as directory, patch("scripts.audit_runtime.verify_runtime_image") as signature, patch("scripts.audit_runtime.socket.gethostbyname", return_value="10.30.0.10"):
            result = prepare_recovery(config, "a" * 40, Path(directory), Azure(), client)
            self.assertEqual(result["identity"], identity)
            self.assertEqual(result["subnet"], "10.30.8.0/24")
            signature.assert_called_once()
            endpoint["privateLinkServiceConnections"][0]["privateLinkServiceConnectionState"]["status"] = "Pending"
            with self.assertRaisesRegex(ValueError, "not approved"):
                prepare_recovery(config, "a" * 40, Path(directory), Azure(), client)
            federation[0]["subject"] = "system:serviceaccount:litellm:llm-api-proxy"
            with self.assertRaisesRegex(ValueError, "federation changed"):
                prepare_recovery(config, "a" * 40, Path(directory), Azure(), client)

    def test_python_and_worker_checkpoint_hashes_agree(self):
        document = {"phase": "paused", "resources": {"deployment": {"replicas": 0, "enabled": False}}, "scope": {"name": "synthetic", "label": chr(0x4e2d)}, "other": None}
        result = subprocess.run(["node", "--input-type=module", "-e", "import {stateHash} from './auth-proxy/audit-recovery-worker.mjs'; let data=''; for await (const chunk of process.stdin) data+=chunk; console.log(stateHash(JSON.parse(data)));"], cwd=ROOT, input=json.dumps(document), capture_output=True, text=True, check=True, timeout=30)
        self.assertEqual(result.stdout.strip(), fingerprint(document))

    @unittest.skipUnless(shutil.which("kubectl"), "kubectl required for local HTTP wire contract")
    def test_real_kubectl_delete_transmits_uid_precondition(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_DELETE(self):
                if self.headers.get("Transfer-Encoding") == "chunked":
                    chunks = []
                    while True:
                        length = int(self.rfile.readline().strip(), 16)
                        if not length:
                            self.rfile.readline()
                            break
                        chunks.append(self.rfile.read(length))
                        self.rfile.read(2)
                    body = b"".join(chunks)
                else:
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                received.append((self.path, json.loads(body)))
                payload = b'{"apiVersion":"v1","kind":"Status","status":"Success"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = AuditCluster(["kubectl", "--kubeconfig=/dev/null", f"--server=http://127.0.0.1:{server.server_port}", "--namespace=litellm"], Path(directory))
                client.delete("configmap", "llmgw-audit-recovery-window", "synthetic-uid")
            self.assertEqual(received[0][0].split("?")[0], "/api/v1/namespaces/litellm/configmaps/llmgw-audit-recovery-window")
            self.assertEqual(received[0][1]["preconditions"], {"uid": "synthetic-uid"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_recovery_runtime_binds_saved_plan_to_pause_and_never_auto_resumes(self):
        client = WindowClient()
        client.assert_managed_writers = lambda: None
        config = customer_config()
        revision = "a" * 40
        scope = {"tenantId": config["azure"]["tenantId"], "clusterId": group_id(config) + "/providers/Microsoft.ContainerService/managedClusters/" + config["parameters"]["platform"]["stage4Aks"]["name"], "configSha256": stage_fingerprint(config, 8)}
        context = {"scope": scope, "identity": {"clientId": "synthetic"}, "image": "synthetic.azurecr.io/proxy@sha256:" + "b" * 64, "storageUrl": "https://synthetic.blob.core.windows.net", "subnet": "10.30.8.0/24", "apiCidr": "10.30.0.10/32"}
        calls = []
        def job_runner(current, documents):
            self.assertIs(current, client)
            input_data = json.loads(documents[4]["data"]["input.json"])
            calls.append(input_data)
            self.assertEqual(assert_paused(client, scope), input_data["window"])
            return {"plan": {"entries": []}, "summary": {"action": "audit-recover", "stage": 8, "stageAccepted": False, "planSha256": "c" * 64, "applied": input_data["operation"] == "execute"}}
        with tempfile.TemporaryDirectory() as directory, patch("scripts.audit_runtime.prepare_recovery", side_effect=lambda *args: dict(context)):
            path = Path(directory)
            preview = audit_operation(config, "audit-pause", "plan", revision, path, "", client=client)
            audit_operation(config, "audit-pause", "execute", revision, path, preview["planSha256"], client=client)
            preview = audit_operation(config, "audit-recover", "plan", revision, path, "", client=client, job_runner=job_runner)
            reviewed = (path / "runtime-review.json").read_text()
            self.assertEqual(fingerprint(json.loads(reviewed)), preview["planSha256"])
            with patch.dict("os.environ", {"MIGRATION_AUDIT_PLAN_JSON": reviewed}):
                def failed_job(*args):
                    raise ValueError("synthetic recovery failure")
                with self.assertRaisesRegex(ValueError, "recovery failure"):
                    audit_operation(config, "audit-recover", "execute", revision, path, preview["planSha256"], client=client, job_runner=failed_job)
                self.assertEqual(client.get("deployment", "llm-api-proxy")["spec"]["replicas"], 0)
                assert_paused(client, scope)
                result = audit_operation(config, "audit-recover", "execute", revision, path, preview["planSha256"], client=client, job_runner=job_runner)
                self.assertTrue(result["applied"])
                self.assertEqual(result["phase"], "paused")
                self.assertEqual(calls[-1]["approved"], "c" * 64)
                context["image"] = "synthetic.azurecr.io/proxy@sha256:" + "d" * 64
                with self.assertRaisesRegex(ValueError, "changed"):
                    audit_operation(config, "audit-recover", "execute", revision, path, preview["planSha256"], client=client, job_runner=job_runner)
            self.assertEqual(client.get("deployment", "llm-api-proxy")["spec"]["replicas"], 0)

    def test_existing_support_accepts_only_known_server_defaults(self):
        documents = recovery_documents({"clientId": "synthetic"}, "synthetic.azurecr.io/proxy@sha256:" + "a" * 64, {}, "test", "10.30.8.0/24", "10.30.0.10/32")
        client = WindowClient()
        for document in documents[:4]:
            client.create(document)
        client.resources[("rolebinding", "l3-recovery-check")]["subjects"][0]["apiGroup"] = ""
        client.resources[("networkpolicy", "l3-recovery")]["spec"].pop("ingress")
        install_recovery_support(client, documents[:4])
        client.resources[("rolebinding", "l3-recovery-check")]["subjects"][0]["name"] = "other"
        with self.assertRaisesRegex(ValueError, "differs"):
            install_recovery_support(client, documents[:4])

    def test_runtime_pause_and_resume_are_approved_and_keep_separate_receipts(self):
        client = WindowClient()
        client.assert_managed_writers = lambda: None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = customer_config()
            preview = audit_operation(config, "audit-pause", "plan", "a" * 40, path, "", client=client)
            with self.assertRaisesRegex(ValueError, "approved"):
                audit_operation(config, "audit-pause", "execute", "a" * 40, path, "f" * 64, client=client)
            result = audit_operation(config, "audit-pause", "execute", "a" * 40, path, preview["planSha256"], client=client)
            self.assertEqual(result["phase"], "paused")
            self.assertFalse(result["stageAccepted"])
            preview = audit_operation(config, "audit-resume", "plan", "a" * 40, path, "", client=client)
            result = audit_operation(config, "audit-resume", "execute", "a" * 40, path, preview["planSha256"], client=client)
            self.assertEqual(result["phase"], "resumed")

    def test_recovery_job_has_dedicated_identity_no_secrets_and_read_only_cluster_permissions(self):
        documents = recovery_documents({"clientId": "synthetic"}, "synthetic.azurecr.io/proxy@sha256:" + "a" * 64, {"operation": "plan"}, "l3-recovery-test", "10.30.8.0/24", "10.30.0.10/32")
        role = next(item for item in documents if item["kind"] == "Role")
        self.assertTrue(all(set(rule["verbs"]) <= {"get", "list"} for rule in role["rules"]))
        self.assertNotIn("secrets", json.dumps(documents))
        pod = documents[-1]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "l3-recovery")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(documents[-1]["spec"]["backoffLimit"], 0)
        self.assertEqual(pod["containers"][0]["command"], ["node", "audit-recovery-worker.mjs"])
        with self.assertRaises(ValueError):
            recovery_documents({}, "untrusted:latest", {}, "test", "10.0.0.0/24", "10.0.0.1/32")

    def test_cluster_delete_uses_uid_preconditions_and_fixed_namespace(self):
        with tempfile.TemporaryDirectory() as directory, patch("scripts.audit_runtime.run_command", return_value="{}") as run:
            client = AuditCluster(["kubectl", "--namespace", "litellm"], Path(directory))
            client.delete("configmap", "llmgw-audit-recovery-window", "synthetic-uid")
            command = run.call_args.args[0]
            self.assertIn("/api/v1/namespaces/litellm/configmaps/llmgw-audit-recovery-window", command)
            options = json.loads(Path(command[command.index("-f") + 1]).read_text())
            self.assertEqual(options["preconditions"], {"uid": "synthetic-uid"})
            with self.assertRaises(ValueError):
                client.delete("secret", "unapproved", "uid")

    def test_terminating_recovery_pod_blocks_resume_even_if_job_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            client = AuditCluster([], Path(directory))
            client.items = lambda kind: [] if kind == "jobs" else [{"spec": {"serviceAccountName": "l3-recovery"}, "status": {"phase": "Running"}, "metadata": {"deletionTimestamp": "synthetic"}}]
            with self.assertRaisesRegex(ValueError, "still active"):
                client.assert_no_recovery_jobs()