"""Post-run source-row normalization adapter for connector runs."""

from __future__ import annotations

import logging
from collections import Counter
from uuid import UUID

from sqlalchemy import select
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
from ums_smart_revenue.db.connector_models import ConnectorRunRawFileORM
from ums_smart_revenue.db.lane import platform_lane
from ums_smart_revenue.db.report_models import RawReportFileORM
from ums_smart_revenue.finance.google_source_normalizer import (
    EvidenceDisposition,
    GoogleSourceNormalizer,
    NonProjectingEvidenceOutcome,
    SkippedSourceRow,
    SkipReason,
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
                run_evidence = _evidence_outcomes_for_analytics_run(
                    session=self._session,
                    run=run,
                    outcomes=result.non_projecting_evidence,
                )
                if run_evidence:
                    _emit_non_projecting_evidence_audit(
                        audit_sink=audit_sink,
                        audit_actor=audit_actor,
                        run=run,
                        report_month=report_month,
                        outcomes=run_evidence,
                    )
                audit_skipped, retained_evidence_skipped = _partition_skipped_rows_for_evidence_run(
                    skipped=result.skipped,
                    run_evidence=run_evidence,
                )
                all_alertable_skips = [*audit_skipped, *retained_evidence_skipped]
                if all_alertable_skips:
                    _emit_skipped_rows_audit(
                        audit_sink=audit_sink,
                        audit_actor=audit_actor,
                        run=run,
                        report_month=report_month,
                        skipped=all_alertable_skips,
                        retained_evidence_count=len(retained_evidence_skipped),
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


# ============================================================================
# Purpose: Scope month-wide normalization outcomes to the exact Analytics
#          connector/account/raw evidence imported by this run.
# Database/ORM: Reads connector_run_raw_files joined to raw_report_files.
# Standards: Exact connector alias, content-owner account, and raw-file lineage;
#            unrelated or unlinked outcomes fail closed.
# Blast Radius: Evidence audit attribution only; finance projection unchanged.
# Connections:
#   - File: backend/ums_smart_revenue/finance/google_source_normalizer.py ->
#     Returns month-wide outcomes carrying preserved source_account_id values.
#   - Function: _emit_non_projecting_evidence_audit -> Receives only the
#     triggering account's evidence snapshot.
# ============================================================================
def _evidence_outcomes_for_analytics_run(
    *,
    session: Session,
    run: ConnectorRunEntry,
    outcomes: list[NonProjectingEvidenceOutcome],
) -> list[NonProjectingEvidenceOutcome]:
    """Return only evidence attributable to this Analytics account run."""
    if run.connector_key not in {"youtube-analytics", "youtube_analytics"}:
        return []
    account_id = run.account_id.strip()
    if not account_id:
        return []
    try:
        run_id = UUID(run.id)
        tenant_id = UUID(run.tenant_id)
    except (TypeError, ValueError, AttributeError):
        return []
    raw_file_ids = {
        str(raw_file_id)
        for raw_file_id in session.scalars(
            select(ConnectorRunRawFileORM.raw_report_file_id)
            .join(
                RawReportFileORM,
                (RawReportFileORM.tenant_id == ConnectorRunRawFileORM.tenant_id)
                & (RawReportFileORM.id == ConnectorRunRawFileORM.raw_report_file_id),
            )
            .where(
                ConnectorRunRawFileORM.tenant_id == tenant_id,
                ConnectorRunRawFileORM.connector_run_id == run_id,
                RawReportFileORM.source == "youtube_analytics",
                RawReportFileORM.report_type == "youtube_analytics_country_evidence",
                RawReportFileORM.parse_status == "PARSED",
            )
        ).all()
    }
    if not raw_file_ids:
        return []
    expected_source_account_id = f"contentOwner=={account_id}"
    return [
        outcome
        for outcome in outcomes
        if outcome.source_system == "youtube_analytics"
        and outcome.source_account_id == expected_source_account_id
        and outcome.raw_file_id in raw_file_ids
    ]


# ============================================================================
# Purpose: Separate current-run skips from retained evidence defects without
#          suppressing either class from operator alerts.
# Database/ORM: None.
# Standards: Stable typed skip reasons and source-row identity matching only;
#            retained defects remain visible but are never run-attributed.
# Blast Radius: Connector audit attribution and HIGH skipped-row alerts.
# Connections:
#   - Function: _evidence_outcomes_for_analytics_run -> Supplies the exact
#     account/run-scoped evidence identities.
#   - Function: _emit_skipped_rows_audit -> Receives the filtered defect set.
# ============================================================================
def _partition_skipped_rows_for_evidence_run(
    *,
    skipped: list[SkippedSourceRow],
    run_evidence: list[NonProjectingEvidenceOutcome],
) -> tuple[list[SkippedSourceRow], list[SkippedSourceRow]]:
    """Return current/general skips separately from retained evidence defects."""
    run_evidence_ids = {outcome.source_row_id for outcome in run_evidence}
    evidence_skip_reasons = {
        SkipReason.INVALID_NON_PROJECTING_EVIDENCE,
        SkipReason.DUPLICATE_NON_PROJECTING_EVIDENCE,
    }
    current_or_general: list[SkippedSourceRow] = []
    retained_evidence: list[SkippedSourceRow] = []
    for row in skipped:
        if row.reason in evidence_skip_reasons and row.source_row_id not in run_evidence_ids:
            retained_evidence.append(row)
        else:
            current_or_general.append(row)
    return current_or_general, retained_evidence


# ============================================================================
# Purpose: Record accepted/rejected U2 evidence counts as an informational
#          connector lifecycle edge distinct from projection-defect alerts.
# Database/ORM: Writes one audit_logs row through record_audit_event.
# Standards: Aggregate counts only; no source-row ids, accounts, amounts, or
#            raw payloads cross the audit boundary.
# Blast Radius: Audit telemetry only. Finance facts/totals/exports unchanged.
# Connections:
#   - File: backend/ums_smart_revenue/finance/google_source_normalizer.py ->
#     Produces typed NonProjectingEvidenceOutcome classifications.
#   - File: backend/ums_smart_revenue/finance/smart_alert_signals.py -> Reads
#     ROWS_SKIPPED only, so healthy accepted evidence cannot raise HIGH alerts.
# ============================================================================
def _emit_non_projecting_evidence_audit(
    *,
    audit_sink: AuditSink,
    audit_actor: UserPrincipal,
    run: ConnectorRunEntry,
    report_month: str,
    outcomes: list[NonProjectingEvidenceOutcome],
) -> None:
    """Emit one privacy-safe NON_PROJECTING_EVIDENCE summary edge."""
    from ums_smart_revenue.auth.audit import AuditEventType
    from ums_smart_revenue.auth.audit_service import record_audit_event
    from ums_smart_revenue.auth.scopes import AccessScope

    disposition_counts = Counter(outcome.disposition.value for outcome in outcomes)
    rejected_by_reason = Counter(
        outcome.reason.value
        for outcome in outcomes
        if outcome.disposition is EvidenceDisposition.REJECTED
    )
    accepted_by_country = Counter(
        outcome.country_code
        for outcome in outcomes
        if outcome.disposition is EvidenceDisposition.ACCEPTED and outcome.country_code is not None
    )
    logger.info(
        "ingestion normalize classified non-projecting evidence "
        "tenant_id=%s month=%s accepted=%d rejected=%d",
        run.tenant_id,
        report_month,
        disposition_counts[EvidenceDisposition.ACCEPTED.value],
        disposition_counts[EvidenceDisposition.REJECTED.value],
    )
    record_audit_event(
        sink=audit_sink,
        actor=audit_actor,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        entity_type="connector_run",
        entity_id=run.id,
        scope=AccessScope.finance_month(report_month),
        reason="connector normalize: non-projecting evidence classified",
        details={
            "lifecycle": "NON_PROJECTING_EVIDENCE",
            "accepted_count": disposition_counts[EvidenceDisposition.ACCEPTED.value],
            "rejected_count": disposition_counts[EvidenceDisposition.REJECTED.value],
            "rejected_by_reason": dict(sorted(rejected_by_reason.items())),
            "accepted_by_country": dict(sorted(accepted_by_country.items())),
            "month": report_month,
            "triggered_by_run_id": run.id,
            "triggered_by_connector_key": run.connector_key,
        },
    )


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
    retained_evidence_count: int = 0,
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
    attribution_details: dict[str, object]
    if retained_evidence_count:
        current_or_general_count = len(skipped) - retained_evidence_count
        attribution_details = {
            "attribution_scope": (
                "MIXED_MONTH_SNAPSHOT"
                if current_or_general_count
                else "RETAINED_MONTH_SNAPSHOT"
            ),
            "current_or_general_skipped_count": current_or_general_count,
            "retained_evidence_skipped_count": retained_evidence_count,
            "observed_during_run_id": run.id,
            "observed_during_connector_key": run.connector_key,
        }
    else:
        attribution_details = {
            "attribution_scope": "CURRENT_RUN",
            "triggered_by_run_id": run.id,
            "triggered_by_connector_key": run.connector_key,
            "triggered_by_account_id": run.account_id,
        }
    record_audit_event(
        sink=audit_sink,
        actor=audit_actor,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        entity_type="finance_month" if retained_evidence_count else "connector_run",
        entity_id=report_month if retained_evidence_count else run.id,
        scope=AccessScope.finance_month(report_month),
        reason=(
            "connector normalize: month snapshot contains retained source-row defects"
            if retained_evidence_count
            else "connector normalize: source rows skipped during projection"
        ),
        details={
            "lifecycle": "ROWS_SKIPPED",
            "skipped_count": len(skipped),
            "skipped_by_reason": reason_counts,
            "month": report_month,
            **attribution_details,
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
