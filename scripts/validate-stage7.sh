#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python_bin="${PYTHON_BIN:-$repo_root/.venv/bin/python}"
[[ -x "$python_bin" ]] || python_bin="$(command -v python3)"
node -e 'if (Number(process.versions.node.split(".")[0]) !== 24) throw new Error("Stage 7 requires Node 24")'

echo "[1/4] Stage 6 regression"
bash scripts/validate-stage6.sh
echo "[2/4] Auth proxy tests"
npm test --prefix auth-proxy
echo "[3/4] Stage 7 manifest checks"
output_dir="$(mktemp -d)"
trap 'rm -rf "$output_dir"' EXIT
kubectl kustomize deploy/validation/stage7 > "$output_dir/stage7.yaml"
"$python_bin" scripts/validate_stage7.py "$output_dir/stage7.yaml"
"$python_bin" -m unittest tests.test_stage7_manifests tests.test_stage7_domain
echo "[4/4] Product boundary and shell syntax"
"$python_bin" scripts/validate_product_boundary.py
bash -n scripts/validate-stage7.sh
echo "Stage 7 offline checks passed; no deployment or tenant validation was performed."