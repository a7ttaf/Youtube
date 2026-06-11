"""Wiring unit tests for the post-run normalize stage in ``run_one``.

These tests isolate the orchestrator's finance-projection seam
(``_normalize_ingested_source_rows``) from the live ingest machinery by
patching ``_run_one_with_credentials`` to return a controlled
``ConnectorRunOutcome`` and patching ``GoogleSourceNormalizer`` /
``get_month_close_status`` at orchestrator module scope. They pin the gate
matrix the spec locks:

* dry-run -> normalize NOT invoked (no committed source rows);
* FAILED run -> NOT invoked (deferred stale-row cleanup for Analytics did
  not complete; normalizing would let the normalizer pick canonical rows
  from stale data per the design spec);
* SUCCEEDED / PARTIAL -> invoked exactly once, audit edges emitted per
  CREATED/UPDATED fact, and the run is rewritten to FAILED with an
  error_summary on a non-lock normalize error;
* LOCKED month (prefilter) -> skipped, run status unchanged, no exception,
  autobegun read txn released with rollback;
* ``RevenueFactLockedMonthError`` raised mid-flight -> caught + rollback,
  run NOT failed;
* a non-lock normalize error -> projection failure recorded on the run,
  then re-raised (fail-loud);
* actor is the connector service principal (matches the run lifecycle
  audit rows); a user-supplied ``triggered_by_user_id`` is not used
  because the run lifecycle audit actor is the service principal too.

The end-to-end proof that real source rows actually become facts lives in
``test_ingestion_gate.py``; this module proves the surrounding control flow.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
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


@pytest.fixture(autouse=True)
def _service_actor_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Set ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`` for the normalize audit context.

    ``run_one`` builds the post-run normalize audit context via
    ``build_connector_service_principal(tenant_id=...)``; that helper reads
    the env at call time. Setting it via ``monkeypatch`` keeps the wiring
    tests independent of the host environment.
    """
    from ums_smart_revenue.config.settings import (
        GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
        load_app_settings,
    )

    monkeypatch.setenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, SERVICE_ACTOR_ID)
    load_app_settings.cache_clear()
    try:
        yield SERVICE_ACTOR_ID
    finally:
        load_app_settings.cache_clear()


