"""Pure diff planning for the bulk channel import."""

from ums_smart_revenue.org.channel_import import (
    ChannelImportOutcome,
    ChannelImportRow,
    ChannelImportRowError,
    plan_channel_import,
)
from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
CONTENT_OWNER = "PlZrS5Fh56RMd9dmSL6XSA"


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
