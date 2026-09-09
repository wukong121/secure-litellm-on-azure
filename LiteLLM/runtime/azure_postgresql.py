"""Azure Workload Identity credentials for the reviewed Prisma database wrapper."""

import asyncio
import hashlib
import os
from pathlib import Path
import re
import threading
import time
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


PRISMA_WRAPPER_SHA256 = "12971b66c0c34f2490226802f5f957a693a7214c287a4fb54fa0d4acc08495de"
DATABASE_SETTINGS_SHA256 = "f892164f9183af557df89d37f35caa96c3e171109d4a57fc5d17b90feb267f47"
DATABASE_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


class DatabaseAuthError(ValueError):
    pass


def database_url_template(value):
    try:
        parsed = urlsplit(value)
        options = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        allowed = {"schema", "connection_limit", "pool_timeout", "connect_timeout", "socket_timeout", "sslmode", "sslaccept", "sslcert"}
        if (parsed.scheme != "postgresql" or not parsed.hostname
                or not re.fullmatch(r"[a-z0-9-]+\.postgres\.database\.azure\.com", parsed.hostname)
                or parsed.port != 5432 or parsed.username not in {"llmgw_app", "llmgw_migrator"}
                or parsed.password is not None or parsed.fragment
                or not re.fullmatch(r"/[a-z_][a-z0-9_]{0,62}", parsed.path)
                or len(dict(options)) != len(options) or set(dict(options)) - allowed):
            raise ValueError
        configured = dict(options)
        if configured.get("sslmode") != "require" or configured.get("sslaccept") != "strict" or configured.get("sslcert") != "/etc/ssl/certs/ca-certificates.crt":
            raise ValueError
        return parsed, urlencode(options)
    except (ValueError, TypeError, AttributeError):
        raise DatabaseAuthError("Azure database URL must select an approved role, server, database and strict TLS without a stored password") from None


class AzureDatabaseTokens:
    def __init__(self, template, credential, clock=time.time):
        self.parsed, self.query = database_url_template(template)
        self.credential = credential
        self.clock = clock
        self._expirations = {}
        self._lock = threading.Lock()

    def new_url(self):
        with self._lock:
            try:
                access = self.credential.get_token(DATABASE_SCOPE)
                if not isinstance(access.token, str) or not access.token or access.expires_on <= self.clock() + 300:
                    raise ValueError
                digest = hashlib.sha256(access.token.encode()).hexdigest()
                self._expirations = {key: expiry for key, expiry in self._expirations.items() if expiry > self.clock()}
                self._expirations[digest] = access.expires_on
                while len(self._expirations) > 8:
                    del self._expirations[next(iter(self._expirations))]
                authority = f"{self.parsed.username}:{quote(access.token, safe='')}@{self.parsed.hostname}:5432"
                return urlunsplit(("postgresql", authority, self.parsed.path, self.query, ""))
            except Exception:
                raise DatabaseAuthError("Azure database credential acquisition failed or returned insufficient token lifetime") from None

    def expires_at(self, token):
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            expiry = self._expirations.get(hashlib.sha256(token.encode()).hexdigest())
        return datetime.fromtimestamp(expiry, timezone.utc).replace(tzinfo=None) if expiry else None


def azure_wrapper_type(base, tokens):
    class AzurePrismaWrapper(base):
        def _parse_token_expiration(self, token):
            return tokens.expires_at(token)

        def get_rds_iam_token(self):
            if not self.iam_token_db_auth or self._db_url_env_var != "DATABASE_URL" or self._iam_endpoint is not None:
                raise DatabaseAuthError("Azure Prisma adapter requires the reviewed single-writer authentication configuration")
            value = tokens.new_url()
            os.environ["DATABASE_URL"] = value
            return value

        async def _safe_refresh_token(self):
            async with self._reconnection_lock:
                if self._token_refresh_not_needed(os.environ.get(self._db_url_env_var)):
                    return
                if not self.iam_token_db_auth or self._db_url_env_var != "DATABASE_URL" or self._iam_endpoint is not None:
                    raise DatabaseAuthError("Unsupported Azure Prisma refresh configuration")
                value = await asyncio.to_thread(tokens.new_url)
                previous = os.environ.get("DATABASE_URL")
                os.environ["DATABASE_URL"] = value
                try:
                    await self._replace_prisma_client_for_token_refresh_locked(value)
                except BaseException:
                    if previous is None:
                        os.environ.pop("DATABASE_URL", None)
                    else:
                        os.environ["DATABASE_URL"] = previous
                    raise
                self._last_refresh_time = datetime.now(timezone.utc).replace(tzinfo=None)

    return AzurePrismaWrapper


def verify_prisma_source(module):
    if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != PRISMA_WRAPPER_SHA256:
        raise DatabaseAuthError("Prisma wrapper differs from the reviewed image; revalidate the Azure adapter before upgrading")


def install_prisma_adapter(tokens):
    from litellm.proxy.db import db_url_settings, prisma_client

    verify_prisma_source(prisma_client)
    if hashlib.sha256(Path(db_url_settings.__file__).read_bytes()).hexdigest() != DATABASE_SETTINGS_SHA256:
        raise DatabaseAuthError("Database startup settings differ from the reviewed image")
    if any(name in sys.modules for name in ("litellm.proxy.utils", "litellm.proxy.proxy_server")):
        raise DatabaseAuthError("Install the Azure database adapter before loading the proxy application")
    if any(os.environ.get(key) for key in os.environ if "READ_REPLICA" in key or key in {"DIRECT_URL", "DATABASE_PASSWORD"}):
        raise DatabaseAuthError("Azure adapter does not support alternate database targets or stored passwords")
    if getattr(prisma_client, "_llmgw_azure_installed", False):
        raise DatabaseAuthError("Azure database adapter is already installed")

    def writer_url(_settings):
        return tokens.new_url()

    def reader_url(_settings):
        if any(os.environ.get(key) for key in os.environ if "READ_REPLICA" in key):
            raise DatabaseAuthError("Azure adapter does not support read replicas")
        return None

    initial = tokens.new_url()
    prisma_client.PrismaWrapper = azure_wrapper_type(prisma_client.PrismaWrapper, tokens)
    db_url_settings.DatabaseURLSettings.build_writer_url = writer_url
    db_url_settings.DatabaseURLSettings.build_reader_url = reader_url
    prisma_client._llmgw_azure_installed = True
    os.environ["IAM_TOKEN_DB_AUTH"] = "True"
    os.environ["DATABASE_URL"] = initial
    return initial


def workload_identity_tokens(template):
    required = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_FEDERATED_TOKEN_FILE")
    if not all(os.environ.get(key) for key in required):
        raise DatabaseAuthError("Explicit Azure Workload Identity configuration is required; developer and node identity fallback is disabled")
    from azure.identity import WorkloadIdentityCredential

    credential = WorkloadIdentityCredential(tenant_id=os.environ[required[0]], client_id=os.environ[required[1]], token_file_path=os.environ[required[2]])
    return AzureDatabaseTokens(template, credential)