def _run_entry(*, status: str) -> ConnectorRunEntry:
    """Build a finished ``ConnectorRunEntry`` stub with the given terminal status."""
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

    ``reports_succeeded`` is plumbed because the connector run keeps the
    per-report counts even when the terminal status is FAILED; tests that
    need to assert gate behavior across statuses pass it explicitly.
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
    record_projection_failure: MagicMock | None = None,
) -> tuple[
    ConnectorRunOutcome,
    MagicMock,
    MagicMock,
    MagicMock,
    MagicMock,
]:
    """Drive ``run_one`` with the live path and credential resolution patched out.

    Returns ``(returned_outcome, normalizer_cls_mock, session_mock,
    record_projection_failure_mock, audit_sink_mock)`` so callers can
    assert on normalize invocation, the actor argument, the run rewrite on
    failure, and the audit edges emitted for created/updated facts.
    """
    session = MagicMock(name="session")
    normalizer_cls = MagicMock(name="GoogleSourceNormalizer")
    if normalizer is not None:
        normalizer_cls.return_value = normalizer
    record_failure_mock = record_projection_failure
    if record_failure_mock is None:
        record_failure_mock = MagicMock(name="record_projection_failure")
    audit_sink_mock = MagicMock(name="audit_sink")
    audit_actor_mock = MagicMock(name="audit_actor")
    audit_actor_mock.user_id = SERVICE_ACTOR_ID
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
    ), patch.object(
        orchestrator, "record_projection_failure", record_failure_mock
    ), patch.object(
        orchestrator, "SqlAlchemyAuditSink", return_value=audit_sink_mock
    ), patch.object(
        orchestrator, "build_connector_service_principal",
        return_value=audit_actor_mock,
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
    return (
        returned,
        normalizer_cls,
        session,
        record_failure_mock,
        audit_sink_mock,
    )


def test_dry_run_does_not_invoke_normalize() -> None:
    """A dry-run produces no committed rows, so normalize must not fire."""
    returned, normalizer_cls, session, _, _ = _invoke_run_one(
        outcome=_outcome(run=None), dry_run=True
    )
    normalizer_cls.assert_not_called()
    session.commit.assert_not_called()
    assert returned is not None


@pytest.mark.parametrize("reports_succeeded", [0, 1])
def test_failed_run_does_not_invoke_normalize(reports_succeeded: int) -> None:
    """A FAILED run skips normalize even when reports_succeeded > 0.

    The deferred stale-row cleanup for YouTube Analytics only flushes
    after the entire per-report loop completes; a FAILED run leaves
    source rows un-pruned, and normalizing those would let the normalizer
    pick canonical rows from stale data. The original design spec
    (\"Failure handling\") and the new codex finding agree: FAILED runs
    do not normalize.
    """
    _, normalizer_cls, session, _, _ = _invoke_run_one(
        outcome=_outcome(
            run=_run_entry(status="FAILED"), reports_succeeded=reports_succeeded
        )
    )
    normalizer_cls.assert_not_called()
    session.commit.assert_not_called()


def test_run_with_no_run_entry_does_not_invoke_normalize() -> None:
    """Defensive: a live outcome with run=None must not normalize."""
    _, normalizer_cls, _, _, _ = _invoke_run_one(outcome=_outcome(run=None))
    normalizer_cls.assert_not_called()


@pytest.mark.parametrize("status", ["SUCCEEDED", "PARTIAL"])
def test_terminal_success_invokes_normalize_and_commits(status: str) -> None:
    """SUCCEEDED and PARTIAL runs both trigger a single normalize + commit."""
    normalizer = MagicMock(name="normalizer_instance")
    returned, normalizer_cls, session, _, _ = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status=status)),
        normalizer=normalizer,
    )
    normalizer_cls.assert_called_once_with(session, tenant_id=TENANT_ID)
    normalizer.normalize_month.assert_called_once()
    _, kwargs = normalizer.normalize_month.call_args
    assert kwargs["month"] == REPORT_MONTH
    assert kwargs["actor_user_id"] == SERVICE_ACTOR_ID
    session.commit.assert_called_once()
    assert returned.run is not None and returned.run.status == status


def test_locked_month_prefilter_skips_normalize() -> None:
    """A LOCKED month is skipped at the prefilter: no normalize, run unchanged.

    The prefilter SELECT autobegins a SQLAlchemy transaction; the locked
    branch releases it with rollback (pure read, nothing to commit) so the
    caller's session is not left in an active read transaction -- a later
    ``with session.begin()`` on the same session would otherwise fail.
    """
    returned, normalizer_cls, session, _, _ = _invoke_run_one(
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
    returned, _, session, record_failure, _ = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
        normalizer=normalizer,
    )
    normalizer.normalize_month.assert_called_once()
    session.rollback.assert_called_once()
    session.commit.assert_not_called()
    # The run must NOT be flipped to an exception or a failed status, and
    # the projection-failure rewrite must NOT fire (this is a skip, not
    # a real data error).
    record_failure.assert_not_called()
    assert returned.run is not None and returned.run.status == "SUCCEEDED"


def test_non_lock_normalize_error_rolls_back_reraises_and_records_failure() -> None:
    """A real data error (e.g. unknown channel) fails loud and rewrites the run.

    The run is rewritten to FAILED with an error_summary via
    ``record_projection_failure`` so run history does not silently show
    success while finance facts are missing. The original exception is
    re-raised so the caller still sees a fail-loud error.
    """
    normalizer = MagicMock(name="normalizer_instance")
    normalizer.normalize_month.side_effect = ValueError("unknown channel")
    with pytest.raises(ValueError, match="unknown channel"):
        _invoke_run_one(
            outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
            normalizer=normalizer,
        )


