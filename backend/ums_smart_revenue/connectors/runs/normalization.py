"""Post-run source-row normalization adapter for connector runs."""

from __future__ import annotations

import logging
from collections import Counter
from uuid import UUID

from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import AuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.connectors.google.audit import (
    build_connector_service_principal,
)
from ums_smart_revenue.connectors.runs.repository import (
    ConnectorRunEntry,
    record_projection_failure,
)
from ums_smart_revenue.db.lane import platform_lane
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
    SkippedSourceRow,
)
from ums_smart_revenue.finance.month_close import get_month_close_status
from ums_smart_revenue.finance.revenue_facts import RevenueFactLockedMonthError

logger = logging.getLogger(__name__)


class SqlAlchemyIngestedSourceRowNormalizationAdapter:
    """Own DB and transaction work for connector source-row normalization."""

    def __init__(
        self,
        session: Session,
        *,
        tenant_id: UUID,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id

    # ============================================================================
    # Purpose: Normalize already-ingested Google source rows for one connector run
    #          and persist matching fact/audit changes in one transaction.
    # Database/ORM: Reads finance_month_close and google_revenue_source_rows;
    #               writes MonthlyChannelRevenueFactORM, audit rows, and
    #               connector_runs projection-failure status when needed.
    # Standards: Adapter owns commit/rollback and repository/normalizer DB calls;
    #            locked-month paths fail closed, non-lock errors are recorded on
    #            the run and re-raised. No secrets/PII logged.
    # Blast Radius: Finance facts, audit trail, connector run history. No graph
    #               projection impact detected.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
    #     Delegates post-run projection here after orchestration gates pass.
    #   - File: backend/ums_smart_revenue/finance/google_source_normalizer.py ->
    #     Performs source-row to fact projection without owning commit.
    #   - File: backend/ums_smart_revenue/connectors/runs/repository.py ->
    #     record_projection_failure records non-lock projection failures.
    # ============================================================================
    def normalize_after_run(
        self,
        *,
        report_month: str,
        run: ConnectorRunEntry,
        actor_user_id: str,
        audit_actor: UserPrincipal,
    ) -> None:
        audit_sink: AuditSink = SqlAlchemyAuditSink(self._session, tenant_id=self._tenant_id)
        try:
            if (
                get_month_close_status(self._session, report_month, tenant_id=self._tenant_id)
                == "LOCKED"
            ):
                self._session.rollback()
                logger.info(
                    "ingestion normalize skipped (month locked) tenant_id=%s month=%s",
                    self._tenant_id,
                    report_month,
                )
                return

            normalizer = GoogleSourceNormalizer(self._session, tenant_id=self._tenant_id)
            # FIX: the normalize write set is platform-only -- record_fact upserts
            # monthly_channel_revenue_facts, get_or_create_month_close_row can
            # INSERT an OPEN finance_month_close row, and the REPORT_IMPORTED
            # emits write audit_logs (all TENANT_PLATFORM_ONLY_WRITE). Elevate
            # the write block through commit so the tenant lane (CLI / executor)
            # does not permission-deny. The LOCKED prefilter SELECT above stays
            # on the tenant lane (read, policy-scoped). No-op off Postgres.
            with platform_lane(self._session):
                result = normalizer.normalize_month(
                    month=report_month,
                    actor_user_id=actor_user_id,
                )
                for fact in result.created:
                    _emit_normalized_fact_audit(
                        audit_sink=audit_sink,
                        audit_actor=audit_actor,
                        run=run,
                        fact=fact,
                        lifecycle="CREATED",
                    )
                for fact in result.updated:
                    _emit_normalized_fact_audit(
                        audit_sink=audit_sink,
                        audit_actor=audit_actor,
                        run=run,
                        fact=fact,
                        lifecycle="UPDATED",
                    )
                if result.skipped:
                    _emit_skipped_rows_audit(
                        audit_sink=audit_sink,
                        audit_actor=audit_actor,
                        run=run,
                        report_month=report_month,
                        skipped=result.skipped,
                    )
                self._session.commit()
        except RevenueFactLockedMonthError:
            self._session.rollback()
            logger.info(
                "ingestion normalize skipped (month locked mid-flight) tenant_id=%s month=%s",
                self._tenant_id,
                report_month,
            )
        except Exception as exc:
            self._session.rollback()
            try:
                _record_projection_failure_on_run(
                    session=self._session,
                    tenant_id=self._tenant_id,
                    run=run,
                    exc=exc,
                )
            except Exception:
                logger.exception(
                    "ingestion normalize could not record projection failure "
                    "on run tenant_id=%s month=%s run_id=%s",
                    self._tenant_id,
                    report_month,
                    run.id,
                )
                self._session.rollback()
            logger.exception(
                "ingestion normalize failed tenant_id=%s month=%s",
                self._tenant_id,
                report_month,
            )
            raise


def _record_projection_failure_on_run(
    *,
    session: Session,
    tenant_id: UUID,
    run: ConnectorRunEntry,
    exc: BaseException,
) -> None:
    """Rewrite a terminal run to FAILED with a projection-failure error summary."""
    from ums_smart_revenue.auth.audit import AuditEventType
    from ums_smart_revenue.auth.audit_service import record_audit_event
    from ums_smart_revenue.auth.scopes import AccessScope

    error_summary = f"normalize failed: {type(exc).__name__}: {exc!s}"
    try:
        run_id = UUID(run.id)
    except (ValueError, TypeError, AttributeError):
        logger.warning(
            "cannot record projection failure: run id is not a UUID (run_id=%r)",
            run.id,
        )
        return
    audit_actor = build_connector_service_principal(tenant_id=tenant_id)
    audit_sink: AuditSink = SqlAlchemyAuditSink(session, tenant_id=tenant_id)
    # FIX: the connector_runs rewrite is tenant-writable, but the
    # PROJECTION_FAILED audit emit writes audit_logs (platform-only). Elevate
    # the whole short transaction so the audit INSERT does not permission-deny
    # on the tenant lane -- otherwise the FAILED rewrite rolls back with it and
    # the durable run stays SUCCEEDED with zero facts (the P2 compounding
    # break). No-op off Postgres.
    with platform_lane(session):
        record_projection_failure(
            session,
            tenant_id=tenant_id,
            connector_run_id=run_id,
            error_summary=error_summary,
        )
        record_audit_event(
            sink=audit_sink,
            actor=audit_actor,
            event_type=AuditEventType.CONNECTOR_JOB_RUN,
            entity_type="connector_run",
            entity_id=run.id,
            scope=AccessScope.connector(run.connector_key),
            reason="post-run normalize failed; run rewritten to FAILED",
            details={
                "lifecycle": "PROJECTION_FAILED",
                "run_id": run.id,
                "connector_key": run.connector_key,
                "account_id": run.account_id,
                "report_month": run.report_month,
                "error_summary_present": True,
            },
        )
        session.commit()


# ============================================================================
# Purpose: Surface source rows the normalizer dropped from the fact projection
#          as one durable audit edge + a WARNING log, so unregistered/inactive
#          channel revenue is not silently lost without any signal.
# Database/ORM: Writes one audit_logs row (via record_audit_event) inside the
#               caller's platform_lane transaction. Reads nothing.
# Standards: One summary edge per run (counts by reason) -- never one row per
#            skip -- to avoid audit flooding; WARNING level because dropped
#            revenue warrants operator attention. No secrets/PII logged.
# Blast Radius: Audit trail only. Does NOT change what gets ingested or
#               projected. No finance number changes. No graph projection
#               impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/finance/google_source_normalizer.py ->
#     Produces NormalizationResult.skipped (SkippedSourceRow + SkipReason).
# ============================================================================
def _emit_skipped_rows_audit(
    *,
    audit_sink: AuditSink,
    audit_actor: UserPrincipal,
    run: ConnectorRunEntry,
    report_month: str,
    skipped: list[SkippedSourceRow],
) -> None:
    """Emit one CONNECTOR_JOB_RUN summary edge for projection-skipped source rows."""
    from ums_smart_revenue.auth.audit import AuditEventType
    from ums_smart_revenue.auth.audit_service import record_audit_event
    from ums_smart_revenue.auth.scopes import AccessScope

    # FIX: avoid eager evaluation of ``str(row.reason)`` for every skipped row.
    # ``getattr(..., default)`` evaluates the default argument before the call,
    # so the previous ``str(row.reason)`` default ran even when ``.value``
    # existed. ``SkipReason`` is an Enum whose ``.value`` always exists, so the
    # ternary short-circuits in practice and never falls back to ``str()`` here;
    # the fallback is retained only to keep the helper safe for non-enum inputs.
    reason_counts = dict(
        Counter(
            row.reason.value if hasattr(row.reason, "value") else str(row.reason) for row in skipped
        )
    )
    logger.warning(
        "ingestion normalize dropped %d source row(s) from fact projection "
        "tenant_id=%s month=%s skipped_by_reason=%s",
        len(skipped),
        run.tenant_id,
        report_month,
        reason_counts,
    )
    record_audit_event(
        sink=audit_sink,
        actor=audit_actor,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        entity_type="connector_run",
        entity_id=run.id,
        scope=AccessScope.finance_month(report_month),
        reason="connector normalize: source rows skipped during projection",
        details={
            "lifecycle": "ROWS_SKIPPED",
            "skipped_count": len(skipped),
            "skipped_by_reason": reason_counts,
            "month": report_month,
            "triggered_by_run_id": run.id,
            "triggered_by_connector_key": run.connector_key,
            "triggered_by_account_id": run.account_id,
        },
    )


def _emit_normalized_fact_audit(
    *,
    audit_sink: AuditSink,
    audit_actor: UserPrincipal,
    run: ConnectorRunEntry,
    fact: object,
    lifecycle: str,
) -> None:
    """Emit a REPORT_IMPORTED audit row for one post-run normalized fact."""
    from ums_smart_revenue.auth.audit import AuditEventType
    from ums_smart_revenue.auth.audit_service import record_audit_event
    from ums_smart_revenue.auth.scopes import AccessScope

    fact_month = getattr(fact, "month", None) or run.report_month
    record_audit_event(
        sink=audit_sink,
        actor=audit_actor,
        event_type=AuditEventType.REPORT_IMPORTED,
        entity_type="monthly_channel_revenue_fact",
        entity_id=getattr(fact, "audit_entity_id", None),
        scope=AccessScope.finance_month(fact_month),
        reason=f"connector normalize: {lifecycle}",
        details={
            "lifecycle": lifecycle,
            "source_kind": getattr(fact, "source_kind", None),
            "source_report_id": getattr(fact, "source_report_id", None),
            "youtube_channel_id": getattr(fact, "youtube_channel_id", None),
            "month": fact_month,
            "triggered_by_run_id": run.id,
            "triggered_by_connector_key": run.connector_key,
            "triggered_by_account_id": run.account_id,
        },
    )
