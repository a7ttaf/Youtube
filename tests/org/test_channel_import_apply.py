"""Domain-side apply execution: write-boundary diffs and archived-group races.

Covers the two plan-to-apply race guards from PR #159 review rounds
(r3712948694, r3712948688): the durable audit diff is computed from what the
registry write ACTUALLY replaced, and a group archived after planning fails
the apply closed instead of silently mutating a retired group.
"""

from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace

import pytest

from ums_smart_revenue.auth.audit_service import AuditRecord, InMemoryAuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistry
from ums_smart_revenue.org.channel_import import (
    ChannelImportGroupAction,
    ChannelImportOutcome,
    ChannelImportPlan,
    ChannelImportPlanEntry,
    parse_channel_import_csv,
    plan_channel_import,
)
from ums_smart_revenue.org.channel_import_apply import (
    ChannelImportAdoptableGroupError,
    ChannelImportArchivedGroupError,
    ChannelImportGroupActionDivergedError,
    ChannelImportGroupOwnerMismatchError,
    _group_write_batches,
    apply_channel_import,
)
from ums_smart_revenue.org.channel_registry import ChannelRegistry, ChannelRegistryEntry

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
# A second valid channel id, for plans that must write more than one row.
SECOND_CHANNEL_ID = "UC3Dci3BzZXDo4jw4dU8KqWg"
CONTENT_OWNER = "TestOwnerAAAAAAAAAAAAA"
ACTOR = UserPrincipal(user_id="user-1", email="user@example.com")


def _plan(entry: ChannelImportPlanEntry) -> ChannelImportPlan:
    counts = {outcome.value: 0 for outcome in ChannelImportOutcome}
    counts[entry.outcome.value] = 1
    return ChannelImportPlan(entries=(entry,), counts=counts)


def _apply(plan: ChannelImportPlan, registry, groups, sink) -> None:
    apply_channel_import(
        plan,
        registry=registry,
        groups=groups,
        audit_sink=sink,
        actor=ACTOR,
        scope=AccessScope.global_scope(),
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
        reason="Quarterly CMS roster load",
        filename="roster.csv",
    )


def test_audit_diff_reflects_write_boundary_not_the_stale_plan() -> None:
    """A concurrent change between plan and apply must not be hidden."""
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Concurrently Renamed",  # changed AFTER planning
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )
    sink = InMemoryAuditSink()
    stale_entry = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.UPDATE,
        channel_name="New Name",
        group_id=None,
        revenue_required=True,
        # The plan believed the old name was "Planned Old" — stale by now.
        changes={"channel_name": ("Planned Old", "New Name")},
    )

    _apply(_plan(stale_entry), registry, ChannelGroupRegistry(), sink)

    updated_events = [r for r in sink.records if r.event_type == "CHANNEL_UPDATED"]
    assert len(updated_events) == 1
    updated = updated_events[0]
    assert updated.details["changes"] == {
        "channel_name": {"from": "Concurrently Renamed", "to": "New Name"}
    }


def test_unchanged_row_writes_through_and_audits_the_healed_drift() -> None:
    """An UNCHANGED row must not preserve a concurrent writer's value.

    Planning classified the row as UNCHANGED from a stale snapshot; a
    concurrent update landed before apply. The file wins: the write-boundary
    write restores the roster value and audits the real diff (review #159
    r3713841231).
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Drifted By Concurrent Patch",  # changed AFTER planning
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )
    sink = InMemoryAuditSink()
    entry = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.UNCHANGED,
        channel_name="Roster Name",
        group_id=None,
        revenue_required=True,
    )

    _apply(_plan(entry), registry, ChannelGroupRegistry(), sink)

    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None and stored.channel_name == "Roster Name"
    updated_events = [r for r in sink.records if r.event_type == "CHANNEL_UPDATED"]
    assert len(updated_events) == 1
    assert updated_events[0].details["changes"] == {
        "channel_name": {"from": "Drifted By Concurrent Patch", "to": "Roster Name"}
    }


def test_truly_unchanged_row_stays_audit_quiet() -> None:
    """A re-import whose values already match writes no CHANNEL_UPDATED event."""
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Roster Name",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )
    sink = InMemoryAuditSink()
    entry = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.UNCHANGED,
        channel_name="Roster Name",
        group_id=None,
        revenue_required=True,
    )

    _apply(_plan(entry), registry, ChannelGroupRegistry(), sink)

    assert [r.event_type for r in sink.records] == ["CHANNEL_IMPORTED"]


def test_summary_counts_come_from_the_write_boundary_not_the_plan() -> None:
    """A planned UPDATE that replaced nothing must not be summarized as UPDATE.

    The plan's outcome came from a possibly-stale snapshot. When a concurrent
    writer commits the roster's own values before the apply locks the row, the
    write replaces nothing and no CHANNEL_UPDATED event is recorded — so a
    summary copied from ``plan.counts`` would claim an update the rest of the
    trail cannot substantiate (review #159 r3715617737).
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Roster Name",  # a concurrent writer already applied it
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )
    sink = InMemoryAuditSink()
    entry = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.UPDATE,  # planned against the stale snapshot
        channel_name="Roster Name",
        group_id=None,
        revenue_required=True,
    )

    _apply(_plan(entry), registry, ChannelGroupRegistry(), sink)

    assert [r.event_type for r in sink.records] == ["CHANNEL_IMPORTED"]
    summaries = [r for r in sink.records if r.event_type == "CHANNEL_IMPORTED"]
    assert len(summaries) == 1
    counts = summaries[0].details["counts"]
    assert counts[ChannelImportOutcome.UPDATE.value] == 0
    assert counts[ChannelImportOutcome.UNCHANGED.value] == 1