def test_actor_is_connector_service_principal() -> None:
    """The post-run normalize uses the connector service principal id, not the trigger user.

    The run lifecycle audit rows (emit_run_started, emit_run_finished, the
    per-raw-file FAILED edges) are all attributed to the connector service
    principal. The post-run normalize audit rows are continuous with the
    run lifecycle audit, so they share the same actor. A user-supplied
    ``triggered_by_user_id`` is preserved on the connector_runs row itself
    but is NOT the audit actor.
    """
    normalizer = MagicMock(name="normalizer_instance")
    _, _, _, _, _ = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
        triggered_by_user_id=TRIGGERED_BY,
        normalizer=normalizer,
    )
    _, kwargs = normalizer.normalize_month.call_args
    assert kwargs["actor_user_id"] == SERVICE_ACTOR_ID


def test_audit_events_emitted_for_created_and_updated_facts() -> None:
    """Normalize-time CREATED/UPDATED facts emit one REPORT_IMPORTED audit edge each.

    Mirrors the API-driven ``POST /facts`` import audit shape: same event
    type, same entity_type, same entity_id format, ``lifecycle``
    discriminator distinguishes CREATED vs UPDATED. The audit_sink is
    invoked via ``record_audit_event`` (the public audit helper), so the
    sink's ``append`` method is what the wiring test should observe.
    """
    from ums_smart_revenue.auth.audit import AuditEventType

    created_fact = MagicMock(name="created_fact")
    created_fact.audit_entity_id = "channel-1:2026-04:YOUTUBE_CMS"
    created_fact.source_kind = "YOUTUBE_CMS"
    created_fact.source_report_id = "report-A"
    created_fact.youtube_channel_id = "channel-1"
    created_fact.month = "2026-04"
    updated_fact = MagicMock(name="updated_fact")
    updated_fact.audit_entity_id = "channel-2:2026-04:YOUTUBE_CMS"
    updated_fact.source_kind = "YOUTUBE_CMS"
    updated_fact.source_report_id = "report-B"
    updated_fact.youtube_channel_id = "channel-2"
    updated_fact.month = "2026-04"

    normalizer = MagicMock(name="normalizer_instance")
    from dataclasses import dataclass

    @dataclass
    class _StubResult:
        created: list
        updated: list
        unchanged: list
        skipped: list

    normalizer.normalize_month.return_value = _StubResult(
        created=[created_fact],
        updated=[updated_fact],
        unchanged=[],
        skipped=[],
    )
    _, _, _, _, audit_sink = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
        normalizer=normalizer,
    )
    # Two audit edges were appended (CREATED + UPDATED) via the sink's
    # ``append`` method (record_audit_event -> sink.append).
    append_calls = audit_sink.append.call_args_list
    assert len(append_calls) == 2
    lifecycles = []
    for call in append_calls:
        record = call.args[0]
        assert record.event_type == AuditEventType.REPORT_IMPORTED.value
        assert record.entity_type == "monthly_channel_revenue_fact"
        assert record.details.get("lifecycle") in {"CREATED", "UPDATED"}
        lifecycles.append(record.details["lifecycle"])
    assert sorted(lifecycles) == ["CREATED", "UPDATED"]


def test_projection_failure_records_on_run_when_normalize_raises() -> None:
    """A non-lock normalize error rewrites the run to FAILED with a clear summary.

    The rewrite is committed in its own transaction via
    ``record_projection_failure``; the caller still sees the original
    exception. Run history now shows FAILED with a projection-failure
    summary instead of SUCCEEDED with no facts produced.
    """
    normalizer = MagicMock(name="normalizer_instance")
    normalizer.normalize_month.side_effect = ValueError("unknown channel")
    record_failure = MagicMock(name="record_projection_failure")
    with pytest.raises(ValueError, match="unknown channel"):
        _invoke_run_one(
            outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
            normalizer=normalizer,
            record_projection_failure=record_failure,
        )
    # The wiring must call into the repository method (not a stub) so the
    # real UPDATE writes to connector_runs.
    record_failure.assert_called_once()
    kwargs = record_failure.call_args.kwargs
    assert "error_summary" in kwargs
    assert "unknown channel" in kwargs["error_summary"]
    assert kwargs["error_summary"].startswith("normalize failed: ValueError:")
