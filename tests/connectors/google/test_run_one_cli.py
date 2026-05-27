"""CLI argparse + dispatch tests for scripts/run_google_connector.py (T30).

Three tests cover the operator-facing surface:

1. ``test_cli_rejects_unknown_connector`` -- argparse ``choices`` is fed by
   ``registry.known_keys()`` (T26), so an unrecognised connector key exits
   non-zero with ``invalid choice`` on stderr. Runs as a subprocess so the
   real argparse error path is exercised.
2. ``test_cli_rejects_bad_month_format`` -- the CLI enforces ``YYYY-MM``
   beyond argparse so ``2026-5`` (single-digit month) is rejected before
   any DB / network call. Subprocess for the same reason.
3. ``test_cli_main_returns_2_when_credential_missing`` -- in-process call
   to ``main([...])`` with ``load_app_settings`` and ``build_session_factory``
   patched so the CLI gets a session backed by the seeded SQLite fixture
   below. A missing credential bubbles ``CredentialNotFoundError`` from
   ``run_one``; the CLI's ``except GoogleConnectorError`` handler prints
   ``<ClassName>: <message>`` to stderr and returns exit code 2.

Spec deviation from the plan's verbatim Step 1 test:

The plan's third test invokes the CLI via subprocess with no env. That
subprocess flow would fail BEFORE ``CredentialNotFoundError`` because
``build_session_factory(None)`` (no ``UMS_DATABASE_URL``) raises
``TypeError`` in ``sqlalchemy.create_engine`` -- not a
``GoogleConnectorError`` -- so the CLI's typed-error handler never runs
and stderr would not contain ``CredentialNotFoundError``. Option C
(in-process with monkeypatch + a real test DB session) tests the same
exit-code-2 contract without making the test depend on an env var the
subprocess can't reliably set across platforms. The first two tests
remain subprocess so the argparse error paths are end-to-end real.
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.db.security_models import SecurityBase
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
    spec = importlib.util.spec_from_file_location(
        "ums_run_google_connector_cli", CLI_PATH
    )
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
            "--tenant", str(uuid4()),
            "--connector", "not-a-thing",
            "--account", "x",
            "--month", "2026-05",
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
            "--tenant", str(uuid4()),
            "--connector", "youtube-reporting",
            "--account", "x",
            "--month", "2026-5",  # wrong format: single-digit month
        ]
    )
    assert out.returncode == 2
    assert "--month" in out.stderr
    assert "YYYY-MM" in out.stderr


@pytest.fixture
def session() -> Session:
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
            "--tenant", str(TENANT_ID),
            "--connector", "youtube-reporting",
            "--account", "missing-account",
            "--month", "2026-05",
            "--dry-run",
        ]
    )

    assert exit_code == 2
    assert "CredentialNotFoundError" in captured_err.getvalue()
