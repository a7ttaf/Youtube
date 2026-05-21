from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID


class SecurityBase(DeclarativeBase):
    """Declarative base for authorization and audit SQL tables."""

    pass


# Shared ``server_default`` text expression for the ``tenant_id`` column
# added by migration 20260517_0001. Sourcing it from a single constant
# keeps every model + migration in lockstep with the tenant #1 UUID
# seeded in 20260516_0001.
_TENANT_ID_DEFAULT = text(f"'{UMS_TENANT_ID}'")
_TENANT_ID_DEFAULT_VALUE = UUID(UMS_TENANT_ID)


class UserORM(SecurityBase):
    """User account row with human/service lifecycle invariants."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    is_service_account: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id_id"),
        CheckConstraint(
            "status IN ('active', 'disabled', 'service')", name="ck_users_status"
        ),
        CheckConstraint(
            "(is_service_account = true AND status IN ('service', 'disabled')) "
            "OR (is_service_account = false AND status IN ('active', 'disabled'))",
            name="ck_users_service_account_status",
        ),
        Index("uq_users_email_lower", "tenant_id", func.lower(email), unique=True),
        Index("ix_users_tenant_id", "tenant_id"),
    )


class RoleORM(SecurityBase):
    """Role catalog row seeded into the authorization model.

    Platform-wide definition catalog — does NOT carry ``tenant_id``.
    """

    __tablename__ = "roles"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    service_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PermissionORM(SecurityBase):
    """Permission catalog row including sensitivity and audit metadata.

    Platform-wide definition catalog — does NOT carry ``tenant_id``.
    """

    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    sensitive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    audit_on_use: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AccessScopeORM(SecurityBase):
    """Normalized authorization scope row for global and entity scopes."""

    __tablename__ = "access_scopes"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('global', 'sector', 'company', 'channel', "
            "'finance-month', 'export', 'connector')",
            name="ck_access_scopes_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'global' AND scope_id IS NULL) "
            "OR (scope_type <> 'global' AND scope_id IS NOT NULL)",
            name="ck_access_scopes_scope_id_required_by_type",
        ),
        Index(
            "uq_access_scopes_scope_type_scope_id",
            "tenant_id",
            "scope_type",
            "scope_id",
            unique=True,
            postgresql_where=text("scope_id IS NOT NULL"),
            sqlite_where=text("scope_id IS NOT NULL"),
        ),
        Index(
            "uq_access_scopes_global_singleton",
            "tenant_id",
            "scope_type",
            unique=True,
            postgresql_where=text("scope_type = 'global' AND scope_id IS NULL"),
            sqlite_where=text("scope_type = 'global' AND scope_id IS NULL"),
        ),
        Index("ix_access_scopes_tenant_id", "tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_access_scopes_tenant_id_id",
        ),
    )


class RolePermissionAssignmentORM(SecurityBase):
    """Static mapping from roles to permissions.

    Platform-wide definition catalog — does NOT carry ``tenant_id``.
    """

    __tablename__ = "role_permission_assignments"

    role_key: Mapped[str] = mapped_column(
        ForeignKey("roles.key", ondelete="CASCADE"), primary_key=True
    )
    permission_key: Mapped[str] = mapped_column(
        ForeignKey("permissions.key", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserRoleAssignmentORM(SecurityBase):
    """Scoped role assignment row with explicit revocation state."""

    __tablename__ = "user_role_assignments"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role_key: Mapped[str] = mapped_column(
        ForeignKey("roles.key", ondelete="RESTRICT"), nullable=False
    )
    scope_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assigned_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        CheckConstraint(
            "(active = true AND revoked_at IS NULL) "
            "OR (active = false AND revoked_at IS NOT NULL)",
            name="ck_user_role_assignments_revocation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scope_id"],
            ["access_scopes.tenant_id", "access_scopes.id"],
            name="fk_user_role_assignments_tenant_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_user_role_assignments_tenant_user",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assigned_by"],
            ["users.tenant_id", "users.id"],
            name="fk_user_role_assignments_tenant_assigned_by",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "revoked_by"],
            ["users.tenant_id", "users.id"],
            name="fk_user_role_assignments_tenant_revoked_by",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_active_user_role_scope",
            "tenant_id",
            "user_id",
            "role_key",
            "scope_id",
            unique=True,
            postgresql_where=text("active = true"),
            sqlite_where=text("active = true"),
        ),
        Index("ix_user_role_assignments_user_id", "user_id"),
        Index("ix_user_role_assignments_tenant_id", "tenant_id"),
    )


class UserPermissionGrantORM(SecurityBase):
    """Scoped direct permission grant row with explicit revocation state."""

    __tablename__ = "user_permission_grants"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    permission_key: Mapped[str] = mapped_column(
        ForeignKey("permissions.key", ondelete="RESTRICT"), nullable=False
    )
    scope_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    granted_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        CheckConstraint(
            "(active = true AND revoked_at IS NULL) "
            "OR (active = false AND revoked_at IS NOT NULL)",
            name="ck_user_permission_grants_revocation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scope_id"],
            ["access_scopes.tenant_id", "access_scopes.id"],
            name="fk_user_permission_grants_tenant_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_user_permission_grants_tenant_user",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "granted_by"],
            ["users.tenant_id", "users.id"],
            name="fk_user_permission_grants_tenant_granted_by",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "revoked_by"],
            ["users.tenant_id", "users.id"],
            name="fk_user_permission_grants_tenant_revoked_by",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_active_user_permission_scope",
            "tenant_id",
            "user_id",
            "permission_key",
            "scope_id",
            unique=True,
            postgresql_where=text("active = true"),
            sqlite_where=text("active = true"),
        ),
        Index("ix_user_permission_grants_user_id", "user_id"),
        Index("ix_user_permission_grants_tenant_id", "tenant_id"),
    )


class AuditLogORM(SecurityBase):
    """Append-only audit log row for sensitive authorization operations."""

    __tablename__ = "audit_logs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    sensitive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_audit_logs_tenant_user",
            ondelete="RESTRICT",
        ),
        Index("ix_audit_logs_user_created", "user_id", "created_at"),
        Index("ix_audit_logs_event_created", "event_type", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_tenant_id", "tenant_id"),
    )


class ApiConnectorCredentialORM(SecurityBase):
    """Connector credential reference row without raw secret material."""

    __tablename__ = "api_connector_credentials"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    connector_key: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    updated_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled', 'rotating', 'failed_auth')",
            name="ck_connector_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["users.tenant_id", "users.id"],
            name="fk_api_connector_credentials_tenant_created_by",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "updated_by"],
            ["users.tenant_id", "users.id"],
            name="fk_api_connector_credentials_tenant_updated_by",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_api_connector_credentials_connector_account",
            "tenant_id",
            "connector_key",
            "account_id",
            unique=True,
        ),
        Index("ix_api_connector_credentials_tenant_id", "tenant_id"),
    )
