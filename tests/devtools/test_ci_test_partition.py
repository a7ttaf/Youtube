from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import ci_test_partition
from scripts.ci_test_partition import PartitionError, build_test_partition


def _write_test(
    project_root: Path,
    relative_path: str,
    source: str = "def test_ok(): pass",
) -> None:
    path = project_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_partition_is_exhaustive_and_keeps_database_suites_out_of_fast_lane(tmp_path):
    _write_test(tmp_path, "tests/api/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(tmp_path, "tests/tenancy/test_isolation.py")
    _write_test(tmp_path, "tests/api/test_snapshot_postgres.py")
    _write_test(tmp_path, "tests/connectors/runs/test_tenant_context.py")

    partition = build_test_partition(tmp_path)

    assert partition.fast == (Path("tests/api/test_regular.py"),)
    assert set(partition.database) == {
        Path("tests/api/test_snapshot_postgres.py"),
        Path("tests/connectors/runs/test_tenant_context.py"),
        Path("tests/db/test_schema.py"),
        Path("tests/tenancy/test_isolation.py"),
    }


def test_partition_fails_closed_for_unsuffixed_real_postgres_test(tmp_path):
    _write_test(tmp_path, "tests/api/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(
        tmp_path,
        "tests/api/test_misplaced.py",
        "from tests.db._postgres_helpers import require_" + "postgres_url\n",
    )

    with pytest.raises(PartitionError, match="test_misplaced.py"):
        build_test_partition(tmp_path)


def test_partition_classifies_postgres_in_inherited_conftest(tmp_path):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(tmp_path, "tests/api/test_uses_fixture.py")
    fixture_sentinel = "require_" + "postgres_url"
    _write_test(
        tmp_path,
        "tests/api/conftest.py",
        f"from tests.db._postgres_helpers import {fixture_sentinel}\n",
    )

    partition = build_test_partition(tmp_path)

    assert partition.fast == (Path("tests/unit/test_regular.py"),)
    assert Path("tests/api/test_uses_fixture.py") in partition.database


def test_partition_follows_transitive_pytest_plugins(tmp_path):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(tmp_path, "tests/api/test_uses_fixture.py")
    _write_test(
        tmp_path,
        "tests/api/conftest.py",
        'pytest_plugins = ("tests.fixtures.bridge",)\n',
    )
    _write_test(
        tmp_path,
        "tests/fixtures/bridge.py",
        'pytest_plugins = ("tests.fixtures.postgres",)\n',
    )
    fixture_sentinel = "UMS_TEST_" + "DATABASE_URL"
    _write_test(
        tmp_path,
        "tests/fixtures/postgres.py",
        f'DATABASE_URL = "{fixture_sentinel}"\n',
    )

    partition = build_test_partition(tmp_path)

    assert partition.fast == (Path("tests/unit/test_regular.py"),)
    assert Path("tests/api/test_uses_fixture.py") in partition.database


def test_partition_follows_mutated_pytest_plugins(tmp_path):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(tmp_path, "tests/api/test_uses_fixture.py")
    _write_test(
        tmp_path,
        "tests/api/conftest.py",
        'pytest_plugins = []\npytest_plugins.append("tests.fixtures.postgres")\n',
    )
    fixture_sentinel = "UMS_TEST_" + "DATABASE_URL"
    _write_test(
        tmp_path,
        "tests/fixtures/postgres.py",
        f'DATABASE_URL = "{fixture_sentinel}"\n',
    )

    partition = build_test_partition(tmp_path)

    assert partition.fast == (Path("tests/unit/test_regular.py"),)
    assert Path("tests/api/test_uses_fixture.py") in partition.database


def test_partition_follows_fixture_imported_into_conftest(tmp_path):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(tmp_path, "tests/api/test_uses_fixture.py")
    _write_test(
        tmp_path,
        "tests/api/conftest.py",
        "from tests.fixtures.postgres import postgres_fixture\n",
    )
    fixture_sentinel = "require_" + "postgres_url"
    _write_test(
        tmp_path,
        "tests/fixtures/postgres.py",
        "import pytest\n"
        "from tests.db._postgres_helpers import "
        f"{fixture_sentinel}\n\n"
        "@pytest.fixture\n"
        "def postgres_fixture():\n"
        f"    return {fixture_sentinel}()\n",
    )

    partition = build_test_partition(tmp_path)

    assert partition.fast == (Path("tests/unit/test_regular.py"),)
    assert Path("tests/api/test_uses_fixture.py") in partition.database


def test_partition_follows_project_root_fixture_imported_into_conftest(tmp_path):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(tmp_path, "tests/api/test_uses_fixture.py")
    _write_test(
        tmp_path,
        "tests/api/conftest.py",
        "from ci_fixtures import postgres_fixture\n",
    )
    fixture_sentinel = "UMS_TEST_" + "DATABASE_URL"
    _write_test(
        tmp_path,
        "ci_fixtures.py",
        "import pytest\n\n"
        f'DATABASE_URL = "{fixture_sentinel}"\n\n'
        "@pytest.fixture\n"
        "def postgres_fixture():\n"
        "    return DATABASE_URL\n",
    )

    partition = build_test_partition(tmp_path)

    assert partition.fast == (Path("tests/unit/test_regular.py"),)
    assert Path("tests/api/test_uses_fixture.py") in partition.database


def test_partition_follows_fixture_from_configured_pythonpath(tmp_path):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(tmp_path, "tests/api/test_uses_fixture.py")
    _write_test(
        tmp_path,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["backend"]\n',
    )
    _write_test(
        tmp_path,
        "tests/api/conftest.py",
        "from ci_fixtures import postgres_fixture\n",
    )
    fixture_sentinel = "UMS_TEST_" + "DATABASE_URL"
    _write_test(
        tmp_path,
        "backend/ci_fixtures.py",
        "import pytest\n\n"
        f'DATABASE_URL = "{fixture_sentinel}"\n\n'
        "@pytest.fixture\n"
        "def postgres_fixture():\n"
        "    return DATABASE_URL\n",
    )

    partition = build_test_partition(tmp_path)

    assert partition.fast == (Path("tests/unit/test_regular.py"),)
    assert Path("tests/api/test_uses_fixture.py") in partition.database


def test_partition_fails_closed_for_pytest_plugins_subscript_mutation(tmp_path):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(tmp_path, "tests/api/test_uses_fixture.py")
    _write_test(
        tmp_path,
        "tests/api/conftest.py",
        'pytest_plugins = ["tests.fixtures.fast"]\npytest_plugins[0] = "tests.fixtures.postgres"\n',
    )

    with pytest.raises(PartitionError, match="mutates pytest_plugins dynamically"):
        build_test_partition(tmp_path)


@pytest.mark.parametrize(
    "source",
    (
        '(pytest_plugins := ["tests.fixtures.postgres"])\n',
        'globals()["pytest_plugins"] = ["tests.fixtures.postgres"]\n',
        (
            "pytest_plugins = []\n"
            "plugin_alias = pytest_plugins\n"
            'plugin_alias.append("tests.fixtures.postgres")\n'
        ),
        (
            "import sys\n"
            "pytest_plugins = []\n"
            'getattr(sys.modules[__name__], "pytest_plugins").append('
            '"tests.fixtures.postgres")\n'
        ),
    ),
)
def test_partition_fails_closed_for_unknown_pytest_plugin_mutation(tmp_path, source):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(tmp_path, "tests/api/test_uses_fixture.py")
    _write_test(tmp_path, "tests/api/conftest.py", source)

    with pytest.raises(PartitionError, match="pytest_plugins dynamically"):
        build_test_partition(tmp_path)


def test_partition_fails_closed_for_dynamic_pytest_plugins(tmp_path):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    _write_test(tmp_path, "tests/api/test_uses_fixture.py")
    _write_test(
        tmp_path,
        "tests/api/conftest.py",
        "pytest_plugins = discover_plugins()\n",
    )

    with pytest.raises(PartitionError, match="computes pytest_plugins dynamically"):
        build_test_partition(tmp_path)


def test_run_lane_uses_project_cwd_absolute_paths_and_sanitized_env(tmp_path, monkeypatch):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    captured: dict[str, object] = {}

    def fake_run(command, *, cwd, env, check, **kwargs):
        if "--collect-only" in command:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "tests/db/test_schema.py::test_schema\n"
                    "tests/unit/test_regular.py::test_regular\n\n"
                    "2 tests collected\n"
                ),
                stderr="",
            )
        captured.update(command=command, cwd=cwd, env=env, check=check)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ci_test_partition.subprocess, "run", fake_run)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--ignore=tests/unit")

    assert ci_test_partition.run_lane("fast", tmp_path) == 0
    command = captured["command"]
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["check"] is False
    assert "PYTEST_ADDOPTS" not in captured["env"]
    assert command[-1] == str((tmp_path / "tests/unit/test_regular.py").resolve())
    assert Path(command[-1]).is_absolute()


