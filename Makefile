SHELL := /usr/bin/env bash

.PHONY: validate validate-stage4 validate-stage5 validate-stage6 validate-stage7 validate-stage8 validate-stage9 validate-oss-callbacks test bicep kustomize

validate:
	bash scripts/validate-stage3.sh

validate-stage4:
	bash scripts/validate-stage4.sh

validate-stage5:
	bash scripts/validate-stage5.sh

validate-stage6:
	bash scripts/validate-stage6.sh

validate-stage7:
	bash scripts/validate-stage7.sh

validate-stage8:
	bash scripts/validate-stage8.sh

validate-stage9:
	bash scripts/validate-stage9.sh

validate-oss-callbacks:
	./.venv/bin/python -m unittest tests.test_litellm_audit_envelope
	bash scripts/validate-oss-callbacks.sh

test:
	./.venv/bin/python -m unittest tests.test_litellm_subscription tests.test_config_templates

bicep:
	LITELLM_ACR_SUFFIX=stage3check bash -c 'set -e; for file in $$(find infra -name "*.bicep" -type f | sort); do az bicep build --file "$$file" --stdout >/dev/null; done; for file in $$(find infra/environments -name "*.bicepparam" -type f | sort); do az bicep build-params --file "$$file" --stdout >/dev/null; done'

kustomize:
	@for environment in dev test prod; do kubectl kustomize "deploy/overlays/$$environment" >/dev/null; done
	@kubectl kustomize deploy/validation/stage6 >/dev/null
	@kubectl kustomize deploy/validation/stage7 >/dev/null
	@kubectl kustomize deploy/validation/stage8 >/dev/null
	@kubectl kustomize deploy/validation/stage9 >/dev/null
