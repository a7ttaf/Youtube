"""CLI-triggered AdSense live payment sync service (spec §5).

Pipeline: resolve credentials -> fetch payments.list -> classify -> read-only
locked-month prefilter -> strict parse (open months only) -> sync_payments ->
audit. Fully separate from the run_one source-row framework.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.connectors.google.adsense_management_client import (
    _validated_account_id,
)
from ums_smart_revenue.connectors.google.adsense_payment_mapping import (
    PaidSettlement,
    SkippedBalance,
    classify_payments,
    parse_amount,
)
from ums_smart_revenue.connectors.google.adsense_payments_client import (
    GoogleAdSensePaymentClient,
)
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.registry import (
    ADSENSE_MANAGEMENT_CONNECTOR_KEY,
)
from ums_smart_revenue.connectors.runs.orchestrator import (
    resolve_connector_credentials,
)
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentInput,
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.finance.month_close import get_month_close_status

# Bound the per-entry skip evidence folded into the audit payload so a large
# full-history pull cannot write an unbounded blob to audit_logs.details.
_MAX_SKIP_EVIDENCE = 50


@dataclass(frozen=True)
class SkippedLockedSettlement:
    """Paid settlement skipped because its finance month is already locked."""

    resource_name: str
    month: str
    payment_date: str  # ISO date string
    raw_amount: str
    reason: str  # always "month_locked"


@dataclass(frozen=True)
class AdSensePaymentSyncResult:
    """Counts and capped evidence returned by one live payment sync run."""

    synced_count: int
    skipped_balance_count: int
    skipped_locked_count: int
    months: list[str]
    skipped_balances: list[SkippedBalance]
    skipped_locked: list[SkippedLockedSettlement]


def _default_client_factory(credentials: object) -> GoogleAdSensePaymentClient:
    """Build the live payments client from resolved Google credentials."""
    return GoogleAdSensePaymentClient(http=GoogleHttpClient(credentials=credentials))


class AdSensePaymentSyncService:
    """Orchestrate one AdSense live payment pull into adsense_payments."""

    def __init__(
        self,
        session: Session,
        *,
        audit_sink: AuditSink,
        credential_resolver=resolve_connector_credentials,
        client_factory=_default_client_factory,
    ) -> None:
        """Bind database, audit, credential, and client dependencies."""
        self._session = session
        self._audit_sink = audit_sink
        self._credential_resolver = credential_resolver
        self._client_factory = client_factory

    # ========================================================================
    # Purpose: Execute the spec §5 payment-sync pipeline for one
    #   (tenant, account): resolve credentials, fetch the full payments list,
    #   classify, skip locked months read-only, strict-parse open months, then
    #   persist + audit (or dry-run with neither).
    # Database/ORM: AdSensePaymentORM (via SqlAlchemyAdSensePaymentRepository),
    #   FinanceMonthCloseORM (read-only prefilter), audit_logs (one event).
    # Standards: Fail-closed typed errors propagate to the caller (CLI exit 2);
    #   no $->USD guess; locked months skipped before parsing; sync_payments is
    #   the authoritative race guard. No secret/token leaves this method.
    # Blast Radius: Finance payment source of truth + audit. No run_one,
    #   connector_runs, source rows, graph, or reconciliation writes.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/adsense_payments.py ->
    #     sync_payments upsert + locked-month write guard.
    #   - File: backend/ums_smart_revenue/finance/month_close.py ->
    #     get_month_close_status read-only prefilter.
    # ========================================================================
    def sync(
        self,
        *,
        tenant_id,
        account_id: str,
        actor: UserPrincipal,
        reason: str,
        dry_run: bool = False,
    ) -> AdSensePaymentSyncResult:
        """Run the live payment pull; return counts + capped skip evidence."""
        canonical_account = _validated_account_id(account_id)
        # Step 1: resolve credentials (typed CredentialNotFoundError /
        # InactiveCredentialError / OAuthRefreshError propagate -> CLI exit 2).
        credentials = self._credential_resolver(
            session=self._session,
            tenant_id=tenant_id,
            connector_key=ADSENSE_MANAGEMENT_CONNECTOR_KEY,
            account_id=canonical_account,
        )
        # Step 2: single GET of the full payments list (no pagination).
        client = self._client_factory(credentials)
        response = client.fetch_payments(account_id=canonical_account)
        canonical_account = str(response["account_id"])
        report_id = str(response["report_id"])

        # Step 3: classify into paid settlements vs retained balances.
        classified = classify_payments(response, account_id=canonical_account)

        # Step 4: read-only locked-month prefilter (no row creation, no lock).
        open_settlements: list[PaidSettlement] = []
        skipped_locked: list[SkippedLockedSettlement] = []
        status_by_month: dict[str, str | None] = {}
        for settlement in classified.paid:
            if settlement.month not in status_by_month:
                status_by_month[settlement.month] = get_month_close_status(
                    self._session,
                    settlement.month,
                    tenant_id=tenant_id,
                )
            if status_by_month[settlement.month] == "LOCKED":
                skipped_locked.append(
                    SkippedLockedSettlement(
                        resource_name=settlement.resource_name,
                        month=settlement.month,
                        payment_date=settlement.payment_date.isoformat(),
                        raw_amount=settlement.raw_amount,
                        reason="month_locked",
                    )
                )
            else:
                open_settlements.append(settlement)

        # Step 5a: strict parse for OPEN-month settlements only. Any parse error
        # raises AdSensePaymentMappingError here -> abort, zero DB writes.
        inputs: list[AdSensePaymentInput] = []
        for settlement in open_settlements:
            amount, currency = parse_amount(settlement.raw_amount)
            inputs.append(
                AdSensePaymentInput(
                    source_account_id=settlement.source_account_id,
                    month=settlement.month,
                    payment_name=settlement.payment_name,
                    payment_date=settlement.payment_date,
                    payment_amount=amount,
                    payment_currency=currency,
                    payment_status="PAID",
                    raw_payload={
                        "name": settlement.resource_name,
                        "amount": settlement.raw_amount,
                    },
                )
            )

        result = AdSensePaymentSyncResult(
            synced_count=len(inputs),
            skipped_balance_count=len(classified.skipped_balances),
            skipped_locked_count=len(skipped_locked),
            months=sorted({s.month for s in open_settlements}),
            skipped_balances=list(classified.skipped_balances),
            skipped_locked=skipped_locked,
        )

        # dry-run: validated + parsed, but NO persistence and NO audit event.
        if dry_run:
            return result

        # Step 5b: skip sync_payments entirely when nothing remains (it rejects
        # an empty batch); otherwise upsert. sync_payments' own per-month
        # FOR UPDATE locked-month gate is the authoritative race guard.
        if inputs:
            repo = SqlAlchemyAdSensePaymentRepository(self._session, tenant_id=tenant_id)
            repo.sync_payments(
                payments=inputs,
                actor_user_id=actor.user_id,
                source_report_id=report_id,
            )

        # Step 5c: always audit a live pull, even when synced_count == 0.
        self._emit_audit(
            actor=actor,
            reason=reason,
            account_id=canonical_account,
            report_id=report_id,
            result=result,
        )
        return result

    def _emit_audit(
        self,
        *,
        actor: UserPrincipal,
        reason: str,
        account_id: str,
        report_id: str,
        result: AdSensePaymentSyncResult,
    ) -> None:
        """Emit ADSENSE_PAYMENT_SYNCED with counts + capped safe skip evidence."""
        record_audit_event(
            sink=self._audit_sink,
            actor=actor,
            event_type=AuditEventType.ADSENSE_PAYMENT_SYNCED,
            entity_type="adsense_payment_pull",
            entity_id=report_id,
            scope=AccessScope.connector(ADSENSE_MANAGEMENT_CONNECTOR_KEY),
            reason=reason,
            details={
                "trigger": "live_pull",
                "source_account_id": account_id,
                "synced_count": result.synced_count,
                "skipped_balance_count": result.skipped_balance_count,
                "skipped_locked_count": result.skipped_locked_count,
                "months": result.months,
                "skipped_balances": [
                    {
                        "resource_name": b.resource_name,
                        "raw_amount": b.raw_amount,
                        "reason": b.reason,
                    }
                    for b in result.skipped_balances[:_MAX_SKIP_EVIDENCE]
                ],
                "skipped_locked": [
                    {
                        "resource_name": s.resource_name,
                        "month": s.month,
                        "payment_date": s.payment_date,
                        "raw_amount": s.raw_amount,
                        "reason": s.reason,
                    }
                    for s in result.skipped_locked[:_MAX_SKIP_EVIDENCE]
                ],
            },
        )
