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
        return _FakeResult()


def _patch_common(monkeypatch, module, *, session, service, settings=None):
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
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)
    rc = module.main(BASE_ARGV + ["--dry-run"])
    assert rc == 0
    assert session.commits == 0                       # dry-run never commits
    assert "DRY-RUN" in capsys.readouterr().out


def test_cli_missing_db_config_returns_2(monkeypatch, capsys):
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
    module = _load_cli()
    session = _SpySession()

    class _Raises:
        def __init__(self, session, *, audit_sink, **kwargs):
            pass

        def sync(self, **kwargs):
            raise error

    _patch_common(monkeypatch, module, session=session, service=_Raises)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert type(error).__name__ in capsys.readouterr().err
    assert session.commits == 0                       # no commit on failure


def test_cli_missing_service_actor_returns_2(monkeypatch, capsys):
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)

    def _no_actor(*, tenant_id):
        raise ValueError("UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID is required")

    monkeypatch.setattr(module, "build_connector_service_principal", _no_actor)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert "ValueError" in capsys.readouterr().err
    assert session.commits == 0


def test_cli_untyped_error_propagates(monkeypatch):
    module = _load_cli()
    session = _SpySession()

    class _Boom:
        def __init__(self, session, *, audit_sink, **kwargs):
            pass

        def sync(self, **kwargs):
            raise RuntimeError("unexpected non-typed error")

    _patch_common(monkeypatch, module, session=session, service=_Boom)
    with pytest.raises(RuntimeError):
        module.main(BASE_ARGV)


def test_cli_bad_tenant_uuid_is_argparse_error():
    module = _load_cli()
    # argparse type=UUID rejects before any settings/session work.
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--tenant", "not-a-uuid", "--account", "pub-1", "--reason", "r"])
    assert excinfo.value.code != 0


def test_cli_blank_reason_is_argparse_error():
    module = _load_cli()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--tenant", TENANT, "--account", "pub-1", "--reason", "   "])
    assert excinfo.value.code != 0