def test_summary_counts_record_a_real_update_and_create() -> None:
    """The applied tally still reports writes that genuinely happened."""
    second_id = "UC3Dci3BzZXDo4jw4dU8KqWg"
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Stale Name",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )
    sink = InMemoryAuditSink()
    entries = (
        ChannelImportPlanEntry(
            row_number=1,
            youtube_channel_id=CHANNEL_ID,
            outcome=ChannelImportOutcome.UPDATE,
            channel_name="Roster Name",
            group_id=None,
            revenue_required=True,
        ),
        ChannelImportPlanEntry(
            row_number=2,
            youtube_channel_id=second_id,
            outcome=ChannelImportOutcome.CREATE,
            channel_name="Brand New",
            group_id=None,
            revenue_required=True,
        ),
    )
    counts = {outcome.value: 0 for outcome in ChannelImportOutcome}
    counts[ChannelImportOutcome.UPDATE.value] = 1
    counts[ChannelImportOutcome.CREATE.value] = 1

    _apply(
        ChannelImportPlan(entries=entries, counts=counts),
        registry,
        ChannelGroupRegistry(),
        sink,
    )

    summaries = [r for r in sink.records if r.event_type == "CHANNEL_IMPORTED"]
    assert len(summaries) == 1
    applied = summaries[0].details["counts"]
    assert applied[ChannelImportOutcome.UPDATE.value] == 1
    assert applied[ChannelImportOutcome.CREATE.value] == 1
    assert applied[ChannelImportOutcome.UNCHANGED.value] == 0


def test_rows_apply_in_deterministic_channel_order() -> None:
    """Execution order is (channel id, group id), not CSV order.

    Every row write takes a row lock held to commit, so two imports listing
    the same channels in opposite file order would deadlock without a total
    order (review #159 r3714142167).
    """
    second_id = "UC3Dci3BzZXDo4jw4dU8KqWg"
    registry = ChannelRegistry([])
    sink = InMemoryAuditSink()
    entries = (
        # CSV order is reversed relative to channel-id order.
        ChannelImportPlanEntry(
            row_number=1,
            youtube_channel_id=second_id,
            outcome=ChannelImportOutcome.CREATE,
            channel_name="Second",
            group_id=None,
            revenue_required=True,
        ),
        ChannelImportPlanEntry(
            row_number=2,
            youtube_channel_id=CHANNEL_ID,
            outcome=ChannelImportOutcome.CREATE,
            channel_name="First",
            group_id=None,
            revenue_required=True,
        ),
    )
    counts = {outcome.value: 0 for outcome in ChannelImportOutcome}
    counts[ChannelImportOutcome.CREATE.value] = 2

    _apply(
        ChannelImportPlan(entries=entries, counts=counts), registry, ChannelGroupRegistry(), sink
    )

    created = [r.entity_id for r in sink.records if r.event_type == "CHANNEL_CREATED"]
    assert created == sorted([CHANNEL_ID, second_id])


