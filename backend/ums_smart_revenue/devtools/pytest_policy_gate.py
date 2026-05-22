"""Fail-closed policy checks for the pytest suite."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = "tests"
PYTEST_COLLECTED_FILE_PATTERNS = ("test_*.py", "*_test.py", "conftest.py", "__init__.py")
TRACKED_IMPORT_ROOTS = frozenset({"pytest", "unittest"})
FORBIDDEN_SYMBOLS = frozenset(
    {
        "pytest.mark.skip",
        "pytest.mark.skipif",
        "pytest.mark.xfail",
        "pytest.importorskip",
        "pytest.skip",
        "pytest.xfail",
        "self.skipTest",
        "super.skipTest",
        "unittest.skip",
        "unittest.SkipTest",
        "unittest.case.skip",
        "unittest.case.SkipTest",
        "unittest.case.skipIf",
        "unittest.case.skipUnless",
        "unittest.case.expectedFailure",
        "unittest.TestCase.skipTest",
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
        symbol_aliases = _symbol_aliases(tree)
        str_aliases = _string_constant_aliases(tree)
        violations.extend(
            _violations_in_tree(tree, relative_path, symbol_aliases, str_aliases)
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
    tree: ast.AST,
    relative_path: Path,
    import_aliases: dict[str, str],
    string_aliases: dict[str, str],
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
            _check_getattr_indirection(
                violations, node, relative_path, import_aliases, string_aliases
            )
        elif isinstance(node, ast.Attribute | ast.Name):
            _append_violation_if_forbidden(
                violations, node, relative_path, import_aliases
            )

    return _dedupe_violations(violations)


def _check_getattr_indirection(
    violations: list[TestPolicyViolation],
    call: ast.Call,
    relative_path: Path,
    import_aliases: dict[str, str],
    string_aliases: dict[str, str],
) -> None:
    if not (
        isinstance(call.func, ast.Name)
        and call.func.id == "getattr"
        and len(call.args) >= 2
    ):
        return
    module_name = _qualified_name(call.args[0], import_aliases)
    attr_arg = call.args[1]
    if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):
        full_symbol = f"{module_name}.{attr_arg.value}"
        if full_symbol in FORBIDDEN_SYMBOLS:
            violations.append(
                TestPolicyViolation(
                    relative_path=relative_path,
                    line=call.lineno,
                    symbol=full_symbol,
                )
            )
    elif isinstance(attr_arg, ast.Name):
        leaf_value = string_aliases.get(attr_arg.id)
        if leaf_value:
            full_symbol = f"{module_name}.{leaf_value}"
            if full_symbol in FORBIDDEN_SYMBOLS:
                violations.append(
                    TestPolicyViolation(
                        relative_path=relative_path,
                        line=call.lineno,
                        symbol=full_symbol,
                    )
                )


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
                    _expand_wildcard(aliases, node.module, root)
                    continue
                local_name = alias.asname or alias.name
                full_form = f"{node.module}.{alias.name}"
                canonical_form = f"{root}.{alias.name}"
                if full_form in FORBIDDEN_SYMBOLS:
                    aliases[local_name] = full_form
                elif canonical_form in FORBIDDEN_SYMBOLS:
                    aliases[local_name] = canonical_form
                else:
                    aliases[local_name] = full_form

    return aliases


def _expand_wildcard(aliases: dict[str, str], module: str, root: str) -> None:
    """Expand a starred import from a tracked root into known forbidden symbols."""
    for symbol in FORBIDDEN_SYMBOLS:
        if symbol.startswith(f"{module}."):
            leaf = symbol[len(module) + 1 :]
            aliases[leaf] = symbol
            if "." in leaf:
                first = leaf.split(".", 1)[0]
                aliases[first] = f"{module}.{first}"
        elif symbol.startswith(f"{root}."):
            _add_wildcard_alias(aliases, root, symbol)


def _add_wildcard_alias(
    aliases: dict[str, str], imported_module: str, symbol: str
) -> None:
    leaf = symbol[len(imported_module) + 1 :]
    aliases.setdefault(leaf, symbol)
    if "." in leaf:
        first = leaf.split(".", 1)[0]
        aliases.setdefault(first, f"{imported_module}.{first}")
    if "." in leaf:
        first = leaf.split(".", 1)[0]
        aliases.setdefault(first, f"{imported_module}.{first}")


def _resolve_string_constant(value: ast.AST) -> str | None:
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    for symbol in FORBIDDEN_SYMBOLS:
        if symbol.rsplit(".", 1)[-1] == value.value:
            return value.value
    return None


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


def _string_constant_aliases(tree: ast.AST) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in ast.walk(tree):
        value = _assignment_value(node)
        if value is None:
            continue
        resolved = _resolve_string_constant(value)
        if not resolved:
            continue
        for target in _assignment_targets(node):
            result[target.id] = resolved
    return result


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Assign | ast.NamedExpr):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _assignment_targets(node: ast.AST) -> tuple[ast.Name, ...]:
    def _collect(target: ast.AST) -> tuple[ast.Name, ...]:
        if isinstance(target, ast.Name):
            return (target,)
        if isinstance(target, ast.Tuple | ast.List):
            names: list[ast.Name] = []
            for elt in target.elts:
                names.extend(_collect(elt))
            return tuple(names)
        return ()
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
        targets = [node.target]
    else:
        return ()
    result: list[ast.Name] = []
    for target in targets:
        result.extend(_collect(target))
    return tuple(result)


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
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "super":
            return "super"
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
