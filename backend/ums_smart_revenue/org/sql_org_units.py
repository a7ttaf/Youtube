# ============================================================================
# Purpose: SQLAlchemy write repository for ``org_units`` — the guarded
#   insert-or-read used by the bootstrap CLI. (``scripts/seed_demo_month.py``
#   still inserts its own demo skeleton directly; this module owns the
#   operator-facing path, not every org_units write in the repo.)
# Database/ORM: OrgUnitORM (org_units). Single-row get + savepointed insert;
#   the self-referential composite FK (tenant_id, parent_id) -> (tenant_id, id)
#   requires a flushed parent before a child insert.
# Standards: Deterministic caller-supplied ids; a concurrent-insert race is
#   confined to a savepoint and resolved by re-reading the winning row so the
#   loser still fails closed downstream via the caller's drift validation.
# Blast Radius: Registry/org mapping rows only; callers own tenant-context,
#   authorization, drift policy, and audit.
# Connections:
#   - File: backend/ums_smart_revenue/db/org_models.py -> OrgUnitORM.
#   - File: scripts/bootstrap_operator.py -> the --org-skeleton writer.
#   - File: scripts/seed_demo_month.py -> the guarded-insert pattern mirrored.
# ============================================================================
"""SQLAlchemy org-unit write repository for bootstrap/seed tooling."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.org_models import OrgUnitORM


# ============================================================================
# Purpose: Return the org_units row at the caller's deterministic id, creating
#   it under a savepoint when absent — the bootstrap CLI's write path for the
#   operator org skeleton (demo seeding keeps its own direct insert).
# Database/ORM: OrgUnitORM (org_units): primary-key get, savepointed insert,
#   re-read of the winning row when a concurrent deterministic insert wins
#   the race (IntegrityError confined to the savepoint).
# Standards: Deterministic caller-supplied ids; a created unit is always
#   active; the loser's re-read row still flows through the caller's drift
#   validation so a concurrent writer can never fail this path open.
# Blast Radius: Registry/org mapping rows only; tenant context, drift policy,
#   and audit are caller-owned.
# Connections:
#   - File: backend/ums_smart_revenue/db/org_models.py -> OrgUnitORM.
#   - File: scripts/bootstrap_operator.py -> --org-skeleton caller.
# ============================================================================
def ensure_org_unit_row(
    session: Session,
    *,
    unit_id,
    tenant_id,
    parent_id,
    unit_type: str,
    name: str,
) -> tuple[OrgUnitORM, bool]:
    """Return the stored org unit, inserting it under a savepoint if absent.

    Returns ``(row, created)`` where ``created`` is True only when THIS call
    wrote the row. Deterministic ids mean a concurrent writer can win between
    the read and the insert; the loser's savepoint rolls back and the winning
    row is re-read so the caller's drift validation still applies to it.
    """
    row = session.get(OrgUnitORM, unit_id)
    if row is not None:
        return row, False
    candidate = OrgUnitORM(
        id=unit_id,
        tenant_id=tenant_id,
        parent_id=parent_id,
        type=unit_type,
        name=name,
        active=True,
    )
    try:
        with session.begin_nested():
            session.add(candidate)
            session.flush()
        return candidate, True
    except IntegrityError:
        row = session.get(OrgUnitORM, unit_id)
        if row is None:
            raise
        return row, False
