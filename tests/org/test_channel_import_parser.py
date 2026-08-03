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


def test_flags_every_copy_of_a_duplicate_id() -> None:
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},First\n{CHANNEL_ID},Second\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert [error.row_number for error in parsed.errors] == [1, 2]


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
