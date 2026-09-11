"""Seed the roles / permissions / role-permission catalog.

Revision ID: 20260825_0001
Revises: 20260805_0001
Create Date: 2026-08-25

Why this revision exists
------------------------
``backend/ums_smart_revenue/db/security_seed.sql`` has always carried these rows,
but **nothing in the repository ran it** — no migration, no Makefile target, no
compose one-shot. A fresh ``alembic upgrade head`` therefore produced a database
with zero roles and zero permissions, which is an FK prerequisite for assigning
any role (``user_role_assignments.role_key -> roles.key``,
``user_permission_grants.permission_key -> permissions.key``) and makes
``UMS_AUTHZ_SOURCE=database`` unusable. Recorded as H1 in
``Docs/20_DEPLOYMENT_READINESS_AUDIT.md`` and as P0.7 in
``Docs/21_BETA_IMPLEMENTATION_PLAN.md``.

Source of truth
---------------
The rows are derived from the **live Python registries** rather than re-typed as
literals, so the seeded catalog cannot drift from what the running application
authorizes against:

* ``auth/roles.py::ROLE_DEFINITIONS``      -> ``roles``
* ``auth/permissions.py::PERMISSION_DEFINITIONS`` -> ``permissions``
* ``auth/seed.py::initial_role_permission_rows`` -> ``role_permission_assignments``

This follows the precedent set by ``20260608_0001_tenant_rls_enforcement``, which
imports the live ``db/rls.py`` allowlist instead of freezing a copy. All three
registry modules are dependency-free (stdlib ``enum``/``dataclasses`` only), so
importing them here cannot drag application wiring into the migration process.

Idempotency
-----------
Mirrors ``security_seed.sql``'s ``ON CONFLICT DO UPDATE`` semantics without
depending on a dialect-specific upsert: existing keys are refreshed in place and
only missing keys are inserted, so re-running against a database that already ran
the raw SQL seed (or an earlier copy of this revision) is a no-op plus a metadata
refresh.

What this revision deliberately does NOT seed
---------------------------------------------
* ``access_scopes`` — the ``'global'`` scope row in ``security_seed.sql`` is
  **tenant-scoped** (``db/rls.py::TENANT_SCOPED_TABLES``) and carries FORCE ROW
  LEVEL SECURITY after ``20260612_0002``. It is created on demand, per tenant, by
  ``auth/user_roles.py::_get_or_create_scope`` and
  ``auth/user_permissions.py::_get_or_create_scope``, so seeding it from a
  tenant-blind migration would be both unnecessary and wrong.
* The ``graph.view`` / ``graph.view_finance`` retirement DELETEs — already owned
  by ``20260513_0002_retire_graph_permissions``.

The three tables written here are platform-wide catalogs with no ``tenant_id``
column, so they are outside every RLS policy and this revision needs no tenant
context.

This revision redefines "a virgin database" (read before editing)
-----------------------------------------------------------------
Seeding rows is not a local act. Before this revision a freshly migrated
database held 180 rows in three tables, and **zero** rows outside
``scripts/backup_database.py::SEED_TABLES``. After it a virgin
``alembic upgrade head`` measures 38 tables / 328 rows, of which 148 sit in the
three tables below. That difference is not cosmetic: the backup content gate
uses "every table outside ``SEED_TABLES`` is empty" as the one signal that tells
a healthy database from one that was wiped and re-migrated, and it is a
fail-closed refusal with no override. Adding these rows silently satisfied that
signal and switched the refusal off, in a file this revision never mentions.

``SEED_TABLES`` therefore now lists ``roles``, ``permissions`` and
``role_permission_assignments``, and
``tests/scripts/test_backup_content_gate.py`` keeps the two in step by parsing
the migrations rather than re-typing the names. That parser recognises exactly
two idioms: ``op.bulk_insert`` whose first argument is an ``sa.table(...)`` —
inline, or a module-level binding such as ``_ROLES`` below — and a literal
SQL insert statement handed to ``op.execute``. Every seeding path here uses the
first. Rewriting them into a third idiom (``bind.execute(_ROLES.insert(), rows)``
is the tempting one, since the refresh half already uses ``bind.execute``) drops
the table from the parser's view; the guard fails loudly rather than silently,
but the fix is to keep the idiom or to update the parser deliberately.

Prose in this file is parsed too. The parser walks every string constant in the
module, docstrings included, so writing a sample insert statement here can
register a table that does not exist — it did exactly that on the first draft of
this section, which is why the sample is described rather than quoted.

So: if a later revision seeds another table, or this one changes how it inserts,
re-measure the virgin state in the same commit.
"""

import sqlalchemy as sa
from alembic import op

from ums_smart_revenue.db.frozen_security_catalog import (
    FROZEN_PERMISSION_ROWS,
    FROZEN_ROLE_PERMISSION_ROWS,
    FROZEN_ROLE_ROWS,
)

revision = "20260825_0001"
down_revision = "20260805_0001"
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
    _seed_role_permission_assignments(bind)


# ============================================================================
# Purpose: Data-only rollback for this revision is intentionally non-destructive.
#   Upgrade is insert-missing + metadata refresh: on a database that already ran
#   ``security_seed.sql`` (a state upgrade explicitly supports) it inserts zero
#   catalog rows. Provenance of any given canonical pair cannot be recovered later,
#   so deleting ``role_permission_seed_rows()`` on downgrade would destroy
#   pre-existing authorization catalog state this revision did not create.
#   Operator-added non-canonical pairs would survive a registry wipe, but the
#   H1/P0.7 catalog itself would not. Leaving the catalog in place keeps
#   PostgreSQL authorization parents intact; re-upgrade remains idempotent.
# Database/ORM: ``roles``, ``permissions``, ``role_permission_assignments`` —
#   left unchanged. No Alembic ``op.*`` DDL.
# Standards: Fail-closed for authorization catalog integrity over perfect
#   reverse symmetry. Documented irreversible data seed (same family as other
#   seed revisions that refuse a destructive reverse).
# Blast Radius: Authorization catalog rows persist after ``alembic downgrade`` of
#   this revision. No finance numbers, no RLS widening, no permission softening.
# Connections:
#   - File: Docs/20_DEPLOYMENT_READINESS_AUDIT.md -> H1 / P0.7.
#   - File: tests/db/test_security_role_permission_seed_migration.py -> guards.
# ============================================================================
def downgrade() -> None:
    """Leave the authorization catalog in place; this seed is not reversed."""
    # Non-destructive by design: upgrade is insert-missing + metadata refresh, so
    # provenance of canonical pairs cannot be recovered. Touch the bind so this
    # path is an intentional no-op statement rather than an empty body.
    _ = op.get_bind()


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
