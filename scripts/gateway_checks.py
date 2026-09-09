"""Bounded live negative probes; proves only the named observations, not Stage7 acceptance."""

from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl

from scripts.customer_migration import ROOT, private_write, require, stage_fingerprint, validate_config


def resolve_addresses(host):
    return sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


def private_address(address):
    selected = ipaddress.ip_address(address)
    ranges = [ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]
    return any(selected.version == network.version and selected in network for network in ranges)


def probe(host, address, path, host_header, method="POST"):
    context = ssl.create_default_context()
    with socket.create_connection((address, 443), timeout=10) as connection:
        with context.wrap_socket(connection, server_hostname=host) as secured:
            certificate = hashlib.sha256(secured.getpeercert(binary_form=True)).hexdigest()
            body = b'{}' if method == "POST" else b''
            request = f"{method} {path} HTTP/1.1\r\nHost: {host_header}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode("ascii") + body
            secured.sendall(request)
            response = http.client.HTTPResponse(secured)
            response.begin()
            headers = {key.lower(): value for key, value in response.getheaders()}
            require("set-cookie" not in headers, "Unauthenticated probe unexpectedly received a cookie")
            return {"status": response.status, "certificateSha256": certificate}


def gateway_checks(config, revision, resolve=resolve_addresses, request=probe):
    require(len(revision) == 40 and all(character in "0123456789abcdef" for character in revision), "Reviewed revision required")
    results = {}
    api = "llm-api." + config["baseDomain"]
    admin = "llm-admin." + config["baseDomain"]
    for name, check in (
        ("api_unauthenticated_denied", (api, "/v1/responses", api, {401, 403}, False)),
        ("api_wrong_host_denied", (api, "/v1/responses", "untrusted.synthetic.invalid", {400, 403, 404, 421}, False)),
        ("admin_private_and_unauthenticated_denied", (admin, "/model/info", admin, {401, 403}, True)),
    ):
        host, path, host_header, statuses, private = check
        try:
            addresses = resolve(host)
            require(addresses and len(addresses) <= 16, "Missing or unbounded DNS address set")
            require(not private or all(private_address(address) for address in addresses), "Admin DNS includes a non-private address")
            observed = [request(host, address, path, host_header, "GET" if private else "POST") for address in addresses]
            require(all(item["status"] in statuses for item in observed), "Request was not denied as expected")
            results[name] = {"status": "passed", "observations": observed, "addressCount": len(addresses)}
        except Exception:
            results[name] = {"status": "failed", "reason": "DNS, TLS or expected denial check failed; no response body retained"}
    return {"revision": revision, "environment": config["environment"], "configSha256": stage_fingerprint(config, 7), "observedAt": datetime.now(timezone.utc).isoformat(), "checkGroup": "gateway-isolation", "checks": results, "stageAccepted": False,
            "notCovered": ["authenticated_client_compatibility", "object_ownership", "database_and_redis", "audit_delivery", "load_and_recovery", "conditional_access"]}


def main():
    config = validate_config(json.loads(os.environ["CUSTOMER_CONFIG_JSON"]), os.environ["CUSTOMER_ENVIRONMENT"])
    result = gateway_checks(config, os.environ["GITHUB_SHA"])
    directory = ROOT / "temp/gateway-checks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_write(directory / "gateway-isolation.json", json.dumps(result, indent=2))
    print(json.dumps({"checkGroup": result["checkGroup"], "checks": {name: check["status"] for name, check in result["checks"].items()}, "stageAccepted": False}))
    require(all(check["status"] == "passed" for check in result["checks"].values()), "Gateway isolation checks failed")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit("Gateway checks failed. No stage acceptance issued; no response bodies or credentials logged.") from None