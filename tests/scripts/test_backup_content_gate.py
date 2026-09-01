"""Migration parity for the database backup seed/content gate."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from ums_smart_revenue.ops.database_backup.contracts import SEED_TABLES

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_INSERT_STATEMENT_RE = re.compile(r"(?:\A|;)\s*INSERT\s+INTO\b", re.IGNORECASE)
_INSERT_ANYWHERE_RE = re.compile(r"\bINSERT\s+INTO\b", re.IGNORECASE)
_INSERT_TABLE_RE = re.compile(
    r"(?:\A|;)\s*INSERT\s+INTO\s+(?:ONLY\s+)?"
    r'(?:(?P<schema>"?[A-Za-z_][A-Za-z0-9_]*"?)\.)?'
    r'(?P<table>"?[A-Za-z_][A-Za-z0-9_]*"?)(?=\s|\(|$)',
    re.IGNORECASE,
)
_SQL_LEADING_TRIVIA_RE = re.compile(
    r"\A(?:\s+|--[^\r\n]*(?:\r?\n|\Z)|/\*.*?\*/)*",
    re.DOTALL,
)
_NON_EXECUTING_STORED_BODY_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\b",
    re.IGNORECASE,
)
_DOLLAR_QUOTE_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _scope_nodes(function: ast.FunctionDef) -> Iterator[ast.AST]:
    """Walk one function body without leaking nested lexical scopes into it."""
    stack: list[ast.AST] = list(reversed(function.body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _assignment(node: ast.AST) -> tuple[str, ast.AST] | None:
    """Return the name and value of a simple assignment node."""
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
        return node.target.id, node.value
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id, node.value
    return None


def _static_strings(node: ast.AST, bindings: dict[str, set[str]]) -> set[str]:
    """Resolve deterministic string expressions without importing a migration."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return bindings.get(node.id, set())
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return {
            left + right
            for left in _static_strings(node.left, bindings)
            for right in _static_strings(node.right, bindings)
        }
    if isinstance(node, ast.IfExp):
        return _static_strings(node.body, bindings) | _static_strings(node.orelse, bindings)
    if isinstance(node, ast.JoinedStr):
        values = {""}
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                part_values = {part.value}
            elif isinstance(part, ast.FormattedValue):
                part_values = _static_strings(part.value, bindings)
            else:
                return set()
            if not part_values:
                return set()
            values = {prefix + suffix for prefix in values for suffix in part_values}
        return values
    return set()


def _qualified_table_name(
    node: ast.AST,
    table_bindings: dict[str, set[str]],
    string_bindings: dict[str, set[str]],
) -> set[str]:
    """Resolve an identifier node to the table names it may denote."""
    if isinstance(node, ast.Name):
        return table_bindings.get(node.id, set())
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sa"
        and node.func.attr == "table"
        and node.args
    ):
        return set()
    names = _static_strings(node.args[0], string_bindings)
    schemas = {"public"}
    for keyword in node.keywords:
        if keyword.arg == "schema":
            schemas = _static_strings(keyword.value, string_bindings)
    if len(names) != 1 or len(schemas) != 1:
        return set()
    return {f"{next(iter(schemas))}.{next(iter(names))}"}


