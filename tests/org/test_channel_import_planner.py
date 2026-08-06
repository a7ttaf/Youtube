"""Pure diff planning for the bulk channel import."""

from ums_smart_revenue.org.channel_import import (
    ChannelImportOutcome,
    ChannelImportRow,
    ChannelImportRowError,
    plan_channel_import,
)
from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
CONTENT_OWNER = "TestOwnerAAAAAAAAAAAAA"


def _row(**overrides: object) -> ChannelImportRow:
    defaults: dict[str, object] = {
        "row_number": 1,
        "youtube_channel_id": CHANNEL_ID,
        "channel_name": "CBC Egypt",
        "group_id": None,
        "view_revenue": None,
    }
    defaults.update(overrides)
    return ChannelImportRow(**defaults)  # type: ignore[arg-type]


def _existing(**overrides: object) -> ChannelRegistryEntry:
    defaults: dict[str, object] = {
        "youtube_channel_id": CHANNEL_ID,
        "channel_name": "CBC Egypt",
        "primary_company_id": None,
        "cms_status": "INSIDE_CMS",
        "revenue_required": True,
        "content_owner_id": CONTENT_OWNER,
    }
    defaults.update(overrides)
    return ChannelRegistryEntry(**defaults)  # type: ignore[arg-type]


def _plan(rows=(), errors=(), existing=None):
    return plan_channel_import(
        rows=rows,
        errors=errors,
        existing=existing if existing is not None else {},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
    )


def test_absent_channel_plans_a_create() -> None:
    plan = _plan(rows=(_row(),))
    assert plan.entries[0].outcome is ChannelImportOutcome.CREATE
    assert plan.has_errors is False
    assert plan.counts["CREATE"] == 1


def test_identical_channel_plans_unchanged() -> None:
    plan = _plan(rows=(_row(),), existing={CHANNEL_ID: _existing()})
    assert plan.entries[0].outcome is ChannelImportOutcome.UNCHANGED
    assert dict(plan.entries[0].changes) == {}


def test_differing_channel_plans_update_with_field_diff() -> None:
    plan = _plan(rows=(_row(channel_name="CBC Masr"),), existing={CHANNEL_ID: _existing()})
    entry = plan.entries[0]
    assert entry.outcome is ChannelImportOutcome.UPDATE
    assert dict(entry.changes) == {"channel_name": ("CBC Egypt", "CBC Masr")}


def test_cms_status_difference_is_an_update() -> None:
    plan = _plan(rows=(_row(),), existing={CHANNEL_ID: _existing(cms_status="UNKNOWN")})
    entry = plan.entries[0]
    assert entry.outcome is ChannelImportOutcome.UPDATE
    assert dict(entry.changes) == {"cms_status": ("UNKNOWN", "INSIDE_CMS")}


def test_content_owner_difference_is_an_update() -> None:
    plan = _plan(rows=(_row(),), existing={CHANNEL_ID: _existing(content_owner_id=None)})
    entry = plan.entries[0]
    assert entry.outcome is ChannelImportOutcome.UPDATE
    assert dict(entry.changes) == {"content_owner_id": (None, CONTENT_OWNER)}


def test_view_revenue_no_clears_revenue_required() -> None:
    plan = _plan(rows=(_row(view_revenue=False),), existing={CHANNEL_ID: _existing()})
    assert dict(plan.entries[0].changes) == {"revenue_required": (True, False)}


def test_absent_view_revenue_column_defaults_to_required() -> None:
    plan = _plan(rows=(_row(),))
    assert plan.entries[0].revenue_required is True


def test_view_revenue_yes_sets_required() -> None:
    plan = _plan(rows=(_row(view_revenue=True),))
    assert plan.entries[0].revenue_required is True


def test_group_id_is_carried_onto_the_entry() -> None:
    plan = _plan(rows=(_row(group_id="cms-tv"),))
    assert plan.entries[0].group_id == "cms-tv"


def test_archived_existing_channel_is_a_row_error() -> None:
    """An archived registry row fails the row instead of planning a doomed CREATE."""
    plan = _plan(rows=(_row(),), existing={CHANNEL_ID: _existing(active=False)})
    entry = plan.entries[0]
    assert entry.outcome is ChannelImportOutcome.ERROR
    assert entry.youtube_channel_id == CHANNEL_ID
    assert entry.reason is not None and "archived" in entry.reason
    assert plan.has_errors is True
    assert plan.counts["ERROR"] == 1


