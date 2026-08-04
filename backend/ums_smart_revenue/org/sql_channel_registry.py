# ============================================================================
# Purpose: SQL-backed channel registry scoped to one tenant — reads and writes
#   YouTubeChannelORM rows for the channel APIs and the bulk inventory import.
# Database/ORM: YouTubeChannelORM (read/write); MonthlyChannelRevenueFactORM +
#   FinanceMonthCloseORM (read-only locked-month guards).
# Standards: Typed domain errors at every boundary; tenant-scoped queries;
#   locked-month guards fail closed (update_mapping re-parenting and the
#   update_inventory revenue_required flip both refuse to rewrite the
#   conditions a LOCKED finance month was finalized under).
# Blast Radius: Channel registry inventory/mapping, finance close integrity
#   via the guards. No allocation math, no exports, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> route boundary.
#   - File: backend/ums_smart_revenue/org/channel_registry.py -> entry
#     dataclass, protocol, and shared derivation/normalization helpers.
# ============================================================================
"""SQL-backed tenant-scoped channel registry."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.finance.month_close_locks import (
    REVENUE_REQUIREMENT_GUARD_MONTH,
    acquire_finance_month_advisory_lock,
)
from ums_smart_revenue.org.channel_registry import (
    ChannelMappingLockedMonthError,
    ChannelRegistryConflictError,
    ChannelRegistryEntry,
    ChannelRegistryValidationError,
    ChannelRevenueRequirementLockedMonthError,
    derive_revenue_source_status,
    normalize_optional_content_owner,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
_SQLITE_TENANT_CHANNEL_UNIQUE_ERROR = (
    "unique constraint failed: youtube_channels.tenant_id, youtube_channels.youtube_channel_id"
)


class SqlAlchemyChannelRegistry:
    """SQL-backed channel registry scoped to a single tenant."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def list_channels(self) -> list[ChannelRegistryEntry]:
        """Return active channels in the bound tenant."""
        rows = self._session.scalars(
            select(YouTubeChannelORM)
            .where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.active.is_(True),
            )
            .order_by(YouTubeChannelORM.youtube_channel_id)
        ).all()
        return [self._to_entry(row) for row in rows]

    def list_channels_by_ids(
        self, youtube_channel_ids: set[str], *, include_inactive: bool = False
    ) -> list[ChannelRegistryEntry]:
        """Return channels matching a set of external ids (active-only by default).

        ``include_inactive=True`` is the import-planning lookup: an archived row
        must be seen as EXISTING so the planner reports a per-row error instead
        of planning a CREATE that create_channel's duplicate guard rejects.
        """
        if not youtube_channel_ids:
            return []
        statement = select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == self._tenant_id,
            YouTubeChannelORM.youtube_channel_id.in_(youtube_channel_ids),
        )
        if not include_inactive:
            statement = statement.where(YouTubeChannelORM.active.is_(True))
        rows = self._session.scalars(statement.order_by(YouTubeChannelORM.youtube_channel_id)).all()
        return [self._to_entry(row) for row in rows]

    def get_channel(self, youtube_channel_id: str) -> ChannelRegistryEntry | None:
        """Return a single channel entry by external id, or None."""
        row = self._get_row(youtube_channel_id)
        if row is None:
            return None
        return self._to_entry(row)

    def create_channel(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        primary_company_id: str | None,
        cms_status: str,
        revenue_required: bool,
        content_owner_id: str | None = None,
    ) -> ChannelRegistryEntry:
        """Create a channel row, raising on tenant-scoped duplicate or FK violation."""
        if self._get_row(youtube_channel_id) is not None:
            raise ChannelRegistryConflictError(f"Channel already exists: {youtube_channel_id}")

        # A revenue-required create serializes with the month-close protocol the
        # same way an OFF->ON flip does (review #159 r3712948674): without this,
        # a create committing between a lock's readiness recheck and its commit
        # would carry created_at <= locked_at and retroactively fail the LOCKED
        # month's effective-dated readiness. Holding the tenant-wide guard means
        # the create either commits before the readiness recheck (and is
        # counted) or waits until the month locks. Same acyclic order as the
        # flip path: this key only, never month-then-this. No-op off Postgres.
        if revenue_required:
            acquire_finance_month_advisory_lock(
                self._session,
                REVENUE_REQUIREMENT_GUARD_MONTH,
                tenant_id=self._tenant_id,
            )
        # Stamp created_at at the actual insertion point (AFTER any guard
        # wait) with the same app wall clock that stamps
        # FinanceMonthCloseORM.locked_at. The column's server default now() is
        # the transaction START time: for a guarded create it predates any
        # lock this transaction waited on (defeating the created_at <=
        # locked_at cutoff, review #159 r3713449080), and for a
        # performance-only create it can predate a month that locked mid-
        # transaction, making a later OFF->ON flip demand a fact for a month
        # the channel never really existed in (review #159 r3713841258).
        created_at = datetime.now(UTC)

        row = YouTubeChannelORM(
            id=uuid4(),
            tenant_id=self._tenant_id,
            youtube_channel_id=youtube_channel_id,
            channel_name=channel_name,
            primary_org_unit_id=_parse_optional_uuid(primary_company_id, "primary_company_id"),
            cms_status=cms_status,
            content_owner_id=normalize_optional_content_owner(content_owner_id),
            revenue_required=revenue_required,
            revenue_source_status=(
                "MISSING_REVENUE_SOURCE" if revenue_required else "PERFORMANCE_ONLY"
            ),
            active=True,
        )
        row.created_at = created_at
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            if (
                _is_duplicate_channel_integrity_error(exc)
                or self._get_row(youtube_channel_id) is not None
            ):
                raise ChannelRegistryConflictError(
                    f"Channel already exists: {youtube_channel_id}"
                ) from exc
            raise _channel_registry_validation_error_from_integrity_error(exc) from exc
        return self._to_entry(row)

    def update_mapping(
        self, *, youtube_channel_id: str, primary_company_id: str | None
    ) -> ChannelRegistryEntry:
        # ====================================================================
        # Purpose: Re-parent a channel's primary org unit, but first reject the
        #   change when the channel has any revenue fact in a LOCKED finance
        #   month — re-parenting would silently rewrite that closed month's
        #   company/sector attribution.
        # Database/ORM: YouTubeChannelORM (write), MonthlyChannelRevenueFactORM +
        #   FinanceMonthCloseORM (read-only lock check).
        # Standards: Read-only guard (no row creation -> RLS-safe, no platform
        #   write lane); tenant-scoped; raises a typed domain error mapped to 409
        #   at the route. The concurrent-close race (a month locking between the
        #   check and flush) is a narrow documented limitation (PR #57 N9).
        #   No-op PATCH (requested mapping equals the current value) is a
        #   fail-fast idempotency path: the lock check is skipped, no row is
        #   written, and the route treats the return value as a no-change marker
        #   so the audit layer does not record a CHANNEL_UPDATED event for a
        #   non-change. The concurrent-close race (a month locking between the
        #   check and flush) is a narrow documented limitation (PR #57 N9).
        # Blast Radius: Finance attribution integrity, month locks, audit (a
        #   rejected change must not be audited; a no-op change must not be
        #   audited either). No Neo4j, no exports.
        # Connections:
        #   - File: backend/ums_smart_revenue/api/channels.py -> 409 boundary +
        #     no-op audit suppression.
        # ====================================================================
        row = self._get_row(youtube_channel_id)
        if row is None:
            raise KeyError(f"Channel not found: {youtube_channel_id}")

        # FIX: Compare the parsed target to the row's current primary_org_unit_id
        # BEFORE the locked-month guard. An idempotent PATCH (same mapping value)
        # is a safe retry: no re-parenting would occur, so the lock check is
        # unnecessary and would otherwise wrongly return 409 to legitimate
        # clients that resubmit the current value.
        parsed_primary_company_id = _parse_optional_uuid(primary_company_id, "primary_company_id")
        if parsed_primary_company_id == row.primary_org_unit_id:
            return self._to_entry(row)

        locked_months = self._locked_months_for_channel(youtube_channel_id)
        if locked_months:
            raise ChannelMappingLockedMonthError(
                "Channel mapping cannot change: revenue facts exist in locked "
                f"finance month(s): {', '.join(locked_months)}"
            )

        row.primary_org_unit_id = parsed_primary_company_id
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise _channel_registry_validation_error_from_integrity_error(exc) from exc
        return self._to_entry(row)

    # ========================================================================
    # Purpose: Set or clear a channel's CMS content_owner_id — the key
    #   list_target_channels matches against the connector account id to
    #   choose which channels a revenue pull targets.
    # Database/ORM: YouTubeChannelORM (write), tenant-scoped.
    # Standards: No locked-month guard (unlike update_mapping). Changing the
    #   content owner never rewrites a closed month's company/sector
    #   attribution — it only retargets FUTURE ingestion — so the lock check
    #   that protects finance attribution does not apply here. A missing row
    #   raises KeyError, which the route maps to HTTP 404. A flush IntegrityError
    #   is converted to ChannelRegistryValidationError, which the route maps to
    #   HTTP 422 (mirroring create_channel / update_mapping).
    # Blast Radius: Future ingestion targeting only. No finance attribution
    #   rewrite, no month locks, no Neo4j, no exports. KNOWN CAVEAT: this write
    #   does not touch google_revenue_source_rows already ingested for an OPEN
    #   month under a previous content owner; normalize_month buckets source rows
    #   by (youtube_channel_id, source_system) and is content_owner-agnostic, so
    #   stale prior-owner rows for the current month can still feed that month's
    #   revenue fact until the next ingestion/normalization cycle replaces them.
    #   Invalidating source rows is a finance-data mutation that belongs in the
    #   ingestion/cleanup layer (locked-month-aware), not this registry write.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/channels.py -> 404 + 422 boundary
    #     and no-op audit suppression + MANAGE_CHANNELS permission_override.
    #   - File: backend/ums_smart_revenue/connectors/google/
    #     youtube_analytics_client.py -> list_target_channels reads it.
    #   - File: backend/ums_smart_revenue/finance/google_source_normalizer.py
    #     -> normalize_month is content_owner-agnostic (see caveat above).
    # ========================================================================
    def update_content_owner(
        self, *, youtube_channel_id: str, content_owner_id: str | None
    ) -> ChannelRegistryEntry:
        row = self._get_row(youtube_channel_id)
        if row is None:
            raise KeyError(f"Channel not found: {youtube_channel_id}")
        row.content_owner_id = normalize_optional_content_owner(content_owner_id)
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise _channel_registry_validation_error_from_integrity_error(exc) from exc
        return self._to_entry(row)

    # ========================================================================
    # Purpose: Replace a channel's inventory fields (name, CMS status, content
    #   owner, revenue-required) from an authoritative CMS roster import row.
    #   This is the upsert "update" half of the bulk channel inventory import
    #   (file-wins): when the import finds a channel that already exists, it
    #   calls this instead of update_mapping / update_content_owner because
    #   neither of those covers channel_name, cms_status, and revenue_required
    #   together.
    # Database/ORM: YouTubeChannelORM (write), tenant-scoped;
    #   FinanceMonthCloseORM x MonthlyChannelRevenueFactORM (read-only guard).
    # Standards: No org-attribution locked-month guard (this method never
    #   touches primary_org_unit_id), but flipping revenue_required OFF->ON is
    #   guarded: readiness evaluates the CURRENT flag, so enabling it while a
    #   LOCKED month lacks a fact for this channel would retroactively break
    #   that month's finalized readiness
    #   (ChannelRevenueRequirementLockedMonthError -> route 409). Preserves
    #   revenue_source_status unless revenue_required flips (see
    #   derive_revenue_source_status) so an inventory refresh cannot downgrade
    #   an established official classification. A missing row raises
    #   ChannelRegistryValidationError (not KeyError, unlike
    #   update_mapping/update_content_owner) so the bulk import can report a
    #   clean per-row validation failure instead of an untyped exception. No
    #   IntegrityError handling is needed here (unlike
    #   create_channel/update_mapping): none of the columns this method writes
    #   carry a foreign key or uniqueness constraint.
    # Blast Radius: Channel inventory fields only (name/status/owner/revenue
    #   flag). No finance attribution rewrite, no month locks, no Neo4j, no
    #   exports.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/channels.py -> bulk import route
    #     (POST /channels/import) upsert-update path.
    # ========================================================================
    def update_inventory(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        cms_status: str,
        content_owner_id: str | None,
        revenue_required: bool,
    ) -> tuple[ChannelRegistryEntry, ChannelRegistryEntry]:
        """Replace a channel's inventory fields from an authoritative import row.

        Returns ``(previous, updated)`` where ``previous`` is the row's state
        observed at the write boundary — re-read under a row lock, not the
        caller's possibly-stale planning snapshot — so audit trails can record
        the values actually replaced.
        """
        row = self._get_row(youtube_channel_id)
        if row is None:
            raise ChannelRegistryValidationError(f"Unknown channel: {youtube_channel_id}")
        # DEADLOCK ORDER: the tenant-wide guard MUST precede the channel row
        # lock. The lock-time readiness recheck acquires this guard and then
        # FOR-UPDATEs required channel rows (with_for_update(of=
        # YouTubeChannelORM) in month_close_readiness); taking the row first
        # and the guard second is the opposite order and deadlocks against a
        # concurrent close (review #159 r3713841225). Whether this write
        # actually flips the flag is only knowable after the locked re-read
        # below, so EVERY requested-True write takes the guard — the extra
        # serialization only affects imports carrying required rows, which
        # already serialize with closes by design. Serializes flip and lock
        # (review #159 r3712948682): whichever commits first is visible to the
        # other's check. No-op off Postgres.
        if revenue_required:
            acquire_finance_month_advisory_lock(
                self._session,
                REVENUE_REQUIREMENT_GUARD_MONTH,
                tenant_id=self._tenant_id,
            )
        # Row-lock and re-read at the write boundary: a concurrent committed
        # update between the caller's planning read and this write must not be
        # silently hidden — the returned `previous` reflects what this write
        # actually replaced. SQLite's dialect ignores FOR UPDATE (single-writer
        # anyway); Postgres serializes with any concurrent row writer.
        self._session.refresh(row, with_for_update=True)
        previous = self._to_entry(row)
        # Flipping revenue_required ON is guarded against LOCKED months: month-
        # close readiness evaluates the CURRENT flag, so enabling it while a
        # locked month has no fact for this channel would retroactively make
        # that already-finalized month report a missing required fact. Mirrors
        # update_mapping's locked-month guard; the flip stays possible after
        # the affected months are unlocked (or once facts exist for them).
        if revenue_required and not row.revenue_required:
            locked_missing = self._locked_months_missing_fact(
                youtube_channel_id, created_at=row.created_at
            )
            if locked_missing:
                raise ChannelRevenueRequirementLockedMonthError(
                    "revenue_required cannot be enabled: locked finance month(s) "
                    f"have no revenue fact for {youtube_channel_id}: "
                    f"{', '.join(locked_missing)}"
                )
        # Re-derive the source status only when revenue_required actually flips;
        # an unrelated inventory refresh must not clobber a proven
        # OFFICIAL_CMS_REVENUE / OFFICIAL_MANUAL_IMPORT classification back to
        # MISSING_REVENUE_SOURCE (see derive_revenue_source_status).
        row.revenue_source_status = derive_revenue_source_status(
            current_status=row.revenue_source_status,
            current_revenue_required=row.revenue_required,
            revenue_required=revenue_required,
        )
        row.channel_name = channel_name
        row.cms_status = cms_status
        row.content_owner_id = normalize_optional_content_owner(content_owner_id)
        row.revenue_required = revenue_required
        self._session.flush()
        return previous, self._to_entry(row)

    def _get_row(self, youtube_channel_id: str) -> YouTubeChannelORM | None:
        """Look up the ORM row filtered by tenant_id + external channel id."""
        return self._session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.youtube_channel_id == youtube_channel_id,
            )
        ).one_or_none()

    def _locked_months_missing_fact(
        self, youtube_channel_id: str, *, created_at: datetime
    ) -> list[str]:
        """Return sorted LOCKED months missing a fact that INCLUDE this channel.

        Read-only, tenant-scoped. These are the months whose close readiness
        would retroactively break if the channel became revenue-required now:
        the readiness query counts active revenue-required channels missing a
        fact, evaluated against the CURRENT registry flag. Mirrors readiness's
        effective dating exactly — a month locked BEFORE the channel existed
        (``locked_at < created_at``) never evaluates the channel, so it must
        not block the flip either; without this symmetry a channel created
        after historical closes could never become revenue-required
        (review #159 r3713449090). A LOCKED month with no ``locked_at`` has no
        provable cutoff and stays blocking (fail closed), matching readiness,
        which applies no cutoff in that case.
        """
        fact_exists = (
            select(MonthlyChannelRevenueFactORM.id)
            .where(
                MonthlyChannelRevenueFactORM.tenant_id == self._tenant_id,
                MonthlyChannelRevenueFactORM.youtube_channel_id == youtube_channel_id,
                MonthlyChannelRevenueFactORM.month == FinanceMonthCloseORM.month,
            )
            .exists()
        )
        rows = self._session.scalars(
            select(FinanceMonthCloseORM.month).where(
                FinanceMonthCloseORM.tenant_id == self._tenant_id,
                FinanceMonthCloseORM.status == "LOCKED",
                or_(
                    FinanceMonthCloseORM.locked_at.is_(None),
                    FinanceMonthCloseORM.locked_at >= created_at,
                ),
                ~fact_exists,
            )
        ).all()
        return sorted(rows)

    def _locked_months_for_channel(self, youtube_channel_id: str) -> list[str]:
        """Return the sorted LOCKED finance months this channel has facts in.

        Read-only, tenant-scoped: joins the channel's revenue facts to the
        finance-month close rows and keeps only months whose close status is
        LOCKED. No row creation, so this stays on the read lane (RLS-safe).
        """
        rows = self._session.scalars(
            select(MonthlyChannelRevenueFactORM.month)
            .distinct()
            .join(
                FinanceMonthCloseORM,
                (FinanceMonthCloseORM.tenant_id == MonthlyChannelRevenueFactORM.tenant_id)
                & (FinanceMonthCloseORM.month == MonthlyChannelRevenueFactORM.month),
            )
            .where(
                MonthlyChannelRevenueFactORM.tenant_id == self._tenant_id,
                MonthlyChannelRevenueFactORM.youtube_channel_id == youtube_channel_id,
                FinanceMonthCloseORM.status == "LOCKED",
            )
        ).all()
        return sorted(rows)

    @staticmethod
    def _to_entry(row: YouTubeChannelORM) -> ChannelRegistryEntry:
        return ChannelRegistryEntry(
            youtube_channel_id=row.youtube_channel_id,
            channel_name=row.channel_name,
            primary_company_id=str(row.primary_org_unit_id)
            if row.primary_org_unit_id is not None
            else None,
            cms_status=row.cms_status,
            revenue_required=row.revenue_required,
            content_owner_id=row.content_owner_id,
            revenue_source_status=row.revenue_source_status,
            active=row.active,
        )


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve tenant id from explicit param, request context, or default fallback."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Normalize tenant constructor input into a UUID object."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise ChannelRegistryValidationError("tenant_id must be a valid UUID") from exc


