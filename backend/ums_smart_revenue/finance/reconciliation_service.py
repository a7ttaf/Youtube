"""Reconciliation workflow service: gather -> compute -> persist -> audit.

Reads source-of-truth finance evidence (CMS gross facts, PAID AdSense totals,
bank receipts + FX), runs the pure compute core, persists the derived per-channel
deductions as typed deduction_components and a reconciliation explanation, writes
ALLOCATION revenue facts for 1:1 outside-CMS channels, and records one audit
event. PostgreSQL/warehouse stays the source of truth; no Neo4j writes.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.finance.adsense_payments import (
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.finance.bank_reconciliation import (
    SqlAlchemyBankReconciliationRepository,
)
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.deduction_components import DeductionComponentInput
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.explanations import (
    SqlAlchemyNumberExplanationRepository,
)
from ums_smart_revenue.finance.month_close import get_month_close_status
from ums_smart_revenue.finance.reconciliation import SOURCE_PRIORITY
from ums_smart_revenue.finance.reconciliation_explanation import (
    build_reconciliation_explanation,
)
from ums_smart_revenue.finance.reconciliation_workflow import (
    ChannelReconciliation,
    MonthReconciliationResult,
    NullUsViewShareProvider,
    UsViewShareProvider,
    compute_month_reconciliation,
)
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactSourceKind,
    SqlAlchemyRevenueFactRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_SOURCE_TABLE = "reconciliation_workflow"
_SOURCE_SYSTEM = "reconciliation"
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
_ALLOCATION_CONFIDENCE = Decimal("0.80")

# Inverse of SOURCE_SYSTEM_TO_SOURCE_KIND: pick a source_system whose mapping a
# reconciliation channel-direct component must adopt so the net-revenue resolver
# (_applicable_deduction_components) treats it as source-aligned to the channel's
# primary fact source_kind. Only net-applicable kinds (TAX) actually reduce net;
# the others carry the channel's source_system for provenance consistency.
_SOURCE_KIND_TO_SOURCE_SYSTEM: dict[RevenueFactSourceKind, str] = {
    RevenueFactSourceKind.YOUTUBE_CMS: "youtube_reporting",
    RevenueFactSourceKind.ADSENSE: "adsense_management",
    RevenueFactSourceKind.YOUTUBE_ANALYTICS: "youtube_analytics",
    RevenueFactSourceKind.ALLOCATION: _SOURCE_SYSTEM,
}


def _source_system_for_source_kind(source_kind: str) -> str:
    """Return the deduction source_system aligned to a revenue fact source_kind."""
    try:
        return _SOURCE_KIND_TO_SOURCE_SYSTEM[RevenueFactSourceKind(source_kind)]
    except ValueError:
        return _SOURCE_SYSTEM


class MonthLockedError(Exception):
    """Raised when reconciliation is attempted on a LOCKED finance month."""


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve explicit, ambient, or default tenant UUID for repository scoping."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    if tenant_id is None:
        current_tenant = get_current_tenant()
        if current_tenant is not None:
            return current_tenant.id
        return _DEFAULT_TENANT_UUID
    return UUID(str(tenant_id).strip())


class ReconciliationWorkflowService:
    """Orchestrate a month's revenue reconciliation end to end."""

    def __init__(
        self,
        session: Session,
        *,
        audit_sink: AuditSink,
        tenant_id: UUID | str | None = None,
        us_view_share_provider: UsViewShareProvider | None = None,
    ) -> None:
        self._session = session
        self._audit_sink = audit_sink
        self._tenant_id = _resolve_tenant_id(tenant_id)
        self._us_view = us_view_share_provider or NullUsViewShareProvider()
        self._facts = SqlAlchemyRevenueFactRepository(session, tenant_id=self._tenant_id)
        self._deductions = SqlAlchemyDeductionComponentRepository(
            session, tenant_id=self._tenant_id
        )
        self._explanations = SqlAlchemyNumberExplanationRepository(
            session, tenant_id=self._tenant_id
        )
        self._links = SqlAlchemyChannelAccountLinkRepository(
            session, tenant_id=self._tenant_id
        )

    def run(
        self, *, month: str, actor: UserPrincipal, reason: str
    ) -> MonthReconciliationResult:
        """Reconcile a finance month and persist the derived deductions + facts.

        Raises:
            MonthLockedError: If the target finance month is LOCKED.
        """
        # ====================================================================
        # Purpose: Smart-reconciliation entry point. Refuses LOCKED months
        #   (fail-closed, no writes/audit), attributes outside-CMS 1:1 account
        #   revenue, gathers CMS gross + AdSense + bank evidence, runs the pure
        #   compute core, persists per-channel deduction_components (replacing
        #   prior reconciliation rows) and reconciliation explanations, then
        #   records one summary-only REVENUE_RECONCILED audit event.
        # Database/ORM: reads monthly_channel_revenue_facts, adsense_payments,
        #   bank_reconciliation_entries, youtube_channels, account/channel links;
        #   writes monthly_channel_revenue_facts (ALLOCATION), deduction_components,
        #   number_explanations (all via repositories).
        # Standards: USD-only inputs; transactional caller commits; typed errors;
        #   audit details carry counts/totals only (no per-channel payloads).
        # Blast Radius: Finance source-of-truth writes + one audit event. The
        #   reconciliation TAX component is source-aligned so the net-revenue
        #   resolver applies it; TRANSFER_FEE/FX_VARIANCE are recorded as
        #   evidence only (never reduce net, by deduction_policy).
        # Connections:
        #   - File: backend/ums_smart_revenue/finance/net_revenue.py -> resolver.
        #   - File: Docs/superpowers/plans/2026-06-09-track-f-smart-reconciliation.md
        # ====================================================================
        if get_month_close_status(
            self._session, month, tenant_id=self._tenant_id
        ) == "LOCKED":
            raise MonthLockedError(f"Finance month {month!r} is locked")

        outside_warnings = self._attribute_outside_cms(month=month, actor=actor)

        channel_gross, primary_source = self._gather_channel_gross(month)
        adsense_total = self._gather_adsense_total(month)
        bank_total, fx_total = self._gather_bank_totals(month)
        us_view_shares = {
            channel: self._us_view.us_view_share(month, channel)
            for channel in channel_gross
        }

        result = compute_month_reconciliation(
            month=month,
            channel_gross=channel_gross,
            us_view_shares=us_view_shares,
            adsense_received_usd=adsense_total,
            bank_received_usd=bank_total,
            fx_total_usd=fx_total,
        )

        components = self._build_components(month, result, primary_source)
        self._deductions.upsert_components(
            month=month,
            components=components,
            replace_source_tables={_SOURCE_TABLE},
        )
        for line in result.channels:
            self._explanations.record_explanation(
                build_reconciliation_explanation(
                    month=month, line=line, warnings=result.warnings,
                )
            )

        record_audit_event(
            sink=self._audit_sink,
            actor=actor,
            event_type=AuditEventType.REVENUE_RECONCILED,
            entity_type="finance_month",
            entity_id=month,
            scope=AccessScope.finance_month(month),
            reason=reason,
            details={
                "month": month,
                "channel_count": len(result.channels),
                "component_count": len(components),
                "net_total_usd": str(result.net_total_usd),
            },
        )
        merged = list(result.warnings) + outside_warnings
        return _with_warnings(result, merged)

    def _gather_channel_gross(
        self, month: str
    ) -> tuple[dict[str, Decimal], dict[str, str]]:
        """Select each channel's primary gross fact using source priority."""
        facts_by_channel: dict[str, list] = defaultdict(list)
        primary: dict[str, str] = {}
        for fact in self._facts.list_month_facts(month=month):
            facts_by_channel[fact.youtube_channel_id].append(fact)

        gross: dict[str, Decimal] = {}
        for channel, facts in facts_by_channel.items():
            primary_fact = min(
                facts,
                key=lambda fact: (
                    SOURCE_PRIORITY.get(fact.source_kind, 99),
                    fact.source_kind,
                ),
            )
            gross[channel] = primary_fact.gross_revenue_usd
            primary[channel] = primary_fact.source_kind
        return gross, primary

    def _gather_adsense_total(self, month: str) -> Decimal | None:
        """Sum PAID USD AdSense payment amounts for the month (None when absent)."""
        repo = SqlAlchemyAdSensePaymentRepository(
            self._session, tenant_id=self._tenant_id
        )
        payments = [
            pay for pay in repo.list_month_payments(month=month)
            if pay.payment_status == "PAID" and pay.payment_currency == "USD"
        ]
        if not payments:
            return None
        return sum((pay.payment_amount for pay in payments), Decimal("0"))

    def _gather_bank_totals(self, month: str) -> tuple[Decimal | None, Decimal]:
        """Sum bank receipts (None when absent) and FX differences for the month."""
        repo = SqlAlchemyBankReconciliationRepository(
            self._session, tenant_id=self._tenant_id
        )
        entries = repo.list_month_entries(month=month)
        if not entries:
            return None, Decimal("0")
        bank_total = sum(
            (entry.bank_received_amount_usd for entry in entries), Decimal("0")
        )
        fx_total = sum((entry.fx_difference_usd for entry in entries), Decimal("0"))
        return bank_total, fx_total

    def _build_components(
        self,
        month: str,
        result: MonthReconciliationResult,
        primary_source: dict[str, str],
    ) -> list[DeductionComponentInput]:
        """Map non-zero per-channel hops to typed deduction-component inputs."""
        components: list[DeductionComponentInput] = []
        for line in result.channels:
            source_system = _source_system_for_source_kind(
                primary_source.get(line.youtube_channel_id, "")
            )
            for kind, amount, suffix in _channel_hops(line):
                if amount == 0:
                    continue
                components.append(
                    self._component(month, line, kind, amount, suffix, source_system)
                )
        return components

    def _component(
        self,
        month: str,
        line: ChannelReconciliation,
        component_kind: str,
        amount: Decimal,
        suffix: str,
        source_system: str,
    ) -> DeductionComponentInput:
        """Build one CHANNEL-scoped reconciliation deduction-component input."""
        channel = line.youtube_channel_id
        return DeductionComponentInput(
            component_kind=component_kind,
            scope_kind="CHANNEL",
            scope_id=channel,
            amount_usd=amount,
            amount_native=amount,
            currency_code="USD",
            source_system=source_system,
            source_table=_SOURCE_TABLE,
            source_id=channel,
            source_key=f"recon:{month}:{channel}:{suffix}",
            source_report_id=None,
            raw_payload={"hop": suffix},
            component_key=f"recon:{month}:{channel}:{suffix}",
        )

    def _attribute_outside_cms(
        self, *, month: str, actor: UserPrincipal
    ) -> list[dict[str, str]]:
        """Write ALLOCATION facts for OUTSIDE_CMS channels mapped 1:1 to a PAID account.

        For each PAID AdSense account, resolve verified account->channel links to
        OUTSIDE_CMS channels that have no existing source gross fact. When exactly
        one such channel maps, record its account total as an ALLOCATION revenue
        fact; when several no-gross channels map, skip (fail-closed) and warn.
        Stale ALLOCATION facts from prior runs are deleted when the current
        payment/link/channel state no longer authorizes them.
        """
        warnings: list[dict[str, str]] = []
        outside = self._outside_cms_channels()
        if not outside:
            return warnings

        month_facts = self._facts.list_month_facts(month=month)
        source_gross_channels = {
            fact.youtube_channel_id
            for fact in month_facts
            if fact.source_kind != RevenueFactSourceKind.ALLOCATION.value
        }
        existing_allocation_channels = {
            fact.youtube_channel_id
            for fact in month_facts
            if fact.source_kind == RevenueFactSourceKind.ALLOCATION.value
            and fact.youtube_channel_id in outside
        }
        repo = SqlAlchemyAdSensePaymentRepository(
            self._session, tenant_id=self._tenant_id
        )
        paid_by_account: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for pay in repo.list_month_payments(month=month):
            if pay.payment_status == "PAID" and pay.payment_currency == "USD":
                paid_by_account[pay.source_account_id] += pay.payment_amount
        current_allocation_channels: set[str] = set()
        for account, total in paid_by_account.items():
            channels = [
                channel
                for channel in self._links.list_verified_adsense_account_channels(
                    tenant_id=self._tenant_id, month=month, adsense_account_id=account
                )
                if channel in outside and channel not in source_gross_channels
            ]
            if len(channels) == 1:
                channel = channels[0]
                current_allocation_channels.add(channel)
                self._record_allocation_fact(month, channel, total, actor)
            elif len(channels) > 1:
                warnings.append(
                    {
                        "code": "MISSING_REVENUE_SOURCE",
                        "message": (
                            f"Account {account} maps to multiple OUTSIDE_CMS "
                            "channels without gross; allocation skipped"
                        ),
                    }
                )
        stale_channels = existing_allocation_channels - current_allocation_channels
        if stale_channels:
            self._facts.delete_month_facts(
                month=month,
                source_kind=RevenueFactSourceKind.ALLOCATION.value,
                youtube_channel_ids=stale_channels,
            )
        return warnings

    def _record_allocation_fact(
        self, month: str, channel: str, gross: Decimal, actor: UserPrincipal
    ) -> None:
        """Record a 1:1 ALLOCATION gross fact for an outside-CMS channel."""
        self._facts.record_fact(
            month=month,
            youtube_channel_id=channel,
            source_kind=RevenueFactSourceKind.ALLOCATION.value,
            source_report_id=None,
            gross_revenue_usd=gross,
            net_revenue_usd=None,
            views=0,
            watch_time_minutes=Decimal("0"),
            confidence_score=_ALLOCATION_CONFIDENCE,
            actor_user_id=actor.user_id,
        )

    def _outside_cms_channels(self) -> set[str]:
        """Return the set of active OUTSIDE_CMS YouTube channel IDs for the tenant."""
        rows = self._session.scalars(
            select(YouTubeChannelORM.youtube_channel_id).where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.cms_status == "OUTSIDE_CMS",
                YouTubeChannelORM.active.is_(True),
            )
        ).all()
        return set(rows)