def test_view_revenue_raw_is_carried_onto_the_entry() -> None:
    """The raw CSV token rides the plan entry so the apply can audit it."""
    plan = _plan(rows=(_row(view_revenue=True, view_revenue_raw="Yes"),))
    assert plan.entries[0].view_revenue_raw == "Yes"


def test_archived_group_is_a_row_error() -> None:
    """A row targeting an archived CMS group fails closed at planning."""
    plan = plan_channel_import(
        rows=(_row(group_id="cms-retired"),),
        errors=(),
        existing={},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
        archived_group_ids=frozenset({"cms-retired"}),
    )
    entry = plan.entries[0]
    assert entry.outcome is ChannelImportOutcome.ERROR
    assert entry.reason is not None and "archived" in entry.reason
    assert "cms-retired" in entry.reason
    assert plan.has_errors is True


def test_group_owned_by_another_content_owner_is_a_row_error() -> None:
    """A cross-owner group conflict must fail at PLANNING, not only at apply.

    The apply already refuses it (ChannelImportGroupOwnerMismatchError -> 409),
    but the conflict is knowable from current database state, so leaving it to
    the write boundary makes ``dry_run=true`` return a clean plan for an import
    that cannot succeed — the operator trusts the preview and then eats a 409.
    Same treatment as an archived group.
    """
    plan = plan_channel_import(
        rows=(_row(group_id="cms-theirs"),),
        errors=(),
        existing={},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
        foreign_owner_group_ids=frozenset({"cms-theirs"}),
    )
    entry = plan.entries[0]
    assert entry.outcome is ChannelImportOutcome.ERROR
    assert entry.reason is not None and "content owner" in entry.reason
    assert "cms-theirs" in entry.reason
    assert plan.has_errors is True


def test_row_errors_surface_and_block() -> None:
    plan = _plan(errors=(ChannelImportRowError(row_number=2, reason="bad id"),))
    assert plan.has_errors is True
    assert plan.entries[0].outcome is ChannelImportOutcome.ERROR
    assert plan.entries[0].reason == "bad id"
    assert plan.counts["ERROR"] == 1


def test_entries_are_sorted_by_row_number() -> None:
    second = "UC3Dci3BzZXDo4jw4dU8KqWg"
    plan = _plan(
        rows=(_row(row_number=3, youtube_channel_id=second),),
        errors=(ChannelImportRowError(row_number=1, reason="bad id"),),
    )
    assert [entry.row_number for entry in plan.entries] == [1, 3]


def test_counts_cover_every_outcome_key() -> None:
    plan = _plan(rows=(_row(),))
    assert set(plan.counts) == {"CREATE", "UPDATE", "UNCHANGED", "ERROR"}
    assert plan.counts["UPDATE"] == 0


def test_duplicate_rows_plan_one_inventory_outcome_plus_membership_rows() -> None:
    """The first copy owns the inventory decision; extra copies attach groups.

    CMS membership is many-to-many, so a roster repeats a channel once per
    group (review #159 r3713966796). The second copy must not re-decide
    inventory (a second CREATE would 500 on the duplicate guard) — it plans
    as UNCHANGED carrying its own group_id for the apply to attach.
    """
    plan = _plan(
        rows=(
            _row(row_number=1, group_id="cms-tv"),
            _row(row_number=2, group_id="cms-news"),
        )
    )

    assert plan.has_errors is False
    assert [entry.outcome for entry in plan.entries] == [
        ChannelImportOutcome.CREATE,
        ChannelImportOutcome.UNCHANGED,
    ]
    assert [entry.group_id for entry in plan.entries] == ["cms-tv", "cms-news"]
    assert plan.counts["CREATE"] == 1
    assert plan.counts["UNCHANGED"] == 1


