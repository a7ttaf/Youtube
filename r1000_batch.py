"""Decompose the four remaining PY-R1000 functions."""

from pathlib import Path

# ---------------- postgres.py: three extractions ----------------
p = Path("backend/ums_smart_revenue/ops/database_backup/postgres.py")
s = p.read_text(encoding="utf-8")

# 1. resolve_rehearsal_image -> _require_postgres_image_family
old_family = '''    config = image.get("Config")
    if not isinstance(config, dict):
        raise BackupToolError("rehearsal image has no readable config", exit_code=3)
    environment = config.get("Env")
    entrypoint = config.get("Entrypoint")
    command = config.get("Cmd")
    has_pg_major = isinstance(environment, list) and any(
        isinstance(item, str) and item.startswith("PG_MAJOR=") for item in environment
    )
    entrypoint_text = (
        " ".join(entrypoint) if isinstance(entrypoint, list) else str(entrypoint or "")
    )
    command_text = " ".join(command) if isinstance(command, list) else str(command or "")
    if (
        not has_pg_major
        or "docker-entrypoint" not in entrypoint_text
        or "postgres" not in command_text
    ):
        raise BackupToolError(
            "operator-selected rehearsal image is not a PostgreSQL image", exit_code=2
        )
    assert isinstance(image_id, str)
    return image_id'''
new_family = '''    _require_postgres_image_family(image)
    assert isinstance(image_id, str)
    return image_id'''
assert s.count(old_family) == 1
s = s.replace(old_family, new_family, 1)

family_helper = '''

def _require_postgres_image_family(image: dict[str, object]) -> None:
    """Refuse an operator-selected image that is not a stock PostgreSQL image."""
    config = image.get("Config")
    if not isinstance(config, dict):
        raise BackupToolError("rehearsal image has no readable config", exit_code=3)
    environment = config.get("Env")
    entrypoint = config.get("Entrypoint")
    command = config.get("Cmd")
    has_pg_major = isinstance(environment, list) and any(
        isinstance(item, str) and item.startswith("PG_MAJOR=") for item in environment
    )
    entrypoint_text = (
        " ".join(entrypoint) if isinstance(entrypoint, list) else str(entrypoint or "")
    )
    command_text = " ".join(command) if isinstance(command, list) else str(command or "")
    if (
        not has_pg_major
        or "docker-entrypoint" not in entrypoint_text
        or "postgres" not in command_text
    ):
        raise BackupToolError(
            "operator-selected rehearsal image is not a PostgreSQL image", exit_code=2
        )

'''
anchor = "\ndef create_rehearsal_container("
assert s.count(anchor) == 1
s = s.replace(anchor, family_helper + anchor, 1)

# 2. resolve_container_connection -> _container_identity
old_identity = '''    image_id = inspect.get("Image")
    container_id = inspect.get("Id")
    config = inspect.get("Config")
    image_reference = config.get("Image") if isinstance(config, dict) else None
    if not isinstance(image_id, str) or not _IMAGE_ID_RE.fullmatch(image_id):
        raise BackupToolError("container image is not identified by SHA-256", exit_code=3)
    if (
        not isinstance(container_id, str)
        or len(container_id) != 64
        or any(character not in "0123456789abcdef" for character in container_id)
    ):
        raise BackupToolError("container immutable id is unavailable", exit_code=3)
    if not isinstance(image_reference, str) or not image_reference:
        raise BackupToolError("container image reference is unavailable", exit_code=3)
    host, port = _published_postgres_endpoint(inspect)'''
new_identity = '''    image_id, container_id, image_reference = _container_identity(inspect)
    host, port = _published_postgres_endpoint(inspect)'''
assert s.count(old_identity) == 1
s = s.replace(old_identity, new_identity, 1)

identity_helper = '''

def _container_identity(inspect: dict[str, object]) -> tuple[str, str, str]:
    """Validate and return the container's immutable image/container identities."""
    image_id = inspect.get("Image")
    container_id = inspect.get("Id")
    config = inspect.get("Config")
    image_reference = config.get("Image") if isinstance(config, dict) else None
    if not isinstance(image_id, str) or not _IMAGE_ID_RE.fullmatch(image_id):
        raise BackupToolError("container image is not identified by SHA-256", exit_code=3)
    if (
        not isinstance(container_id, str)
        or len(container_id) != 64
        or any(character not in "0123456789abcdef" for character in container_id)
    ):
        raise BackupToolError("container immutable id is unavailable", exit_code=3)
    if not isinstance(image_reference, str) or not image_reference:
        raise BackupToolError("container image reference is unavailable", exit_code=3)
    return image_id, container_id, image_reference

'''
anchor2 = "\ndef _connect(source: ContainerConnection)"
assert s.count(anchor2) == 1
s = s.replace(anchor2, identity_helper + anchor2, 1)

