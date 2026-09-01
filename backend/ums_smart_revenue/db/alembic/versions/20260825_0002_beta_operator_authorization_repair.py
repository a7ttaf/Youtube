"""Converge the beta-operator authorization catalog without rewriting history.

Revision ID: 20260825_0002
Revises: 20260825_0001
Create Date: 2026-08-31

``20260825_0001`` and ``db/frozen_security_catalog.py`` may already be stamped
and are therefore immutable. That historical snapshot contains 17 roles, 26
permissions, and 121 role-permission edges. In particular, it grants
``beta_operator`` the broad ``connectors.run_jobs`` permission and does not know
the bounded ``finance.import_manual_revenue`` permission.

Historical documentation erratum: despite ``20260825_0001`` saying its rows are
derived from live Python registries, the published code imports the frozen
``db/frozen_security_catalog.py`` snapshot. Its published counts are also 17
roles + 26 permissions + 121 edges = 164 catalog rows; with the 180 pre-existing
rows stated there, the virgin-database total was 344, not 328. The incorrect
historical prose remains byte-for-byte untouched so its published Git blob stays
auditable; this forward revision records the correction.

This forward-only repair supports both database states that can exist in the
field: the original historical seed and a database that ran the briefly amended
copy of that same revision. Upgrade is idempotent in both cases. It refreshes the
current role/permission metadata, inserts missing canonical rows, removes only
the unsafe beta-operator connector edge, and preserves every other custom edge.
The immutable snapshot for this revision lives in
``db/frozen_security_catalog_20260825_0002.py``; later registry changes require
another revision and another snapshot.

The three catalog tables are platform-wide and outside tenant RLS. This revision
is an irreversible security floor: downgrading would restore the historical
beta-role contract that grants broad connector execution, even when no active
assignment exists. Its downgrade therefore refuses unconditionally before any
database read or write. Rollback requires a reviewed reset/redeploy plan, not an
Alembic downgrade across ``20260825_0002``.

Fresh upgrade through both revisions converges to 17 roles, 27 permissions, and
122 role-permission edges (166 catalog rows). No finance fact, formula, lock,
override, reconciliation, payment, or export row is read or changed.
"""

import sqlalchemy as sa
from alembic import op

from ums_smart_revenue.db.frozen_security_catalog_20260825_0002 import (
    FROZEN_PERMISSION_ROWS,
    FROZEN_ROLE_PERMISSION_ROWS,
    FROZEN_ROLE_ROWS,
)

revision = "20260825_0002"
down_revision = "20260825_0001"
branch_labels = None
depends_on = None

_ROLES = sa.table(
    "roles",
    sa.column("key", sa.Text()),
    sa.column("label", sa.Text()),
    sa.column("description", sa.Text()),
    sa.column("service_only", sa.Boolean()),
)
_PERMISSIONS = sa.table(
    "permissions",
    sa.column("key", sa.Text()),
    sa.column("label", sa.Text()),
    sa.column("sensitive", sa.Boolean()),
    sa.column("audit_on_use", sa.Boolean()),
)
_ROLE_PERMISSIONS = sa.table(
    "role_permission_assignments",
    sa.column("role_key", sa.Text()),
    sa.column("permission_key", sa.Text()),
)
_BETA_OPERATOR_ROLE = "beta_operator"
_UNSAFE_BETA_CONNECTOR_PERMISSION = "connectors.run_jobs"


class IrreversibleAuthorizationRepairError(RuntimeError):
    """Downgrade would restore an authorization contract removed for safety."""


def role_seed_rows() -> list[dict[str, object]]:
    """Return the frozen ``roles`` catalog rows for this revision."""
    return [dict(row) for row in FROZEN_ROLE_ROWS]


def permission_seed_rows() -> list[dict[str, object]]:
    """Return the frozen ``permissions`` catalog rows for this revision."""
    return [dict(row) for row in FROZEN_PERMISSION_ROWS]


def role_permission_seed_rows() -> list[dict[str, object]]:
    """Return the frozen ``role_permission_assignments`` rows for this revision."""
    return [dict(row) for row in FROZEN_ROLE_PERMISSION_ROWS]


