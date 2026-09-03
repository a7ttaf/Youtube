# ============================================================================
# Purpose: Guard the P0.7 seed at three levels — the migration actually writes
#   the catalog, it is idempotent and self-healing, and the three sources of
#   truth (auth/roles.py, auth/permissions.py, auth/seed.py) cannot drift from
#   the raw db/security_seed.sql twin that operators may still run by hand.
# Database/ORM: RoleORM, PermissionORM, RolePermissionAssignmentORM,
#   UserRoleAssignmentORM, UserPermissionGrantORM — exercised against a real
#   migrated SQLite database, not against ORM metadata.
# Standards: The migration module is loaded by path (Alembic revisions are not
#   importable by name) and bound to a live connection's Operations, the same
#   pattern test_channel_group_content_owner_migration.py uses.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260825_0001_security_role_permission_seed.py -> subject.
#   - File: backend/ums_smart_revenue/db/security_seed.sql -> the raw-SQL twin.
#   - File: Docs/20_DEPLOYMENT_READINESS_AUDIT.md -> H1.
# ============================================================================
"""Guards for the roles/permissions seed migration and its SQL twin."""

import hashlib
import importlib.util
import json
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.permissions import PERMISSION_DEFINITIONS, Permission
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS, RoleKey
from ums_smart_revenue.auth.seed import initial_role_permission_rows
from ums_smart_revenue.db.frozen_security_catalog import (
    FROZEN_PERMISSION_ROWS as HISTORICAL_PERMISSION_ROWS,
)
from ums_smart_revenue.db.frozen_security_catalog import (
    FROZEN_ROLE_PERMISSION_ROWS as HISTORICAL_ROLE_PERMISSION_ROWS,
)
from ums_smart_revenue.db.frozen_security_catalog import (
    FROZEN_ROLE_ROWS as HISTORICAL_ROLE_ROWS,
)
from ums_smart_revenue.db.frozen_security_catalog_20260825_0002 import (
    FROZEN_PERMISSION_ROWS as REPAIR_PERMISSION_ROWS,
)
from ums_smart_revenue.db.frozen_security_catalog_20260825_0002 import (
    FROZEN_ROLE_PERMISSION_ROWS as REPAIR_ROLE_PERMISSION_ROWS,
)
from ums_smart_revenue.db.frozen_security_catalog_20260825_0002 import (
    FROZEN_ROLE_ROWS as REPAIR_ROLE_ROWS,
)
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    PermissionORM,
    RoleORM,
    RolePermissionAssignmentORM,
    SecurityBase,
    UserORM,
    UserPermissionGrantORM,
    UserRoleAssignmentORM,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_MIGRATION_PATH = (
    PROJECT_ROOT
    / "backend/ums_smart_revenue/db/alembic/versions/"
    / "20260825_0001_security_role_permission_seed.py"
)
REPAIR_MIGRATION_PATH = (
    PROJECT_ROOT
    / "backend/ums_smart_revenue/db/alembic/versions/"
    / "20260825_0002_beta_operator_authorization_repair.py"
)
HISTORICAL_SNAPSHOT_PATH = PROJECT_ROOT / "backend/ums_smart_revenue/db/frozen_security_catalog.py"
SEED_SQL_PATH = PROJECT_ROOT / "backend/ums_smart_revenue/db/security_seed.sql"

