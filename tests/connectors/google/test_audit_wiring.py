"""T36 connector audit emitters + service principal contract tests.

The emitters live in ``backend/ums_smart_revenue/connectors/google/audit.py``
and are the only path through which the B2.6 orchestrator (wired in T37)
turns connector-run lifecycle and raw-file lifecycle events into audit
log rows. Tests in this module pin three contracts:

1. The service principal carries Permission.RUN_CONNECTOR_JOBS on a
   tenant-pinned UserPrincipal with ``is_service_account=True``. Audit
   sinks see "this service identity ran a connector job inside tenant X".
2. Each emitter writes the right ``AuditEventType`` with a ``lifecycle``
   discriminator in ``details``: CONNECTOR_JOB_RUN for run lifecycle
   (STARTED|FINISHED) and REPORT_IMPORTED for raw-file lifecycle
   (DOWNLOADED|PARSED|FAILED). Dry-run skips the STARTED event entirely.
3. Emitters never serialize secret-bearing attributes that callers may
   attach to run/raw_file mocks; the details payload stays restricted to
   the fields the emitter explicitly names.

All tests use ``InMemoryAuditSink`` from the auth layer so no DB session
is required and the contract is asserted on what reaches the sink.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.config.settings import (
    GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
    GOOGLE_CONNECTOR_SERVICE_ACTOR_PLACEHOLDER_ID,
)
from ums_smart_revenue.connectors.google.audit import (
    build_connector_service_principal,
    emit_raw_file_downloaded,
    emit_raw_file_failed,
    emit_raw_file_parsed,
    emit_run_finished,
    emit_run_started,
)
from ums_smart_revenue.connectors.google.errors import (
    ConnectorServicePrincipalUnavailableError,
    GoogleConnectorError,
)

_TENANT_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
_SERVICE_ACTOR_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def configured_service_actor(monkeypatch: pytest.MonkeyPatch) -> str:
    """Ensure the service-actor env is set for tests that need a built principal."""
    monkeypatch.setenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, _SERVICE_ACTOR_ID)
    return _SERVICE_ACTOR_ID


@pytest.fixture
def service_principal(configured_service_actor: str):
    """Build a real service principal for emitter tests."""
    return build_connector_service_principal(tenant_id=_TENANT_ID)


def _make_run_mock(**overrides: object) -> MagicMock:
    """Build a connector_runs row mock with the canonical attribute set."""
    base: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": str(_TENANT_ID),
        "connector_key": "youtube-reporting",
        "account_id": "owner-acct-1",
        "report_month": "2026-05",
        "status": "RUNNING",
        "counts": {"reports_attempted": 0, "reports_succeeded": 0},
        "error_summary": None,
    }
    base.update(overrides)
    return MagicMock(**base)


def _make_raw_file_mock(**overrides: object) -> MagicMock:
    """Build a raw_report_files row mock with the canonical attribute set."""
    base: dict[str, object] = {
        "id": uuid4(),
        "source": "youtube_reporting",
        "report_type": "content_owner_estimated_revenue_a1",
        "report_month": "2026-05",
        "checksum": "abc123",
        "file_url": "local-fs://raw/youtube_reporting/2026-05/abc.csv",
    }
    base.update(overrides)
    return MagicMock(**base)


# ---------------------------------------------------------------------------
# build_connector_service_principal
# ---------------------------------------------------------------------------


def test_build_service_principal_carries_run_connector_jobs(
    configured_service_actor: str,
) -> None:
    """Built principal is a tenant-pinned service account with RUN_CONNECTOR_JOBS."""
    principal = build_connector_service_principal(tenant_id=_TENANT_ID)

    assert principal.user_id == configured_service_actor
    assert principal.is_service_account is True
    assert principal.disabled is False
    assert principal.tenant_id == str(_TENANT_ID)
    granted_permissions = {
        grant.permission for grant in principal.direct_permissions if grant.active
    }
    assert Permission.RUN_CONNECTOR_JOBS in granted_permissions


def test_build_service_principal_requires_actor_id_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env value -> the typed Bucket-A failure names the env var.

    The builder raises ``ConnectorServicePrincipalUnavailableError`` directly
    (a ``GoogleConnectorError``), so every caller — including the direct
    normalization caller that has no wrapper — gets the classified pre-start
    failure family instead of an untyped ``ValueError``.
    """
    monkeypatch.delenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, raising=False)
    with pytest.raises(ConnectorServicePrincipalUnavailableError) as excinfo:
        build_connector_service_principal(tenant_id=_TENANT_ID)
    assert isinstance(excinfo.value, GoogleConnectorError)
    assert excinfo.value.env_var == GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV
    message = str(excinfo.value)
    assert GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV in message


