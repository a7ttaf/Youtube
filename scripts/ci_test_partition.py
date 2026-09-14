# ============================================================================
# Purpose: Define and enforce the exhaustive pytest split used by required CI.
# Database/ORM: PostgreSQL test suites and database-layer tests only; no writes.
# Standards: Use an exact manifest plus runtime pytest enforcement; fail closed.
# Blast Radius: CI coverage only; prevents database tests from leaking or vanishing.
# Connections:
#   - File: scripts/ci_pytest_lanes.conf -> Auditable per-module lane contract.
#   - File: scripts/ci_pytest_lane_guard.py -> Enforces the contract at runtime.
#   - File: .github/workflows/ci-fast.yml -> Runs the no-database lane.
#   - File: .github/workflows/ci-database.yml -> Runs the database lane.
# ============================================================================
"""Build and execute the authoritative pytest partition for required CI."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
MANIFEST_PATH: Final = Path("scripts/ci_pytest_lanes.conf")
GUARD_PLUGIN: Final = "scripts.ci_pytest_lane_guard"
DATABASE_MARKER: Final = "UMS_CI_DATABASE_REQUIRED"
POSTGRES_URL_ENV: Final = "UMS_TEST_" + "DATABASE_URL"
DATABASE_ACCESS_TOKENS: Final = (
    "require_" + "postgres_url",
    POSTGRES_URL_ENV,
    "psycopg." + "connect(",
)

# Updated deliberately after merging main with #225 (P0-e dev gateway):
# its four new fast-lane tests grew the collected manifest 3117 -> 3121.
# Updated deliberately after merging main with #227 (U2 country evidence):
# two new fast-lane modules plus expanded normalizer suites 3121 -> 3204.
# Updated deliberately after merging main with #228 (identity/withholding):
# its new fast-lane suites grew the collected manifest 3204 -> 3265.
EXPECTED_FAST_ITEM_COUNT: Final = 3265
EXPECTED_FAST_NODEID_SHA256: Final = (
    "8b2156688a1e2e97513ed7ff6e377263f9eec0ff854bf5e9e8c60500f58d9dc4"
)
EXPECTED_DATABASE_ITEM_COUNT: Final = 461
EXPECTED_DATABASE_NODEID_SHA256: Final = (
    "79418ba74bc9fdd9dfe5926234f6dc0c9fa07c9ade3729e1d41a8c7eca5b766b"
)


class PartitionError(RuntimeError):
    """Raised when the repository's pytest partition is incomplete or unsafe."""


@dataclass(frozen=True)
class TestPartition:
    """Exact, exhaustive Python test assignment plus database support modules."""

    fast: tuple[Path, ...]
    database: tuple[Path, ...]
    database_support: tuple[Path, ...] = ()


def _relative_to_project(path: Path, project_root: Path) -> Path:
    """Return the path relative to the resolved project root."""
    return path.resolve().relative_to(project_root.resolve())


