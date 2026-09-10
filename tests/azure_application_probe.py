"""Isolated-container application probe with synthetic credentials only."""

import argparse
import asyncio
import os
import time
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit, urlunsplit

from LiteLLM.runtime.application import create_application
from LiteLLM.runtime.azure_postgresql import AzureDatabaseTokens
from tests.test_azure_postgresql import TEMPLATE
from scripts.proxy_credentials import binding_contract, credential_bindings, key_payload, user_payload, validate_key, validate_user
from tests.test_proxy_config import proxy_customer


async def probe(config):
    credential = Mock()
    credential.get_token.return_value = SimpleNamespace(token="synthetic-application-password", expires_on=int(time.time()) + 3600)
    os.environ["LLMGW_BACKEND_SECRETS_DIR"] = str(Path(config).parent / "backend")
    os.environ.pop("LITELLM_MASTER_KEY", None)
    os.environ.pop("LITELLM_SALT_KEY", None)
    os.environ.pop("LLMGW_DATABASE_TOKEN_FILE", None)
    tokens = AzureDatabaseTokens(TEMPLATE, credential)
    app = create_application(config, TEMPLATE, tokens)
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
    asyncio.run(probe(parser.parse_args().config))