def test_collection_gate_rejects_assigned_module_with_no_items(tmp_path, monkeypatch):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    partition = build_test_partition(tmp_path)

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="tests/unit/test_regular.py::test_regular\n\n1 test collected\n",
            stderr="",
        )

    monkeypatch.setattr(ci_test_partition.subprocess, "run", fake_run)

    with pytest.raises(PartitionError, match="test_schema.py"):
        ci_test_partition._validate_collected_items(partition, tmp_path.resolve())


def test_collection_gate_rejects_changed_fast_lane_node_manifest(tmp_path, monkeypatch):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    partition = build_test_partition(tmp_path)

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "tests/db/test_schema.py::test_schema\n"
                "tests/unit/test_regular.py::test_regular\n\n"
                "2 tests collected\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(ci_test_partition.subprocess, "run", fake_run)
    monkeypatch.setattr(ci_test_partition, "PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(ci_test_partition, "EXPECTED_FAST_ITEM_COUNT", 2)
    monkeypatch.setattr(ci_test_partition, "EXPECTED_FAST_NODEID_SHA256", "stale")
    monkeypatch.setattr(ci_test_partition, "EXPECTED_DATABASE_ITEM_COUNT", 1)
    monkeypatch.setattr(ci_test_partition, "EXPECTED_DATABASE_NODEID_SHA256", "stale")

    with pytest.raises(PartitionError, match="collected pytest fast lane manifest changed"):
        ci_test_partition._validate_collected_items(partition, tmp_path.resolve())


def test_collection_gate_rejects_lane_assignment_swap(tmp_path, monkeypatch):
    _write_test(tmp_path, "tests/unit/test_regular.py")
    _write_test(tmp_path, "tests/db/test_schema.py")
    partition = build_test_partition(tmp_path)
    fast_nodeid = "tests/unit/test_regular.py::test_regular"
    database_nodeid = "tests/db/test_schema.py::test_schema"

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=f"{database_nodeid}\n{fast_nodeid}\n\n2 tests collected\n",
            stderr="",
        )

    def digest(nodeid):
        return ci_test_partition.hashlib.sha256(nodeid.encode("utf-8")).hexdigest()

    monkeypatch.setattr(ci_test_partition.subprocess, "run", fake_run)
    monkeypatch.setattr(ci_test_partition, "PROJECT_ROOT", tmp_path.resolve())
    monkeypatch.setattr(ci_test_partition, "EXPECTED_FAST_ITEM_COUNT", 1)
    monkeypatch.setattr(ci_test_partition, "EXPECTED_FAST_NODEID_SHA256", digest(fast_nodeid))
    monkeypatch.setattr(ci_test_partition, "EXPECTED_DATABASE_ITEM_COUNT", 1)
    monkeypatch.setattr(
        ci_test_partition, "EXPECTED_DATABASE_NODEID_SHA256", digest(database_nodeid)
    )
    swapped = ci_test_partition.TestPartition(
        fast=partition.database,
        database=partition.fast,
    )

    with pytest.raises(PartitionError, match="collected pytest fast lane manifest changed"):
        ci_test_partition._validate_collected_items(swapped, tmp_path.resolve())


def test_collection_nodeid_normalizes_module_path_only(tmp_path):
    canonical = ci_test_partition._canonical_collected_nodeid(
        r"tests\unit\test_regular.py::test_regular[a\b]",
        tmp_path.resolve(),
    )

    assert canonical == (
        "tests/unit/test_regular.py",
        r"tests/unit/test_regular.py::test_regular[a\b]",
    )
