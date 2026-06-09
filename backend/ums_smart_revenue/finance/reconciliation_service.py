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
    AdSensePaymentEntry,
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
    DeductionComponentLockedMonthError,
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.explanations import (
    REVENUE_RECONCILIATION_METRIC,
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
    RevenueFactEntry,
    RevenueFactLockedMonthError,
    RevenueFactSourceKind,
    SqlAlchemyRevenueFactRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_SOURCE_TABLE = "reconciliation_workflow"
_SOURCE_SYSTEM = "reconciliation"
_MANUAL_UPLOAD_SOURCE_SYSTEM = "manual_upload"
_ALLOCATION_SOURCE_REPORT_ID = f"{_SOURCE_TABLE}:outside_cms_allocation"
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
_ALLOCATION_CONFIDENCE = Decimal("0.80")
_LOCKED_WRITE_ERRORS = (DeductionComponentLockedMonthError, RevenueFactLockedMonthError)

# Inverse of SOURCE_SYSTEM_TO_SOURCE_KIND: pick a source_system whose mapping a
# reconciliation channel-direct component must adopt so the net-revenue resolver
# (_applicable_deduction_components) treats it as source-aligned to the channel's
# primary fact source_kind. Only net-applicable kinds (TAX) actually reduce net;
# the others carry the channel's source_system for provenance consistency.
_SOURCE_KIND_TO_SOURCE_SYSTEM: dict[RevenueFactSourceKind, str] = {
    RevenueFactSourceKind.YOUTUBE_CMS: "youtube_reporting",
    RevenueFactSourceKind.ADSENSE: "adsense_management",
    RevenueFactSourceKind.YOUTUBE_ANALYTICS: "youtube_analytics",
    RevenueFactSourceKind.MANUAL_UPLOAD: _MANUAL_UPLOAD_SOURCE_SYSTEM,
    RevenueFactSourceKind.ALLOCATION: _SOURCE_SYSTEM,
}


def _source_system_for_source_kind(source_kind: str) -> str:
    """Return the deduction source_system aligned to a revenue fact source_kind."""
    try:
        normalized_source_kind = RevenueFactSourceKind(source_kind)
    except ValueError:
        return _SOURCE_SYSTEM
    return _SOURCE_KIND_TO_SOURCE_SYSTEM.get(normalized_source_kind, _SOURCE_SYSTEM)


def _is_reconciliation_allocation_fact(fact: RevenueFactEntry) -> bool:
    """Return True only for ALLOCATION facts owned by this reconciliation service."""
    return (
        fact.source_kind == RevenueFactSourceKind.ALLOCATION.value
        and fact.source_report_id == _ALLOCATION_SOURCE_REPORT_ID
    )


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
        #   revenue as ALLOCATION facts, gathers source-backed gross + verified
        #   AdSense + bank evidence, runs the pure compute core, persists
        #   per-channel deduction_components (replacing prior reconciliation rows)
        #   and reconciliation explanations, then records one summary-only
        #   REVENUE_RECONCILED audit event.
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

        try:
            (
                outside_warnings,
                adsense_total,
                bank_basis_scoped,
            ) = self._attribute_outside_cms(
                month=month, actor=actor
            )

            channel_gross, primary_source = self._gather_channel_gross(month)
            bank_total, fx_total = self._gather_bank_totals(month)
            precompute_warnings = list(outside_warnings)
            if not bank_basis_scoped and bank_total is not None:
                precompute_warnings.append(
                    {
                        "code": "BANK_BASIS_UNSCOPED",
                        "message": (
                            "Bank receipts are month-level while AdSense basis "
                            "excluded outside-CMS or unmapped payments; bank "
                            "fee/FX not derived"
                        ),
                    }
                )
                bank_total = None
                fx_total = Decimal("0")
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
            merged_warnings = list(result.warnings) + precompute_warnings

            components = self._build_components(month, result, primary_source)
            self._deductions.upsert_components(
                month=month,
                components=components,
                replace_source_tables={_SOURCE_TABLE},
            )
            live_channels = {line.youtube_channel_id for line in result.channels}
            self._explanations.delete_month_metric_explanations_except(
                month=month,
                entity_type="channel",
                metric=REVENUE_RECONCILIATION_METRIC,
                keep_entity_ids=live_channels,
            )
            for line in result.channels:
                self._explanations.record_explanation(
                    build_reconciliation_explanation(
                        month=month, line=line, warnings=merged_warnings,
                    )
                )
        # FIX: Repository write guards also run under row locks; if a month locks
        # after the service preflight, normalize those races to the API's 409 path.
        except _LOCKED_WRITE_ERRORS as exc:
            raise MonthLockedError(f"Finance month {month!r} is locked") from exc

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
        return _with_warnings(result, merged_warnings)

    def _gather_channel_gross(
        self, month: str
    ) -> tuple[dict[str, Decimal], dict[str, str]]:
        """Select source-backed primary gross facts using source priority."""
        facts_by_channel: dict[str, list] = defaultdict(list)
        primary: dict[str, str] = {}
        for fact in self._facts.list_month_facts(month=month):
            if _is_reconciliation_allocation_fact(fact):
                continue
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

    def _list_paid_payments(self, month: str) -> list[AdSensePaymentEntry]:
        """Return PAID AdSense payments for a month."""
        repo = SqlAlchemyAdSensePaymentRepository(
            self._session, tenant_id=self._tenant_id
        )
        return [
            pay for pay in repo.list_month_payments(month=month)
            if pay.payment_status == "PAID"
        ]

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
    ) -> tuple[list[dict[str, str]], Decimal | None, bool]:
        """Write ALLOCATION facts and return outside-CMS attribution results.

        For each PAID AdSense account, resolve verified account->channel links.
        Payments that map wholly to source-backed channels feed the residual
        YouTube->AdSense total. Payments that map exactly 1:1 to one active
        OUTSIDE_CMS channel with no source gross are persisted as pass-through
        ALLOCATION facts and excluded from residual math. Unmapped, mixed, or
        ambiguous no-gross mappings are skipped fail-closed and warned. Stale
        reconciliation-owned ALLOCATION facts from prior runs are deleted when
        the current state no longer authorizes them. The returned bool is false
        when the AdSense basis is not safe for month-level bank/FX derivation.
        """
        warnings: list[dict[str, str]] = []
        bank_basis_scoped = True
        outside = self._outside_cms_channels()
        month_facts = self._facts.list_month_facts(month=month)
        source_gross_channels = {
            fact.youtube_channel_id
            for fact in month_facts
            if fact.source_kind != RevenueFactSourceKind.ALLOCATION.value
            or not _is_reconciliation_allocation_fact(fact)
        }
        # FIX: Scope stale cleanup to reconciliation-owned ALLOCATION facts. A
        # connector-loaded ALLOCATION fact is source evidence, not a derived row
        # this workflow may delete or overwrite.
        existing_allocation_channels = {
            fact.youtube_channel_id
            for fact in month_facts
            if _is_reconciliation_allocation_fact(fact)
        }
        paid_by_account: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for pay in self._list_paid_payments(month):
            currency = str(pay.payment_currency or "").upper()
            if currency != "USD":
                # FIX: Bank receipts are month-level USD rows. If reconciliation
                # skips a paid non-USD AdSense payment, bank fee/FX attribution
                # is no longer source-scoped to the computed AdSense basis.
                bank_basis_scoped = False
                warnings.append(
                    {
                        "code": "UNSUPPORTED_PAYMENT_CURRENCY",
                        "message": (
                            f"Account {pay.source_account_id} has a PAID "
                            f"{currency or 'unknown-currency'} payment; "
                            "payment excluded from USD reconciliation"
                        ),
                    }
                )
                continue
            paid_by_account[pay.source_account_id] += pay.payment_amount
        allocation_by_channel: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        residual_adsense_total = Decimal("0")
        residual_account_count = 0
        for account, total in paid_by_account.items():
            channels = set(
                self._links.list_verified_adsense_account_channels(
                    tenant_id=self._tenant_id, month=month, adsense_account_id=account
                )
            )
            if not channels:
                bank_basis_scoped = False
                warnings.append(
                    {
                        "code": "MISSING_REVENUE_SOURCE",
                        "message": (
                            f"Account {account} has no verified channel mapping; "
                            "payment excluded from reconciliation"
                        ),
                    }
                )
                continue

            outside_no_gross = (channels & outside) - source_gross_channels
            unknown_no_gross = channels - outside - source_gross_channels
            if len(channels) == 1 and len(outside_no_gross) == 1:
                channel = next(iter(outside_no_gross))
                allocation_by_channel[channel] += total
                bank_basis_scoped = False
                continue

            if not outside_no_gross and not unknown_no_gross:
                residual_adsense_total += total
                residual_account_count += 1
                continue

            bank_basis_scoped = False
            warnings.append(
                {
                    "code": "MISSING_REVENUE_SOURCE",
                    "message": (
                        f"Account {account} does not resolve to a single "
                        "source-backed or 1:1 OUTSIDE_CMS channel; payment "
                        "excluded from reconciliation"
                    ),
                }
            )

        current_allocation_channels = set(allocation_by_channel)
        for channel, total in allocation_by_channel.items():
            self._record_allocation_fact(month, channel, total, actor)
        stale_channels = existing_allocation_channels - current_allocation_channels
        if stale_channels:
            self._facts.delete_month_facts(
                month=month,
                source_kind=RevenueFactSourceKind.ALLOCATION.value,
                youtube_channel_ids=stale_channels,
            )
        if residual_account_count == 0:
            return warnings, None, bank_basis_scoped
        return warnings, residual_adsense_total, bank_basis_scoped

    def _record_allocation_fact(
        self, month: str, channel: str, gross: Decimal, actor: UserPrincipal
    ) -> None:
        """Record a 1:1 ALLOCATION gross fact for an outside-CMS channel."""
        self._facts.record_fact(
            month=month,
            youtube_channel_id=channel,
            source_kind=RevenueFactSourceKind.ALLOCATION.value,
            source_report_id=_ALLOCATION_SOURCE_REPORT_ID,
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
