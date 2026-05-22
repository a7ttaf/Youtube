"""Fail-closed policy checks for the pytest suite."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = "tests"
PYTEST_COLLECTED_FILE_PATTERNS = ("test_*.py", "*_test.py", "conftest.py")
TRACKED_IMPORT_ROOTS = frozenset({"pytest", "unittest"})
FORBIDDEN_SYMBOLS = frozenset(
    {
        "pytest.mark.skip",
        "pytest.mark.skipif",
        "pytest.mark.xfail",
        "pytest.importorskip",
        "pytest.skip",
        "pytest.xfail",
        "unittest.skip",
        "unittest.skipIf",
        "unittest.skipUnless",
    }
)


@dataclass(frozen=True)
class TestPolicyViolation:
    """A pytest policy violation found in a test file."""

    relative_path: Path
    line: int
    symbol: str


# ============================================================================
# Purpose: Enforce the repository rule that tests are never skipped or xfailed
# to make validation pass across pytest-collected test modules and conftests.
# Database/ORM: None.
# Standards: AST scanning, import-alias resolution, deterministic reporting.
# Blast Radius: Test validation gate only.
# Connections:
#   - File: AGENTS.md -> Enforces "Never skip, xfail, delete, or loosen tests".
#   - File: backend/ums_smart_revenue/devtools/quality_gate.py -> Runs this
#     policy before the full pytest suite.
# ============================================================================
def find_policy_violations(
    project_root: Path = PROJECT_ROOT,
) -> tuple[TestPolicyViolation, ...]:
    """Return all forbidden skip/xfail policy violations under tests/."""
    tests_root = project_root / TESTS_DIR
    if not tests_root.exists():
        return ()

    violations: list[TestPolicyViolation] = []
    for path in _iter_policy_files(tests_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(project_root)
        violations.extend(
            _violations_in_tree(tree, relative_path, _import_aliases(tree))
        )
    return tuple(violations)


def run_policy_gate(project_root: Path = PROJECT_ROOT) -> int:
    """Print policy violations and return a shell-friendly exit code."""
    violations = find_policy_violations(project_root)
    if not violations:
        return 0

    print(
        f"Forbidden pytest skip/xfail policy violations: {len(violations)}",
        file=sys.stderr,
    )
    for violation in violations:
        print(
            f"{violation.relative_path.as_posix()}:{violation.line}: "
            f"{violation.symbol}",
            file=sys.stderr,
        )
    return 1


def _violations_in_tree(
    tree: ast.AST, relative_path: Path, import_aliases: dict[str, str]
) -> tuple[TestPolicyViolation, ...]:
    violations: list[TestPolicyViolation] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            violations.extend(
                _decorator_violations(
                    node.decorator_list, relative_path, import_aliases
                )
            )
        elif isinstance(node, ast.Call):
            symbol = _qualified_name(node.func, import_aliases)
            if symbol in FORBIDDEN_SYMBOLS:
                violations.append(
                    TestPolicyViolation(
                        relative_path=relative_path,
                        line=node.lineno,
                        symbol=symbol,
                    )
                )

    return _dedupe_violations(violations)


def _decorator_violations(
    decorators: list[ast.expr],
    relative_path: Path,
    import_aliases: dict[str, str],
) -> tuple[TestPolicyViolation, ...]:
    violations: list[TestPolicyViolation] = []
    for decorator in decorators:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        symbol = _qualified_name(target, import_aliases)
        if symbol in FORBIDDEN_SYMBOLS:
            violations.append(
                TestPolicyViolation(
                    relative_path=relative_path,
                    line=decorator.lineno,
                    symbol=symbol,
                )
            )
    return tuple(violations)


def _iter_policy_files(tests_root: Path) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for pattern in PYTEST_COLLECTED_FILE_PATTERNS:
        paths.update(tests_root.rglob(pattern))
    return tuple(sorted(paths))


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.partition(".")[0]
                if root not in TRACKED_IMPORT_ROOTS:
                    continue
                local_name = alias.asname or root
                aliases[local_name] = alias.name if alias.asname else root
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.partition(".")[0]
            if root not in TRACKED_IMPORT_ROOTS:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                aliases[local_name] = f"{node.module}.{alias.name}"

    return aliases


def _qualified_name(node: ast.AST, import_aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return import_aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value, import_aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _dedupe_violations(
    violations: list[TestPolicyViolation],
) -> tuple[TestPolicyViolation, ...]:
    unique = {
        (violation.relative_path, violation.line, violation.symbol): violation
        for violation in violations
    }
    return tuple(
        sorted(unique.values(), key=lambda item: (item.relative_path, item.line))
    )


def main() -> int:
    """CLI entry point for the pytest policy gate."""
    return run_policy_gate()


if __name__ == "__main__":
    raise SystemExit(main())
