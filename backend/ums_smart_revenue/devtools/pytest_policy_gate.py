"""Fail-closed policy checks for the pytest suite."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = "tests"
FORBIDDEN_SYMBOLS = frozenset(
    {
        "pytest.mark.skip",
        "pytest.mark.skipif",
        "pytest.mark.xfail",
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
# to make validation pass.
# Database/ORM: None.
# Standards: AST-based scanning, deterministic ordering, fail-closed reporting.
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
    for path in sorted(tests_root.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(project_root)
        violations.extend(_violations_in_tree(tree, relative_path))
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
    tree: ast.AST, relative_path: Path
) -> tuple[TestPolicyViolation, ...]:
    violations: list[TestPolicyViolation] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            violations.extend(
                _decorator_violations(node.decorator_list, relative_path)
            )
        elif isinstance(node, ast.Call):
            symbol = _qualified_name(node.func)
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
    decorators: list[ast.expr], relative_path: Path
) -> tuple[TestPolicyViolation, ...]:
    violations: list[TestPolicyViolation] = []
    for decorator in decorators:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        symbol = _qualified_name(target)
        if symbol in FORBIDDEN_SYMBOLS:
            violations.append(
                TestPolicyViolation(
                    relative_path=relative_path,
                    line=decorator.lineno,
                    symbol=symbol,
                )
            )
    return tuple(violations)


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
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
