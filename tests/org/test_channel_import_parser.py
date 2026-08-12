"""Pure CSV parsing for the bulk channel import."""

import pytest

from ums_smart_revenue.org.channel_import import (
    ChannelImportFormatError,
    parse_channel_import_csv,
)

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"


def test_parses_required_columns() -> None:
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC Egypt\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.errors == ()
    assert len(parsed.rows) == 1
    row = parsed.rows[0]
    assert row.row_number == 1
    assert row.youtube_channel_id == CHANNEL_ID
    assert row.channel_name == "CBC Egypt"
    assert row.group_id is None
    assert row.view_revenue is None


def test_parses_optional_columns_in_any_order_and_case() -> None:
    csv_text = (
        f"View_Revenue,Group_ID,CHANNEL_NAME,youtube_channel_id\nYes,cms-tv,CBC,{CHANNEL_ID}\n"
    )
    parsed = parse_channel_import_csv(csv_text)
    row = parsed.rows[0]
    assert row.group_id == "cms-tv"
    assert row.view_revenue is True


def test_tolerates_utf8_bom_and_arabic_names() -> None:
    csv_text = f"﻿youtube_channel_id,channel_name\n{CHANNEL_ID},هاشتاج\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.errors == ()
    assert parsed.rows[0].channel_name == "هاشتاج"


def test_rejects_unknown_header() -> None:
    csv_text = f"youtube_channel_id,channel_name,revenue_usd\n{CHANNEL_ID},CBC,100\n"
    with pytest.raises(ChannelImportFormatError, match="unknown column"):
        parse_channel_import_csv(csv_text)


def test_rejects_missing_required_header() -> None:
    csv_text = "channel_name\nCBC\n"
    with pytest.raises(ChannelImportFormatError, match="missing required column"):
        parse_channel_import_csv(csv_text)


def test_rejects_empty_file() -> None:
    with pytest.raises(ChannelImportFormatError, match="empty"):
        parse_channel_import_csv("")


def test_flags_every_copy_of_a_conflicting_duplicate_id() -> None:
    """Copies that disagree on inventory fields are ambiguous and all fail."""
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},First\n{CHANNEL_ID},Second\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert [error.row_number for error in parsed.errors] == [1, 2]
    assert "conflicting duplicate" in parsed.errors[0].reason


def test_keeps_agreeing_duplicates_for_multi_group_membership() -> None:
    """CMS membership is many-to-many: one row per group is a legal roster."""
    csv_text = (
        "youtube_channel_id,channel_name,group_id\n"
        f"{CHANNEL_ID},Alpha News,cms-tv\n"
        f"{CHANNEL_ID},Alpha News,cms-news\n"
    )
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.errors == ()
    assert [row.group_id for row in parsed.rows] == ["cms-tv", "cms-news"]


def test_flags_every_copy_of_a_repeated_channel_group_pair() -> None:
    """Repeating one association says nothing the first copy did not say.

    The write side collapses it, so keeping both copies would let the preview
    promise the group work twice for one membership.
    """
    csv_text = (
        "youtube_channel_id,channel_name,group_id\n"
        f"{CHANNEL_ID},Alpha News,cms-tv\n"
        f"{CHANNEL_ID},Alpha News,cms-tv\n"
    )
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert [error.row_number for error in parsed.errors] == [1, 2]
    assert "already associated with group_id cms-tv" in parsed.errors[0].reason


def test_flags_a_repeated_channel_carrying_no_group() -> None:
    """With no group there is no many-to-many justification for the repeat."""
    csv_text = (
        "youtube_channel_id,channel_name\n"
        f"{CHANNEL_ID},Alpha News\n"
        f"{CHANNEL_ID},Alpha News\n"
    )
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert [error.row_number for error in parsed.errors] == [1, 2]
    assert "with no group_id" in parsed.errors[0].reason


