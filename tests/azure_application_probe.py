"""Isolated-container application probe with synthetic credentials only."""

import argparse
import asyncio
import os
import time
import hashlib
import json
import jwt
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit, urlunsplit

from LiteLLM.runtime.application import create_application
from LiteLLM.runtime.azure_postgresql import AzureDatabaseTokens
from tests.test_azure_postgresql import TEMPLATE
from scripts.proxy_credentials import binding_contract, credential_bindings, key_payload, user_payload, validate_key, validate_user
from tests.test_proxy_config import proxy_customer


def probe_application(config, password):
    credential = Mock()
    credential.get_token.return_value = SimpleNamespace(token=password, expires_on=int(time.time()) + 3600)
    os.environ["LLMGW_BACKEND_SECRETS_DIR"] = str(Path(config).parent / "backend")
    os.environ.pop("LITELLM_MASTER_KEY", None)
    os.environ.pop("LITELLM_SALT_KEY", None)
    os.environ.pop("LLMGW_DATABASE_TOKEN_FILE", None)
    os.environ.pop("UI_USERNAME", None)
    os.environ.pop("UI_PASSWORD", None)
    os.environ["LLMGW_GATEWAY_AUTH_MODE"] = "native"
    os.environ["LLMGW_NATIVE_ADMIN_USERNAME"] = "gateway-admin"
    tokens = AzureDatabaseTokens(TEMPLATE, credential)
    app = create_application(config, TEMPLATE, tokens)
    return app, tokens, credential


