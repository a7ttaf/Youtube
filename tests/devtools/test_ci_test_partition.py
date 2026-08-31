import hashlib
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import ci_test_partition
from scripts.ci_lane_runtime import (
    assert_database_access_allowed,
    enter_test_module,
    exit_test_module,
)
from scripts.ci_test_partition import PartitionError, build_test_partition


def _write_file(project_root: Path, relative_path: str, source: str) -> None:
    path = project_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _write_test(
    project_root: Path,
    relative_path: str,
    source: str = "def test_ok(): pass\n",
) -> None:
    _write_file(project_root, relative_path, source)


def _write_manifest(
    project_root: Path,
    *,
    fast: tuple[str, ...],
    database: tuple[str, ...],
    support: tuple[str, ...] = (),
) -> None:
    rows = [
        *(f"fast|{path}" for path in fast),
        *(f"database|{path}" for path in database),
        *(f"support|{path}" for path in support),
    ]
    _write_file(
        project_root,
        "scripts/ci_pytest_lanes.conf",
        "\n".join(sorted(rows)) + "\n",
    )


def _basic_project(project_root: Path) -> None:
    _write_test(project_root, "tests/unit/test_regular.py")
    _write_test(project_root, "tests/db/test_schema.py")
    _write_manifest(
        project_root,
        fast=("tests/unit/test_regular.py",),
        database=("tests/db/test_schema.py",),
    )


def test_partition_uses_exact_exhaustive_manifest(tmp_path):
    _basic_project(tmp_path)

    partition = build_test_partition(tmp_path)

    assert partition.fast == (Path("tests/unit/test_regular.py"),)
    assert partition.database == (Path("tests/db/test_schema.py"),)
    assert partition.database_support == ()


def test_partition_rejects_unassigned_test_module(tmp_path):
    _basic_project(tmp_path)
    _write_test(tmp_path, "tests/api/test_new.py")

    with pytest.raises(PartitionError, match="unassigned: tests/api/test_new.py"):
        build_test_partition(tmp_path)


def test_partition_rejects_stale_manifested_test(tmp_path):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_file(
        tmp_path,
        "scripts/ci_pytest_lanes.conf",
        "\n".join(
            sorted(
                (
                    "database|tests/db/test_schema.py",
                    "fast|tests/unit/test_missing.py",
                    "fast|tests/unit/test_regular.py",
                )
            )
        )
        + "\n",
    )

    with pytest.raises(PartitionError, match="manifested pytest path is missing"):
        build_test_partition(tmp_path)


@pytest.mark.parametrize(
    "rows, message",
    (
        (
            (
                "fast|tests/unit/test_regular.py",
                "database|tests/db/test_schema.py",
            ),
            "sorted",
        ),
        (
            (
                "database|tests/db/test_schema.py",
                "fast|tests/unit/test_regular.py",
                "fast|tests/unit/test_regular.py",
            ),
            "duplicates",
        ),
        (
            (
                "database|tests/db/test_schema.py",
                "fast|../test_regular.py",
            ),
            "invalid lane manifest path",
        ),
        (
            (
                "database|tests/db/test_schema.py",
                r"fast|tests\unit\test_regular.py",
            ),
            "invalid lane manifest path",
        ),
        (
            (
                "database|tests/db/test_schema.py",
                "fast|tests/unit/test_*.py",
            ),
            "invalid lane manifest path",
        ),
    ),
)
def test_partition_rejects_malformed_manifest(tmp_path, rows, message):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_file(tmp_path, "scripts/ci_pytest_lanes.conf", "\n".join(rows) + "\n")

    with pytest.raises(PartitionError, match=message):
        build_test_partition(tmp_path)


def test_partition_requires_support_marker(tmp_path):
    _basic_project(tmp_path)
    _write_file(tmp_path, "ci_db_plugin.py", "VALUE = 1\n")
    _write_manifest(
        tmp_path,
        fast=("tests/unit/test_regular.py",),
        database=("tests/db/test_schema.py",),
        support=("ci_db_plugin.py",),
    )

    with pytest.raises(PartitionError, match="must declare UMS_CI_DATABASE_REQUIRED"):
        build_test_partition(tmp_path)


def test_partition_rejects_known_database_access_in_fast_module(tmp_path):
    postgres_env = "UMS_TEST_" + "DATABASE_URL"
    _write_test(
        tmp_path,
        "tests/unit/test_regular.py",
        f'def test_ok(): return "{postgres_env}"\n',
    )
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_manifest(
        tmp_path,
        fast=("tests/unit/test_regular.py",),
        database=("tests/db/test_schema.py",),
    )

    with pytest.raises(PartitionError, match="tests/unit/test_regular.py"):
        build_test_partition(tmp_path)


