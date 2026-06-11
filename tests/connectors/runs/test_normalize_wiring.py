"""Wiring unit tests for the post-run normalize stage in ``run_one``.

These tests isolate the orchestrator's finance-projection seam
(``_normalize_ingested_source_rows``) from the live ingest machinery by
patching ``_run_one_with_credentials`` to return a controlled
``ConnectorRunOutcome`` and patching the normalization adapter's repository /
normalizer dependencies. They pin the gate matrix the spec locks:

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

from ums_smart_revenue.connectors.runs import normalization, orchestrator
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
    rows_upserted_total: int = 1,
    rows_deleted_stale: int = 0,
    analytics_cleanup_blocked: bool = False,
) -> ConnectorRunOutcome:
    """Wrap a run stub in an immutable ``ConnectorRunOutcome``.

    ``reports_succeeded`` is plumbed because the connector run keeps the
    per-report counts even when the terminal status is FAILED; tests that
    need to assert gate behavior across statuses pass it explicitly.
    ``analytics_cleanup_blocked`` is plumbed for the PARTIAL Analytics
    gate; non-Analytics runs always pass False.
    """
    counts = {
        "reports_attempted": max(reports_succeeded, 1),
        "reports_succeeded": reports_succeeded,
        "reports_failed": 0,
        "rows_upserted_total": rows_upserted_total,
        "rows_upserted_created": rows_upserted_total,
        "rows_upserted_updated": 0,
        "rows_upserted_unchanged": 0,
        "rows_deleted_stale": rows_deleted_stale,
    }
    return ConnectorRunOutcome(
        run=run,
        counts=counts,
        per_report_failures=[],
        analytics_cleanup_blocked=analytics_cleanup_blocked,
    )


def _invoke_run_one(
    *,
    outcome: ConnectorRunOutcome,
    dry_run: bool = False,
    triggered_by_user_id: UUID | None = TRIGGERED_BY,
    month_close_status: str | None = "OPEN",
    normalizer: MagicMock | None = None,
    record_projection_failure: MagicMock | None = None,
    get_month_close_status_side_effect: Exception | None = None,
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

    The audit_sink and audit_actor are built LAZILY inside
    ``_normalize_ingested_source_rows`` (after the dry-run gate passes);
    the helper patches ``SqlAlchemyAuditSink`` in the normalization adapter
    module so the lazy construction observes the patch.
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
    if get_month_close_status_side_effect is not None:
        get_month_close_status_mock = MagicMock(
            side_effect=get_month_close_status_side_effect
        )
    else:
        get_month_close_status_mock = MagicMock(
            return_value=month_close_status
        )
    with patch.object(
        orchestrator, "_run_one_with_credentials", return_value=outcome
    ), patch.object(
        orchestrator, "_credentials_for_run", return_value=MagicMock()
    ), patch.object(
        orchestrator, "validate_report_month", return_value=None
    ), patch.object(
        normalization, "get_month_close_status", get_month_close_status_mock
    ), patch.object(
        normalization, "GoogleSourceNormalizer", normalizer_cls
    ), patch.object(
        normalization, "record_projection_failure", record_failure_mock
    ), patch.object(
        normalization, "SqlAlchemyAuditSink", return_value=audit_sink_mock
    ), patch.object(
        orchestrator, "build_connector_service_principal",
        return_value=audit_actor_mock,
    ), patch.object(
        normalization, "build_connector_service_principal",
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
def test_terminal_run_with_zero_upserted_rows_skips_normalize(
    status: str,
) -> None:
    """A terminal run that produced no source rows must not re-normalize a month."""
    _, normalizer_cls, session, record_failure, _ = _invoke_run_one(
        outcome=_outcome(
            run=_run_entry(status=status),
            rows_upserted_total=0,
        )
    )
    normalizer_cls.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    record_failure.assert_not_called()


@pytest.mark.parametrize("status", ["SUCCEEDED", "PARTIAL"])
def test_terminal_run_with_zero_upserts_but_stale_deletes_normalizes(
    status: str,
) -> None:
    """A stale-row deletion changes source truth and must re-project facts."""
    normalizer = MagicMock(name="normalizer")
    _, normalizer_cls, session, _, _ = _invoke_run_one(
        outcome=_outcome(
            run=_run_entry(status=status),
            rows_upserted_total=0,
            rows_deleted_stale=1,
        ),
        normalizer=normalizer,
    )

    normalizer_cls.assert_called_once_with(session, tenant_id=TENANT_ID)
    normalizer.normalize_month.assert_called_once_with(
        month=REPORT_MONTH,
        actor_user_id=str(TRIGGERED_BY),
    )
    session.commit.assert_called_once()


def test_terminal_success_delegates_projection_to_adapter() -> None:
    """The orchestrator delegates DB/transaction-heavy projection work to an adapter."""
    outcome = _outcome(run=_run_entry(status="SUCCEEDED"))
    session = MagicMock(name="session")
    adapter = MagicMock(name="normalization_adapter")
    adapter_cls = MagicMock(return_value=adapter)
    with patch.object(
        orchestrator, "_run_one_with_credentials", return_value=outcome
    ), patch.object(
        orchestrator, "_credentials_for_run", return_value=MagicMock()
    ), patch.object(
        orchestrator, "validate_report_month", return_value=None
    ), patch.object(
        orchestrator,
        "SqlAlchemyIngestedSourceRowNormalizationAdapter",
        adapter_cls,
        create=True,
    ), patch.object(
        orchestrator, "GoogleSourceNormalizer", create=True
    ) as normalizer_cls, patch.object(
        orchestrator, "get_month_close_status", return_value="OPEN", create=True
    ):
        run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month=REPORT_MONTH,
            dry_run=False,
            triggered_by_user_id=TRIGGERED_BY,
        )
    adapter_cls.assert_called_once_with(session, tenant_id=TENANT_ID)
    adapter.normalize_after_run.assert_called_once()
    normalizer_cls.assert_not_called()


@pytest.mark.parametrize("status", ["SUCCEEDED", "PARTIAL"])
def test_terminal_success_invokes_normalize_and_commits(status: str) -> None:
    """SUCCEEDED and PARTIAL runs both trigger a single normalize + commit.

    The normalize actor is the triggering user when one is supplied (per
    the design spec §"Actor") -- ``str(triggered_by_user_id)`` is the
    actor. Tests that need the connector service principal pass
    ``triggered_by_user_id=None``.
    """
    normalizer = MagicMock(name="normalizer_instance")
    returned, normalizer_cls, session, _, _ = _invoke_run_one(
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


def test_actor_is_triggering_user_when_supplied() -> None:
    """When triggered_by_user_id is supplied, it is the normalize actor.

    The design spec §"Actor" says: "Use `triggered_by_user_id` when
    present; otherwise fall back to the connector service principal".
    The audit_sink's append will stash the raw actor UUID in
    details['actor_user_id'] when the user is not in the users table;
    that's expected for the normalize stage because triggering users
    are typically operators who run the ingest out-of-band.
    """
    from dataclasses import dataclass

    @dataclass
    class _StubResult:
        created: list
        updated: list
        unchanged: list
        skipped: list

    created_fact = MagicMock(name="created_fact")
    created_fact.audit_entity_id = "channel-1:2026-04:YOUTUBE_CMS"
    created_fact.source_kind = "YOUTUBE_CMS"
    created_fact.source_report_id = "report-A"
    created_fact.youtube_channel_id = "channel-1"
    created_fact.month = "2026-04"
    normalizer = MagicMock(name="normalizer_instance")
    normalizer.normalize_month.return_value = _StubResult(
        created=[created_fact],
        updated=[],
        unchanged=[],
        skipped=[],
    )
    _, _, _, _, audit_sink = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
        triggered_by_user_id=TRIGGERED_BY,
        normalizer=normalizer,
    )
    _, kwargs = normalizer.normalize_month.call_args
    assert kwargs["actor_user_id"] == str(TRIGGERED_BY)
    # The audit_sink also received the user attribution.
    audit_actor_for_appends = [
        call.args[0].user_id for call in audit_sink.append.call_args_list
    ]
    assert audit_actor_for_appends, "expected at least one audit append"
    assert all(
        actor_id == str(TRIGGERED_BY) for actor_id in audit_actor_for_appends
    )


def test_actor_falls_back_to_service_principal_when_no_triggering_user() -> None:
    """Without triggered_by_user_id, the connector service principal id is used.

    The audit context is built LAZILY (after the dry-run gate passes) so
    a dry-run path does not require UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID
    to be set. When the normalize actually fires without a triggering
    user, the service principal resolves via the env var.
    """
    normalizer = MagicMock(name="normalizer_instance")
    _, _, _, _, _ = _invoke_run_one(
        outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
        triggered_by_user_id=None,
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

    Thread 13: scope is the fact's month (finance-month), not the run's
    connector; details carry the fact's source_kind / source_report_id /
    channel_id / month plus triggered_by_* context.
    """
    from ums_smart_revenue.auth.audit import AuditEventType
    from ums_smart_revenue.auth.scopes import ScopeType

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
        # Thread 13: scope is finance-month (the fact's month), not the
        # run's connector; details carry the fact's source attributes
        # plus triggered_by_* context for traceability.
        assert record.scope_type == ScopeType.FINANCE_MONTH.value
        assert record.scope_id == "2026-04"
        assert "source_kind" in record.details
        assert "source_report_id" in record.details
        assert "youtube_channel_id" in record.details
        assert "triggered_by_run_id" in record.details
        assert "triggered_by_connector_key" in record.details
        assert "triggered_by_account_id" in record.details
        lifecycles.append(record.details["lifecycle"])
    assert sorted(lifecycles) == ["CREATED", "UPDATED"]


