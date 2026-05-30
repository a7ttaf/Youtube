#!/usr/bin/env python
"""CLI entrypoint for deduction-component ingestion."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PATH = str(_PROJECT_ROOT / "backend")
if _BACKEND_PATH not in sys.path:
    sys.path.insert(0, _BACKEND_PATH)

from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink  # noqa: E402
from ums_smart_revenue.config.settings import load_app_settings  # noqa: E402
from ums_smart_revenue.connectors.google.audit import (  # noqa: E402
    build_connector_service_principal,
)
from ums_smart_revenue.db.session import build_session_factory  # noqa: E402
from ums_smart_revenue.finance.deduction_ingestion import (  # noqa: E402
    INGESTION_SOURCES,
    DeductionComponentError,
    DeductionIngestionService,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse operator CLI arguments for one deduction ingestion run."""
    parser = argparse.ArgumentParser(
        description="Run deduction-component ingestion for one (tenant, month).",
    )
    parser.add_argument("--tenant", required=True, type=UUID, help="Tenant UUID.")
    parser.add_argument("--month", required=True, help="Finance month YYYY-MM.")
    parser.add_argument("--reason", required=True, help="Non-empty audit reason.")
    parser.add_argument(
        "--source", choices=list(INGESTION_SOURCES), default=None,
        help="Limit to one source adapter (default: all).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute counts only: no DB writes, no audit, no commit.",
    )
    args = parser.parse_args(argv)
    if not args.reason.strip():
        parser.error("--reason must not be blank")
    return args


# ============================================================================
# Purpose: Operator CLI driving one deduction-component ingestion run for a
#   single (tenant, month). Translates argparse/config/typed errors into stable
#   exit codes; live mode commits, dry-run never commits.
# Database/ORM: Opens one Session; SQL owned by DeductionIngestionService /
#   SqlAlchemyDeductionComponentRepository / SqlAlchemyAuditSink.
# Standards: thin entrypoint; typed DeductionComponentError / operator
#   ValueError -> exit 2; untyped non-ValueError failures propagate. No
#   secret/token printed.
# Blast Radius: Operator surface only. No finance math, no allocation here.
# Connections:
#   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> service.
#   - File: backend/ums_smart_revenue/connectors/google/audit.py ->
#     build_connector_service_principal (RUN_CONNECTOR_JOBS service actor).
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Run deduction ingestion and return the operator exit code."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        settings = load_app_settings()
    except ValueError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not settings.database_url:
        print(
            "UMS_DATABASE_URL is required to run deduction ingestion",
            file=sys.stderr,
        )
        return 2
    session_factory = build_session_factory(settings.database_url)
    with session_factory() as session:
        try:
            actor = build_connector_service_principal(tenant_id=args.tenant)
        except ValueError as exc:
            print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2
        audit_sink = SqlAlchemyAuditSink(session, tenant_id=args.tenant)
        service = DeductionIngestionService(
            session, audit_sink=audit_sink, tenant_id=args.tenant
        )
        try:
            result = service.ingest(
                month=args.month, actor=actor, reason=args.reason,
                source=args.source, dry_run=args.dry_run,
            )
        except (DeductionComponentError, ValueError) as exc:
            print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2

        if args.dry_run:
            print(
                f"DRY-RUN would_upsert={result.total_upserted} "
                f"by_kind={result.by_kind} skipped_non_usd={result.skipped_non_usd} "
                f"month={result.month}"
            )
            return 0
        session.commit()

    print(
        f"INGESTED upserted={result.total_upserted} by_kind={result.by_kind} "
        f"skipped_non_usd={result.skipped_non_usd} month={result.month}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