async def probe(config):
    app, tokens, credential = probe_application(config, "synthetic-application-password")
    assert os.environ["LITELLM_MASTER_KEY"] == "synthetic-application-master-key"
    assert os.environ["LITELLM_SALT_KEY"] == "synthetic-application-salt-key"
    from litellm.proxy import proxy_server

    async with app.router.lifespan_context(app):
        client = proxy_server.prisma_client
        assert client is not None
        rows = await client.db.query_raw("SELECT current_user AS name")
        assert rows[0]["name"] == "llmgw_app"
        await client.check_view_exists()
        assert client.db._token_refresh_task is not None
        rights = await client.db.query_raw("SELECT has_schema_privilege(current_user, 'public', 'CREATE') AS allowed")
        assert rights[0]["allowed"] is False
        import httpx

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated.invalid", headers={"Authorization": "Bearer synthetic-application-master-key"}) as http:
            invalid_login = await http.post("/v2/login", json={"username": "gateway-admin", "password": "wrong-password"})
            assert invalid_login.status_code in {401, 403}, "Invalid native admin password was accepted"
            login = await http.post("/v2/login", json={"username": "gateway-admin", "password": "synthetic-ui-password"})
            assert login.status_code == 200, "Native admin login failed: " + login.text
            ui_token = login.json().get("token")
            assert isinstance(ui_token, str) and ui_token.count(".") == 2, "Native admin login did not issue a UI session token"
            ui_claims = jwt.decode(ui_token, "synthetic-application-master-key", algorithms=["HS256"])
            ui_key = ui_claims.get("key")
            assert isinstance(ui_key, str) and ui_key.startswith("sk-"), "Native UI session did not contain a bounded virtual key"
            native_key = "sk-" + hashlib.sha256(b"synthetic-native-login-key").hexdigest()
            native_created = await http.post("/key/generate", headers={"Authorization": "Bearer " + ui_key}, json={"models": ["coding"], "key": native_key})
            assert native_created.status_code == 200, "Native admin UI token could not create a virtual key: " + native_created.text
            native_info = await http.get("/key/info", params={"key": hashlib.sha256(native_key.encode()).hexdigest()})
            assert native_info.status_code == 200, "Native login virtual key was not persisted"
            config_fixture = proxy_customer()
            config_fixture["proxy"]["bindings"].append({"oid": "99999999-9999-4999-8999-999999999999", "plane": "admin", "role": "proxy_admin", "models": ["coding"]})
            for binding in credential_bindings(config_fixture):
                contract = binding_contract(config_fixture, binding)
                value = "sk-" + hashlib.sha256(contract["userId"].encode()).hexdigest()
                response = await http.post("/user/new", json=user_payload(contract))
                assert response.status_code == 200, "Synthetic user creation failed: " + response.text
                user = await http.get("/user/info", params={"user_id": contract["userId"]})
                assert user.status_code == 200
                validate_user(contract, user.json()["user_info"])
                response = await http.post("/key/generate", json=key_payload(contract, value))
                assert response.status_code == 200, "Synthetic key creation failed: " + response.text
                info = await http.get("/key/info", params={"key": hashlib.sha256(value.encode()).hexdigest()})
                assert info.status_code == 200
                validate_key(contract, info.json()["info"], value)
                denied = await http.post("/key/generate", json={"models": ["coding"]}, headers={"Authorization": "Bearer " + value})
                assert denied.status_code in {401, 403}, "Per-subject key unexpectedly granted credential creation"
            subject = hashlib.sha256(b"synthetic-tenant:synthetic-actor").hexdigest()
            api_key = "sk-" + hashlib.sha256(b"synthetic-native-audit-key").hexdigest()
            created = await http.post("/user/new", json={"user_id": "synthetic-native-user", "user_role": "internal_user", "models": ["coding"]})
            assert created.status_code == 200, "Native probe user creation failed"
            created = await http.post("/key/generate", json={"user_id": "synthetic-native-user", "models": ["coding"], "key": api_key})
            assert created.status_code == 200, "Native probe key creation failed"
            for streaming in (False, True):
                response = await http.post("/v1/chat/completions", headers={"Authorization": "Bearer " + api_key}, json={"model": "coding", "messages": [{"role": "user", "content": "SYNTHETIC_NATIVE_PROMPT"}], "user": subject, "stream": streaming})
                assert response.status_code == 200, "Native audit synthetic inference failed: " + response.text
                if streaming:
                    assert "[DONE]" in response.text
                else:
                    assert response.json()["choices"][0]["message"]["content"] == "SYNTHETIC_NATIVE_RESPONSE"
            from litellm.proxy.utils import update_spend_logs_job

            deadline = time.monotonic() + 20
            while True:
                await update_spend_logs_job(client, None, proxy_server.proxy_logging_obj)
                logs = await client.db.query_raw('SELECT request_id, api_key, end_user, messages, proxy_server_request, response FROM "LiteLLM_SpendLogs" WHERE end_user = $1', subject)
                if len(logs) == 2:
                    break
                assert time.monotonic() < deadline, "Native JSON/SSE spend logs were not written"
                await asyncio.sleep(0.2)
            for log in logs:
                assert log["api_key"] == hashlib.sha256(api_key.encode()).hexdigest()
                assert "SYNTHETIC_NATIVE_PROMPT" in json.dumps(log["proxy_server_request"]), "Synthetic stored request: " + repr(log["proxy_server_request"])
                assert "SYNTHETIC_NATIVE_RESPONSE" in json.dumps(log["response"]), "Synthetic stored response: " + repr(log["response"])
                assert api_key not in json.dumps(log)
                assert "synthetic-model-key" not in json.dumps(log)
            native_config = proxy_customer()
            native_config["proxy"]["nativeUi"] = True
            reader = {"oid": "12121212-1212-4212-8212-121212121212", "plane": "admin", "role": "proxy_admin_viewer", "models": ["coding"], "nativeAuditRead": True}
            native_config["proxy"]["bindings"].append(reader)
            contract = binding_contract(native_config, reader)
            reader_key = "sk-" + hashlib.sha256(b"synthetic-native-reader").hexdigest()
            assert (await http.post("/user/new", json=user_payload(contract))).status_code == 200
            assert (await http.post("/key/generate", json=key_payload(contract, reader_key))).status_code == 200
            read_headers = {"Authorization": "Bearer " + reader_key}
            today = datetime.now(timezone.utc)
            listing = await http.get("/spend/logs/ui", params={"page": 1, "page_size": 50, "start_date": (today - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), "end_date": (today + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")}, headers=read_headers)
            assert listing.status_code == 200, "Native log list failed: " + listing.text
            for log in logs:
                detail = await http.get("/spend/logs/ui/" + log["request_id"], headers=read_headers)
                assert detail.status_code == 200, "Native log detail failed: " + detail.text
                assert "SYNTHETIC_NATIVE_PROMPT" in detail.text and "SYNTHETIC_NATIVE_RESPONSE" in detail.text
                assert api_key not in detail.text and reader_key not in detail.text
            denied = await http.post("/key/generate", json={"models": ["coding"]}, headers=read_headers)
            assert denied.status_code in {401, 403}, "Native viewer unexpectedly created credentials"
        from prisma import Prisma

        parsed = urlsplit(TEMPLATE)
        admin_url = urlunsplit((parsed.scheme, "postgres:synthetic-admin-password" + "@" + parsed.hostname + ":5432", parsed.path, parsed.query, ""))
        admin = Prisma(datasource={"url": admin_url})
        try:
            await admin.connect()
            async with client.db.tx() as transaction:
                await transaction.query_raw("SELECT 1 AS value")
                await admin.execute_raw("ALTER ROLE llmgw_app PASSWORD 'synthetic-rotated-password'")
                credential.get_token.return_value = SimpleNamespace(token="synthetic-rotated-password", expires_on=int(time.time()) + 3600)
                tokens._expirations[hashlib.sha256(b"synthetic-application-password").hexdigest()] = int(time.time()) + 60
                previous = client.db.engine_generation
                await client.db._safe_refresh_token()
                assert client.db.engine_generation == previous + 1
                assert (await transaction.query_raw("SELECT 2 AS value"))[0]["value"] == 2
                assert (await client.db.query_raw("SELECT current_user AS name"))[0]["name"] == "llmgw_app"
        finally:
            if admin.is_connected():
                await admin.disconnect()
    print("Azure application entry point verified with synthetic TLS database credentials; no Azure identity claims tested.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if args.serve:
        import uvicorn
        app, _, _ = probe_application(args.config, "synthetic-rotated-password")
        uvicorn.run(app, host="0.0.0.0", port=4000, access_log=False)
    else:
        asyncio.run(probe(args.config))