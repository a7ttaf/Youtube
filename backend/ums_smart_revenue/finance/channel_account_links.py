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
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import AdsenseContentOwnerLinkORM
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