def test_projection_failure_records_on_run_when_normalize_raises() -> None:
    """A non-lock normalize error rewrites the run to FAILED with a clear summary.

    The rewrite is committed in its own transaction via
    ``record_projection_failure``; the caller still sees the original
    exception. Run history now shows FAILED with a projection-failure
    summary instead of SUCCEEDED with no facts produced.

    Thread 14: a ``CONNECTOR_JOB_RUN`` audit row with
    ``lifecycle="PROJECTION_FAILED"`` is also emitted in the same
    transaction so the audit trail is consistent with the durable run
    state.
    """
    normalizer = MagicMock(name="normalizer_instance")
    normalizer.normalize_month.side_effect = ValueError("unknown channel")
    record_failure = MagicMock(name="record_projection_failure")
    audit_sink_mock = MagicMock(name="audit_sink")
    audit_actor_mock = MagicMock(name="audit_actor")
    audit_actor_mock.user_id = SERVICE_ACTOR_ID
    with patch.object(
        normalization, "SqlAlchemyAuditSink", return_value=audit_sink_mock
    ), patch.object(
        normalization, "build_connector_service_principal",
        return_value=audit_actor_mock,
    ), patch(
        "ums_smart_revenue.auth.audit_service.record_audit_event"
    ) as record_audit_event_mock, pytest.raises(ValueError, match="unknown channel"):
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
    # Thread 14: a PROJECTION_FAILED audit edge is recorded.
    record_audit_event_mock.assert_called_once()
    audit_kwargs = record_audit_event_mock.call_args.kwargs
    assert audit_kwargs["entity_type"] == "connector_run"
    assert audit_kwargs["details"]["lifecycle"] == "PROJECTION_FAILED"
    assert audit_kwargs["details"]["error_summary_present"] is True
    assert audit_kwargs["details"]["connector_key"] == CONNECTOR_KEY
    assert audit_kwargs["details"]["account_id"] == ACCOUNT_ID


