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
import ast
import hashlib
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
EXPECTED_COLLECTED_ITEM_COUNT: Final = 2919
EXPECTED_COLLECTED_NODEID_SHA256: Final = (
    "9fd700d17551ea30175e9ff4466bd3d5266763c92f50627de242d714f78c9687"
)

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
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PartitionError(f"cannot inspect pytest support file {path}: {exc}") from exc
    return any(sentinel in source for sentinel in REAL_POSTGRES_SENTINELS)


def _module_scope_statements(body: Sequence[ast.stmt]) -> tuple[ast.stmt, ...]:
    """Return import-time statements without descending into functions/classes."""

    statements: list[ast.stmt] = []
    pending = list(body)
    while pending:
        statement = pending.pop(0)
        statements.append(statement)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field_name in ("body", "orelse", "finalbody"):
            nested = getattr(statement, field_name, ())
            if isinstance(nested, list):
                pending.extend(node for node in nested if isinstance(node, ast.stmt))
        handlers = getattr(statement, "handlers", ())
        for handler in handlers:
            pending.extend(handler.body)
        cases = getattr(statement, "cases", ())
        for case in cases:
            pending.extend(case.body)
    return tuple(statements)


def _plugin_literals(value: ast.expr, source_path: Path) -> tuple[str, ...]:
    """Resolve a literal pytest_plugins value or fail closed on computation."""

    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return (value.value,)
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        modules: list[str] = []
        for element in value.elts:
            modules.extend(_plugin_literals(element, source_path))
        return tuple(modules)
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        return _plugin_literals(value.left, source_path) + _plugin_literals(
            value.right, source_path
        )
    raise PartitionError(
        f"{source_path} computes pytest_plugins dynamically; "
        "CI cannot prove its fixture database requirements"
    )


def _pytest_plugin_modules(source_path: Path) -> tuple[str, ...]:
    """Return literal import-time pytest_plugins declarations from one source."""

    try:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise PartitionError(f"cannot inspect pytest support file {source_path}: {exc}") from exc

    modules: list[str] = []
    for statement in _module_scope_statements(tree.body):
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and isinstance(statement.value.func.value, ast.Name)
            and statement.value.func.value.id == "pytest_plugins"
        ):
            call = statement.value
            if call.keywords or call.func.attr not in {"append", "extend"} or len(call.args) != 1:
                raise PartitionError(
                    f"{source_path} mutates pytest_plugins dynamically; "
                    "CI cannot prove its fixture database requirements"
                )
            modules.extend(_plugin_literals(call.args[0], source_path))
            continue
        value: ast.expr | None = None
        targets: Sequence[ast.expr] = ()
        if isinstance(statement, ast.Assign):
            value = statement.value
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            value = statement.value
            targets = (statement.target,)
        elif isinstance(statement, ast.AugAssign):
            value = statement.value
            targets = (statement.target,)
        targets_plugins = any(
            isinstance(target, ast.Name) and target.id == "pytest_plugins" for target in targets
        )
        mutates_plugin_member = any(
            not isinstance(target, ast.Name)
            and any(
                isinstance(node, ast.Name) and node.id == "pytest_plugins"
                for node in ast.walk(target)
            )
            for target in targets
        )
        if mutates_plugin_member:
            raise PartitionError(
                f"{source_path} mutates pytest_plugins dynamically; "
                "CI cannot prove its fixture database requirements"
            )
        if value is None or not targets_plugins:
            continue
        modules.extend(_plugin_literals(value, source_path))
    return tuple(dict.fromkeys(modules))


def _absolute_import_name(
    source_path: Path,
    project_root: Path,
    module: str | None,
    level: int,
) -> str | None:
    """Resolve one Python import name without importing test support code."""

    if level == 0:
        return module
    try:
        relative_source = source_path.resolve().relative_to(project_root.resolve())
    except ValueError as exc:
        raise PartitionError(f"pytest support file escapes project root: {source_path}") from exc
    package = list(relative_source.with_suffix("").parts[:-1])
    keep = len(package) - (level - 1)
    if keep < 0:
        raise PartitionError(f"invalid relative import in pytest support file {source_path}")
    parts = package[:keep]
    if module:
        parts.extend(module.split("."))
    return ".".join(parts) or None


def _resolve_project_support_import(
    project_root: Path,
    source_path: Path,
    module: str,
) -> Path | None:
    """Resolve an imported project module without allowing path escape."""

    relative = Path(*module.split("."))
    candidates = [
        project_root / relative.with_suffix(".py"),
        project_root / relative / "__init__.py",
    ]
    if "." not in module:
        candidates.extend(
            (
                source_path.parent / relative.with_suffix(".py"),
                source_path.parent / relative / "__init__.py",
            )
        )
    resolved_root = project_root.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and (
            resolved == resolved_root or resolved.is_relative_to(resolved_root)
        ):
            return resolved
    return None


def _project_support_imports(source_path: Path, project_root: Path) -> tuple[Path, ...]:
    """Return project modules imported at module scope by pytest support."""

    try:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise PartitionError(f"cannot inspect pytest support file {source_path}: {exc}") from exc

    imported: list[Path] = []
    for statement in _module_scope_statements(tree.body):
        module_names: list[str] = []
        if isinstance(statement, ast.Import):
            module_names.extend(alias.name for alias in statement.names)
        elif isinstance(statement, ast.ImportFrom):
            base = _absolute_import_name(
                source_path, project_root, statement.module, statement.level
            )
            if base:
                module_names.append(base)
                module_names.extend(
                    f"{base}.{alias.name}" for alias in statement.names if alias.name != "*"
                )
        for module_name in module_names:
            resolved = _resolve_project_support_import(project_root, source_path, module_name)
            if resolved is not None:
                imported.append(resolved)
    return tuple(dict.fromkeys(imported))