def test_reports_the_conflict_when_a_repeat_also_disagrees() -> None:
    """A channel with no settled inventory has no group to attach yet.

    Both rules match these rows; naming the repeat would send the operator to
    fix the lesser defect.
    """
    csv_text = (
        "youtube_channel_id,channel_name,group_id\n"
        f"{CHANNEL_ID},First,cms-tv\n"
        f"{CHANNEL_ID},Second,cms-tv\n"
    )
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert all("conflicting duplicate" in error.reason for error in parsed.errors)


def test_flags_malformed_channel_id() -> None:
    csv_text = "youtube_channel_id,channel_name\nهاشتاج,CBC\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert "invalid youtube_channel_id" in parsed.errors[0].reason


def test_flags_empty_channel_name() -> None:
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},   \n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert "channel_name is empty" in parsed.errors[0].reason


def test_flags_blank_view_revenue_when_column_present() -> None:
    csv_text = f"youtube_channel_id,channel_name,view_revenue\n{CHANNEL_ID},CBC,\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert "view_revenue is present but blank" in parsed.errors[0].reason


def test_flags_unrecognised_view_revenue_token() -> None:
    csv_text = f"youtube_channel_id,channel_name,view_revenue\n{CHANNEL_ID},CBC,maybe\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert "view_revenue" in parsed.errors[0].reason


def test_accepts_all_view_revenue_token_forms() -> None:
    for token, expected in (
        ("yes", True),
        ("TRUE", True),
        ("1", True),
        ("no", False),
        ("False", False),
        ("0", False),
    ):
        csv_text = f"youtube_channel_id,channel_name,view_revenue\n{CHANNEL_ID},CBC,{token}\n"
        parsed = parse_channel_import_csv(csv_text)
        assert parsed.errors == (), token
        assert parsed.rows[0].view_revenue is expected, token


def test_rejects_row_wider_than_header() -> None:
    """An unescaped comma must fail the row, not silently truncate the name."""
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},Alpha,News\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert "3 cell(s)" in parsed.errors[0].reason
    assert "2 column(s)" in parsed.errors[0].reason


def test_rejects_duplicate_header_columns() -> None:
    """A duplicated header is an ambiguous schema and fails the whole file."""
    csv_text = f"youtube_channel_id,channel_name,channel_name\n{CHANNEL_ID},First,Second\n"
    with pytest.raises(ChannelImportFormatError, match="duplicate column"):
        parse_channel_import_csv(csv_text)


def test_rejects_an_absurdly_wide_header_before_scanning_it() -> None:
    """A pathological header is rejected on width, not scanned per cell.

    Duplicate detection is linear (Counter), and the width gate stops a
    multi-hundred-thousand-cell header from being examined at all — only four
    columns are ever valid (review #159 r3714142163).
    """
    wide = "youtube_channel_id,channel_name" + ",filler" * 50_000
    with pytest.raises(ChannelImportFormatError, match="at most 16 are valid"):
        parse_channel_import_csv(f"{wide}\n")


def test_max_rows_aborts_the_parse_early() -> None:
    """The row cap fires during parsing, counting only non-blank data rows."""
    rows = "\n".join(f"UC{index:022d},Channel {index}" for index in range(3))
    csv_text = f"youtube_channel_id,channel_name\n{rows}\n"
    with pytest.raises(ChannelImportFormatError, match="exceeds 2 rows"):
        parse_channel_import_csv(csv_text, max_rows=2)
    # A blank line is skipped, not counted against the data-row cap (blanks
    # carry their own equal bound — see test_blank_rows_are_bounded_by_the_same_cap).
    spaced = f"youtube_channel_id,channel_name\n\n{CHANNEL_ID},CBC\n"
    parsed = parse_channel_import_csv(spaced, max_rows=1)
    assert len(parsed.rows) == 1


