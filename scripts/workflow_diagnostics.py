"""Produce bounded diagnostics that are safe to include in workflow logs."""

import json
import os
from pathlib import Path
import re
import traceback


DIAGNOSTIC_PREFIX = "LLMGW_WORKFLOW_DIAGNOSTIC="
ROOT = Path(__file__).resolve().parents[1]
MAX_MESSAGE = 800


def _string_values(value):
    pending = [value]
    inspected = 0
    while pending and inspected < 4096:
        item = pending.pop()
        inspected += 1
        if isinstance(item, dict):
            pending.extend(list(item.values())[:4096 - inspected])
        elif isinstance(item, list):
            pending.extend(item[:4096 - inspected])
        elif isinstance(item, str) and 6 <= len(item) <= 1024:
            yield item


def _sensitive_values():
    values = set()
    for name, value in os.environ.items():
        if 8 <= len(value) <= 4096 and re.search(r"SECRET|TOKEN|PASSWORD|CREDENTIAL|PRIVATE_KEY|ARTIFACT_KEY", name, re.I):
            values.add(value)
        if name.endswith("_JSON") and len(value) <= 2 * 1024 * 1024:
            try:
                values.update(_string_values(json.loads(value)))
            except (ValueError, TypeError):
                pass
    return sorted(values, key=len, reverse=True)


