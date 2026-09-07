#!/usr/bin/env python3
"""Reject LiteLLM Enterprise-only configuration from deployable assets."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOYABLE_ROOTS = (ROOT / "deploy", ROOT / "infra", ROOT / "auth-proxy", ROOT / "LiteLLM/observability")
TEXT_SUFFIXES = {".yaml", ".yml", ".json", ".bicep", ".bicepparam", ".py", ".mjs"}
FORBIDDEN_CONFIG = {
    "enable_jwt_auth": "LiteLLM native JWT authentication is Enterprise-only",
    "litellm_jwtauth": "LiteLLM native JWT configuration is Enterprise-only",
    "enforce_rbac": "LiteLLM native JWT/RBAC enforcement is Enterprise-only",
}
REQUIRED_POLICY_DOCS = (
    ROOT / "docs/litellm-security-hardening-implementation-roadmap-zh.md",
    ROOT / "docs/litellm-stage2-decisions-and-spikes-2026-09-02.md",
)


def validate_deployable_assets() -> None:
    violations: list[str] = []
    for directory in DEPLOYABLE_ROOTS:
        for path in directory.rglob("*"):
            if "node_modules" in path.parts:
                continue
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8").lower()
            for marker, reason in FORBIDDEN_CONFIG.items():
                if marker in text:
                    violations.append(f"{path.relative_to(ROOT)}: {marker}: {reason}")
    assert not violations, "\n".join(violations)


def validate_policy_decision() -> None:
    for path in REQUIRED_POLICY_DOCS:
        text = path.read_text(encoding="utf-8")
        assert "客户自有" in text and "认证代理" in text, f"{path.name}: missing customer-owned auth proxy decision"
        assert "不采购" in text and "LiteLLM Enterprise" in text, f"{path.name}: missing Enterprise exclusion"


if __name__ == "__main__":
    validate_deployable_assets()
    validate_policy_decision()
    print("LiteLLM OSS product-boundary checks passed.")
