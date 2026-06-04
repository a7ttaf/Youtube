#!/usr/bin/env python
"""Stand up one populated demo finance month for the MVP dashboard.

Idempotent operator seed: org hierarchy + channels + revenue facts + an
ACCOUNT-scoped deduction + a verified account->channel map + a committed
account-allocation snapshot + a finance-month close row, so the dashboard read
paths (net-revenue, account-allocation, explain, exports, finance-close) all
return real data served from the committed snapshot.

Lock behavior: by default the seed asks the production lock service to close the
month. A minimal demo month has single-source channels, so the real lock-time
readiness recheck legitimately refuses (an INSUFFICIENT_SOURCES reconciliation
blocker). When it refuses the seed leaves the month OPEN, prints the blocker
types, and exits 0 — it never silently forges a LOCKED status that the
production gate would reject. Pass ``--demo-lock-bypass`` to force a direct
status flip on a disposable demo database; that flip writes NO audit event and
bypasses the production readiness gate, so it is for demo databases only.

Effective-month boundaries: the AdSense owner link and owner->channel links use
the requested ``--month`` as their effective_month_start so seeding an earlier
month stays allocatable (the link repo filters effective_month_start <= month).
Both link kinds also fold ``--month`` into their deterministic row id, so the
same database can be seeded for MULTIPLE months without a duplicate-PK collision
while same-month re-runs remain idempotent. Demo databases seeded by an OLDER
version of this script (which hardcoded a 2026-01 effective_month_start, or used
a month-free link id) should be recreated fresh.

Allocation method on a FRESH vs EXISTING database: both advertised methods seed
on a FRESH database. Under the default gross method the demo deliberately leaves
one channel with NO source net (it exercises the account-allocated missing-net
derivation). Under ``--allocation-method post_tax_revenue_proportional`` that
channel is seeded WITH a derived source net, because the post-tax net basis omits
any null-net (channel, source) pair and an incomplete basis would reject the
commit. Switching methods on an EXISTING database is NOT supported: the seed
never rewrites already-persisted finance facts to fit a different method, so a
gross-seeded database re-run under post_tax fails the commit (incomplete net
basis -> unallocated). That case exits non-zero with a clear message to recreate
the demo database fresh for the requested method.

The script orchestrates the existing service/repository layer; it never opens
its own engine/transaction semantics beyond the app's own
``build_session_factory`` and never reimplements finance math.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
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
    _ChannelSpec(
        "demo-channel-alpha", "Demo Alpha",
        Decimal("6000.00"), Decimal("5100.00"), 1_500_000,
    ),
    _ChannelSpec(
        "demo-channel-bravo", "Demo Bravo",
        Decimal("3000.00"), Decimal("2550.00"), 720_000,
    ),
    _ChannelSpec("demo-channel-charlie", "Demo Charlie", Decimal("1000.00"), None, 240_000),
)
_ACCOUNT_ID = "demo-pub-1"
_CONTENT_OWNER_ID = "demo-owner-1"
_ACCOUNT_DEDUCTION_USD = Decimal("400.00")


def _ensure_backend_path() -> None:
    """Make the local backend package importable for direct script execution."""
    if _BACKEND_PATH not in sys.path:
        sys.path.insert(0, _BACKEND_PATH)


@lru_cache(maxsize=1)
def _load_dependencies() -> dict[str, Any]:
    """Import the backend symbols this script orchestrates, once (memoized)."""
    # FIX: Memoize via lru_cache instead of a module-level ``global _deps``,
    # preserving single-import semantics without the global statement.
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
        CommittedAllocationLockedMonthError,
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

    return {
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
        "CommittedAllocationLockedMonthError": CommittedAllocationLockedMonthError,
        "SqlAlchemyDeductionComponentRepository": SqlAlchemyDeductionComponentRepository,
        "SqlAlchemyRevenueFactRepository": SqlAlchemyRevenueFactRepository,
        "SqlAlchemyFinanceMonthCloseRepository": SqlAlchemyFinanceMonthCloseRepository,
        "FinanceMonthCloseReadinessError": FinanceMonthCloseReadinessError,
        "UMS_TENANT_ID": UMS_TENANT_ID,
    }


def _demo_uuid(*parts: str) -> UUID:
    """Return a deterministic UUID for a logical seed row identity."""
    return uuid5(_DEMO_NAMESPACE, "|".join(parts))


def _demo_tenant_uuid(tenant_id: UUID, *parts: str) -> UUID:
    """Return a deterministic, TENANT-SCOPED UUID for a per-tenant seed row.

    The bare ``_demo_uuid`` derives an id from the logical parts ONLY, so two
    tenants seeded into the SAME database produced identical ids for their
    channels, org units, facts, deductions, links, committer user, and commit
    fingerprint — colliding on the (global) primary keys. Folding the tenant id
    into the derivation gives each tenant its own deterministic id space while
    same-tenant re-runs stay idempotent.
    """
    # FIX: per-tenant demo rows used tenant-independent ids, so seeding a second
    # --tenant into an already-seeded database raised a UNIQUE/PK IntegrityError
    # (e.g. monthly_channel_revenue_facts.id). The tenant id now discriminates the
    # id, so multiple tenants coexist in one demo database without collision.
    return _demo_uuid(tenant_id.hex, *parts)


# ============================================================================
# Purpose: Resolve the tenant slug the seed writes for a given tenant id so a
#   non-default tenant never collides with the bootstrap UMS tenant's "ums" slug.
# Database/ORM: tenants (slug; uq_tenants_slug UNIQUE + ck_tenants_slug_lower
#   require slug == lower(slug)).
# Standards: pure, typed; no DB access. Returns an all-lowercase slug.
# Blast Radius: Demo/seed data only — the slug value a new tenant row receives.
# Connections:
#   - File: backend/ums_smart_revenue/db/tenant_models.py -> TenantORM slug
#     constraints (uq_tenants_slug, ck_tenants_slug_lower).
#   - File: backend/ums_smart_revenue/tenancy/constants.py -> UMS_TENANT_ID.
# ============================================================================
def _tenant_slug_for(tenant_id: UUID, *, default_tenant_id: UUID) -> str:
    """Return the slug to seed for ``tenant_id``.

    The foundation migration already seeds the default UMS tenant with slug
    ``"ums"``, so re-using that slug for any OTHER tenant id violates
    ``uq_tenants_slug``. The default tenant keeps ``"ums"``; every non-default
    tenant gets a deterministic, tenant-specific slug ``demo-<first-8-uuid-hex>``
    (all lowercase hex, satisfying ``ck_tenants_slug_lower``) so a custom
    ``--tenant`` on an already-migrated database inserts a distinct slug.
    """
    if tenant_id == default_tenant_id:
        return "ums"
    return f"demo-{tenant_id.hex[:8]}"


def _redact_database_url(database_url: str) -> str:
    """Return ``database_url`` with any password in the userinfo masked.

    A PostgreSQL URL (``postgresql+psycopg://user:secret@host:5432/db``) carries
    a real password in its userinfo, so echoing it verbatim in the operator
    summary leaks a credential. This keeps scheme, host, port, path, query, and
    the username intact while replacing the password with ``***``. SQLite URLs
    (and any URL without a password) are returned unchanged.
    """
    # FIX: The operator summary echoed the full database_url, leaking the
    # PostgreSQL password in its userinfo. Rebuild the URL via urllib.parse with
    # the password masked so the printed value is safe to share.
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(database_url)
    if parts.password is None:
        return database_url
    username = parts.username or ""
    host = parts.hostname or ""
    userinfo = f"{username}:***"
    netloc = f"{userinfo}@{host}" if host else f"{userinfo}@"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


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
    session: Any, *, month: str, tenant_id: UUID, tenant_slug: str,
    allocation_method: str, deps: dict[str, Any],
) -> dict[str, Any]:
    """Insert (idempotently) the tenant, org, channels, facts, deduction, map, user, close row.

    ``tenant_slug`` is the (already-resolved) slug to seed for ``tenant_id`` —
    ``"ums"`` for the default tenant, a tenant-specific ``demo-<hex>`` slug
    otherwise — so a custom ``--tenant`` cannot collide with the bootstrap UMS
    tenant's ``"ums"`` slug.

    ``allocation_method`` controls the missing-net channel: under the default
    gross method, Charlie keeps NO source net so the demo exercises the
    account-allocated missing-net derivation. Under post_tax_revenue_proportional
    the net basis OMITS any null-net (channel, source) pair, which would leave the
    account unable to fully allocate (a rejected commit), so every channel gets a
    source net derived from its gross to keep the post_tax commit zero-unallocated.

    The per-entity inserts are delegated to focused ``_seed_*`` helpers so this
    orchestrator stays a flat, low-complexity sequence of guarded seed steps.
    """
    post_tax = allocation_method == "post_tax_revenue_proportional"
    summary: dict[str, Any] = {"created": [], "reused": []}
    mark = _summary_marker(summary)

    _seed_tenant(session, deps, mark, tenant_id=tenant_id, tenant_slug=tenant_slug)
    company_id = _seed_org_hierarchy(session, deps, mark, tenant_id=tenant_id)
    user_id = _seed_committer_user(session, deps, mark, tenant_id=tenant_id)
    seeded_channels = _seed_channels(
        session, deps, mark, tenant_id=tenant_id, company_id=company_id
    )
    _seed_revenue_facts(
        session, deps, mark, tenant_id=tenant_id, month=month,
        user_id=user_id, post_tax=post_tax,
    )
    _seed_account_deduction(session, deps, mark, tenant_id=tenant_id, month=month)
    _seed_account_links(session, deps, mark, tenant_id=tenant_id, month=month)
    _seed_finance_close_row(session, deps, mark, tenant_id=tenant_id, month=month)

    summary["channels"] = seeded_channels
    summary["committer_user_id"] = str(user_id)
    return summary


def _summary_marker(summary: dict[str, Any]) -> Callable[[str, bool], None]:
    """Return a closure that records each seeded row as created or reused."""

    def _mark(kind: str, created: bool) -> None:
        """Append ``kind`` to the created or reused list per the insert outcome."""
        summary["created" if created else "reused"].append(kind)

    return _mark


def _seed_tenant(
    session: Any, deps: dict[str, Any], mark: Callable[[str, bool], None],
    *, tenant_id: UUID, tenant_slug: str,
) -> None:
    """Insert the tenant FK parent (idempotent) and flush.

    The lookup is by primary key, so re-running with the SAME ``tenant_id``
    reuses the existing row (idempotent). ``tenant_slug`` is seeded only when a
    new row is created and, for a non-default tenant, is a tenant-specific
    ``demo-<hex>`` value (see ``_tenant_slug_for``) so a custom ``--tenant`` on an
    already-migrated database does not collide with the bootstrap UMS tenant's
    ``"ums"`` slug under ``uq_tenants_slug``.
    """
    # FIX: the slug was hardcoded to "ums" for EVERY tenant id. On an
    # already-migrated database the foundation migration already owns the "ums"
    # slug, so a non-default --tenant inserted a second row with the same slug and
    # raised an IntegrityError on uq_tenants_slug. The slug is now derived per
    # tenant id (default -> "ums", otherwise demo-<first-8-uuid-hex>).
    tenant_orm = deps["TenantORM"]
    if session.get(tenant_orm, tenant_id) is None:
        session.add(
            tenant_orm(
                id=tenant_id, slug=tenant_slug, display_name="UMS",
                primary_currency="USD", status="ACTIVE",
            )
        )
        mark("tenant", True)
    else:
        mark("tenant", False)
    session.flush()


def _seed_org_hierarchy(
    session: Any, deps: dict[str, Any], mark: Callable[[str, bool], None],
    *, tenant_id: UUID,
) -> UUID:
    """Insert the SECTOR -> COMPANY org units (idempotent); return the company id."""
    org_orm = deps["OrgUnitORM"]
    sector_id = _demo_tenant_uuid(tenant_id, "org", "sector")
    company_id = _demo_tenant_uuid(tenant_id, "org", "company")
    if session.get(org_orm, sector_id) is None:
        session.add(
            org_orm(
                id=sector_id, tenant_id=tenant_id, parent_id=None, type="SECTOR",
                name="Demo Sector", active=True,
            )
        )
        mark("sector", True)
    else:
        mark("sector", False)
    session.flush()
    if session.get(org_orm, company_id) is None:
        session.add(
            org_orm(
                id=company_id, tenant_id=tenant_id, parent_id=sector_id, type="COMPANY",
                name="Demo Company", active=True,
            )
        )
        mark("company", True)
    else:
        mark("company", False)
    session.flush()
    return company_id


def _seed_committer_user(
    session: Any, deps: dict[str, Any], mark: Callable[[str, bool], None],
    *, tenant_id: UUID,
) -> UUID:
    """Insert the committer user (committed_by / imported_by); return its id.

    The user id is tenant-scoped so seeding a second tenant into the same demo
    database does not collide on the (global) users primary key. The email index
    (``uq_users_email_lower``) is already tenant-scoped, so the per-tenant user
    keeps the shared demo email without collision.
    """
    user_orm = deps["UserORM"]
    user_id = _demo_tenant_uuid(tenant_id, "user", "committer")
    if session.get(user_orm, user_id) is None:
        session.add(
            user_orm(
                id=user_id, tenant_id=tenant_id,
                email=_DEMO_COMMITTER_EMAIL, display_name="Demo Seed Committer",
            )
        )
        mark("user", True)
    else:
        mark("user", False)
    session.flush()
    return user_id


def _seed_channels(
    session: Any, deps: dict[str, Any], mark: Callable[[str, bool], None],
    *, tenant_id: UUID, company_id: UUID,
) -> list[str]:
    """Insert each demo channel (idempotent); return the seeded public ids.

    Flushes before facts because monthly_channel_revenue_facts FK -> youtube_channels.
    """
    channel_orm = deps["YouTubeChannelORM"]
    seeded_channels: list[str] = []
    for spec in _CHANNELS:
        row_id = _demo_tenant_uuid(tenant_id, "channel", spec.youtube_channel_id)
        if session.get(channel_orm, row_id) is None:
            session.add(
                channel_orm(
                    id=row_id, tenant_id=tenant_id,
                    youtube_channel_id=spec.youtube_channel_id,
                    channel_name=spec.channel_name, primary_org_unit_id=company_id,
                    cms_status="INSIDE_CMS", revenue_required=True, active=True,
                )
            )
            mark(f"channel:{spec.youtube_channel_id}", True)
        else:
            mark(f"channel:{spec.youtube_channel_id}", False)
        seeded_channels.append(spec.youtube_channel_id)
    session.flush()
    return seeded_channels


def _seed_revenue_facts(
    session: Any, deps: dict[str, Any], mark: Callable[[str, bool], None],
    *, tenant_id: UUID, month: str, user_id: UUID, post_tax: bool,
) -> None:
    """Insert one ADSENSE revenue fact per channel (idempotent).

    Net is source-populated except for the deliberate missing-net channel under
    the gross method; post_tax derives a net for every channel so its net basis
    can fully allocate the account.
    """
    fact_orm = deps["MonthlyChannelRevenueFactORM"]
    for spec in _CHANNELS:
        net_revenue_usd = spec.net_revenue_usd
        if net_revenue_usd is None and post_tax:
            # 85% margin mirrors the populated channels (5100/6000, 2550/3000).
            net_revenue_usd = (
                spec.gross_revenue_usd * Decimal("0.85")
            ).quantize(Decimal("0.01"))
        if _fact_exists(
            session, fact_orm, tenant_id=tenant_id, month=month,
            youtube_channel_id=spec.youtube_channel_id, source_kind="ADSENSE",
        ):
            mark(f"fact:{spec.youtube_channel_id}", False)
            continue
        session.add(
            fact_orm(
                id=_demo_tenant_uuid(tenant_id, "fact", month, spec.youtube_channel_id, "ADSENSE"),
                tenant_id=tenant_id, month=month,
                youtube_channel_id=spec.youtube_channel_id, source_kind="ADSENSE",
                source_report_id=f"demo-adsense-{month}",
                gross_revenue_usd=spec.gross_revenue_usd,
                net_revenue_usd=net_revenue_usd,
                views=spec.views, watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("0.9500"), imported_by=user_id,
            )
        )
        mark(f"fact:{spec.youtube_channel_id}", True)
    session.flush()


def _seed_account_deduction(
    session: Any, deps: dict[str, Any], mark: Callable[[str, bool], None],
    *, tenant_id: UUID, month: str,
) -> None:
    """Insert the ACCOUNT-scoped deduction the allocation distributes (idempotent)."""
    deduction_orm = deps["DeductionComponentORM"]
    component_key = f"demo-srcrow:adsense_management:{_ACCOUNT_ID}:{month}"
    if not _deduction_exists(
        session, deduction_orm, tenant_id=tenant_id, component_key=component_key
    ):
        session.add(
            deduction_orm(
                id=_demo_tenant_uuid(tenant_id, "deduction", month, _ACCOUNT_ID),
                tenant_id=tenant_id, month=month, component_kind="DEDUCTION",
                scope_kind="ACCOUNT", scope_id=_ACCOUNT_ID,
                amount_usd=_ACCOUNT_DEDUCTION_USD, amount_native=None, currency_code="USD",
                source_system="adsense_management", source_table="google_revenue_source_rows",
                source_id=None, source_key=f"demo-{_ACCOUNT_ID}", source_report_id=None,
                raw_payload={"demo": True}, component_key=component_key,
            )
        )
        mark("account_deduction", True)
    else:
        mark("account_deduction", False)
    session.flush()


def _seed_account_links(
    session: Any, deps: dict[str, Any], mark: Callable[[str, bool], None],
    *, tenant_id: UUID, month: str,
) -> None:
    """Insert the verified account->owner link and active owner->channel links.

    Both link kinds set effective_month_start to the requested ``month`` so the
    link repo (which filters effective_month_start <= month) can still resolve
    them when seeding a month earlier than the old hardcoded 2026-01. The same
    ``month`` is used both in the uniqueness filters AND in each row's
    deterministic id, so re-runs stay idempotent per month while seeding a
    SECOND month allocates a distinct PK (no duplicate-PK collision). Demo DBs
    seeded by the old 2026-01 version should be recreated fresh.
    """
    # Verified account->owner link (the money-gating trust decision).
    adsense_link_orm = deps["AdsenseContentOwnerLinkORM"]
    if not _adsense_link_exists(
        session, adsense_link_orm, tenant_id=tenant_id,
        adsense_account_id=_ACCOUNT_ID, content_owner_id=_CONTENT_OWNER_ID, month=month,
    ):
        session.add(
            adsense_link_orm(
                # FIX: Include ``month`` in the deterministic id. The existence
                # check is month-scoped (effective_month_start == month) but the
                # id was not, so seeding a SECOND month re-used the SAME PK and
                # raised a duplicate-PK error before allocation. Each month's
                # link now owns a distinct id; same-month re-runs stay idempotent.
                id=_demo_tenant_uuid(
                    tenant_id, "adsense-link", month, _ACCOUNT_ID, _CONTENT_OWNER_ID
                ),
                tenant_id=tenant_id, adsense_account_id=_ACCOUNT_ID,
                content_owner_id=_CONTENT_OWNER_ID, verification_status="VERIFIED",
                provenance_kind="OPERATOR_ASSERTED", provenance_payload={"demo": True},
                effective_month_start=month,
            )
        )
        mark("adsense_owner_link", True)
    else:
        mark("adsense_owner_link", False)

    # Active owner->channel links (one per channel = a real distribution).
    owner_link_orm = deps["ContentOwnerChannelLinkORM"]
    for spec in _CHANNELS:
        if _owner_channel_link_exists(
            session, owner_link_orm, tenant_id=tenant_id,
            content_owner_id=_CONTENT_OWNER_ID,
            youtube_channel_id=spec.youtube_channel_id, month=month,
        ):
            mark(f"owner_channel_link:{spec.youtube_channel_id}", False)
            continue
        session.add(
            owner_link_orm(
                # FIX: Same month-scoping mismatch as the adsense link above —
                # include ``month`` so each effective_month_start gets a distinct
                # deterministic id and multi-month seeding cannot collide on the PK.
                id=_demo_tenant_uuid(
                    tenant_id, "owner-link", month, _CONTENT_OWNER_ID, spec.youtube_channel_id
                ),
                tenant_id=tenant_id, content_owner_id=_CONTENT_OWNER_ID,
                youtube_channel_id=spec.youtube_channel_id, provenance_kind="SOURCE_ROW",
                active=True, effective_month_start=month,
            )
        )
        mark(f"owner_channel_link:{spec.youtube_channel_id}", True)
    session.flush()


def _seed_finance_close_row(
    session: Any, deps: dict[str, Any], mark: Callable[[str, bool], None],
    *, tenant_id: UUID, month: str,
) -> None:
    """Insert the finance-month close control row (OPEN; lock step flips it)."""
    close_orm = deps["FinanceMonthCloseORM"]
    if session.get(close_orm, (tenant_id, month)) is None:
        session.add(
            close_orm(tenant_id=tenant_id, month=month, status="OPEN", allocation_rule_payload={})
        )
        mark("finance_month_close", True)
    else:
        mark("finance_month_close", False)
    session.flush()


def _fact_exists(
    session: Any, fact_orm: Any, *, tenant_id: UUID, month: str,
    youtube_channel_id: str, source_kind: str,
) -> bool:
    """Return whether a revenue fact already exists for the unique business key."""
    from sqlalchemy import select

    return session.scalar(
        select(fact_orm.id).where(
            fact_orm.tenant_id == tenant_id, fact_orm.month == month,
            fact_orm.youtube_channel_id == youtube_channel_id,
            fact_orm.source_kind == source_kind,
        )
    ) is not None


def _deduction_exists(
    session: Any, deduction_orm: Any, *, tenant_id: UUID, component_key: str,
) -> bool:
    """Return whether a deduction component already exists for (tenant, component_key)."""
    from sqlalchemy import select

    return session.scalar(
        select(deduction_orm.id).where(
            deduction_orm.tenant_id == tenant_id,
            deduction_orm.component_key == component_key,
        )
    ) is not None


def _adsense_link_exists(
    session: Any, link_orm: Any, *, tenant_id: UUID,
    adsense_account_id: str, content_owner_id: str, month: str,
) -> bool:
    """Return whether the VERIFIED account->owner link already exists for the unique key.

    The uniqueness filter pins effective_month_start to the requested ``month``
    so each seeded month owns its own link row and re-runs stay idempotent.
    """
    from sqlalchemy import select

    return session.scalar(
        select(link_orm.id).where(
            link_orm.tenant_id == tenant_id,
            link_orm.adsense_account_id == adsense_account_id,
            link_orm.content_owner_id == content_owner_id,
            link_orm.effective_month_start == month,
        )
    ) is not None


def _owner_channel_link_exists(
    session: Any, link_orm: Any, *, tenant_id: UUID,
    content_owner_id: str, youtube_channel_id: str, month: str,
) -> bool:
    """Return whether the owner->channel link already exists for the unique key.

    The uniqueness filter pins effective_month_start to the requested ``month``
    so each seeded month owns its own link row and re-runs stay idempotent.
    """
    from sqlalchemy import select

    return session.scalar(
        select(link_orm.id).where(
            link_orm.tenant_id == tenant_id,
            link_orm.content_owner_id == content_owner_id,
            link_orm.youtube_channel_id == youtube_channel_id,
            link_orm.effective_month_start == month,
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
                demo_lock_bypass: bool, deps: dict[str, Any]) -> dict[str, Any]:
    """Lock the month via the production close service; honor the readiness gate.

    The faithful path is SqlAlchemyFinanceMonthCloseRepository.lock_month, which
    runs the real lock-time readiness recheck. A minimal demo month has
    single-source channels (an INSUFFICIENT_SOURCES reconciliation blocker), so
    that recheck legitimately refuses.

    Default behavior on refusal: do NOT forge a LOCKED status. The month is left
    OPEN, the blocker types are returned to the operator, and the run still exits
    0 — the seed never bypasses the production readiness gate behind the
    operator's back.

    Only when ``demo_lock_bypass`` is True (the explicit ``--demo-lock-bypass``
    flag) does the seed restore the direct status flip, with a loud warning. That
    flip writes NO audit event and bypasses the production readiness gate, so it
    must only run against a disposable demo database.
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
        if not demo_lock_bypass:
            # FIX: The previous branch flipped status to LOCKED directly on a
            # readiness refusal, forging a LOCKED state the production lock gate
            # would never grant (no audit, partial fields). Default behavior now
            # leaves the month OPEN and surfaces the blockers instead.
            return {
                "status": "OPEN",
                "lock_path": (
                    "production_readiness_gate_refused"
                    " (month left OPEN; rerun with --demo-lock-bypass to force)"
                ),
                "readiness_blockers": blocker_types,
            }
        # --demo-lock-bypass: explicit, disposable-demo-only direct status flip.
        print(
            "WARNING: --demo-lock-bypass forced a direct LOCKED status flip;"
            " this BYPASSES the production readiness gate, writes NO audit event,"
            " and must ONLY be used on a disposable demo database."
        )
        row = session.get(close_orm, (tenant_id, month))
        row.status = "LOCKED"
        row.locked_by = UUID(committer_user_id)
        session.flush()
        return {
            "status": "LOCKED",
            "lock_path": "demo_lock_bypass_direct_flip (production readiness gate bypassed)",
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
    """Print a clear, operator-facing summary of the seeded demo month.

    The database_url is printed through ``_redact_database_url`` so a PostgreSQL
    password never appears in the summary; the trusted-gateway token is shown as
    ``<redacted>``.
    """
    print("=" * 72)
    print("UMS Smart Revenue — demo month seed complete")
    print("=" * 72)
    # FIX: Redact any password in the database_url userinfo before printing — a
    # PostgreSQL URL carries a real credential that must not land in the summary.
    print(f"database_url        : {_redact_database_url(database_url)}")
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
        # FIX: Never echo the trusted-gateway token value. When
        # UMS_TRUSTED_GATEWAY_TOKEN is set the header carries a real secret, so
        # print a redaction + source hint instead of the value.
        if key == "X-UMS-Trusted-Gateway-Token":
            source = (
                "set via UMS_TRUSTED_GATEWAY_TOKEN"
                if gateway_token_set
                else "built-in demo default"
            )
            print(f"  {key}: <redacted> ({source})")
            continue
        print(f"  {key}: {value}")
    if not gateway_token_set:
        print("  NOTE: UMS_TRUSTED_GATEWAY_TOKEN is not set in this environment.")
        print("        Set it (and use the same value above) before the API")
        print("        will accept these headers.")
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
    parser.add_argument(
        "--month", default=_DEFAULT_MONTH,
        help=f"Finance month YYYY-MM (default {_DEFAULT_MONTH}).",
    )
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
    parser.add_argument(
        "--demo-lock-bypass", action="store_true",
        help=(
            "When the production lock service refuses on readiness, force a direct"
            " LOCKED status flip. Bypasses the readiness gate, writes NO audit"
            " event; disposable demo databases ONLY."
        ),
    )
    return parser.parse_args(argv)


def _validate_month(month: str) -> None:
    """Reject a malformed month before any DB work.

    Raises:
        ValueError: when ``month`` is not YYYY-MM, or its month component falls
            outside the calendar range 01-12.
    """
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

    default_tenant_id = UUID(deps["UMS_TENANT_ID"])
    tenant_id = args.tenant or default_tenant_id
    tenant_slug = _tenant_slug_for(tenant_id, default_tenant_id=default_tenant_id)
    session_factory = deps["build_session_factory"](database_url)
    engine = session_factory.kw["bind"]

    if args.create_schema:
        _create_schema(engine, deps)

    safe_database_url = _redact_database_url(database_url)
    print(f"Seeding demo month {args.month} into {safe_database_url} (tenant {tenant_id})...")

    try:
        with session_factory() as session:
            seed_summary = _seed_substrate(
                session, month=args.month, tenant_id=tenant_id, tenant_slug=tenant_slug,
                allocation_method=args.allocation_method, deps=deps,
            )
            commit_summary = _commit_allocation(
                session, month=args.month, tenant_id=tenant_id,
                committer_user_id=seed_summary["committer_user_id"],
                allocation_method=args.allocation_method, deps=deps,
            )
            lock_summary = _lock_month(
                session, month=args.month, tenant_id=tenant_id,
                committer_user_id=seed_summary["committer_user_id"],
                demo_lock_bypass=args.demo_lock_bypass, deps=deps,
            )
            session.commit()
    except deps["CommittedAllocationValidationError"] as exc:
        # FIX: The compute rejected the snapshot. The dominant operator cause is
        # a method-switch on an EXISTING demo DB: facts seeded under one method
        # carry a net shape the requested method's basis cannot fully cover (e.g.
        # the gross-seeded missing-net channel leaves the post_tax net basis
        # INCOMPLETE -> unallocated -> rejected). We never silently rewrite
        # already-seeded finance facts to fit the new method; instead we surface
        # an actionable message telling the operator to recreate the demo DB
        # fresh for the requested method. Roll back is automatic (session context
        # manager). Exit 2 matches this script's operator-error convention.
        print(f"CommittedAllocationValidationError: {exc!s}", file=sys.stderr)
        print(
            "This demo database's finance facts are incompatible with"
            f" --allocation-method {args.allocation_method}.",
            file=sys.stderr,
        )
        print(
            "The seed does NOT mutate already-seeded finance facts to fit a"
            " different method. Recreate the demo database fresh for this method"
            " (a new SQLite file, or a fresh --create-schema target) and re-run.",
            file=sys.stderr,
        )
        return 2
    except deps["CommittedAllocationLockedMonthError"] as exc:
        # FIX: A method-switch on a demo DB whose month is ALREADY LOCKED produces
        # a new (method-scoped) idempotency key, so the commit cannot replay the
        # existing run and the locked-month guard rejects it. Previously this
        # raised an unhandled traceback; catch the typed rejection and tell the
        # operator to recreate the demo DB fresh for the requested method. Exit 2
        # matches this script's operator-error convention.
        print(f"CommittedAllocationLockedMonthError: {exc!s}", file=sys.stderr)
        print(
            f"Finance month {args.month} is already LOCKED under a previous"
            f" commit, so --allocation-method {args.allocation_method} cannot be"
            " committed on this database.",
            file=sys.stderr,
        )
        print(
            "Recreate the demo database fresh for this method (a new SQLite file,"
            " or a fresh --create-schema target) and re-run.",
            file=sys.stderr,
        )
        return 2

    headers = _demo_principal_headers(
        committer_user_id=seed_summary["committer_user_id"],
        gateway_token=settings.trusted_gateway_token,
    )
    _print_summary(
        database_url=database_url, month=args.month, tenant_id=tenant_id, tenant_slug=tenant_slug,
        seed_summary=seed_summary, commit_summary=commit_summary, lock_summary=lock_summary,
        headers=headers, gateway_token_set=bool(settings.trusted_gateway_token),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
