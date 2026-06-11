"""Wiring unit tests for the post-run normalize stage in ``run_one``.

These tests isolate the orchestrator's finance-projection seam
(``_normalize_ingested_source_rows``) from the live ingest machinery by
patching ``_run_one_with_credentials`` to return a controlled
``ConnectorRunOutcome`` and patching ``GoogleSourceNormalizer`` /
``get_month_close_status`` at orchestrator module scope. They pin the gate
matrix the spec locks:

* dry-run -> normalize NOT invoked (no committed source rows);
* FAILED run -> NOT invoked (no new committed rows worth projecting);
* SUCCEEDED / PARTIAL -> invoked exactly once;
* LOCKED month (prefilter) -> skipped, run status unchanged, no exception;
* ``RevenueFactLockedMonthError`` raised mid-flight -> caught + rollback,
  run NOT failed;
* a non-lock normalize error -> rolled back and re-raised (fail-loud);
* actor falls back to the connector service principal when
  ``triggered_by_user_id`` is None, else uses ``str(triggered_by_user_id)``.

The end-to-end proof that real source rows actually become facts lives in
``test_ingestion_gate.py``; this module proves the surrounding control flow.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from ums_smart_revenue.connectors.runs import orchestrator
from ums_smart_revenue.connectors.runs.orchestrator import (
    ConnectorRunOutcome,
    run_one,
)
from ums_smart_revenue.connectors.runs.repository import ConnectorRunEntry
from ums_smart_revenue.finance.revenue_facts import RevenueFactLockedMonthError

TENANT_ID = UUID("00000000-0000-0000-0000-0000008a0001")
TRIGGERED_BY = UUID("00000000-0000-0000-0000-0000008a0099")
SERVICE_ACTOR_ID = "ddddeeee-ffff-0000-1111-2222008a0000"
REPORT_MONTH = "2026-04"
CONNECTOR_KEY = "youtube-reporting"
ACCOUNT_ID = "cms-1"


def _run_entry(*, status: str) -> ConnectorRunEntry:
    """Build a finished ``ConnectorRunEntry`` stub with the given terminal status."""
    from datetime import UTC, datetime

    return ConnectorRunEntry(
        id=str(uuid4()),
        tenant_id=str(TENANT_ID),
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
        report_month=REPORT_MONTH,
        triggered_by_user_id=None,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        status=status,
        counts={},
        error_summary=None,
    )


def _outcome(
    *,
    run: ConnectorRunEntry | None,
    reports_succeeded: int = 1,
) -> ConnectorRunOutcome:
    """Wrap a run stub in an immutable ``ConnectorRunOutcome``.

    ``reports_succeeded`` defaults to 1 so the existing success-path tests do
    not have to repeat the count plumbing; the FAILED-no-committed-success
    test passes 0 to prove the new gate skips such runs.
    """
    counts = {
        "reports_attempted": max(reports_succeeded, 1),
        "reports_succeeded": reports_succeeded,
        "reports_failed": 0,
        "rows_upserted_total": 0,
        "rows_upserted_created": 0,
        "rows_upserted_updated": 0,
        "rows_upserted_unchanged": 0,
    }
    return ConnectorRunOutcome(run=run, counts=counts, per_report_failures=[])


def _invoke_run_one(
    *,
    outcome: ConnectorRunOutcome,
    dry_run: bool = False,
    triggered_by_user_id: UUID | None = TRIGGERED_BY,
    month_close_status: str | None = "OPEN",
    normalizer: MagicMock | None = None,
) -> tuple[ConnectorRunOutcome, MagicMock, MagicMock]:
    """Drive ``run_one`` with the live path and credential resolution patched out.

    Returns ``(returned_outcome, normalizer_cls_mock, session_mock)`` so callers
    can assert on normalize invocation, the actor argument, and rollback/commit.
    """
    session = MagicMock(name="session")
    normalizer_cls = MagicMock(name="GoogleSourceNormalizer")
    if normalizer is not None:
        normalizer_cls.return_value = normalizer
    with patch.object(
        orchestrator, "_run_one_with_credentials", return_value=outcome
    ), patch.object(
        orchestrator, "_credentials_for_run", return_value=MagicMock()
    ), patch.object(
        orchestrator, "validate_report_month", return_value=None
    ), patch.object(
        orchestrator, "get_month_close_status", return_value=month_close_status
    ), patch.object(
        orchestrator, "GoogleSourceNormalizer", normalizer_cls
    ):
        returned = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month=REPORT_MONTH,
            dry_run=dry_run,
            triggered_by_user_id=triggered_by_user_id,
        )
    return returned, normalizer_cls, session


def test_dry_run_does_not_invoke_normalize() -> None:
    """A dry-run produces no committed rows, so normalize must not fire."""
    returned, normalizer_cls, session = _invoke_run_one(
        outcome=_outcome(run=None), dry_run=True
    )
    normalizer_cls.assert_not_called()
    session.commit.assert_not_called()
    assert returned is not None


def test_failed_run_with_no_committed_successes_does_not_invoke_normalize() -> None:
    """A FAILED run that committed no successful reports has no rows to project.

    The gate is keyed off ``counts["reports_succeeded"]`` (not the terminal
    status) because ``_process_live_reports`` commits each successful report
    before continuing. A FAILED run with zero committed successes has nothing
    to project, so normalize must not fire -- a real-data failure during
    processing must not silently produce a partial facts projection.
    """
    _, normalizer_cls, session = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status="FAILED"), reports_succeeded=0)
    )
    normalizer_cls.assert_not_called()
    session.commit.assert_not_called()


def test_failed_run_with_committed_successes_invokes_normalize() -> None:
    """A FAILED run whose processing already committed successes must still project.

    The post-run normalize gate is keyed off ``counts["reports_succeeded"]``
    rather than the terminal status: a later generator-level exception after
    some reports succeeded leaves committed ``google_revenue_source_rows``
    that must be collapsed into revenue facts, otherwise the dashboard /
    allocation / exports stay stale for the (tenant, report_month) window.
    """
    normalizer = MagicMock(name="normalizer_instance")
    returned, normalizer_cls, session = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status="FAILED"), reports_succeeded=1),
        normalizer=normalizer,
    )
    normalizer_cls.assert_called_once_with(session, tenant_id=TENANT_ID)
    normalizer.normalize_month.assert_called_once()
    session.commit.assert_called_once()
    assert returned.run is not None and returned.run.status == "FAILED"


def test_run_with_no_run_entry_does_not_invoke_normalize() -> None:
    """Defensive: a live outcome with run=None must not normalize."""
    _, normalizer_cls, _ = _invoke_run_one(outcome=_outcome(run=None))
    normalizer_cls.assert_not_called()


@pytest.mark.parametrize("status", ["SUCCEEDED", "PARTIAL"])
def test_terminal_success_invokes_normalize_and_commits(status: str) -> None:
    """SUCCEEDED and PARTIAL runs both trigger a single normalize + commit."""
    normalizer = MagicMock(name="normalizer_instance")
    returned, normalizer_cls, session = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status=status)),
        normalizer=normalizer,
    )
    normalizer_cls.assert_called_once_with(session, tenant_id=TENANT_ID)
    normalizer.normalize_month.assert_called_once()
    _, kwargs = normalizer.normalize_month.call_args
    assert kwargs["month"] == REPORT_MONTH
    assert kwargs["actor_user_id"] == str(TRIGGERED_BY)
    session.commit.assert_called_once()
    assert returned.run is not None and returned.run.status == status


def test_locked_month_prefilter_skips_normalize() -> None:
    """A LOCKED month is skipped at the prefilter: no normalize, run unchanged.

    The prefilter SELECT autobegins a SQLAlchemy transaction; the locked
    branch releases it with rollback (pure read, nothing to commit) so the
    caller's session is not left in an active read transaction -- a later
    ``with session.begin()`` on the same session would otherwise fail.
    """
    returned, normalizer_cls, session = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
        month_close_status="LOCKED",
    )
    normalizer_cls.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_called_once()
    assert returned.run is not None and returned.run.status == "SUCCEEDED"


def test_locked_month_error_midflight_is_caught_and_rolled_back() -> None:
    """A lock acquired between prefilter and write is a skip, not a run failure."""
    normalizer = MagicMock(name="normalizer_instance")
    normalizer.normalize_month.side_effect = RevenueFactLockedMonthError("locked")
    returned, _, session = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
        normalizer=normalizer,
    )
    normalizer.normalize_month.assert_called_once()
    session.rollback.assert_called_once()
    session.commit.assert_not_called()
    # The run must NOT be flipped to an exception or a failed status.
    assert returned.run is not None and returned.run.status == "SUCCEEDED"


def test_non_lock_normalize_error_rolls_back_and_reraises() -> None:
    """A real data error (e.g. unknown channel) fails loud after rollback."""
    normalizer = MagicMock(name="normalizer_instance")
    normalizer.normalize_month.side_effect = ValueError("unknown channel")
    with pytest.raises(ValueError, match="unknown channel"):
        _invoke_run_one(
            outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
            normalizer=normalizer,
        )


def test_actor_falls_back_to_service_principal_when_no_triggering_user() -> None:
    """With triggered_by_user_id=None, the connector service principal id is used."""
    normalizer = MagicMock(name="normalizer_instance")
    service_principal = MagicMock(name="service_principal")
    service_principal.user_id = SERVICE_ACTOR_ID
    with patch.object(
        orchestrator,
        "build_connector_service_principal",
        return_value=service_principal,
    ) as build_principal:
        _, _, _session = _invoke_run_one(
            outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
            triggered_by_user_id=None,
            normalizer=normalizer,
        )
    build_principal.assert_called_once_with(tenant_id=TENANT_ID)
    _, kwargs = normalizer.normalize_month.call_args
    assert kwargs["actor_user_id"] == SERVICE_ACTOR_ID
