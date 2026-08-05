"""Domain-side apply execution for CMS group sync.

Covers the write-boundary contract: every non-UNCHANGED entry executes through
the group store and emits exactly ONE GROUP_UPDATED audit event, UNCHANGED
entries write and audit nothing, and the returned counts are the ACTUAL
executed tally rather than the plan's (a plan is a snapshot; the write boundary
is the record).
"""

import pytest

from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.org.channel_group_sync import (
    GroupSyncOutcome,
    GroupSyncPlan,
    GroupSyncPlanEntry,
)
from ums_smart_revenue.org.channel_group_sync_apply import (
    AUDIT_SOURCE_CMS_SYNC,
    apply_group_sync,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistry

CH_A = "UCB6sc84dcg6VQGB_d89sx2g"
CH_B = "UC3Dci3BzZXDo4jw4dU8KqWg"
CONTENT_OWNER = "PlZrS5Fh56RMd9dmSL6XSA"
ACTOR = UserPrincipal(user_id="user-1", email="user@example.com")
REASON = "Mirror CMS grouping for August close"


class _RecordingGroupRegistry(ChannelGroupRegistry):
    """In-memory store that records which write methods the apply called.

    Lets a test assert what did NOT happen — an UNCHANGED entry touching the
    store at all, or a DEACTIVATE reaching membership — which a state-only
    assertion cannot distinguish from a write that happened to be a no-op.
    """

    def __init__(self, groups: list[ChannelGroupEntry] | None = None) -> None:
        """Seed the in-memory groups and start with an empty write log."""
        super().__init__(groups)
        self.writes: list[str] = []

    def create_group(self, **kwargs: object) -> ChannelGroupEntry:
        """Record and delegate the group creation."""
        self.writes.append("create_group")
        return super().create_group(**kwargs)  # type: ignore[arg-type]

    def update_group(self, **kwargs: object) -> ChannelGroupEntry:
        """Record and delegate the name/active update."""
        self.writes.append("update_group")
        return super().update_group(**kwargs)  # type: ignore[arg-type]

    def add_members(self, **kwargs: object) -> ChannelGroupEntry:
        """Record and delegate the membership addition."""
        self.writes.append("add_members")
        return super().add_members(**kwargs)  # type: ignore[arg-type]

    def remove_member(self, **kwargs: object) -> ChannelGroupEntry:
        """Record and delegate the membership removal."""
        self.writes.append("remove_member")
        return super().remove_member(**kwargs)  # type: ignore[arg-type]


def _entry(**overrides: object) -> GroupSyncPlanEntry:
    defaults: dict[str, object] = {
        "cms_group_id": "g1",
        "outcome": GroupSyncOutcome.CREATE,
        "title": "TV Sector",
        "local_group_id": None,
        "name_change": None,
        "active_change": None,
        "members_added": (),
        "members_removed": (),
        "unknown_channel_ids": (),
    }
    defaults.update(overrides)
    return GroupSyncPlanEntry(**defaults)  # type: ignore[arg-type]


def _plan(*entries: GroupSyncPlanEntry, counts: dict[str, int] | None = None) -> GroupSyncPlan:
    tallied = {outcome.value: 0 for outcome in GroupSyncOutcome}
    for entry in entries:
        tallied[entry.outcome.value] += 1
    return GroupSyncPlan(
        entries=entries,
        counts=counts if counts is not None else tallied,
        unknown_channel_total=0,
        non_channel_member_count=0,
    )


def _apply(
    plan: GroupSyncPlan, groups: ChannelGroupRegistry, sink: InMemoryAuditSink
) -> dict[str, int]:
    return apply_group_sync(
        plan,
        groups=groups,
        audit_sink=sink,
        actor=ACTOR,
        scope=AccessScope.global_scope(),
        content_owner_id=CONTENT_OWNER,
        reason=REASON,
    )


def _seeded(**overrides: object) -> _RecordingGroupRegistry:
    defaults: dict[str, object] = {
        "id": "local-1",
        "name": "TV Sector",
        "group_type": "SECTOR",
        "active": True,
        "channel_ids": (CH_A,),
        "cms_group_id": "g1",
    }
    defaults.update(overrides)
    registry = _RecordingGroupRegistry([ChannelGroupEntry(**defaults)])  # type: ignore[arg-type]
    return registry


def test_create_entry_creates_the_group_and_audits_the_sync_provenance() -> None:
    """A CREATE lands a SECTOR group carrying the CMS key, with one audit."""
    groups = _RecordingGroupRegistry()
    sink = InMemoryAuditSink()

    executed = _apply(
        _plan(_entry(members_added=(CH_A,))),
        groups,
        sink,
    )

    created = groups.get_group_by_cms_id("g1")
    assert created is not None
    assert created.name == "TV Sector"
    assert created.group_type == "SECTOR"
    assert created.active is True
    assert created.channel_ids == (CH_A,)
    assert executed["CREATE"] == 1
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.event_type == "GROUP_UPDATED"
    assert record.entity_type == "channel_group"
    assert record.entity_id == created.id
    assert record.reason == REASON
    assert record.details == {
        "source": AUDIT_SOURCE_CMS_SYNC,
        "content_owner_id": CONTENT_OWNER,
        "cms_group_id": "g1",
        "outcome": "CREATE",
        "name_change": None,
        "active_change": None,
        "members_added": 1,
        "members_removed": 0,
    }


def test_rename_overwrites_the_local_name_and_audits_the_change() -> None:
    """YouTube wins on titles; the audit carries the from/to pair."""
    groups = _seeded()
    sink = InMemoryAuditSink()

    _apply(
        _plan(
            _entry(
                outcome=GroupSyncOutcome.RENAME,
                title="TV",
                local_group_id="local-1",
                name_change=("TV Sector", "TV"),
            )
        ),
        groups,
        sink,
    )

    stored = groups.get_group("local-1")
    assert stored is not None
    assert stored.name == "TV"
    assert stored.active is True
    assert stored.channel_ids == (CH_A,)
    assert groups.writes == ["update_group"]
    assert len(sink.records) == 1
    assert sink.records[0].details["name_change"] == ["TV Sector", "TV"]
    assert sink.records[0].details["outcome"] == "RENAME"


def test_members_changed_adds_and_removes_through_the_store() -> None:
    """Membership is set-reconciled: additions AND removals both execute."""
    groups = _seeded()
    sink = InMemoryAuditSink()

    _apply(
        _plan(
            _entry(
                outcome=GroupSyncOutcome.MEMBERS_CHANGED,
                local_group_id="local-1",
                members_added=(CH_B,),
                members_removed=(CH_A,),
            )
        ),
        groups,
        sink,
    )

    stored = groups.get_group("local-1")
    assert stored is not None
    assert stored.channel_ids == (CH_B,)
    assert stored.name == "TV Sector"
    # No name or active change was planned, so the store must not be asked to
    # update either — only the two membership writes.
    assert groups.writes == ["add_members", "remove_member"]
    assert len(sink.records) == 1
    assert sink.records[0].details["members_added"] == 1
    assert sink.records[0].details["members_removed"] == 1
    assert sink.records[0].details["outcome"] == "MEMBERS_CHANGED"


def test_stale_members_changed_entry_already_applied_is_recounted_unchanged() -> None:
    """A concurrent writer beating this apply to the same change is a no-op.

    The plan is a snapshot from before this apply; if another writer (a
    racing sync, or the bulk import) already added CH_B to this group between
    planning and now, re-checking the group's CURRENT membership must treat
    the entry as UNCHANGED — no store write, no audit event claiming a
    membership change this request did not make.
    """
    groups = _seeded(channel_ids=(CH_A, CH_B))
    sink = InMemoryAuditSink()

    executed = _apply(
        _plan(
            _entry(
                outcome=GroupSyncOutcome.MEMBERS_CHANGED,
                local_group_id="local-1",
                members_added=(CH_B,),
            )
        ),
        groups,
        sink,
    )

    stored = groups.get_group("local-1")
    assert stored is not None
    assert stored.channel_ids == (CH_A, CH_B)
    assert groups.writes == []
    assert sink.records == []
    assert executed["MEMBERS_CHANGED"] == 0
    assert executed["UNCHANGED"] == 1


def test_partial_race_reports_the_outcome_that_actually_happened() -> None:
    """A plan whose rename/activation a racer already landed is not mislabelled.

    The entry plans REACTIVATE (activation + member churn), but by the write
    boundary another writer has already reactivated the group and applied the
    rename. Only membership is left for this request, so both the count and
    the audit row must say MEMBERS_CHANGED — claiming REACTIVATE would assert
    an activation this request never performed.
    """
    groups = _seeded(active=True, name="TV", channel_ids=(CH_A,))
    sink = InMemoryAuditSink()

    executed = _apply(
        _plan(
            _entry(
                outcome=GroupSyncOutcome.REACTIVATE,
                title="TV",
                local_group_id="local-1",
                name_change=("TV Sector", "TV"),
                active_change=(False, True),
                members_added=(CH_B,),
            )
        ),
        groups,
        sink,
    )

    assert executed["REACTIVATE"] == 0
    assert executed["MEMBERS_CHANGED"] == 1
    assert groups.writes == ["add_members"]
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.details["outcome"] == "MEMBERS_CHANGED"
    assert record.details["active_change"] is None
    assert record.details["name_change"] is None
    assert record.details["members_added"] == 1


def test_deactivate_flips_active_and_leaves_membership_untouched() -> None:
    """A vanished CMS group deactivates locally; its members stay attached."""
    groups = _seeded(channel_ids=(CH_A, CH_B))
    sink = InMemoryAuditSink()

    _apply(
        _plan(
            _entry(
                outcome=GroupSyncOutcome.DEACTIVATE,
                title=None,
                local_group_id="local-1",
                active_change=(True, False),
            )
        ),
        groups,
        sink,
    )

    stored = groups.get_group("local-1")
    assert stored is not None
    assert stored.active is False
    assert stored.name == "TV Sector"
    assert stored.channel_ids == (CH_A, CH_B)
    assert groups.writes == ["update_group"]
    assert len(sink.records) == 1
    assert sink.records[0].details["active_change"] == [True, False]
    assert sink.records[0].details["outcome"] == "DEACTIVATE"


def test_reactivate_applies_name_active_and_membership_in_one_audit_event() -> None:
    """A reappearing CMS key heals every drift under a single audit record."""
    groups = _seeded(active=False)
    sink = InMemoryAuditSink()

    _apply(
        _plan(
            _entry(
                outcome=GroupSyncOutcome.REACTIVATE,
                title="TV",
                local_group_id="local-1",
                name_change=("TV Sector", "TV"),
                active_change=(False, True),
                members_added=(CH_B,),
                members_removed=(CH_A,),
            )
        ),
        groups,
        sink,
    )

    stored = groups.get_group("local-1")
    assert stored is not None
    assert stored.active is True
    assert stored.name == "TV"
    assert stored.channel_ids == (CH_B,)
    assert groups.writes == ["update_group", "add_members", "remove_member"]
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.details["outcome"] == "REACTIVATE"
    assert record.details["name_change"] == ["TV Sector", "TV"]
    assert record.details["active_change"] == [False, True]
    assert record.details["members_added"] == 1
    assert record.details["members_removed"] == 1


def test_unchanged_entry_writes_nothing_and_audits_nothing() -> None:
    """An already-mirrored group must not churn the store or the audit trail."""
    groups = _seeded()
    sink = InMemoryAuditSink()

    executed = _apply(
        _plan(
            _entry(
                outcome=GroupSyncOutcome.UNCHANGED,
                local_group_id="local-1",
            )
        ),
        groups,
        sink,
    )

    assert groups.writes == []
    assert sink.records == []
    assert groups.get_group("local-1") == ChannelGroupEntry(
        id="local-1",
        name="TV Sector",
        group_type="SECTOR",
        active=True,
        channel_ids=(CH_A,),
        cms_group_id="g1",
    )
    assert executed["UNCHANGED"] == 1


def test_returned_counts_are_the_executed_tally_not_the_plan_counts() -> None:
    """The summary's counts come from the write boundary, never from the plan."""
    groups = _RecordingGroupRegistry(
        [
            ChannelGroupEntry(
                id="local-1",
                name="TV Sector",
                group_type="SECTOR",
                active=True,
                channel_ids=(CH_A,),
                cms_group_id="g1",
            ),
            ChannelGroupEntry(
                id="local-2",
                name="News",
                group_type="SECTOR",
                active=True,
                channel_ids=(),
                cms_group_id="g2",
            ),
            ChannelGroupEntry(
                id="local-3",
                name="Sport",
                group_type="SECTOR",
                active=True,
                channel_ids=(),
                cms_group_id="g3",
            ),
        ]
    )
    sink = InMemoryAuditSink()
    # Deliberately wrong plan counts: the return value must be built from what
    # actually executed, so copying these through would be visible.
    bogus_counts = {outcome.value: 99 for outcome in GroupSyncOutcome}

    executed = _apply(
        _plan(
            _entry(cms_group_id="g0", members_added=(CH_B,)),
            _entry(
                cms_group_id="g1",
                outcome=GroupSyncOutcome.RENAME,
                title="TV",
                local_group_id="local-1",
                name_change=("TV Sector", "TV"),
            ),
            _entry(
                cms_group_id="g2",
                outcome=GroupSyncOutcome.UNCHANGED,
                title="News",
                local_group_id="local-2",
            ),
            _entry(
                cms_group_id="g3",
                outcome=GroupSyncOutcome.DEACTIVATE,
                title=None,
                local_group_id="local-3",
                active_change=(True, False),
            ),
            counts=bogus_counts,
        ),
        groups,
        sink,
    )

    assert executed == {
        "CREATE": 1,
        "RENAME": 1,
        "MEMBERS_CHANGED": 0,
        "DEACTIVATE": 1,
        "REACTIVATE": 0,
        "UNCHANGED": 1,
    }
    # Three changed groups, three audit events; UNCHANGED contributes none.
    assert len(sink.records) == 3
    assert [record.details["outcome"] for record in sink.records] == [
        "CREATE",
        "RENAME",
        "DEACTIVATE",
    ]


def test_non_create_entry_without_a_local_group_id_fails_closed() -> None:
    """An entry the planner cannot produce raises instead of silently skipping.

    Skipping would let the returned counts claim a rename or deactivation that
    the store never executed and no audit event backs.
    """
    groups = _RecordingGroupRegistry()
    sink = InMemoryAuditSink()

    with pytest.raises(ValueError, match="g1"):
        _apply(
            _plan(
                _entry(
                    outcome=GroupSyncOutcome.RENAME,
                    local_group_id=None,
                    name_change=("TV Sector", "TV"),
                )
            ),
            groups,
            sink,
        )

    assert groups.writes == []
    assert sink.records == []
