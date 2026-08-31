# ============================================================================
# Purpose: CLI argparse and in-process dispatch coverage for
# scripts/run_google_connector.py, including live-run credential admission
# failures before run_one starts.
# Database/ORM: Uses disposable SQLite sessions seeded with tenant and
# credential rows; patches the CLI session factory to avoid host env coupling.
# Standards: Subprocess coverage for argparse paths, in-process coverage for
# typed Bucket-A errors, and no external Google or secret-manager calls.
# Blast Radius: Test suite only. No production runtime, schema, or finance
# logic changes live in this module.
# Connections:
#   - File: scripts/run_google_connector.py -> CLI entrypoint under test.
#   - File: backend/ums_smart_revenue/connectors/credentials.py -> live
#     credential smoke admission rule.
# ============================================================================
"""CLI argparse + dispatch tests for scripts/run_google_connector.py (T30).

Three tests cover the operator-facing surface:

1. ``test_cli_rejects_unknown_connector`` -- argparse ``choices`` is fed by
   ``registry.known_keys()`` (T26), so an unrecognised connector key exits
   non-zero with ``invalid choice`` on stderr. Runs as a subprocess so the
   real argparse error path is exercised.
2. ``test_cli_rejects_bad_month_format`` -- the CLI enforces ``YYYY-MM``
   beyond argparse so ``2026-5`` (single-digit month) is rejected before
   any DB / network call. Subprocess for the same reason.
3. ``test_cli_main_returns_2_when_database_url_missing`` -- in-process call
   to ``main([...])`` with ``load_app_settings`` patched to return no
   database URL. The CLI emits a clear operator error and returns exit code 2
   before trying to construct an engine.
4. ``test_cli_main_returns_2_when_credential_missing`` -- in-process call
   to ``main([...])`` with ``load_app_settings`` and ``build_session_factory``
   patched so the CLI gets a session backed by the seeded SQLite fixture
   below. A missing credential bubbles ``CredentialNotFoundError`` from
   ``run_one``; the CLI's ``except GoogleConnectorError`` handler prints
   ``<ClassName>: <message>`` to stderr and returns exit code 2.

The first two tests remain subprocess so the argparse error paths are
end-to-end real. The database/credential tests run in-process so they can
patch settings and session construction without relying on host env state.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import TenantLifecycleError
from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.db.security_models import ApiConnectorCredentialORM, SecurityBase
from ums_smart_revenue.db.source_models import CurrencyORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_PATH = PROJECT_ROOT / "scripts" / "run_google_connector.py"
TENANT_ID = UUID("00000000-0000-0000-0000-000000830001")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI as a subprocess so argparse exit paths are end-to-end real.

    ``cwd=PROJECT_ROOT`` so the script's own ``sys.path`` bootstrap of
    ``backend/`` resolves the same way it will for an operator running the
    command from the repository root.
    """
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        check=False,
    )


def _load_cli_module():
    """Load ``scripts/run_google_connector.py`` as a module without making
    ``scripts/`` a Python package.

    The ``scripts/`` directory is a flat collection of operator entrypoints,
    not an importable package (no ``__init__.py``). Using
    ``importlib.util.spec_from_file_location`` lets the in-process test
    drive ``main([...])`` and patch its module-level globals
    (``load_app_settings``, ``build_session_factory``) without polluting
    ``sys.path`` for the rest of the test session.
    """
    spec = importlib.util.spec_from_file_location("ums_run_google_connector_cli", CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_rejects_unknown_connector() -> None:
    """An unknown ``--connector`` value exits non-zero with argparse's
    ``invalid choice`` error mentioning the rejected token on stderr.
    """
    out = _run(
        [
            "--tenant",
            str(uuid4()),
            "--connector",
            "not-a-thing",
            "--account",
            "x",
            "--month",
            "2026-05",
        ]
    )
    assert out.returncode == 2
    assert "not-a-thing" in out.stderr or "invalid choice" in out.stderr


def test_cli_rejects_bad_month_format() -> None:
    """``--month`` must be strict ``YYYY-MM``; ``2026-5`` is rejected before
    any DB / network call. argparse delegates to ``parser.error`` which exits
    with status 2.
    """
    out = _run(
        [
            "--tenant",
            str(uuid4()),
            "--connector",
            "youtube-reporting",
            "--account",
            "x",
            "--month",
            "2026-5",  # wrong format: single-digit month
        ]
    )
    assert out.returncode == 2
    assert "--month" in out.stderr
    assert "YYYY-MM" in out.stderr


def test_cli_main_returns_2_when_database_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()

    class _StubSettings:
        database_url = None

    def _load_stub_settings() -> _StubSettings:
        return _StubSettings()

    def _build_session_factory_should_not_run(_url: str):
        raise AssertionError("build_session_factory must not run without a database URL")

    monkeypatch.setattr(module, "load_app_settings", _load_stub_settings)
    monkeypatch.setattr(module, "build_session_factory", _build_session_factory_should_not_run)

    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured_err)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            "youtube-reporting",
            "--account",
            "acct",
            "--month",
            "2026-05",
        ]
    )

    assert exit_code == 2
    assert "UMS_DATABASE_URL" in captured_err.getvalue()


