from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.runs.repository import (
    CONNECTOR_RUN_COUNT_KEYS,
    ConnectorRunLinkConflictError,
    ConnectorRunValidationError,
    finish_run,
    link_raw_file,
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
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ReportBase.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_connector_run_models_are_registered_on_report_base() -> None:
    assert "connector_runs" in ReportBase.metadata.tables
    assert "connector_run_raw_files" in ReportBase.metadata.tables


def test_connector_run_constraints_and_indexes_match_contract() -> None:
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
    constraints = {c.name for c in RawReportFileORM.__table__.constraints}

    assert "uq_raw_report_files_tenant_id_id" in constraints


def test_connector_run_raw_file_constraints_and_indexes_match_contract() -> None:
    constraints = {c.name for c in ConnectorRunRawFileORM.__table__.constraints}
    indexes = {i.name for i in ConnectorRunRawFileORM.__table__.indexes}

    assert "uq_connector_run_raw_files_run_file" in constraints
    assert "ck_connector_run_raw_files_ordering_index_non_negative" in constraints
    assert "ix_connector_run_raw_files_tenant_raw_file" in indexes


def test_start_run_inserts_running_row_with_zero_counts(session: Session) -> None:
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
    entry = start_run(
        session,
        tenant_id=TENANT_ID,
        connector_key="youtube-reporting",
        account_id="acct-test-1",
        report_month="2026-04",
        triggered_by_user_id=None,
    )

    assert entry.triggered_by_user_id is None


@pytest.mark.parametrize("month", ["26-04", "2026-00", "2026-13", "202604"])
def test_start_run_rejects_malformed_report_month(
    session: Session, month: str
) -> None:
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
    entry = _start_default_run(session)
    counts = {
        "reports_attempted": 2,
        "reports_succeeded": 2,
        "reports_failed": 0,
        "rows_upserted_total": 6,
        "rows_upserted_created": 4,
        "rows_upserted_updated": 1,
        "rows_upserted_unchanged": 1,
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
    return dict.fromkeys(CONNECTOR_RUN_COUNT_KEYS, 0)


def _start_default_run(session: Session):
    return start_run(
        session,
        tenant_id=TENANT_ID,
        connector_key="youtube-reporting",
        account_id="acct-test-1",
        report_month="2026-04",
        triggered_by_user_id=None,
    )


def _raw_file(
    session: Session,
    *,
    tenant_id: UUID,
    report_type: str,
) -> RawReportFileORM:
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
