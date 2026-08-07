# ============================================================================
# Purpose: Pin the CMS group-sync WRITE boundary — which plan entries reach the
#   store, what each one audits, and what the caller is told actually happened.
# Database/ORM: None directly. The store and audit sink are in-memory doubles
#   that implement the same Protocols the SQL versions do, so a Protocol method
#   added without a double here fails loudly instead of silently no-op'ing.
# Standards: Every assertion is on the EXECUTED write, never on the plan that
#   proposed it — a plan is a snapshot, the locked write boundary is the record.
#   Mid-flight cases (rename, archive, cross-owner claim) are exercised through
#   registry doubles that mutate between plan and apply, because that race is
#   the only thing the locked re-read exists to catch.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py -> subject.
#   - File: tests/org/test_channel_group_sync_planner.py -> the planning half of
#     the same contract; outcomes planned there are executed here.
# ============================================================================
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
    _ID_LOOKUP_CHUNK,
    AUDIT_SOURCE_CMS_SYNC,
    GroupSyncExecution,
    _known_member_channel_ids,
    apply_group_sync,
)
from ums_smart_revenue.org.channel_groups import (
    ChannelGroupEntry,
    ChannelGroupOwnerReassignmentError,
    ChannelGroupRegistry,
)
from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry

CH_A = "UCB6sc84dcg6VQGB_d89sx2g"
CH_B = "UC3Dci3BzZXDo4jw4dU8KqWg"
CONTENT_OWNER = "TestOwnerAAAAAAAAAAAAA"
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
        return super().create_group(**kwargs)

    def update_group(self, **kwargs: object) -> ChannelGroupEntry:
        """Record and delegate the name/active update."""
        self.writes.append("update_group")
        return super().update_group(**kwargs)

    def add_members(self, **kwargs: object) -> ChannelGroupEntry:
        """Record and delegate the membership addition."""
        self.writes.append("add_members")
        return super().add_members(**kwargs)

    def remove_member(self, **kwargs: object) -> ChannelGroupEntry:
        """Record and delegate the membership removal."""
        self.writes.append("remove_member")
        return super().remove_member(**kwargs)


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
        # Matches the planner: every outcome except the vanished-group ones is
        # built from an upstream item, and upstream presence is what the write
        # boundary mirrors into `active`. DEACTIVATE tests override this.
        "upstream_present": True,
    }
    defaults.update(overrides)
    return GroupSyncPlanEntry(**defaults)


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
) -> GroupSyncExecution:
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
        # Already owned by the syncing content owner — the normal case. Tests
        # covering legacy/unstamped rows pass content_owner_id=None explicitly,
        # which triggers the adoption path.
        "content_owner_id": CONTENT_OWNER,
    }
    defaults.update(overrides)
    registry = _RecordingGroupRegistry([ChannelGroupEntry(**defaults)])
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
    assert executed.counts["CREATE"] == 1
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
        # A freshly created group is stamped with its owner at creation, so
        # there is no legacy row to adopt.
        "adopted_content_owner": False,
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
    assert executed.counts["MEMBERS_CHANGED"] == 0
    assert executed.counts["UNCHANGED"] == 1


def test_matching_a_legacy_owner_null_group_adopts_it() -> None:
    """Matching this owner's upstream key stamps an owner-NULL legacy group.

    Without the stamp the row stays unclaimable forever: the planner skips
    owner-NULL groups when deciding DEACTIVATE, so a group that later vanishes
    upstream would never be retired and the mirror would silently stop
    reflecting deletions.
    """
    groups = _seeded(content_owner_id=None, name="Old Name")
    sink = InMemoryAuditSink()

    _apply(
        _plan(
            _entry(
                outcome=GroupSyncOutcome.RENAME,
                local_group_id="local-1",
                name_change=("Old Name", "TV Sector"),
            )
        ),
        groups,
        sink,
    )

    stored = groups.get_group("local-1")
    assert stored is not None
    assert stored.content_owner_id == CONTENT_OWNER
    assert stored.name == "TV Sector"
    assert sink.records[0].details["adopted_content_owner"] is True


