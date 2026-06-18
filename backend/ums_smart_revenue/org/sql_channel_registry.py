from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.org.channel_registry import (
    ChannelMappingLockedMonthError,
    ChannelRegistryConflictError,
    ChannelRegistryEntry,
    ChannelRegistryValidationError,
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

    def list_channels_by_ids(self, youtube_channel_ids: set[str]) -> list[ChannelRegistryEntry]:
        """Return active channels matching a set of external channel ids."""
        if not youtube_channel_ids:
            return []
        rows = self._session.scalars(
            select(YouTubeChannelORM)
            .where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.active.is_(True),
                YouTubeChannelORM.youtube_channel_id.in_(youtube_channel_ids),
            )
            .order_by(YouTubeChannelORM.youtube_channel_id)
        ).all()
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

    def _get_row(self, youtube_channel_id: str) -> YouTubeChannelORM | None:
        """Look up the ORM row filtered by tenant_id + external channel id."""
        return self._session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.youtube_channel_id == youtube_channel_id,
            )
        ).one_or_none()

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
