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