def test_nul_in_upload_filename_does_not_reach_audit_details() -> None:
    """A NUL-bearing filename is sanitized, not persisted into JSONB details."""
    registry = ChannelRegistry([])
    sink = InMemoryAuditSink()
    entry = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.CREATE,
        channel_name="Alpha News",
        group_id=None,
        revenue_required=True,
    )

    apply_channel_import(
        _plan(entry),
        registry=registry,
        groups=ChannelGroupRegistry(),
        audit_sink=sink,
        actor=ACTOR,
        scope=AccessScope.global_scope(),
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
        reason="Quarterly CMS roster load",
        filename="roster\x00.csv",
    )

    summaries = [r for r in sink.records if r.event_type == "CHANNEL_IMPORTED"]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.details["filename"] == "roster.csv"
    assert "\x00" not in summary.details["filename"]


class _ArchivedAtApplyGroups(ChannelGroupRegistry):
    """Simulate a group archived between planning and the apply lookup."""

    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        group = super().get_group_by_cms_id(cms_group_id, for_update=for_update)
        if group is not None and for_update:
            return replace(group, active=False)
        return group


def test_group_pass_failure_undoes_the_inventory_writes() -> None:
    """A pass-2 refusal takes pass 1's channel writes back — on THIS tier (C2).

    The window review #184 named: by the time the group pass raises, every
    channel row has been written, and before the store transaction boundary
    the in-memory registry had no way to take those back — a 409 answered
    while the CREATEs stayed installed. Two rows on purpose: the first carries
    no group at all, so its write is undone purely by the boundary, not by
    anything group-shaped. The sink stays EMPTY because the audit buffer only
    flushes after both passes succeed — without that, restoring the stores
    would have produced the worse state, an audit trail describing writes
    that were undone.
    """
    registry = ChannelRegistry([])
    groups = _ArchivedAtApplyGroups()
    groups.create_group(
        name="cms-tv",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id=CONTENT_OWNER,
    )
    sink = InMemoryAuditSink()
    plain_row = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=SECOND_CHANNEL_ID,
        outcome=ChannelImportOutcome.CREATE,
        channel_name="Beta News",
        group_id=None,
        revenue_required=True,
    )
    group_row = ChannelImportPlanEntry(
        row_number=2,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.CREATE,
        channel_name="Alpha News",
        group_id="cms-tv",
        revenue_required=True,
    )
    counts = {outcome.value: 0 for outcome in ChannelImportOutcome}
    counts[ChannelImportOutcome.CREATE.value] = 2

    with pytest.raises(ChannelImportArchivedGroupError, match="cms-tv"):
        _apply(
            ChannelImportPlan(entries=(plain_row, group_row), counts=counts),
            registry,
            groups,
            sink,
        )

    # BOTH pass-1 writes are gone, the group is untouched, and not a single
    # audit record reached the sink.
    assert registry.get_channel(CHANNEL_ID) is None
    assert registry.get_channel(SECOND_CHANNEL_ID) is None
    stored = groups.get_group_by_cms_id("cms-tv")
    assert stored is not None and stored.channel_ids == ()
    assert sink.records == []


class _UnavailableSink:
    """AuditSink double whose very first append raises — a sink outage."""

    @staticmethod
    def append(record: AuditRecord) -> None:
        """Refuse every record; static because the outage needs no state."""
        raise RuntimeError("sink unavailable")

    @staticmethod
    def transaction() -> AbstractContextManager[None]:
        """No record ever lands, so there is nothing for a boundary to undo."""
        return nullcontext()


def test_a_sink_failure_during_flush_undoes_the_stores() -> None:
    """The audit flush sits INSIDE the boundary, and this is why (C2).

    Both passes succeed here; the REAL sink then refuses its first record.
    A flush outside the boundary would leave the channel installed with no
    audit trail at all — the exact shape the atomic-audit wiring exists to
    prevent on SQL — so the raise must take the write back with it.
    """
    registry = ChannelRegistry([])
    entry = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.CREATE,
        channel_name="Alpha News",
        group_id=None,
        revenue_required=True,
    )

    with pytest.raises(RuntimeError, match="sink unavailable"):
        _apply(_plan(entry), registry, ChannelGroupRegistry(), _UnavailableSink())

    assert registry.get_channel(CHANNEL_ID) is None


class _FailsMidFlushSink(InMemoryAuditSink):
    """Real in-memory sink that accepts two records, then refuses the third."""

    def append(self, record: AuditRecord) -> None:
        if len(self.records) >= 2:
            raise RuntimeError("sink full")
        super().append(record)


