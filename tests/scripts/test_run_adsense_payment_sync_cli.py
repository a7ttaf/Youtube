"""
Test suite for the run_adsense_payment_sync CLI script.

Contains helper functions and pytest tests for various CLI behaviors,
including live run, dry run, error handling, and argument validation.
"""
import importlib.util
from pathlib import Path

import pytest

from ums_smart_revenue.connectors.google.adsense_payment_mapping import (
    AdSensePaymentMappingError,
)
from ums_smart_revenue.connectors.google.errors import GoogleConnectorError
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentLockedMonthError,
    AdSensePaymentValidationError,
)

_CLI_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "run_adsense_payment_sync.py"
)
TENANT = "00000000-0000-0000-0000-000000031001"
BASE_ARGV = ["--tenant", TENANT, "--account", "pub-1", "--reason", "r"]


def _load_cli():
    """
    Load the run_adsense_payment_sync script as a module for testing.

    Uses importlib to load the script by file path and returns the module object.
    """
    # Load the standalone script by path (scripts/ is not an importable
    # package). exec_module runs its sys.path bootstrap + imports once per test
    # for clean monkeypatch isolation.
    spec = importlib.util.spec_from_file_location("run_adsense_payment_sync", _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSettings:
    def __init__(self, database_url="sqlite+pysqlite:///:memory:"):
        self.database_url = database_url


class _SpySession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        """
        Record a commit operation by incrementing the commit counter.

        Called to simulate database session commits in tests.
        """
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False  # never suppress; untyped errors propagate


class _FakeResult:
    synced_count = 1
    skipped_balance_count = 0
    skipped_locked_count = 0
    months = ["2026-04"]


class _FakeServiceOK:
    def __init__(self, session, *, audit_sink, **kwargs):
        pass

    def sync(self, **kwargs):
        """
        Perform a successful sync operation and return a fake result.

        Returns a _FakeResult instance with example counts for testing.
        """
        return _FakeResult()


def _patch_common(monkeypatch, module, *, session, service, settings=None):
    """
    Monkeypatch common dependencies in the CLI module.

    Replaces settings loader, session factory, service principal builder,
    audit sink, and sync service with test doubles.
    """
    monkeypatch.setattr(
        module,
        "load_app_settings",
        lambda: settings if settings is not None else _FakeSettings(),
    )
    monkeypatch.setattr(module, "build_session_factory", lambda _url: (lambda: session))
    monkeypatch.setattr(
        module, "build_connector_service_principal", lambda *, tenant_id: object()
    )
    monkeypatch.setattr(
        module, "SqlAlchemyAuditSink", lambda _session, *, tenant_id=None: object()
    )
    monkeypatch.setattr(module, "AdSensePaymentSyncService", service)


def test_cli_live_success_commits_and_returns_0(monkeypatch, capsys):
    """
    Test that live CLI invocation commits once and returns exit code 0.

    Verifies output contains synced count information in live mode.
    """
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)
    rc = module.main(BASE_ARGV)
    assert rc == 0
    assert session.commits == 1                       # live mode commits
    out = capsys.readouterr().out
    assert "SYNCED" in out
    assert "synced=1" in out


def test_cli_dry_run_does_not_commit(monkeypatch, capsys):
    """
    Test that dry-run mode does not commit and returns exit code 0.

    """
    Verifies output indicates dry-run mode.
    """
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)
    rc = module.main(BASE_ARGV + ["--dry-run"])
    assert rc == 0
    assert session.commits == 0                       # dry-run never commits
    assert "DRY-RUN" in capsys.readouterr().out


def test_cli_missing_db_config_returns_2(monkeypatch, capsys):
    """
    Test that missing database URL in settings results in exit code 2.

    Verifies error message for missing UMS_DATABASE_URL and no commits.
    """
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
    GoogleConnectorError("boom"),
    AdSensePaymentValidationError("bad batch"),
    AdSensePaymentLockedMonthError("month locked"),
    AdSensePaymentMappingError("$ ambiguous"),
])
def test_cli_typed_failure_returns_2(monkeypatch, capsys, error):
    """
    Test that typed exceptions from the sync service result in exit code 2.

    Verifies the specific error type is reported and no commits occur.
    """
    module = _load_cli()
    session = _SpySession()

    class _Raises:
        """Stub service actor that always raises the provided error during sync, for testing error handling."""
        def __init__(self, session, *, audit_sink, **kwargs):
            pass  # No initialization needed for stub service actor.

        def sync(self, **kwargs):
            raise error

    _patch_common(monkeypatch, module, session=session, service=_Raises)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert type(error).__name__ in capsys.readouterr().err
    assert session.commits == 0                       # no commit on failure


def test_cli_missing_service_actor_returns_2(monkeypatch, capsys):
    """
    Test that missing service actor ID raises ValueError and returns exit code 2.

    Verifies the ValueError is logged and no commits occur.
    """
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)

    def _no_actor(*, tenant_id):
        """
        Raise ValueError when service actor ID is missing.

        Ensures that the CLI properly handles missing actor configuration.
        """
        raise ValueError("UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID is required")

    monkeypatch.setattr(module, "build_connector_service_principal", _no_actor)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert "ValueError" in capsys.readouterr().err
    assert session.commits == 0


def test_cli_untyped_error_propagates(monkeypatch):
    """
    Test that untyped exceptions from the sync service propagate as RuntimeError.

    Verifies that CLI does not catch non-typed errors.
    """
    module = _load_cli()
    session = _SpySession()

    class _Boom:
        """Stub service actor that raises a RuntimeError during sync to test untyped error propagation."""
        def __init__(self, session, *, audit_sink, **kwargs):
            pass  # No initialization needed for stub service actor.

        def sync(self, **kwargs):
            raise RuntimeError("unexpected non-typed error")

    _patch_common(monkeypatch, module, session=session, service=_Boom)
    with pytest.raises(RuntimeError):
        module.main(BASE_ARGV)