_HISTORICAL_MIGRATION_GIT_BLOB = "9df02500cdc0508da211ad51e5d3e0306a67771b"
_HISTORICAL_MIGRATION_SHA256 = "01a93d377fa9b5c296daffbc0cc600a6949021fa53bddeae546ce7cb6b7766c5"
# Repinned 2026-09-03: whitespace-only reformat of the frozen literal rows
# (one key per line, <=100 cols) to clear analyzer line-length findings
# pre-merge; the parsed catalog data is byte-for-data identical (verified
# by ast comparison and the semantic digest assertions below).
_HISTORICAL_SNAPSHOT_GIT_BLOB = "0f7defa1748ebe1a0406bd59dc96567416be0297"
_HISTORICAL_SNAPSHOT_SHA256 = "afe311d65396b0cdef58dbd73d907b58593ebd7eee28bb3970fe0db89faafef1"
_HISTORICAL_SNAPSHOT_SEMANTIC_SHA256 = (
    "376561bbe0f37448800df279d39b161f1f0d9ce03381dfc0c578df3e69704705"
)
_REPAIR_SNAPSHOT_SEMANTIC_SHA256 = (
    "7a4377117cee090b4ee3c74a009adb33c8b84d30c6037155b2c94abd4226644e"
)
_HISTORICAL_COUNTS = (17, 26, 121)
_REPAIR_COUNTS = (17, 27, 122)
_MANUAL_PERMISSION_ROW = {
    "key": "finance.import_manual_revenue",
    "label": "Import manual revenue facts",
    "sensitive": True,
    "audit_on_use": True,
}
_HISTORICAL_BETA_DESCRIPTION = "First-beta finance operator with manual-import connector access."
_REPAIR_BETA_DESCRIPTION = "First-beta finance operator with manual revenue-upload access."
_BETA_CONNECTOR_EDGE = ("beta_operator", "connectors.run_jobs")
_BETA_MANUAL_REVENUE_EDGE = ("beta_operator", "finance.import_manual_revenue")
_SUPER_OWNER_MANUAL_REVENUE_EDGE = ("super_owner", "finance.import_manual_revenue")
_EXPECTED_CURRENT_PERMISSION_METADATA: dict[str, tuple[str, bool, bool]] = {
    "analytics.view": ("View performance analytics", False, False),
    "analytics.view_confidence": ("View confidence labels and issue flags", False, False),
    "audit.view": ("View audit log", True, True),
    "audit.view_sensitive_payloads": ("View sensitive audit payloads", True, True),
    "connectors.manage": ("Manage connectors", True, True),
    "connectors.run_jobs": ("Run connector jobs", True, True),
    "connectors.view_health": ("View connector health", False, False),
    "exports.analytics": ("Export analytics report", True, True),
    "exports.manage_templates": ("Manage export templates", True, True),
    "exports.revenue": ("Export revenue report", True, True),
    "finance.approve_manual_override": ("Approve manual override", True, True),
    "finance.change_allocation_rule": ("Change allocation rule", True, True),
    "finance.create_manual_override": ("Create manual override", True, True),
    "finance.import_manual_revenue": ("Import manual revenue facts", True, True),
    "finance.lock_month": ("Lock finance month", True, True),
    "finance.manage_bank_reconciliation": ("Manage bank reconciliation", True, True),
    "finance.unlock_month": ("Unlock finance month", True, True),
    "finance.view_bank_reconciliation": ("View bank reconciliation", True, True),
    "finance.view_finalized_payments": ("View finalized payments", True, True),
    "finance.view_revenue": ("View revenue values", True, True),
    "platform.manage_settings": ("Manage platform settings", True, True),
    "raw_files.view": ("View raw report files", True, True),
    "registry.manage_channels": ("Manage channel registry", True, True),
    "registry.manage_groups": ("Manage channel groups", True, True),
    "registry.manage_org_mapping": ("Manage organization mapping", True, True),
    "roles.assign": ("Assign roles", True, True),
    "users.manage": ("Manage users", True, True),
}

_EXPECTED_CURRENT_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "assistant_analyst": {"analytics.view", "analytics.view_confidence"},
    "audit_viewer": {"audit.view"},
    "beta_operator": {
        "analytics.view",
        "analytics.view_confidence",
        "audit.view",
        "exports.analytics",
        "exports.revenue",
        "finance.approve_manual_override",
        "finance.change_allocation_rule",
        "finance.create_manual_override",
        "finance.import_manual_revenue",
        "finance.lock_month",
        "finance.manage_bank_reconciliation",
        "finance.unlock_month",
        "finance.view_bank_reconciliation",
        "finance.view_finalized_payments",
        "finance.view_revenue",
    },
    "channel_manager": {"analytics.view", "exports.analytics", "analytics.view_confidence"},
    "company_manager": {"analytics.view", "exports.analytics", "analytics.view_confidence"},
    "connector_admin": {
        "connectors.manage",
        "connectors.run_jobs",
        "connectors.view_health",
        "raw_files.view",
    },
    "corporate_admin": {
        "analytics.view",
        "analytics.view_confidence",
        "audit.view",
        "connectors.view_health",
        "exports.analytics",
        "exports.manage_templates",
        "platform.manage_settings",
        "registry.manage_channels",
        "registry.manage_groups",
        "registry.manage_org_mapping",
        "roles.assign",
        "users.manage",
    },
    "data_steward": {
        "analytics.view",
        "analytics.view_confidence",
        "registry.manage_channels",
        "registry.manage_groups",
        "registry.manage_org_mapping",
    },
    "export_operator": {"analytics.view", "exports.analytics", "analytics.view_confidence"},
    "finance_admin": {
        "analytics.view",
        "analytics.view_confidence",
        "audit.view",
        "exports.analytics",
        "exports.revenue",
        "finance.approve_manual_override",
        "finance.change_allocation_rule",
        "finance.create_manual_override",
        "finance.lock_month",
        "finance.manage_bank_reconciliation",
        "finance.unlock_month",
        "finance.view_bank_reconciliation",
        "finance.view_finalized_payments",
        "finance.view_revenue",
        "roles.assign",
    },
    "finance_approver": {
        "analytics.view",
        "analytics.view_confidence",
        "audit.view",
        "exports.revenue",
        "finance.approve_manual_override",
        "finance.change_allocation_rule",
        "finance.manage_bank_reconciliation",
        "finance.unlock_month",
        "finance.view_bank_reconciliation",
        "finance.view_finalized_payments",
        "finance.view_revenue",
    },
    "finance_viewer": {
        "analytics.view",
        "analytics.view_confidence",
        "finance.view_bank_reconciliation",
        "finance.view_finalized_payments",
        "finance.view_revenue",
    },
    "news_sector_manager": {"analytics.view", "exports.analytics", "analytics.view_confidence"},
    "revenue_operations_admin": {
        "analytics.view",
        "analytics.view_confidence",
        "connectors.run_jobs",
        "connectors.view_health",
        "exports.analytics",
        "registry.manage_channels",
        "registry.manage_groups",
        "registry.manage_org_mapping",
    },
    "super_owner": {
        "analytics.view",
        "analytics.view_confidence",
        "audit.view",
        "audit.view_sensitive_payloads",
        "connectors.manage",
        "connectors.run_jobs",
        "connectors.view_health",
        "exports.analytics",
        "exports.manage_templates",
        "exports.revenue",
        "finance.approve_manual_override",
        "finance.change_allocation_rule",
        "finance.create_manual_override",
        "finance.import_manual_revenue",
        "finance.lock_month",
        "finance.manage_bank_reconciliation",
        "finance.unlock_month",
        "finance.view_bank_reconciliation",
        "finance.view_finalized_payments",
        "finance.view_revenue",
        "platform.manage_settings",
        "raw_files.view",
        "registry.manage_channels",
        "registry.manage_groups",
        "registry.manage_org_mapping",
        "roles.assign",
        "users.manage",
    },
    "system_integration_user": {"connectors.view_health", "connectors.run_jobs"},
    "tv_sector_manager": {"analytics.view", "exports.analytics", "analytics.view_confidence"},
}

