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

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
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
    serialization_timestamp,
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
        self._guard_held = False
        # A plain bool, not thread-local like the in-memory store's journal:
        # this registry is request-scoped, never a shared singleton.
        self._txn_active = False

    # ========================================================================
    # Purpose: SQL implementation of the store's transaction boundary — a
    #   real SAVEPOINT on the request session, so the protocol's promise
    #   ("on exception every write inside the boundary is undone") holds even
    #   for a direct caller that CATCHES the exception and later commits the
    #   session. The request path is unchanged in outcome: the savepoint
    #   rolls back first, then the route's converted HTTPException reaches
    #   the dependency, whose rollback was already the end state.
    # Database/ORM: SAVEPOINT via Session.begin_nested() — no table, column,
    #   or query-shape change; begin/release/rollback-to only. Commit of the
    #   OUTER transaction stays with the request dependency, never here.
    # Standards: Locks are unaffected by design: row locks and the advisory
    #   close guard (pg_advisory_xact_lock) are TRANSACTION-scoped, so a
    #   savepoint rollback releases neither — the _guard_held memo stays
    #   valid, and the guard-before-row-lock total order is untouched. The
    #   platform-lane audit elevation is orthogonal: elevation changes the
    #   ROLE a statement runs under, never which transaction (or savepoint)
    #   it belongs to. Exceptions always propagate. SQLite emits the same
    #   SAVEPOINT statements, so both SQL tiers behave identically.
    # Blast Radius: Which writes survive a caught apply failure for direct
    #   service/bootstrap callers — previously the accepted prefix could be
    #   committed by such a caller (PR #196 round 2, codex). Request-path
    #   behaviour and end state unchanged.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_registry.py -> the
    #     protocol contract this implements.
    #   - File: backend/ums_smart_revenue/api/dependencies.py ->
    #     authenticated_session_dependency, the outer transaction's owner.
    #   - File: backend/ums_smart_revenue/finance/month_close_locks.py ->
    #     the transaction-scoped advisory guard a savepoint rollback keeps.
    # ========================================================================
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Wrap the writes in a SAVEPOINT; roll back to it on exception.

        ``begin_nested()`` releases the savepoint on clean exit and rolls
        back to it on exception before re-raising — undoing exactly the
        writes made inside the boundary while the outer request transaction,
        its locks, and any earlier writes stand. SAVEPOINTs would stack, but
        the protocol says one enter per logical operation, so same-store
        nesting is refused here exactly as the in-memory adapter refuses it —
        the two tiers must not drift on the contract (PR #196 round 2, qodo).

        Raises:
            RuntimeError: when entered while this store already holds an
                open boundary.
        """
        if self._txn_active:
            raise RuntimeError("SqlAlchemyChannelRegistry.transaction does not nest")
        self._txn_active = True
        try:
            with self._session.begin_nested():
                yield
        except BaseException:
            # Advisory locks acquired AFTER a savepoint are released by the
            # rollback to it (PostgreSQL explicit-locking rules), so a guard
            # first taken inside this boundary is GONE while the memo said
            # otherwise — a direct caller catching the failure and writing
            # again in the same outer transaction would skip re-acquiring
            # the finance-close guard (PR #196 round 3, codex). Reset
            # unconditionally: re-acquiring a still-held lock is a cheap
            # re-entrant no-op, while trusting a stale memo is a lock hole.
            self._guard_held = False
            raise
        finally:
            self._txn_active = False

    # ========================================================================
    # Purpose: Take the tenant-wide REVENUE_REQUIREMENT_GUARD_MONTH advisory
    #   lock that serializes registry writes against the finance month-close
    #   protocol, at most once per request transaction.
    # Database/ORM: PostgreSQL advisory lock only (pg_advisory_xact_lock via
    #   month_close_locks). Touches no ORM row. No-op off PostgreSQL.
    # Standards: This is the FIRST half of a TOTAL lock order — guard BEFORE
    #   any row lock, on EVERY write path, unconditionally. Lock-time readiness
    #   takes the month key THEN this sentinel and then FOR-UPDATEs channel
    #   rows, so any caller that locks a row before calling this deadlocks
    #   against a concurrent month close. Memoized on _guard_held because the
    #   registry is request-scoped and the lock is transaction-scoped: one
    #   acquisition covers every write in the request (a 5000-row import would
    #   otherwise pay one round trip per row for a re-entrant no-op).
    # Blast Radius: Concurrency only — the ordering between channel writes and
    #   month locks. No data, audit, or authorization behavior.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/month_close_locks.py ->
    #     defines the sentinel key and the acquire helper.
    #   - File: backend/ums_smart_revenue/finance/month_close_readiness.py ->
    #     the other side of the guard; takes month-then-sentinel.
    # ========================================================================
    def _acquire_revenue_requirement_guard(self) -> None:
        """Take the tenant-wide close guard once per request transaction.

        The lock itself is a transaction-scoped advisory lock and re-entrant,
        but re-issuing it per row costs one round trip per row on a 5000-row
        import. This registry is request-scoped, and the lock lives until the
        request's transaction ends, so a single acquisition covers every write
        it performs while keeping the invariant every caller relies on:
        guard BEFORE any row lock.
        """
        if self._guard_held:
            return
        acquire_finance_month_advisory_lock(
            self._session,
            REVENUE_REQUIREMENT_GUARD_MONTH,
            tenant_id=self._tenant_id,
        )
        self._guard_held = True

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

    # ========================================================================
    # Purpose: Insert a new channel into the tenant's registry with its
    #   inventory fields and derived revenue-source status, serialized against
    #   concurrent finance month closes.
    # Database/ORM: YouTubeChannelORM INSERT, preceded by an existence SELECT
    #   and followed by flush(); the unique (tenant_id, youtube_channel_id)
    #   constraint is the authoritative duplicate guard (the pre-check only
    #   turns the common case into a clean 409).
    # Standards: Guard-then-rows — the advisory guard is taken UNCONDITIONALLY
    #   before the insert, not only for revenue_required rows, so COMMIT ORDER
    #   rather than an uncommitted timestamp decides which side of a month lock
    #   a channel falls on. created_at AND updated_at are stamped in
    #   application code from serialization_timestamp AFTER that wait: the
    #   columns' server default now() is transaction-START time, which would
    #   predate the lock this transaction waited on (defeating the
    #   LOCKED-month effective-dating cutoff) and would leave updated_at
    #   earlier than created_at on the row it just created. A duplicate
    #   IntegrityError is rolled back and re-raised as
    #   ChannelRegistryConflictError (409); anything else becomes a validation
    #   error, never a 500.
    # Blast Radius: Channel registry rows, connector ingest targeting via
    #   cms_status/content_owner_id, and month-close readiness (a
    #   revenue_required channel demands a revenue fact). No revenue math.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py -> bulk
    #     import CREATE path.
    #   - File: backend/ums_smart_revenue/api/channels.py -> single-channel
    #     create route.
    #   - File: backend/ums_smart_revenue/finance/month_close_locks.py ->
    #     the shared guard and database clock.
    # ========================================================================
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
        # EVERY create takes the guard, not just revenue-required ones. A
        # performance-only channel that flushes before a concurrent lock but
        # commits after it is invisible to that close's readiness check, yet
        # keeps a pre-lock created_at — so a later OFF->ON flip would treat it
        # as present at that close and demand a fact for a month whose
        # readiness never saw it. Holding the guard makes COMMIT ORDER, not an
        # uncommitted timestamp, decide which side of the lock a channel falls
        # on (review #159 r3714401797).
        self._acquire_revenue_requirement_guard()
        # Stamp created_at at the actual insertion point (AFTER the guard
        # wait) from the SAME database clock that stamps
        # FinanceMonthCloseORM.locked_at. Two things this avoids: the column's
        # server default now() is the transaction START time, which predates
        # any lock this transaction waited on (r3713449080, r3713841258); and
        # an application-host wall clock is not comparable with the lock's
        # timestamp across hosts, so skew could place a post-lock create
        # before locked_at and recreate the race (r3715073210).
        created_at = serialization_timestamp(self._session)

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
        # updated_at must move with it. Its server default is the same now()
        # (transaction START), so leaving it defaulted persists
        # updated_at < created_at on EVERY create — guaranteed once the guard
        # above makes the transaction wait, and true by milliseconds even when
        # it does not (review #159 r3715427808). A row whose "last modified"
        # predates its own creation is an impossible lifecycle for any
        # consumer that orders by it.
        row.updated_at = created_at
        try:
            # The INSERT gets its OWN savepoint so a lost check-to-insert race
            # discards only this row: `Session.rollback()` here rolled back
            # the TOPMOST transaction, so a direct caller that caught the
            # typed conflict and committed lost every earlier write in the
            # transaction — including work flushed before entering the store's
            # transaction() boundary, which a root rollback discards along
            # with the savepoints protecting it (PR #196 round 3, codex).
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
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

        try:
            # Savepoint-local recovery, mirroring create_channel: a refused
            # flush must not root-rollback a direct caller's earlier work.
            # The MUTATION sits inside the savepoint too, so its rollback
            # restores the object snapshot — a dirty attribute surviving the
            # typed raise would re-flush on the outer transaction later.
            with self._session.begin_nested():
                row.primary_org_unit_id = parsed_primary_company_id
                self._session.flush()
        except IntegrityError as exc:
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
        try:
            # Savepoint-local recovery with the mutation inside the savepoint,
            # mirroring update_mapping — see its comment.
            with self._session.begin_nested():
                row.content_owner_id = normalize_optional_content_owner(content_owner_id)
                self._session.flush()
        except IntegrityError as exc:
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
        require_pre_state: Callable[[ChannelRegistryEntry], None] | None = None,
    ) -> tuple[ChannelRegistryEntry, ChannelRegistryEntry]:
        """Replace a channel's inventory fields from an authoritative import row.

        Returns ``(previous, updated)`` where ``previous`` is the row's state
        observed at the write boundary — re-read under a row lock, not the
        caller's possibly-stale planning snapshot — so audit trails can record
        the values actually replaced.

        ``require_pre_state`` runs against that locked pre-state before any
        assignment, so a refusal leaves the row untouched rather than relying on
        the surrounding transaction to undo it. Here the rollback would in fact
        happen, but the guarantee belongs to the protocol, not to this backend.
        """
        row = self._get_row(youtube_channel_id)
        if row is None:
            raise ChannelRegistryValidationError(f"Unknown channel: {youtube_channel_id}")
        # DEADLOCK ORDER: the tenant-wide guard MUST precede the channel row
        # lock, on EVERY inventory write. The lock-time readiness recheck
        # acquires this guard and then FOR-UPDATEs required channel rows
        # (with_for_update(of=YouTubeChannelORM) in month_close_readiness), so
        # row-then-guard is the opposite order and deadlocks against a
        # concurrent close (review #159 r3713841225). Guarding only the
        # requested-True writes left the same inversion ACROSS rows of one
        # batch: a performance-only row's lock could precede a later row's
        # guard acquisition, and two imports ordering those rows differently
        # deadlock each other (review #159 r3714401827). Unconditional here
        # makes guard-then-rows a total order for every writer. Also
        # serializes the flip against a concurrent lock (r3712948682):
        # whichever commits first is visible to the other's check. No-op off
        # Postgres. Acquired once per request transaction (the lock outlives
        # the call), so a 5000-row import pays one round trip, not 5000.
        self._acquire_revenue_requirement_guard()
        # Row-lock and re-read at the write boundary: a concurrent committed
        # update between the caller's planning read and this write must not be
        # silently hidden — the returned `previous` reflects what this write
        # actually replaced. SQLite's dialect ignores row locks (single-writer
        # anyway); Postgres serializes with any concurrent row writer.
        #
        # key_share=True emits FOR NO KEY UPDATE, not FOR UPDATE: this write
        # never touches the row's key columns, and plain FOR UPDATE conflicts
        # with the FOR KEY SHARE lock that a channel_group_members INSERT
        # takes on its referenced channel. With FOR UPDATE, an import holding
        # a channel and waiting on a group could deadlock a concurrent
        # POST /groups/{id}/members holding that group and needing the
        # channel's FK lock (review #159 r3714644431). FOR NO KEY UPDATE still
        # excludes every other row WRITER, so the write-boundary guarantee is
        # unchanged.
        self._session.refresh(row, with_for_update={"key_share": True})
        previous = self._to_entry(row)
        # Inside the row lock, before any assignment: "is this still the row the
        # operator reviewed?" is asked ahead of "is this write permitted?" (the
        # locked-month guard below), because a plan that no longer applies
        # should send them back to re-review rather than report a month lock on
        # a diff they would no longer approve.
        if require_pre_state is not None:
            require_pre_state(previous)
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
        updated = self._to_entry(row)
        # Stamp ONLY a real change, and from the same clock as create_channel
        # (r3715427808). Two rules in one line:
        #
        # - Why stamp at all: the column's onupdate=now() is this transaction's
        #   START time, and this transaction may have started BEFORE the create
        #   it is now updating committed — READ COMMITTED lets a later
        #   statement see that row — so the default could persist
        #   updated_at < created_at here too.
        # - Why only on a change: an unmodified row has no dirty attribute, so
        #   today it emits no UPDATE at all. Stamping unconditionally would
        #   make every UNCHANGED row of a 5000-row re-import write a row and
        #   pay a clock round trip, and would move a "last modified" time that
        #   nothing modified. The entry comparison is exact — the frozen entry
        #   carries every column this method assigns.
        if updated != previous:
            row.updated_at = serialization_timestamp(self._session)
        self._session.flush()
        return previous, updated

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
