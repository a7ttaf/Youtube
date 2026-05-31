"""Channel↔account map: read models, typed errors, and the repository.

Two layers: operator-verified adsense_account_id ↔ content_owner_id, and derived
content_owner_id ↔ youtube_channel_id. The repository exposes propose/verify/
reject (verify guarded by a per-account advisory lock + fail-closed overlap
check), an idempotent source-row derivation, and the verified read contract that
the allocation engine (Spec 2b) consumes.

NOTE — built across Tasks 4–10; imports accrete. Add each symbol in the task
that first uses it so every commit stays ruff-clean:
  Task 5 → `from sqlalchemy import func, select`; `from sqlalchemy.orm import Session`
  Task 6 → `from ums_smart_revenue.db.finance_models import AdsenseContentOwnerLinkORM`
  Task 7 → add `UTC` to the datetime import (datetime.now(UTC))
  Task 9 → add `ContentOwnerChannelLinkORM` to the finance_models import;
           `from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM`
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
)
from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
_MONTH_LENGTH = 7
_OPEN_END = "9999-12"  # sentinel for open-ended ranges in overlap comparison


class ChannelAccountLinkError(ValueError):
    """Base error for channel-account map operations."""


class ChannelAccountLinkValidationError(ChannelAccountLinkError):
    """Raised on malformed input (month, bounds, status)."""


class ChannelAccountLinkConflictError(ChannelAccountLinkError):
    """Raised when verifying would overlap an existing VERIFIED link."""


class ChannelAccountLinkNotFoundError(ChannelAccountLinkError):
    """Raised when a link id does not exist for the tenant."""


@dataclass(frozen=True)
class AccountOwnerLink:
    """Account↔owner link read model. provenance_payload is never serialized."""

    id: str
    adsense_account_id: str
    content_owner_id: str
    verification_status: str
    provenance_kind: str
    provenance_payload: dict[str, object]
    verified_by: str | None
    verified_at: datetime | None
    verification_reason: str | None
    effective_month_start: str
    effective_month_end: str | None

    def to_api(self) -> dict[str, object]:
        """Return the API payload, excluding raw provenance_payload."""
        # provenance_payload is intentionally omitted (raw evidence; see spec §3).
        return {
            "id": self.id,
            "adsense_account_id": self.adsense_account_id,
            "content_owner_id": self.content_owner_id,
            "verification_status": self.verification_status,
            "provenance_kind": self.provenance_kind,
            "verified_by": self.verified_by,
            "verified_at": (
                None if self.verified_at is None else self.verified_at.isoformat()
            ),
            "verification_reason": self.verification_reason,
            "effective_month_start": self.effective_month_start,
            "effective_month_end": self.effective_month_end,
        }


@dataclass(frozen=True)
class AccountOwnerLinkPage:
    """One page of account-owner links plus the full-match count."""

    total_count: int
    links: list[AccountOwnerLink]


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve explicit, ambient, or default tenant UUID for repository scoping.

    Raises:
        ChannelAccountLinkValidationError: If an explicit tenant id is invalid.
    """
    if isinstance(tenant_id, UUID):
        return tenant_id
    if tenant_id is None:
        current_tenant = get_current_tenant()
        if current_tenant is not None:
            return current_tenant.id
        return _DEFAULT_TENANT_UUID
    try:
        return UUID(str(tenant_id).strip())
    except ValueError as exc:
        raise ChannelAccountLinkValidationError(f"invalid tenant_id: {tenant_id!r}") from exc


def _validate_month(month: str) -> None:
    """Validate YYYY-MM month input.

    Raises:
        ChannelAccountLinkValidationError: If the month is malformed.
    """
    if len(month) != _MONTH_LENGTH or month[4] != "-":
        raise ChannelAccountLinkValidationError("month must use YYYY-MM")
    year, mm = month[:4], month[5:]
    if not (
        all("0" <= char <= "9" for char in year)
        and all("0" <= char <= "9" for char in mm)
        and 1 <= int(mm) <= 12
    ):
        raise ChannelAccountLinkValidationError("month must use YYYY-MM")


