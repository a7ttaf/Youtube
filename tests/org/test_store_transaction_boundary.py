# ============================================================================
# Purpose: Store-level pins for the transaction() boundary itself (review
#   #184, C2) — own writes undone on raise, a FOREIGN write interleaved
#   mid-boundary survives, success keeps everything, and the boundary
#   refuses to nest; independent of the import that wraps it.
# Database/ORM: None (in-memory adapters); the SQL adapters' SAVEPOINT pins
#   live in tests/org/test_sql_channel_registry.py and the end-to-end
#   request-path proof in tests/api/test_channels_import_postgres.py.
# Standards: Every test builds its own stores and seeds explicitly; no
#   shared fixtures, no cross-test state.
# Blast Radius: Test-only.
# Connections: the adapters whose boundary is pinned and the sibling tiers.
#   - File: backend/ums_smart_revenue/org/channel_registry.py -> the
#     in-memory registry whose transaction() is under test.
#   - File: backend/ums_smart_revenue/org/channel_groups.py -> the groups
#     counterpart of the same boundary.
#   - File: tests/org/test_sql_channel_registry.py -> the SQL adapters'
#     SAVEPOINT pins these in-memory pins mirror.
# ============================================================================
"""Store-level pins for the transaction boundary (review #184, C2).

The bulk import wraps its two passes in ``registry.transaction()`` +
``groups.transaction()``; these tests pin the boundary's own contract on the
in-memory adapters, independent of the import: own writes are undone on raise,
a FOREIGN write interleaved mid-boundary survives (a committed SQL row
survives our rollback, and the in-memory idiom for "another transaction" is a
direct dict mutation), success keeps everything, and the boundary refuses to
nest. The SQL adapters open a SAVEPOINT on the request's session transaction —
their direct-caller pin lives in tests/org/test_sql_channel_registry.py and the
end-to-end request-path proof in tests/api/test_channels_import_postgres.py.
"""

import dataclasses
import threading

import pytest

from ums_smart_revenue.auth.audit_service import AuditRecord, InMemoryAuditSink
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistry
from ums_smart_revenue.org.channel_registry import ChannelRegistry, ChannelRegistryEntry

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
SECOND_CHANNEL_ID = "UC3Dci3BzZXDo4jw4dU8KqWg"


class _BoomError(Exception):
    """A failure staged by the tests below, never a real domain error."""


def _seeded_channel(name: str = "Old Name") -> ChannelRegistryEntry:
    return ChannelRegistryEntry(
        youtube_channel_id=CHANNEL_ID,
        channel_name=name,
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        revenue_required=True,
        content_owner_id="TestOwnerAAAAAAAAAAAAA",
    )


def test_registry_boundary_undoes_creates_and_restores_updates_on_raise() -> None:
    """Every write THIS store performed inside the boundary is taken back."""
    registry = ChannelRegistry([_seeded_channel()])

    with pytest.raises(_BoomError), registry.transaction():
        registry.create_channel(
            youtube_channel_id=SECOND_CHANNEL_ID,
            channel_name="Minted Inside",
            primary_company_id=None,
            cms_status="INSIDE_CMS",
            revenue_required=True,
        )
        registry.update_inventory(
            youtube_channel_id=CHANNEL_ID,
            channel_name="Renamed Inside",
            cms_status="INSIDE_CMS",
            content_owner_id="TestOwnerAAAAAAAAAAAAA",
            revenue_required=True,
        )
        raise _BoomError()

    assert registry.get_channel(SECOND_CHANNEL_ID) is None
    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None and stored.channel_name == "Old Name"


def test_registry_boundary_leaves_a_foreign_write_standing() -> None:
    """The undo scope is this store's OWN writes, never "the world at enter".

    The direct dict mutation below is the established in-memory idiom for a
    concurrent writer's COMMITTED change (the import's race tests stage theirs
    the same way): SQL rollback cannot revert another transaction's committed
    rows, so the journal must not either. This staging puts the foreign write
    on a key this boundary ALSO wrote — the sharpest case: the restore is a
    compare-and-restore, so the entry is reinstated only while the key still
    holds the exact object this boundary wrote. Here it does not, and the
    foreign value stands — the SQL end state, where the foreign writer would
    have serialized BEHIND our rollback and then committed on top of it
    (PR #196 round 2, qodo).
    """
    registry = ChannelRegistry([_seeded_channel()])

    with pytest.raises(_BoomError), registry.transaction():
        registry.update_inventory(
            youtube_channel_id=CHANNEL_ID,
            channel_name="Renamed Inside",
            cms_status="INSIDE_CMS",
            content_owner_id="TestOwnerAAAAAAAAAAAAA",
            revenue_required=True,
        )
        # The foreign writer lands AFTER our journaled write, on OUR key; its
        # value is what the store holds when the boundary unwinds.
        current = registry._channels[CHANNEL_ID]
        registry._channels[CHANNEL_ID] = dataclasses.replace(
            current, content_owner_id=None
        )
        raise _BoomError()

    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None
    # The foreign overwrite survives the rollback — not our rename's
    # pre-image, and not our rename either.
    assert stored.channel_name == "Renamed Inside"
    assert stored.content_owner_id is None


