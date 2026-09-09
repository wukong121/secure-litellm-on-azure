#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python_bin="${PYTHON_BIN:-$repo_root/.venv/bin/python}"
[[ -x "$python_bin" ]] || python_bin="$(command -v python3)"

echo "[1/4] Stage 7 regression"
bash scripts/validate-stage7.sh
echo "[2/4] Synthetic L3 lifecycle demonstration"
node auth-proxy/test/stage8-demo.mjs
echo "[3/4] Stage 8 rendered policies"
output_dir="$(mktemp -d)"
trap 'rm -rf "$output_dir"' EXIT
kubectl kustomize deploy/validation/stage8 > "$output_dir/stage8.yaml"
"$python_bin" scripts/validate_stage8.py "$output_dir/stage8.yaml"
"$python_bin" -m unittest tests.test_stage8_manifests
"$python_bin" -m unittest -b tests.test_audit_window tests.test_audit_runtime tests.test_audit_plan tests.test_audit_manifest tests.test_proxy_manifest
"$python_bin" -m unittest -b tests.test_observability tests.test_workflow_artifacts
echo "[4/4] Audit storage parameters and product boundary"
az bicep build-params --file infra/audit-storage/main.bicepparam --outfile "$output_dir/storage-parameters.json" >/dev/null
"$python_bin" scripts/validate_product_boundary.py
bash -n scripts/validate-stage8.sh
echo "Stage 8 offline gates passed. No production collection or cloud deployment was performed."