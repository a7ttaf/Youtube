"""SQLAlchemy ORM for the tenancy domain.

`tenants` holds the registry of holdings/companies onboarded onto the
platform; UMS is seeded as tenant #1 in the foundation migration. Every
operational table elsewhere in the schema will gain a ``tenant_id`` FK
to this table in a follow-up migration (S2.4) along with Row-Level
Security policies — but those changes belong to a different PR.

`platform_admins` is the **separate** identity table for platform-level
administrators (tenant CRUD, cross-tenant reporting). Keeping it outside
the per-tenant ``users`` table is a deliberate isolation guarantee: a
platform admin cannot be granted tenant-bound resources because the FK
shapes simply do not exist.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class TenantBase(DeclarativeBase):
    """Declarative base for tenant + platform-admin SQL tables."""

    pass


# Status vocabularies are tiny enums (CHECK constraints, not separate tables)
# so the migration test on SQLite stays trivial.
TENANT_STATUS_VALUES = ("ACTIVE", "SUSPENDED", "ARCHIVED")
PLATFORM_ADMIN_STATUS_VALUES = ("ACTIVE", "SUSPENDED", "RETIRED")


class TenantORM(TenantBase):
    """Registry row for a tenant (holding) onboarded onto the platform."""

    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    primary_currency: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'USD'")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ACTIVE'")
    )
    onboarding_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
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

    __table_args__ = (
        CheckConstraint("slug = lower(slug)", name="ck_tenants_slug_lower"),
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')",
            name="ck_tenants_status",
        ),
        CheckConstraint(
            "length(primary_currency) = 3 AND primary_currency = upper(primary_currency)",
            name="ck_tenants_primary_currency_iso4217",
        ),
        Index("uq_tenants_slug", "slug", unique=True),
    )


class PlatformAdminORM(TenantBase):
    """Platform-level administrator (manages tenants themselves)."""

    __tablename__ = "platform_admins"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ACTIVE'")
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

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'RETIRED')",
            name="ck_platform_admins_status",
        ),
        Index("uq_platform_admins_email_lower", func.lower(email), unique=True),
    )