def test_a_mid_flush_failure_leaves_no_partial_audit_trail() -> None:
    """A raise on the Nth append takes the accepted prefix with it (round 2).

    Sequential appends alone are not atomic: without the sink's own boundary
    around the flush, records 1..N-1 stayed in the real sink while the stores
    rolled back — audit rows describing an import that did not happen, the
    exact lie the buffer exists to prevent (PR #196, codex P2 + qodo High,
    found independently). Two CREATE rows produce three records (two
    CHANNEL_CREATED, one CHANNEL_IMPORTED); the sink accepts the first two
    and refuses the third.
    """
    registry = ChannelRegistry([])
    sink = _FailsMidFlushSink()
    first = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=SECOND_CHANNEL_ID,
        outcome=ChannelImportOutcome.CREATE,
        channel_name="Beta News",
        group_id=None,
        revenue_required=True,
    )
    second = ChannelImportPlanEntry(
        row_number=2,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.CREATE,
        channel_name="Alpha News",
        group_id=None,
        revenue_required=True,
    )
    counts = {outcome.value: 0 for outcome in ChannelImportOutcome}
    counts[ChannelImportOutcome.CREATE.value] = 2

    with pytest.raises(RuntimeError, match="sink full"):
        _apply(
            ChannelImportPlan(entries=(first, second), counts=counts),
            registry,
            ChannelGroupRegistry(),
            sink,
        )

    # The two ACCEPTED records are gone too, and every store write with them.
    assert sink.records == []
    assert registry.get_channel(CHANNEL_ID) is None
    assert registry.get_channel(SECOND_CHANNEL_ID) is None


def test_flushed_audit_trail_keeps_event_order_and_ends_with_the_summary() -> None:
    """Buffering must change WHEN records reach the sink, never their order.

    The per-row CHANNEL_CREATED comes first and the one CHANNEL_IMPORTED
    summary stays last — the order consumers of the trail already rely on.
    """
    registry = ChannelRegistry([])
    groups = ChannelGroupRegistry()
    sink = InMemoryAuditSink()
    entry = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.CREATE,
        channel_name="Alpha News",
        group_id=None,
        revenue_required=True,
    )

    _apply(_plan(entry), registry, groups, sink)

    event_types = [record.event_type for record in sink.records]
    assert event_types[0] == "CHANNEL_CREATED"
    assert event_types[-1] == "CHANNEL_IMPORTED"
    assert event_types.count("CHANNEL_IMPORTED") == 1


def test_group_archived_between_plan_and_apply_fails_closed() -> None:
    """The write-boundary recheck raises instead of mutating a retired group."""
    registry = ChannelRegistry([])
    groups = _ArchivedAtApplyGroups()
    groups.create_group(name="cms-tv", group_type="SECTOR", channel_ids=[], cms_group_id="cms-tv")
    sink = InMemoryAuditSink()
    entry = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.CREATE,
        channel_name="Alpha News",
        group_id="cms-tv",
        revenue_required=True,
    )

    with pytest.raises(ChannelImportArchivedGroupError, match="cms-tv"):
        _apply(_plan(entry), registry, groups, sink)

    stored = groups.get_group_by_cms_id("cms-tv")
    assert stored is not None and stored.channel_ids == ()


def test_import_into_another_owners_group_fails_closed() -> None:
    """A CMS key owned by a DIFFERENT content owner is rejected, not attached.

    ``cms_group_id`` is unique per TENANT, so this import's lookup can resolve
    a group another owner's CMS sync owns. Attaching here would inject a
    foreign channel into that owner's mirrored group, which their next sync
    would then manage — and remove — as if YouTube had said so.
    """
    registry = ChannelRegistry([])
    groups = ChannelGroupRegistry()
    groups.create_group(
        name="cms-tv",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id="SomeOtherOwner",
    )
    sink = InMemoryAuditSink()
    entry = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.CREATE,
        channel_name="Alpha News",
        group_id="cms-tv",
        revenue_required=True,
    )

    with pytest.raises(ChannelImportGroupOwnerMismatchError, match="SomeOtherOwner"):
        _apply(_plan(entry), registry, groups, sink)

    # Nothing was attached to the other owner's group.
    stored = groups.get_group_by_cms_id("cms-tv")
    assert stored is not None
    assert stored.channel_ids == ()
    assert stored.content_owner_id == "SomeOtherOwner"