def test_in_sync_legacy_group_is_still_adopted_and_audited() -> None:
    """Adoption is owed even when the mirror itself has nothing to change.

    This is the one documented exception to "UNCHANGED writes nothing": the
    owner stamp is a real write, so it happens AND is audited rather than
    being applied silently.

    The entry carries outcome=UNCHANGED deliberately — that is exactly what
    ``plan_group_sync`` emits for a legacy group whose name and membership
    already match upstream, and an earlier version of this fix short-circuited
    UNCHANGED before the adoption path could run, leaving those rows
    permanently unclaimable.
    """
    groups = _seeded(content_owner_id=None)
    sink = InMemoryAuditSink()

    executed = _apply(
        _plan(_entry(outcome=GroupSyncOutcome.UNCHANGED, local_group_id="local-1")),
        groups,
        sink,
    )

    assert groups.get_group("local-1").content_owner_id == CONTENT_OWNER
    assert groups.writes == ["update_group"]
    # No mirror change happened, so the label is UNCHANGED ...
    assert executed.counts["UNCHANGED"] == 1
    assert executed.counts["RENAME"] == 0
    # ... but the write is still on the record.
    assert len(sink.records) == 1
    assert sink.records[0].details["adopted_content_owner"] is True
    assert sink.records[0].details["outcome"] == "UNCHANGED"


def test_unchanged_owned_group_still_writes_and_audits_nothing() -> None:
    """Routing UNCHANGED through the apply path must not start writing.

    Adoption is the ONLY reason an UNCHANGED entry may touch the store; an
    already-owned, already-in-sync group must still be a pure no-op.
    """
    groups = _seeded()
    sink = InMemoryAuditSink()

    executed = _apply(
        _plan(_entry(outcome=GroupSyncOutcome.UNCHANGED, local_group_id="local-1")),
        groups,
        sink,
    )

    assert groups.writes == []
    assert sink.records == []
    assert executed.counts["UNCHANGED"] == 1


def test_already_owned_group_is_mirrored_without_being_restamped() -> None:
    """Adoption is owed once, not on every sync of an already-owned group.

    The mirror still applies in full; the difference from the legacy path is
    that no owner stamp rides along, so ``adopted_content_owner`` stays false
    and an auditor can tell a one-time claim from routine mirroring.
    """
    groups = _seeded(name="Old Name")
    sink = InMemoryAuditSink()

    _apply(
        _plan(
            _entry(
                outcome=GroupSyncOutcome.RENAME,
                local_group_id="local-1",
                name_change=("Old Name", "TV Sector"),
            )
        ),
        groups,
        sink,
    )

    stored = groups.get_group("local-1")
    assert stored.name == "TV Sector"
    assert stored.content_owner_id == CONTENT_OWNER
    assert sink.records[0].details["adopted_content_owner"] is False


def test_group_deactivated_mid_flight_is_reactivated_because_it_is_upstream() -> None:
    """Active state is mirrored from UPSTREAM PRESENCE, not from the stale diff.

    A group that was active when the plan was built carries no active_change,
    so re-diffing the plan's fields alone cannot notice that an operator
    archived it in the plan-to-apply window (the group API deliberately still
    allows archiving a synced group). The sync would then report success and
    leave an upstream-present group inactive until some later run happened to
    plan REACTIVATE — a mirror that silently is not one.
    """
    groups = _seeded(active=False)
    sink = InMemoryAuditSink()

    executed = _apply(
        _plan(
            _entry(
                outcome=GroupSyncOutcome.UNCHANGED,
                local_group_id="local-1",
                active_change=None,
            )
        ),
        groups,
        sink,
    )

    assert groups.get_group("local-1").active is True
    assert executed.counts["REACTIVATE"] == 1
    assert sink.records[0].details["active_change"] == [False, True]


