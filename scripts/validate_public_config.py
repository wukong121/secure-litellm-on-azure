#!/usr/bin/env python3
"""Check publishable files without reading ignored customer configuration."""

from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
AZURE_SCOPE = re.compile(r"/subscriptions/([0-9a-f-]{36})", re.I)
RULES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "storage-key": re.compile(r"AccountKey=[A-Za-z0-9+/]{30,}={0,2}"),
    "api-key": re.compile(r"\bsk-[A-Za-z0-9_-]{24,}"),
    "personal-home-path": re.compile(r"/(?:home|Users)/[a-zA-Z][a-zA-Z0-9_-]+/"),
}


def violations(text, synthetic_tests=False):
    results = set()
    for match in EMAIL.finditer(text):
        domain = match[1].lower()
        if domain not in {"example.com", "example.net", "example.org", "contoso.com"} and not domain.endswith((".invalid", ".example.com", ".example.net", ".example.org")):
            results.add("non-example-email")
    for match in AZURE_SCOPE.finditer(text):
        synthetic = synthetic_tests and match[1] == "11111111-1111-4111-8111-111111111111"
        if not synthetic and len(set(match[1].replace("-", ""))) > 1:
            results.add("literal-subscription-id")
    for name, pattern in RULES.items():
        if pattern.search(text):
            results.add(name)
    return results


def main():
    names = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    problems = []
    for name in sorted(set(names) - {""}):
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for rule in sorted(violations(text, synthetic_tests=name.startswith(("tests/", "auth-proxy/test/")))):
            problems.append(f"{name}: {rule}")
    if problems:
        raise SystemExit("Public configuration checks failed (values suppressed):\n" + "\n".join(problems))
    print("Public configuration checks passed; ignored customer files were not read.")


if __name__ == "__main__":
    main()