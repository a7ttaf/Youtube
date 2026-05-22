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
        "unittest.expectedFailure",
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
# Standards: AST scanning, import/local-alias resolution, deterministic reporting.
# Blast Radius: Test validation gate only.
# Connections:
#   - File: AGENTS.md -> Enforces "Never skip, xfail, delete, or loosen tests".
#   - File: backend/ums_smart_revenue/devtools/quality_gate.py -> Runs this
#     policy before the full pytest suite.
# ============================================================================
def find_policy_violations(
    project_root: Path = PROJECT_ROOT,
) -> tuple[TestPolicyViolation, ...]:
    """Return all forbidden skip/xfail policy violations in pytest inputs."""
    violations: list[TestPolicyViolation] = []
    for path in _iter_policy_files(project_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(project_root)
        violations.extend(
            _violations_in_tree(tree, relative_path, _symbol_aliases(tree))
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
        if isinstance(node, ast.Call):
            _append_violation_if_forbidden(
                violations, node.func, relative_path, import_aliases
            )
        elif isinstance(node, ast.Attribute | ast.Name):
            _append_violation_if_forbidden(
                violations, node, relative_path, import_aliases
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
        _append_violation_if_forbidden(
            violations, target, relative_path, import_aliases
        )
    return tuple(violations)


def _append_violation_if_forbidden(
    violations: list[TestPolicyViolation],
    target: ast.AST,
    relative_path: Path,
    import_aliases: dict[str, str],
) -> None:
    symbol = _qualified_name(target, import_aliases)
    line = getattr(target, "lineno", None)
    if symbol in FORBIDDEN_SYMBOLS and isinstance(line, int):
        violations.append(
            TestPolicyViolation(
                relative_path=relative_path,
                line=line,
                symbol=symbol,
            )
        )


def _iter_policy_files(project_root: Path) -> tuple[Path, ...]:
    paths: set[Path] = set()
    tests_root = project_root / TESTS_DIR
    if tests_root.exists():
        for pattern in PYTEST_COLLECTED_FILE_PATTERNS:
            paths.update(tests_root.rglob(pattern))
    root_conftest = project_root / "conftest.py"
    if root_conftest.exists():
        paths.add(root_conftest)
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


def _symbol_aliases(tree: ast.AST) -> dict[str, str]:
    aliases = _import_aliases(tree)
    changed = True

    while changed:
        changed = False
        for node in ast.walk(tree):
            value = _assignment_value(node)
            if value is None:
                continue

            symbol = _qualified_name(value, aliases)
            if not _is_policy_symbol_or_prefix(symbol):
                continue

            for target in _assignment_targets(node):
                if aliases.get(target.id) == symbol:
                    continue
                aliases[target.id] = symbol
                changed = True

    return aliases


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Assign | ast.NamedExpr):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _assignment_targets(node: ast.AST) -> tuple[ast.Name, ...]:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
        targets = [node.target]
    else:
        return ()
    return tuple(target for target in targets if isinstance(target, ast.Name))


def _is_policy_symbol_or_prefix(symbol: str) -> bool:
    return any(
        forbidden == symbol or forbidden.startswith(f"{symbol}.")
        for forbidden in FORBIDDEN_SYMBOLS
    )


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