def _parse_value_tuples(body: str) -> list[tuple[str, ...]]:
    """Split a VALUES body into top-level tuples of literal values.

    Handles single-line and wrapped tuples identically; string values keep
    their exact text and boolean literals are normalized to lowercase so the
    registry comparisons stay byte-for-byte equivalent.
    """
    rows: list[tuple[str, ...]] = []
    depth = 0
    current: list[str] = []
    token: list[str] = []
    in_string = False
    i = 0
    while i < len(body):
        character = body[i]
        if in_string:
            token.append(character)
            if character == "'":
                if i + 1 < len(body) and body[i + 1] == "'":
                    token.append("'")
                    i += 2
                    continue
                in_string = False
        elif character == "'":
            in_string = True
            token.append(character)
        elif character == "(":
            depth += 1
            if depth == 1:
                current = []
                token = []
            else:
                token.append(character)
        elif character == ")":
            depth -= 1
            if depth == 0:
                current.append("".join(token).strip())
                rows.append(tuple(_literal_value(part) for part in current))
                token = []
            else:
                token.append(character)
        elif character == "," and depth == 1:
            current.append("".join(token).strip())
            token = []
        elif depth >= 1:
            token.append(character)
        i += 1
    return rows


def _literal_value(token: str) -> str:
    """Strip one pair of quoting apostrophes and lowercase boolean literals."""
    lowered = token.lower()
    if lowered in ("true", "false"):
        return lowered
    if len(token) >= 2 and token[0] == token[-1] == "'":
        return token[1:-1]
    return token


def _migration_module(path: Path, name: str) -> ModuleType:
    """Load one migration module by path (Alembic revisions are not packages)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _historical_migration_module() -> ModuleType:
    """Load the 20260825_0001 migration module without running it."""
    return _migration_module(HISTORICAL_MIGRATION_PATH, "m_20260825_0001")


def _repair_migration_module() -> ModuleType:
    """Load the 20260825_0002 repair migration module without running it."""
    return _migration_module(REPAIR_MIGRATION_PATH, "m_20260825_0002")


def _bind_operations(module: ModuleType, connection: Connection) -> None:
    """Point the migration module's ``op`` at this connection's Operations."""
    module.op = Operations(MigrationContext.configure(connection))


def _security_engine() -> Engine:
    """Return an in-memory SQLite engine carrying the security schema."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SecurityBase.metadata.create_all(engine)
    return engine


def _stored_roles(connection: Connection) -> dict[str, tuple[str, str, bool]]:
    """Return every stored role row keyed by role key."""
    rows = connection.execute(
        select(RoleORM.key, RoleORM.label, RoleORM.description, RoleORM.service_only)
    ).all()
    return {row.key: (row.label, row.description, bool(row.service_only)) for row in rows}


def _stored_permissions(connection: Connection) -> dict[str, tuple[str, bool, bool]]:
    """Return every stored permission row keyed by permission key."""
    rows = connection.execute(
        select(
            PermissionORM.key,
            PermissionORM.label,
            PermissionORM.sensitive,
            PermissionORM.audit_on_use,
        )
    ).all()
    return {row.key: (row.label, bool(row.sensitive), bool(row.audit_on_use)) for row in rows}


