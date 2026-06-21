#!/usr/bin/env python
"""Credential-only smoke CLI for Google connector credentials (PR #132 contract).

Resolves an external secret reference and performs an official Google token
refresh without creating connector runs, source rows, or finance facts.
"""

# ============================================================================
# Purpose: Operator CLI that smoke-checks one Google connector credential by
#          resolving the external secret reference and performing an official
#          token refresh only — no connector run, ingestion, or finance writes.
# Database/ORM: Reads/writes ``api_connector_credentials`` refresh telemetry
#               inside a tenant-scoped session; no ``connector_runs`` rows.
# Standards: Thin entrypoint; typed; catches ``GoogleConnectorError`` only and
#            prints redacted stderr lines with exit code 2; keyword-only calls
#            into ``resolve_connector_credentials``.
# Blast Radius: Operator surface only — commits credential refresh telemetry.
#               No ingestion, finance facts, audit events, or connector runs.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     ``resolve_connector_credentials``.
#   - File: backend/ums_smart_revenue/connectors/runs/tenant_context.py ->
#     ``connector_tenant_context``.
#   - File: Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md -> operator runbook.
# ============================================================================
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PATH = str(_PROJECT_ROOT / "backend")
if _BACKEND_PATH not in sys.path:
    sys.path.insert(0, _BACKEND_PATH)

from ums_smart_revenue.config.settings import load_app_settings  # noqa: E402
from ums_smart_revenue.connectors.google import registry  # noqa: E402
from ums_smart_revenue.connectors.google.errors import (  # noqa: E402
    CredentialNotFoundError,
    GoogleConnectorError,
    InactiveCredentialError,
    MalformedSecretPayloadError,
    MalformedSecretUriError,
    OAuthRefreshError,
    SecretFetchError,
    SecretNotFoundError,
    TenantLifecycleError,
    UnsupportedSecretSchemeError,
)
from ums_smart_revenue.connectors.runs.orchestrator import (  # noqa: E402
    resolve_connector_credentials,
)
from ums_smart_revenue.connectors.runs.tenant_context import (  # noqa: E402
    connector_tenant_context,
)
from ums_smart_revenue.db.session import build_session_factory  # noqa: E402


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-check a Google connector credential without running ingestion.",
    )
    parser.add_argument("--tenant", required=True, type=UUID, help="Tenant UUID for this check.")
    parser.add_argument(
        "--connector",
        required=True,
        choices=sorted(registry.known_keys()),
        help="Connector key registered in connectors.google.registry.",
    )
    parser.add_argument("--account", required=True, help="Connector account identifier.")
    return parser.parse_args(argv)


def _format_expiry(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return "unknown"


def _safe_error_line(exc: GoogleConnectorError) -> str:
    if isinstance(exc, CredentialNotFoundError):
        message = "connector credential not found"
    elif isinstance(exc, InactiveCredentialError):
        message = "credential is inactive or revoked"
    elif isinstance(exc, TenantLifecycleError):
        message = "tenant is missing or not active"
    elif isinstance(exc, UnsupportedSecretSchemeError):
        message = "stored credential uses an unsupported secret scheme"
    elif isinstance(exc, MalformedSecretUriError):
        message = "stored credential secret URI is malformed"
    elif isinstance(exc, SecretNotFoundError):
        message = "stored credential secret was not found"
    elif isinstance(exc, SecretFetchError):
        message = "stored credential secret could not be fetched"
    elif isinstance(exc, MalformedSecretPayloadError):
        message = "stored credential secret payload is malformed"
    elif isinstance(exc, OAuthRefreshError):
        message = "Google credential token refresh failed"
    else:
        message = "connector credential smoke failed"
    return f"{type(exc).__name__}: {message}"


# ============================================================================
# Purpose: CLI entrypoint. Parse argv, open a tenant-scoped session, resolve
#          the credential via official Google token refresh, commit telemetry,
#          and print the operator OK line or a redacted stderr failure.
# Database/ORM: One ``Session``; writes ``api_connector_credentials`` refresh
#               telemetry only on success.
# Standards: Fail-closed on typed connector errors (exit 2); keyword-only
#            resolver call; session rollback on failure before exit.
# Blast Radius: Public operator contract for credential smoke — exit codes and
#               redacted stderr lines are consumed by runbooks and automation.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     ``resolve_connector_credentials``.
#   - File: backend/ums_smart_revenue/db/session.py ->
#     ``build_session_factory``.
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        settings = load_app_settings()
    except ValueError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not settings.database_url:
        print(
            "UMS_DATABASE_URL is required to smoke-check a Google connector credential",
            file=sys.stderr,
        )
        return 2

    session_factory = build_session_factory(settings.database_url)
    with session_factory() as session:
        try:
            with connector_tenant_context(args.tenant, session=session):
                credentials = resolve_connector_credentials(
                    session=session,
                    tenant_id=args.tenant,
                    connector_key=args.connector,
                    account_id=args.account,
                )
            session.commit()
        except GoogleConnectorError as exc:
            session.rollback()
            print(_safe_error_line(exc), file=sys.stderr)
            return 2

    print(
        "OK "
        f"connector={args.connector} "
        f"account={args.account} "
        f"token_expiry={_format_expiry(getattr(credentials, 'expiry', None))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