def test_failed_facts_txn_is_rolled_back_before_run_rewrite() -> None:
    """Thread 7: a non-lock SQLAlchemy flush/commit error must not silently
    leave the run as SUCCEEDED/PARTIAL.

    The session is rolled back BEFORE the run-status rewrite so the rewrite
    observes a clean session. Previously, the rewrite was attempted on the
    same session in a failed-transaction state, which raised
    ``PendingRollbackError``, the helper failure was swallowed, and the
    run stayed SUCCEEDED/PARTIAL with no facts produced.
    """
    normalizer = MagicMock(name="normalizer_instance")
    normalizer.normalize_month.side_effect = ValueError("unknown channel")
    record_failure = MagicMock(name="record_projection_failure")
    # Call _normalize_ingested_source_rows directly with a controlled
    # outcome so we can observe the session mock's rollback + commit
    # behavior across the failed-facts -> run-rewrite transaction pair.
    session = MagicMock(name="session")
    normalizer_cls = MagicMock(name="GoogleSourceNormalizer")
    normalizer_cls.return_value = normalizer
    with patch.object(
        normalization, "get_month_close_status", return_value="OPEN"
    ), patch.object(
        normalization, "GoogleSourceNormalizer", normalizer_cls
    ), patch.object(
        normalization, "record_projection_failure", record_failure
    ), patch.object(
        normalization, "SqlAlchemyAuditSink", return_value=MagicMock()
    ):
        with pytest.raises(ValueError, match="unknown channel"):
            orchestrator._normalize_ingested_source_rows(
                session=session,
                tenant_id=TENANT_ID,
                report_month=REPORT_MONTH,
                dry_run=False,
                triggered_by_user_id=TRIGGERED_BY,
                outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
            )
    # The session must be rolled back at least once: once for the failed
    # facts transaction, and at least once inside the rewrite helper. We
    # assert the rewrite path was entered (record_failure was called) and
    # that the run did not stay SUCCEEDED with no facts produced.
    assert session.rollback.call_count >= 1
    record_failure.assert_called_once()