def _stored_pairs(connection: Connection) -> set[tuple[str, str]]:
    """Return every stored (role, permission) catalog edge."""
    rows = connection.execute(
        select(
            RolePermissionAssignmentORM.role_key,
            RolePermissionAssignmentORM.permission_key,
        )
    ).all()
    return {(row.role_key, row.permission_key) for row in rows}


def _expected_pairs() -> set[tuple[str, str]]:
    """Return the (role, permission) edges ``auth/seed.py`` declares."""
    return {(row["role"], row["permission"]) for row in initial_role_permission_rows()}


def _historical_pairs() -> set[tuple[str, str]]:
    """Return the exact immutable 20260825_0001 catalog edges."""
    return {
        (str(row["role_key"]), str(row["permission_key"]))
        for row in HISTORICAL_ROLE_PERMISSION_ROWS
    }


def _repair_pairs() -> set[tuple[str, str]]:
    """Return the exact immutable 20260825_0002 catalog edges."""
    return {
        (str(row["role_key"]), str(row["permission_key"])) for row in REPAIR_ROLE_PERMISSION_ROWS
    }


def _role_permission_map(
    pairs: Iterable[tuple[str, str]],
) -> dict[str, set[str]]:
    """Group exact role-permission pairs without consulting production policy."""
    grouped: dict[str, set[str]] = {}
    for role_key, permission_key in pairs:
        grouped.setdefault(role_key, set()).add(permission_key)
    return grouped


def _catalog_semantic_digest(
    role_rows: list[dict[str, object]],
    permission_rows: list[dict[str, object]],
    role_permission_rows: list[dict[str, object]],
) -> str:
    """Hash normalized row values, independent of Python source formatting."""
    payload = [role_rows, permission_rows, role_permission_rows]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _git_blob_oid(content: bytes) -> str:
    """Compute the immutable Git blob identity without reading repository state."""
    payload = f"blob {len(content)}\0".encode() + content
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def _table_counts(connection: Connection) -> dict[str, int]:
    """Return the row count of every table in the security schema."""
    return {
        table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
        for table in SecurityBase.metadata.sorted_tables
    }


# The tables this revision is allowed to write. Named rather than counted on
# purpose: the ROW COUNT moves whenever a permission is added, but the TABLE SET
# is a design decision — and it is the half that couples to the backup gate.
_SEEDED_TABLES = {"roles", "permissions", "role_permission_assignments"}


def test_migration_writes_only_the_catalog_tables() -> None:
    """The revision seeds exactly three tables, and each one is a backup seed table.

    This is the coupling that already bit once, which is why this test measures
    rather than assumes. ``scripts/backup_database.py`` judges a dump partly by
    whether every table OUTSIDE its own ``SEED_TABLES`` is empty: that emptiness
    is how a first run tells a healthy database from one that was wiped and
    re-migrated, and it is a fail-closed refusal with no override. When this
    revision landed, ``SEED_TABLES`` was ``(alembic_version, currencies,
    tenants)`` — so the 164 historical rows seeded below read as application data
    and the refusal stopped firing. The forward repair brings the current catalog
    to 166 rows without widening the table set. A
    data-seeding migration had disabled a safety check in a file it never
    mentions. ``SEED_TABLES`` now lists all three names.

    So widening ``_SEEDED_TABLES`` below is not a local change: add the table to
    ``SEED_TABLES`` and re-measure the backup gate's virgin baseline in the same
    commit. This test is the near half of that pair —
    ``tests/scripts/test_backup_content_gate.py`` owns the far half and derives
    its expectation by parsing the migrations.
    """
    module = _historical_migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        before = _table_counts(connection)
        _bind_operations(module, connection)
        module.upgrade()
        after = _table_counts(connection)

        assert set(before) == set(after)
        written = {name for name, count in after.items() if count > before[name]}
        assert written == _SEEDED_TABLES
        assert (
            tuple(
                len(rows)
                for rows in (
                    module.role_seed_rows(),
                    module.permission_seed_rows(),
                    module.role_permission_seed_rows(),
                )
            )
            == _HISTORICAL_COUNTS
        )
        assert sum(after[name] - before[name] for name in written) == sum(_HISTORICAL_COUNTS)


def test_historical_migration_seeds_its_exact_frozen_catalog() -> None:
    """The stamped revision keeps its original catalog rather than today's one."""
    module = _historical_migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        module.upgrade()

        assert _stored_roles(connection) == {
            str(row["key"]): (
                str(row["label"]),
                str(row["description"]),
                bool(row["service_only"]),
            )
            for row in HISTORICAL_ROLE_ROWS
        }
        assert _stored_permissions(connection) == {
            str(row["key"]): (
                str(row["label"]),
                bool(row["sensitive"]),
                bool(row["audit_on_use"]),
            )
            for row in HISTORICAL_PERMISSION_ROWS
        }
        assert _stored_pairs(connection) == _historical_pairs()
        assert _BETA_CONNECTOR_EDGE in _stored_pairs(connection)
        assert _BETA_MANUAL_REVENUE_EDGE not in _stored_pairs(connection)
        assert "finance.import_manual_revenue" not in _stored_permissions(connection)
        assert _stored_roles(connection)["beta_operator"][1] == _HISTORICAL_BETA_DESCRIPTION