def redact_message(value):
    raw = str(value or "")
    if len(raw) > 32768:
        raw = raw[:16384] + " ... " + raw[-16384:]
    message = " ".join(raw.split())
    for sensitive in _sensitive_values():
        message = re.sub(re.escape(sensitive), "<redacted>", message, flags=re.I)
    substitutions = (
        (r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 <redacted>"),
        (r"(?i)\b(password|passwd|token|secret|api[_-]?key|client[_-]?secret)\s*[=:]\s*[^\s,;]+", r"\1=<redacted>"),
        (r"https?://[^\s\]\[(){}<>]+", "<redacted-url>"),
        (r"/subscriptions/[A-Za-z0-9_./-]+", "<redacted-resource-id>"),
        (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b", "<redacted-id>"),
        (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<redacted-ip>"),
        (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "<redacted-email>"),
        (r"\b(?=[A-Za-z0-9.-]{4,253}\b)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\b(?!/)", "<redacted-host>"),
        (r"\b[0-9a-fA-F]{32,}\b", "<redacted-digest>"),
        (r"(?<![A-Za-z0-9])/(?:home|tmp|var|etc)/[^\s,;]+", "<redacted-path>"),
    )
    for pattern, replacement in substitutions:
        message = re.sub(pattern, replacement, message)
    message = re.sub(r"\s+", " ", message).strip()
    if not message:
        return "No safe error text was available; use the error code and source location."
    return message[:MAX_MESSAGE]


def _source_location(error, context):
    frames = traceback.extract_tb(error.__traceback__)
    for frame in reversed(frames):
        try:
            relative = Path(frame.filename).resolve().relative_to(ROOT)
        except ValueError:
            continue
        if relative.parts[0] != "scripts" or (relative.name == "customer_migration.py" and frame.name == "require"):
            continue
        function = re.sub(r"[^A-Za-z0-9_.<>-]", "", frame.name)[:80] or "module"
        return f"{relative.as_posix()}:{frame.lineno}:{function}"
    return context


def _error_code(error, message):
    lowered = message.lower()
    name = type(error).__name__
    if "returned no json" in lowered or "invalid json" in lowered:
        return "invalid-json-response"
    if "azure command failed" in lowered or re.match(r"az [a-z0-9-]+", lowered):
        return "azure-command-failed"
    if "plan changed" in lowered or "plan changed or was not approved" in lowered:
        return "plan-changed"
    if lowered.startswith("runner checks failed"):
        return "runner-checks-failed"
    if lowered.startswith("gateway isolation checks failed"):
        return "gateway-checks-failed"
    if "failed:" in lowered and "exit code" in lowered:
        return "command-failed"
    if name == "JSONDecodeError":
        return "invalid-json"
    if name == "KeyError":
        return "missing-required-field"
    if name in {"TypeError", "AttributeError"}:
        return "unexpected-data-shape"
    if name in {"YAMLError", "MarkedYAMLError", "ParserError", "ScannerError"}:
        return "invalid-yaml"
    if isinstance(error, OSError):
        return "operating-system-error"
    if name.endswith("SubprocessError"):
        return "command-execution-error"
    return "validation-failed" if name == "MigrationError" else "unexpected-error"


def _exception_message(error, fallback):
    name = type(error).__name__
    if name in {"YAMLError", "MarkedYAMLError", "ParserError", "ScannerError"}:
        mark = getattr(error, "problem_mark", None)
        if mark is not None and isinstance(getattr(mark, "line", None), int) and isinstance(getattr(mark, "column", None), int):
            return f"Invalid YAML at line {mark.line + 1}, column {mark.column + 1}"
        return "Invalid YAML input"
    if name == "KeyError":
        field = error.args[0] if len(error.args) == 1 else None
        if isinstance(field, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,79}", field):
            return f"Missing required field: {field}"
        return "A required field is missing"
    if isinstance(error, OSError):
        error_number = getattr(error, "errno", None)
        return f"{name} (errno {error_number})" if isinstance(error_number, int) else name
    if name in {"SubprocessError", "CalledProcessError", "TimeoutExpired"}:
        return f"{name}; command arguments and raw output remain private"
    return str(error).strip() or fallback


def exception_diagnostic(error, context, fallback="Operation failed"):
    raw = _exception_message(error, fallback)
    message = redact_message(raw)
    return {
        "version": 1,
        "context": context,
        "code": _error_code(error, message),
        "errorType": type(error).__name__,
        "message": message,
        "location": _source_location(error, context),
    }


def diagnostic_exit(error, context, fallback="Operation failed"):
    return DIAGNOSTIC_PREFIX + json.dumps(exception_diagnostic(error, context, fallback), sort_keys=True, separators=(",", ":"))


def format_diagnostic(diagnostic):
    return (f"Operation failed. Context: {diagnostic['context']}. Result category: {diagnostic['code']}.\n"
            f"Error: {diagnostic['message']}\nSource: {diagnostic['location']} ({diagnostic['errorType']}).")


def parse_diagnostic(stderr):
    for line in reversed(stderr.splitlines()):
        if not line.startswith(DIAGNOSTIC_PREFIX):
            continue
        try:
            value = json.loads(line.removeprefix(DIAGNOSTIC_PREFIX))
        except ValueError:
            continue
        if not isinstance(value, dict) or value.get("version") != 1:
            continue
        if not all(isinstance(value.get(key), str) for key in ("context", "code", "errorType", "message", "location")):
            continue
        if not re.fullmatch(r"[a-z0-9-]{1,80}", value["context"]) or not re.fullmatch(r"[a-z0-9-]{1,80}", value["code"]):
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,79}", value["errorType"]):
            continue
        if len(value["location"]) > 180 or not re.fullmatch(r"[A-Za-z0-9_.<>/:-]+", value["location"]):
            continue
        return {**value, "message": redact_message(value["message"])}
    return None


def command_name(arguments, limit=5):
    selected = []
    for index, value in enumerate(arguments):
        token = Path(value).name if index == 0 else value
        if token.startswith("-") or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,50}", token) is None:
            break
        selected.append(token)
        if len(selected) == limit:
            break
    return " ".join(selected) or "external command"


def command_failure_summary(stdout, stderr, returncode):
    raw = (stderr or "").strip() or (stdout or "").strip()
    if len(raw) > 65536:
        raw = raw[:32768] + " ... " + raw[-32768:]
    code = ""
    message = raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            failure = parsed.get("error", parsed)
            if isinstance(failure, dict):
                code = failure.get("code") if isinstance(failure.get("code"), str) else ""
                message = failure.get("message") if isinstance(failure.get("message"), str) else raw
    except (ValueError, TypeError):
        match = re.search(r"(?:ERROR:\s*)?\(([A-Za-z][A-Za-z0-9_.-]{1,79})\)", raw)
        code = match.group(1) if match else ""
    detail = redact_message(message) if raw else "The command returned no error text."
    if code and code.lower() not in detail.lower():
        detail = f"{code}: {detail}"
    return f"exit code {returncode}; {detail}"