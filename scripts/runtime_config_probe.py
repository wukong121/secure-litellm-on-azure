"""Read-only, credential-redacted checks executed inside an existing backend Pod."""

import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError("Runtime API redirects are prohibited")


def admin_facts(document, user_id, bootstrap_ids):
    if not isinstance(document, dict):
        raise ValueError("Independent admin response must be an object")
    email = document.get("user_email")
    return {
        "userIdMatches": document.get("user_id") == user_id,
        "proxyAdmin": document.get("user_role") == "proxy_admin",
        "emailLoginIdentityPresent": isinstance(email, str) and bool(email.strip())
        and "@" in email and not email.startswith("@") and not email.endswith("@"),
        "independentIdentity": user_id not in bootstrap_ids,
    }


def probe(user_id=""):
    key = Path("/mnt/backend-secrets/LITELLM_MASTER_KEY").read_text(encoding="utf-8").strip()
    if not key or "\x00" in key or len(key) > 25000:
        raise ValueError("Backend authentication file is invalid")
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def get(path):
        request = Request("http://127.0.0.1:4000" + path,
                          headers={"Authorization": "Bearer " + key})
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    details = get("/health/readiness/details")
    if not isinstance(details, dict) or type(details.get("show_env_credential_login_warning")) is not bool:
        raise ValueError("Readiness details lack the effective environment-login setting")
    result = {"environmentLoginEnabled": details["show_env_credential_login_warning"]}
    if user_id:
        account = get("/v2/user/info?" + urlencode({"user_id": user_id}))
        bootstrap_ids = {"litellm-proxy-admin", os.environ.get("PROXY_ADMIN_ID"),
                         os.environ.get("LITELLM_PROXY_ADMIN_NAME")}
        result["admin"] = admin_facts(account, user_id, bootstrap_ids)
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(probe(sys.argv[1] if len(sys.argv) > 1 else ""), sort_keys=True))
    except (HTTPError, URLError, OSError, ValueError, TimeoutError):
        sys.exit("Runtime read-only API probe failed; credentials and response bodies are withheld")