class _UnstampedAtApplyGroups(ChannelGroupRegistry):
    """Simulate an owner stamp cleared between planning and the apply lookup.

    Planning's bulk adoptable-key lookup sees the group STAMPED, so no row
    error is raised and the plan is clean; the apply's locked write-boundary
    lookup (for_update=True) observes it owner-NULL — the window the admin
    clear-stamp action opens.
    """

    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        group = super().get_group_by_cms_id(cms_group_id, for_update=for_update)
        if group is not None and for_update:
            return replace(group, content_owner_id=None)
        return group


def test_group_unstamped_between_plan_and_apply_fails_closed() -> None:
    """The write boundary refuses the adoption planning can no longer catch.

    Path A refuses owner-NULL groups at PLANNING, which makes this path
    unreachable for any row the planner vetted — but only for the state the
    planner read. A stamp cleared in the plan-to-apply window would otherwise
    put the apply back in the business of minting ownership from a CSV cell,
    so the locked recheck raises and the whole import rolls back. Nothing is
    written and nothing is audited: the row is inventory-identical, so its
    registry write replaces nothing, and the raise precedes both the group
    mutation and the import summary.
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Alpha News",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )
    groups = _UnstampedAtApplyGroups()
    groups.create_group(
        name="cms-tv",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id=CONTENT_OWNER,
    )
    sink = InMemoryAuditSink()
    entry = ChannelImportPlanEntry(
        row_number=1,
        youtube_channel_id=CHANNEL_ID,
        outcome=ChannelImportOutcome.UNCHANGED,
        channel_name="Alpha News",
        group_id="cms-tv",
        revenue_required=True,
    )

    with pytest.raises(ChannelImportAdoptableGroupError, match="cms-tv"):
        _apply(_plan(entry), registry, groups, sink)

    stored = groups.get_group_by_cms_id("cms-tv")
    assert stored is not None
    # No membership added, and the stamp the import never had a right to
    # rewrite is exactly as it was.
    assert stored.channel_ids == ()
    assert stored.content_owner_id == CONTENT_OWNER
    assert sink.records == []


def test_a_diverged_group_key_owned_by_another_owner_is_refused_before_writing() -> None:
    """Existence, not ownership — the pre-flight must judge what the lock judges.

    A stale CREATE label whose key has since been created by a DIFFERENT
    content owner is absent from this owner's set, so an ownership-only
    pre-flight passes it, the inventory pass writes every channel, and only
    then does the locked check see the group and raise (review #184, codex P2).

    The four bulk lookups are exhaustive over the ways a key can resolve, so
    their union is the same existence question ``get_group_by_cms_id`` answers.
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Old Name",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )
    groups = ChannelGroupRegistry()
    groups.create_group(
        name="cms-tv",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id="SomeOtherOwnerAAAAAAAA",
    )
    sink = InMemoryAuditSink()
    plan = _plan(
        ChannelImportPlanEntry(
            row_number=1,
            youtube_channel_id=CHANNEL_ID,
            outcome=ChannelImportOutcome.UPDATE,
            channel_name="Renamed By Import",
            group_id="cms-tv",
            group_action=ChannelImportGroupAction.CREATE,
            revenue_required=True,
            changes={"channel_name": ("Old Name", "Renamed By Import")},
        )
    )

    with pytest.raises(ChannelImportGroupActionDivergedError):
        _apply(plan, registry, groups, sink)

    assert sink.records == []
    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None
    assert stored.channel_name == "Old Name"


def test_a_diverged_group_effect_is_refused_before_any_channel_is_written() -> None:
    """Refuse while there is still nothing to roll back.

    The authoritative group-effect check runs under each group's row lock, in
    the SECOND pass — after every channel row has already been written. A store
    with no transaction cannot take those back, so the caller saw a refusal
    while the registry held the roster values that refusal said were not
    applied (review #184, codex P2).

    Called directly rather than through the route, because that is the shape
    this protects: the route re-plans inside the apply request and puts
    `group_action` in the fingerprint, so a bound apply catches most of this at
    the fingerprint compare. A DIRECT caller — tests, bootstrap, any future
    domain-side caller — hands over a plan built earlier, and this plan's
    CREATE label is already wrong when apply_channel_import is entered.

    Asserting the REGISTRY, not just the raise: the raise was already correct
    before the pre-flight existed.
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Old Name",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )
    groups = ChannelGroupRegistry()
    # The group the plan says will be CREATED already exists, and belongs to
    # this owner — so the reviewed effect is a JOIN and the label is stale.
    groups.create_group(
        name="cms-tv",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id=CONTENT_OWNER,
    )
    sink = InMemoryAuditSink()
    plan = _plan(
        ChannelImportPlanEntry(
            row_number=1,
            youtube_channel_id=CHANNEL_ID,
            outcome=ChannelImportOutcome.UPDATE,
            channel_name="Renamed By Import",
            group_id="cms-tv",
            group_action=ChannelImportGroupAction.CREATE,
            revenue_required=True,
            changes={"channel_name": ("Old Name", "Renamed By Import")},
        )
    )

    with pytest.raises(ChannelImportGroupActionDivergedError):
        _apply(plan, registry, groups, sink)

    assert sink.records == []
    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None
    # The refusal cost no write: the roster's name never landed.
    assert stored.channel_name == "Old Name"
    assert groups.get_group_by_cms_id("cms-tv").channel_ids == ()


def test_a_plan_disagreeing_with_itself_about_a_group_is_refused_before_writing() -> None:
    """Two actions for one key must not slip past by being collapsed.

    The pre-flight used a dict keyed by group_id, which is LAST-wins, while the
    locked group pass takes its action from ``entries[0]`` of the batch — the
    FIRST. So a plan carrying both CREATE and JOIN for one key had the two
    halves judging different labels, and the pre-flight could approve the one
    the write boundary was never going to use (review #184, qodo).

    Checking every entry removes the need to detect the contradiction as such:
    existence is one fact per key, so of two disagreeing labels exactly one must
    contradict it. Here the group EXISTS, so the JOIN copy is consistent and the
    CREATE copy is not — and it is the CREATE copy that the dict discarded.
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Old Name",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )
    groups = ChannelGroupRegistry()
    groups.create_group(
        name="cms-tv",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id=CONTENT_OWNER,
    )
    sink = InMemoryAuditSink()
    # CREATE first, JOIN second: the dict comprehension kept JOIN (which the
    # store agrees with) and dropped the CREATE that should have failed.
    entries = (
        ChannelImportPlanEntry(
            row_number=1,
            youtube_channel_id=CHANNEL_ID,
            outcome=ChannelImportOutcome.UPDATE,
            channel_name="Renamed By Import",
            group_id="cms-tv",
            group_action=ChannelImportGroupAction.CREATE,
            revenue_required=True,
            changes={"channel_name": ("Old Name", "Renamed By Import")},
        ),
        ChannelImportPlanEntry(
            row_number=2,
            youtube_channel_id=CHANNEL_ID,
            outcome=ChannelImportOutcome.UNCHANGED,
            channel_name="Renamed By Import",
            group_id="cms-tv",
            group_action=ChannelImportGroupAction.JOIN,
            revenue_required=True,
        ),
    )
    counts = {outcome.value: 0 for outcome in ChannelImportOutcome}
    counts[ChannelImportOutcome.UPDATE.value] = 1
    counts[ChannelImportOutcome.UNCHANGED.value] = 1

    with pytest.raises(ChannelImportGroupActionDivergedError):
        _apply(ChannelImportPlan(entries=entries, counts=counts), registry, groups, sink)

    assert sink.records == []
    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None
    assert stored.channel_name == "Old Name"


def test_a_stale_label_on_a_non_actionable_entry_is_not_refused() -> None:
    """The pre-flight must judge exactly what the write pass will act on.

    Checking every entry rather than a collapsed dict fixed a last-wins bug, but
    it also widened the SET being judged: the group pass skips any entry without
    a channel identity (``_group_write_batches``), so judging those refuses a
    plan over work the write boundary was never going to do.

    Here the only actionable group work is a valid JOIN. The second entry
    carries a stale CREATE for the same key but no ``youtube_channel_id``, so
    the write pass ignores it — and so must the pre-flight. Both filters now go
    through ``_performs_group_write`` so they cannot drift apart again.
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Alpha News",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )
    groups = ChannelGroupRegistry()
    groups.create_group(
        name="cms-tv",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id=CONTENT_OWNER,
    )
    sink = InMemoryAuditSink()
    entries = (
        ChannelImportPlanEntry(
            row_number=1,
            youtube_channel_id=CHANNEL_ID,
            outcome=ChannelImportOutcome.UNCHANGED,
            channel_name="Alpha News",
            group_id="cms-tv",
            group_action=ChannelImportGroupAction.JOIN,
            revenue_required=True,
        ),
        # No channel identity: not group work, so its label is not a claim the
        # write boundary will ever act on.
        ChannelImportPlanEntry(
            row_number=2,
            youtube_channel_id=None,
            outcome=ChannelImportOutcome.ERROR,
            group_id="cms-tv",
            group_action=ChannelImportGroupAction.CREATE,
            reason="something else was wrong with this row",
        ),
    )
    counts = {outcome.value: 0 for outcome in ChannelImportOutcome}
    counts[ChannelImportOutcome.UNCHANGED.value] = 1
    counts[ChannelImportOutcome.ERROR.value] = 1

    _apply(ChannelImportPlan(entries=entries, counts=counts), registry, groups, sink)

    # The JOIN happened; nothing was refused.
    assert groups.get_group_by_cms_id("cms-tv").channel_ids == (CHANNEL_ID,)


def test_no_plan_promises_a_group_effect_the_write_pass_will_not_perform() -> None:
    """Every row disclosing a group effect must map to a membership write.

    The batcher collapses a repeated ``(channel, group)`` pair so one channel
    is never handed to ``add_members`` twice. Planning has to refuse that
    repeat rather than emit a second row carrying the same ``group_action``,
    or the preview counts group work the apply does once (review #184).
    """
    csv_text = (
        "youtube_channel_id,channel_name,group_id\n"
        f"{CHANNEL_ID},Alpha News,cms-tv\n"
        f"{CHANNEL_ID},Alpha News,cms-tv\n"
        f"{CHANNEL_ID},Alpha News,cms-radio\n"
    )
    parsed = parse_channel_import_csv(csv_text)
    plan = plan_channel_import(
        rows=parsed.rows,
        errors=parsed.errors,
        existing={},
        content_owner_id="OWNER1",
        cms_status="INSIDE_CMS",
        owned_group_ids=frozenset(),
    )

    claimed = [entry for entry in plan.entries if entry.group_action is not None]
    written = [entry for _, entries in _group_write_batches(plan.entries) for entry in entries]

    assert len(claimed) == len(written)
    # Only the repeated pair is refused: making the counts agree by dropping
    # the legal cms-radio association would satisfy the assertion above while
    # losing the roster's actual intent.
    assert plan.counts[ChannelImportOutcome.ERROR.value] == 2
    surviving = [
        entry for entry in plan.entries if entry.outcome is not ChannelImportOutcome.ERROR
    ]
    assert [entry.group_id for entry in surviving] == ["cms-radio"]


def _empty_plan() -> ChannelImportPlan:
    return ChannelImportPlan(
        entries=(), counts={outcome.value: 0 for outcome in ChannelImportOutcome}
    )


def test_apply_refuses_sql_adapters_wired_over_distinct_sessions() -> None:
    """The shared-unit-of-work validation fails loud BEFORE any write.

    The savepoint boundaries are per session: wired over distinct sessions
    they are independent transactions, and a caller committing them
    separately could persist group and audit writes for channel writes that
    never landed (PR #196 round 6, codex). The in-memory stores stand in for
    the SQL adapters here by declaring the same public ``sql_unit_of_work``
    attribute the validation duck-types; the declaration on the REAL adapters
    is pinned in tests/org/test_sql_channel_registry.py.
    """
    registry = ChannelRegistry()
    groups = ChannelGroupRegistry()
    sink = InMemoryAuditSink()
    registry.sql_unit_of_work = object()
    groups.sql_unit_of_work = object()

    with pytest.raises(RuntimeError, match="share ONE session"):
        _apply(_empty_plan(), registry, groups, sink)

    assert registry.list_channels() == []
    assert sink.records == []


def test_apply_accepts_sql_adapters_sharing_one_session() -> None:
    """One shared unit of work passes the validation and applies normally."""
    registry = ChannelRegistry()
    groups = ChannelGroupRegistry()
    sink = InMemoryAuditSink()
    shared = object()
    registry.sql_unit_of_work = shared
    groups.sql_unit_of_work = shared
    sink.sql_unit_of_work = shared

    _apply(_empty_plan(), registry, groups, sink)

    assert [record.event_type for record in sink.records] == ["CHANNEL_IMPORTED"]
