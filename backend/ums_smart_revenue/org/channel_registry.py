# ============================================================================
# Purpose: Channel registry domain contract — the ChannelRegistryEntry value
#   object, the ChannelRegistryStore protocol, typed registry errors, the
#   in-memory reference implementation, and the shared derivation/
#   normalization helpers both implementations use.
# Database/ORM: None here; backend/ums_smart_revenue/org/sql_channel_registry.py
#   is the SQL implementation of the same protocol.
# Standards: Typed domain errors (ChannelRegistryError subclasses); frozen
#   dataclass entries; derivation logic shared via helpers so the in-memory
#   and SQL registries cannot drift.
# Blast Radius: Channel inventory/mapping semantics everywhere the registry
#   protocol is consumed (channel APIs, bulk import, connectors targeting).
# Connections:
#   - File: backend/ums_smart_revenue/org/sql_channel_registry.py -> SQL impl.
#   - File: backend/ums_smart_revenue/api/channels.py -> route consumers.
# ============================================================================
"""Channel registry domain contract, errors, and in-memory implementation."""

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from ums_smart_revenue.org.bootstrap_registry import BOOTSTRAP_CHANNELS


@dataclass(frozen=True)
class ChannelRegistryEntry:
    youtube_channel_id: str
    channel_name: str
    primary_company_id: str | None
    cms_status: str
    revenue_required: bool
    content_owner_id: str | None = None
    revenue_source_status: str = "MISSING_REVENUE_SOURCE"
    active: bool = True

    def to_api(self) -> dict[str, object]:
        return {
            "youtube_channel_id": self.youtube_channel_id,
            "channel_name": self.channel_name,
            "primary_company_id": self.primary_company_id,
            "cms_status": self.cms_status,
            "content_owner_id": self.content_owner_id,
            "revenue_required": self.revenue_required,
            "revenue_source_status": self.revenue_source_status,
            "active": self.active,
        }


class ChannelRegistryError(ValueError):
    pass


class ChannelRegistryConflictError(ChannelRegistryError):
    pass


class ChannelRegistryValidationError(ChannelRegistryError):
    pass


class ChannelMappingLockedMonthError(ChannelRegistryError):
    # ========================================================================
    # Purpose: Signal that a channel mapping change is blocked because the
    #   channel carries revenue facts in a LOCKED finance month, so re-parenting
    #   it would silently rewrite that closed month's company/sector attribution.
    # Database/ORM: None (raised by the SQL registry after a read-only check on
    #   MonthlyChannelRevenueFactORM x FinanceMonthCloseORM).
    # Standards: Typed domain error; the route boundary maps it to HTTP 409.
    # Blast Radius: Finance attribution integrity, audit (a rejected change must
    #   not be audited). No Neo4j, no exports.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_registry.py ->
    #       raised by SqlAlchemyChannelRegistry.update_mapping.
    #   - File: backend/ums_smart_revenue/api/channels.py -> translated to 409.
    # ========================================================================
    pass


class ChannelRevenueRequirementLockedMonthError(ChannelRegistryError):
    # ========================================================================
    # Purpose: Signal that flipping a channel's revenue_required flag ON is
    #   blocked because a LOCKED finance month has no revenue fact for it.
    #   Month-close readiness evaluates the CURRENT flag, so the flip would
    #   retroactively make an already-finalized month report a missing
    #   required fact and no longer satisfy the conditions it was locked
    #   under.
    # Database/ORM: None (raised by the SQL registry after a read-only check
    #   on FinanceMonthCloseORM x MonthlyChannelRevenueFactORM).
    # Standards: Typed domain error; the import route maps it to HTTP 409.
    #   Mirrors ChannelMappingLockedMonthError above.
    # Blast Radius: Finance close integrity, audit (a rejected flip must not
    #   be audited). No Neo4j, no exports.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_registry.py ->
    #       raised by SqlAlchemyChannelRegistry.update_inventory.
    #   - File: backend/ums_smart_revenue/finance/month_close_readiness.py ->
    #       the readiness query this guard keeps stable for LOCKED months.
    # ========================================================================
    pass