def test_build_service_principal_rejects_template_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The .env.example placeholder UUID is refused, never silently attributed.

    The template ships the placeholder UNCOMMENTED, so a ``cp .env.example
    .env`` deployment reaches the builder with this exact value. Accepting it
    would attribute every connector-emitted audit row to a UUID published in
    a public template; the builder must fail closed, name the placeholder,
    and raise the same typed Bucket-A family as the unset case.
    """
    monkeypatch.setenv(
        GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
        GOOGLE_CONNECTOR_SERVICE_ACTOR_PLACEHOLDER_ID,
    )
    with pytest.raises(ConnectorServicePrincipalUnavailableError) as excinfo:
        build_connector_service_principal(tenant_id=_TENANT_ID)
    assert isinstance(excinfo.value, GoogleConnectorError)
    assert excinfo.value.env_var == GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV
    message = str(excinfo.value)
    assert GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV in message
    assert GOOGLE_CONNECTOR_SERVICE_ACTOR_PLACEHOLDER_ID in message


# ---------------------------------------------------------------------------
# emit_run_started / emit_run_finished
# ---------------------------------------------------------------------------


def test_emit_run_started_uses_connector_job_run_with_started_lifecycle(
    service_principal,
) -> None:
    """Run-start emits one CONNECTOR_JOB_RUN row with lifecycle=STARTED."""
    sink = InMemoryAuditSink()
    run = _make_run_mock()

    emit_run_started(sink=sink, actor=service_principal, run=run, dry_run=False)

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.event_type == AuditEventType.CONNECTOR_JOB_RUN.value
    assert record.details["lifecycle"] == "STARTED"
    assert record.details["dry_run"] is False
    assert record.details["connector_key"] == "youtube-reporting"
    assert record.details["account_id"] == "owner-acct-1"
    assert record.details["report_month"] == "2026-05"
    assert record.details["run_id"] == str(run.id)


def test_emit_run_started_emits_zero_events_when_dry_run(service_principal) -> None:
    """Dry-run runs must not write any audit event at the STARTED edge."""
    sink = InMemoryAuditSink()
    run = _make_run_mock()

    emit_run_started(sink=sink, actor=service_principal, run=run, dry_run=True)

    assert sink.records == []


def test_emit_run_finished_uses_connector_job_run_with_finished_lifecycle(
    service_principal,
) -> None:
    """Run-finish emits one CONNECTOR_JOB_RUN row with status + counts captured."""
    sink = InMemoryAuditSink()
    run = _make_run_mock(
        status="PARTIAL",
        counts={
            "reports_attempted": 3,
            "reports_succeeded": 2,
            "reports_failed": 1,
            "rows_upserted_total": 17,
        },
        error_summary="ParserError: bad column",
    )

    emit_run_finished(sink=sink, actor=service_principal, run=run)

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.event_type == AuditEventType.CONNECTOR_JOB_RUN.value
    assert record.details["lifecycle"] == "FINISHED"
    assert record.details["status"] == "PARTIAL"
    assert record.details["counts"] == {
        "reports_attempted": 3,
        "reports_succeeded": 2,
        "reports_failed": 1,
        "rows_upserted_total": 17,
    }
    assert record.details["error_summary_present"] is True
    assert record.details["run_id"] == str(run.id)


def test_emit_run_finished_marks_error_summary_absent_when_none(
    service_principal,
) -> None:
    """A None ``error_summary`` is reported as ``error_summary_present=False``."""
    sink = InMemoryAuditSink()
    run = _make_run_mock(status="SUCCEEDED", error_summary=None)

    emit_run_finished(sink=sink, actor=service_principal, run=run)

    assert sink.records[0].details["error_summary_present"] is False


# ---------------------------------------------------------------------------
# emit_raw_file_downloaded / parsed / failed
# ---------------------------------------------------------------------------


def test_emit_raw_file_downloaded_uses_report_imported_with_downloaded_lifecycle(
    service_principal,
) -> None:
    """Raw-file download emits one REPORT_IMPORTED row scoped to the connector."""
    sink = InMemoryAuditSink()
    run = _make_run_mock()
    raw_file = _make_raw_file_mock()

    emit_raw_file_downloaded(
        sink=sink,
        actor=service_principal,
        run=run,
        raw_file=raw_file,
    )

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.event_type == AuditEventType.REPORT_IMPORTED.value
    assert record.details["lifecycle"] == "DOWNLOADED"
    assert record.details["source"] == "youtube_reporting"
    assert record.details["report_type"] == "content_owner_estimated_revenue_a1"
    assert record.details["report_month"] == "2026-05"
    assert record.details["checksum"] == "abc123"
    assert record.details["storage_uri"] == raw_file.file_url
    assert record.details["raw_file_id"] == str(raw_file.id)
    assert record.details["run_id"] == str(run.id)


def test_emit_raw_file_parsed_uses_report_imported_with_parsed_lifecycle(
    service_principal,
) -> None:
    """Raw-file parse emits one REPORT_IMPORTED row carrying the upsert count."""
    sink = InMemoryAuditSink()
    run = _make_run_mock()
    raw_file = _make_raw_file_mock()

    emit_raw_file_parsed(
        sink=sink,
        actor=service_principal,
        run=run,
        raw_file=raw_file,
        count_upserted=42,
    )

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.event_type == AuditEventType.REPORT_IMPORTED.value
    assert record.details["lifecycle"] == "PARSED"
    assert record.details["count_upserted"] == 42
    assert record.details["raw_file_id"] == str(raw_file.id)


def test_emit_raw_file_failed_uses_report_imported_with_failed_lifecycle(
    service_principal,
) -> None:
    """Raw-file failure emits one REPORT_IMPORTED row carrying the error class."""
    sink = InMemoryAuditSink()
    run = _make_run_mock()
    raw_file = _make_raw_file_mock()

    emit_raw_file_failed(
        sink=sink,
        actor=service_principal,
        run=run,
        raw_file=raw_file,
        error_class="ParserError",
    )

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.event_type == AuditEventType.REPORT_IMPORTED.value
    assert record.details["lifecycle"] == "FAILED"
    assert record.details["error_class"] == "ParserError"
    assert record.details["raw_file_id"] == str(raw_file.id)


# ---------------------------------------------------------------------------
# Secret-leak guard
# ---------------------------------------------------------------------------


def test_emitters_do_not_leak_secret_material(service_principal) -> None:
    """No emitter copies arbitrary mock attributes into the recorded details.

    Future drift where someone adds a credentials field to RawReportFileORM
    or ConnectorRunORM must not silently propagate to the audit log via a
    generic ``__dict__``-style copy. The emitters are required to name the
    fields they read, so an unrelated attribute on the source object never
    reaches the sink.
    """
    sink = InMemoryAuditSink()
    run = _make_run_mock(credentials_secret="DO-NOT-LEAK-RUN")
    raw_file = _make_raw_file_mock(credentials_secret="DO-NOT-LEAK-RAW")

    emit_run_started(sink=sink, actor=service_principal, run=run, dry_run=False)
    emit_run_finished(sink=sink, actor=service_principal, run=run)
    emit_raw_file_downloaded(sink=sink, actor=service_principal, run=run, raw_file=raw_file)
    emit_raw_file_parsed(
        sink=sink,
        actor=service_principal,
        run=run,
        raw_file=raw_file,
        count_upserted=1,
    )
    emit_raw_file_failed(
        sink=sink,
        actor=service_principal,
        run=run,
        raw_file=raw_file,
        error_class="ParserError",
    )

    assert sink.records, "expected at least one record from the emitters"
    for record in sink.records:
        serialized = repr(record.details)
        assert "DO-NOT-LEAK-RUN" not in serialized
        assert "DO-NOT-LEAK-RAW" not in serialized
