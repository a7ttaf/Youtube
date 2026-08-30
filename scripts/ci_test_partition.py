# ============================================================================
# Purpose: Define and enforce the exhaustive pytest split used by required CI.
# Database/ORM: PostgreSQL test suites and database-layer tests only; no writes.
# Standards: Fail closed on unassigned real-Postgres tests and run typed lanes.
# Blast Radius: CI coverage only; prevents database tests from leaking or vanishing.
# Connections:
#   - File: .github/workflows/ci-fast.yml -> Runs the no-database lane.
#   - File: .github/workflows/ci-database.yml -> Runs the database lane.
# ============================================================================
"""Build and execute the authoritative pytest partition for required CI."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]

DATABASE_DIRECTORIES: Final = (
    Path("tests/db"),
    Path("tests/tenancy"),
)
DATABASE_EXACT_FILES: Final = frozenset(
    {
        Path("tests/connectors/runs/test_tenant_context.py"),
    }
)
REAL_POSTGRES_SENTINELS: Final = (
    "require_postgres_url",
    "UMS_TEST_DATABASE_URL",
)


class PartitionError(RuntimeError):
    """Raised when the repository's pytest partition is incomplete or unsafe."""


@dataclass(frozen=True)
class TestPartition:
    """Exhaustive, non-overlapping Python test-module assignment."""

    fast: tuple[Path, ...]
    database: tuple[Path, ...]


def _relative_to_project(path: Path, project_root: Path) -> Path:
    return path.relative_to(project_root)


def discover_test_modules(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    """Return every pytest test module under the configured test root."""

    tests_root = project_root / "tests"
    discovered = {
        path
        for pattern in ("test_*.py", "*_test.py")
        for path in tests_root.rglob(pattern)
        if path.is_file()
    }
    if not discovered:
        raise PartitionError(f"no pytest modules discovered under {tests_root}")
    return tuple(sorted(discovered))


def belongs_to_database_lane(relative_path: Path) -> bool:
    """Return whether a test module belongs to the database-required lane."""

    # FIX: A filename-only ``*postgres*`` ignore leaked unsuffixed tests/db and
    # tenancy suites into the no-database job while omitting most Postgres tests
    # from the database job. This predicate is the one source of lane truth.
    return (
        any(relative_path.is_relative_to(directory) for directory in DATABASE_DIRECTORIES)
        or relative_path in DATABASE_EXACT_FILES
        or relative_path.name.endswith("_postgres.py")
    )


def _uses_real_postgres(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    return any(sentinel in source for sentinel in REAL_POSTGRES_SENTINELS)


def build_test_partition(project_root: Path = PROJECT_ROOT) -> TestPartition:
    """Build a complete partition and fail if Postgres coverage could escape."""

    fast: list[Path] = []
    database: list[Path] = []
    leaked_postgres: list[Path] = []

    for path in discover_test_modules(project_root):
        relative_path = _relative_to_project(path, project_root)
        if belongs_to_database_lane(relative_path):
            database.append(relative_path)
        else:
            fast.append(relative_path)
            if _uses_real_postgres(path):
                leaked_postgres.append(relative_path)

    if leaked_postgres:
        rendered = "\n".join(f"  - {path.as_posix()}" for path in leaked_postgres)
        raise PartitionError(
            "real-Postgres tests are not assigned to the database lane:\n" + rendered
        )
    if not fast or not database:
        raise PartitionError("both pytest lanes must contain at least one test module")

    assigned = set(fast) | set(database)
    expected = {
        _relative_to_project(path, project_root) for path in discover_test_modules(project_root)
    }
    overlap = set(fast) & set(database)
    if assigned != expected or overlap:
        raise PartitionError("pytest lanes must be exhaustive and non-overlapping")

    return TestPartition(fast=tuple(fast), database=tuple(database))


def check_partition(project_root: Path = PROJECT_ROOT) -> int:
    """Validate and summarize the current partition without running pytest."""

    partition = build_test_partition(project_root)
    print(
        "pytest partition OK: "
        f"{len(partition.fast)} no-database module(s), "
        f"{len(partition.database)} database module(s)"
    )
    return 0


def run_lane(lane: str, project_root: Path = PROJECT_ROOT) -> int:
    """Run exactly one validated lane through the locked pytest installation."""

    import pytest

    partition = build_test_partition(project_root)
    selected: Sequence[Path] = getattr(partition, lane)
    os.environ.pop("PYTEST_ADDOPTS", None)
    return pytest.main(["-q", *(path.as_posix() for path in selected)])


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point used by the required CI workflows."""

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--lane", choices=("fast", "database"))
    args = parser.parse_args(argv)

    try:
        if args.check:
            return check_partition()
        return run_lane(args.lane)
    except PartitionError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
