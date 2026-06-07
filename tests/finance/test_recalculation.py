"""Unit tests for build_recalculation_preview, including verified_channel_ids."""
from decimal import Decimal

from ums_smart_revenue.finance.recalculation import build_recalculation_preview
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry


def _fact(*, channel_id: str, month: str = "2026-04", gross: str = "100.00") -> RevenueFactEntry:
    """Minimal RevenueFactEntry for preview tests."""
    return RevenueFactEntry(
        id=f"fact-{channel_id}",
        month=month,
        youtube_channel_id=channel_id,
        source_kind="ADSENSE",
        source_report_id=None,
        gross_revenue_usd=Decimal(gross),
        net_revenue_usd=None,
        views=1,
        watch_time_minutes=Decimal("1"),
        confidence_score=Decimal("1"),
        imported_by=None,
    )


def _base_preview(**kwargs):
    """Call build_recalculation_preview with common defaults."""
    return build_recalculation_preview(
        month="2026-04",
        allocation_method="company_level",
        scope_type="sector",
        scope_id="sec-1",
        currency="USD",
        dry_run=True,
        manual_overrides=[],
        **kwargs,
    )


def test_verified_channel_ids_blocks_when_extra_channel_unmapped():
    """A channel in verified_channel_ids but absent from channel_company triggers BLOCKED.

    Without verified_channel_ids the preview only checks fact channels, so it
    would falsely return READY_FOR_REVIEW while the commit engine would fail
    with COMPANY_UNMAPPED for the fact-less but verified channel.
    """
    # chA has a fact and a company mapping — fine on its own.
    # chB is verified (in scope) but has no fact row and no company mapping.
    preview = _base_preview(
        facts=[_fact(channel_id="chA")],
        channel_company={"chA": "compA"},
        verified_channel_ids=frozenset({"chA", "chB"}),
    )

    assert preview.status == "BLOCKED"
    issue_types = {issue.issue_type for issue in preview.blocking_issues}
    assert "COMPANY_MAPPING_MISSING" in issue_types


def test_without_verified_channel_ids_misses_fact_less_unmapped_channel():
    """Without verified_channel_ids the preview only checks fact channels.

    This documents the known limitation: chB (no facts, no company) is not
    detected as unmapped and the preview incorrectly returns READY_FOR_REVIEW.
    The verified_channel_ids parameter closes this gap for non-global scopes.
    """
    preview = _base_preview(
        facts=[_fact(channel_id="chA")],
        channel_company={"chA": "compA"},
        verified_channel_ids=None,  # not supplied
    )

    # Only fact channels are checked — chA is mapped, so no blocking issue.
    assert preview.status == "READY_FOR_REVIEW"
    assert preview.blocking_issues == ()


def test_verified_channel_ids_ready_when_all_channels_mapped():
    """If all verified channels (including fact-less ones) are mapped, status is READY."""
    preview = _base_preview(
        facts=[_fact(channel_id="chA")],
        channel_company={"chA": "compA", "chB": "compA"},
        verified_channel_ids=frozenset({"chA", "chB"}),
    )

    assert preview.status == "READY_FOR_REVIEW"
    assert preview.blocking_issues == ()


def test_verified_channel_ids_none_falls_back_to_fact_channels_only():
    """verified_channel_ids=None is equivalent to not supplying it."""
    preview_none = _base_preview(
        facts=[_fact(channel_id="chA")],
        channel_company={"chA": "compA"},
        verified_channel_ids=None,
    )
    preview_default = _base_preview(
        facts=[_fact(channel_id="chA")],
        channel_company={"chA": "compA"},
    )
    assert preview_none.status == preview_default.status
    assert preview_none.blocking_issues == preview_default.blocking_issues


def test_no_allocation_factless_preview_is_ready():
    """no_allocation is in SOURCE_FACTS_OPTIONAL_METHODS: zero facts → READY_FOR_REVIEW."""
    preview = build_recalculation_preview(
        month="2026-04",
        allocation_method="no_allocation",
        scope_type="global",
        scope_id=None,
        currency="USD",
        dry_run=True,
        facts=[],
        manual_overrides=[],
    )
    assert preview.status == "READY_FOR_REVIEW"
    assert not preview.blocking_issues


def test_manual_factless_preview_is_blocked():
    """manual is NOT in SOURCE_FACTS_OPTIONAL_METHODS: zero facts → BLOCKED.

    The recalculation route rejects dry_run=false/manual with 422 (it redirects
    to the account-allocations/commit endpoint), so a manual dry-run returning
    READY_FOR_REVIEW with no facts would be a false positive with no commit path.
    """
    preview = build_recalculation_preview(
        month="2026-04",
        allocation_method="manual",
        scope_type="global",
        scope_id=None,
        currency="USD",
        dry_run=True,
        facts=[],
        manual_overrides=[],
    )
    assert preview.status == "BLOCKED"
    issue_types = [i.issue_type for i in preview.blocking_issues]
    assert "NO_REVENUE_FACTS" in issue_types
