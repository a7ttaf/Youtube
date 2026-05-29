#!/usr/bin/env python
"""CLI entrypoint for the AdSense live payment sync.

Usage:
    python scripts/run_adsense_payment_sync.py \
        --tenant <UUID> \
        --account <account-id or accounts/...> \
        --reason "<audit reason>" \
        [--dry-run]

Loads runtime config from ``UMS_*`` env vars, opens a SQLAlchemy session, builds
the connector service principal + audit sink, and runs
``AdSensePaymentSyncService.sync``. Live mode commits on success; dry-run prints
counts and never commits.

Exit codes (operator contract -- spec §10):
    0 -- success, including a clean dry-run and a synced_count=0 no-op.
    2 -- typed pre-write/config failure: missing UMS_DATABASE_URL, a missing
         service-actor ValueError, or GoogleConnectorError / AdSensePaymentError
         / AdSensePaymentMappingError. No session commit happened.
    !=0 -- argparse rejection (bad --tenant UUID, missing/blank required flag).
    (untyped errors propagate with a real traceback.)
"""
# ============================================================================
# Purpose: Operator CLI surface that drives one AdSense live payment sync for a
#          single (tenant, account). Translates argparse errors, typed
#          pre-write failures, and success/dry-run into stable exit codes for
#          cron/runbook callers.
# Database/ORM: Opens one Session; all SQL is owned by
#          AdSensePaymentSyncService / SqlAlchemyAdSensePaymentRepository /
#          SqlAlchemyAuditSink. The CLI commits the session in live mode only.
# Standards: Thin entrypoint -- argparse + session bootstrap + exit-code
#          translation. Typed connector/payment errors -> exit 2; untyped
#          errors propagate. No secret/token is printed.
# Blast Radius: Pure operator surface. No authorization, finance math, graph,
#          run_one, connector_runs, or source-row logic here.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/adsense_payment_sync.py
#     -> AdSensePaymentSyncService.sync (the whole §5 pipeline).
#   - File: backend/ums_smart_revenue/connectors/google/audit.py ->
#     build_connector_service_principal (RUN_CONNECTOR_JOBS service actor).
#   - File: backend/ums_smart_revenue/auth/sql_audit_sink.py ->
#     SqlAlchemyAuditSink (persists ADSENSE_PAYMENT_SYNCED).
#   - File: Docs/superpowers/specs/2026-05-28-spec-adsense-payment-sync-design.md
#     §10 -> CLI flags, exit codes, dry-run semantics.
# ============================================================================
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

# Bootstrap: make ``backend/`` importable when invoked directly. Mirrors
# scripts/run_google_connector.py; pyproject ``pythonpath = ["backend"]`` covers
# pytest collection but not direct script execution.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PATH = str(_PROJECT_ROOT / "backend")
if _BACKEND_PATH not in sys.path:
    sys.path.insert(0, _BACKEND_PATH)

from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink  # noqa: E402
from ums_smart_revenue.config.settings import load_app_settings  # noqa: E402
from ums_smart_revenue.connectors.google.adsense_payment_mapping import (  # noqa: E402
    AdSensePaymentMappingError,
)
from ums_smart_revenue.connectors.google.adsense_payment_sync import (  # noqa: E402
    AdSensePaymentSyncService,
)
from ums_smart_revenue.connectors.google.audit import (  # noqa: E402
    build_connector_service_principal,
)
from ums_smart_revenue.connectors.google.errors import (  # noqa: E402
    GoogleConnectorError,
)
from ums_smart_revenue.db.session import build_session_factory  # noqa: E402
from ums_smart_revenue.finance.adsense_payments import (  # noqa: E402
    AdSensePaymentError,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse + validate CLI args; argparse exits non-zero on rejection."""
    parser = argparse.ArgumentParser(
        description="Run the AdSense live payment sync for one (tenant, account).",
    )
    parser.add_argument(
        "--tenant", required=True, type=UUID, help="Tenant UUID for this sync."
    )
    parser.add_argument(
        "--account",
        required=True,
        help="AdSense account id (bare id or accounts/<id>).",
    )
    parser.add_argument(
        "--reason", required=True, help="Non-empty audit reason for the pull."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + validate + parse only: no DB writes, no audit, no commit.",
    )
    args = parser.parse_args(argv)
    if not args.reason.strip():
        # parser.error exits with code 2 (argparse convention).
        parser.error("--reason must not be blank")
    return args


def main(argv: list[str] | None = None) -> int:
    """Return the CLI exit code; ``__main__`` wraps this in ``SystemExit``."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        settings = load_app_settings()
    except ValueError as exc:
        # FIX: Malformed operator settings are input/configuration errors, not
        # untyped runtime failures, and must be reported before DB setup.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not settings.database_url:
        print(
            "UMS_DATABASE_URL is required to run the AdSense payment sync",
            file=sys.stderr,
        )
        return 2
    session_factory = build_session_factory(settings.database_url)
    with session_factory() as session:
        try:
            # Service-actor resolution is fail-closed: a missing/blank
            # UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID raises ValueError here,
            # before any fetch or DB write. Caught narrowly so unrelated
            # ValueErrors inside service.sync still propagate.
            actor = build_connector_service_principal(tenant_id=args.tenant)
        except ValueError as exc:
            print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2

        audit_sink = SqlAlchemyAuditSink(session, tenant_id=args.tenant)
        service = AdSensePaymentSyncService(session, audit_sink=audit_sink)
        try:
            result = service.sync(
                tenant_id=args.tenant,
                account_id=args.account,
                actor=actor,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        except (
            GoogleConnectorError,
            AdSensePaymentError,
            AdSensePaymentMappingError,
        ) as exc:
            # Typed pre-write/parse/validation/locked failure. No commit; the
            # operator gets ``<ClassName>: <message>`` on stderr. (Untyped
            # errors are NOT caught here -- they propagate with a traceback.)
            print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2

        if args.dry_run:
            # Dry-run: print would-sync counts, do not commit.
            print(
                f"DRY-RUN would_sync={result.synced_count} "
                f"skipped_balances={result.skipped_balance_count} "
                f"skipped_locked={result.skipped_locked_count} "
                f"months={result.months}"
            )
            return 0

        # Live mode: persist the pull (sync_payments + audit already ran inside
        # service.sync within this transaction scope).
        session.commit()

    print(
        f"SYNCED synced={result.synced_count} "
        f"skipped_balances={result.skipped_balance_count} "
        f"skipped_locked={result.skipped_locked_count} "
        f"months={result.months}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