def test_migration_upgrade_is_idempotent() -> None:
    """Running the upgrade twice inserts nothing new and raises nothing."""
    module = _historical_migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        module.upgrade()
        first = (_stored_roles(connection), _stored_permissions(connection))
        first_pairs = _stored_pairs(connection)

        module.upgrade()

        assert (_stored_roles(connection), _stored_permissions(connection)) == first
        assert _stored_pairs(connection) == first_pairs


def test_forward_repair_converges_historical_state_and_preserves_custom_edges() -> None:
    """The new revision repairs the stamped draft without deleting operator policy."""
    historical = _historical_migration_module()
    repair = _repair_migration_module()
    engine = _security_engine()
    custom_edge = ("assistant_analyst", "audit.view")

    with engine.begin() as connection:
        _bind_operations(historical, connection)
        historical.upgrade()
        assert _BETA_CONNECTOR_EDGE in _stored_pairs(connection)
        connection.execute(
            RolePermissionAssignmentORM.__table__.insert().values(
                role_key=custom_edge[0],
                permission_key=custom_edge[1],
            )
        )

        _bind_operations(repair, connection)
        repair.upgrade()
        repair.upgrade()

        pairs = _stored_pairs(connection)
        assert _BETA_CONNECTOR_EDGE not in pairs
        assert _BETA_MANUAL_REVENUE_EDGE in pairs
        assert _SUPER_OWNER_MANUAL_REVENUE_EDGE in pairs
        assert custom_edge in pairs
        assert pairs == _repair_pairs() | {custom_edge}
        assert _stored_permissions(connection)["finance.import_manual_revenue"] == (
            "Import manual revenue facts",
            True,
            True,
        )
        assert _stored_roles(connection)["beta_operator"][1] == _REPAIR_BETA_DESCRIPTION


def test_forward_repair_seeds_exact_current_catalog_on_fresh_schema() -> None:
    """The repair is self-contained and idempotent for already-corrected databases."""
    repair = _repair_migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(repair, connection)
        repair.upgrade()
        first = (
            _stored_roles(connection),
            _stored_permissions(connection),
            _stored_pairs(connection),
        )
        repair.upgrade()

        assert (
            len(first[0]),
            len(first[1]),
            len(first[2]),
        ) == _REPAIR_COUNTS
        assert first[1] == _EXPECTED_CURRENT_PERMISSION_METADATA
        assert _role_permission_map(first[2]) == _EXPECTED_CURRENT_ROLE_PERMISSIONS
        assert (
            _stored_roles(connection),
            _stored_permissions(connection),
            _stored_pairs(connection),
        ) == first
        assert first[2] == _repair_pairs()
        assert _BETA_CONNECTOR_EDGE not in first[2]
        assert _BETA_MANUAL_REVENUE_EDGE in first[2]
        assert _SUPER_OWNER_MANUAL_REVENUE_EDGE in first[2]
        assert {
            ("connector_admin", "connectors.run_jobs"),
            ("revenue_operations_admin", "connectors.run_jobs"),
            ("super_owner", "connectors.run_jobs"),
            ("system_integration_user", "connectors.run_jobs"),
        } <= first[2]


def test_migration_refreshes_stale_catalog_metadata() -> None:
    """A pre-existing row with drifted metadata converges to the registry values."""
    module = _historical_migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO roles (key, label, description, service_only) "
                "VALUES ('finance_admin', 'stale label', 'stale description', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO permissions (key, label, sensitive, audit_on_use) "
                "VALUES ('finance.view_revenue', 'stale label', 0, 0)"
            )
        )
        _bind_operations(module, connection)
        module.upgrade()

        role_definition = ROLE_DEFINITIONS[RoleKey.FINANCE_ADMIN]
        assert _stored_roles(connection)["finance_admin"] == (
            role_definition.label,
            role_definition.description,
            role_definition.service_only,
        )
        # The refresh must restore the sensitive/audit flags, never soften them.
        assert _stored_permissions(connection)["finance.view_revenue"] == (
            PERMISSION_DEFINITIONS[Permission.VIEW_REVENUE].label,
            True,
            True,
        )


def test_migration_downgrade_preserves_the_seeded_catalog() -> None:
    """Downgrade is non-destructive: the catalog seed is not reversed."""
    module = _historical_migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        module.upgrade()
        after_upgrade_roles = _stored_roles(connection)
        after_upgrade_permissions = _stored_permissions(connection)
        after_upgrade_pairs = _stored_pairs(connection)
        module.downgrade()

        assert _stored_roles(connection) == after_upgrade_roles
        assert _stored_permissions(connection) == after_upgrade_permissions
        assert _stored_pairs(connection) == after_upgrade_pairs
        assert after_upgrade_pairs == _historical_pairs()


