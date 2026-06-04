#!/usr/bin/env python
"""Stand up one fully-populated, LOCKED demo finance month for the MVP.

Idempotent operator seed: org hierarchy + channels + revenue facts + an
ACCOUNT-scoped deduction + a verified account->channel map + a committed
account-allocation snapshot + a LOCKED finance-month close, so the dashboard
read paths (net-revenue, account-allocation, explain, exports, finance-close)
all return real data served from the committed snapshot.

The script orchestrates the existing service/repository layer; it never opens
its own engine/transaction semantics beyond the app's own
``build_session_factory`` and never reimplements finance math.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PATH = str(_PROJECT_ROOT / "backend")

# Deterministic UUIDs derived from a fixed namespace so re-running the seed
# (and seeding multiple months) yields stable, collision-free identity rows.
_DEMO_NAMESPACE = UUID("00000000-0000-0000-0000-0000deadbeef")
_DEFAULT_MONTH = "2026-03"
_COMMIT_IDEMPOTENCY_PREFIX = "demo-seed-commit"
_COMMIT_REASON = "demo month seed close"

_DEMO_COMMITTER_EMAIL = "demo-seed@ums.local"
_DEMO_GATEWAY_TOKEN = "demo-trusted-gateway-token"

# FIX: Keep project imports lazy so direct script execution can adjust sys.path
# before importing the backend package (mirrors scripts/run_deduction_ingestion.py).
_deps: Any = None


@dataclass(frozen=True)
class _ChannelSpec:
    """One demo channel: its public id, name, and source-aligned ADSENSE money.

    ``net_revenue_usd`` is ``None`` for a deliberately missing-net channel so the
    demo exercises BOTH net paths: CALCULATED (source net present) AND the
    account-allocated missing-net derivation, giving a richer PARTIAL response.
    """

    youtube_channel_id: str
    channel_name: str
    gross_revenue_usd: Decimal
    net_revenue_usd: Decimal | None
    views: int


# A real distribution: three channels with distinct gross weights so the
# account-level deduction allocates proportionally across more than one channel.
# Charlie has NO source net, so the account-allocated deduction derives its net
# on the missing-net path (the surface the net-revenue dashboard column shows).
_CHANNELS: tuple[_ChannelSpec, ...] = (
    _ChannelSpec("demo-channel-alpha", "Demo Alpha", Decimal("6000.00"), Decimal("5100.00"), 1_500_000),
    _ChannelSpec("demo-channel-bravo", "Demo Bravo", Decimal("3000.00"), Decimal("2550.00"), 720_000),
    _ChannelSpec("demo-channel-charlie", "Demo Charlie", Decimal("1000.00"), None, 240_000),
)
_ACCOUNT_ID = "demo-pub-1"
_CONTENT_OWNER_ID = "demo-owner-1"
_ACCOUNT_DEDUCTION_USD = Decimal("400.00")


def _ensure_backend_path() -> None:
    """Make the local backend package importable for direct script execution."""
    if _BACKEND_PATH not in sys.path:
        sys.path.insert(0, _BACKEND_PATH)


def _load_dependencies() -> Any:
    """Import the backend symbols this script orchestrates, once."""
    global _deps
    if _deps is not None:
        return _deps
    _ensure_backend_path()

    from ums_smart_revenue.config.settings import load_app_settings
    from ums_smart_revenue.db.explanation_models import ExplanationBase
    from ums_smart_revenue.db.finance_models import (
        AdsenseContentOwnerLinkORM,
        ContentOwnerChannelLinkORM,
        DeductionComponentORM,
        FinanceBase,
        FinanceMonthCloseORM,
        MonthlyChannelRevenueFactORM,
    )
    from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
    from ums_smart_revenue.db.report_models import ReportBase
    from ums_smart_revenue.db.security_models import SecurityBase, UserORM
    from ums_smart_revenue.db.session import build_session_factory
    from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
    from ums_smart_revenue.finance.channel_account_links import (
        SqlAlchemyChannelAccountLinkRepository,
    )
    from ums_smart_revenue.finance.committed_allocation import (
        CommittedAllocationValidationError,
        SqlAlchemyCommittedAllocationRepository,
    )
    from ums_smart_revenue.finance.deduction_ingestion import (
        SqlAlchemyDeductionComponentRepository,
    )
    from ums_smart_revenue.finance.month_close import (
        FinanceMonthCloseReadinessError,
        SqlAlchemyFinanceMonthCloseRepository,
    )
    from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
    from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

    _deps = {
        "load_app_settings": load_app_settings,
        "build_session_factory": build_session_factory,
        "FinanceBase": FinanceBase,
        "OrgBase": OrgBase,
        "SecurityBase": SecurityBase,
        "TenantBase": TenantBase,
        "ExplanationBase": ExplanationBase,
        "ReportBase": ReportBase,
        "TenantORM": TenantORM,
        "OrgUnitORM": OrgUnitORM,
        "YouTubeChannelORM": YouTubeChannelORM,
        "MonthlyChannelRevenueFactORM": MonthlyChannelRevenueFactORM,
        "DeductionComponentORM": DeductionComponentORM,
        "AdsenseContentOwnerLinkORM": AdsenseContentOwnerLinkORM,
        "ContentOwnerChannelLinkORM": ContentOwnerChannelLinkORM,
        "UserORM": UserORM,
        "FinanceMonthCloseORM": FinanceMonthCloseORM,
        "SqlAlchemyChannelAccountLinkRepository": SqlAlchemyChannelAccountLinkRepository,
        "SqlAlchemyCommittedAllocationRepository": SqlAlchemyCommittedAllocationRepository,
        "CommittedAllocationValidationError": CommittedAllocationValidationError,
        "SqlAlchemyDeductionComponentRepository": SqlAlchemyDeductionComponentRepository,
        "SqlAlchemyRevenueFactRepository": SqlAlchemyRevenueFactRepository,
        "SqlAlchemyFinanceMonthCloseRepository": SqlAlchemyFinanceMonthCloseRepository,
        "FinanceMonthCloseReadinessError": FinanceMonthCloseReadinessError,
        "UMS_TENANT_ID": UMS_TENANT_ID,
    }
    return _deps


def _demo_uuid(*parts: str) -> UUID:
    """Return a deterministic UUID for a logical seed row identity."""
    return uuid5(_DEMO_NAMESPACE, "|".join(parts))


def _create_schema(engine: Any, deps: dict[str, Any]) -> None:
    """Create the metadata families the demo read paths touch if absent (SQLite path).

    On a fresh SQLite file the app's Alembic schema is not bootstrapped, so the
    seed creates the same metadata the tests create (TenantBase + OrgBase +
    SecurityBase + FinanceBase) plus ExplanationBase (the explain endpoint
    persists to number_explanations) plus ReportBase (the exports read path the
    docstring promises queries export_jobs; without it GET /exports 500s on a
    fresh SQLite file). ``create_all`` is a safe no-op for tables that already
    exist (the Postgres/Alembic path), so this stays idempotent.
    """
    deps["TenantBase"].metadata.create_all(engine)
    deps["OrgBase"].metadata.create_all(engine)
    deps["SecurityBase"].metadata.create_all(engine)
    deps["FinanceBase"].metadata.create_all(engine)
    deps["ExplanationBase"].metadata.create_all(engine)
    # FIX: Create ReportBase too. The module docstring lists "exports" as a
    # supported dashboard read path, but export_jobs (ReportBase) was never
    # created here, so GET /exports raised "no such table: export_jobs" on a
    # fresh SQLite seed. Creating it makes the promised exports read path return
    # an empty (200) list instead of 500.
    deps["ReportBase"].metadata.create_all(engine)


# ============================================================================
# Purpose: Idempotently seed every org/security/finance row one demo month
#   needs, then commit + lock the account allocation so LOCKED-month readers
#   serve the committed snapshot. Each insert is guarded by an existence check,
#   so re-running mutates nothing and never duplicates.
# Database/ORM: tenants, org_units, youtube_channels, monthly_channel_revenue_facts,
#   deduction_components, adsense_content_owner_links, content_owner_channel_links,
#   users, finance_month_close (writes); committed-allocation tables via the repo.
# Standards: orchestration only (finance math owned by the committed-allocation
#   service); typed; no bare except; shared request-style session per step.
# Blast Radius: Demo/seed data only (single tenant, single month). No auth
#   weakening, no schema/migration change, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/committed_allocation.py -> commit.
#   - File: backend/ums_smart_revenue/finance/month_close.py -> lock service.
#   - File: tests/api/test_committed_allocation_api.py -> proven _seed pattern.
# ============================================================================
def _seed_substrate(
    session: Any, *, month: str, tenant_id: UUID, allocation_method: str, deps: dict[str, Any]
) -> dict[str, Any]:
    """Insert (idempotently) the tenant, org, channels, facts, deduction, map, user, close row.

    ``allocation_method`` controls the missing-net channel: under the default
    gross method, Charlie keeps NO source net so the demo exercises the
    account-allocated missing-net derivation. Under post_tax_revenue_proportional
    the net basis OMITS any null-net (channel, source) pair, which would leave the
    account unable to fully allocate (a rejected commit), so every channel gets a
    source net derived from its gross to keep the post_tax commit zero-unallocated.
    """
    post_tax = allocation_method == "post_tax_revenue_proportional"
    summary: dict[str, Any] = {"created": [], "reused": []}

    def _mark(kind: str, created: bool) -> None:
        summary["created" if created else "reused"].append(kind)

    # Tenant (FK parent for deduction/link/committed-run rows).
    tenant_orm = deps["TenantORM"]
    if session.get(tenant_orm, tenant_id) is None:
        session.add(
            tenant_orm(
                id=tenant_id, slug="ums", display_name="UMS",
                primary_currency="USD", status="ACTIVE",
            )
        )
        _mark("tenant", True)
    else:
        _mark("tenant", False)
    session.flush()

    # Org hierarchy: SECTOR -> COMPANY.
    org_orm = deps["OrgUnitORM"]
    sector_id = _demo_uuid("org", "sector")
    company_id = _demo_uuid("org", "company")
    if session.get(org_orm, sector_id) is None:
        session.add(org_orm(id=sector_id, parent_id=None, type="SECTOR", name="Demo Sector", active=True))
        _mark("sector", True)
    else:
        _mark("sector", False)
    session.flush()
    if session.get(org_orm, company_id) is None:
        session.add(
            org_orm(id=company_id, parent_id=sector_id, type="COMPANY", name="Demo Company", active=True)
        )
        _mark("company", True)
    else:
        _mark("company", False)
    session.flush()

    # Committer user (committed_by / imported_by).
    user_orm = deps["UserORM"]
    user_id = _demo_uuid("user", "committer")
    if session.get(user_orm, user_id) is None:
        session.add(
            user_orm(id=user_id, email=_DEMO_COMMITTER_EMAIL, display_name="Demo Seed Committer")
        )
        _mark("user", True)
    else:
        _mark("user", False)
    session.flush()

    # Channels (flush before facts: facts FK -> youtube_channels).
    channel_orm = deps["YouTubeChannelORM"]
    seeded_channels: list[str] = []
    for spec in _CHANNELS:
        row_id = _demo_uuid("channel", spec.youtube_channel_id)
        if session.get(channel_orm, row_id) is None:
            session.add(
                channel_orm(
                    id=row_id, tenant_id=tenant_id,
                    youtube_channel_id=spec.youtube_channel_id,
                    channel_name=spec.channel_name, primary_org_unit_id=company_id,
                    cms_status="INSIDE_CMS", revenue_required=True, active=True,
                )
            )
            _mark(f"channel:{spec.youtube_channel_id}", True)
        else:
            _mark(f"channel:{spec.youtube_channel_id}", False)
        seeded_channels.append(spec.youtube_channel_id)
    session.flush()

    # Monthly revenue facts (ADSENSE). Net is source-populated except for the
    # deliberate missing-net channel under the gross method; post_tax derives a
    # net for every channel so its net basis can fully allocate the account.
    fact_orm = deps["MonthlyChannelRevenueFactORM"]
    for spec in _CHANNELS:
        net_revenue_usd = spec.net_revenue_usd
        if net_revenue_usd is None and post_tax:
            # 85% margin mirrors the populated channels (5100/6000, 2550/3000).
            net_revenue_usd = (spec.gross_revenue_usd * Decimal("0.85")).quantize(Decimal("0.01"))
        if not _fact_exists(session, fact_orm, tenant_id=tenant_id, month=month,
                            youtube_channel_id=spec.youtube_channel_id, source_kind="ADSENSE"):
            session.add(
                fact_orm(
                    id=_demo_uuid("fact", month, spec.youtube_channel_id, "ADSENSE"),
                    tenant_id=tenant_id, month=month,
                    youtube_channel_id=spec.youtube_channel_id, source_kind="ADSENSE",
                    source_report_id=f"demo-adsense-{month}",
                    gross_revenue_usd=spec.gross_revenue_usd,
                    net_revenue_usd=net_revenue_usd,
                    views=spec.views, watch_time_minutes=Decimal("0"),
                    confidence_score=Decimal("0.9500"), imported_by=user_id,
                )
            )
            _mark(f"fact:{spec.youtube_channel_id}", True)
        else:
            _mark(f"fact:{spec.youtube_channel_id}", False)
    session.flush()

    # ACCOUNT-scoped deduction component (the thing the allocation distributes).
    deduction_orm = deps["DeductionComponentORM"]
    component_key = f"demo-srcrow:adsense_management:{_ACCOUNT_ID}:{month}"
    if not _deduction_exists(session, deduction_orm, tenant_id=tenant_id, component_key=component_key):
        session.add(
            deduction_orm(
                id=_demo_uuid("deduction", month, _ACCOUNT_ID),
                tenant_id=tenant_id, month=month, component_kind="DEDUCTION",
                scope_kind="ACCOUNT", scope_id=_ACCOUNT_ID,
                amount_usd=_ACCOUNT_DEDUCTION_USD, amount_native=None, currency_code="USD",
                source_system="adsense_management", source_table="google_revenue_source_rows",
                source_id=None, source_key=f"demo-{_ACCOUNT_ID}", source_report_id=None,
                raw_payload={"demo": True}, component_key=component_key,
            )
        )
        _mark("account_deduction", True)
    else:
        _mark("account_deduction", False)
    session.flush()

    # Verified account->owner link (the money-gating trust decision).
    adsense_link_orm = deps["AdsenseContentOwnerLinkORM"]
    if not _adsense_link_exists(session, adsense_link_orm, tenant_id=tenant_id,
                               adsense_account_id=_ACCOUNT_ID, content_owner_id=_CONTENT_OWNER_ID):
        session.add(
            adsense_link_orm(
                id=_demo_uuid("adsense-link", _ACCOUNT_ID, _CONTENT_OWNER_ID),
                tenant_id=tenant_id, adsense_account_id=_ACCOUNT_ID,
                content_owner_id=_CONTENT_OWNER_ID, verification_status="VERIFIED",
                provenance_kind="OPERATOR_ASSERTED", provenance_payload={"demo": True},
                effective_month_start="2026-01",
            )
        )
        _mark("adsense_owner_link", True)
    else:
        _mark("adsense_owner_link", False)

    # Active owner->channel links (one per channel = a real distribution).
    owner_link_orm = deps["ContentOwnerChannelLinkORM"]
    for spec in _CHANNELS:
        if not _owner_channel_link_exists(
            session, owner_link_orm, tenant_id=tenant_id,
            content_owner_id=_CONTENT_OWNER_ID, youtube_channel_id=spec.youtube_channel_id,
        ):
            session.add(
                owner_link_orm(
                    id=_demo_uuid("owner-link", _CONTENT_OWNER_ID, spec.youtube_channel_id),
                    tenant_id=tenant_id, content_owner_id=_CONTENT_OWNER_ID,
                    youtube_channel_id=spec.youtube_channel_id, provenance_kind="SOURCE_ROW",
                    active=True, effective_month_start="2026-01",
                )
            )
            _mark(f"owner_channel_link:{spec.youtube_channel_id}", True)
        else:
            _mark(f"owner_channel_link:{spec.youtube_channel_id}", False)
    session.flush()

    # Finance-month close control row (OPEN initially; lock step flips it).
    close_orm = deps["FinanceMonthCloseORM"]
    if session.get(close_orm, (tenant_id, month)) is None:
        session.add(
            close_orm(tenant_id=tenant_id, month=month, status="OPEN", allocation_rule_payload={})
        )
        _mark("finance_month_close", True)
    else:
        _mark("finance_month_close", False)
    session.flush()

    summary["channels"] = seeded_channels
    summary["committer_user_id"] = str(user_id)
    return summary


def _fact_exists(session: Any, fact_orm: Any, *, tenant_id: UUID, month: str,
                youtube_channel_id: str, source_kind: str) -> bool:
    """Return whether a revenue fact already exists for the unique business key."""
    from sqlalchemy import select

    return session.scalar(
        select(fact_orm.id).where(
            fact_orm.tenant_id == tenant_id, fact_orm.month == month,
            fact_orm.youtube_channel_id == youtube_channel_id, fact_orm.source_kind == source_kind,
        )
    ) is not None


def _deduction_exists(session: Any, deduction_orm: Any, *, tenant_id: UUID, component_key: str) -> bool:
    """Return whether a deduction component already exists for (tenant, component_key)."""
    from sqlalchemy import select

    return session.scalar(
        select(deduction_orm.id).where(
            deduction_orm.tenant_id == tenant_id, deduction_orm.component_key == component_key,
        )
    ) is not None


def _adsense_link_exists(session: Any, link_orm: Any, *, tenant_id: UUID,
                        adsense_account_id: str, content_owner_id: str) -> bool:
    """Return whether the VERIFIED account->owner link already exists for the unique key."""
    from sqlalchemy import select

    return session.scalar(
        select(link_orm.id).where(
            link_orm.tenant_id == tenant_id, link_orm.adsense_account_id == adsense_account_id,
            link_orm.content_owner_id == content_owner_id, link_orm.effective_month_start == "2026-01",
        )
    ) is not None


def _owner_channel_link_exists(session: Any, link_orm: Any, *, tenant_id: UUID,
                              content_owner_id: str, youtube_channel_id: str) -> bool:
    """Return whether the owner->channel link already exists for the unique key."""
    from sqlalchemy import select

    return session.scalar(
        select(link_orm.id).where(
            link_orm.tenant_id == tenant_id, link_orm.content_owner_id == content_owner_id,
            link_orm.youtube_channel_id == youtube_channel_id, link_orm.effective_month_start == "2026-01",
        )
    ) is not None


def _commit_allocation(
    session: Any, *, month: str, tenant_id: UUID, committer_user_id: str,
    allocation_method: str, deps: dict[str, Any],
) -> dict[str, Any]:
    """Commit (or idempotently replay) the account-allocation snapshot for the month."""
    committed_repo = deps["SqlAlchemyCommittedAllocationRepository"](session, tenant_id=tenant_id)
    deduction_repo = deps["SqlAlchemyDeductionComponentRepository"](session, tenant_id=tenant_id)
    revenue_repo = deps["SqlAlchemyRevenueFactRepository"](session, tenant_id=tenant_id)
    link_repo = deps["SqlAlchemyChannelAccountLinkRepository"](session, tenant_id=tenant_id)

    # Stable, month+method-scoped idempotency key + matching fingerprint so a
    # re-run replays the same single run rather than creating a new version.
    idempotency_key = f"{_COMMIT_IDEMPOTENCY_PREFIX}:{month}:{allocation_method}"
    fingerprint = _demo_uuid("commit-fp", month, allocation_method, _COMMIT_REASON).hex

    outcome = committed_repo.commit_allocation(
        month=month, allocation_method=allocation_method,
        idempotency_key=idempotency_key, request_fingerprint=fingerprint,
        reason=_COMMIT_REASON, committed_by=committer_user_id,
        deduction_repository=deduction_repo, revenue_repository=revenue_repo,
        link_repository=link_repo,
    )
    session.flush()
    run = outcome.run
    return {
        "created": outcome.created,
        "run_id": str(run.id),
        "commit_version": run.commit_version,
        "allocation_method": run.allocation_method,
        "idempotency_key": idempotency_key,
        "allocated_total_usd": str(run.allocated_total_usd),
        "net_applicable_total_usd": str(run.net_applicable_total_usd),
        "allocated_component_count": run.allocated_component_count,
        "unallocated_component_count": run.unallocated_component_count,
        "line_count": len(outcome.lines),
    }


def _lock_month(session: Any, *, month: str, tenant_id: UUID, committer_user_id: str,
                deps: dict[str, Any]) -> dict[str, Any]:
    """Lock the month via the close service; fall back to a direct status flip if readiness blocks.

    The faithful path is SqlAlchemyFinanceMonthCloseRepository.lock_month, which
    runs the real lock-time readiness recheck. A minimal demo month has
    single-source channels (an INSUFFICIENT_SOURCES reconciliation blocker), so
    that recheck legitimately refuses; when it does, the seed flips the close row
    to LOCKED directly (the tests' _lock_month pattern) and reports the path so
    the operator knows the demo bypassed the production readiness gate.
    """
    close_orm = deps["FinanceMonthCloseORM"]
    row = session.get(close_orm, (tenant_id, month))
    if row is not None and row.status == "LOCKED":
        return {"status": "LOCKED", "lock_path": "already_locked"}

    repo = deps["SqlAlchemyFinanceMonthCloseRepository"](session, tenant_id=tenant_id)
    try:
        entry = repo.lock_month(month=month, actor_user_id=committer_user_id)
        session.flush()
        return {"status": entry.status, "lock_path": "lock_service"}
    except deps["FinanceMonthCloseReadinessError"] as exc:
        blocker_types = [b.blocker_type for b in exc.readiness.blockers]
        row = session.get(close_orm, (tenant_id, month))
        row.status = "LOCKED"
        row.locked_by = UUID(committer_user_id)
        session.flush()
        return {
            "status": "LOCKED",
            "lock_path": "direct_status_flip (readiness gate not met for demo data)",
            "readiness_blockers": blocker_types,
        }


def _demo_principal_headers(*, committer_user_id: str, gateway_token: str | None) -> dict[str, str]:
    """Return the trusted-gateway headers an operator/Vite proxy would send."""
    return {
        "X-User-Id": committer_user_id,
        "X-User-Email": _DEMO_COMMITTER_EMAIL,
        "X-Role": "finance_admin",
        "X-Scope-Type": "global",
        "X-UMS-Trusted-Gateway-Token": gateway_token or _DEMO_GATEWAY_TOKEN,
    }


def _print_summary(
    *, database_url: str, month: str, tenant_id: UUID, tenant_slug: str,
    seed_summary: dict[str, Any], commit_summary: dict[str, Any],
    lock_summary: dict[str, Any], headers: dict[str, str], gateway_token_set: bool,
) -> None:
    """Print a clear, operator-facing summary of the seeded demo month."""
    print("=" * 72)
    print("UMS Smart Revenue — demo month seed complete")
    print("=" * 72)
    print(f"database_url        : {database_url}")
    print(f"tenant_id           : {tenant_id}")
    print(f"tenant_slug         : {tenant_slug}")
    print(f"month               : {month}")
    print(f"channels            : {', '.join(seed_summary['channels'])}")
    print(f"rows_created        : {seed_summary['created'] or '(none — idempotent re-run)'}")
    print(f"rows_reused         : {seed_summary['reused'] or '(none)'}")
    print("-" * 72)
    print("committed allocation:")
    print(f"  created           : {commit_summary['created']} (False = idempotent replay)")
    print(f"  run_id            : {commit_summary['run_id']}")
    print(f"  commit_version    : {commit_summary['commit_version']}")
    print(f"  allocation_method : {commit_summary['allocation_method']}")
    print(f"  idempotency_key   : {commit_summary['idempotency_key']}")
    print(f"  allocated_total   : {commit_summary['allocated_total_usd']} USD")
    print(f"  net_applicable    : {commit_summary['net_applicable_total_usd']} USD")
    print(f"  allocated_lines   : {commit_summary['line_count']}")
    print(f"  unallocated_count : {commit_summary['unallocated_component_count']}")
    print("-" * 72)
    print("finance-month lock  :")
    print(f"  status            : {lock_summary['status']}")
    print(f"  lock_path         : {lock_summary['lock_path']}")
    if "readiness_blockers" in lock_summary:
        print(f"  readiness_blockers: {lock_summary['readiness_blockers']}")
    print("-" * 72)
    print("demo principal headers (Vite dev proxy / operator curl):")
    for key, value in headers.items():
        print(f"  {key}: {value}")
    if not gateway_token_set:
        print("  NOTE: UMS_TRUSTED_GATEWAY_TOKEN is not set in this environment.")
        print("        Set it (and use the same value above) before the API will accept these headers.")
    print("=" * 72)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse operator CLI arguments for the demo-month seed."""
    parser = argparse.ArgumentParser(
        description="Seed one fully-populated, LOCKED demo finance month (idempotent).",
    )
    parser.add_argument(
        "--database-url", default=None,
        help="SQLAlchemy URL. Defaults to UMS_DATABASE_URL from settings.",
    )
    parser.add_argument("--month", default=_DEFAULT_MONTH, help=f"Finance month YYYY-MM (default {_DEFAULT_MONTH}).")
    parser.add_argument(
        "--tenant", default=None, type=UUID,
        help="Tenant UUID (default: the bootstrap UMS tenant).",
    )
    parser.add_argument(
        "--allocation-method", default="gross_revenue_proportional",
        choices=["gross_revenue_proportional", "post_tax_revenue_proportional"],
        help="Committed-allocation method (default gross_revenue_proportional).",
    )
    parser.add_argument(
        "--create-schema", action="store_true",
        help="Create metadata tables first (needed for a fresh SQLite file).",
    )
    return parser.parse_args(argv)


def _validate_month(month: str) -> None:
    """Reject a malformed month before any DB work."""
    valid = (
        len(month) == 7 and month[4] == "-" and month[:4].isdigit()
        and month[5:].isdigit() and 1 <= int(month[5:7]) <= 12
    )
    if not valid:
        raise ValueError("--month must use YYYY-MM with a calendar month from 01 to 12")


# ============================================================================
# Purpose: Operator entrypoint. Resolve DB + session the app's way, create the
#   schema when asked (fresh SQLite), seed the demo month, commit the account
#   allocation, lock the month, and print the operator summary + demo headers.
# Database/ORM: One app session_factory session per logical step; all SQL owned
#   by the seeded ORMs / committed-allocation + close repositories.
# Standards: thin entrypoint; operator ValueError / commit ValidationError ->
#   exit 2; no secrets/tokens beyond the demo header value printed.
# Blast Radius: Demo/seed data only. No auth/finance-math/schema change.
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> build_session_factory.
#   - File: backend/ums_smart_revenue/finance/committed_allocation.py -> commit.
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Seed the demo month end-to-end and return the operator exit code."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        _validate_month(args.month)
    except ValueError as exc:
        print(f"ValueError: {exc!s}", file=sys.stderr)
        return 2

    deps = _load_dependencies()
    try:
        settings = deps["load_app_settings"]()
    except ValueError:
        print("ValueError: invalid operator settings", file=sys.stderr)
        return 2

    database_url = args.database_url or settings.database_url
    if not database_url:
        print(
            "A database URL is required: pass --database-url or set UMS_DATABASE_URL.",
            file=sys.stderr,
        )
        return 2

    tenant_id = args.tenant or UUID(deps["UMS_TENANT_ID"])
    session_factory = deps["build_session_factory"](database_url)
    engine = session_factory.kw["bind"]

    if args.create_schema:
        _create_schema(engine, deps)

    print(f"Seeding demo month {args.month} into {database_url} (tenant {tenant_id})...")

    try:
        with session_factory() as session:
            seed_summary = _seed_substrate(
                session, month=args.month, tenant_id=tenant_id,
                allocation_method=args.allocation_method, deps=deps,
            )
            commit_summary = _commit_allocation(
                session, month=args.month, tenant_id=tenant_id,
                committer_user_id=seed_summary["committer_user_id"],
                allocation_method=args.allocation_method, deps=deps,
            )
            lock_summary = _lock_month(
                session, month=args.month, tenant_id=tenant_id,
                committer_user_id=seed_summary["committer_user_id"], deps=deps,
            )
            session.commit()
    except deps["CommittedAllocationValidationError"] as exc:
        # The compute rejected the snapshot (e.g. an unmapped account). Roll back
        # is automatic (session context manager); surface a stable operator error.
        print(f"CommittedAllocationValidationError: {exc!s}", file=sys.stderr)
        return 2

    headers = _demo_principal_headers(
        committer_user_id=seed_summary["committer_user_id"],
        gateway_token=settings.trusted_gateway_token,
    )
    _print_summary(
        database_url=database_url, month=args.month, tenant_id=tenant_id, tenant_slug="ums",
        seed_summary=seed_summary, commit_summary=commit_summary, lock_summary=lock_summary,
        headers=headers, gateway_token_set=bool(settings.trusted_gateway_token),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