def test_blank_rows_are_bounded_by_the_same_cap() -> None:
    """A file packed with blank records aborts instead of consuming free scans.

    Blank rows never count toward the data-row cap, so without their own bound
    a small byte-capped file of blank lines would be scanned end-to-end while
    'never exceeding' the row limit (PR #159 review).
    """
    blanks = "\n" * 4
    csv_text = f"youtube_channel_id,channel_name\n{blanks}{CHANNEL_ID},CBC\n"
    with pytest.raises(ChannelImportFormatError, match="exceeds 2 blank rows"):
        parse_channel_import_csv(csv_text, max_rows=2)
    # A legitimate export's few blank records stay tolerated under the bound.
    tolerated = f"youtube_channel_id,channel_name\n\n{CHANNEL_ID},CBC\n\n"
    parsed = parse_channel_import_csv(tolerated, max_rows=2)
    assert len(parsed.rows) == 1


def test_rejects_nul_in_channel_name() -> None:
    """PostgreSQL cannot store NUL in text; the row must fail at parse time."""
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},Alpha\x00News\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert "NUL" in parsed.errors[0].reason


def test_rejects_nul_in_group_id() -> None:
    """A NUL-bearing group key fails the row instead of 500ing at persistence."""
    csv_text = f"youtube_channel_id,channel_name,group_id\n{CHANNEL_ID},Alpha News,cms\x00tv\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert "NUL" in parsed.errors[0].reason


def test_rejects_oversized_group_id() -> None:
    """A multi-KB group key fails the row: it would exceed the unique B-tree
    index's per-entry limit at apply and 500 after a clean dry run."""
    long_key = "g" * 256
    csv_text = f"youtube_channel_id,channel_name,group_id\n{CHANNEL_ID},Alpha News,{long_key}\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert "group_id exceeds 255 characters" in parsed.errors[0].reason
    # The boundary itself is legal.
    ok = f"youtube_channel_id,channel_name,group_id\n{CHANNEL_ID},Alpha News,{'g' * 255}\n"
    assert parse_channel_import_csv(ok).errors == ()


def test_rejects_malformed_quoted_csv() -> None:
    """An unterminated quote rejects the file instead of folding rows into one name."""
    second = "UC3Dci3BzZXDo4jw4dU8KqWg"
    csv_text = f'youtube_channel_id,channel_name\n{CHANNEL_ID},"Alpha News\n{second},Beta\n'
    with pytest.raises(ChannelImportFormatError, match="malformed CSV"):
        parse_channel_import_csv(csv_text)


def test_preserves_raw_view_revenue_token() -> None:
    """The operator's original token survives parsing for audit provenance."""
    csv_text = f"youtube_channel_id,channel_name,view_revenue\n{CHANNEL_ID},CBC, TRUE \n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.errors == ()
    assert parsed.rows[0].view_revenue is True
    assert parsed.rows[0].view_revenue_raw == "TRUE"


def test_raw_view_revenue_is_none_when_column_absent() -> None:
    """An absent column leaves no raw token, distinguishing default from explicit."""
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows[0].view_revenue is None
    assert parsed.rows[0].view_revenue_raw is None


def test_skips_fully_blank_lines() -> None:
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n\n"
    parsed = parse_channel_import_csv(csv_text)
    assert len(parsed.rows) == 1
    assert parsed.errors == ()


def test_row_numbers_are_one_based_and_exclude_the_header() -> None:
    second = "UC3Dci3BzZXDo4jw4dU8KqWg"
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n{second},CBC Drama\n"
    parsed = parse_channel_import_csv(csv_text)
    assert [row.row_number for row in parsed.rows] == [1, 2]


def test_rejects_a_header_only_file() -> None:
    """An empty roster is an incomplete export, not a successful import.

    Without this the apply would return 200 and audit a CHANNEL_IMPORTED
    summary for a file that processed nothing (review #159 r3714884517).
    """
    with pytest.raises(ChannelImportFormatError, match="no data rows"):
        parse_channel_import_csv("youtube_channel_id,channel_name\n")


def test_rejects_a_blank_only_file() -> None:
    """Blank records alone do not make a roster either."""
    with pytest.raises(ChannelImportFormatError, match="no data rows"):
        parse_channel_import_csv("youtube_channel_id,channel_name\n\n\n")