def _channel_hops(
    line: ChannelReconciliation,
) -> tuple[tuple[str, Decimal, str], ...]:
    """Return the (component_kind, amount, key_suffix) tuples for one channel."""
    return (
        ("TAX", line.us_tax_usd, "us_tax"),
        ("TRANSFER_FEE", line.yt_adsense_fee_usd, "yt_adsense_fee"),
        ("TRANSFER_FEE", line.adsense_bank_fee_usd, "adsense_bank_fee"),
        ("FX_VARIANCE", line.fx_variance_usd, "fx_variance"),
    )


def _with_warnings(
    result: MonthReconciliationResult, warnings: list[dict[str, str]]
) -> MonthReconciliationResult:
    """Return a copy of the result carrying the merged warning list."""
    return MonthReconciliationResult(
        month=result.month,
        channels=result.channels,
        gross_total_usd=result.gross_total_usd,
        us_tax_total_usd=result.us_tax_total_usd,
        yt_adsense_fee_total_usd=result.yt_adsense_fee_total_usd,
        adsense_bank_fee_total_usd=result.adsense_bank_fee_total_usd,
        fx_total_usd=result.fx_total_usd,
        net_total_usd=result.net_total_usd,
        yt_adsense_fee_pct=result.yt_adsense_fee_pct,
        warnings=warnings,
    )