def discover_test_modules(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    """Return every pytest test module under the configured test root."""
    tests_root = project_root / "tests"
    discovered = {
        path.resolve()
        for pattern in ("test_*.py", "*_test.py")
        for path in tests_root.rglob(pattern)
        if path.is_file()
    }
    if not discovered:
        raise PartitionError(f"no pytest modules discovered under {tests_root}")
    return tuple(sorted(discovered))


def _manifest_path(raw_path: str, project_root: Path, line_number: int) -> Path:
    """Validate one canonical, existing project-relative Python path."""
    pure_path = PurePosixPath(raw_path)
    if (
        pure_path.is_absolute()
        or not pure_path.parts
        or ".." in pure_path.parts
        or pure_path.suffix != ".py"
        or "\\" in raw_path
        or any(character in raw_path for character in "*?[]")
    ):
        raise PartitionError(f"invalid lane manifest path at line {line_number}: {raw_path!r}")
    relative_path = Path(*pure_path.parts)
    resolved_root = project_root.resolve()
    resolved = (resolved_root / relative_path).resolve()
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise PartitionError(f"manifested pytest path is missing: {raw_path}")
    return relative_path


def _require_sorted_unique_rows(raw_lines: list[str]) -> None:
    """Reject manifests whose effective rows are unsorted or duplicated."""
    rows = [
        line.strip() for line in raw_lines if line.strip() and not line.lstrip().startswith("#")
    ]
    if rows != sorted(rows) or len(rows) != len(set(rows)):
        raise PartitionError("pytest lane manifest rows must be sorted with no duplicates")


def _parse_manifest_lanes(raw_lines: list[str], resolved_root: Path) -> dict[str, list[Path]]:
    """Parse each manifest row into its lane bucket, rejecting cross-lane paths."""
    lanes: dict[str, list[Path]] = {"fast": [], "database": [], "support": []}
    seen_paths: dict[Path, str] = {}
    for line_number, row in enumerate(raw_lines, start=1):
        stripped = row.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lane, separator, raw_path = stripped.partition("|")
        if not separator or lane not in lanes or not raw_path:
            raise PartitionError(f"invalid pytest lane manifest row at line {line_number}: {row!r}")
        relative_path = _manifest_path(raw_path, resolved_root, line_number)
        previous_lane = seen_paths.get(relative_path)
        if previous_lane is not None:
            raise PartitionError(
                f"pytest path appears in both {previous_lane} and {lane}: {raw_path}"
            )
        seen_paths[relative_path] = lane
        lanes[lane].append(relative_path)
    return lanes


def _require_exhaustive_assignment(assigned: set[Path], discovered: set[Path]) -> None:
    """Reject lane manifests that do not cover every discovered test module."""
    if assigned == discovered:
        return
    missing = sorted(discovered - assigned)
    stale = sorted(assigned - discovered)
    details = [*(f"  unassigned: {path.as_posix()}" for path in missing)]
    details.extend(f"  not a discovered test: {path.as_posix()}" for path in stale)
    raise PartitionError("pytest lane manifest is not exhaustive:\n" + "\n".join(details))


def _validate_support_entries(
    support_paths: list[Path],
    support: set[Path],
    discovered: set[Path],
    resolved_root: Path,
) -> None:
    """Reject support rows that are test modules or lack the database marker."""
    if support & discovered:
        rendered = "\n".join(f"  - {path.as_posix()}" for path in sorted(support & discovered))
        raise PartitionError("support entries cannot be pytest test modules:\n" + rendered)

    for support_path in support_paths:
        source = (resolved_root / support_path).read_text(encoding="utf-8")
        if not _module_declares_database_marker(source):
            raise PartitionError(
                f"database support module {support_path.as_posix()} must declare "
                f"{DATABASE_MARKER} = True"
            )


def _module_declares_database_marker(source: str) -> bool:
    """Return True only for a module-level ``UMS_CI_DATABASE_REQUIRED = True`` assignment."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or node.value.value is not True:
            continue
        targets_marker = any(
            isinstance(target, ast.Name) and target.id == DATABASE_MARKER for target in node.targets
        )
        if targets_marker:
            return True
    return False


@cache
def load_test_partition(project_root: Path = PROJECT_ROOT) -> TestPartition:
    """Load the exact lane manifest without executing project Python."""
    resolved_root = project_root.resolve()
    manifest_path = resolved_root / MANIFEST_PATH
    try:
        raw_lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PartitionError(f"cannot read pytest lane manifest {manifest_path}: {exc}") from exc

    _require_sorted_unique_rows(raw_lines)
    lanes = _parse_manifest_lanes(raw_lines, resolved_root)
    discovered = {
        _relative_to_project(path, resolved_root) for path in discover_test_modules(resolved_root)
    }
    fast = set(lanes["fast"])
    database = set(lanes["database"])
    support = set(lanes["support"])
    _require_exhaustive_assignment(fast | database, discovered)
    if not fast or not database:
        raise PartitionError("both pytest lanes must contain at least one test module")
    _validate_support_entries(lanes["support"], support, discovered, resolved_root)
    _validate_database_access_inventory(
        resolved_root,
        database_modules=database,
        support_modules=support,
    )
    return TestPartition(
        fast=tuple(lanes["fast"]),
        database=tuple(lanes["database"]),
        database_support=tuple(lanes["support"]),
    )


def _project_python_sources(project_root: Path) -> tuple[Path, ...]:
    """Return lane-governed Python sources: test modules only.

    Backend and ops scripts legitimately hold real Postgres access (that is
    what adapters are for); the lane contract governs which TEST modules may
    carry it, and the runtime guard strips the DSN in the fast lane.
    """
    tests_root = project_root / "tests"
    if not tests_root.is_dir():
        return ()
    return tuple(sorted(path for path in tests_root.rglob("*.py") if path.is_file()))


def _validate_database_access_inventory(
    project_root: Path,
    *,
    database_modules: set[Path],
    support_modules: set[Path],
) -> None:
    """Keep known real-Postgres access APIs inside declared database files."""
    allowed = database_modules | support_modules
    violations: list[Path] = []
    for source_path in _project_python_sources(project_root):
        try:
            source = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PartitionError(f"cannot inspect Python source {source_path}: {exc}") from exc
        if any(token in source for token in DATABASE_ACCESS_TOKENS):
            relative_path = _relative_to_project(source_path, project_root)
            if relative_path not in allowed:
                violations.append(relative_path)
    if violations:
        rendered = "\n".join(f"  - {path.as_posix()}" for path in sorted(violations))
        raise PartitionError(
            "known real-Postgres access appears outside the explicit database lane contract:\n"
            + rendered
        )


def build_test_partition(project_root: Path = PROJECT_ROOT) -> TestPartition:
    """Build the explicit partition; never infer arbitrary Python control flow."""
    return load_test_partition(project_root.resolve())


def _lane_tmp_root(environment: Mapping[str, str]) -> Path:
    """Resolve the private per-user temp root shared by lane subprocesses."""
    cache_root = environment.get("RUNNER_TEMP") or environment.get("XDG_CACHE_HOME")
    base = Path(cache_root) if cache_root else Path.home() / ".cache"
    return base / "ums-pytest-tmp"


def _pytest_environment(
    partition: TestPartition,
    lane: str,
    project_root: Path,
    expected_collection: tuple[int, str] | None = None,
    nodeid_dump: Path | None = None,
) -> dict[str, str]:
    """Build the identical fail-closed environment used for collection and execution."""
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    # FIX: ratchet/dump vars belong to one exact subprocess; tests that spawn
    # fake lane projects inside a real lane must not inherit them.
    environment.pop("UMS_CI_LANE_ITEM_COUNT", None)
    environment.pop("UMS_CI_LANE_NODEID_SHA256", None)
    environment.pop("UMS_CI_LANE_NODEID_DUMP", None)
    if lane == "fast":
        environment.pop(POSTGRES_URL_ENV, None)
    # FIX: the host TMPDIR (/tmp is 01777) breaks tests exercising the storage
    # ancestor boundary. The lane needs a private temp root whose ancestors are
    # all operator-owned; it must live outside the worktree, otherwise fake
    # pytest projects built under tmp_path would adopt this repository's
    # pyproject.toml as their rootdir and emit non-canonical node IDs.
    lane_tmp = _lane_tmp_root(environment)
    lane_tmp.mkdir(mode=0o700, exist_ok=True)
    environment["TMPDIR"] = str(lane_tmp)
    environment["UMS_CI_PYTEST_LANE"] = lane
    environment["UMS_CI_PROJECT_ROOT"] = str(project_root)
    environment["UMS_CI_SELECTED_TEST_MODULES"] = json.dumps(
        [path.as_posix() for path in getattr(partition, lane)]
    )
    environment["UMS_CI_DATABASE_TEST_MODULES"] = json.dumps(
        [path.as_posix() for path in partition.database]
    )
    environment["UMS_CI_DATABASE_SUPPORT_MODULES"] = json.dumps(
        [path.as_posix() for path in partition.database_support]
    )
    # FIX: the node-ID ratchet must hold inside the execution process, not only
    # the collection subprocess that produced it; a mutable collection between
    # the two runs would otherwise drop ratcheted items and still exit green.
    if expected_collection is not None:
        expected_count, expected_digest = expected_collection
        environment["UMS_CI_LANE_ITEM_COUNT"] = str(expected_count)
        environment["UMS_CI_LANE_NODEID_SHA256"] = expected_digest
    if nodeid_dump is not None:
        environment["UMS_CI_LANE_NODEID_DUMP"] = str(nodeid_dump)
    source_root = str(PROJECT_ROOT.resolve())
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root
        if not existing_pythonpath
        else os.pathsep.join((source_root, existing_pythonpath))
    )
    return environment


def _pytest_command(
    selected: Sequence[Path],
    project_root: Path,
    *,
    collect_only: bool,
) -> list[str]:
    """Build guarded pytest argv with exact absolute lane module paths."""
    command = [sys.executable, "-m", "pytest", "-p", GUARD_PLUGIN]
    if collect_only:
        command.append("--collect-only")
    command.extend(("-q", *(str((project_root / path).resolve()) for path in selected)))
    return command


def _canonical_collected_nodeid(raw_line: str, project_root: Path) -> tuple[str, str] | None:
    """Normalize only a collected node's module path, preserving its parameter ID."""
    if "::" not in raw_line:
        return None
    raw_nodeid = raw_line.strip()
    raw_module, _, suffix = raw_nodeid.partition("::")
    module = raw_module.replace("\\", "/")
    module_path = Path(raw_module)
    if module_path.is_absolute():
        try:
            module = module_path.resolve().relative_to(project_root).as_posix()
        except ValueError:
            return None
    return module, f"{module}::{suffix}"


def _expected_lane_manifest(lane: str) -> tuple[int, str]:
    """Return the ratcheted item count and node-id digest for one lane."""
    if lane == "fast":
        return EXPECTED_FAST_ITEM_COUNT, EXPECTED_FAST_NODEID_SHA256
    return EXPECTED_DATABASE_ITEM_COUNT, EXPECTED_DATABASE_NODEID_SHA256


def _validate_collected_lane(
    partition: TestPartition,
    lane: str,
    project_root: Path,
) -> tuple[int, str]:
    """Collect one exact lane and ratchet its independently observed node IDs."""
    selected: Sequence[Path] = getattr(partition, lane)
    nodeid_dump = _lane_tmp_root(os.environ) / f"nodeids-{lane}.txt"
    nodeid_dump.unlink(missing_ok=True)
    completed = subprocess.run(
        _pytest_command(selected, project_root, collect_only=True),
        cwd=project_root,
        env=_pytest_environment(partition, lane, project_root, nodeid_dump=nodeid_dump),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        details = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        raise PartitionError(
            f"pytest {lane} lane collection failed while validating the authoritative partition"
            + (f":\n{details}" if details else "")
        )
    if not nodeid_dump.exists():
        raise PartitionError(
            f"pytest {lane} lane collection produced no node-ID dump at {nodeid_dump}"
        )

    # Node IDs can embed newlines inside parameter IDs; only the in-process
    # dump produced by the lane guard carries the exact collected set.
    nodeids = nodeid_dump.read_text(encoding="utf-8").splitlines()
    expected_modules = {path.as_posix() for path in selected}
    collected_modules: set[str] = set()
    for nodeid in nodeids:
        module, _, _ = nodeid.partition("::")
        module = module.replace("\\", "/")
        module_path = Path(module)
        if module_path.is_absolute():
            try:
                module = module_path.resolve().relative_to(project_root).as_posix()
            except ValueError:
                continue
        if module in expected_modules:
            collected_modules.add(module)

    missing = sorted(expected_modules - collected_modules)
    if missing:
        rendered = "\n".join(f"  - {module}" for module in missing)
        raise PartitionError(f"assigned pytest module(s) collected no test items:\n{rendered}")

    digest = hashlib.sha256("\n".join(sorted(nodeids)).encode("utf-8")).hexdigest()
    if project_root != PROJECT_ROOT.resolve():
        return len(nodeids), digest
    expected_count, expected_digest = _expected_lane_manifest(lane)
    if len(nodeids) != expected_count or digest != expected_digest:
        raise PartitionError(
            f"collected pytest {lane} lane manifest changed: "
            f"count={len(nodeids)} sha256={digest}; expected "
            f"count={expected_count} sha256={expected_digest}. Review the node-ID "
            "or lane-assignment change and update the ratchet deliberately."
        )
    return len(nodeids), digest


def check_partition(project_root: Path = PROJECT_ROOT) -> int:
    """Validate each exact lane independently, then summarize the partition."""
    resolved_root = project_root.resolve()
    partition = build_test_partition(resolved_root)
    for lane in ("fast", "database"):
        _validate_collected_lane(partition, lane, resolved_root)
    print(
        "pytest partition OK: "
        f"{len(partition.fast)} no-database module(s), "
        f"{len(partition.database)} database module(s)"
    )
    return 0


def run_lane(lane: str, project_root: Path = PROJECT_ROOT) -> int:
    """Run exactly one independently collected lane through guarded pytest."""
    resolved_root = project_root.resolve()
    partition = build_test_partition(resolved_root)
    selected: Sequence[Path] = getattr(partition, lane)
    expected_collection = _validate_collected_lane(partition, lane, resolved_root)
    completed = subprocess.run(
        _pytest_command(selected, resolved_root, collect_only=False),
        cwd=resolved_root,
        env=_pytest_environment(
            partition, lane, resolved_root, expected_collection=expected_collection
        ),
        check=False,
    )
    return completed.returncode


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
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