def test_migration_downgrade_preserves_preexisting_security_seed_catalog() -> None:
    """A SQL-preseeded catalog survives upgrade+downgrade (the P1 case)."""
    module = _historical_migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        # Simulate security_seed.sql having already populated the catalog.
        module.upgrade()
        pre_roles = _stored_roles(connection)
        pre_permissions = _stored_permissions(connection)
        pre_pairs = _stored_pairs(connection)
        # Second upgrade is metadata-refresh only; downgrade must not wipe.
        module.upgrade()
        module.downgrade()

        assert _stored_roles(connection) == pre_roles
        assert _stored_permissions(connection) == pre_permissions
        assert _stored_pairs(connection) == pre_pairs


def test_migration_downgrade_keeps_rows_a_live_assignment_still_needs() -> None:
    """Live grants remain valid because the catalog is not wiped on downgrade."""
    module = _historical_migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        module.upgrade()
        pairs_before = _stored_pairs(connection)

    user_id = uuid4()
    scope_id = uuid4()
    with Session(engine) as session:
        session.add(UserORM(id=user_id, email="ops@example.com", display_name="Ops"))
        session.add(AccessScopeORM(id=scope_id, scope_type="global", scope_id=None, label="Global"))
        session.add(
            UserRoleAssignmentORM(
                id=uuid4(),
                user_id=user_id,
                role_key="finance_admin",
                scope_id=scope_id,
                active=True,
            )
        )
        session.add(
            UserPermissionGrantORM(
                id=uuid4(),
                user_id=user_id,
                permission_key="finance.view_revenue",
                scope_id=scope_id,
                active=True,
            )
        )
        session.commit()

    with engine.begin() as connection:
        _bind_operations(module, connection)
        module.downgrade()

        assert "finance_admin" in _stored_roles(connection)
        assert "finance.view_revenue" in _stored_permissions(connection)
        assert _stored_pairs(connection) == pairs_before
        assert ("finance_admin", "finance.view_revenue") in _stored_pairs(connection)


def test_repair_downgrade_is_unconditionally_irreversible() -> None:
    """Even an empty assignment state cannot restore the unsafe beta contract."""
    repair = _repair_migration_module()
    engine = _security_engine()

    with engine.begin() as connection:
        _bind_operations(repair, connection)
        repair.upgrade()
        before = (
            _stored_roles(connection),
            _stored_permissions(connection),
            _stored_pairs(connection),
        )
        with pytest.raises(
            repair.IrreversibleAuthorizationRepairError,
            match="irreversible security repair",
        ):
            repair.downgrade()
        assert (
            _stored_roles(connection),
            _stored_permissions(connection),
            _stored_pairs(connection),
        ) == before
        assert _BETA_CONNECTOR_EDGE not in before[2]


def test_beta_operator_catalog_grants_manual_revenue_but_not_connector_execution() -> None:
    """The beta role is finance-capable without inheriting global connector runs."""
    beta_permissions = {
        permission for role, permission in _expected_pairs() if role == RoleKey.BETA_OPERATOR.value
    }

    assert Permission.IMPORT_MANUAL_REVENUE.value in beta_permissions
    assert Permission.RUN_CONNECTOR_JOBS.value not in beta_permissions


def test_historical_migration_logic_and_snapshot_semantics_are_immutable() -> None:
    """Stamped source bytes, upgrade logic, and catalog values remain historical."""
    module = _historical_migration_module()
    migration_bytes = HISTORICAL_MIGRATION_PATH.read_bytes()
    snapshot_bytes = HISTORICAL_SNAPSHOT_PATH.read_bytes()
    assert _git_blob_oid(migration_bytes) == _HISTORICAL_MIGRATION_GIT_BLOB
    assert hashlib.sha256(migration_bytes).hexdigest() == _HISTORICAL_MIGRATION_SHA256
    assert HISTORICAL_SNAPSHOT_PATH.is_file()
    assert _git_blob_oid(snapshot_bytes) == _HISTORICAL_SNAPSHOT_GIT_BLOB
    assert hashlib.sha256(snapshot_bytes).hexdigest() == _HISTORICAL_SNAPSHOT_SHA256
    assert (
        _catalog_semantic_digest(
            HISTORICAL_ROLE_ROWS,
            HISTORICAL_PERMISSION_ROWS,
            HISTORICAL_ROLE_PERMISSION_ROWS,
        )
        == _HISTORICAL_SNAPSHOT_SEMANTIC_SHA256
    )
    assert module.role_seed_rows() == HISTORICAL_ROLE_ROWS
    assert module.permission_seed_rows() == HISTORICAL_PERMISSION_ROWS
    assert module.role_permission_seed_rows() == HISTORICAL_ROLE_PERMISSION_ROWS
    assert (
        len(HISTORICAL_ROLE_ROWS),
        len(HISTORICAL_PERMISSION_ROWS),
        len(HISTORICAL_ROLE_PERMISSION_ROWS),
    ) == _HISTORICAL_COUNTS
    assert _BETA_CONNECTOR_EDGE in _historical_pairs()
    assert _BETA_MANUAL_REVENUE_EDGE not in _historical_pairs()
    assert _MANUAL_PERMISSION_ROW not in HISTORICAL_PERMISSION_ROWS