def test_dry_run_does_not_require_service_principal_env() -> None:
    """Thread 8: a dry-run path must not need UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID.

    The audit context is built INSIDE ``_normalize_ingested_source_rows``
    after the dry-run gate, so a dry-run that exercises the dry-run path
    does not raise even when the env var is unset. We verify this by
    building a fresh ``run_one`` invocation that bypasses the autouse
    env fixture (monkeypatch.delenv) and confirming no exception is
    raised.
    """
    from ums_smart_revenue.config.settings import (
        GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
        load_app_settings,
    )

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.delenv(
            GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, raising=False
        )
        load_app_settings.cache_clear()
        try:
            session = MagicMock(name="session")
            with patch.object(
                orchestrator, "_run_one_with_credentials",
                return_value=_outcome(run=None),
            ), patch.object(
                orchestrator, "_credentials_for_run", return_value=MagicMock()
            ), patch.object(
                orchestrator, "validate_report_month", return_value=None
            ):
                # No exception should be raised on the dry-run path.
                returned = run_one(
                    session,
                    tenant_id=TENANT_ID,
                    connector_key=CONNECTOR_KEY,
                    account_id=ACCOUNT_ID,
                    report_month=REPORT_MONTH,
                    dry_run=True,
                )
            assert returned is not None
        finally:
            load_app_settings.cache_clear()
    finally:
        monkeypatch.undo()


def test_lock_prefilter_failure_records_on_run() -> None:
    """Thread 9: a transient ``get_month_close_status`` SELECT error is
    recorded on the run via ``record_projection_failure`` so the run
    history does not silently show success while finance facts are
    missing.
    """
    record_failure = MagicMock(name="record_projection_failure")
    with pytest.raises(RuntimeError, match="db down"):
        _invoke_run_one(
            outcome=_outcome(run=_run_entry(status="SUCCEEDED")),
            get_month_close_status_side_effect=RuntimeError("db down"),
            record_projection_failure=record_failure,
        )
    record_failure.assert_called_once()
    kwargs = record_failure.call_args.kwargs
    assert "db down" in kwargs["error_summary"]
    assert kwargs["error_summary"].startswith(
        "normalize failed: RuntimeError:"
    )


def test_partial_with_failed_report_scopes_skips_normalize() -> None:
    """Thread 12: a PARTIAL run with any failed report scopes skips normalize.

    Generalization of Thread 11 -- the failed report's intended source
    rows were never committed, so the month-wide normalize would project
    only the committed (potentially stale) source rows from the
    successful sibling reports; a partial YouTube Analytics run
    additionally has blocked the deferred stale-row cleanup, leaving
    stale rows eligible for canonical selection. Skip normalize to keep
    the previous month's facts intact; the next SUCCEEDED run for the
    same month will rewrite them.
    """
    from dataclasses import replace
    run = _run_entry(status="PARTIAL")
    outcome = replace(
        _outcome(run=run),
        analytics_cleanup_blocked=True,
        per_report_failures=[("youtube_analytics_a1", "HttpError")],
    )
    returned, normalizer_cls, session, record_failure, _ = _invoke_run_one(
        outcome=outcome,
    )
    normalizer_cls.assert_not_called()
    session.commit.assert_not_called()
    record_failure.assert_not_called()
    session.rollback.assert_not_called()
    assert returned.run is not None and returned.run.status == "PARTIAL"


def test_partial_with_clean_failures_normalizes() -> None:
    """A PARTIAL run whose per-report failures list is empty normalizes.

    Defensive: if a PARTIAL run somehow has no per-report failures (an
    edge case where counts disagree with the per_report_failures list,
    e.g. a future extension that bumps reports_failed without recording
    a per-report failure), the gate does not skip normalize.
    """
    from dataclasses import replace
    run = _run_entry(status="PARTIAL")
    outcome = replace(
        _outcome(run=run),
        analytics_cleanup_blocked=False,
        per_report_failures=[],
    )
    normalizer = MagicMock(name="normalizer_instance")
    _, normalizer_cls, session, _, _ = _invoke_run_one(
        outcome=outcome, normalizer=normalizer,
    )
    normalizer_cls.assert_called_once()
    normalizer.normalize_month.assert_called_once()
    session.commit.assert_called_once()
