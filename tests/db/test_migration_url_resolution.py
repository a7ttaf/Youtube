"""Unit tests for the Alembic migration URL resolver (the env.py footgun fix).

``alembic/env.py`` runs migrations at import time, so it cannot be imported in a
unit test. The URL-resolution logic therefore lives in an importable, pure seam
(``ums_smart_revenue.db.migration_url``) that these tests exercise directly with
fabricated ``alembic.config.Config`` objects — no Alembic runtime context, no DB.

The four quadrants come straight from the spec
(``Docs/superpowers/specs/2026-06-11-alembic-env-url-precedence-design.md``):

  | config source        | UMS_DATABASE_URL | expected winner                              |
  |----------------------|------------------|----------------------------------------------|
  | programmatic (no ini)| decoy set        | the injected sqlalchemy.url (footgun closed) |
  | ini-based (prod)     | set              | UMS_DATABASE_URL (prod contract)             |
  | programmatic (no ini)| unset            | the injected sqlalchemy.url                  |
  | ini-based (prod)     | unset            | the ini placeholder                          |

Plus the case the original spec missed: a ``Config(ini)`` that *overrides* the
ini's url in code (the four ``tests/tenancy`` + ``test_session_tenant_hook``
migration tests do exactly this). The resolver detects the override by comparing
against the ini's on-disk value, so the injected url still wins over an ambient
``UMS_DATABASE_URL`` — closing the footgun for that pattern too.

Ini-based cases write a throwaway ``alembic.ini`` under ``tmp_path`` so the test
never reads (or couples to) the repository's real ini file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config

from ums_smart_revenue.db.migration_url import resolve_database_url

PROGRAMMATIC_URL = "postgresql+psycopg://ums:ums@127.0.0.1:5599/test_real"
DECOY_URL = "postgresql+psycopg://decoy:decoy@127.0.0.1:5599/DECOY"
INI_PLACEHOLDER = "postgresql+psycopg://ums:ums@localhost:5432/ums_smart_revenue"
PROD_DB_URL = "postgresql+psycopg://prod:prod@db.internal:5432/prod"


def _programmatic_config(url: str | None) -> Config:
    """A Config built in code (no ini path) — ``config_file_name`` is None."""
    cfg = Config()
    assert cfg.config_file_name is None
    if url is not None:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _ini_based_config(tmp_path: Path, url: str | None) -> Config:
    """A Config backed by a throwaway ini file — ``config_file_name`` is set.

    Writing a real (temp) ini exercises the genuine production code path
    (alembic reads ``sqlalchemy.url`` from the file) without coupling the test to
    the repository's checked-in ``alembic.ini``.
    """
    ini = tmp_path / "alembic.ini"
    body = "[alembic]\n"
    if url is not None:
        body += f"sqlalchemy.url = {url}\n"
    ini.write_text(body, encoding="utf-8")
    cfg = Config(str(ini))
    assert cfg.config_file_name is not None
    return cfg


def _ini_based_config_with_injection(tmp_path: Path, *, file_url: str, injected_url: str) -> Config:
    """A Config backed by an ini whose url is overridden in code.

    Mirrors tests/tenancy/test_isolation.py et al.: ``Config(ini)`` (the ini
    declares ``file_url``) followed by ``set_main_option("sqlalchemy.url", ...)``.
    The injected value masks the file value in alembic's in-memory parser.
    """
    cfg = _ini_based_config(tmp_path, file_url)
    cfg.set_main_option("sqlalchemy.url", injected_url)
    return cfg


def test_programmatic_config_ignores_ambient_decoy_env_var() -> None:
    # Quadrant 1 — the regression test for the footgun.
    cfg = _programmatic_config(PROGRAMMATIC_URL)
    env = {"UMS_DATABASE_URL": DECOY_URL}
    assert resolve_database_url(cfg, env) == PROGRAMMATIC_URL


def test_ini_based_config_env_var_overrides_placeholder(tmp_path: Path) -> None:
    # Quadrant 2 — production contract: env var wins over the ini placeholder.
    cfg = _ini_based_config(tmp_path, INI_PLACEHOLDER)
    env = {"UMS_DATABASE_URL": PROD_DB_URL}
    assert resolve_database_url(cfg, env) == PROD_DB_URL


def test_programmatic_config_no_env_var_returns_configured() -> None:
    # Quadrant 3.
    cfg = _programmatic_config(PROGRAMMATIC_URL)
    assert resolve_database_url(cfg, {}) == PROGRAMMATIC_URL


def test_ini_based_config_no_env_var_returns_placeholder(tmp_path: Path) -> None:
    # Quadrant 4 — production fallback to the ini placeholder.
    cfg = _ini_based_config(tmp_path, INI_PLACEHOLDER)
    assert resolve_database_url(cfg, {}) == INI_PLACEHOLDER


def test_ini_based_config_no_url_anywhere_raises_runtime_error(tmp_path: Path) -> None:
    # Empty-guard preserved from the original env.py implementation (prod path:
    # ini with no sqlalchemy.url and no env var -> fail fast, never "").
    cfg = _ini_based_config(tmp_path, None)
    with pytest.raises(RuntimeError, match="Database URL not configured"):
        resolve_database_url(cfg, {})


def test_programmatic_config_without_injected_url_falls_back_to_env_var() -> None:
    # Edge: a programmatic Config that injected NO url has nothing explicit to
    # honor, so an ambient env var is still respected (no surprise).
    cfg = _programmatic_config(None)
    env = {"UMS_DATABASE_URL": PROD_DB_URL}
    assert resolve_database_url(cfg, env) == PROD_DB_URL


def test_ini_based_config_with_injected_url_beats_decoy_env_var(tmp_path: Path) -> None:
    # The spec-missed case: Config(ini) whose url is overridden in code (the
    # tests/tenancy pattern). The injected url differs from the ini's on-disk
    # placeholder, so it wins over an ambient decoy UMS_DATABASE_URL — the
    # footgun is closed for this pattern, not just for no-ini Config().
    cfg = _ini_based_config_with_injection(
        tmp_path, file_url=INI_PLACEHOLDER, injected_url=PROGRAMMATIC_URL
    )
    env = {"UMS_DATABASE_URL": DECOY_URL}
    assert resolve_database_url(cfg, env) == PROGRAMMATIC_URL


def test_ini_based_config_with_injected_url_no_env_returns_injected(tmp_path: Path) -> None:
    cfg = _ini_based_config_with_injection(
        tmp_path, file_url=INI_PLACEHOLDER, injected_url=PROGRAMMATIC_URL
    )
    assert resolve_database_url(cfg, {}) == PROGRAMMATIC_URL


def test_ini_based_config_explicit_placeholder_equals_disk_still_honors_env_var(
    tmp_path: Path,
) -> None:
    # A caller that sets sqlalchemy.url to the SAME value as the ini placeholder
    # has no distinct injection intent; UMS_DATABASE_URL must still win (prod
    # contract).  This closes the edge-case where a test hardcodes the local
    # placeholder and an ambient UMS_DATABASE_URL would otherwise be ignored.
    cfg = _ini_based_config(tmp_path, INI_PLACEHOLDER)
    cfg.set_main_option("sqlalchemy.url", INI_PLACEHOLDER)
    env = {"UMS_DATABASE_URL": PROD_DB_URL}
    assert resolve_database_url(cfg, env) == PROD_DB_URL


def test_unreadable_ini_raises_runtime_error(tmp_path: Path) -> None:
    # If the ini path exists but cannot be parsed, _ini_declared_url must fail
    # fast instead of returning None (which would silently flip precedence).
    ini = tmp_path / "alembic.ini"
    ini.write_text("[alembic]\nsqlalchemy.url = some-url\n", encoding="utf-8")
    cfg = Config(str(ini))
    # Pre-initialize Alembic's lazy file_config cache so get_main_option()
    # inside resolve_database_url uses the cached in-memory parse and does not
    # re-read the file (which would raise configparser.MissingSectionHeaderError
    # from the garbage content we write next).
    cfg.get_main_option("sqlalchemy.url")
    # Replace the file with garbage so our _ini_declared_url configparser.read
    # raises configparser.Error (no section headers), which is caught and
    # re-raised as RuntimeError.
    ini.write_text("NOT VALID INI CONTENT [[[\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not be parsed"):
        resolve_database_url(cfg, {"UMS_DATABASE_URL": PROD_DB_URL})


def test_invalid_utf8_ini_raises_runtime_error(tmp_path: Path) -> None:
    # If the ini file contains invalid UTF-8 bytes, configparser.read raises
    # UnicodeDecodeError.  _ini_declared_url must catch it and raise RuntimeError
    # instead of letting the encoding error propagate.
    ini = tmp_path / "alembic.ini"
    ini.write_text("[alembic]\nsqlalchemy.url = some-url\n", encoding="utf-8")
    cfg = Config(str(ini))
    cfg.get_main_option("sqlalchemy.url")
    # Overwrite with invalid UTF-8 bytes so our _ini_declared_url's fresh
    # configparser.read() raises UnicodeDecodeError, which is caught and
    # re-raised as RuntimeError.
    ini.write_bytes(b"[alembic]\nsqlalchemy.url = \xff\xfe not-utf8\n")
    with pytest.raises(RuntimeError, match="could not be parsed"):
        resolve_database_url(cfg, {"UMS_DATABASE_URL": PROD_DB_URL})