def test_repair_snapshot_matches_live_registries_and_exact_literals() -> None:
    """The new immutable snapshot is current and independently pinned."""
    module = _repair_migration_module()
    assert len(_EXPECTED_CURRENT_PERMISSION_METADATA) == _REPAIR_COUNTS[1]
    assert len(_EXPECTED_CURRENT_ROLE_PERMISSIONS) == _REPAIR_COUNTS[0]
    assert (
        sum(len(values) for values in _EXPECTED_CURRENT_ROLE_PERMISSIONS.values())
        == (_REPAIR_COUNTS[2])
    )
    live_role_rows = [
        {
            "key": role.value,
            "label": definition.label,
            "description": definition.description,
            "service_only": definition.service_only,
        }
        for role, definition in sorted(ROLE_DEFINITIONS.items(), key=lambda item: item[0].value)
    ]
    live_permission_rows = [
        {
            "key": permission.value,
            "label": definition.label,
            "sensitive": definition.sensitive,
            "audit_on_use": definition.audit_on_use,
        }
        for permission, definition in sorted(
            PERMISSION_DEFINITIONS.items(), key=lambda item: item[0].value
        )
    ]
    live_role_permission_rows = [
        {"role_key": row["role"], "permission_key": row["permission"]}
        for row in initial_role_permission_rows()
    ]
    migration_role_rows = module.role_seed_rows()
    migration_permission_rows = module.permission_seed_rows()
    migration_role_permission_rows = module.role_permission_seed_rows()

    assert len(live_role_rows) == _REPAIR_COUNTS[0]
    assert len(live_permission_rows) == _REPAIR_COUNTS[1]
    assert len(live_role_permission_rows) == _REPAIR_COUNTS[2]
    assert len(migration_role_rows) == _REPAIR_COUNTS[0]
    assert len(migration_permission_rows) == _REPAIR_COUNTS[1]
    assert len(migration_role_permission_rows) == _REPAIR_COUNTS[2]
    assert len(REPAIR_ROLE_ROWS) == _REPAIR_COUNTS[0]
    assert len(REPAIR_PERMISSION_ROWS) == _REPAIR_COUNTS[1]
    assert len(REPAIR_ROLE_PERMISSION_ROWS) == _REPAIR_COUNTS[2]

    assert migration_role_rows == live_role_rows
    assert REPAIR_ROLE_ROWS == migration_role_rows
    assert REPAIR_PERMISSION_ROWS == migration_permission_rows
    assert REPAIR_ROLE_PERMISSION_ROWS == migration_role_permission_rows
    assert (
        _catalog_semantic_digest(
            REPAIR_ROLE_ROWS,
            REPAIR_PERMISSION_ROWS,
            REPAIR_ROLE_PERMISSION_ROWS,
        )
        == _REPAIR_SNAPSHOT_SEMANTIC_SHA256
    )
    live_permission_metadata = {
        str(row["key"]): (
            str(row["label"]),
            bool(row["sensitive"]),
            bool(row["audit_on_use"]),
        )
        for row in live_permission_rows
    }
    migration_permission_metadata = {
        str(row["key"]): (
            str(row["label"]),
            bool(row["sensitive"]),
            bool(row["audit_on_use"]),
        )
        for row in migration_permission_rows
    }
    frozen_permission_metadata = {
        str(row["key"]): (
            str(row["label"]),
            bool(row["sensitive"]),
            bool(row["audit_on_use"]),
        )
        for row in REPAIR_PERMISSION_ROWS
    }
    assert len(live_permission_metadata) == _REPAIR_COUNTS[1]
    assert len(migration_permission_metadata) == _REPAIR_COUNTS[1]
    assert len(frozen_permission_metadata) == _REPAIR_COUNTS[1]
    assert live_permission_metadata == _EXPECTED_CURRENT_PERMISSION_METADATA
    assert migration_permission_metadata == _EXPECTED_CURRENT_PERMISSION_METADATA
    assert frozen_permission_metadata == _EXPECTED_CURRENT_PERMISSION_METADATA

    live_pairs = [
        (str(row["role_key"]), str(row["permission_key"])) for row in live_role_permission_rows
    ]
    migration_pairs = [
        (str(row["role_key"]), str(row["permission_key"])) for row in migration_role_permission_rows
    ]
    frozen_pairs = [
        (str(row["role_key"]), str(row["permission_key"])) for row in REPAIR_ROLE_PERMISSION_ROWS
    ]
    assert len(live_pairs) == _REPAIR_COUNTS[2]
    assert len(migration_pairs) == _REPAIR_COUNTS[2]
    assert len(frozen_pairs) == _REPAIR_COUNTS[2]
    assert len(set(live_pairs)) == _REPAIR_COUNTS[2]
    assert len(set(migration_pairs)) == _REPAIR_COUNTS[2]
    assert len(set(frozen_pairs)) == _REPAIR_COUNTS[2]
    assert _role_permission_map(live_pairs) == _EXPECTED_CURRENT_ROLE_PERMISSIONS
    assert _role_permission_map(migration_pairs) == _EXPECTED_CURRENT_ROLE_PERMISSIONS
    assert _role_permission_map(frozen_pairs) == _EXPECTED_CURRENT_ROLE_PERMISSIONS
    assert _MANUAL_PERMISSION_ROW in REPAIR_PERMISSION_ROWS
    assert next(row for row in REPAIR_ROLE_ROWS if row["key"] == "beta_operator") == {
        "key": "beta_operator",
        "label": "Beta Operator",
        "description": _REPAIR_BETA_DESCRIPTION,
        "service_only": False,
    }
    assert _BETA_CONNECTOR_EDGE not in _repair_pairs()
    assert _BETA_MANUAL_REVENUE_EDGE in _repair_pairs()
    assert _SUPER_OWNER_MANUAL_REVENUE_EDGE in _repair_pairs()