def _ranges_overlap(
    start_a: str, end_a: str | None, start_b: str, end_b: str | None
) -> bool:
    """Return True if two YYYY-MM ranges overlap (None end = open-ended)."""
    ea = end_a if end_a is not None else _OPEN_END
    eb = end_b if end_b is not None else _OPEN_END
    return start_a <= eb and start_b <= ea


def _account_owner_lock_key(tenant_id: UUID, adsense_account_id: str) -> int:
    """Return a stable signed-bigint advisory-lock key for one (tenant, account).

    Mirrors connectors/google_source_rows/repository.py: blake2b of a \\0-joined
    discriminator, shifted into the positive signed-bigint range. Never includes
    payload, amounts, or credentials.
    """
    payload = (
        f"adsense_content_owner_links\0{tenant_id}\0{adsense_account_id}"
    ).encode()
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1


class SqlAlchemyChannelAccountLinkRepository:
    """Tenant-scoped storage for the channel↔account map."""

    # ========================================================================
    # Purpose: Manage account↔owner link lifecycle (propose/verify/reject) with
    #   a per-account advisory lock guarding a fail-closed overlap invariant,
    #   derive owner↔channel links from source rows, and serve the verified
    #   read contract for allocation.
    # Database/ORM: adsense_content_owner_links, content_owner_channel_links,
    #   read-only google_revenue_source_rows.
    # Standards: tenant-explicit; pg_advisory_xact_lock on PostgreSQL (SQLite
    #   no-op); typed errors; reads never write.
    # Blast Radius: Finance source-of-truth writes (new tables). No Neo4j.
    # ========================================================================
    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def _acquire_account_owner_lock(self, adsense_account_id: str) -> None:
        """Serialize verify/reject for one account via a transaction advisory lock.

        PostgreSQL-only; SQLite has no comparable primitive and no-ops (unit
        tests run serially, so the overlap check is still exercised).
        """
        if self._session.get_bind().dialect.name != "postgresql":
            return
        lock_key = _account_owner_lock_key(self._tenant_id, adsense_account_id)
        self._session.execute(select(func.pg_advisory_xact_lock(lock_key)))

    def propose_account_owner_link(
        self, *,
        adsense_account_id: str,
        content_owner_id: str,
        effective_month_start: str,
        effective_month_end: str | None,
        provenance_kind: str,
        provenance_payload: dict[str, object],
    ) -> AccountOwnerLink:
        """Insert an UNVERIFIED account↔owner candidate.

        Raises:
            ChannelAccountLinkValidationError: If a month is malformed or end <
                start.
        """
        _validate_month(effective_month_start)
        if effective_month_end is not None:
            _validate_month(effective_month_end)
            if effective_month_end < effective_month_start:
                raise ChannelAccountLinkValidationError(
                    "effective_month_end must be >= effective_month_start"
                )
        row = AdsenseContentOwnerLinkORM(
            tenant_id=self._tenant_id,
            adsense_account_id=adsense_account_id,
            content_owner_id=content_owner_id,
            verification_status="UNVERIFIED",
            provenance_kind=provenance_kind,
            provenance_payload=dict(provenance_payload or {}),
            effective_month_start=effective_month_start,
            effective_month_end=effective_month_end,
        )
        self._session.add(row)
        self._session.flush()
        return self._to_account_owner_link(row)

    @staticmethod
    def _to_account_owner_link(row: AdsenseContentOwnerLinkORM) -> AccountOwnerLink:
        """Convert an ORM row into the read-model dataclass."""
        return AccountOwnerLink(
            id=str(row.id),
            adsense_account_id=row.adsense_account_id,
            content_owner_id=row.content_owner_id,
            verification_status=row.verification_status,
            provenance_kind=row.provenance_kind,
            provenance_payload=dict(row.provenance_payload or {}),
            verified_by=None if row.verified_by is None else str(row.verified_by),
            verified_at=row.verified_at,
            verification_reason=row.verification_reason,
            effective_month_start=row.effective_month_start,
            effective_month_end=row.effective_month_end,
        )

    def _load_owned(self, link_id: str) -> AdsenseContentOwnerLinkORM:
        """Load a tenant-owned account↔owner row by id or raise NotFound."""
        try:
            uuid_id = UUID(str(link_id))
        except ValueError as exc:
            raise ChannelAccountLinkNotFoundError(f"unknown link: {link_id!r}") from exc
        row = self._session.scalars(
            select(AdsenseContentOwnerLinkORM).where(
                AdsenseContentOwnerLinkORM.id == uuid_id,
                AdsenseContentOwnerLinkORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if row is None:
            raise ChannelAccountLinkNotFoundError(f"unknown link: {link_id!r}")
        return row

    def get_account_owner_link(self, link_id: str) -> AccountOwnerLink:
        """Return one account↔owner link by id (tenant-scoped, exact read).

        Used by the verify/reject route to authorize on the link's own month
        without scanning a paginated list.

        Raises:
            ChannelAccountLinkNotFoundError: If the link id is unknown.
        """
        return self._to_account_owner_link(self._load_owned(link_id))

    def verify_account_owner_link(
        self, link_id: str, *, verified_by: UUID, reason: str
    ) -> AccountOwnerLink:
        """Transition a link to VERIFIED, enforcing the per-account overlap invariant.

        Acquires the per-account advisory lock first so concurrent verifies for
        one account cannot both commit overlapping VERIFIED rows.

        A REJECTED or already-VERIFIED link may be re-verified: this is the recovery
        path, since the (tenant, account, owner, effective_month_start) unique key
        blocks re-proposing an identical row. The overlap invariant is re-checked on
        every transition, so re-verifying can never create overlapping VERIFIED links.

        Raises:
            ChannelAccountLinkNotFoundError: If the link id is unknown.
            ChannelAccountLinkConflictError: If a VERIFIED link already overlaps.
        """
        row = self._load_owned(link_id)
        self._acquire_account_owner_lock(row.adsense_account_id)
        existing = self._session.scalars(
            select(AdsenseContentOwnerLinkORM).where(
                AdsenseContentOwnerLinkORM.tenant_id == self._tenant_id,
                AdsenseContentOwnerLinkORM.adsense_account_id == row.adsense_account_id,
                AdsenseContentOwnerLinkORM.verification_status == "VERIFIED",
                AdsenseContentOwnerLinkORM.id != row.id,
            )
        ).all()
        for other in existing:
            if _ranges_overlap(
                row.effective_month_start, row.effective_month_end,
                other.effective_month_start, other.effective_month_end,
            ):
                raise ChannelAccountLinkConflictError(
                    "a verified link already covers an overlapping month range "
                    f"for account {row.adsense_account_id}"
                )
        row.verification_status = "VERIFIED"
        row.verified_by = verified_by
        row.verified_at = datetime.now(UTC)
        row.verification_reason = reason
        self._session.flush()
        return self._to_account_owner_link(row)

    def reject_account_owner_link(
        self, link_id: str, *, verified_by: UUID, reason: str
    ) -> AccountOwnerLink:
        """Transition a link to REJECTED (money-affecting; same gate as verify).

        Raises:
            ChannelAccountLinkNotFoundError: If the link id is unknown.
        """
        row = self._load_owned(link_id)
        self._acquire_account_owner_lock(row.adsense_account_id)
        row.verification_status = "REJECTED"
        row.verified_by = verified_by
        row.verified_at = datetime.now(UTC)
        row.verification_reason = reason
        self._session.flush()
        return self._to_account_owner_link(row)

    def list_account_owner_links(
        self, *,
        status: str | None = None,
        adsense_account_id: str | None = None,
        content_owner_id: str | None = None,
        month: str | None = None,
        limit: int,
        offset: int,
    ) -> AccountOwnerLinkPage:
        """Return a filtered, paginated page of account↔owner links + full count.

        ``month`` filters to links valid for that month (start <= month <=
        coalesce(end, month)).

        Raises:
            ChannelAccountLinkValidationError: If month is malformed, limit < 1,
                or offset < 0.
        """
        if limit < 1:
            raise ChannelAccountLinkValidationError("limit must be >= 1")
        if offset < 0:
            raise ChannelAccountLinkValidationError("offset must be >= 0")
        filters = [AdsenseContentOwnerLinkORM.tenant_id == self._tenant_id]
        if status is not None:
            filters.append(AdsenseContentOwnerLinkORM.verification_status == status)
        if adsense_account_id is not None:
            filters.append(AdsenseContentOwnerLinkORM.adsense_account_id == adsense_account_id)
        if content_owner_id is not None:
            filters.append(AdsenseContentOwnerLinkORM.content_owner_id == content_owner_id)
        if month is not None:
            _validate_month(month)
            filters.append(AdsenseContentOwnerLinkORM.effective_month_start <= month)
            filters.append(
                (AdsenseContentOwnerLinkORM.effective_month_end.is_(None))
                | (AdsenseContentOwnerLinkORM.effective_month_end >= month)
            )
        total_count = self._session.scalar(
            select(func.count()).select_from(AdsenseContentOwnerLinkORM).where(*filters)
        )
        rows = self._session.scalars(
            select(AdsenseContentOwnerLinkORM)
            .where(*filters)
            .order_by(
                AdsenseContentOwnerLinkORM.adsense_account_id,
                AdsenseContentOwnerLinkORM.content_owner_id,
                AdsenseContentOwnerLinkORM.effective_month_start,
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return AccountOwnerLinkPage(
            total_count=int(total_count or 0),
            links=[self._to_account_owner_link(row) for row in rows],
        )

    def upsert_owner_channel_links_from_source(self) -> int:
        """Idempotently derive owner↔channel links from source-row co-occurrence.

        Only rows where BOTH content_owner_id and youtube_channel_id are present
        produce links. source_account_id is never read (it must not infer the
        account↔owner link). Returns the count of newly inserted links.
        """
        observed = self._session.execute(
            select(
                GoogleRevenueSourceRowORM.content_owner_id,
                GoogleRevenueSourceRowORM.youtube_channel_id,
                GoogleRevenueSourceRowORM.report_month,
                func.min(GoogleRevenueSourceRowORM.source_row_key),
            )
            .where(
                GoogleRevenueSourceRowORM.tenant_id == self._tenant_id,
                GoogleRevenueSourceRowORM.content_owner_id.is_not(None),
                GoogleRevenueSourceRowORM.youtube_channel_id.is_not(None),
            )
            .group_by(
                GoogleRevenueSourceRowORM.content_owner_id,
                GoogleRevenueSourceRowORM.youtube_channel_id,
                GoogleRevenueSourceRowORM.report_month,
            )
        ).all()
        created = 0
        for owner_id, channel_id, month, source_key in observed:
            exists = self._session.scalar(
                select(func.count())
                .select_from(ContentOwnerChannelLinkORM)
                .where(
                    ContentOwnerChannelLinkORM.tenant_id == self._tenant_id,
                    ContentOwnerChannelLinkORM.content_owner_id == owner_id,
                    ContentOwnerChannelLinkORM.youtube_channel_id == channel_id,
                    ContentOwnerChannelLinkORM.effective_month_start == month,
                )
            )
            if exists:
                continue
            self._session.add(
                ContentOwnerChannelLinkORM(
                    tenant_id=self._tenant_id,
                    content_owner_id=owner_id,
                    youtube_channel_id=channel_id,
                    provenance_kind="SOURCE_ROW",
                    provenance_source_id=source_key,
                    active=True,
                    effective_month_start=month,
                    effective_month_end=month,
                )
            )
            created += 1
        self._session.flush()
        return created