def _resolve_project_plugin(project_root: Path, module: str) -> Path:
    """Resolve a project-local pytest plugin module without importing it."""

    relative = Path(*module.split("."))
    candidates = (
        project_root / relative.with_suffix(".py"),
        project_root / relative / "__init__.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise PartitionError(
        f"pytest plugin {module!r} is not a readable project module; "
        "CI cannot classify its database requirements"
    )


def _applicable_conftests(test_path: Path, project_root: Path) -> tuple[Path, ...]:
    """Return every conftest inherited by a test module, root first."""

    resolved_root = project_root.resolve()
    current = test_path.resolve().parent
    directories: list[Path] = []
    while current == resolved_root or current.is_relative_to(resolved_root):
        directories.append(current)
        if current == resolved_root:
            break
        current = current.parent
    return tuple(
        conftest
        for directory in reversed(directories)
        if (conftest := directory / "conftest.py").is_file()
    )


def _pytest_support_files(test_path: Path, project_root: Path) -> tuple[Path, ...]:
    """Resolve applicable conftests and their transitive project plugin closure."""

    pending = [test_path, *_applicable_conftests(test_path, project_root)]
    support_files: list[Path] = []
    seen: set[Path] = set()
    while pending:
        source_path = pending.pop(0).resolve()
        if source_path in seen:
            continue
        seen.add(source_path)
        support_files.append(source_path)
        pending.extend(
            _resolve_project_plugin(project_root, module)
            for module in _pytest_plugin_modules(source_path)
        )
        pending.extend(_project_support_imports(source_path, project_root))
    return tuple(support_files)


def _fixture_support_uses_real_postgres(test_path: Path, project_root: Path) -> bool:
    """Return whether inherited fixture/plugin support requires real Postgres."""

    return any(
        support_path != test_path.resolve() and _uses_real_postgres(support_path)
        for support_path in _pytest_support_files(test_path, project_root)
    )


def build_test_partition(project_root: Path = PROJECT_ROOT) -> TestPartition:
    """Build a complete partition and fail if Postgres coverage could escape."""

    fast: list[Path] = []
    database: list[Path] = []
    leaked_postgres: list[Path] = []

    for path in discover_test_modules(project_root):
        relative_path = _relative_to_project(path, project_root)
        if belongs_to_database_lane(relative_path):
            database.append(relative_path)
        elif _uses_real_postgres(path):
            fast.append(relative_path)
            leaked_postgres.append(relative_path)
        elif _fixture_support_uses_real_postgres(path, project_root):
            database.append(relative_path)
        else:
            fast.append(relative_path)

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
    """Validate assignment and collection, then summarize the partition."""

    resolved_root = project_root.resolve()
    partition = build_test_partition(resolved_root)
    _validate_collected_items(partition, resolved_root)
    print(
        "pytest partition OK: "
        f"{len(partition.fast)} no-database module(s), "
        f"{len(partition.database)} database module(s)"
    )
    return 0


def _validate_collected_items(partition: TestPartition, project_root: Path) -> None:
    """Prove every assigned module collects tests and ratchet canonical node IDs."""

    assigned = tuple(
        sorted((*partition.fast, *partition.database), key=lambda path: path.as_posix())
    )
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            *(str(project_root / path) for path in assigned),
        ],
        cwd=project_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        details = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        raise PartitionError(
            "pytest collection failed while validating the authoritative partition"
            + (f":\n{details}" if details else "")
        )

    expected_modules = {path.as_posix() for path in assigned}
    nodeids: list[str] = []
    collected_modules: set[str] = set()
    for raw_line in completed.stdout.splitlines():
        if "::" not in raw_line:
            continue
        nodeid = raw_line.strip().replace("\\", "/")
        raw_module, _, suffix = nodeid.partition("::")
        module = raw_module
        module_path = Path(raw_module)
        if module_path.is_absolute():
            try:
                module = module_path.resolve().relative_to(project_root).as_posix()
            except ValueError:
                continue
            nodeid = f"{module}::{suffix}"
        if module not in expected_modules:
            continue
        nodeids.append(nodeid)
        collected_modules.add(module)

    missing = sorted(expected_modules - collected_modules)
    if missing:
        rendered = "\n".join(f"  - {module}" for module in missing)
        raise PartitionError(f"assigned pytest module(s) collected no test items:\n{rendered}")

    if project_root == PROJECT_ROOT.resolve():
        digest = hashlib.sha256("\n".join(sorted(nodeids)).encode("utf-8")).hexdigest()
        if (
            len(nodeids) != EXPECTED_COLLECTED_ITEM_COUNT
            or digest != EXPECTED_COLLECTED_NODEID_SHA256
        ):
            raise PartitionError(
                "collected pytest item manifest changed: "
                f"count={len(nodeids)} sha256={digest}; expected "
                f"count={EXPECTED_COLLECTED_ITEM_COUNT} "
                f"sha256={EXPECTED_COLLECTED_NODEID_SHA256}. Review the node-ID change "
                "and update the ratchet deliberately."
            )


def run_lane(lane: str, project_root: Path = PROJECT_ROOT) -> int:
    """Run exactly one validated lane through the locked pytest installation."""

    resolved_root = project_root.resolve()
    partition = build_test_partition(resolved_root)
    _validate_collected_items(partition, resolved_root)
    selected: Sequence[Path] = getattr(partition, lane)
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    # FIX: Run pytest from the requested project root with absolute inputs; the
    # previous relative argv silently depended on the caller already being there.
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *(str(resolved_root / path) for path in selected),
        ],
        cwd=resolved_root,
        env=environment,
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


if __name__ == "__main__":
    raise SystemExit(main())
