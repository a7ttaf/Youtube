"""Read-only org-unit listing for the Registry view's name/structure metadata.

Surfaces active org units (HOLDING / SECTOR / COMPANY) for the current request
tenant so the frontend can label channels by company and sector. This is
structure metadata, not a financial number and not an authorization source:
PostgreSQL remains the source of truth and the read mirrors the tenant-scoped,
active-only query already used to build the org-access index.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.org_models import OrgUnitORM
from ums_smart_revenue.tenancy.context import require_current_tenant


@dataclass(frozen=True)
class OrgUnitEntry:
    """Immutable view of one active org unit for the GET /org-units response."""

    id: str
    parent_id: str | None
    type: str
    name: str
    active: bool

    def to_api(self) -> dict[str, object]:
        """Serialize to the fixed GET /org-units item shape (UUIDs already str)."""
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "type": self.type,
            "name": self.name,
            "active": self.active,
        }


# ============================================================================
# Purpose: Tenant-scoped, active-only read of org units for the Registry view,
#   ordered deterministically so the frontend's name lookups are stable.
# Database/ORM: OrgUnitORM (org_units). Read-only SELECT; no writes.
# Standards: Typed boundary (OrgUnitEntry); SQLAlchemy parameterization via the
#   ORM; tenant resolved from request context, never from caller input.
# Blast Radius: None detected. Read-only structure metadata; does not feed
#   finance totals, authorization, audit, Neo4j, or exports. Mirrors the
#   active-only/tenant filters of load_org_access_index_from_session so the
#   listing can never expose a unit authorization itself cannot see.
# Connections:
#   - File: backend/ums_smart_revenue/org/access_index.py ->
#       load_org_access_index_from_session (the query shape mirrored here).
#   - File: backend/ums_smart_revenue/api/org_units.py -> the GET route caller.
# ============================================================================
class SqlAlchemyOrgUnitReader:
    """SQL-backed reader returning active org units for the current tenant."""

    def __init__(self, session: Session) -> None:
        """Bind the reader to a request-scoped SQLAlchemy session."""
        self._session = session

    def list_active_units(self) -> list[OrgUnitEntry]:
        """Return active units for the current tenant, ordered (type, name, id)."""
        tenant_id = require_current_tenant().id
        rows = self._session.execute(
            select(
                OrgUnitORM.id,
                OrgUnitORM.parent_id,
                OrgUnitORM.type,
                OrgUnitORM.name,
                OrgUnitORM.active,
            )
            .where(
                OrgUnitORM.tenant_id == tenant_id,
                OrgUnitORM.active.is_(True),
            )
            .order_by(OrgUnitORM.type, OrgUnitORM.name, OrgUnitORM.id)
        ).all()
        return [
            OrgUnitEntry(
                id=str(row.id),
                parent_id=str(row.parent_id) if row.parent_id is not None else None,
                type=row.type,
                name=row.name,
                active=row.active,
            )
            for row in rows
        ]
