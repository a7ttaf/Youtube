"""Secret resolver dispatch.

resolve_secret(ref) parses the URI scheme and dispatches to a registered
SecretResolver. Implemented schemes (registered at app/test boot):
- gcp-secret-manager:// -> GcpSecretManagerResolver (B2.1)
- secret-manager://     -> GcpSecretManagerResolver alias for admin API refs
- local-secret://       -> LocalSecretResolver (B2.1, test only), plus an
  opt-in file-backed variant for demo/self-host deployments enabled by
  UMS_CONNECTOR_LOCAL_SECRETS_FILE (see ensure_default_resolvers)

Other ORM-accepted prefixes (aws-secretsmanager://, vault://, kms://,
azure-keyvault://) are intentionally unregistered until a future
credential-lifecycle PR. They raise UnsupportedSecretSchemeError so B2 fails
closed instead of silently dropping the secret.
"""

from __future__ import annotations

import json
from threading import RLock
from typing import Protocol

from ums_smart_revenue.config.settings import load_app_settings
from ums_smart_revenue.connectors.google.errors import (
    LocalSecretsFileError,
    MalformedSecretUriError,
    ResolverAlreadyRegisteredError,
    UnsupportedSecretSchemeError,
)
from ums_smart_revenue.connectors.google.local_secret_resolver import (
    LocalSecretResolver,
)


class SecretResolver(Protocol):
    """Contract every concrete secret resolver must satisfy."""

    def resolve(self, ref: str) -> str:
        """Return the secret payload as a string. Raise SecretNotFoundError /
        SecretFetchError on backend failure."""


_GCP_SECRET_MANAGER_SCHEMES = ("gcp-secret-manager", "secret-manager")
_REGISTRY: dict[str, SecretResolver] = {}
_REGISTRY_LOCK = RLock()


# ============================================================================
# Purpose: Register a concrete resolver for a secret URI scheme.
# Database/ORM: None.
# Standards: Fail-fast duplicate registration; typed connector errors only.
# Blast Radius: Credential secret bootstrap only. Authorization, finance,
#               audit, Neo4j, and exports are unaffected.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/gcp_secret_manager.py
#     -> Production resolver.
#   - File: backend/ums_smart_revenue/connectors/google/local_secret_resolver.py
#     -> Test/dev resolver.
# ============================================================================
def register_resolver(*, scheme: str, resolver: SecretResolver) -> None:
    with _REGISTRY_LOCK:
        if scheme in _REGISTRY:
            raise ResolverAlreadyRegisteredError(scheme=scheme)
        _REGISTRY[scheme] = resolver


