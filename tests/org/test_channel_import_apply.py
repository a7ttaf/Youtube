"""Domain-side apply execution: write-boundary diffs and archived-group races.

Covers the two plan-to-apply race guards from PR #159 review rounds
(r3712948694, r3712948688): the durable audit diff is computed from what the
registry write ACTUALLY replaced, and a group archived after planning fails
the apply closed instead of silently mutating a retired group.
"""

from dataclasses import replace

import pytest

from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistry
from ums_smart_revenue.org.channel_import import (
    ChannelImportOutcome,
    ChannelImportPlan,
    ChannelImportPlanEntry,
)
from ums_smart_revenue.org.channel_import_apply import (
    ChannelImportArchivedGroupError,
    apply_channel_import,
)
from ums_smart_revenue.org.channel_registry import ChannelRegistry, ChannelRegistryEntry

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
CONTENT_OWNER = "PlZrS5Fh56RMd9dmSL6XSA"
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


class _ArchivedAtApplyGroups(ChannelGroupRegistry):
    """Simulate a group archived between planning and the apply lookup."""

    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        group = super().get_group_by_cms_id(cms_group_id, for_update=for_update)
        if group is not None and for_update:
            return replace(group, active=False)
        return group


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
