#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON_BIN:-$repo_root/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  python_bin="$(command -v python3)"
fi

for tool in az kubectl; do
  command -v "$tool" >/dev/null || {
    echo "Required tool not found: $tool" >&2
    exit 1
  }
done

export LITELLM_ACR_SUFFIX="${LITELLM_ACR_SUFFIX:-stage3check}"
output_dir="$(mktemp -d)"
trap 'rm -rf "$output_dir"' EXIT

echo "[1/6] Python unit tests"
"$python_bin" -m unittest \
  tests.test_litellm_subscription \
  tests.test_config_templates

echo "[2/6] Bicep templates"
while IFS= read -r file; do
  az bicep build --file "$file" --outfile "$output_dir/$(echo "$file" | tr '/.' '__').json" >/dev/null
done < <(find infra -name '*.bicep' -type f | sort)

echo "[3/6] Bicep parameter files"
while IFS= read -r file; do
  az bicep build-params --file "$file" --outfile "$output_dir/$(echo "$file" | tr '/.' '__').json" >/dev/null
done < <(find infra/environments -name '*.bicepparam' -type f | sort)

echo "[4/6] Kustomize overlays"
for environment in dev test prod; do
  kubectl kustomize "deploy/overlays/$environment" > "$output_dir/$environment.yaml"
done

echo "[5/6] Manifest security policy"
"$python_bin" scripts/validate_manifests.py "$output_dir/dev.yaml" "$output_dir/test.yaml" "$output_dir/prod.yaml"

echo "[6/6] Repository policy"
if grep -RInE 'Microsoft\.ApiManagement|azure-mgmt-apimanagement|az[[:space:]]+apim' infra deploy .github 2>/dev/null \
  || grep -RInE --exclude='validate-stage3.sh' --exclude='validate-stage4.sh' --exclude='validate-stage5.sh' --exclude='validate-stage6.sh' 'Microsoft\.ApiManagement|azure-mgmt-apimanagement|az[[:space:]]+apim' scripts 2>/dev/null; then
  echo "Stage 3 assets must not create or manage APIM." >&2
  exit 1
fi
if grep -RInE '(sk-[A-Za-z0-9_-]{20,}|AccountKey=|postgresql://[^[:space:]]+:[^[:space:]]+@)' infra deploy .github 2>/dev/null \
  || grep -RInE --exclude='validate-stage3.sh' --exclude='validate-stage5.sh' --exclude='validate-stage6.sh' --exclude='validate_public_config.py' '(sk-[A-Za-z0-9_-]{20,}|AccountKey=|postgresql://[^[:space:]]+:[^[:space:]]+@)' scripts 2>/dev/null; then
  echo "Potential credential found in Stage 3 assets." >&2
  exit 1
fi

"$python_bin" -m scripts.validate_public_config

echo "Stage 3 validation passed."