def test_per_group_results_report_the_write_not_the_plan() -> None:
    """A raced-away entry must not be reported back as a change that happened.

    The route renders an apply's per-group response from these results. If it
    rendered from the plan instead, this entry would come back as RENAME with
    a full name_change diff, while the audit trail correctly recorded nothing
    — telling the operator this request renamed a group it never touched.
    """
    # The rename the plan wants is ALREADY the stored name: a concurrent writer
    # landed it between planning and this apply.
    groups = _seeded(name="TV")
    sink = InMemoryAuditSink()

    executed = _apply(
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

    assert groups.writes == []
    assert sink.records == []
    (result,) = executed.entries
    assert result.outcome is GroupSyncOutcome.UNCHANGED
    assert result.name_change is None
    assert result.members_added == ()
    assert executed.counts["RENAME"] == 0


def test_apply_refuses_a_group_another_owner_claimed_mid_flight() -> None:
    """The locked re-read must re-verify SCOPE, not just the mirrored fields.

    Sequence: the planner sees an owner-NULL legacy group (the OR-NULL scoped
    read deliberately includes it), another owner's import adopts it before
    this apply takes the lock, and the entry's premise — "this group is mine
    or unclaimed" — is now false. Skipping only the owner stamp is not enough:
    the rename/membership writes would still land on the other owner's group,
    and the GROUP_UPDATED row would carry THIS owner's content_owner_id on it,
    so the audit trail would misattribute the change too.

    Fails closed at the entry rather than absorbing it as UNCHANGED, because
    the operator has to be shown the state — a group whose jurisdiction moved
    mid-sync is exactly what a silent no-op would hide.
    """
    groups = _seeded(content_owner_id="SomeOtherOwner")
    sink = InMemoryAuditSink()

    with pytest.raises(ChannelGroupOwnerReassignmentError):
        _apply(
            _plan(
                _entry(
                    outcome=GroupSyncOutcome.RENAME,
                    local_group_id="local-1",
                    name_change=("TV Sector", "TV Sector (renamed upstream)"),
                    members_added=(CH_B,),
                )
            ),
            groups,
            sink,
        )

    survivor = groups.get_group("local-1")
    assert survivor.name == "TV Sector"
    assert survivor.channel_ids == (CH_A,)
    assert survivor.content_owner_id == "SomeOtherOwner"
    assert groups.writes == []
    assert sink.records == []


def test_store_refuses_to_reassign_an_owned_group() -> None:
    """The store enforces adopt-only itself, not just the sync call site.

    content_owner_id is what scopes sync, so a future call-site bug that
    reassigned a group between owners would corrupt both owners' plans. The
    guard is in the store so it fails loudly instead of writing.
    """
    groups = ChannelGroupRegistry(
        [
            ChannelGroupEntry(
                id="local-1",
                name="TV Sector",
                group_type="SECTOR",
                active=True,
                channel_ids=(),
                cms_group_id="g1",
                content_owner_id="OwnerA",
            )
        ]
    )

    # Re-stamping the SAME owner is a harmless no-op.
    groups.update_group(group_id="local-1", name=None, active=None, content_owner_id="OwnerA")
    assert groups.get_group("local-1").content_owner_id == "OwnerA"

    with pytest.raises(ChannelGroupOwnerReassignmentError):
        groups.update_group(group_id="local-1", name=None, active=None, content_owner_id="OwnerB")
    assert groups.get_group("local-1").content_owner_id == "OwnerA"


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

    assert executed.counts["REACTIVATE"] == 0
    assert executed.counts["MEMBERS_CHANGED"] == 1
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
                upstream_present=False,
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
        content_owner_id=CONTENT_OWNER,
    )
    assert executed.counts["UNCHANGED"] == 1


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
                content_owner_id=CONTENT_OWNER,
            ),
            ChannelGroupEntry(
                id="local-2",
                name="News",
                group_type="SECTOR",
                active=True,
                channel_ids=(),
                cms_group_id="g2",
                content_owner_id=CONTENT_OWNER,
            ),
            ChannelGroupEntry(
                id="local-3",
                name="Sport",
                group_type="SECTOR",
                active=True,
                channel_ids=(),
                cms_group_id="g3",
                content_owner_id=CONTENT_OWNER,
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
                upstream_present=False,
            ),
            counts=bogus_counts,
        ),
        groups,
        sink,
    )

    assert executed.counts == {
        "CREATE": 1,
        "CONFLICT": 0,
        "RENAME": 1,
        "MEMBERS_CHANGED": 0,
        "DEACTIVATE": 1,
        "REACTIVATE": 0,
        "UNCHANGED": 1,
    }
    # One result per plan entry, including the one that wrote nothing — the
    # route renders the apply response from these, so a missing entry would
    # silently drop a group from the operator's view.
    assert [result.cms_group_id for result in executed.entries] == ["g0", "g1", "g2", "g3"]
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