def _parse_optional_uuid(value: str | None, field_name: str) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise ChannelRegistryValidationError(f"{field_name} must be a valid UUID") from exc


def _is_duplicate_channel_integrity_error(exc: IntegrityError) -> bool:
    constraint_name = _constraint_name(exc)
    error_text = _integrity_error_text(exc)
    return (
        "youtube_channel_id" in constraint_name
        or ("youtube_channels" in constraint_name and "youtube_channel" in constraint_name)
        or "unique constraint failed: youtube_channels.youtube_channel_id" in error_text
        or _SQLITE_TENANT_CHANNEL_UNIQUE_ERROR in error_text
    )


def _channel_registry_validation_error_from_integrity_error(
    exc: IntegrityError,
) -> ChannelRegistryValidationError:
    constraint_name = _constraint_name(exc)
    error_text = _integrity_error_text(exc)
    if (
        "primary_org_unit_id" in constraint_name
        or "tenant_org_unit" in constraint_name
        or "foreign key constraint failed" in error_text
    ):
        return ChannelRegistryValidationError(
            "primary_company_id must reference an existing org unit"
        )
    return ChannelRegistryValidationError("Channel registry values violate database constraints")


def _constraint_name(exc: IntegrityError) -> str:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return str(getattr(diag, "constraint_name", "") or "").lower()


def _integrity_error_text(exc: IntegrityError) -> str:
    return f"{exc.orig!s} {exc!s}".lower()
