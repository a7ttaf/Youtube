#!/usr/bin/env python
"""CLI entrypoint for deduction-component ingestion."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PATH = str(_PROJECT_ROOT / "backend")
INGESTION_SOURCES = ("source_rows", "bank", "gap")

# FIX: Keep project imports lazy so direct script execution can adjust
# sys.path without exposing module-level E402 import-order findings.
SqlAlchemyAuditSink: Any = None
load_app_settings: Any = None
build_connector_service_principal: Any = None
build_session_factory: Any = None
DeductionComponentError: Any = None
DeductionIngestionService: Any = None


class _RuntimeDependencies:
    """Project dependency bundle resolved after the backend path is importable."""

    def __init__(
        self,
        *,
        audit_sink_cls: Any,
        load_settings: Any,
        service_principal_factory: Any,
        session_factory_builder: Any,
        ingestion_error_cls: type[Exception],
        ingestion_service_cls: Any,
    ) -> None:
        self.audit_sink_cls = audit_sink_cls
        self.load_settings = load_settings
        self.service_principal_factory = service_principal_factory
        self.session_factory_builder = session_factory_builder
        self.ingestion_error_cls = ingestion_error_cls
        self.ingestion_service_cls = ingestion_service_cls


def _ensure_backend_path() -> None:
    """Make the local backend package importable for direct script execution."""
    if _BACKEND_PATH not in sys.path:
        sys.path.insert(0, _BACKEND_PATH)


def _load_runtime_dependencies() -> _RuntimeDependencies:
    """Load project dependencies lazily after fixing the import path."""
    _ensure_backend_path()
    audit_sink_cls = SqlAlchemyAuditSink
    load_settings = load_app_settings
    service_principal_factory = build_connector_service_principal
    session_factory_builder = build_session_factory
    ingestion_error_cls = DeductionComponentError
    ingestion_service_cls = DeductionIngestionService

    if audit_sink_cls is None:
        from ums_smart_revenue.auth.sql_audit_sink import (
            SqlAlchemyAuditSink as _SqlAlchemyAuditSink,
        )

        audit_sink_cls = _SqlAlchemyAuditSink
    if load_settings is None:
        from ums_smart_revenue.config.settings import (
            load_app_settings as _load_app_settings,
        )

        load_settings = _load_app_settings
    if service_principal_factory is None:
        from ums_smart_revenue.connectors.google.audit import (
            build_connector_service_principal as _build_connector_service_principal,
        )

        service_principal_factory = _build_connector_service_principal
    if session_factory_builder is None:
        from ums_smart_revenue.db.session import (
            build_session_factory as _build_session_factory,
        )

        session_factory_builder = _build_session_factory
    if ingestion_error_cls is None or ingestion_service_cls is None:
        from ums_smart_revenue.finance.deduction_ingestion import (
            DeductionComponentError as _DeductionComponentError,
        )
        from ums_smart_revenue.finance.deduction_ingestion import (
            DeductionIngestionService as _DeductionIngestionService,
        )

        if ingestion_error_cls is None:
            ingestion_error_cls = _DeductionComponentError
        if ingestion_service_cls is None:
            ingestion_service_cls = _DeductionIngestionService

    return _RuntimeDependencies(
        audit_sink_cls=audit_sink_cls,
        load_settings=load_settings,
        service_principal_factory=service_principal_factory,
        session_factory_builder=session_factory_builder,
        ingestion_error_cls=ingestion_error_cls,
        ingestion_service_cls=ingestion_service_cls,
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
        "--source",
        choices=list(INGESTION_SOURCES),
        default=None,
        help="Limit to one source adapter (default: all).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
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
# Standards: thin entrypoint; service DeductionComponentError / operator
#   ValueError -> exit 2; unexpected service failures propagate. No secret/token
#   printed.
# Blast Radius: Operator surface only. No finance math, no allocation here.
# Connections:
#   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> service.
#   - File: backend/ums_smart_revenue/connectors/google/audit.py ->
#     build_connector_service_principal (RUN_CONNECTOR_JOBS service actor).
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Run deduction ingestion and return the operator exit code."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    deps = _load_runtime_dependencies()
    try:
        settings = deps.load_settings()
    except ValueError as exc:
        # FIX: Do not echo settings validation text; it can include rejected
        # secret values such as database URLs from operator environments.
        print(f"{type(exc).__name__}: invalid operator settings", file=sys.stderr)
        return 2
    if not settings.database_url:
        print(
            "UMS_DATABASE_URL is required to run deduction ingestion",
            file=sys.stderr,
        )
        return 2
    session_factory = deps.session_factory_builder(settings.database_url)
    with session_factory() as session:
        try:
            actor = deps.service_principal_factory(tenant_id=args.tenant)
        except ValueError as exc:
            # FIX: Service principal setup reads operator configuration, so keep
            # the terminal error stable without echoing raw settings values.
            print(
                f"{type(exc).__name__}: invalid service principal configuration",
                file=sys.stderr,
            )
            return 2
        audit_sink = deps.audit_sink_cls(session, tenant_id=args.tenant)
        service = deps.ingestion_service_cls(session, audit_sink=audit_sink, tenant_id=args.tenant)
        try:
            result = service.ingest(
                month=args.month,
                actor=actor,
                reason=args.reason,
                source=args.source,
                dry_run=args.dry_run,
            )
        except deps.ingestion_error_cls as exc:
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