@pytest.fixture
def session() -> Generator[Session]:
    """Seeded in-memory SQLite with the multi-base schema the orchestrator
    will read.

    Mirrors ``test_orchestrator.py``'s session fixture: TenantORM + USD
    currency are FK pre-requisites for the orchestrator's source-row
    repository, SecurityBase carries the ``ApiConnectorCredentialORM`` table
    the orchestrator's ``_load_credential`` reads from, and ReportBase plus
    FinanceBase round out the schema so a future test in this file can
    exercise a full happy-path run if needed.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SecurityBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    from ums_smart_revenue.db.finance_models import FinanceBase

    FinanceBase.metadata.create_all(engine)
    with Session(engine) as test_session:
        now = datetime.now(UTC)
        test_session.add_all(
            [
                TenantORM(id=TENANT_ID, slug="tenant-cli", display_name="CLI Tenant"),
                CurrencyORM(
                    code="USD",
                    numeric_code="840",
                    name="US Dollar",
                    minor_unit=2,
                    is_supported=True,
                    activated_at=now,
                ),
            ]
        )
        test_session.flush()
        yield test_session


class _SessionCtx:
    def __init__(self, db_session: Session) -> None:
        self._session = db_session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, *_exc_info: object) -> None:
        return None


def _patch_cli_runtime(module, monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    class _StubSettings:
        database_url = "sqlite+pysqlite:///:memory:"

    def _load_stub_settings() -> _StubSettings:
        return _StubSettings()

    def _fake_factory() -> _SessionCtx:
        return _SessionCtx(db_session)

    def _build_fake_session_factory(_url: str):
        return _fake_factory

    monkeypatch.setattr(module, "load_app_settings", _load_stub_settings)
    monkeypatch.setattr(module, "build_session_factory", _build_fake_session_factory)


def _seed_cli_credential(
    db_session: Session,
    *,
    account_id: str = "content-owner-1",
    status: str = "active",
    last_refresh_status: str | None = None,
    last_refresh_attempt_at: datetime | None = None,
    token_expiry_at: datetime | None = None,
) -> None:
    db_session.add(
        ApiConnectorCredentialORM(
            id=uuid4(),
            tenant_id=TENANT_ID,
            connector_key="youtube_reporting",
            account_id=account_id,
            encrypted_secret_ref="secret-manager://ums/yt/content-owner-1",
            status=status,
            last_refresh_attempt_at=last_refresh_attempt_at,
            last_refresh_status=last_refresh_status,
            last_refresh_error_class=None,
            token_expiry_at=token_expiry_at,
        )
    )
    db_session.commit()


def test_cli_main_returns_2_when_credential_missing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process: ``main([...])`` handles ``CredentialNotFoundError`` -> exit 2
    and prints ``CredentialNotFoundError: <message>`` to stderr.

    Patches ``load_app_settings`` (so no env lookup is required) and
    ``build_session_factory`` (so the CLI uses the test session). The
    orchestrator's ``_load_credential`` returns ``None`` against the empty
    ``api_connector_credentials`` table, which raises
    ``CredentialNotFoundError``. The CLI's ``except GoogleConnectorError``
    handler is the code under test.
    """
    module = _load_cli_module()

    class _SessionCtx:
        def __init__(self, db_session: Session) -> None:
            self._session = db_session

        def __enter__(self) -> Session:
            return self._session

        def __exit__(self, *_exc_info: object) -> None:
            # Test owns the session lifecycle via the ``session`` fixture's
            # ``with Session(engine) as ...:`` so the context manager here
            # is a no-op exit. Closing the real session here would break
            # the fixture's cleanup.
            return None

    def _fake_factory() -> _SessionCtx:
        return _SessionCtx(session)

    # ``load_app_settings`` is patched to return a stub with
    # ``database_url`` so the CLI's ``settings.database_url`` access works.
    # ``build_session_factory`` is patched to ignore the URL and return
    # ``_fake_factory`` itself (a zero-arg callable that yields the
    # session context manager the CLI calls via ``with factory() as ...``).
    class _StubSettings:
        database_url = "sqlite+pysqlite:///:memory:"

    def _load_stub_settings() -> _StubSettings:
        return _StubSettings()

    def _build_fake_session_factory(_url: str):
        return _fake_factory

    monkeypatch.setattr(module, "load_app_settings", _load_stub_settings)
    monkeypatch.setattr(module, "build_session_factory", _build_fake_session_factory)

    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured_err)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            "youtube-reporting",
            "--account",
            "missing-account",
            "--month",
            "2026-05",
            "--dry-run",
        ]
    )

    assert exit_code == 2
    stderr = captured_err.getvalue()
    assert "CredentialNotFoundError" in stderr
    assert "connector credential not found" in stderr
    assert "missing-account" not in stderr