# 3. snapshot_authorization_catalog_digest -> row fetch + shape checks
old_rows = '''    role_rows = connection.execute(
        """
        SELECT key, label, description, service_only
        FROM public.roles ORDER BY key
        """
    ).fetchall()
    permission_rows = connection.execute(
        """
        SELECT key, label, sensitive, audit_on_use
        FROM public.permissions ORDER BY key
        """
    ).fetchall()
    assignment_rows = connection.execute(
        """
        SELECT role_key, permission_key
        FROM public.role_permission_assignments
        ORDER BY role_key, permission_key
        """
    ).fetchall()
    if (
        any(
            len(row) != 4
            or not all(isinstance(value, str) for value in row[:3])
            or not isinstance(row[3], bool)
            for row in role_rows
        )
        or any(
            len(row) != 4
            or not all(isinstance(value, str) for value in row[:2])
            or not all(isinstance(value, bool) for value in row[2:])
            for row in permission_rows
        )
        or any(
            len(row) != 2 or not all(isinstance(value, str) for value in row)
            for row in assignment_rows
        )
    ):
        raise BackupToolError("authorization catalog rows are malformed", exit_code=8)
    payload = payload_from_database_rows(
        roles=cast(Sequence[tuple[str, str, str, bool]], role_rows),
        permissions=cast(Sequence[tuple[str, bool, bool]], permission_rows),
        assignments=cast(Sequence[tuple[str, str]], assignment_rows),
    )'''
new_rows = '''    role_rows, permission_rows, assignment_rows = _fetch_authorization_rows(connection)
    _require_canonical_row_shapes(role_rows, permission_rows, assignment_rows)
    payload = payload_from_database_rows(
        roles=cast(Sequence[tuple[str, str, str, bool]], role_rows),
        permissions=cast(Sequence[tuple[str, bool, bool]], permission_rows),
        assignments=cast(Sequence[tuple[str, str]], assignment_rows),
    )'''
assert s.count(old_rows) == 1
s = s.replace(old_rows, new_rows, 1)

rows_helper = '''

def _fetch_authorization_rows(
    connection: Connection[tuple[object, ...]],
) -> tuple[Sequence[object], Sequence[object], Sequence[object]]:
    """Fetch the ordered authorization catalog rows from the live database."""
    role_rows = connection.execute(
        """
        SELECT key, label, description, service_only
        FROM public.roles ORDER BY key
        """
    ).fetchall()
    permission_rows = connection.execute(
        """
        SELECT key, label, sensitive, audit_on_use
        FROM public.permissions ORDER BY key
        """
    ).fetchall()
    assignment_rows = connection.execute(
        """
        SELECT role_key, permission_key
        FROM public.role_permission_assignments
        ORDER BY role_key, permission_key
        """
    ).fetchall()
    return role_rows, permission_rows, assignment_rows


def _role_rows_well_formed(role_rows: Sequence[object]) -> bool:
    """Return whether every role row carries three strings and one boolean."""
    return not any(
        len(row) != 4
        or not all(isinstance(value, str) for value in row[:3])
        or not isinstance(row[3], bool)
        for row in role_rows
    )


def _permission_rows_well_formed(permission_rows: Sequence[object]) -> bool:
    """Return whether every permission row carries two strings and two booleans."""
    return not any(
        len(row) != 4
        or not all(isinstance(value, str) for value in row[:2])
        or not all(isinstance(value, bool) for value in row[2:])
        for row in permission_rows
    )


def _assignment_rows_well_formed(assignment_rows: Sequence[object]) -> bool:
    """Return whether every assignment row is a pair of strings."""
    return not any(
        len(row) != 2 or not all(isinstance(value, str) for value in row)
        for row in assignment_rows
    )


def _require_canonical_row_shapes(
    role_rows: Sequence[object],
    permission_rows: Sequence[object],
    assignment_rows: Sequence[object],
) -> None:
    """Refuse authorization catalog rows whose runtime shapes are malformed."""
    if not (
        _role_rows_well_formed(role_rows)
        and _permission_rows_well_formed(permission_rows)
        and _assignment_rows_well_formed(assignment_rows)
    ):
        raise BackupToolError("authorization catalog rows are malformed", exit_code=8)

'''
anchor3 = "\ndef snapshot_authorization_catalog_digest("
assert s.count(anchor3) == 1
s = s.replace(anchor3, rows_helper + anchor3, 1)
p.write_bytes(s.encode("utf-8"))
print("postgres.py: 3 refactors")

# ---------------- test_backup_content_gate.py ----------------
t = Path("tests/scripts/test_backup_content_gate.py")
g = t.read_text(encoding="utf-8")

old_reach = '''    module_tables, module_strings = _module_bindings(tree)
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

    result: set[str] = set()'''
new_reach = '''    module_tables, module_strings = _module_bindings(tree)
    reachable = _reachable_upgrade_functions(tree, functions)

    result: set[str] = set()'''
assert g.count(old_reach) == 1
g = g.replace(old_reach, new_reach, 1)

reach_helper = '''

def _reachable_upgrade_functions(
    tree: ast.Module, functions: dict[str, ast.FunctionDef]
) -> set[str]:
    """Walk every function transitively callable from upgrade()."""
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
    return reachable

'''
anchor4 = "\ndef _migration_seed_tables("
assert g.count(anchor4) == 1
g = g.replace(anchor4, reach_helper + anchor4, 1)
t.write_bytes(g.encode("utf-8"))
print("content gate: reachability extracted")
