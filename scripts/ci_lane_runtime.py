"""Runtime capability state shared by the CI pytest guard and Postgres helper."""

from __future__ import annotations

import json
import os
from contextvars import ContextVar, Token
from functools import cache
from pathlib import Path
from typing import Final

_ACTIVE_TEST_MODULE: Final[ContextVar[str | None]] = ContextVar(
    "ums_ci_active_test_module", default=None
)


def enter_test_module(relative_path: str) -> Token[str | None]:
    """Activate one manifested test module for setup, call, and teardown."""

    return _ACTIVE_TEST_MODULE.set(relative_path)


def exit_test_module(token: Token[str | None]) -> None:
    """Restore the prior runtime capability context."""

    _ACTIVE_TEST_MODULE.reset(token)


def assert_database_access_allowed() -> None:
    """Fail closed when guarded CI requests Postgres outside its database lane."""

    lane = os.environ.get("UMS_CI_PYTEST_LANE")
    if lane is None:
        return
    active_module = _ACTIVE_TEST_MODULE.get()
    try:
        database_modules = json.loads(os.environ["UMS_CI_DATABASE_TEST_MODULES"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("CI database capability manifest is missing or invalid") from exc
    if not isinstance(database_modules, list) or not all(
        isinstance(module, str) for module in database_modules
    ):
        raise RuntimeError("CI database capability manifest must be a string list")
    if lane != "database" or active_module is None or active_module not in database_modules:
        raise RuntimeError(
            "real PostgreSQL access is allowed only while a database-manifested "
            "test item is active in the CI database lane"
        )


@cache
def _relative_source(raw_path: str, project_root: Path) -> str | None:
    try:
        path = Path(raw_path).resolve()
        if not path.is_relative_to(project_root):
            return None
        return path.relative_to(project_root).as_posix()
    except (OSError, ValueError):
        return None


def relative_test_module(raw_path: object, project_root: Path) -> str | None:
    """Normalize an item source only when it belongs to the guarded project."""

    if raw_path is None:
        return None
    return _relative_source(str(raw_path), project_root)
