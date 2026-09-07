#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON_BIN:-$repo_root/.venv/bin/python}"
[[ -x "$python_bin" ]] || python_bin="$(command -v python3)"

output_dir="$(mktemp -d)"
trap 'rm -rf "$output_dir"' EXIT

export LITELLM_ACR_SUFFIX="${LITELLM_ACR_SUFFIX:-stage5check}"

echo "[1/6] Stage 4 regression"
bash scripts/validate-stage4.sh

echo "[2/6] Stage 5 static safety"
"$python_bin" scripts/validate_stage5.py

echo "[3/6] Stage 5 Bicep modules"
for file in \
  infra/modules/key-vault/main.bicep \
  infra/modules/postgresql-flexible-server/main.bicep \
  infra/modules/managed-redis/main.bicep \
  infra/modules/key-vault-private-endpoint/main.bicep \
  infra/modules/postgresql-private-endpoint/main.bicep \
  infra/modules/redis-private-endpoint/main.bicep; do
  az bicep build --file "$file" --outfile "$output_dir/$(echo "$file" | tr '/.' '__').json" >/dev/null
done

echo "[4/6] Environment templates"
az bicep build --file infra/environments/main.bicep --outfile "$output_dir/environment.json" >/dev/null
for file in infra/environments/{dev,test,prod}/main.bicepparam; do
  az bicep build-params --file "$file" --outfile "$output_dir/$(echo "$file" | tr '/.' '__').json" >/dev/null
done

echo "[5/6] Stage 5 render-only target"
kubectl kustomize deploy/validation/stage5 >"$output_dir/stage5.yaml"
grep -q 'kind: SecretProviderClass' "$output_dir/stage5.yaml"
grep -q 'driver: secrets-store.csi.k8s.io' "$output_dir/stage5.yaml"
grep -q 'azure_redis_ad_token: true' "$output_dir/stage5.yaml"

echo "[6/6] Stage 5 repository policy"
if grep -RInE 'Microsoft\.ApiManagement|azure-mgmt-apimanagement|az[[:space:]]+apim' infra/modules infra/environments deploy/components/stage5-data 2>/dev/null; then
  echo "Stage 5 assets must not create or manage APIM." >&2
  exit 1
fi
if grep -RInE 'postgres(ql)?://[^<[:space:]]+:[^<[:space:]]+@|rediss?://[^<[:space:]]+@|sk-[A-Za-z0-9_-]{16,}' infra/environments infra/modules deploy/components/stage5-data 2>/dev/null; then
  echo "Stage 5 committed assets contain a credential or connection string." >&2
  exit 1
fi

echo "Stage 5 validation passed."
