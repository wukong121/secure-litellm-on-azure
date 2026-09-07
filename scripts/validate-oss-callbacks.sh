#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
image='docker.litellm.ai/berriai/litellm@sha256:20b5044b619055374061a6d5b7b08754cad75aeabbf82ddf4f69cc0cf80ddaf4'

for probe in test_oss_audit_adapter.py test_oss_callback_sdk.py test_oss_callback_proxy.py; do
  docker run --rm --network none --read-only --user 10001:10001 \
    --cap-drop ALL --security-opt no-new-privileges \
    --memory 2g --cpus 2 --pids-limit 256 \
    --tmpfs /tmp:rw,nosuid,size=256m \
    -e HOME=/tmp -e LITELLM_LOCAL_MODEL_COST_MAP=true \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/repo:/spike \
    -v "$repo_root/LiteLLM/observability:/repo/LiteLLM/observability:ro" \
    -v "$repo_root/tests/spikes:/spike:ro" \
    --entrypoint python "$image" "/spike/$probe"
done