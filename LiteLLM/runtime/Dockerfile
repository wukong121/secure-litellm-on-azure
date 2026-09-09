FROM docker.litellm.ai/berriai/litellm@sha256:20b5044b619055374061a6d5b7b08754cad75aeabbf82ddf4f69cc0cf80ddaf4

COPY --chown=10001:10001 LiteLLM/runtime /opt/llmgw/LiteLLM/runtime
ENV PYTHONPATH=/opt/llmgw \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    LITELLM_LOCAL_MODEL_COST_MAP=True
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/python", "-m", "LiteLLM.runtime.application"]
CMD ["--config", "/app/config/config.yaml"]