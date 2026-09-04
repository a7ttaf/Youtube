# ============================================================================
# Purpose: Enforce the explicit CI pytest lane contract after Python executes.
# Database/ORM: Database-only pytest support modules; no database operations.
# Standards: Inspect actual items, plugins, fixture closure, and loaded modules.
# Blast Radius: CI test selection only; fast lane fails on database support use.
# Connections:
#   - File: scripts/ci_test_partition.py -> Supplies the exact lane contract.
#   - File: scripts/ci_lane_runtime.py -> Activates per-item DB capability state.
# ============================================================================
"""Runtime pytest guard for the authoritative CI test partition."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
from scripts.ci_lane_runtime import enter_test_module, exit_test_module, relative_test_module

DATABASE_MARKER: Final = "UMS_CI_DATABASE_REQUIRED"


def _json_path_set(name: str) -> frozenset[str]:
    """Read one path list supplied by the partition launcher."""

    try:
        value = json.loads(os.environ[name])
    except (KeyError, json.JSONDecodeError) as exc:
        raise pytest.UsageError(f"CI pytest lane guard requires valid {name}") from exc
    if not isinstance(value, list) or not all(isinstance(path, str) for path in value):
        raise pytest.UsageError(f"CI pytest lane guard requires {name} to be a string list")
    return frozenset(value)


@cache
def _contract() -> tuple[str, Path, frozenset[str], frozenset[str], frozenset[str]]:
    """Return the launcher-owned runtime lane contract."""

    lane = os.environ.get("UMS_CI_PYTEST_LANE")
    if lane not in {"fast", "database"}:
        raise pytest.UsageError("CI pytest lane guard requires a valid UMS_CI_PYTEST_LANE")
    try:
        project_root = Path(os.environ["UMS_CI_PROJECT_ROOT"]).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise pytest.UsageError("CI pytest lane guard requires UMS_CI_PROJECT_ROOT") from exc
    return (
        lane,
        project_root,
        _json_path_set("UMS_CI_SELECTED_TEST_MODULES"),
        _json_path_set("UMS_CI_DATABASE_TEST_MODULES"),
        _json_path_set("UMS_CI_DATABASE_SUPPORT_MODULES"),
    )


def _module_source(module: ModuleType, project_root: Path) -> str | None:
    return relative_test_module(getattr(module, "__file__", None), project_root)


def _loaded_modules(project_root: Path) -> Iterable[tuple[ModuleType, str]]:
    """Yield actual imported project modules and canonical source paths."""

    for module in tuple(sys.modules.values()):
        if not isinstance(module, ModuleType):
            continue
        relative = _module_source(module, project_root)
        if relative is not None:
            yield module, relative


def _plugin_sources(config: pytest.Config, project_root: Path) -> set[str]:
    """Return source paths for actual registered pytest plugins."""

    sources: set[str] = set()
    for plugin in config.pluginmanager.get_plugins():
        module = (
            plugin if isinstance(plugin, ModuleType) else sys.modules.get(type(plugin).__module__)
        )
        if isinstance(module, ModuleType):
            relative = _module_source(module, project_root)
            if relative is not None:
                sources.add(relative)
    return sources


def _fixture_sources(items: Iterable[pytest.Item], project_root: Path) -> set[str]:
    """Return implementation modules from collected fixture closures."""

    sources: set[str] = set()
    for item in items:
        fixture_info = getattr(item, "_fixtureinfo", None)
        definitions = getattr(fixture_info, "name2fixturedefs", {})
        for fixture_defs in definitions.values():
            for fixture_def in fixture_defs or ():
                function = getattr(fixture_def, "func", None)
                module = sys.modules.get(getattr(function, "__module__", ""))
                if isinstance(module, ModuleType):
                    relative = _module_source(module, project_root)
                    if relative is not None:
                        sources.add(relative)
    return sources


def _enforce_collection_contract(config: pytest.Config, items: Iterable[pytest.Item]) -> None:
    """Reject wrong-lane items and observed database support in the fast lane."""

    lane, project_root, selected, database_tests, database_support = _contract()
    item_list = tuple(items)
    wrong_items: list[str] = []
    for item in item_list:
        relative = relative_test_module(getattr(item, "path", None), project_root)
        if relative is None or relative not in selected:
            wrong_items.append(item.nodeid)
            continue
        if (relative in database_tests) != (lane == "database"):
            wrong_items.append(item.nodeid)
    if wrong_items:
        rendered = "\n".join(f"  - {nodeid}" for nodeid in sorted(wrong_items))
        raise pytest.UsageError(
            f"CI pytest {lane} lane collected item(s) outside its exact manifest:\n{rendered}"
        )

    loaded = {relative for _, relative in _loaded_modules(project_root)}
    loaded.update(_plugin_sources(config, project_root))
    loaded.update(_fixture_sources(item_list, project_root))
    marked = {
        relative
        for module, relative in _loaded_modules(project_root)
        if getattr(module, DATABASE_MARKER, False) is True
    }
    undeclared_marked = marked - database_support - database_tests
    if undeclared_marked:
        rendered = "\n".join(f"  - {path}" for path in sorted(undeclared_marked))
        raise pytest.UsageError(
            "loaded database-marked module(s) are absent from the support manifest:\n" + rendered
        )
    if lane == "fast":
        offenders = (loaded & database_support) | marked
        if offenders:
            rendered = "\n".join(f"  - {path}" for path in sorted(offenders))
            raise pytest.UsageError(
                "CI pytest fast lane loaded database-only support module(s):\n" + rendered
            )


def _enforce_loaded_support(config: pytest.Config) -> None:
    """Reject database support imported after collection without rescanning items."""

    lane, project_root, _, database_tests, database_support = _contract()
    loaded_modules = tuple(_loaded_modules(project_root))
    marked = {
        relative
        for module, relative in loaded_modules
        if getattr(module, DATABASE_MARKER, False) is True
    }
    undeclared_marked = marked - database_support - database_tests
    if undeclared_marked:
        rendered = "\n".join(f"  - {path}" for path in sorted(undeclared_marked))
        raise pytest.UsageError(
            "loaded database-marked module(s) are absent from the support manifest:\n" + rendered
        )
    if lane == "fast":
        offenders = {relative for _, relative in loaded_modules} & database_support
        offenders.update(marked)
        if offenders:
            rendered = "\n".join(f"  - {path}" for path in sorted(offenders))
            raise pytest.UsageError(
                "CI pytest fast lane loaded database-only support module(s):\n" + rendered
            )


def _enforce_fixture_definition(fixturedef: Any, config: pytest.Config) -> None:
    """Reject one database-only fixture before its setup function executes."""

    lane, project_root, _, database_tests, database_support = _contract()
    function = getattr(fixturedef, "func", None)
    module = sys.modules.get(getattr(function, "__module__", ""))
    if not isinstance(module, ModuleType):
        return
    relative = _module_source(module, project_root)
    if relative is None:
        return
    marked = getattr(module, DATABASE_MARKER, False) is True
    if marked and relative not in database_support and relative not in database_tests:
        raise pytest.UsageError(
            f"database-marked fixture module is absent from support manifest: {relative}"
        )
    if lane == "fast" and (marked or relative in database_support):
        raise pytest.UsageError(
            f"CI pytest fast lane requested database-only fixture from {relative}"
        )


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_finish(session: pytest.Session):
    """Enforce after every plugin finishes mutating collected items."""

    yield
    _enforce_collection_contract(session.config, session.items)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_fixture_setup(fixturedef: Any, request: pytest.FixtureRequest):
    """Reject a database-only fixture before setup without a global rescan."""

    _enforce_fixture_definition(fixturedef, request.config)
    yield


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item):
    """Grant database capability only for the duration of one manifested item."""

    _, project_root, _, _, _ = _contract()
    relative = relative_test_module(getattr(item, "path", None), project_root)
    if relative is None:
        raise pytest.UsageError(f"cannot resolve guarded pytest item path: {item.nodeid}")
    token = enter_test_module(relative)
    try:
        yield
    finally:
        exit_test_module(token)
        _enforce_loaded_support(item.config)
