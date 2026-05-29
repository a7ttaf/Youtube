"""Tests for the run_deduction_ingestion CLI script."""
import importlib.util
from pathlib import Path

import pytest

from ums_smart_revenue.finance.deduction_ingestion import (
    DeductionComponentLockedMonthError,
    DeductionComponentValidationError,
)

_CLI_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "run_deduction_ingestion.py"
)
TENANT = "00000000-0000-0000-0000-00000000d001"
BASE_ARGV = ["--tenant", TENANT, "--month", "2026-04", "--reason", "r"]


def _load_cli():
    """Dynamically load the run_deduction_ingestion CLI module for testing."""
    spec = importlib.util.spec_from_file_location("run_deduction_ingestion", _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSettings:
    def __init__(self, database_url="sqlite+pysqlite:///:memory:"):
        self.database_url = database_url


class _SpySession:
    """
    Dummy session used for testing that tracks the number of commit operations.
    """
    def __init__(self):
        self.commits = 0

    def commit(self):
        """Simulate a database commit by incrementing the commit counter."""
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_result():
    """Return a dummy result object with expected ingestion attributes."""
    return type(
        "R", (), {"month": "2026-04", "total_upserted": 4,
                  "by_kind": {"TRANSFER_FEE": 1}, "skipped_non_usd": 0, "dry_run": False},
    )()


class _FakeServiceOK:
    def __init__(self, session, *, audit_sink, tenant_id=None):
        pass

    @staticmethod
    def ingest(**kwargs):
        """Perform a simulated ingestion and return a fake result."""
        return _fake_result()


def _patch_common(monkeypatch, module, *, session, service, settings=None):
    """Patch common CLI dependencies: settings, session factory, connector, and service."""
    monkeypatch.setattr(
        module, "load_app_settings",
        lambda: settings if settings is not None else _FakeSettings(),
    )
    monkeypatch.setattr(module, "build_session_factory", lambda _url: (lambda: session))
    monkeypatch.setattr(
        module, "build_connector_service_principal", lambda *, tenant_id: object()
    )
    monkeypatch.setattr(module, "SqlAlchemyAuditSink", lambda _s, *, tenant_id=None: object())
    monkeypatch.setattr(module, "DeductionIngestionService", service)


def test_cli_live_success_commits_and_returns_0(monkeypatch, capsys):
    """Test that in live mode, main commits once and returns zero."""
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)
    rc = module.main(BASE_ARGV)
    assert rc == 0
    assert session.commits == 1
    assert "INGESTED" in capsys.readouterr().out


def test_cli_dry_run_does_not_commit(monkeypatch, capsys):
    """Test that dry-run mode does not commit any transactions and returns zero."""
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)
    rc = module.main(BASE_ARGV + ["--dry-run"])
    assert rc == 0
    assert session.commits == 0
    assert "DRY-RUN" in capsys.readouterr().out


def test_cli_missing_db_config_returns_2(monkeypatch, capsys):
    """Test that missing database configuration causes exit code 2 and emits an error."""
    module = _load_cli()
    session = _SpySession()
    _patch_common(
        monkeypatch, module, session=session, service=_FakeServiceOK,
        settings=_FakeSettings(database_url=""),
    )
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert "UMS_DATABASE_URL" in capsys.readouterr().err
    assert session.commits == 0


@pytest.mark.parametrize("error", [
    DeductionComponentValidationError("bad"),
    DeductionComponentLockedMonthError("locked"),
])
def test_cli_typed_failure_returns_2(monkeypatch, capsys, error):
    """Test that typed service failures return code 2 and include the error name."""
    module = _load_cli()
    session = _SpySession()

    class _Raises:
        """Mock service class that always raises the specified error during ingestion."""

        def __init__(self, session, *, audit_sink, tenant_id=None):
            # No initialization required for this mock
            pass

        @staticmethod
        def ingest(**kwargs):
            raise error

    _patch_common(monkeypatch, module, session=session, service=_Raises)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert type(error).__name__ in capsys.readouterr().err
    assert session.commits == 0


def test_cli_malformed_settings_returns_2_before_db_session(monkeypatch, capsys):
    """Test that malformed settings errors are caught before DB setup and return code 2."""
    # Settings validation failures are operator input errors -> exit 2 before any
    # DB setup. build_session_factory must NOT run after a settings ValueError.
    module = _load_cli()

    def _bad_settings():
        """Raise ValueError to simulate malformed operator settings."""
        raise ValueError("malformed operator settings")

    def _unexpected_session_factory(_url):
        """Raise AssertionError if session factory is called unexpectedly."""
        raise AssertionError("database setup must not run after settings validation")

    monkeypatch.setattr(module, "load_app_settings", _bad_settings)
    monkeypatch.setattr(module, "build_session_factory", _unexpected_session_factory)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert "ValueError" in capsys.readouterr().err


def test_cli_missing_service_actor_returns_2(monkeypatch, capsys):
    """Test that missing service actor id raises ValueError and returns code 2 without committing."""
    # A missing/blank service-actor id raises ValueError before any write -> exit 2.
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)

    def _no_actor(*, tenant_id):
        """Raise ValueError to simulate missing service actor id."""
        raise ValueError("service actor id is required")

    monkeypatch.setattr(module, "build_connector_service_principal", _no_actor)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert "ValueError" in capsys.readouterr().err
    assert session.commits == 0


def test_cli_untyped_error_propagates(monkeypatch):
    """Test that untyped runtime errors are propagated with a traceback."""
    # Non-typed errors are NOT caught (no exit-2 swallow); they propagate with a
    # traceback, matching the AdSense sync CLI contract.
    module = _load_cli()
    session = _SpySession()

    class _Boom:
        def __init__(self, session, *, audit_sink, tenant_id=None):
            raise NotImplementedError()

        @staticmethod
        def ingest(**kwargs):
            raise RuntimeError("unexpected non-typed error")

    _patch_common(monkeypatch, module, session=session, service=_Boom)
    with pytest.raises(RuntimeError):
        module.main(BASE_ARGV)


def test_cli_bad_tenant_uuid_is_argparse_error():
    """Test that invalid tenant UUID raises argparse SystemExit error with non-zero code."""
    module = _load_cli()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--tenant", "nope", "--month", "2026-04", "--reason", "r"])
    assert excinfo.value.code != 0


def test_cli_blank_reason_is_argparse_error():
    """Test that blank reason argument raises argparse SystemExit error with non-zero code."""
    module = _load_cli()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--tenant", TENANT, "--month", "2026-04", "--reason", "  "])
    assert excinfo.value.code != 0
