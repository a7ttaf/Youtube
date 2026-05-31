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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    FinanceMonthCloseORM,
)
from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
_MONTH_LENGTH = 7
_OPEN_END = "9999-12"  # sentinel for open-ended ranges in overlap comparison
_VALID_LINK_STATUSES = frozenset({"UNVERIFIED", "VERIFIED", "REJECTED", "CONFLICT"})


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return True when exc wraps a unique-constraint violation, else False.

    Recognizes psycopg3 (``sqlstate``), psycopg2 (``pgcode``), and SQLite
    (message text) so a duplicate proposal surfaces as a 409 on every backend.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    # FIX: this project pins psycopg3 (psycopg[binary]==3.3.4), which exposes
    # SQLSTATE via `sqlstate`, not the psycopg2 `pgcode`. Checking only `pgcode`
    # let a real PostgreSQL duplicate fall through to the SQLite-message branch
    # and return False, so the duplicate POST bubbled as an unhandled
    # IntegrityError instead of the intended 409. Check `sqlstate` first, then
    # `pgcode` (psycopg2), then the SQLite message.
    for attr in ("sqlstate", "pgcode"):
        code = getattr(orig, attr, None)
        if code is not None:
            return code == "23505"
    return "UNIQUE constraint failed" in str(orig)


class ChannelAccountLinkError(ValueError):
    """Base error for channel-account map operations."""


class ChannelAccountLinkValidationError(ChannelAccountLinkError):
    """Raised on malformed input (month, bounds, status)."""


class ChannelAccountLinkConflictError(ChannelAccountLinkError):
    """Raised when verifying would overlap an existing VERIFIED link."""


class ChannelAccountLinkNotFoundError(ChannelAccountLinkError):
    """Raised when a link id does not exist for the tenant."""