def test_every_repeated_row_for_an_archived_channel_is_an_error() -> None:
    """A repeated ARCHIVED channel fails every copy, not just the first.

    The archived check must precede the repeated-membership shortcut, or the
    later copies would be reported as writable UNCHANGED membership rows the
    apply never reaches (review #159 r3714401817).
    """
    plan = _plan(
        rows=(
            _row(row_number=1, group_id="cms-tv"),
            _row(row_number=2, group_id="cms-news"),
        ),
        existing={CHANNEL_ID: _existing(active=False)},
    )

    assert [entry.outcome for entry in plan.entries] == [
        ChannelImportOutcome.ERROR,
        ChannelImportOutcome.ERROR,
    ]
    assert plan.counts["ERROR"] == 2
    assert all("archived" in (entry.reason or "") for entry in plan.entries)
    assert all("reactivate" in (entry.reason or "") for entry in plan.entries)


def test_owner_null_group_is_flagged_as_an_ownership_write() -> None:
    """A row attaching to an owner-NULL group must SAY it stamps that group.

    This is the gap the archived and cross-owner cases do not cover: those
    fail the row closed, so the preview is honest by refusing. Adoption
    succeeds silently — the apply stamps ``content_owner_id`` permanently
    while the row itself reports UNCHANGED and an empty diff, so a dry run
    without this flag shows an operator nothing at all before an irreversible
    ownership claim (review #169 r3723536284).
    """
    plan = plan_channel_import(
        rows=(_row(group_id="cms-legacy"),),
        errors=(),
        existing={CHANNEL_ID: _existing()},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
        adoptable_group_ids=frozenset({"cms-legacy"}),
    )
    entry = plan.entries[0]
    assert entry.outcome is ChannelImportOutcome.UNCHANGED
    assert dict(entry.changes) == {}
    assert entry.will_adopt_content_owner is True


def test_already_owned_group_is_not_flagged_as_an_adoption() -> None:
    """Only owner-NULL keys adopt; a group this owner already holds writes nothing."""
    plan = _plan(rows=(_row(group_id="cms-mine"),), existing={CHANNEL_ID: _existing()})
    assert plan.entries[0].will_adopt_content_owner is False


def test_blocked_group_rows_never_claim_an_adoption() -> None:
    """An ERROR row is never applied, so it must not advertise an ownership write.

    A key cannot be both archived and adoptable in the same read, but the flag
    and the block are computed from independent sets, so pin that the block
    wins rather than relying on the store never disagreeing with itself.
    """
    plan = plan_channel_import(
        rows=(_row(group_id="cms-legacy"),),
        errors=(),
        existing={},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
        archived_group_ids=frozenset({"cms-legacy"}),
        adoptable_group_ids=frozenset({"cms-legacy"}),
    )
    entry = plan.entries[0]
    assert entry.outcome is ChannelImportOutcome.ERROR
    assert entry.will_adopt_content_owner is False


def test_every_row_targeting_one_adoptable_group_carries_the_flag() -> None:
    """The flag describes the row's TARGET, not which UPDATE statement runs.

    The apply stamps the group once, on whichever row its (group, channel)
    write order reaches first. Marking only that row would leave the other
    rows silently claiming nothing while their group is in fact being claimed,
    and would couple planning to the apply's write order.
    """
    other = "UC3Dci3BzZXDo4jw4dU8KqWg"
    plan = plan_channel_import(
        rows=(
            _row(row_number=1, group_id="cms-legacy"),
            _row(row_number=2, youtube_channel_id=other, group_id="cms-legacy"),
        ),
        errors=(),
        existing={},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
        adoptable_group_ids=frozenset({"cms-legacy"}),
    )
    assert [entry.will_adopt_content_owner for entry in plan.entries] == [True, True]


def test_repeated_membership_rows_carry_the_adoption_flag() -> None:
    """The second row for one channel is a bare membership add — and can adopt.

    That branch emits its entry through a different code path than the
    inventory branch, so it is exactly where a flag added in one place and not
    the other would go missing.
    """
    plan = plan_channel_import(
        rows=(
            _row(row_number=1, group_id="cms-mine"),
            _row(row_number=2, group_id="cms-legacy"),
        ),
        errors=(),
        existing={CHANNEL_ID: _existing()},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
        adoptable_group_ids=frozenset({"cms-legacy"}),
    )
    assert [entry.will_adopt_content_owner for entry in plan.entries] == [False, True]
