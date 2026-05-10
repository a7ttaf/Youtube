from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, JSON, Text, Uuid, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class SecurityBase(DeclarativeBase):
    pass


class UserORM(SecurityBase):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    is_service_account: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled', 'service')", name="ck_users_status"),
        Index("uq_users_email_lower", func.lower(email), unique=True),
    )


class RoleORM(SecurityBase):
    __tablename__ = "roles"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    service_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PermissionORM(SecurityBase):
    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    audit_on_use: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AccessScopeORM(SecurityBase):
    __tablename__ = "access_scopes"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('global', 'sector', 'company', 'channel', 'finance-month', 'export', 'connector', 'graph-read')",
            name="ck_access_scopes_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'global' AND scope_id IS NULL) OR (scope_type <> 'global' AND scope_id IS NOT NULL)",
            name="ck_access_scopes_scope_id_required_by_type",
        ),
        Index(
            "uq_access_scopes_scope_type_scope_id",
            "scope_type",
            "scope_id",
            unique=True,
            postgresql_where=text("scope_id IS NOT NULL"),
        ),
        Index(
            "uq_access_scopes_global_singleton",
            "scope_type",
            unique=True,
            postgresql_where=text("scope_type = 'global' AND scope_id IS NULL"),
        ),
    )


class RolePermissionAssignmentORM(SecurityBase):
    __tablename__ = "role_permission_assignments"

    role_key: Mapped[str] = mapped_column(ForeignKey("roles.key", ondelete="CASCADE"), primary_key=True)
    permission_key: Mapped[str] = mapped_column(ForeignKey("permissions.key", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UserRoleAssignmentORM(SecurityBase):
    __tablename__ = "user_role_assignments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role_key: Mapped[str] = mapped_column(ForeignKey("roles.key", ondelete="RESTRICT"), nullable=False)
    scope_id: Mapped[UUID] = mapped_column(ForeignKey("access_scopes.id", ondelete="RESTRICT"), nullable=False)
    assigned_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint(
            "(active = true AND revoked_at IS NULL) OR (active = false AND revoked_at IS NOT NULL)",
            name="ck_user_role_assignments_revocation",
        ),
        Index(
            "uq_active_user_role_scope",
            "user_id",
            "role_key",
            "scope_id",
            unique=True,
            postgresql_where=text("active = true"),
        ),
        Index("ix_user_role_assignments_user_id", "user_id"),
    )


class UserPermissionGrantORM(SecurityBase):
    __tablename__ = "user_permission_grants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    permission_key: Mapped[str] = mapped_column(ForeignKey("permissions.key", ondelete="RESTRICT"), nullable=False)
    scope_id: Mapped[UUID] = mapped_column(ForeignKey("access_scopes.id", ondelete="RESTRICT"), nullable=False)
    granted_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint(
            "(active = true AND revoked_at IS NULL) OR (active = false AND revoked_at IS NOT NULL)",
            name="ck_user_permission_grants_revocation",
        ),
        Index(
            "uq_active_user_permission_scope",
            "user_id",
            "permission_key",
            "scope_id",
            unique=True,
            postgresql_where=text("active = true"),
        ),
        Index("ix_user_permission_grants_user_id", "user_id"),
    )


class AuditLogORM(SecurityBase):
    __tablename__ = "audit_logs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_audit_logs_user_created", "user_id", "created_at"),
        Index("ix_audit_logs_event_created", "event_type", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )


class ApiConnectorCredentialORM(SecurityBase):
    __tablename__ = "api_connector_credentials"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    connector_key: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled', 'rotating', 'failed_auth')", name="ck_connector_status"),
        Index("uq_api_connector_credentials_connector_account", "connector_key", "account_id", unique=True),
    )