def test_cli_main_returns_2_when_live_credential_smoke_missing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cli_credential(session)
    module = _load_cli_module()
    _patch_cli_runtime(module, monkeypatch, session)

    def _run_one_should_not_run(*_args, **_kwargs):
        raise AssertionError("run_one must not start before credential smoke passes")

    monkeypatch.setattr(module, "run_one", _run_one_should_not_run)
    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured_err)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            "youtube-reporting",
            "--account",
            "content-owner-1",
            "--month",
            "2026-05",
        ]
    )

    assert exit_code == 2
    assert "CredentialSmokeRequiredError" in captured_err.getvalue()
    assert "credential smoke" in captured_err.getvalue()


def test_cli_main_returns_2_when_live_credential_smoke_expired(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cli_credential(
        session,
        last_refresh_status="succeeded",
        last_refresh_attempt_at=datetime.now(UTC) - timedelta(hours=2),
        token_expiry_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    module = _load_cli_module()
    _patch_cli_runtime(module, monkeypatch, session)

    def _run_one_should_not_run(*_args, **_kwargs):
        raise AssertionError("run_one must not start after expired credential smoke")

    monkeypatch.setattr(module, "run_one", _run_one_should_not_run)
    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured_err)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            "youtube-reporting",
            "--account",
            "content-owner-1",
            "--month",
            "2026-05",
        ]
    )

    assert exit_code == 2
    assert "CredentialSmokeRequiredError" in captured_err.getvalue()
    assert "credential smoke" in captured_err.getvalue()


def test_cli_main_preserves_inactive_credential_error_before_smoke_wrapper(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cli_credential(
        session,
        status="disabled",
        last_refresh_status="succeeded",
        last_refresh_attempt_at=datetime.now(UTC),
        token_expiry_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    module = _load_cli_module()
    _patch_cli_runtime(module, monkeypatch, session)

    def _run_one_should_not_run(*_args, **_kwargs):
        raise AssertionError("run_one must not start for inactive credentials")

    monkeypatch.setattr(module, "run_one", _run_one_should_not_run)
    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured_err)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            "youtube-reporting",
            "--account",
            "content-owner-1",
            "--month",
            "2026-05",
        ]
    )

    assert exit_code == 2
    assert "InactiveCredentialError" in captured_err.getvalue()
    assert "CredentialSmokeRequiredError" not in captured_err.getvalue()


