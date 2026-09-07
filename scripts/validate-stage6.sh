#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON_BIN:-$repo_root/.venv/bin/python}"
[[ -x "$python_bin" ]] || python_bin="$(command -v python3)"

output_dir="$(mktemp -d)"
trap 'rm -rf "$output_dir"' EXIT

export LITELLM_ACR_SUFFIX="${LITELLM_ACR_SUFFIX:-stage6check}"

echo "[1/5] Stage 5 regression"
bash scripts/validate-stage5.sh

echo "[2/5] LiteLLM OSS product boundary"
"$python_bin" scripts/validate_product_boundary.py

echo "[3/5] Stage 6 render-only target"
kubectl kustomize deploy/validation/stage6 >"$output_dir/stage6.yaml"

echo "[4/5] Stage 6 HA and routing safety"
"$python_bin" scripts/validate_stage6.py "$output_dir/stage6.yaml"

echo "[5/5] Stage 6 repository policy"
if grep -RInE 'usage-based-routing-v2|routing_strategy_args' deploy/components/stage6-ha 2>/dev/null; then
  echo "Unapproved Stage 6 capacity routing found; keep simple-shuffle until A/B gates pass." >&2
  exit 1
fi
if grep -RInE 'postgres(ql)?://[^<[:space:]]+:[^<[:space:]]+@|rediss?://[^<[:space:]]+@|sk-[A-Za-z0-9_-]{16,}' deploy/components/stage6-ha deploy/validation/stage6 2>/dev/null; then
  echo "Stage 6 committed assets contain a credential or connection string." >&2
  exit 1
fi

echo "Stage 6 validation passed."