@pytest.mark.parametrize(
    "conftest_source, bridge_source",
    (
        (
            "from ci_bridge import *  # noqa: F403\n",
            '__all__ = ["pytest_plugins"]\npytest_plugins = ("ci_db_plugin",)\n',
        ),
        (
            'class Configure:\n    globals()["pytest_plugins"] = ("ci_db_plugin",)\n',
            None,
        ),
        (
            'def configure(_=globals().__setitem__("pytest_plugins", '
            '("ci_db_plugin",))):\n    pass\n',
            None,
        ),
        (
            '@(lambda fn: (globals().__setitem__("pytest_plugins", '
            '("ci_db_plugin",)) or fn))\ndef configure():\n    pass\n',
            None,
        ),
        (
            'def configure():\n    globals()["pytest_plugins"] = '
            '("ci_db_plugin",)\n\nconfigure()\n',
            None,
        ),
    ),
)
def test_runtime_guard_rejects_dynamic_database_plugin_in_fast_lane(
    tmp_path,
    conftest_source,
    bridge_source,
):
    _write_test(
        tmp_path,
        "tests/unit/test_regular.py",
        "def test_regular(database_fixture): assert database_fixture == 'db'\n",
    )
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_file(tmp_path, "conftest.py", conftest_source)
    if bridge_source is not None:
        _write_file(tmp_path, "ci_bridge.py", bridge_source)
    _write_file(
        tmp_path,
        "ci_db_plugin.py",
        "import pytest\n\n"
        "UMS_CI_DATABASE_REQUIRED = True\n\n"
        "@pytest.fixture\n"
        "def database_fixture():\n"
        "    return 'db'\n",
    )
    _write_manifest(
        tmp_path,
        fast=("tests/unit/test_regular.py",),
        database=("tests/db/test_schema.py",),
        support=("ci_db_plugin.py",),
    )
    partition = build_test_partition(tmp_path)

    with pytest.raises(PartitionError, match="database-only support"):
        ci_test_partition._validate_collected_lane(partition, "fast", tmp_path.resolve())


def test_runtime_guard_allows_manifested_database_support_in_database_lane(tmp_path):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(
        tmp_path,
        "tests/db/test_schema.py",
        "def test_schema(database_fixture): assert database_fixture == 'db'\n",
    )
    _write_file(tmp_path, "conftest.py", 'pytest_plugins = ("ci_db_plugin",)\n')
    _write_file(
        tmp_path,
        "ci_db_plugin.py",
        "import pytest\n\n"
        "UMS_CI_DATABASE_REQUIRED = True\n\n"
        "@pytest.fixture\n"
        "def database_fixture():\n"
        "    return 'db'\n",
    )
    _write_manifest(
        tmp_path,
        fast=("tests/unit/test_regular.py",),
        database=("tests/db/test_schema.py",),
        support=("ci_db_plugin.py",),
    )
    partition = build_test_partition(tmp_path)

    ci_test_partition._validate_collected_lane(partition, "database", tmp_path.resolve())


def test_database_capability_fails_closed_and_allows_active_database_item(monkeypatch):
    postgres_env = "UMS_TEST_" + "DATABASE_URL"
    module_path = "tests/db/test_schema.py"
    monkeypatch.setenv("UMS_CI_DATABASE_TEST_MODULES", f'["{module_path}"]')
    helper = getattr(import_module("tests.db._postgres_helpers"), "require_" + "postgres_url")

    monkeypatch.setenv("UMS_CI_PYTEST_LANE", "fast")
    token = enter_test_module(module_path)
    try:
        with pytest.raises(RuntimeError, match="database-manifested"):
            helper()
    finally:
        exit_test_module(token)

    monkeypatch.setenv("UMS_CI_PYTEST_LANE", "database")
    with pytest.raises(RuntimeError, match="database-manifested"):
        helper()
    token = enter_test_module(module_path)
    try:
        with pytest.raises(RuntimeError, match="UMS_TEST_DATABASE_URL required"):
            helper()
        monkeypatch.setenv(postgres_env, "postgresql+psycopg://example/test_ums")
        assert_database_access_allowed()
        assert helper() == "postgresql+psycopg://example/test_ums"
    finally:
        exit_test_module(token)