# ============================================================================
# Purpose: Ensure production secret resolver schemes exist before live runs.
# Database/ORM: None.
# Standards: Idempotent runtime bootstrap; typed duplicate protection remains
#            owned by register_resolver().
# Blast Radius: Credential secret bootstrap only. Authorization, finance,
#               audit, Neo4j, and exports are unaffected.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/gcp_secret_manager.py ->
#     Production gcp-secret-manager resolver.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     Calls before resolving the credential secret reference.
# ============================================================================
# ============================================================================
# Purpose: Opt-in demo/self-host resolver for ``local-secret://{name}`` refs,
#   backed by the JSON mapping file named by UMS_CONNECTOR_LOCAL_SECRETS_FILE.
#   The file is re-read on every resolve so operators rotate credential
#   payloads without an app restart; unreadable files, invalid JSON, and
#   non-string values fail closed with LocalSecretsFileError before any
#   payload material is handed to the OAuth layer. Registered ONLY when that
#   setting is present (ensure_default_resolvers); production deployments that
#   leave it unset keep local-secret:// unregistered and unsupported.
# Database/ORM: None.
# Standards: Reuses LocalSecretResolver's URI parsing and SecretNotFoundError
#            contract; secret payloads are never included in error messages.
# Blast Radius: Connector credential secret resolution in demo deployments
#               only. No finance, authorization, audit, or export impact.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/local_secret_resolver.py
#     -> LocalSecretResolver performs the ref parse + mapping lookup.
#   - File: backend/ums_smart_revenue/config/settings.py ->
#     connector_local_secrets_file gates registration.
#   - File: backend/ums_smart_revenue/connectors/google/errors.py ->
#     LocalSecretsFileError carries the path and inner error type only.
# ============================================================================
class _FileBackedLocalSecretResolver:
    """Resolve ``local-secret://{name}`` from the opt-in JSON secrets file."""

    # Bounded read so a misconfigured oversized file cannot allocate unbounded
    # memory per credential resolve. A credential mapping of a few OAuth
    # payloads is a few KiB; 1 MiB is generous headroom.
    _MAX_FILE_BYTES = 1024 * 1024

    def __init__(self, *, path: str) -> None:
        self._path = path

    def resolve(self, ref: str) -> str:
        """Re-read the mapping file and resolve ``ref`` against it, failing closed."""
        try:
            with open(self._path, encoding="utf-8") as handle:
                content = handle.read(self._MAX_FILE_BYTES + 1)
            if len(content.encode("utf-8")) > self._MAX_FILE_BYTES:
                raise LocalSecretsFileError(path=self._path)
            mapping = json.loads(content)
        except (OSError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError is a ValueError sibling, not a JSONDecodeError:
            # invalid UTF-8 in the secrets file must map to the same typed
            # fail-closed error as an unreadable file, not escape as a 500.
            raise LocalSecretsFileError(path=self._path, inner=exc) from exc
        except json.JSONDecodeError as exc:
            raise LocalSecretsFileError(path=self._path, inner=exc) from exc
        if not isinstance(mapping, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
        ):
            raise LocalSecretsFileError(path=self._path)
        return LocalSecretResolver(mapping=mapping).resolve(ref)


def ensure_default_resolvers() -> None:
    """Register production secret resolver schemes exactly once at runtime boot."""
    with _REGISTRY_LOCK:
        missing = [scheme for scheme in _GCP_SECRET_MANAGER_SCHEMES if scheme not in _REGISTRY]
        if missing:
            from ums_smart_revenue.connectors.google.gcp_secret_manager import (
                GcpSecretManagerResolver,
            )

            resolver = _REGISTRY.get("gcp-secret-manager") or _REGISTRY.get("secret-manager")
            if resolver is None:
                resolver = GcpSecretManagerResolver()
            for scheme in missing:
                _REGISTRY[scheme] = resolver
        # Demo/self-host opt-in: register the file-backed local-secret resolver
        # only when UMS_CONNECTOR_LOCAL_SECRETS_FILE is configured. Unset (the
        # production default) leaves the scheme unregistered so resolve_secret
        # fails closed with UnsupportedSecretSchemeError exactly as before.
        # FIX: defer strict tenant-currency validation (contract shared with
        # app.py / connectors/google/audit.py): resolver boot is authz-mode
        # independent and must not crash on a malformed currency env in
        # database-authz deployments.
        local_secrets_file = load_app_settings(
            validate_tenant_currency=False
        ).connector_local_secrets_file
        if local_secrets_file and "local-secret" not in _REGISTRY:
            _REGISTRY["local-secret"] = _FileBackedLocalSecretResolver(path=local_secrets_file)


def _parse_scheme(ref: str) -> str:
    if not ref or "://" not in ref:
        raise MalformedSecretUriError(ref=ref)
    scheme, _, rest = ref.partition("://")
    if not scheme or not rest:
        raise MalformedSecretUriError(ref=ref)
    return scheme


def resolve_secret(ref: str) -> str:
    scheme = _parse_scheme(ref)
    with _REGISTRY_LOCK:
        resolver = _REGISTRY.get(scheme)
    if resolver is None:
        raise UnsupportedSecretSchemeError(scheme=scheme)
    return resolver.resolve(ref)