def _values_block(sql: str, header: str, *, last: bool = False) -> str:
    """Return the VALUES body of the INSERT introduced by ``header``."""
    start = sql.rindex(header) if last else sql.index(header)
    values_at = sql.index("VALUES", start)
    end = sql.index("ON CONFLICT", values_at)
    return sql[values_at + len("VALUES") : end]


def test_security_seed_sql_matches_the_python_registries() -> None:
    """The raw SQL twin declares exactly the catalog the Python registries do.

    ``security_seed.sql`` is still the documented raw-reseed path, so it must not
    drift from the registries the migration now seeds from. This is the test the
    audit found missing: nothing pinned the SQL file to ``auth/permissions.py``.
    """
    sql = SEED_SQL_PATH.read_text(encoding="utf-8")

    role_block = _values_block(sql, "INSERT INTO roles (key, label, description, service_only)")
    raw_sql_roles = [row for row in _parse_value_tuples(role_block) if len(row) == 4]
    assert len(raw_sql_roles) == _REPAIR_COUNTS[0]
    sql_roles = {
        key: (label, description, service_only == "true")
        for key, label, description, service_only in raw_sql_roles
    }
    assert len(sql_roles) == _REPAIR_COUNTS[0]
    assert sql_roles == {
        role.value: (definition.label, definition.description, definition.service_only)
        for role, definition in ROLE_DEFINITIONS.items()
    }

    permission_block = _values_block(
        sql, "INSERT INTO permissions (key, label, sensitive, audit_on_use)"
    )
    raw_sql_permissions = [
        row for row in _parse_value_tuples(permission_block) if len(row) == 4
    ]
    assert len(raw_sql_permissions) == _REPAIR_COUNTS[1]
    sql_permissions = {
        key: (label, sensitive == "true", audit_on_use == "true")
        for key, label, sensitive, audit_on_use in raw_sql_permissions
    }
    assert len(sql_permissions) == _REPAIR_COUNTS[1]
    assert sql_permissions == _EXPECTED_CURRENT_PERMISSION_METADATA
    assert sql_permissions == {
        permission.value: (definition.label, definition.sensitive, definition.audit_on_use)
        for permission, definition in PERMISSION_DEFINITIONS.items()
    }

    pair_block = _values_block(
        sql,
        "INSERT INTO role_permission_assignments (role_key, permission_key)",
        last=True,
    )
    raw_explicit_pairs = [row for row in _parse_value_tuples(pair_block) if len(row) == 2]
    assert len(raw_explicit_pairs) == _REPAIR_COUNTS[2] - _REPAIR_COUNTS[1]
    explicit_pairs = set(raw_explicit_pairs)
    assert len(explicit_pairs) == len(raw_explicit_pairs)
    # The SQL file grants super_owner every permission with a SELECT rather than
    # an explicit tuple per permission, so re-add that implicit fan-out here.
    super_owner_pairs = {
        (RoleKey.SUPER_OWNER.value, permission.value) for permission in PERMISSION_DEFINITIONS
    }
    assert len(super_owner_pairs) == _REPAIR_COUNTS[1]
    all_sql_pairs = explicit_pairs | super_owner_pairs
    assert len(all_sql_pairs) == _REPAIR_COUNTS[2]
    assert _role_permission_map(all_sql_pairs) == _EXPECTED_CURRENT_ROLE_PERMISSIONS
    assert all_sql_pairs == _expected_pairs()
    assert not (explicit_pairs & super_owner_pairs), (
        "security_seed.sql lists a super_owner pair explicitly AND via the "
        "SELECT fan-out; one of the two is now redundant."
    )