def test_cli_main_allows_live_after_successful_credential_smoke(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cli_credential(
        session,
        last_refresh_status="succeeded",
        last_refresh_attempt_at=datetime.now(UTC),
        token_expiry_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    module = _load_cli_module()
    _patch_cli_runtime(module, monkeypatch, session)
    calls: list[dict[str, object]] = []

    class _Run:
        status = "SUCCEEDED"

    class _Outcome:
        run = _Run()
        counts = {"reports_seen": 0}
        per_report_failures: list[object] = []

    def _fake_run_one(*_args, **kwargs):
        calls.append(kwargs)
        return _Outcome()

    monkeypatch.setattr(module, "run_one", _fake_run_one)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            "youtube-reporting",
            "--account",
            "content-owner-1",
            "--month",
            "2026-05",
        ]
    )

    assert exit_code == 0
    assert calls == [
        {
            "tenant_id": TENANT_ID,
            "connector_key": "youtube-reporting",
            "account_id": "content-owner-1",
            "report_month": "2026-05",
            "dry_run": False,
        }
    ]


def test_cli_main_returns_2_when_tenant_lifecycle_rejected(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kody review (PR #94 thread PRRT_kwDOSZIgN86I-yFZ) regression: a
    ``TenantLifecycleError`` raised by ``connector_tenant_context`` while
    looking up the tenant (suspended / archived / missing) MUST be caught
    by the CLI's ``except GoogleConnectorError`` handler and translated to
    exit code 2 with a typed error class name on stderr.

    Pre-fix behaviour: the ``with`` statement listed
    ``connector_tenant_context`` alongside ``session_factory``, so its
    ``__enter__`` ran OUTSIDE the try block and a lookup-time
    ``TenantLifecycleError`` propagated uncaught through ``main()``,
    surfacing as a raw Python traceback with exit 1. Post-fix: the context
    manager is nested inside the try block, so the typed error is caught
    and translated to the documented exit 2 (Bucket A: pre-start_run typed
    error, no ``connector_runs`` row created).
    """
    module = _load_cli_module()

    class _SessionCtx:
        def __init__(self, db_session: Session) -> None:
            self._session = db_session

        def __enter__(self) -> Session:
            return self._session

        def __exit__(self, *_exc_info: object) -> None:
            return None

    def _fake_factory() -> _SessionCtx:
        return _SessionCtx(session)

    class _StubSettings:
        database_url = "sqlite+pysqlite:///:memory:"

    def _load_stub_settings() -> _StubSettings:
        return _StubSettings()

    def _build_fake_session_factory(_url: str):
        return _fake_factory

    class _RaiseOnEnter:
        """Stand-in for the real helper that raises on every __enter__.

        Pins that the CLI catches the typed ``TenantLifecycleError`` raised
        during the context manager's ``__enter__`` (i.e. the lookup phase)
        regardless of whether the underlying lookup is production or
        test-grade. Pre-fix: the ``with`` statement listed
        ``connector_tenant_context`` alongside ``session_factory``, so its
        ``__enter__`` ran OUTSIDE the try block and the typed error
        propagated uncaught. Post-fix: the context manager is nested inside
        the try block, so the existing ``except GoogleConnectorError``
        translates the typed error to exit 2.
        """

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """Accept and ignore the production helper's call signature."""

        def __enter__(self) -> None:
            raise TenantLifecycleError(tenant_id=TENANT_ID, status="SUSPENDED")

        def __exit__(self, *_exc_info: object) -> None:
            return None  # pragma: no cover -- __enter__ always raises

    monkeypatch.setattr(module, "load_app_settings", _load_stub_settings)
    monkeypatch.setattr(module, "build_session_factory", _build_fake_session_factory)
    monkeypatch.setattr(module, "connector_tenant_context", _RaiseOnEnter)

    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured_err)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            "youtube-reporting",
            "--account",
            "any-account",
            "--month",
            "2026-05",
        ]
    )

    assert exit_code == 2
    stderr = captured_err.getvalue()
    assert "TenantLifecycleError" in stderr
    assert "SUSPENDED" in stderr