def test_run_lane_uses_exact_cwd_paths_guard_and_sanitized_environment(tmp_path, monkeypatch):
    _basic_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_run(command, *, cwd, env, check, **kwargs):
        if "--collect-only" in command:
            assert not any("test_schema.py" in argument for argument in command)
            return SimpleNamespace(
                returncode=0,
                stdout="tests/unit/test_regular.py::test_regular\n\n1 test collected\n",
                stderr="",
            )
        captured.update(command=command, cwd=cwd, env=env, check=check)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ci_test_partition.subprocess, "run", fake_run)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--ignore=tests/unit")
    monkeypatch.setenv("UMS_TEST_" + "DATABASE_URL", "must-be-removed")

    assert ci_test_partition.run_lane("fast", tmp_path) == 0
    command = captured["command"]
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["check"] is False
    assert "PYTEST_ADDOPTS" not in captured["env"]
    assert "UMS_TEST_" + "DATABASE_URL" not in captured["env"]
    assert command[3:5] == ["-p", "scripts.ci_pytest_lane_guard"]
    assert command[-1] == str((tmp_path / "tests/unit/test_regular.py").resolve())
    assert Path(command[-1]).is_absolute()


def test_collection_gate_uses_exact_lane_argv_and_rejects_disappearing_item(
    tmp_path,
    monkeypatch,
):
    _basic_project(tmp_path)
    partition = build_test_partition(tmp_path)

    def fake_run(command, **kwargs):
        has_database_argv = any("test_schema.py" in argument for argument in command)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "tests/unit/test_regular.py::test_regular\n\n1 test collected\n"
                if has_database_argv
                else "no tests collected\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(ci_test_partition.subprocess, "run", fake_run)

    with pytest.raises(PartitionError, match="test_regular.py"):
        ci_test_partition._validate_collected_lane(partition, "fast", tmp_path.resolve())


def test_collection_gate_rejects_changed_lane_node_manifest(tmp_path, monkeypatch):
    _basic_project(tmp_path)
    partition = build_test_partition(tmp_path)

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="tests/unit/test_regular.py::test_regular\n\n1 test collected\n",
            stderr="",
        )

    monkeypatch.setattr(ci_test_partition.subprocess, "run", fake_run)
    monkeypatch.setattr(ci_test_partition, "PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(ci_test_partition, "EXPECTED_FAST_ITEM_COUNT", 2)
    monkeypatch.setattr(ci_test_partition, "EXPECTED_FAST_NODEID_SHA256", "stale")

    with pytest.raises(PartitionError, match="collected pytest fast lane manifest changed"):
        ci_test_partition._validate_collected_lane(partition, "fast", tmp_path.resolve())


def test_collection_gate_rejects_lane_assignment_swap(tmp_path, monkeypatch):
    _basic_project(tmp_path)
    partition = build_test_partition(tmp_path)
    fast_nodeid = "tests/unit/test_regular.py::test_regular"
    database_nodeid = "tests/db/test_schema.py::test_schema"

    def fake_run(command, **kwargs):
        selected_nodeid = (
            database_nodeid
            if any("test_schema.py" in argument for argument in command)
            else fast_nodeid
        )
        return SimpleNamespace(
            returncode=0,
            stdout=f"{selected_nodeid}\n\n1 test collected\n",
            stderr="",
        )

    def digest(nodeid):
        return hashlib.sha256(nodeid.encode("utf-8")).hexdigest()

    monkeypatch.setattr(ci_test_partition.subprocess, "run", fake_run)
    monkeypatch.setattr(ci_test_partition, "PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(ci_test_partition, "EXPECTED_FAST_ITEM_COUNT", 1)
    monkeypatch.setattr(ci_test_partition, "EXPECTED_FAST_NODEID_SHA256", digest(fast_nodeid))
    swapped = ci_test_partition.TestPartition(
        fast=partition.database,
        database=partition.fast,
    )

    with pytest.raises(PartitionError, match="collected pytest fast lane manifest changed"):
        ci_test_partition._validate_collected_lane(swapped, "fast", tmp_path.resolve())


def test_collection_nodeid_normalizes_module_path_only(tmp_path):
    canonical = ci_test_partition._canonical_collected_nodeid(
        r"tests\unit\test_regular.py::test_regular[a\b]",
        tmp_path.resolve(),
    )

    assert canonical == (
        "tests/unit/test_regular.py",
        r"tests/unit/test_regular.py::test_regular[a\b]",
    )