def _scope_bindings(
    nodes: list[ast.AST],
    *,
    parameter_names: set[str],
    module_tables: dict[str, set[str]],
    module_strings: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Collect the table and string bindings visible from a statement scope."""
    assignments = [item for node in nodes if (item := _assignment(node))]
    local_names = parameter_names | {name for name, _value in assignments}
    tables = {
        name: values.copy() for name, values in module_tables.items() if name not in local_names
    }
    strings = {
        name: values.copy() for name, values in module_strings.items() if name not in local_names
    }
    for _ in range(len(assignments) + 1):
        changed = False
        for name, value in assignments:
            table_values = _qualified_table_name(value, tables, strings)
            string_values = _static_strings(value, strings)
            if table_values - tables.get(name, set()):
                tables.setdefault(name, set()).update(table_values)
                changed = True
            if string_values - strings.get(name, set()):
                strings.setdefault(name, set()).update(string_values)
                changed = True
        if not changed:
            break
    return tables, strings


def _module_bindings(tree: ast.Module) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Collect module-level table and string bindings."""
    assignment_nodes = [node for node in tree.body if _assignment(node)]
    if not assignment_nodes:
        return {}, {}
    return _scope_bindings(
        assignment_nodes,
        parameter_names=set(),
        module_tables={},
        module_strings={},
    )


def _callable_bindings(
    nodes: list[ast.AST],
    *,
    parameter_names: set[str],
    inherited: dict[str, set[str]],
) -> dict[str, set[str]]:
    """Resolve direct and chained helper aliases in one lexical scope."""
    assignments = [item for node in nodes if (item := _assignment(node))]
    local_names = parameter_names | {name for name, _value in assignments}
    callables = {
        name: values.copy() for name, values in inherited.items() if name not in local_names
    }
    for _ in range(len(assignments) + 1):
        changed = False
        for name, value in assignments:
            values = callables.get(value.id, set()) if isinstance(value, ast.Name) else set()
            if values - callables.get(name, set()):
                callables.setdefault(name, set()).update(values)
                changed = True
        if not changed:
            break
    return callables


def _module_callable_bindings(
    tree: ast.Module,
    functions: dict[str, ast.FunctionDef],
) -> dict[str, set[str]]:
    """Map module callables to the tables their bodies may touch."""
    direct = {name: {name} for name in functions}
    assignment_nodes = [node for node in tree.body if _assignment(node)]
    return _callable_bindings(
        assignment_nodes,
        parameter_names=set(),
        inherited=direct,
    )


def _function_parameter_names(function: ast.FunctionDef) -> set[str]:
    """List the parameter names declared by a function node."""
    result = {
        argument.arg
        for argument in function.args.posonlyargs + function.args.args + function.args.kwonlyargs
    }
    if function.args.vararg:
        result.add(function.args.vararg.arg)
    if function.args.kwarg:
        result.add(function.args.kwarg.arg)
    return result


def _primary_call_argument(call: ast.Call, *, keywords: set[str]) -> ast.AST | None:
    """Pick the positional or keyword argument that names the SQL text."""
    candidates = list(call.args[:1])
    candidates.extend(keyword.value for keyword in call.keywords if keyword.arg in keywords)
    return candidates[0] if len(candidates) == 1 else None


def _insert_target(node: ast.AST) -> tuple[bool, ast.AST | None]:
    """Recognize SQLAlchemy insert constructors, including fluent wrappers."""
    if not isinstance(node, ast.Call):
        return False, None
    if isinstance(node.func, ast.Name) and node.func.id == "insert":
        return True, node.args[0] if node.args else None
    if isinstance(node.func, ast.Attribute):
        if node.func.attr == "insert":
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "sa":
                return True, node.args[0] if node.args else None
            return True, node.func.value
        if node.func.attr in {
            "inline",
            "on_conflict_do_nothing",
            "on_conflict_do_update",
            "prefix_with",
            "return_defaults",
            "returning",
            "values",
            "with_dialect_options",
        }:
            return _insert_target(node.func.value)
    return False, None


def _sql_strings(node: ast.AST, strings: dict[str, set[str]]) -> set[str]:
    """Unwrap SQLAlchemy text/bindparams calls and resolve their SQL strings."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "text" and node.args:
            return _static_strings(node.args[0], strings)
        if node.func.attr in {"bindparams", "execution_options"}:
            return _sql_strings(node.func.value, strings)
    return _static_strings(node, strings)


def _tables_from_sql(sql: str) -> set[str]:
    """Return the tables a SQL string writes to."""
    result: set[str] = set()
    for match in _INSERT_TABLE_RE.finditer(sql):
        schema = (match.group("schema") or "public").strip('"')
        table = match.group("table").strip('"')
        result.add(f"{schema}.{table}")
    return result


def _statement_after_leading_trivia(sql: str) -> str:
    """Strip leading comments and whitespace from a SQL string."""
    match = _SQL_LEADING_TRIVIA_RE.match(sql)
    return sql[match.end() :] if match else sql


def _is_single_non_executing_stored_body(sql: str) -> bool:
    """Report SQL that is only an inert stored-routine body."""
    statement = _statement_after_leading_trivia(sql)
    if not _NON_EXECUTING_STORED_BODY_RE.match(statement):
        return False
    opening = _DOLLAR_QUOTE_RE.search(statement)
    if opening is None:
        return False
    closing_index = statement.find(opening.group(), opening.end())
    if closing_index < 0:
        return False
    tail = statement[closing_index + len(opening.group()) :]
    return bool(re.fullmatch(r"\s*;?\s*", tail))


def _sql_expression_skeleton(node: ast.AST) -> str:
    """Preserve SQL literal structure while replacing dynamic f-string values."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else "__DYNAMIC_VALUE__"
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _sql_expression_skeleton(node.left) + _sql_expression_skeleton(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "text" and node.args:
            return _sql_expression_skeleton(node.args[0])
        if node.func.attr in {"bindparams", "execution_options"}:
            return _sql_expression_skeleton(node.func.value)
    return "__DYNAMIC_VALUE__"


def _has_unresolved_insert_literal(node: ast.AST) -> bool:
    """Report an INSERT target the scanner could not resolve to a table."""
    return any(
        isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and _INSERT_ANYWHERE_RE.search(child.value)
        for child in ast.walk(node)
    )


# ============================================================================
# Purpose: Derive migration-seeded tables from only upgrade-reachable writes,
#   with lexical bindings isolated per helper and unresolved insert targets
#   rejected instead of silently weakening the backup content gate.
# Database/ORM: Parses Alembic source only; no import, connection, or mutation.
# Standards: Detects Alembic bulk inserts, SQLAlchemy insert constructors, and
#   deterministic literal INSERT statements without executing migration code.
# Blast Radius: Backup admission; missing or ambiguous seed identities fail CI.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions -> every seed source.
#   - File: backend/ums_smart_revenue/ops/database_backup/contracts.py -> gate.
# ============================================================================
def _migration_seed_tables(source: str, *, source_name: str) -> set[str]:
    """Extract the tables an Alembic upgrade seeds."""
    tree = ast.parse(source, filename=source_name)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if "upgrade" not in functions:
        raise AssertionError(f"{source_name}: migration has no upgrade() function")

    module_tables, module_strings = _module_bindings(tree)
    module_callables = _module_callable_bindings(tree, functions)
    reachable = {"upgrade"}
    pending = ["upgrade"]
    while pending:
        function = functions[pending.pop()]
        nodes = list(_scope_nodes(function))
        callables = _callable_bindings(
            nodes,
            parameter_names=_function_parameter_names(function),
            inherited=module_callables,
        )
        for node in nodes:
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            for target in callables.get(node.func.id, set()):
                if target not in reachable:
                    reachable.add(target)
                    pending.append(target)

    result: set[str] = set()
    for function_name in sorted(reachable):
        function = functions[function_name]
        nodes = list(_scope_nodes(function))
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
            for node in nodes
        ):
            raise AssertionError(
                f"{source_name}:{function_name}: nested scope requires explicit scanner support"
            )
        tables, strings = _scope_bindings(
            nodes,
            parameter_names=_function_parameter_names(function),
            module_tables=module_tables,
            module_strings=module_strings,
        )
        for node in nodes:
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "bulk_insert":
                table_argument = _primary_call_argument(node, keywords={"table"})
                targets = (
                    _qualified_table_name(table_argument, tables, strings)
                    if table_argument is not None
                    else set()
                )
                if len(targets) != 1:
                    raise AssertionError(
                        f"{source_name}:{function_name}: unresolved bulk_insert table"
                    )
                result.update(targets)
                continue
            if node.func.attr != "execute":
                continue
            statement = _primary_call_argument(node, keywords={"sqltext", "statement"})
            if statement is None:
                raise AssertionError(
                    f"{source_name}:{function_name}: unresolved execute statement argument"
                )
            is_insert, target = _insert_target(statement)
            if is_insert:
                targets = (
                    _qualified_table_name(target, tables, strings) if target is not None else set()
                )
                if len(targets) != 1:
                    raise AssertionError(
                        f"{source_name}:{function_name}: unresolved SQLAlchemy insert table"
                    )
                result.update(targets)
                continue
            nested_insert_targets = [
                nested_target
                for child in ast.walk(statement)
                if isinstance(child, ast.Call)
                for nested_is_insert, nested_target in [_insert_target(child)]
                if nested_is_insert
            ]
            if nested_insert_targets:
                for nested_target in nested_insert_targets:
                    targets = (
                        _qualified_table_name(nested_target, tables, strings)
                        if nested_target is not None
                        else set()
                    )
                    if len(targets) != 1:
                        raise AssertionError(
                            f"{source_name}:{function_name}: unresolved nested insert table"
                        )
                    result.update(targets)
                continue
            sql_strings = _sql_strings(statement, strings)
            if not sql_strings and _has_unresolved_insert_literal(statement):
                if _is_single_non_executing_stored_body(_sql_expression_skeleton(statement)):
                    continue
                raise AssertionError(
                    f"{source_name}:{function_name}: unresolved literal INSERT statement"
                )
            if not sql_strings and isinstance(statement, (ast.Name, ast.Attribute, ast.Subscript)):
                raise AssertionError(f"{source_name}:{function_name}: unresolved execute statement")
            for sql in sql_strings:
                statement_sql = _statement_after_leading_trivia(sql)
                if _INSERT_ANYWHERE_RE.search(sql) and re.match(
                    r"(?:WITH|DO)\b", statement_sql, re.IGNORECASE
                ):
                    raise AssertionError(
                        f"{source_name}:{function_name}: unresolved compound INSERT statement"
                    )
                targets = _tables_from_sql(sql)
                if _INSERT_STATEMENT_RE.search(sql) and not targets:
                    raise AssertionError(
                        f"{source_name}:{function_name}: unresolved literal INSERT table"
                    )
                if (
                    _INSERT_ANYWHERE_RE.search(sql)
                    and not targets
                    and not _is_single_non_executing_stored_body(sql)
                ):
                    raise AssertionError(
                        f"{source_name}:{function_name}: unresolved embedded INSERT statement"
                    )
                result.update(targets)
    return result


def test_migration_seed_scanner_detects_supported_upgrade_insert_idioms() -> None:
    """The scanner recognises every INSERT idiom the migrations use."""
    source = """
MODULE_TABLE = sa.table("bulk_seed")
SQL = "INSERT INTO literal_seed (id) VALUES (1)"

def upgrade():
    local_table = sa.table("constructor_seed")
    keyword_table = sa.table("keyword_constructor_seed")
    op.bulk_insert(MODULE_TABLE, rows)
    bind.execute(sa.insert(local_table), rows)
    bind.execute(statement=sa.insert(keyword_table), parameters=rows)
    _table_insert_helper()
    op.execute(sa.text(SQL))
    op.execute(sqltext=sa.text("INSERT INTO keyword_literal_seed (id) VALUES (1)"))

def _table_insert_helper():
    helper_table = sa.table("fluent_seed", schema="public")
    bind.execute(helper_table.insert().values(id=1))

def downgrade():
    pass
"""
    assert _migration_seed_tables(source, source_name="synthetic.py") == {
        "public.bulk_seed",
        "public.constructor_seed",
        "public.fluent_seed",
        "public.keyword_constructor_seed",
        "public.keyword_literal_seed",
        "public.literal_seed",
    }


def test_migration_seed_scanner_follows_module_and_local_helper_aliases() -> None:
    """The scanner follows module-level and local helper aliases."""
    source = """
def seed_rows():
    table = sa.table("aliased_helper_seed")
    op.bulk_insert(table, rows)

SEED_ROWS = seed_rows

def alias_layer():
    local_alias = SEED_ROWS
    local_alias()

def upgrade():
    alias_layer()

def downgrade():
    SEED_ROWS = downgrade_rows
    SEED_ROWS()

def downgrade_rows():
    table = sa.table("downgrade_alias_seed")
    op.bulk_insert(table, rows)
"""
    assert _migration_seed_tables(source, source_name="synthetic.py") == {
        "public.aliased_helper_seed"
    }


def test_migration_seed_scanner_isolates_upgrade_scope_and_reachability() -> None:
    """Only reachable upgrade-scope statements count as seeds."""
    source = """
table = sa.table("module_only")

def upgrade():
    table = sa.table("upgrade_seed")
    op.bulk_insert(table, rows)

def unused_helper():
    table = sa.table("unreachable_seed")
    op.bulk_insert(table, rows)

def downgrade():
    table = sa.table("downgrade_seed")
    op.bulk_insert(table, rows)
"""
    assert _migration_seed_tables(source, source_name="synthetic.py") == {"public.upgrade_seed"}


def test_migration_seed_scanner_rejects_unresolved_insert_target() -> None:
    """The scanner rejects an INSERT whose target cannot be resolved."""
    source = """
def upgrade():
    bind.execute(sa.insert(resolve_table()), rows)

def downgrade():
    pass
"""
    with pytest.raises(AssertionError, match="unresolved SQLAlchemy insert table"):
        _migration_seed_tables(source, source_name="synthetic.py")


def test_migration_seed_scanner_rejects_dynamic_literal_insert_target() -> None:
    """The scanner rejects INSERT targets built from dynamic literals."""
    source = """
def upgrade():
    bind.execute(sa.text("INSERT INTO " + resolve_table()))

def downgrade():
    pass
"""
    with pytest.raises(AssertionError, match="unresolved literal INSERT statement"):
        _migration_seed_tables(source, source_name="synthetic.py")


@pytest.mark.parametrize(
    "sql",
    [
        "WITH source AS (SELECT 1) INSERT INTO cte_seed SELECT * FROM source",
        "DO $$ BEGIN INSERT INTO do_seed (id) VALUES (1); END $$",
    ],
)
def test_migration_seed_scanner_rejects_compound_insert_statements(sql: str) -> None:
    """The scanner rejects compound statements instead of guessing."""
    source = f"""\
def upgrade():
    op.execute(sa.text({sql!r}))

def downgrade():
    pass
"""
    with pytest.raises(AssertionError, match="unresolved compound INSERT statement"):
        _migration_seed_tables(source, source_name="synthetic.py")


def test_migration_seed_scanner_rejects_unresolved_helper_statement() -> None:
    """The scanner rejects unresolved helpers rather than skipping them."""
    source = """
def upgrade():
    run_statement("INSERT INTO hidden_seed (id) VALUES (1)")

def run_statement(statement):
    bind.execute(statement)

def downgrade():
    pass
"""
    with pytest.raises(AssertionError, match="unresolved execute statement"):
        _migration_seed_tables(source, source_name="synthetic.py")


# ============================================================================
# Purpose: Keep the backup content gate aligned with every migration-seeded
#   table without copying any measured row-count literal into production code.
# Database/ORM: Parses Alembic source only; no database connection or mutation.
# Standards: The production gate names tables and measures their counts from the
#   exported PostgreSQL snapshot after the final migration head.
# Blast Radius: Backup admission; missing seed identities fail this test.
# Connections:
#   - File: scripts/backup_database.py -> exported SEED_TABLES compatibility.
#   - File: backend/ums_smart_revenue/db/alembic/versions -> every seed source.
# ============================================================================
def test_seed_tables_match_what_the_migrations_actually_seed() -> None:
    """The scanned seed set equals what the migrations really insert."""
    versions = REPOSITORY_ROOT / "backend/ums_smart_revenue/db/alembic/versions"
    migration_seed_tables: set[str] = set()
    for migration in sorted(versions.glob("*.py")):
        migration_seed_tables.update(
            _migration_seed_tables(
                migration.read_text(encoding="utf-8"),
                source_name=str(migration.relative_to(REPOSITORY_ROOT)),
            )
        )
    assert SEED_TABLES == {"public.alembic_version"} | migration_seed_tables
