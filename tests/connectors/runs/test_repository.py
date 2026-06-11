"""Repository tests for tenant-scoped connector runs."""
# pylint: disable=redefined-outer-name, too-many-arguments

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.runs.repository import (
    CONNECTOR_RUN_COUNT_KEYS,
    MAX_CONNECTOR_RUN_PAGE_SIZE,
    ConnectorRunLinkConflictError,
    ConnectorRunValidationError,
    finish_run,
    link_raw_file,
    list_runs,
    start_run,
)
from ums_smart_revenue.db import connector_models as _connector_models  # noqa: F401
from ums_smart_revenue.db.connector_models import (
    ConnectorRunORM,
    ConnectorRunRawFileORM,
)
from ums_smart_revenue.db.report_models import RawReportFileORM, ReportBase

TENANT_ID = UUID("00000000-0000-0000-0000-000000082301")
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000082302")
USER_ID = UUID("00000000-0000-0000-0000-000000082303")


@pytest.fixture
def session() -> Session:
    """Provide an in-memory SQLite session with initialized metadata."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ReportBase.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_connector_run_models_are_registered_on_report_base() -> None:
    """Verify that the connector run tables are registered in ReportBase."""
    assert "connector_runs" in ReportBase.metadata.tables
    assert "connector_run_raw_files" in ReportBase.metadata.tables


def test_connector_run_constraints_and_indexes_match_contract() -> None:
    """Ensure connector_runs constraints and indexes match the contract."""
    constraints = {c.name for c in ConnectorRunORM.__table__.constraints}
    indexes = {i.name for i in ConnectorRunORM.__table__.indexes}

    assert "uq_connector_runs_tenant_id" in constraints
    assert "fk_connector_runs_tenant" in constraints
    assert "fk_connector_runs_triggered_by_user" in constraints
    assert "ck_connector_runs_report_month_format" in constraints
    assert "ck_connector_runs_status" in constraints
    assert "ck_connector_runs_error_summary_len" in constraints
    assert "ck_connector_runs_finished_at_order" in constraints
    assert "ix_connector_runs_tenant_connector_month" in indexes
    assert "ix_connector_runs_tenant_started" in indexes


def test_raw_report_file_has_tenant_id_composite_unique_for_b2_3_fk() -> None:
    """Confirm RawReportFileORM has the expected composite unique constraint."""
    constraints = {c.name for c in RawReportFileORM.__table__.constraints}

    assert "uq_raw_report_files_tenant_id_id" in constraints


def test_connector_run_raw_file_constraints_and_indexes_match_contract() -> None:
    """Validate connector_run_raw_files constraints and indexes."""
    constraints = {c.name for c in ConnectorRunRawFileORM.__table__.constraints}
    indexes = {i.name for i in ConnectorRunRawFileORM.__table__.indexes}

    assert "uq_connector_run_raw_files_run_file" in constraints
    assert "ck_connector_run_raw_files_ordering_index_non_negative" in constraints
    assert "ix_connector_run_raw_files_tenant_raw_file" in indexes


def test_start_run_inserts_running_row_with_zero_counts(session: Session) -> None:
    """Start run creates a RUNNING entry with zeroed counts."""
    entry = start_run(
        session,
        tenant_id=TENANT_ID,
        connector_key="youtube-reporting",
        account_id="acct-test-1",
        report_month="2026-04",
        triggered_by_user_id=USER_ID,
    )

    row = session.scalars(select(ConnectorRunORM)).one()
    assert entry.id == str(row.id)
    assert row.tenant_id == TENANT_ID
    assert row.status == "RUNNING"
    assert row.finished_at is None
    assert row.error_summary is None
    assert row.counts_json == _zero_counts()
    assert entry.counts == _zero_counts()
    assert entry.triggered_by_user_id == str(USER_ID)


def test_start_run_accepts_null_triggered_by_user_id(session: Session) -> None:
    """Ensure start_run allows None for triggered_by_user_id."""
    entry = start_run(
        session,
        tenant_id=TENANT_ID,
        connector_key="youtube-reporting",
        account_id="acct-test-1",
        report_month="2026-04",
        triggered_by_user_id=None,
    )

    assert entry.triggered_by_user_id is None

@pytest.mark.parametrize(
    "month", ["26-04", "2026-00", "2026-13", "202604", "٢٠٢٦-٠٤"]
)
def test_start_run_rejects_malformed_report_month(
    session: Session, month: str
) -> None:
    """Check that start_run rejects malformed report_month values."""
    with pytest.raises(ConnectorRunValidationError, match="report_month"):
        start_run(
            session,
            tenant_id=TENANT_ID,
            connector_key="youtube-reporting",
            account_id="acct-test-1",
            report_month=month,
            triggered_by_user_id=None,
        )


def test_finish_run_sets_terminal_status_counts_and_finished_at(
    session: Session,
) -> None:
    """Verify finish_run updates status, counts, and finished_at."""
    entry = _start_default_run(session)
    counts = {
        "reports_attempted": 2,
        "reports_succeeded": 2,
        "reports_failed": 0,
        "rows_upserted_total": 6,
        "rows_upserted_created": 4,
        "rows_upserted_updated": 1,
        "rows_upserted_unchanged": 1,
        "rows_deleted_stale": 0,
    }

    finished = finish_run(
        session,
        tenant_id=TENANT_ID,
        connector_run_id=UUID(entry.id),
        status="SUCCEEDED",
        counts=counts,
        error_summary=None,
    )

    assert finished.status == "SUCCEEDED"
    assert finished.finished_at is not None
    assert finished.error_summary is None
    assert finished.counts == counts


def test_finish_run_truncates_error_summary_to_500_chars(
    session: Session,
) -> None:
    """Ensure finish_run trims error_summary to 500 characters."""
    entry = _start_default_run(session)

    finished = finish_run(
        session,
        tenant_id=TENANT_ID,
        connector_run_id=UUID(entry.id),
        status="FAILED",
        counts=_zero_counts(),
        error_summary="x" * 600,
    )

    assert finished.error_summary == "x" * 500

@pytest.mark.parametrize("status", ["RUNNING", "SUCCESS", "failed"])
def test_finish_run_rejects_non_terminal_or_unknown_status(
    session: Session, status: str
) -> None:
    """Check that finish_run rejects non-terminal or invalid statuses."""
    entry = _start_default_run(session)

    with pytest.raises(ConnectorRunValidationError, match="terminal"):
        finish_run(
            session,
            tenant_id=TENANT_ID,
            connector_run_id=UUID(entry.id),
            status=status,
            counts=_zero_counts(),
            error_summary=None,
        )


def test_finish_run_rejects_counts_with_missing_or_extra_keys(
    session: Session,
) -> None:
    """Validate finish_run rejects counts with missing or extra keys."""
    entry = _start_default_run(session)
    bad_counts = _zero_counts()
    bad_counts.pop("reports_failed")

    with pytest.raises(ConnectorRunValidationError, match="counts_json"):
        finish_run(
            session,
            tenant_id=TENANT_ID,
            connector_run_id=UUID(entry.id),
            status="FAILED",
            counts=bad_counts,
            error_summary="failed",
        )


def test_finish_run_rejects_counts_with_negative_or_non_int_values(
    session: Session,
) -> None:
    """Ensure finish_run rejects negative or non-integer counts."""
    entry = _start_default_run(session)
    bad_counts = _zero_counts()
    bad_counts["reports_failed"] = -1

    with pytest.raises(ConnectorRunValidationError, match="non-negative integer"):
        finish_run(
            session,
            tenant_id=TENANT_ID,
            connector_run_id=UUID(entry.id),
            status="FAILED",
            counts=bad_counts,
            error_summary="failed",
        )


def test_finish_run_rejects_cross_tenant_run_id(session: Session) -> None:
    """Finish run rejects a run ID that belongs to another tenant."""
    entry = _start_default_run(session)

    with pytest.raises(LookupError, match="Connector run not found"):
        finish_run(
            session,
            tenant_id=OTHER_TENANT_ID,
            connector_run_id=UUID(entry.id),
            status="FAILED",
            counts=_zero_counts(),
            error_summary="wrong tenant",
        )


def test_finish_run_rejects_already_terminal_run(session: Session) -> None:
    """Finish run rejects attempts to finish an already terminal run."""
    entry = _start_default_run(session)
    finish_run(
        session,
        tenant_id=TENANT_ID,
        connector_run_id=UUID(entry.id),
        status="FAILED",
        counts=_zero_counts(),
        error_summary="first failure",
    )

    with pytest.raises(ConnectorRunValidationError, match="already terminal"):
        finish_run(
            session,
            tenant_id=TENANT_ID,
            connector_run_id=UUID(entry.id),
            status="FAILED",
            counts=_zero_counts(),
            error_summary="second failure",
        )


def test_link_raw_file_inserts_tenant_scoped_join_row(session: Session) -> None:
    """Link raw file creates a tenant-scoped join row."""
    entry = _start_default_run(session)
    raw_file = _raw_file(session, tenant_id=TENANT_ID, report_type="rt-a")

    link_raw_file(
        session,
        tenant_id=TENANT_ID,
        connector_run_id=UUID(entry.id),
        raw_report_file_id=raw_file.id,
        ordering_index=0,
    )

    row = session.scalars(select(ConnectorRunRawFileORM)).one()
    assert row.tenant_id == TENANT_ID
    assert row.connector_run_id == UUID(entry.id)
    assert row.raw_report_file_id == raw_file.id
    assert row.ordering_index == 0

@pytest.mark.parametrize("ordering_index", [-1, None, True, "1"])
def test_link_raw_file_rejects_invalid_ordering_index(
    session: Session, ordering_index
) -> None:
    """Link raw file rejects invalid ordering_index values."""
    entry = _start_default_run(session)
    raw_file = _raw_file(session, tenant_id=TENANT_ID, report_type="rt-a")

    with pytest.raises(ConnectorRunValidationError, match="ordering_index"):
        link_raw_file(
            session,
            tenant_id=TENANT_ID,
            connector_run_id=UUID(entry.id),
            raw_report_file_id=raw_file.id,
            ordering_index=ordering_index,
        )


def test_link_raw_file_rejects_raw_file_from_different_tenant(
    session: Session,
) -> None:
    """Link raw file rejects raw files from a different tenant."""
    entry = _start_default_run(session)
    raw_file = _raw_file(session, tenant_id=OTHER_TENANT_ID, report_type="rt-other")

    with pytest.raises(LookupError, match="Raw report file not found"):
        link_raw_file(
            session,
            tenant_id=TENANT_ID,
            connector_run_id=UUID(entry.id),
            raw_report_file_id=raw_file.id,
            ordering_index=0,
        )


def test_link_raw_file_rejects_cross_tenant_run_id(session: Session) -> None:
    """Link raw file rejects run IDs from a different tenant."""
    entry = _start_default_run(session)
    raw_file = _raw_file(session, tenant_id=OTHER_TENANT_ID, report_type="rt-other")

    with pytest.raises(LookupError, match="Connector run not found"):
        link_raw_file(
            session,
            tenant_id=OTHER_TENANT_ID,
            connector_run_id=UUID(entry.id),
            raw_report_file_id=raw_file.id,
            ordering_index=0,
        )


def test_link_raw_file_duplicate_is_rejected_by_unique_constraint(
    session: Session,
) -> None:
    """Duplicate raw-file links raise ConnectorRunLinkConflictError."""
    entry = _start_default_run(session)
    raw_file = _raw_file(session, tenant_id=TENANT_ID, report_type="rt-a")

    link_raw_file(
        session,
        tenant_id=TENANT_ID,
        connector_run_id=UUID(entry.id),
        raw_report_file_id=raw_file.id,
        ordering_index=0,
    )

    with pytest.raises(ConnectorRunLinkConflictError, match="already linked"):
        link_raw_file(
            session,
            tenant_id=TENANT_ID,
            connector_run_id=UUID(entry.id),
            raw_report_file_id=raw_file.id,
            ordering_index=1,
        )

    second_raw_file = _raw_file(session, tenant_id=TENANT_ID, report_type="rt-b")
    link_raw_file(
        session,
        tenant_id=TENANT_ID,
        connector_run_id=UUID(entry.id),
        raw_report_file_id=second_raw_file.id,
        ordering_index=2,
    )
    rows = session.scalars(select(ConnectorRunRawFileORM)).all()
    assert len(rows) == 2


def _zero_counts() -> dict[str, int]:
    """Return a zeroed count mapping for connector-run helpers."""
    return dict.fromkeys(CONNECTOR_RUN_COUNT_KEYS, 0)


def _start_default_run(session: Session):
    """Start the default connector run fixture used by the tests."""
    return start_run(
        session,
        tenant_id=TENANT_ID,
        connector_key="youtube-reporting",
        account_id="acct-test-1",
        report_month="2026-04",
        triggered_by_user_id=None,
    )

_TERMINAL_COUNTS = {
    "reports_attempted": 2,
    "reports_succeeded": 2,
    "reports_failed": 0,
    "rows_upserted_total": 10,
    "rows_upserted_created": 6,
    "rows_upserted_updated": 3,
    "rows_upserted_unchanged": 1,
    "rows_deleted_stale": 0,
}


def _seed_run(
    session: Session,
    *,
    tenant_id: UUID = TENANT_ID,
    connector_key: str = "youtube-reporting",
    account_id: str = "acct-test-1",
    started_at: datetime,
    status: str = "SUCCEEDED",
) -> ConnectorRunORM:
    """Insert a deterministic connector run row for ordering and cursor tests."""
    row = ConnectorRunORM(
        id=uuid4(),
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
        report_month="2026-04",
        triggered_by_user_id=USER_ID,
        started_at=started_at,
        finished_at=started_at,
        status=status,
        counts_json=dict(_TERMINAL_COUNTS),
        error_summary=None,
    )
    session.add(row)
    session.flush()
    return row


def test_list_runs_returns_newest_first(session: Session) -> None:
    """Newest-first ordering returns the latest run first."""
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    oldest = _seed_run(session, started_at=base)
    middle = _seed_run(session, started_at=base.replace(hour=13))
    newest = _seed_run(session, started_at=base.replace(hour=14))

    page = list_runs(session, tenant_id=TENANT_ID, limit=10)

    assert [item.id for item in page.items] == [
        str(newest.id),
        str(middle.id),
        str(oldest.id),
    ]
    assert page.next_cursor is None
    assert page.limit == 10


def test_list_runs_cursor_walk_returns_next_page(session: Session) -> None:
    """Cursor pagination returns the next window and boundary cursor."""
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    rows = [_seed_run(session, started_at=base.replace(minute=m)) for m in range(5)]
    # rows[4] is newest; newest-first order = reversed.
    ordered = list(reversed(rows))

    first = list_runs(session, tenant_id=TENANT_ID, limit=2)
    assert [i.id for i in first.items] == [str(ordered[0].id), str(ordered[1].id)]
    assert first.next_cursor is not None
    assert first.next_cursor["id"] == str(ordered[1].id)
    assert first.next_cursor["started_at"] == ordered[1].started_at.isoformat()

    second = list_runs(
        session,
        tenant_id=TENANT_ID,
        limit=2,
        cursor_started_at=datetime.fromisoformat(first.next_cursor["started_at"]),
        cursor_id=first.next_cursor["id"],
    )
    assert [i.id for i in second.items] == [str(ordered[2].id), str(ordered[3].id)]


def test_list_runs_half_cursor_raises(session: Session) -> None:
    """A half-present cursor pair is rejected with a validation error."""
    with pytest.raises(ConnectorRunValidationError):
        list_runs(
            session,
            tenant_id=TENANT_ID,
            limit=10,
            cursor_started_at=datetime.now(UTC),
        )
    with pytest.raises(ConnectorRunValidationError):
        list_runs(session, tenant_id=TENANT_ID, limit=10, cursor_id=str(uuid4()))


def test_list_runs_limit_out_of_range_raises(session: Session) -> None:
    """Out-of-range list limits fail closed."""
    with pytest.raises(ConnectorRunValidationError):
        list_runs(session, tenant_id=TENANT_ID, limit=0)
    with pytest.raises(ConnectorRunValidationError):
        list_runs(
            session, tenant_id=TENANT_ID, limit=MAX_CONNECTOR_RUN_PAGE_SIZE + 1
        )


def test_list_runs_excludes_other_tenant(session: Session) -> None:
    """Tenant scoping excludes rows from other tenants."""
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    mine = _seed_run(session, started_at=base)
    _seed_run(session, tenant_id=OTHER_TENANT_ID, started_at=base.replace(hour=13))

    page = list_runs(session, tenant_id=TENANT_ID, limit=10)

    assert [item.id for item in page.items] == [str(mine.id)]


def test_list_runs_connector_key_filter(session: Session) -> None:
    """Connector-key filtering returns only matching runs."""
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    yt = _seed_run(session, connector_key="youtube-reporting", started_at=base)
    _seed_run(session, connector_key="adsense", started_at=base.replace(hour=13))

    page = list_runs(
        session, tenant_id=TENANT_ID, connector_key="youtube-reporting", limit=10
    )

    assert [item.id for item in page.items] == [str(yt.id)]


def test_list_runs_connector_keys_filter(session: Session) -> None:
    """Connector-key set filtering returns only the allowed connector runs."""
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    yt = _seed_run(session, connector_key="youtube-reporting", started_at=base)
    adsense = _seed_run(
        session, connector_key="adsense", started_at=base.replace(hour=13)
    )
    _seed_run(session, connector_key="news", started_at=base.replace(hour=14))

    page = list_runs(
        session,
        tenant_id=TENANT_ID,
        connector_keys={"youtube-reporting", "adsense"},
        limit=10,
    )

    assert [item.id for item in page.items] == [str(adsense.id), str(yt.id)]


def test_list_runs_account_id_filter(session: Session) -> None:
    """Account-id filtering returns only rows for the requested account."""
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    a = _seed_run(session, account_id="acct-a", started_at=base)
    _seed_run(session, account_id="acct-b", started_at=base.replace(hour=13))

    page = list_runs(session, tenant_id=TENANT_ID, account_id="acct-a", limit=10)

    assert [item.id for item in page.items] == [str(a.id)]


def test_list_runs_has_more_boundary(session: Session) -> None:
    """The page reports next_cursor only when an extra row exists."""
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    for m in range(3):
        _seed_run(session, started_at=base.replace(minute=m))

    exact = list_runs(session, tenant_id=TENANT_ID, limit=3)
    assert len(exact.items) == 3
    assert exact.next_cursor is None

    over = list_runs(session, tenant_id=TENANT_ID, limit=2)
    assert len(over.items) == 2
    assert over.next_cursor is not None


def test_to_api_shape_excludes_tenant_id(session: Session) -> None:
    """The API payload hides tenant_id and preserves operational fields."""
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    _seed_run(session, started_at=base)

    page = list_runs(session, tenant_id=TENANT_ID, limit=10)
    api = page.items[0].to_api()

    assert "tenant_id" not in api
    assert api["status"] == "SUCCEEDED"
    assert api["counts"] == _TERMINAL_COUNTS
    assert api["started_at"] == page.items[0].started_at.isoformat()
    assert api["finished_at"] == page.items[0].finished_at.isoformat()
    assert api["connector_key"] == "youtube-reporting"
    assert api["account_id"] == "acct-test-1"
    assert api["report_month"] == "2026-04"
    assert api["triggered_by_user_id"] == str(USER_ID)


def test_to_api_normalizes_legacy_counts_missing_rows_deleted_stale(
    session: Session,
) -> None:
    """Historical rows written before ``rows_deleted_stale`` was added read back normalized.

    The PR added ``rows_deleted_stale`` to the fixed ``counts_json`` B2.3
    contract. Pre-existing ``connector_runs`` rows in the database still
    carry the old 7-key shape on disk. ``list_runs`` and ``to_api`` must
    return the full fixed key set with the missing key defaulted to 0 so
    the operator-facing read API never emits mixed-shape payloads.
    """
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    legacy_counts = {key: _TERMINAL_COUNTS[key] for key in _TERMINAL_COUNTS if key != "rows_deleted_stale"}
    assert "rows_deleted_stale" not in legacy_counts

    legacy_row = ConnectorRunORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        connector_key="youtube-reporting",
        account_id="acct-test-1",
        report_month="2026-04",
        triggered_by_user_id=USER_ID,
        started_at=base,
        finished_at=base,
        status="SUCCEEDED",
        counts_json=legacy_counts,
        error_summary=None,
    )
    session.add(legacy_row)
    session.flush()

    page = list_runs(session, tenant_id=TENANT_ID, limit=10)
    api = page.items[0].to_api()

    assert set(api["counts"].keys()) == set(CONNECTOR_RUN_COUNT_KEYS)
    assert api["counts"]["rows_deleted_stale"] == 0
    for key, value in legacy_counts.items():
        assert api["counts"][key] == value


def test_to_entry_normalizes_legacy_counts_with_extra_keys_dropped(
    session: Session,
) -> None:
    """Defensive read-time normalization drops unknown keys and coerces non-int values to 0.

    A pre-existing or corrupted row whose ``counts_json`` carries extra
    legacy keys, a missing key, or a non-integer value for an expected
    key must not break the operator-facing read path; the read API still
    returns the full fixed B2.3 key set.
    """
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    messy_counts = {
        "reports_attempted": 1,
        "reports_succeeded": 1,
        "reports_failed": 0,
        "rows_upserted_total": 4,
        "rows_upserted_created": 4,
        "rows_upserted_updated": 0,
        "rows_upserted_unchanged": 0,
        "rows_deleted_stale": "missing",  # Non-integer: must coerce to 0.
        "legacy_unsupported_key": 99,  # Extra key: must be dropped on read.
    }
    messy_row = ConnectorRunORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        connector_key="youtube-reporting",
        account_id="acct-test-1",
        report_month="2026-04",
        triggered_by_user_id=USER_ID,
        started_at=base,
        finished_at=base,
        status="SUCCEEDED",
        counts_json=messy_counts,
        error_summary=None,
    )
    session.add(messy_row)
    session.flush()

    page = list_runs(session, tenant_id=TENANT_ID, limit=10)
    counts = page.items[0].counts

    assert set(counts.keys()) == set(CONNECTOR_RUN_COUNT_KEYS)
    assert "legacy_unsupported_key" not in counts
    assert counts["rows_deleted_stale"] == 0
    assert counts["rows_upserted_total"] == 4


def test_to_entry_coerces_negative_counts_to_zero(
    session: Session,
) -> None:
    """Read-time normalization coerces negative integers to 0.

    The B2.3 spec promises every read-side payload contains the full
    key set with ``defaults to 0``; a negative integer (which can only
    be the result of data corruption, since row counts are non-negative
    by definition) violates that promise. ``_normalize_counts_for_read``
    must clamp negative integers to 0 so a corrupted historical row
    cannot surface a negative count to the operator-facing API.
    """
    base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    corrupted_counts = dict(_TERMINAL_COUNTS)
    corrupted_counts["rows_upserted_total"] = -3
    corrupted_counts["rows_deleted_stale"] = -1
    corrupted_row = ConnectorRunORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        connector_key="youtube-reporting",
        account_id="acct-test-1",
        report_month="2026-04",
        triggered_by_user_id=USER_ID,
        started_at=base,
        finished_at=base,
        status="SUCCEEDED",
        counts_json=corrupted_counts,
        error_summary=None,
    )
    session.add(corrupted_row)
    session.flush()

    page = list_runs(session, tenant_id=TENANT_ID, limit=10)
    counts = page.items[0].counts

    assert counts["rows_upserted_total"] == 0
    assert counts["rows_deleted_stale"] == 0


def _raw_file(
    session: Session,
    *,
    tenant_id: UUID,
    report_type: str,
) -> RawReportFileORM:
    """Insert a raw report file row for connector-run linkage tests."""
    row = RawReportFileORM(
        id=uuid4(),
        tenant_id=tenant_id,
        source="youtube-reporting",
        report_type=report_type,
        report_month="2026-04",
        file_url=f"file-store://tenant/{report_type}.csv",
        checksum=uuid4().hex,
        parse_status="DOWNLOADED",
    )
    session.add(row)
    session.flush()
    return row
