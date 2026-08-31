# ============================================================================
# Purpose: Provide the tenant-scoped external-identity lookup repository needed
#   by a future trusted-gateway adapter (Docs/23 A7).
# Database/ORM: external_identities, users.
# Standards: Repository-only SQLAlchemy access; fail-closed when unmapped and
#   fail-closed on malformed claims (A7: missing or malformed identities must
#   never resolve a real UMS user).
# Blast Radius: Authorization schema/read foundation; principal loading is not wired.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> X-User-ID UUID gate.
#   - File: Docs/23_ADMIN_ACCESS_AND_CONFIG_PLAN.md -> A7 acceptance criteria.
# ============================================================================
"""External-identity repository foundation.

This module deliberately does not alter trusted-gateway headers, principal
loading, user provisioning, or home-org authorization. Those require their own
permission-gated, audited integration slice after this schema foundation.

Malformed claims (blank, whitespace-padded, or control-character-bearing
provider subjects and emails) raise :class:`InvalidExternalIdentityClaimError`
instead of querying: an adapter bug that defaults missing claims to empty
strings must fail closed, not resolve a row an owner loaded with blank keys.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import ExternalIdentityORM

_MAX_CLAIM_LENGTH = 512


class ExternalIdentityError(Exception):
    """Base class for external-identity repository failures."""


class InvalidExternalIdentityClaimError(ExternalIdentityError, ValueError):
    """A claim was malformed; authentication must be refused, not fall back."""


class ExternalIdentityStorageError(ExternalIdentityError):
    """The identity mapping could not be read from storage."""


def _require_claim(kind: str, value: str) -> str:
    """Return ``value`` when it is a nonblank, trimmed, control-free claim.

    Mirrors the database CHECK constraints on ``external_identities`` so a
    malformed claim fails at the repository boundary even when the row was
    loaded before the constraints existed.
    """
    if not isinstance(value, str):
        raise InvalidExternalIdentityClaimError(f"{kind} must be a string")
    if not value or value != value.strip():
        raise InvalidExternalIdentityClaimError(f"{kind} must be nonblank and trimmed")
    if len(value) > _MAX_CLAIM_LENGTH:
        raise InvalidExternalIdentityClaimError(f"{kind} exceeds {_MAX_CLAIM_LENGTH} characters")
    if any(char.isspace() for char in value):
        raise InvalidExternalIdentityClaimError(f"{kind} must not contain whitespace")
    return value


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

    # ========================================================================
    # Purpose: Resolve one exact provider subject inside an explicit tenant.
    # Database/ORM: external_identities via ExternalIdentityORM.
    # Standards: Parameterized SQLAlchemy read; absent mappings return None;
    #   malformed claims raise; storage failures translate to a typed error.
    # Blast Radius: Future principal loading; no authorization is inferred here.
    # Connections:
    #   - File: backend/ums_smart_revenue/db/security_models.py -> Tenant FK/RLS model.
    # ========================================================================
    def resolve_user_id(
        self,
        *,
        tenant_id: UUID,
        provider: str,
        provider_subject: str,
    ) -> UUID | None:
        _require_claim("provider", provider)
        _require_claim("provider_subject", provider_subject)
        try:
            return self._session.scalar(
                select(ExternalIdentityORM.user_id).where(
                    ExternalIdentityORM.tenant_id == tenant_id,
                    ExternalIdentityORM.provider == provider,
                    ExternalIdentityORM.provider_subject == provider_subject,
                )
            )
        except SQLAlchemyError as exc:
            raise ExternalIdentityStorageError(
                "Unable to load external identity mapping"
            ) from exc

    # ========================================================================
    # Purpose: Resolve a provider mapping by case-insensitive normalized email
    #   within an explicit tenant.
    # Database/ORM: external_identities via ExternalIdentityORM.
    # Standards: Parameterized SQLAlchemy read; absent mappings return None;
    #   malformed claims raise; storage failures translate to a typed error.
    # Blast Radius: Future principal loading; no provisioning or grants occur.
    # Connections:
    #   - File: backend/ums_smart_revenue/db/security_models.py -> Email unique index.
    # ========================================================================
    def resolve_by_email(
        self,
        *,
        tenant_id: UUID,
        provider: str,
        normalized_email: str,
    ) -> UUID | None:
        _require_claim("provider", provider)
        _require_claim("normalized_email", normalized_email)
        try:
            return self._session.scalar(
                select(ExternalIdentityORM.user_id).where(
                    ExternalIdentityORM.tenant_id == tenant_id,
                    ExternalIdentityORM.provider == provider,
                    func.lower(ExternalIdentityORM.normalized_email) == normalized_email.lower(),
                )
            )
        except SQLAlchemyError as exc:
            raise ExternalIdentityStorageError(
                "Unable to load external identity mapping"
            ) from exc
