# ============================================================================
# Purpose: Resolve external IdP identities to internal UMS user UUIDs for the
#   trusted-gateway adapter (Docs/23 A7).
# Database/ORM: external_identities, users.
# Standards: Repository-only SQLAlchemy access; fail-closed when unmapped.
# Blast Radius: Authorization principal loading only.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> X-User-ID UUID gate.
#   - File: Docs/23_ADMIN_ACCESS_AND_CONFIG_PLAN.md -> A7 acceptance criteria.
# ============================================================================
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import ExternalIdentityORM


@dataclass(frozen=True)
class ExternalIdentityRecord:
    provider: str
    provider_subject: str
    normalized_email: str
    user_id: UUID


class SqlAlchemyExternalIdentityRepository:
    """Tenant-scoped lookup for mapped external identities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def resolve_user_id(
        self,
        *,
        tenant_id: UUID,
        provider: str,
        provider_subject: str,
    ) -> UUID | None:
        return self._session.scalar(
            select(ExternalIdentityORM.user_id).where(
                ExternalIdentityORM.tenant_id == tenant_id,
                ExternalIdentityORM.provider == provider,
                ExternalIdentityORM.provider_subject == provider_subject,
            )
        )

    def resolve_by_email(
        self,
        *,
        tenant_id: UUID,
        provider: str,
        normalized_email: str,
    ) -> UUID | None:
        return self._session.scalar(
            select(ExternalIdentityORM.user_id).where(
                ExternalIdentityORM.tenant_id == tenant_id,
                ExternalIdentityORM.provider == provider,
                func.lower(ExternalIdentityORM.normalized_email) == normalized_email.lower(),
            )
        )
