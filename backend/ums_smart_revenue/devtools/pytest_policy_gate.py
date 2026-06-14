"""Fail-closed policy checks for the pytest suite."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = "tests"
PYTEST_PLUGINS_NAME = "pytest_plugins"
GETATTR_SYMBOLS = frozenset({"getattr", "builtins.getattr"})
TRACKED_IMPORT_ROOTS = frozenset({"builtins", "pytest", "unittest"})
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


@dataclass
class _ScopeContext:
    """Static aliases visible from the current Python scope."""

    aliases: dict[str, str]
    string_aliases: dict[str, str]
    attr_string_aliases: dict[str, str]
    getattr_arg_aliases: dict[str, tuple[str, str]]
    shadowed_getattr_names: set[str]

    def child(self) -> _ScopeContext:
        """Return a child scope that can be narrowed without mutating parents."""
        return _ScopeContext(
            aliases=dict(self.aliases),
            string_aliases=dict(self.string_aliases),
            attr_string_aliases=dict(self.attr_string_aliases),
            getattr_arg_aliases=dict(self.getattr_arg_aliases),
            shadowed_getattr_names=set(self.shadowed_getattr_names),
        )


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
            f"{violation.relative_path.as_posix()}:{violation.line}: {violation.symbol}",
            file=sys.stderr,
        )
    return 1


def _violations_in_tree(
    tree: ast.AST,
    relative_path: Path,
) -> tuple[TestPolicyViolation, ...]:
    violations: list[TestPolicyViolation] = []
    context = _ScopeContext({}, {}, {}, {}, set())
    if isinstance(tree, ast.Module):
        _scan_statement_body(tree.body, relative_path, context, violations)
    return _dedupe_violations(violations)


def _scan_statement_body(
    body: list[ast.stmt],
    relative_path: Path,
    context: _ScopeContext,
    violations: list[TestPolicyViolation],
) -> None:
    for statement in body:
        _scan_statement(statement, relative_path, context, violations)


def _scan_statement(
    statement: ast.stmt,
    relative_path: Path,
    context: _ScopeContext,
    violations: list[TestPolicyViolation],
) -> None:
    if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
        violations.extend(_decorator_violations(statement.decorator_list, relative_path, context))
        _shadow_local_name(context, statement.name)
        child_context = _child_context_for_function(context, statement)
        _scan_statement_body(statement.body, relative_path, child_context, violations)
        return

    if isinstance(statement, ast.ClassDef):
        violations.extend(_decorator_violations(statement.decorator_list, relative_path, context))
        _record_class_string_aliases(statement, context)
        _shadow_local_name(context, statement.name)
        child_context = _child_context_for_body(context, statement.body)
        _scan_statement_body(statement.body, relative_path, child_context, violations)
        return

    if isinstance(statement, ast.If):
        _scan_node_for_violations(statement.test, relative_path, context, violations)
        _scan_statement_body(statement.body, relative_path, context, violations)
        _scan_statement_body(statement.orelse, relative_path, context, violations)
        return

    if isinstance(statement, ast.For | ast.AsyncFor):
        _scan_node_for_violations(statement.iter, relative_path, context, violations)
        _scan_node_for_violations(statement.target, relative_path, context, violations)
        _scan_statement_body(statement.body, relative_path, context, violations)
        _scan_statement_body(statement.orelse, relative_path, context, violations)
        return

    if isinstance(statement, ast.While):
        _scan_node_for_violations(statement.test, relative_path, context, violations)
        _scan_statement_body(statement.body, relative_path, context, violations)
        _scan_statement_body(statement.orelse, relative_path, context, violations)
        return

    if isinstance(statement, ast.With | ast.AsyncWith):
        for item in statement.items:
            _scan_node_for_violations(item.context_expr, relative_path, context, violations)
            if item.optional_vars is not None:
                _scan_node_for_violations(item.optional_vars, relative_path, context, violations)
        _scan_statement_body(statement.body, relative_path, context, violations)
        return

    if isinstance(statement, ast.Try):
        _scan_statement_body(statement.body, relative_path, context, violations)
        for handler in statement.handlers:
            if handler.type is not None:
                _scan_node_for_violations(handler.type, relative_path, context, violations)
            _scan_statement_body(handler.body, relative_path, context, violations)
        _scan_statement_body(statement.orelse, relative_path, context, violations)
        _scan_statement_body(statement.finalbody, relative_path, context, violations)
        return

    if isinstance(statement, ast.Match):
        _scan_node_for_violations(statement.subject, relative_path, context, violations)
        for case in statement.cases:
            if case.guard is not None:
                _scan_node_for_violations(case.guard, relative_path, context, violations)
            _scan_statement_body(case.body, relative_path, context, violations)
        return

    _scan_node_for_violations(statement, relative_path, context, violations)
    _update_context_from_statement(context, statement)


def _scan_node_for_violations(
    node: ast.AST,
    relative_path: Path,
    context: _ScopeContext,
    violations: list[TestPolicyViolation],
) -> None:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            _append_violation_if_forbidden(violations, child.func, relative_path, context)
            _check_getattr_indirection(violations, child, relative_path, context)
        elif isinstance(child, ast.Attribute | ast.Name):
            _append_violation_if_forbidden(violations, child, relative_path, context)


def _child_context_for_function(
    context: _ScopeContext,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> _ScopeContext:
    child = _child_context_for_body(context, function.body)
    for arg in _function_argument_names(function):
        _shadow_local_name(child, arg)
    return child


def _child_context_for_body(
    context: _ScopeContext,
    body: list[ast.stmt],
) -> _ScopeContext:
    child = context.child()
    for name in _local_binding_names(body):
        _shadow_local_name(child, name)
    return child


def _function_argument_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ...]:
    args = function.args
    names = [arg.arg for arg in args.posonlyargs + args.args + args.kwonlyargs]
    if args.vararg is not None:
        names.append(args.vararg.arg)
    if args.kwarg is not None:
        names.append(args.kwarg.arg)
    return tuple(names)


def _local_binding_names(body: list[ast.stmt]) -> tuple[str, ...]:
    names: set[str] = set()

    def _scan(node: ast.AST) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
            return
        for child in ast.iter_child_nodes(node):
            _scan(child)

    for statement in body:
        _scan(statement)
    return tuple(names)


def _shadow_local_name(context: _ScopeContext, name: str) -> None:
    context.aliases.pop(name, None)
    context.string_aliases.pop(name, None)
    context.getattr_arg_aliases.pop(name, None)
    if name == "getattr":
        context.shadowed_getattr_names.add(name)


def _record_class_string_aliases(
    statement: ast.ClassDef,
    context: _ScopeContext,
) -> None:
    for node in _iter_module_scope_statements(statement.body):
        for key, value in _string_alias_bindings(node):
            resolved = _resolve_string_constant(value)
            if resolved and "." not in key:
                context.attr_string_aliases[f"{statement.name}.{key}"] = resolved


def _update_context_from_statement(context: _ScopeContext, statement: ast.stmt) -> None:
    _update_import_aliases(context, statement)
    _update_assignment_aliases(context, statement)


def _update_import_aliases(context: _ScopeContext, statement: ast.stmt) -> None:
    if isinstance(statement, ast.Import):
        for alias in statement.names:
            root = alias.name.partition(".")[0]
            if root not in TRACKED_IMPORT_ROOTS:
                continue
            local_name = alias.asname or root
            context.aliases[local_name] = alias.name if alias.asname else root
        return

    if not isinstance(statement, ast.ImportFrom) or not statement.module:
        return

    root = statement.module.partition(".")[0]
    if root not in TRACKED_IMPORT_ROOTS:
        return
    for alias in statement.names:
        if alias.name == "*":
            _expand_wildcard(context.aliases, statement.module, root)
            continue
        local_name = alias.asname or alias.name
        full_form = f"{statement.module}.{alias.name}"
        canonical_form = f"{root}.{alias.name}"
        if full_form in FORBIDDEN_SYMBOLS or full_form in GETATTR_SYMBOLS:
            context.aliases[local_name] = full_form
        elif canonical_form in FORBIDDEN_SYMBOLS:
            context.aliases[local_name] = canonical_form
        else:
            context.aliases[local_name] = full_form


def _update_assignment_aliases(
    context: _ScopeContext,
    statement: ast.stmt,
) -> None:
    for target, value in _assignment_bindings(statement):
        _update_symbol_alias(context, target, value)
        _update_getattr_arg_alias(context, target, value)

    for key, value in _string_alias_bindings(statement):
        _update_string_alias(context, key, value)


def _update_symbol_alias(
    context: _ScopeContext,
    target: ast.Name,
    value: ast.AST,
) -> None:
    symbol = _qualified_name(value, context.aliases)
    if symbol in GETATTR_SYMBOLS or _is_policy_symbol_or_prefix(symbol):
        context.aliases[target.id] = symbol
        if target.id == "getattr":
            context.shadowed_getattr_names.discard(target.id)
        return

    context.aliases.pop(target.id, None)
    if target.id == "getattr":
        context.shadowed_getattr_names.add(target.id)


def _update_getattr_arg_alias(
    context: _ScopeContext,
    target: ast.Name,
    value: ast.AST,
) -> None:
    resolved = _resolve_getattr_arg_tuple(value, context)
    if resolved is None:
        context.getattr_arg_aliases.pop(target.id, None)
        return
    context.getattr_arg_aliases[target.id] = resolved


def _update_string_alias(
    context: _ScopeContext,
    key: str,
    value: ast.AST,
) -> None:
    resolved = _resolve_string_constant(value)
    if not resolved:
        if "." in key:
            context.attr_string_aliases.pop(key, None)
        else:
            context.string_aliases.pop(key, None)
        return

    if "." in key:
        context.attr_string_aliases[key] = resolved
    else:
        context.string_aliases[key] = resolved


def _resolve_getattr_arg_tuple(
    value: ast.AST,
    context: _ScopeContext,
) -> tuple[str, str] | None:
    if not isinstance(value, ast.List | ast.Tuple) or len(value.elts) != 2:
        return None
    module_name = _qualified_name(value.elts[0], context.aliases)
    leaf_value = _resolve_attr_leaf(value.elts[1], context)
    if not module_name or not leaf_value:
        return None
    return module_name, leaf_value


def _resolve_attr_leaf(
    value: ast.AST,
    context: _ScopeContext,
) -> str | None:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Name):
        return context.string_aliases.get(value.id)
    if isinstance(value, ast.Attribute):
        return context.attr_string_aliases.get(_qualified_name(value, context.aliases))
    return None


def _check_getattr_indirection(
    violations: list[TestPolicyViolation],
    call: ast.Call,
    relative_path: Path,
    context: _ScopeContext,
) -> None:
    getattr_args = _resolve_getattr_call_args(call, context)
    if getattr_args is None:
        return
    module_name, leaf_value = getattr_args
    full_symbol = f"{module_name}.{leaf_value}"
    if full_symbol not in FORBIDDEN_SYMBOLS:
        return
    violations.append(
        TestPolicyViolation(
            relative_path=relative_path,
            line=call.lineno,
            symbol=full_symbol,
        )
    )


def _resolve_getattr_call_args(
    call: ast.Call,
    context: _ScopeContext,
) -> tuple[str, str] | None:
    if not _is_getattr_call(call, context):
        return None
    if len(call.args) >= 2:
        module_name = _qualified_name(call.args[0], context.aliases)
        leaf_value = _resolve_attr_leaf(call.args[1], context)
        if module_name and leaf_value:
            return module_name, leaf_value
    if len(call.args) == 1 and isinstance(call.args[0], ast.Starred):
        starred = call.args[0].value
        if isinstance(starred, ast.Name):
            return context.getattr_arg_aliases.get(starred.id)
    return None


def _is_getattr_call(call: ast.Call, context: _ScopeContext) -> bool:
    if isinstance(call.func, ast.Name):
        if call.func.id in context.shadowed_getattr_names and call.func.id not in context.aliases:
            return False
    return _qualified_name(call.func, context.aliases) in GETATTR_SYMBOLS


def _decorator_violations(
    decorators: list[ast.expr],
    relative_path: Path,
    context: _ScopeContext,
) -> tuple[TestPolicyViolation, ...]:
    violations: list[TestPolicyViolation] = []
    for decorator in decorators:
        _scan_node_for_violations(decorator, relative_path, context, violations)
    return tuple(violations)


def _append_violation_if_forbidden(
    violations: list[TestPolicyViolation],
    target: ast.AST,
    relative_path: Path,
    context: _ScopeContext,
) -> None:
    symbol = _qualified_name(target, context.aliases)
    line = getattr(target, "lineno", None)
    if symbol in FORBIDDEN_SYMBOLS and isinstance(line, int):
        violations.append(
            TestPolicyViolation(
                relative_path=relative_path,
                line=line,
                symbol=symbol,
            )
        )


# ============================================================================
# Purpose: Find every Python file that can influence pytest collection,
# including conftests and modules named through pytest_plugins declarations.
# Database/ORM: None.
# Standards: Static filesystem resolution only; no plugin imports are executed.
# Blast Radius: Test validation gate only.
# Connections:
#   - File: pyproject.toml -> Pytest collection is constrained to tests/.
#   - File: tests/devtools/test_policy_gate_edge_cases.py -> Covers plugins.
# ============================================================================
def _iter_policy_files(project_root: Path) -> tuple[Path, ...]:
    paths: set[Path] = set()
    tests_root = project_root / TESTS_DIR
    if tests_root.exists():
        paths.update(tests_root.rglob("*.py"))
    root_conftest = project_root / "conftest.py"
    if root_conftest.exists():
        paths.add(root_conftest)
    paths = _include_declared_pytest_plugins(project_root, paths)
    return tuple(sorted(paths))


def _include_declared_pytest_plugins(project_root: Path, initial_paths: set[Path]) -> set[Path]:
    paths = set(initial_paths)
    pending = list(initial_paths)
    while pending:
        path = pending.pop()
        for module in _pytest_plugins_from_file(path):
            plugin_path = _resolve_pytest_plugin_path(project_root, module)
            if plugin_path is None or plugin_path in paths:
                continue
            paths.add(plugin_path)
            pending.append(plugin_path)
    return paths


def _pytest_plugins_from_file(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    plugin_aliases: dict[str, tuple[str, ...]] = {}
    for node in _iter_module_scope_statements(tree.body):
        _update_pytest_plugin_aliases(plugin_aliases, node)
        value = _assignment_value(node)
        if value is not None and _assigns_to_name(node, PYTEST_PLUGINS_NAME):
            modules.extend(_string_literals(value, plugin_aliases))
        modules.extend(_pytest_plugin_mutation_literals(node, plugin_aliases))
    return tuple(modules)


def _update_pytest_plugin_aliases(
    plugin_aliases: dict[str, tuple[str, ...]], node: ast.AST
) -> None:
    # FIX: Preserve aliases for full pytest_plugins sequences, not only one
    # string literal, so backend plugin modules cannot evade static scanning.
    for key, value in _string_alias_bindings(node):
        if "." in key:
            continue
        modules = _string_literals(value, plugin_aliases)
        if modules:
            plugin_aliases[key] = modules
        else:
            plugin_aliases.pop(key, None)


def _pytest_plugin_mutation_literals(
    node: ast.AST, plugin_aliases: dict[str, tuple[str, ...]]
) -> tuple[str, ...]:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return ()
    call = node.value
    if not isinstance(call.func, ast.Attribute):
        return ()
    if call.func.attr not in {"append", "extend"}:
        return ()
    if not (isinstance(call.func.value, ast.Name) and call.func.value.id == PYTEST_PLUGINS_NAME):
        return ()
    if len(call.args) != 1:
        return ()
    return _string_literals(call.args[0], plugin_aliases)


def _iter_module_scope_statements(body: list[ast.stmt]) -> tuple[ast.stmt, ...]:
    # FIX: Include import-time pytest_plugins assignments nested in module-level
    # control flow without scanning function or class bodies.
    statements: list[ast.stmt] = []
    for statement in body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue

        statements.append(statement)
        if isinstance(statement, ast.If | ast.For | ast.AsyncFor | ast.While):
            statements.extend(_iter_module_scope_statements(statement.body))
            statements.extend(_iter_module_scope_statements(statement.orelse))
        elif isinstance(statement, ast.With | ast.AsyncWith):
            statements.extend(_iter_module_scope_statements(statement.body))
        elif isinstance(statement, ast.Try):
            statements.extend(_iter_module_scope_statements(statement.body))
            statements.extend(_iter_module_scope_statements(statement.orelse))
            statements.extend(_iter_module_scope_statements(statement.finalbody))
            for handler in statement.handlers:
                statements.extend(_iter_module_scope_statements(handler.body))
        elif isinstance(statement, ast.Match):
            for case in statement.cases:
                statements.extend(_iter_module_scope_statements(case.body))
    return tuple(statements)


def _resolve_pytest_plugin_path(project_root: Path, module: str) -> Path | None:
    if not module or module.startswith("."):
        return None
    module_parts = module.split(".")
    candidates = []
    for root in (project_root, project_root / "backend"):
        module_path = root.joinpath(*module_parts)
        candidates.extend((module_path.with_suffix(".py"), module_path / "__init__.py"))
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _assigns_to_name(node: ast.AST, name: str) -> bool:
    targets: list[ast.AST]
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
    elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
        targets = [node.target]
    else:
        return False
    return any(isinstance(target, ast.Name) and target.id == name for target in targets)


def _string_literals(
    node: ast.AST,
    string_aliases: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.Name) and string_aliases is not None and node.id in string_aliases:
        return string_aliases[node.id]
    if isinstance(node, ast.Starred):
        return _string_literals(node.value, string_aliases)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return (
            *_string_literals(node.left, string_aliases),
            *_string_literals(node.right, string_aliases),
        )
    if isinstance(node, ast.List | ast.Set | ast.Tuple):
        values: list[str] = []
        for item in node.elts:
            values.extend(_string_literals(item, string_aliases))
        return tuple(values)
    return ()


def _expand_wildcard(aliases: dict[str, str], module: str, root: str) -> None:
    """Expand a starred import from a tracked root into known forbidden symbols."""
    for symbol in FORBIDDEN_SYMBOLS:
        if symbol.startswith(f"{module}."):
            _add_wildcard_alias(aliases, module, symbol, override=True)
        elif symbol.startswith(f"{root}."):
            _add_wildcard_alias(aliases, root, symbol)


def _add_wildcard_alias(
    aliases: dict[str, str],
    imported_module: str,
    symbol: str,
    *,
    override: bool = False,
) -> None:
    leaf = symbol[len(imported_module) + 1 :]
    if override:
        aliases[leaf] = symbol
    else:
        aliases.setdefault(leaf, symbol)
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


def _string_alias_bindings(node: ast.AST) -> tuple[tuple[str, ast.AST], ...]:
    value = _assignment_value(node)
    if value is None:
        return ()
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
        targets = [node.target]
    else:
        return ()

    bindings: list[tuple[str, ast.AST]] = []
    for target in targets:
        bindings.extend(_target_string_value_bindings(target, value))
    return tuple(bindings)


def _target_string_value_bindings(
    target: ast.AST, value: ast.AST
) -> tuple[tuple[str, ast.AST], ...]:
    key = _assignment_string_key(target)
    if key:
        return ((key, value),)
    if isinstance(target, ast.List | ast.Tuple) and isinstance(value, ast.List | ast.Tuple):
        bindings: list[tuple[str, ast.AST]] = []
        for child_target, child_value in zip(target.elts, value.elts, strict=False):
            bindings.extend(_target_string_value_bindings(child_target, child_value))
        return tuple(bindings)
    return ()


def _assignment_string_key(target: ast.AST) -> str:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return _qualified_name(target, {})
    return ""


def _assignment_bindings(node: ast.AST) -> tuple[tuple[ast.Name, ast.AST], ...]:
    value = _assignment_value(node)
    if value is None:
        return ()
    targets: list[ast.AST]
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
        targets = [node.target]
    else:
        return ()

    bindings: list[tuple[ast.Name, ast.AST]] = []
    for target in targets:
        bindings.extend(_target_value_bindings(target, value))
    return tuple(bindings)


def _target_value_bindings(target: ast.AST, value: ast.AST) -> tuple[tuple[ast.Name, ast.AST], ...]:
    if isinstance(target, ast.Name):
        return ((target, value),)
    if isinstance(target, ast.List | ast.Tuple) and isinstance(value, ast.List | ast.Tuple):
        bindings: list[tuple[ast.Name, ast.AST]] = []
        for child_target, child_value in zip(target.elts, value.elts, strict=False):
            bindings.extend(_target_value_bindings(child_target, child_value))
        return tuple(bindings)
    return ()


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Assign | ast.NamedExpr):
        return node.value
    if isinstance(node, ast.AugAssign):
        # FIX: pytest_plugins += (...) appends plugin declarations that must be
        # scanned without treating augmented assignment as a general alias bind.
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _is_policy_symbol_or_prefix(symbol: str) -> bool:
    return any(
        forbidden == symbol or forbidden.startswith(f"{symbol}.") for forbidden in FORBIDDEN_SYMBOLS
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
    return tuple(sorted(unique.values(), key=lambda item: (item.relative_path, item.line)))


def main() -> int:
    """CLI entry point for the pytest policy gate."""
    return run_policy_gate()


if __name__ == "__main__":
    raise SystemExit(main())