class ChannelAccountLinkLockedMonthError(ChannelAccountLinkError):
    """Raised when verify/reject would change a link covering a LOCKED month."""


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
    r"""Return a stable signed-bigint advisory-lock key for one (tenant, account).

    Mirrors connectors/google_source_rows/repository.py: blake2b of a \0-joined
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

    def _require_month_open(self, month: str) -> None:
        """Raise ChannelAccountLinkLockedMonthError when the month is LOCKED.

        Acquires the finance-month advisory lock (for_update=True) to serialize
        with concurrent month close/unlock operations.
        """
        close = get_or_create_month_close_row(
            self._session, month, tenant_id=self._tenant_id, for_update=True
        )
        if close.status == "LOCKED":
            raise ChannelAccountLinkLockedMonthError(
                f"Finance month {month!r} is locked; verify is not permitted"
            )

    def _require_range_open(self, start: str, end: str | None) -> None:
        """Raise ChannelAccountLinkLockedMonthError if any covered month is LOCKED.

        A VERIFIED link feeds allocation for every month in ``[start, end]`` (or
        ``[start, ∞)`` when open-ended), so verify/reject must reject when ANY
        covered finance month is LOCKED — not only the start month.

        The start month is lock-checked under its per-month advisory lock
        (materializing only its own OPEN close row). The remaining covered range
        is then screened with a single ``SELECT ... FOR UPDATE`` over the
        ``finance_month_close`` rows that ALREADY exist in ``[start, end]``:

        - It row-locks only existing rows, so an authorized far-future
          ``effective_month_end`` (e.g. 9999-12) cannot materialize/advisory-lock
          ~95k close rows in one transaction.
        - The row lock serializes against the month-close path, which takes the
          same ``FOR UPDATE`` on the close row via
          ``get_or_create_month_close_row(for_update=True)``. So a concurrent
          close cannot flip an existing OPEN covered month to LOCKED between this
          scan and our verify/reject commit — it blocks until we commit (and we
          then read its OPEN state) or we block until it commits (and we read
          LOCKED and reject).

        Residual (tracked as PR #57 N9 in Docs/15_DELIVERY_BACKLOG.md): a covered
        month with NO close row at scan time that is closed concurrently — the
        close inserts a fresh LOCKED row a row lock cannot cover. Closing that
        narrower window needs a shared serialization point on the month-close path
        (gap lock / per-tenant close epoch), a close-path change out of scope here.

        Raises:
            ChannelAccountLinkLockedMonthError: If any covered finance month in
                ``[start, end]`` (or ``[start, ∞)`` when open-ended) is LOCKED.
        """
        # FIX: the prior bounded path called _require_month_open() for every
        # covered month, and that helper creates a close row + takes an advisory
        # lock per month — a far-future end (e.g. 9999-12) would materialize ~95k
        # rows/locks in one transaction. Lock+check only the start, then row-lock
        # the EXISTING close rows across the covered range with FOR UPDATE (no
        # materialization) so a concurrent close cannot flip an OPEN covered month
        # to LOCKED behind us; FOR UPDATE is a no-op on SQLite (unit tests).
        self._require_month_open(start)
        statement = select(FinanceMonthCloseORM).where(
            FinanceMonthCloseORM.tenant_id == self._tenant_id,
            FinanceMonthCloseORM.month >= start,
        )
        if end is not None:
            statement = statement.where(FinanceMonthCloseORM.month <= end)
        statement = statement.order_by(FinanceMonthCloseORM.month).with_for_update()
        locked_month = next(
            (
                row.month
                for row in self._session.scalars(statement)
                if row.status == "LOCKED"
            ),
            None,
        )
        if locked_month is not None:
            raise ChannelAccountLinkLockedMonthError(
                f"Finance month {locked_month!r} is locked; verify is not permitted"
            )

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
            ChannelAccountLinkConflictError: If a proposal for the same
                (account, owner, effective_month_start) already exists.
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
        try:
            self._session.flush()
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            raise ChannelAccountLinkConflictError(
                f"a proposal for account {adsense_account_id!r} / owner "
                f"{content_owner_id!r} starting {effective_month_start!r} already exists"
            ) from exc
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
            ChannelAccountLinkLockedMonthError: If any month in the link's
                effective range is LOCKED.
            ChannelAccountLinkConflictError: If a VERIFIED link already overlaps.
        """
        row = self._load_owned(link_id)
        self._acquire_account_owner_lock(row.adsense_account_id)
        # FIX: a VERIFIED link is consumed by allocation for EVERY month in
        # [start, end], so a bounded link starting in an OPEN month but spanning
        # a later LOCKED month must not verify. The prior single-month check only
        # guarded effective_month_start; require the whole covered range open.
        self._require_range_open(row.effective_month_start, row.effective_month_end)
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
            ChannelAccountLinkLockedMonthError: If the link is currently VERIFIED
                and any month in its effective range is LOCKED.
        """
        row = self._load_owned(link_id)
        self._acquire_account_owner_lock(row.adsense_account_id)
        # FIX: re-read the row AFTER acquiring the per-account advisory lock. The
        # row is loaded before the lock; if a concurrent verify commits while this
        # call waits on the lock, the in-memory verification_status is stale and
        # the locked-month guard below would be wrongly skipped, letting a
        # now-VERIFIED link flip to REJECTED over a closed month. refresh() reads
        # the committed state the lock now serializes us behind.
        self._session.refresh(row)
        # FIX: rejecting a currently-VERIFIED link removes it from
        # list_verified_adsense_account_channels() for every covered month,
        # which would silently change allocation for already-closed months.
        # Apply the same locked-month guard verify uses before mutating. Only
        # VERIFIED rows feed the read contract, so UNVERIFIED/REJECTED proposals
        # in locked months remain freely rejectable (cleanup is harmless).
        if row.verification_status == "VERIFIED":
            self._require_range_open(row.effective_month_start, row.effective_month_end)
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
            if status not in _VALID_LINK_STATUSES:
                raise ChannelAccountLinkValidationError(
                    f"invalid status {status!r}; expected one of {sorted(_VALID_LINK_STATUSES)}"
                )
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

    def _owner_channel_link_exists(
        self, owner_id: str, channel_id: str, month: str
    ) -> bool:
        """Return True if an owner↔channel link for (owner, channel, month) already
        exists under this tenant. Fast existence probe for derivation idempotency.
        """
        return bool(
            self._session.scalar(
                select(func.count())
                .select_from(ContentOwnerChannelLinkORM)
                .where(
                    ContentOwnerChannelLinkORM.tenant_id == self._tenant_id,
                    ContentOwnerChannelLinkORM.content_owner_id == owner_id,
                    ContentOwnerChannelLinkORM.youtube_channel_id == channel_id,
                    ContentOwnerChannelLinkORM.effective_month_start == month,
                )
            )
        )

    def upsert_owner_channel_links_from_source(self) -> int:
        """Idempotently derive owner↔channel links from source-row co-occurrence.

        Only rows where BOTH content_owner_id and youtube_channel_id are present
        produce links. source_account_id is never read (it must not infer the
        account↔owner link). Returns the count of newly inserted links.

        Months whose finance close row is LOCKED are skipped: the read contract
        consumes active owner↔channel links for that month, so deriving a new
        link after close would change allocation for an already-closed period.
        The existing close rows for the observed months are screened with a single
        ``SELECT ... FOR UPDATE`` so a concurrent month close of an existing OPEN
        observed month is serialized against this derivation — either we read its
        committed LOCKED state (and skip the month) or it blocks until we commit.
        FOR UPDATE is a no-op on SQLite.

        Idempotent under concurrency: a per-row existence probe avoids insert
        churn on the common path, and each insert runs in its own SAVEPOINT so a
        parallel worker that inserts the same key between our probe and flush
        raises ``uq_content_owner_channel_links_key`` only inside that savepoint;
        we roll it back and treat the key as already-derived rather than failing
        the whole derivation job.
        """
        # FIX (post-close serialization, was PR #57 -GSk): read the observed
        # (owner, channel, month) co-occurrences first, then row-lock the EXISTING
        # finance_month_close rows for exactly those months with FOR UPDATE before
        # deciding which are LOCKED. This takes the SAME row lock the close path
        # acquires via get_or_create_month_close_row(for_update=True), so a
        # concurrent close of an existing OPEN observed month cannot flip it to
        # LOCKED between this read and our insert (it blocks until we commit, or we
        # read its committed LOCKED state and skip). Residual: a month with NO
        # close row at read time, closed concurrently via a fresh LOCKED insert —
        # the same absent-month window tracked as PR #57 N9. No-op on SQLite.
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
        observed_months = {month for _owner, _channel, month, _key in observed}
        locked_months: set[str] = set()
        if observed_months:
            close_rows = self._session.execute(
                select(FinanceMonthCloseORM.month, FinanceMonthCloseORM.status)
                .where(
                    FinanceMonthCloseORM.tenant_id == self._tenant_id,
                    FinanceMonthCloseORM.month.in_(observed_months),
                )
                .order_by(FinanceMonthCloseORM.month)
                .with_for_update()
            ).all()
            locked_months = {
                month for month, status in close_rows if status == "LOCKED"
            }
        created = 0
        for owner_id, channel_id, month, source_key in observed:
            if month in locked_months:
                continue  # post-close protection: no new evidence for a LOCKED month
            if self._owner_channel_link_exists(owner_id, channel_id, month):
                continue  # fast path: already derived, skip the savepoint
            try:
                # FIX (idempotent under concurrency, was PR #57 -GSr): a parallel
                # worker may insert the same (owner, channel, month) key between the
                # probe above and this flush. Each insert is its own SAVEPOINT, so
                # the unique-key violation is contained here; we swallow it and
                # treat the key as already-derived instead of failing the whole job.
                with self._session.begin_nested():
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
                    self._session.flush()
            except IntegrityError as exc:
                if not _is_unique_violation(exc):
                    raise
                continue
            created += 1
        return created

    # ============================================================================
    # Purpose: Return channels reachable from an AdSense account in a given month
    #   via a VERIFIED account↔owner link and an active owner↔channel link, both
    #   valid for that month. This is the verified read contract consumed by the
    #   allocation engine (Spec 2b). Unmapped or unverified accounts return [].
    # Database/ORM: AdsenseContentOwnerLinkORM (adsense_content_owner_links),
    #   ContentOwnerChannelLinkORM (content_owner_channel_links). Pure read.
    # Standards: tenant-explicit; month-validation gate; typed error on malformed
    #   month; no writes; no derivation.
    # Blast Radius: None — pure read; no mutation of any table.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/channel_account_links.py ->
    #       _validate_month, _resolve_tenant_id, AdsenseContentOwnerLinkORM,
    #       ContentOwnerChannelLinkORM all defined/imported in this module.
    #   - Spec 2b allocation engine: consumes the returned channel list to determine
    #       which channels a payment row can be allocated to.
    # ============================================================================
    def list_verified_adsense_account_channels(
        self, *, tenant_id: UUID | str, month: str, adsense_account_id: str
    ) -> list[str]:
        """Return channels for an account in a month via VERIFIED+valid links only.

        Joins VERIFIED account↔owner links valid for ``month`` to active
        owner↔channel links valid for ``month``. Empty when the account is
        unmapped/unverified (Spec 2b turns that into UNALLOCATED + a blocking
        issue). Pure read — no derivation, no writes.

        The explicit ``tenant_id`` argument is the authoritative scope for this
        standalone contract (the value Spec 2b passes); both query layers are
        filtered to it.

        Raises:
            ChannelAccountLinkValidationError: If ``month`` is malformed.
        """
        _validate_month(month)
        resolved_tenant = _resolve_tenant_id(tenant_id)
        owner_subquery = (
            select(AdsenseContentOwnerLinkORM.content_owner_id)
            .where(
                AdsenseContentOwnerLinkORM.tenant_id == resolved_tenant,
                AdsenseContentOwnerLinkORM.adsense_account_id == adsense_account_id,
                AdsenseContentOwnerLinkORM.verification_status == "VERIFIED",
                AdsenseContentOwnerLinkORM.effective_month_start <= month,
                (AdsenseContentOwnerLinkORM.effective_month_end.is_(None))
                | (AdsenseContentOwnerLinkORM.effective_month_end >= month),
            )
        )
        rows = self._session.scalars(
            select(ContentOwnerChannelLinkORM.youtube_channel_id)
            .where(
                ContentOwnerChannelLinkORM.tenant_id == resolved_tenant,
                ContentOwnerChannelLinkORM.content_owner_id.in_(owner_subquery),
                ContentOwnerChannelLinkORM.active.is_(True),
                ContentOwnerChannelLinkORM.effective_month_start <= month,
                (ContentOwnerChannelLinkORM.effective_month_end.is_(None))
                | (ContentOwnerChannelLinkORM.effective_month_end >= month),
            )
            .order_by(ContentOwnerChannelLinkORM.youtube_channel_id)
            .distinct()
        ).all()
        return list(rows)