def test_registry_boundary_does_not_resurrect_a_foreign_delete() -> None:
    """A key a foreign writer DELETED after our write stays deleted.

    The compare-and-restore's other half: our CREATE journaled
    ``(key, None, written)``, a foreign writer then removed the key, and the
    rollback must not put our pre-image (or anything) back — on SQL the
    delete would have serialized behind our rollback and won.
    """
    registry = ChannelRegistry([])

    with pytest.raises(_BoomError), registry.transaction():
        registry.create_channel(
            youtube_channel_id=CHANNEL_ID,
            channel_name="Minted Then Foreign-Deleted",
            primary_company_id=None,
            cms_status="INSIDE_CMS",
            revenue_required=True,
        )
        del registry._channels[CHANNEL_ID]
        raise _BoomError()

    assert registry.get_channel(CHANNEL_ID) is None


def test_registry_boundary_keeps_writes_on_success_and_journals_nothing_outside() -> None:
    """A clean exit keeps every write; writes outside a boundary never journal."""
    registry = ChannelRegistry([])
    registry.create_channel(
        youtube_channel_id=CHANNEL_ID,
        channel_name="Outside Any Boundary",
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        revenue_required=True,
    )

    with registry.transaction():
        registry.update_inventory(
            youtube_channel_id=CHANNEL_ID,
            channel_name="Committed Inside",
            cms_status="INSIDE_CMS",
            content_owner_id=None,
            revenue_required=True,
        )

    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None and stored.channel_name == "Committed Inside"
    # A later failing boundary must not resurrect anything from before it.
    with pytest.raises(_BoomError), registry.transaction():
        raise _BoomError()
    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None and stored.channel_name == "Committed Inside"


def test_registry_boundary_does_not_nest() -> None:
    """One enter per logical operation; nesting is a different contract.

    One compound ``with`` on purpose: contexts enter left to right, so the
    second ``transaction()`` raises INSIDE the first's scope and
    ``pytest.raises`` absorbs it — the same shape as the nested spelling,
    conformed to the analyzer's collapsible-with rule (PTC-W0062).
    """
    registry = ChannelRegistry([])

    with (
        registry.transaction(),
        pytest.raises(RuntimeError, match="does not nest"),
        registry.transaction(),
    ):
        pass


def test_groups_boundary_undoes_own_writes_and_keeps_a_foreign_group() -> None:
    """Mirror of the registry pins, over the group store's write methods."""
    groups = ChannelGroupRegistry()
    groups.create_group(
        name="seeded", group_type="SECTOR", channel_ids=[], cms_group_id="cms-seeded"
    )

    with pytest.raises(_BoomError), groups.transaction():
        minted = groups.create_group(
            name="minted", group_type="SECTOR", channel_ids=[], cms_group_id="cms-minted"
        )
        seeded = groups.get_group_by_cms_id("cms-seeded")
        assert seeded is not None
        groups.add_members(group_id=seeded.id, channel_ids=[CHANNEL_ID])
        # A concurrent writer's committed group, staged as the race tests
        # stage theirs: directly, invisible to the journal.
        foreign = ChannelGroupEntry(
            id="foreign-group",
            name="foreign",
            group_type="SECTOR",
            active=True,
            channel_ids=(),
            cms_group_id="cms-foreign",
        )
        groups._groups[foreign.id] = foreign
        assert minted.cms_group_id == "cms-minted"
        raise _BoomError()

    assert groups.get_group_by_cms_id("cms-minted") is None
    seeded = groups.get_group_by_cms_id("cms-seeded")
    assert seeded is not None and seeded.channel_ids == ()
    foreign_stored = groups.get_group_by_cms_id("cms-foreign")
    assert foreign_stored is not None and foreign_stored.name == "foreign"


def test_groups_boundary_does_not_nest() -> None:
    """Same compound-with shape as the registry's nest test, same reason."""
    groups = ChannelGroupRegistry()

    with (
        groups.transaction(),
        pytest.raises(RuntimeError, match="does not nest"),
        groups.transaction(),
    ):
        pass


