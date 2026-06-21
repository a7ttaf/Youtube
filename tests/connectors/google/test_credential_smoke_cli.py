# ============================================================================
# Purpose: Exercise the Google credential smoke CLI subprocess and in-process
#   entrypoints without live Google credentials or connector runs.
# Database/ORM: Disposable SQLite with tenant + credential fixtures only.
# Standards: Keyword-only resolver stubs mirror production call shape; typed
#   helpers; redacted stderr contract asserted for OAuth failures.
# Blast Radius: Test coverage only for scripts/check_google_connector_credential.py.
# Connections:
#   - File: scripts/check_google_connector_credential.py -> CLI under test.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     ``resolve_connector_credentials`` (keyword-only).
# ============================================================================
from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ums_smart_revenue.connectors.google.errors import (
    OAuthRefreshError,
    TenantLifecycleError,
)
from ums_smart_revenue.db.connector_models import ConnectorRunORM
from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.db.security_models import ApiConnectorCredentialORM, SecurityBase
from ums_smart_revenue.db.source_models import CurrencyORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_PATH = PROJECT_ROOT / "scripts" / "check_google_connector_credential.py"
TENANT_ID = UUID("00000000-0000-0000-0000-000000830101")
ACCOUNT_ID = "content-owner-credential-smoke"
CONNECTOR_KEY = "youtube-reporting"
TOKEN_EXPIRY = datetime(2026, 6, 21, 12, 30, tzinfo=UTC)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        check=False,
    )


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ums_google_credential_smoke_cli", CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine() -> Iterator[Engine]:
    db_engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(db_engine)
    SecurityBase.metadata.create_all(db_engine)
    ReportBase.metadata.create_all(db_engine)
    FinanceBase.metadata.create_all(db_engine)
    with Session(db_engine) as session:
        session.add_all(
            [
                TenantORM(id=TENANT_ID, slug="tenant-credential-smoke", display_name="Smoke"),
                CurrencyORM(
                    code="USD",
                    numeric_code="840",
                    name="US Dollar",
                    minor_unit=2,
                    is_supported=True,
                    activated_at=TOKEN_EXPIRY,
                ),
                ApiConnectorCredentialORM(
                    id=UUID("00000000-0000-0000-0000-000000830102"),
                    tenant_id=TENANT_ID,
                    connector_key=CONNECTOR_KEY,
                    account_id=ACCOUNT_ID,
                    encrypted_secret_ref="gcp-secret-manager://projects/p/secrets/s/versions/latest",
                    status="active",
                ),
            ]
        )
        session.commit()
    try:
        yield db_engine
    finally:
        db_engine.dispose()


def _session_context_factory(db_engine: Engine) -> type:
    class _SessionCtx:
        def __enter__(self) -> Session:
            self._session = Session(db_engine)
            return self._session

        def __exit__(self, *_exc_info: object) -> None:
            self._session.close()

    return _SessionCtx


def _patch_settings_and_session(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, db_engine: Engine
) -> None:
    class _StubSettings:
        database_url = "sqlite+pysqlite://"

    def _load_settings() -> _StubSettings:
        return _StubSettings()

    def _build_factory(_url: str):
        return _session_context_factory(db_engine)

    monkeypatch.setattr(module, "load_app_settings", _load_settings)
    monkeypatch.setattr(module, "build_session_factory", _build_factory)


class _RaiseOnTenantEnterCtx:
    def __enter__(self) -> None:
        raise TenantLifecycleError(tenant_id=TENANT_ID, status="SUSPENDED")

    def __exit__(self, *_exc_info: object) -> None:
        return None


def _connector_tenant_context_raises(*_args: object, **_kwargs: object) -> _RaiseOnTenantEnterCtx:
    return _RaiseOnTenantEnterCtx()


def test_credential_smoke_rejects_unknown_connector() -> None:
    out = _run(
        [
            "--tenant",
            str(uuid4()),
            "--connector",
            "not-a-thing",
            "--account",
            "x",
        ]
    )
    assert out.returncode == 2
    assert "not-a-thing" in out.stderr or "invalid choice" in out.stderr


def test_credential_smoke_returns_2_when_database_url_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_cli_module()

    class _StubSettings:
        database_url = ""

    def _load_empty_settings() -> _StubSettings:
        return _StubSettings()

    monkeypatch.setattr(module, "load_app_settings", _load_empty_settings)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            CONNECTOR_KEY,
            "--account",
            ACCOUNT_ID,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "UMS_DATABASE_URL" in captured.err


def test_credential_smoke_commits_refresh_telemetry_without_connector_run(
    engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_cli_module()
    _patch_settings_and_session(module, monkeypatch, engine)

    def _fake_resolver(
        *,
        session: Session,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
    ) -> SimpleNamespace:
        assert tenant_id == TENANT_ID
        assert connector_key == CONNECTOR_KEY
        assert account_id == ACCOUNT_ID
        row = session.scalars(select(ApiConnectorCredentialORM)).one()
        row.last_refresh_attempt_at = TOKEN_EXPIRY
        row.token_expiry_at = TOKEN_EXPIRY
        row.last_refresh_status = "succeeded"
        row.last_refresh_error_class = None
        return SimpleNamespace(expiry=TOKEN_EXPIRY)

    monkeypatch.setattr(module, "resolve_connector_credentials", _fake_resolver)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            CONNECTOR_KEY,
            "--account",
            ACCOUNT_ID,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"OK connector={CONNECTOR_KEY} account={ACCOUNT_ID}" in captured.out
    assert TOKEN_EXPIRY.isoformat() in captured.out
    with Session(engine) as session:
        credential = session.scalars(select(ApiConnectorCredentialORM)).one()
        run_count = session.scalar(select(func.count()).select_from(ConnectorRunORM))
    assert credential.last_refresh_status == "succeeded"
    assert credential.token_expiry_at is not None
    assert run_count == 0


def test_credential_smoke_returns_2_for_tenant_lifecycle_error(
    engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_cli_module()
    _patch_settings_and_session(module, monkeypatch, engine)

    monkeypatch.setattr(module, "connector_tenant_context", _connector_tenant_context_raises)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            CONNECTOR_KEY,
            "--account",
            ACCOUNT_ID,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "TenantLifecycleError: tenant is missing or not active" in captured.err


def test_credential_smoke_redacts_oauth_refresh_inner_error(
    engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_cli_module()
    _patch_settings_and_session(module, monkeypatch, engine)

    def _raise_oauth_refresh_error(
        *,
        session: Session,
        tenant_id: UUID,
        connector_key: str,
        account_id: str,
    ) -> None:
        _ = (session, tenant_id, connector_key, account_id)
        raise OAuthRefreshError(inner=RuntimeError("inner-refresh-secret-marker"))

    monkeypatch.setattr(module, "resolve_connector_credentials", _raise_oauth_refresh_error)

    exit_code = module.main(
        [
            "--tenant",
            str(TENANT_ID),
            "--connector",
            CONNECTOR_KEY,
            "--account",
            ACCOUNT_ID,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "OAuthRefreshError: Google credential token refresh failed" in captured.err
    assert "inner-refresh-secret-marker" not in captured.err
    with Session(engine) as session:
        run_count = session.scalar(select(func.count()).select_from(ConnectorRunORM))
    assert run_count == 0