def upgrade() -> None:
    """Seed (or refresh) the role, permission, and role-permission catalogs."""
    bind = op.get_bind()
    _seed_roles(bind)
    _seed_permissions(bind)
    _remove_unsafe_beta_connector_edge(bind)
    _seed_role_permission_assignments(bind)


# ============================================================================
# Purpose: Refuse every downgrade across this irreversible security repair.
#   Returning to 20260825_0001 restores beta_operator's broad connector-job
#   authorization contract even on an empty database, so data-dependent guards
#   cannot make that rollback safe.
# Database/ORM: None. The refusal occurs before acquiring or mutating a bind.
# Standards: Typed fail-closed boundary with operator-safe recovery guidance.
#   The Alembic transaction/version stamp remains at 20260825_0002.
# Blast Radius: Authorization rollback is intentionally unavailable. No finance
#   number, RLS posture, catalog row, assignment, or audit row is touched.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260825_0001_security_role_permission_seed.py -> immutable predecessor.
#   - File: tests/db/test_security_role_permission_seed_migration.py -> guards.
# ============================================================================
def downgrade() -> None:
    """Refuse rollback across the irreversible beta authorization repair."""
    # FIX: The historical revision grants beta_operator broad connector-job
    # execution. Absence of active grants does not make restoring that policy
    # safe, so downgrade must refuse before any RLS-dependent inspection.
    raise IrreversibleAuthorizationRepairError(
        "Cannot downgrade across 20260825_0002: this irreversible security repair "
        "removed beta_operator's broad connectors.run_jobs authorization. Keep the "
        "database at 20260825_0002 or later. Rollback requires a reviewed database "
        "reset/redeploy plan, not an Alembic downgrade."
    )


# ============================================================================
# Purpose: Idempotently seed the platform-wide ``roles`` catalog. Missing keys
#   are inserted; keys that already exist have their admin-facing metadata
#   refreshed to the values in ``auth/roles.py`` so a database seeded by an older
#   copy of ``security_seed.sql`` converges instead of silently diverging.
# Database/ORM: ``roles`` (RoleORM). Platform-wide catalog, no ``tenant_id``, so
#   no RLS policy applies and no tenant context is required.
# Standards: Dialect-portable SELECT/INSERT/UPDATE (no ``ON CONFLICT``), so the
#   SQLite migration-testing path executes the same statements as PostgreSQL.
#   ``service_only`` is copied verbatim from the registry — it is the flag that
#   stops a service-only role being assigned to a human account, so it must never
#   be softened here.
# Blast Radius: Authorization — this is the FK parent for every role assignment.
#   Adds rows only; never deletes and never grants anything to a user.
# Connections:
#   - File: backend/ums_smart_revenue/auth/roles.py -> ROLE_DEFINITIONS source.
#   - File: backend/ums_smart_revenue/db/security_seed.sql -> the raw-SQL twin
#     this revision replaces as the executed path.
#   - File: tests/db/test_security_role_permission_seed_migration.py -> guard.
# ============================================================================
def _seed_roles(bind: sa.engine.Connection) -> None:
    """Insert missing role rows and refresh the metadata of existing ones."""
    rows = role_seed_rows()
    existing = set(bind.execute(sa.select(_ROLES.c.key)).scalars())
    missing = [row for row in rows if row["key"] not in existing]
    if missing:
        op.bulk_insert(_ROLES, missing)
    for row in rows:
        if row["key"] not in existing:
            continue
        bind.execute(
            _ROLES.update()
            .where(_ROLES.c.key == row["key"])
            .values(
                label=row["label"],
                description=row["description"],
                service_only=row["service_only"],
            )
        )