class ChannelRegistryStore(Protocol):
    def list_channels(self) -> list[ChannelRegistryEntry]:
        pass

    def list_channels_by_ids(
        self, youtube_channel_ids: set[str], *, include_inactive: bool = False
    ) -> list[ChannelRegistryEntry]:
        pass

    def get_channel(self, youtube_channel_id: str) -> ChannelRegistryEntry | None:
        pass

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
        pass

    def update_mapping(
        self, *, youtube_channel_id: str, primary_company_id: str | None
    ) -> ChannelRegistryEntry:
        pass

    def update_content_owner(
        self, *, youtube_channel_id: str, content_owner_id: str | None
    ) -> ChannelRegistryEntry:
        pass

    # ========================================================================
    # Purpose: The registry's inventory WRITE, declared as a compare-and-update:
    #   re-read the row at the write boundary, let the caller judge that
    #   pre-state, and only then replace the four inventory fields — returning
    #   what was actually replaced so the caller can audit it.
    # Database/ORM: YouTubeChannelORM in the SQL adapter (row-locked re-read,
    #   then assignment); a dict entry in the in-memory one. Both also re-derive
    #   revenue_source_status when revenue_required flips.
    # Standards: ORDERING IS THE CONTRACT, not an implementation detail.
    #   ``require_pre_state`` MUST be invoked with the write-boundary pre-state
    #   after the locked re-read and BEFORE any mutation, and it may raise to
    #   refuse the write. Calling it afterwards — or judging the returned
    #   ``previous`` in the caller — makes correctness depend on the store
    #   having a transaction to roll back, which a non-transactional adapter
    #   does not: the row keeps the roster values under a request that answered
    #   409 (review #184). ``previous`` must be the RE-READ state, never the
    #   caller's planning snapshot, or an audit trail records a diff that did
    #   not happen. Policy belongs to the caller; adapters owe only the
    #   ordering.
    # Blast Radius: FINANCE-SCOPE. The four inventory fields drive connector
    #   ingest targeting (cms_status/content_owner_id) and, through the
    #   revenue_required flip, the revenue_source_status classification that
    #   feeds missing-official-revenue state. An adapter that ignores the
    #   ordering reintroduces a partial write on a refused import.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     _write_inventory_row, which supplies the guard closure.
    #   - File: backend/ums_smart_revenue/org/sql_channel_registry.py -> the
    #     row-locked adapter (lock order: tenant guard before channel row).
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
        observed at the write boundary (re-read under a row lock where the
        backend supports it), NOT the caller's possibly-stale planning
        snapshot — audit trails must record the values actually replaced.

        ``require_pre_state`` makes this a COMPARE-AND-UPDATE. An implementation
        must call it with the write-boundary pre-state after its locked re-read
        and BEFORE mutating anything, letting it raise to refuse the write. The
        ordering is the whole contract: a caller that validates the returned
        ``previous`` afterwards is relying on the store's transaction to undo
        the mutation, which a non-transactional implementation cannot do — it
        would leave the roster values written under a request that answered 409
        (review #184). Policy stays with the caller; implementations owe only
        the ordering.
        """

    # ========================================================================
    # Purpose: The store's ATOMICITY contract, made explicit: a caller wraps
    #   one logical multi-write operation (the bulk import's two passes) so a
    #   raise anywhere inside undoes every write THIS store performed for it.
    # Database/ORM: The SQL adapter opens a SAVEPOINT on the REQUEST's
    #   Session transaction — rolled back to on exception, released on
    #   success, never a commit — so the promise below holds even for a
    #   direct caller that catches the exception and later commits the
    #   session. The in-memory adapter has no database, which is the reason
    #   this method exists at the protocol: it journals its own writes and
    #   replays them backwards on raise (review #184, C2).
    # Standards: The undo scope is THIS STORE'S OWN WRITES, not "the world at
    #   enter" — SQL rollback cannot and must not revert another transaction's
    #   committed rows, and the import's race tests assert exactly that (a
    #   channel renamed or a group minted by a concurrent writer mid-apply
    #   survives the refusal). Adapters must NOT commit here — commit stays
    #   with whoever owns the session/request lifecycle. The boundary is not
    #   nestable and not a savepoint: one enter per logical operation, and a
    #   caller needing partial undo is asking for a different contract.
    #   Exceptions always propagate; the boundary undoes state, never
    #   outcomes.
    # Blast Radius: Whether a refused import can leave PARTIAL channel writes
    #   behind on adapters without a database underneath (direct/test/
    #   bootstrap callers). Production SQL behaviour is unchanged by design.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     apply_channel_import, the one boundary consumer, which wraps BOTH
    #     passes and the audit flush in this plus the group store's boundary.
    #   - File: backend/ums_smart_revenue/org/sql_channel_registry.py -> the
    #     delegating SQL implementation.
    # ========================================================================
    def transaction(self) -> AbstractContextManager[None]:
        """Return a context manager making the wrapped writes all-or-nothing.

        On a clean exit the writes stand (their durability still governed by
        whoever owns the session/request lifecycle — this is never a commit).
        On an exception every write this store performed inside the boundary
        is undone — writes by OTHER actors interleaved during it stand, as a
        foreign transaction's committed rows survive a SQL rollback — and the
        exception propagates unchanged.
        """
        ...


class ChannelRegistry:
    def __init__(self, channels: list[ChannelRegistryEntry] | None = None):
        self._channels: dict[str, ChannelRegistryEntry] = {}
        # Boundary state is PER-THREAD (threading.local): the no-database tier
        # serves this store as a long-lived singleton, so a threaded server
        # can run two requests through it at once — one request's journal
        # must neither capture nor revert another thread's writes, and a
        # second thread's own boundary must not be refused as "nested"
        # (PR #196 round 2, codex). The `undo` attribute holds the active
        # journal — (key, pre-image-or-None) per write — or None.
        self._txn = threading.local()
        # The store-wide lock — the coarse in-memory analogue of the SQL
        # tier's transaction isolation. A transaction() boundary holds it
        # for its whole duration, and every public method — writes AND
        # reads — takes it (re-entrant, so the boundary-holding thread
        # passes through):
        #   - another thread's write serializes BEHIND an open boundary
        #     and lands after its rollback, never building on state the
        #     rollback is about to retract (PR #196 round 3, codex);
        #   - another thread's READ serializes the same way, so an open
        #     boundary's uncommitted writes are never observable off-thread
        #     — the GIL gives memory safety, not isolation, and SQL's MVCC
        #     never shows uncommitted rows (PR #196 round 5, codex). The
        #     boundary thread reads its own writes, matching SQL.
        # Iterating readers also take list(...) snapshots of the dict views
        # (PR #196 round 4, qodo) — redundant under the lock, kept as
        # iteration safety independent of the lock discipline.
        self._lock = threading.RLock()
        for channel in channels or []:
            if channel.youtube_channel_id in self._channels:
                raise ChannelRegistryConflictError(
                    f"Duplicate channel id: {channel.youtube_channel_id}"
                )
            self._channels[channel.youtube_channel_id] = channel

    # ========================================================================
    # Purpose: The in-memory implementation of the store's transaction
    #   boundary — journal this store's own writes on this thread, undo
    #   exactly those on raise.
    # Database/ORM: None (in-memory dict store); the SQL implementation in
    #   sql_channel_registry.py maps the same protocol method to a SAVEPOINT
    #   on the request session.
    # Standards: An UNDO JOURNAL, deliberately not a whole-dict snapshot —
    #   the two differ on exactly the case the import's race tests pin: a
    #   concurrent writer's change landing mid-apply. SQL rollback discards
    #   only the failing transaction's writes, so parity demands undoing only
    #   what THIS store's write methods performed inside the boundary.
    #   Journal state is PER-THREAD (threading.local), because this store is
    #   a long-lived singleton on the no-database tier: another thread's
    #   writes must be neither captured nor reverted, and its own boundary
    #   must not be refused as nested. Fails loud on same-thread nesting.
    # Blast Radius: Whether a refused import leaves partial channel writes on
    #   direct/test/bootstrap callers. No finance math, no audit of its own.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     apply_channel_import, the boundary's consumer.
    #   - File: backend/ums_smart_revenue/org/sql_channel_registry.py -> the
    #     SAVEPOINT implementation of the same protocol method.
    # ========================================================================
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Journal this store's own writes on this thread; undo them on raise.

        Each write method records ``(key, pre-image, written)`` just before
        mutating; on raise the entries replay in reverse, and each one
        restores its pre-image ONLY while the key still holds the exact entry
        this boundary wrote (identity compare — entries are frozen and every
        write installs a fresh object). A key holding anything else was
        overwritten or deleted by a FOREIGN writer after us; SQL's row lock
        would have serialized that writer BEHIND our rollback, ending on
        their value, so the journal leaves it standing rather than clobbering
        it with a stale pre-image (PR #196 round 2, qodo). Same-key writes
        within one boundary chain naturally: each entry's ``written`` is the
        next entry's pre-image, so the reversed replay walks back to the
        oldest pre-image. Test-staged "concurrent writers" mutate
        ``_channels`` directly — a foreign transaction's committed write is
        not this store's write, and the journal must not see it.

        The boundary holds the store lock for its whole duration, and every
        public read and write takes that lock, so other threads neither
        build on nor observe uncommitted boundary state; the boundary
        thread itself reads its own writes (re-entrant lock), matching SQL.

        Raises:
            RuntimeError: when entered while this THREAD already holds an
                open boundary — one enter per logical operation; nesting is
                a different contract.
        """
        if getattr(self._txn, "undo", None) is not None:
            raise RuntimeError("ChannelRegistry.transaction does not nest")
        with self._lock:
            undo: list[tuple[str, ChannelRegistryEntry | None, ChannelRegistryEntry]] = []
            self._txn.undo = undo
            try:
                yield
            except BaseException:
                for key, previous, written in reversed(undo):
                    if self._channels.get(key) is not written:
                        continue
                    if previous is None:
                        self._channels.pop(key, None)
                    else:
                        self._channels[key] = previous
                raise
            finally:
                self._txn.undo = None

    def _journal(self, youtube_channel_id: str, written: ChannelRegistryEntry) -> None:
        """Record (key, pre-image, written-entry) for the active boundary, if any."""
        undo: list[tuple[str, ChannelRegistryEntry | None, ChannelRegistryEntry]] | None = getattr(
            self._txn, "undo", None
        )
        if undo is not None:
            undo.append(
                (youtube_channel_id, self._channels.get(youtube_channel_id), written)
            )

    def list_channels(self) -> list[ChannelRegistryEntry]:
        """Return every ACTIVE channel, sorted by youtube_channel_id.

        Committed reads: takes the store lock, so an open transaction()
        boundary's uncommitted writes are never observed off-thread (see
        the lock note in ``__init__``).
        """
        with self._lock:
            return sorted(
                [channel for channel in list(self._channels.values()) if channel.active],
                key=lambda channel: channel.youtube_channel_id,
            )

    def list_channels_by_ids(
        self, youtube_channel_ids: set[str], *, include_inactive: bool = False
    ) -> list[ChannelRegistryEntry]:
        """Return the requested channels sorted by youtube_channel_id.

        Inactive channels are omitted unless ``include_inactive`` is set;
        unknown ids are silently absent (the caller diffs). Committed reads
        — see the store lock note in ``__init__``.
        """
        with self._lock:
            return sorted(
                [
                    channel
                    for channel_id, channel in list(self._channels.items())
                    if channel_id in youtube_channel_ids
                    and (channel.active or include_inactive)
                ],
                key=lambda channel: channel.youtube_channel_id,
            )

    def get_channel(self, youtube_channel_id: str) -> ChannelRegistryEntry | None:
        """Return the channel (active or not) by id, or None. Committed read."""
        with self._lock:
            return self._channels.get(youtube_channel_id)

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
        normalized_company_id = _parse_optional_uuid(primary_company_id, "primary_company_id")
        with self._lock:
            if youtube_channel_id in self._channels:
                raise ChannelRegistryConflictError(
                    f"Channel already exists: {youtube_channel_id}"
                )
            initial_revenue_source_status = (
                "MISSING_REVENUE_SOURCE" if revenue_required else "PERFORMANCE_ONLY"
            )
            channel = ChannelRegistryEntry(
                youtube_channel_id=youtube_channel_id,
                channel_name=channel_name,
                primary_company_id=normalized_company_id,
                cms_status=cms_status,
                revenue_required=revenue_required,
                content_owner_id=normalize_optional_content_owner(content_owner_id),
                revenue_source_status=initial_revenue_source_status,
            )
            self._journal(youtube_channel_id, channel)
            self._channels[youtube_channel_id] = channel
            return channel

    def update_mapping(
        self, *, youtube_channel_id: str, primary_company_id: str | None
    ) -> ChannelRegistryEntry:
        normalized_company_id = _parse_optional_uuid(primary_company_id, "primary_company_id")
        with self._lock:
            existing = self._channels.get(youtube_channel_id)
            if existing is None:
                raise KeyError(youtube_channel_id)
            updated = ChannelRegistryEntry(
                youtube_channel_id=existing.youtube_channel_id,
                channel_name=existing.channel_name,
                primary_company_id=normalized_company_id,
                cms_status=existing.cms_status,
                revenue_required=existing.revenue_required,
                content_owner_id=existing.content_owner_id,
                revenue_source_status=existing.revenue_source_status,
                active=existing.active,
            )
            self._journal(youtube_channel_id, updated)
            self._channels[youtube_channel_id] = updated
            return updated

    def update_content_owner(
        self, *, youtube_channel_id: str, content_owner_id: str | None
    ) -> ChannelRegistryEntry:
        with self._lock:
            existing = self._channels.get(youtube_channel_id)
            if existing is None:
                raise KeyError(youtube_channel_id)
            updated = ChannelRegistryEntry(
                youtube_channel_id=existing.youtube_channel_id,
                channel_name=existing.channel_name,
                primary_company_id=existing.primary_company_id,
                cms_status=existing.cms_status,
                revenue_required=existing.revenue_required,
                content_owner_id=normalize_optional_content_owner(content_owner_id),
                revenue_source_status=existing.revenue_source_status,
                active=existing.active,
            )
            self._journal(youtube_channel_id, updated)
            self._channels[youtube_channel_id] = updated
            return updated

    # ========================================================================
    # Purpose: In-memory twin of the SQL registry's bulk-import inventory
    #   write — replaces a channel's name, CMS status, content owner, and
    #   revenue_required from an authoritative roster row.
    # Database/ORM: None (in-memory dict store); the SQL implementation in
    #   sql_channel_registry.py owns the ORM write, row lock, and month-close
    #   guard for the same protocol method.
    # Standards: Returns (previous, updated) so audit trails record the
    #   values actually replaced; revenue_source_status re-derives ONLY on a
    #   revenue_required flip via derive_revenue_source_status, never on an
    #   unrelated refresh. No locked-month guard here — the in-memory store
    #   backs tests and bootstrap flows with no finance close rows.
    # Blast Radius: Channel inventory + revenue-source classification in
    #   in-memory-backed tests and bootstrap. No finance totals, no audit.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     apply_channel_import consumes the (previous, updated) contract.
    #   - File: backend/ums_smart_revenue/org/sql_channel_registry.py ->
    #     production implementation of the same protocol method.
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

        Returns ``(previous, updated)`` so audit trails record the values this
        write actually replaced (mirrors the SQL registry's write-boundary
        re-read; in memory the current entry IS the write-boundary state).
        """
        with self._lock:
            current = self._channels.get(youtube_channel_id)
            if current is None:
                raise ChannelRegistryValidationError(f"Unknown channel: {youtube_channel_id}")
            # BEFORE the mutation, and for this store that ordering is the only
            # protection there is: a dict has no transaction to roll back, so a
            # caller that validated `previous` after the fact would answer 409
            # with the roster values already installed (review #184).
            if require_pre_state is not None:
                require_pre_state(current)
            updated = replace(
                current,
                channel_name=channel_name,
                cms_status=cms_status,
                content_owner_id=content_owner_id,
                revenue_required=revenue_required,
                # Re-derive the source status only when revenue_required
                # actually flips; an unrelated inventory refresh must not
                # clobber a proven OFFICIAL_CMS_REVENUE /
                # OFFICIAL_MANUAL_IMPORT classification back to
                # MISSING_REVENUE_SOURCE.
                revenue_source_status=derive_revenue_source_status(
                    current_status=current.revenue_source_status,
                    current_revenue_required=current.revenue_required,
                    revenue_required=revenue_required,
                ),
            )
            # AFTER require_pre_state: a refused write must leave no journal
            # entry, and the pre-image recorded is the write-boundary state
            # the undo must reinstate.
            self._journal(youtube_channel_id, updated)
            self._channels[youtube_channel_id] = updated
            return current, updated


def bootstrap_channel_registry() -> ChannelRegistry:
    channels = [
        ChannelRegistryEntry(
            youtube_channel_id=channel.youtube_channel_id,
            channel_name=channel.youtube_channel_id,
            primary_company_id=_parse_optional_uuid(
                channel.primary_org_unit_id, "primary_company_id"
            ),
            cms_status="UNKNOWN",
            revenue_required=True,
            content_owner_id=None,
            revenue_source_status="MISSING_REVENUE_SOURCE",
            active=channel.active,
        )
        for channel in BOOTSTRAP_CHANNELS
    ]
    return ChannelRegistry(channels)


def _parse_optional_uuid(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ChannelRegistryValidationError(f"{field_name} must be a valid UUID") from exc


# ============================================================================
# Purpose: Single source of truth for how an inventory update derives the
#   channel's revenue_source_status — the finance-facing classification that
#   marks a channel's revenue evidence as official, missing, or not required.
# Database/ORM: None (pure function); both registry implementations call it.
# Standards: Re-derive ONLY on a revenue_required flip; preserve otherwise, so
#   a roster refresh can never downgrade OFFICIAL_CMS_REVENUE /
#   OFFICIAL_MANUAL_IMPORT back to MISSING_REVENUE_SOURCE (review #159
#   r3706996021).
# Blast Radius: Channel revenue-source classification feeding missing-source
#   monitors and registry issue feeds. No finance totals, no allocation.
# Connections:
#   - File: backend/ums_smart_revenue/org/sql_channel_registry.py ->
#     update_inventory caller.
#   - File: backend/ums_smart_revenue/org/channel_registry.py ->
#     ChannelRegistry.update_inventory caller (same module, above).
# ============================================================================
def derive_revenue_source_status(
    *,
    current_status: str,
    current_revenue_required: bool,
    revenue_required: bool,
) -> str:
    """Return the revenue_source_status an inventory update should persist.

    The status is re-derived ONLY when ``revenue_required`` changes: flipping
    to required starts the channel at MISSING_REVENUE_SOURCE, flipping to
    not-required parks it at PERFORMANCE_ONLY. When the flag is unchanged the
    existing status is preserved, so an import that merely refreshes the name,
    CMS status, or content owner cannot downgrade an established
    OFFICIAL_CMS_REVENUE / OFFICIAL_MANUAL_IMPORT classification.
    """
    if revenue_required == current_revenue_required:
        return current_status
    return "MISSING_REVENUE_SOURCE" if revenue_required else "PERFORMANCE_ONLY"


def normalize_optional_content_owner(value: str | None) -> str | None:
    """Normalize a content owner id, treating blank/None as unset (None).

    The content owner id is a free-form Google CMS string matched against the
    connector account id, not a UUID. The API layer rejects a present-but-blank
    value; this defensive normalization keeps direct registry callers from
    persisting a whitespace-only owner that could never match an account.
    """
    if value is None:
        return None
    return value.strip() or None
