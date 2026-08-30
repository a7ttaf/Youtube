from pathlib import Path

import pytest
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
