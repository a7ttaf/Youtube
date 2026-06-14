"""Secret resolver dispatch.

resolve_secret(ref) parses the URI scheme and dispatches to a registered
SecretResolver. Implemented schemes (registered at app/test boot):
- gcp-secret-manager:// -> GcpSecretManagerResolver (B2.1)
- secret-manager://     -> GcpSecretManagerResolver alias for admin API refs
- local-secret://       -> LocalSecretResolver (B2.1, test only)

Other ORM-accepted prefixes (aws-secretsmanager://, vault://, kms://,
azure-keyvault://) are intentionally unregistered until a future
credential-lifecycle PR. They raise UnsupportedSecretSchemeError so B2 fails
closed instead of silently dropping the secret.
"""

from __future__ import annotations

from threading import RLock
from typing import Protocol

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    ResolverAlreadyRegisteredError,
    UnsupportedSecretSchemeError,
)


class SecretResolver(Protocol):
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
def ensure_default_resolvers() -> None:
    """Register production secret resolvers exactly once at runtime boot."""
    with _REGISTRY_LOCK:
        missing = [scheme for scheme in _GCP_SECRET_MANAGER_SCHEMES if scheme not in _REGISTRY]
        if not missing:
            return
        from ums_smart_revenue.connectors.google.gcp_secret_manager import (
            GcpSecretManagerResolver,
        )

        resolver = _REGISTRY.get("gcp-secret-manager") or _REGISTRY.get("secret-manager")
        if resolver is None:
            resolver = GcpSecretManagerResolver()
        for scheme in missing:
            _REGISTRY[scheme] = resolver


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