# ============================================================================
# Purpose: Idempotently seed the platform-wide ``permissions`` catalog, including
#   the ``sensitive`` and ``audit_on_use`` metadata that drives sensitive-value
#   masking and audit-on-read behaviour.
# Database/ORM: ``permissions`` (PermissionORM). Platform-wide catalog, no
#   ``tenant_id``, so no RLS policy applies.
# Standards: Dialect-portable SELECT/INSERT/UPDATE. The refresh copies the
#   registry values verbatim; it is the same convergence ``security_seed.sql``
#   performs with ``ON CONFLICT (key) DO UPDATE``, so it cannot mark a
#   code-sensitive permission as non-sensitive.
# Blast Radius: Authorization and audit — FK parent for every direct permission
#   grant, and the source of the sensitivity/audit flags read at request time.
# Connections:
#   - File: backend/ums_smart_revenue/auth/permissions.py -> PERMISSION_DEFINITIONS.
#   - File: backend/ums_smart_revenue/db/security_seed.sql -> raw-SQL twin.
# ============================================================================
def _seed_permissions(bind: sa.engine.Connection) -> None:
    """Insert missing permission rows and refresh the metadata of existing ones."""
    rows = permission_seed_rows()
    existing = set(bind.execute(sa.select(_PERMISSIONS.c.key)).scalars())
    missing = [row for row in rows if row["key"] not in existing]
    if missing:
        op.bulk_insert(_PERMISSIONS, missing)
    for row in rows:
        if row["key"] not in existing:
            continue
        bind.execute(
            _PERMISSIONS.update()
            .where(_PERMISSIONS.c.key == row["key"])
            .values(
                label=row["label"],
                sensitive=row["sensitive"],
                audit_on_use=row["audit_on_use"],
            )
        )


# ============================================================================
# Purpose: Remove the exact unsafe edge shipped by the earlier PR #223 draft.
#   ``connectors.run_jobs`` authorizes every connector and is not a substitute
#   for the bounded manual revenue-upload workflow.
# Database/ORM: ``role_permission_assignments`` only.
# Standards: Parameterized SQLAlchemy delete scoped to one known role/permission
#   pair; no user-supplied SQL and no widening of any other role.
# Blast Radius: Authorization tightens beta_operator; connector admins and
#   service integrations retain their connector execution grants.
# Connections:
#   - File: backend/ums_smart_revenue/auth/seed.py -> corrected beta role grants.
#   - File: backend/ums_smart_revenue/api/revenue.py -> dedicated manual-fact gate.
# ============================================================================
def _remove_unsafe_beta_connector_edge(bind: sa.engine.Connection) -> None:
    """Delete only beta_operator's obsolete global connector execution edge."""
    bind.execute(
        _ROLE_PERMISSIONS.delete().where(
            sa.and_(
                _ROLE_PERMISSIONS.c.role_key == _BETA_OPERATOR_ROLE,
                _ROLE_PERMISSIONS.c.permission_key == _UNSAFE_BETA_CONNECTOR_PERMISSION,
            )
        )
    )


# ============================================================================
# Purpose: Idempotently seed the (role, permission) catalog edges. Only pairs the
#   registry declares are inserted; an operator-added pair is never removed here.
# Database/ORM: ``role_permission_assignments`` (RolePermissionAssignmentORM),
#   FK-dependent on ``roles`` and ``permissions`` seeded above.
# Standards: Reads the existing pair set once and inserts only the difference, so
#   a re-run cannot violate the composite primary key.
# Blast Radius: Authorization — this table is what turns a role assignment into
#   an effective permission set. Insert-only.
# Connections:
#   - File: backend/ums_smart_revenue/auth/seed.py -> initial_role_permission_rows.
#   - File: backend/ums_smart_revenue/auth/policy.py -> consumes the resolved set.
# ============================================================================
def _seed_role_permission_assignments(bind: sa.engine.Connection) -> None:
    """Insert the registry's (role, permission) pairs that are not stored yet."""
    existing = {
        (row.role_key, row.permission_key)
        for row in bind.execute(
            sa.select(_ROLE_PERMISSIONS.c.role_key, _ROLE_PERMISSIONS.c.permission_key)
        )
    }
    missing = [
        row
        for row in role_permission_seed_rows()
        if (row["role_key"], row["permission_key"]) not in existing
    ]
    if missing:
        op.bulk_insert(_ROLE_PERMISSIONS, missing)