def test_registry_boundary_is_thread_local_and_serializes_writers() -> None:
    """Another thread is a FOREIGN transaction: serialized, not entangled.

    The no-database tier serves the in-memory registry as a long-lived
    singleton, so two requests can run through it on different threads
    (PR #196, codex rounds 2-3). The worker STARTS while our boundary is
    open, so its write serializes BEHIND the store-wide write lock and lands
    only after our rollback — the coarse analogue of a PG row-lock wait. It
    must therefore be joined AFTER the boundary exits (joining inside would
    deadlock on the very lock this test exists to prove). Pinned properties:
    the worker's own boundary is not refused as "nested" by ours, its write
    is neither captured into our journal nor reverted by our rollback, and
    our own write still undoes.
    """
    registry = ChannelRegistry([])
    worker_errors: list[BaseException] = []

    def worker() -> None:
        try:
            with registry.transaction():
                registry.create_channel(
                    youtube_channel_id=SECOND_CHANNEL_ID,
                    channel_name="Other Thread's Channel",
                    primary_company_id=None,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                )
        except BaseException as exc:  # noqa: BLE001 — the test must SEE any failure
            worker_errors.append(exc)

    thread = threading.Thread(target=worker)
    with pytest.raises(_BoomError), registry.transaction():
        registry.create_channel(
            youtube_channel_id=CHANNEL_ID,
            channel_name="This Thread's Channel",
            primary_company_id=None,
            cms_status="INSIDE_CMS",
            revenue_required=True,
        )
        thread.start()
        raise _BoomError()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert worker_errors == []
    assert registry.get_channel(CHANNEL_ID) is None
    survivor = registry.get_channel(SECOND_CHANNEL_ID)
    assert survivor is not None and survivor.channel_name == "Other Thread's Channel"


def test_registry_readers_never_observe_uncommitted_boundary_state() -> None:
    """A reader on another thread sees committed state only, as under MVCC.

    Reads take the same store lock as writes (PR #196 round 5, codex): a
    lock-free read could observe a channel this open boundary minted and is
    about to roll back — a dirty read SQL never shows — letting a concurrent
    request return or plan against a phantom entry. The GIL cannot pin this
    (it gives memory safety, not isolation). The reader STARTS while the
    boundary is open, so it blocks on the store lock until the rollback
    completes; the timed join inside the boundary hands a hypothetical
    lock-free reader a generous window to run and capture the phantom, so
    this test fails on lock-free reads instead of passing by scheduling
    luck, while a blocked reader just waits the timeout out.
    """
    registry = ChannelRegistry([])
    observed: dict[str, object] = {}
    reader_errors: list[BaseException] = []

    def reader() -> None:
        try:
            observed["get"] = registry.get_channel(CHANNEL_ID)
            observed["listed"] = registry.list_channels()
        except BaseException as exc:  # noqa: BLE001 — the test must SEE any failure
            reader_errors.append(exc)

    thread = threading.Thread(target=reader)
    with pytest.raises(_BoomError), registry.transaction():
        registry.create_channel(
            youtube_channel_id=CHANNEL_ID,
            channel_name="Uncommitted",
            primary_company_id=None,
            cms_status="INSIDE_CMS",
            revenue_required=True,
        )
        # The boundary thread reads its OWN uncommitted write (the lock is
        # re-entrant) — the read-your-own-writes half of the SQL parity.
        own_read = registry.get_channel(CHANNEL_ID)
        assert own_read is not None and own_read.channel_name == "Uncommitted"
        thread.start()
        thread.join(timeout=0.2)
        raise _BoomError()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert reader_errors == []
    assert observed["get"] is None
    assert observed["listed"] == []


def test_groups_readers_never_observe_uncommitted_boundary_state() -> None:
    """Mirror of the registry reader-isolation pin, over the group store."""
    groups = ChannelGroupRegistry()
    observed: dict[str, object] = {}
    reader_errors: list[BaseException] = []

    def reader() -> None:
        try:
            observed["by_cms"] = groups.get_group_by_cms_id("cms-minted")
            observed["listed"] = groups.list_groups()
        except BaseException as exc:  # noqa: BLE001 — the test must SEE any failure
            reader_errors.append(exc)

    thread = threading.Thread(target=reader)
    with pytest.raises(_BoomError), groups.transaction():
        groups.create_group(
            name="minted", group_type="SECTOR", channel_ids=[], cms_group_id="cms-minted"
        )
        thread.start()
        thread.join(timeout=0.2)
        raise _BoomError()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert reader_errors == []
    assert observed["by_cms"] is None
    assert observed["listed"] == []


def _audit_record(event_type: str) -> AuditRecord:
    """A minimal record; only identity matters to the boundary tests."""
    return AuditRecord(
        user_id="user-1",
        event_type=event_type,
        entity_type=None,
        entity_id=None,
        scope_type=None,
        scope_id=None,
        request_id=None,
        reason=None,
        details={},
        sensitive=False,
        permission=None,
    )


def test_in_memory_sink_boundary_truncates_only_its_own_appends() -> None:
    """The sink's transaction removes exactly the records appended inside it."""
    sink = InMemoryAuditSink()
    before = _audit_record("BEFORE_BOUNDARY")
    sink.append(before)

    with pytest.raises(_BoomError), sink.transaction():
        sink.append(_audit_record("INSIDE_BOUNDARY"))
        sink.append(_audit_record("ALSO_INSIDE"))
        raise _BoomError()

    assert sink.records == [before]