class _RecordingChannelRegistry:
    """Channel store double that records the size of each id lookup."""

    def __init__(self, known: set[str], *, content_owner_id: str) -> None:
        """Hold the ids that exist and the owner every one of them carries."""
        self._known = known
        self._content_owner_id = content_owner_id
        self.lookup_sizes: list[int] = []

    def list_channels_by_ids(
        self, youtube_channel_ids: set[str], *, include_inactive: bool = False
    ) -> list[ChannelRegistryEntry]:
        """Record the batch size, then return the matching entries."""
        self.lookup_sizes.append(len(youtube_channel_ids))
        return [
            ChannelRegistryEntry(
                youtube_channel_id=channel_id,
                channel_name=channel_id,
                primary_company_id="company-tv",
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=self._content_owner_id,
            )
            for channel_id in sorted(youtube_channel_ids & self._known)
        ]


def test_realistic_member_lookup_is_one_atomic_read() -> None:
    """Splitting a read costs consistency, so realistic syncs must not split.

    Each statement takes its own snapshot under READ COMMITTED, so N chunks can
    observe N different committed states. The chunk size is set so that any
    plausible content owner — the production one has roughly 300 channels —
    resolves in ONE statement, i.e. exactly as atomic as before chunking
    existed. The cap only exists to remove the bind-parameter cliff.
    """
    member_ids = {f"UC{index:022d}" for index in range(5_000)}
    registry = _RecordingChannelRegistry(member_ids, content_owner_id=CONTENT_OWNER)

    known = _known_member_channel_ids(
        registry,
        member_ids=member_ids,
        content_owner_id=CONTENT_OWNER,
    )

    assert known == member_ids
    assert registry.lookup_sizes == [5_000]


def test_member_lookup_is_chunked_under_the_bind_parameter_cap() -> None:
    """A pathological CMS snapshot must not become one oversized IN (...).

    SQLAlchemy expands IN to one bind parameter per element and PostgreSQL caps
    a statement at 65535 of them. These lookups run AFTER the whole Google
    fetch, so blowing the cap would fail the sync having already paid for the
    upstream work. The import path is capped at 5000 rows; a sync's id set is
    bounded only by what the CMS returns.

    Sized off the constant so the guarantee is "always chunked above the
    threshold", not a number that silently stops meaning anything if the
    threshold moves.
    """
    count = _ID_LOOKUP_CHUNK * 2 + 1
    member_ids = {f"UC{index:022d}" for index in range(count)}
    registry = _RecordingChannelRegistry(member_ids, content_owner_id=CONTENT_OWNER)

    known = _known_member_channel_ids(
        registry,
        member_ids=member_ids,
        content_owner_id=CONTENT_OWNER,
    )

    assert known == member_ids
    assert len(registry.lookup_sizes) == 3
    assert max(registry.lookup_sizes) <= _ID_LOOKUP_CHUNK
