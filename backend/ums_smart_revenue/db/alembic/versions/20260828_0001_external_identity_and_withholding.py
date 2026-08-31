# ============================================================================
# Purpose: Add external identity mapping, user home org scope, and effective-dated
#   account-scoped US withholding display-rate configuration (Docs/23 A6/A7,
#   Docs/24 U3).
# Database/ORM: users.home_org_unit_id; external_identities;
#   us_withholding_rate_configs.
# Standards: Additive nullable home scope; tenant-scoped tables with RLS;
#   account rate bounded 0..0.30 with no default/fallback row seeded.
# Blast Radius: Authorization/estimate schema foundation; runtime wiring is deferred.
# Connections:
#   - File: backend/ums_smart_revenue/auth/external_identities.py
#   - File: backend/ums_smart_revenue/finance/us_withholding_config.py
# ============================================================================
"""External identity, home org scope, and US withholding config.

Revision ID: 20260828_0001
Revises: 20260825_0002
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op

from ums_smart_revenue.db.rls import (
    APP_PLATFORM_ROLE,
    APP_TENANT_ROLE,
    TENANT_CONTEXT_GETTER,
    tenant_rls_policy_name,
)

revision = "20260828_0001"
down_revision = "20260825_0002"
branch_labels = None
depends_on = None

_EXTERNAL_IDENTITIES_TABLE = "external_identities"
_WITHHOLDING_CONFIGS_TABLE = "us_withholding_rate_configs"
_TENANT_TABLE_GRANTS: dict[str, dict[str, str]] = {
    _EXTERNAL_IDENTITIES_TABLE: {
        APP_TENANT_ROLE: "SELECT",
        # Mapping writes are not authorized until the audited admin service
        # ships; granting DML in this schema-only slice would enable account
        # rebinding from every existing platform-lane caller.
        APP_PLATFORM_ROLE: "SELECT",
    },
    _WITHHOLDING_CONFIGS_TABLE: {
        # The audited, permission-gated confirmation service is deferred. Keep
        # the schema readable but not mutable by generic app lanes until that
        # writer can commit the config row and its audit evidence atomically.
        APP_TENANT_ROLE: "SELECT",
        APP_PLATFORM_ROLE: "SELECT",
    },
}


# ============================================================================
# Purpose: Preserve the tenant-scoped functional user-email index across SQLite
#   batch table rebuilds, which cannot reflect expression indexes.
# Database/ORM: users; uq_users_email_lower.
# Standards: Dialect-guarded Alembic DDL with the existing canonical index name.
# Blast Radius: Authorization uniqueness only; no finance or audit changes.
# Connections:
#   - File: tests/db/test_external_identity_withholding_migration.py -> Round-trip guard.
#   - File: backend/ums_smart_revenue/db/security_models.py -> ORM index mirror.
# ============================================================================
def _drop_sqlite_user_email_index_before_batch(bind) -> None:
    if bind.dialect.name != "sqlite":
        return
    # FIX: Remove the expression index before batch reflection so Alembic does
    # not silently omit it with an unsupported-reflection warning.
    op.execute(sa.text("DROP INDEX IF EXISTS uq_users_email_lower"))


def _restore_sqlite_user_email_index(bind) -> None:
    if bind.dialect.name != "sqlite":
        return
    op.create_index(
        "uq_users_email_lower",
        "users",
        ["tenant_id", sa.text("lower(email)")],
        unique=True,
    )


# ============================================================================
# Purpose: Install tenant isolation and an explicit least-privilege grant
#   surface for both new tenant-owned configuration tables.
# Database/ORM: external_identities; us_withholding_rate_configs.
# Standards: PostgreSQL-only ENABLE/FORCE RLS, USING/WITH CHECK policy, and
#   enumerated grants with no implicit public access.
# Blast Radius: Authorization, identity mapping, and finance estimate config.
# Connections:
#   - File: backend/ums_smart_revenue/db/rls.py -> Canonical roles and policy name.
#   - File: tests/db/test_external_identity_withholding_migration_postgres.py -> Live proof.
# ============================================================================
def _configure_tenant_isolation(bind) -> None:
    if bind.dialect.name != "postgresql":
        return

    for table, role_grants in _TENANT_TABLE_GRANTS.items():
        qualified_table = f'public."{table}"'
        policy = tenant_rls_policy_name(table)
        bind.execute(sa.text(f"ALTER TABLE {qualified_table} ENABLE ROW LEVEL SECURITY"))
        bind.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {qualified_table}"))
        bind.execute(
            sa.text(
                f"CREATE POLICY {policy} ON {qualified_table} "
                f"USING (tenant_id = {TENANT_CONTEXT_GETTER}()) "
                f"WITH CHECK (tenant_id = {TENANT_CONTEXT_GETTER}())"
            )
        )
        bind.execute(sa.text(f"ALTER TABLE {qualified_table} FORCE ROW LEVEL SECURITY"))
        bind.execute(sa.text(f"REVOKE ALL ON {qualified_table} FROM PUBLIC"))
        for role, privileges in role_grants.items():
            bind.execute(sa.text(f'REVOKE ALL ON {qualified_table} FROM "{role}"'))
            bind.execute(sa.text(f'GRANT {privileges} ON {qualified_table} TO "{role}"'))


# ============================================================================
# Purpose: Add nullable home-org scope and the two explicitly tenant-owned,
#   RLS-protected identity/account-withholding foundation tables.
# Database/ORM: users, org_units, external_identities, withholding configs.
# Standards: Additive Alembic DDL, composite tenant FKs, append-only revisions.
# Blast Radius: Authorization identity/home scope and finance estimate config.
# Connections:
#   - File: backend/ums_smart_revenue/db/security_models.py -> ORM mirror.
#   - File: tests/db/test_external_identity_withholding_migration_postgres.py -> Live proof.
# ============================================================================
def upgrade() -> None:
    """Create the tenant-isolated identity and withholding foundation."""
    bind = op.get_bind()
    _drop_sqlite_user_email_index_before_batch(bind)
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("home_org_unit_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_users_tenant_home_org_unit",
            "org_units",
            ["tenant_id", "home_org_unit_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_users_home_org_unit_id",
            ["tenant_id", "home_org_unit_id"],
        )
    _restore_sqlite_user_email_index(bind)

    op.create_table(
        "external_identities",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_subject", sa.Text(), nullable=False),
        sa.Column("normalized_email", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_external_identities_tenant_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_subject",
            name="uq_external_identities_provider_subject",
        ),
        # FIX (review P2): mirror the ORM's fail-closed claim constraints so a
        # blank or whitespace-bearing provider/subject/email row cannot be
        # stored on either dialect; A7 malformed identities must not resolve.
        *[
            sa.CheckConstraint(
                f"length({column}) > 0 AND {column} = trim({column})",
                name=f"ck_external_identities_{column}_nonblank",
            )
            for column in ("provider", "provider_subject", "normalized_email")
        ],
        sa.CheckConstraint(
            r"provider !~ E'[\t\n\r\f\v]' "
            r"AND provider_subject !~ E'[\t\n\r\f\v]' "
            r"AND normalized_email !~ E'[\t\n\r\f\v]'",
            name="ck_external_identities_claims_ascii_whitespace_pg",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "instr(provider, char(9)) = 0 AND instr(provider, char(10)) = 0 "
            "AND instr(provider, char(11)) = 0 AND instr(provider, char(12)) = 0 "
            "AND instr(provider, char(13)) = 0 "
            "AND instr(provider_subject, char(9)) = 0 "
            "AND instr(provider_subject, char(10)) = 0 "
            "AND instr(provider_subject, char(11)) = 0 "
            "AND instr(provider_subject, char(12)) = 0 "
            "AND instr(provider_subject, char(13)) = 0 "
            "AND instr(normalized_email, char(9)) = 0 "
            "AND instr(normalized_email, char(10)) = 0 "
            "AND instr(normalized_email, char(11)) = 0 "
            "AND instr(normalized_email, char(12)) = 0 "
            "AND instr(normalized_email, char(13)) = 0",
            name="ck_external_identities_claims_ascii_whitespace_sqlite",
        ).ddl_if(dialect="sqlite"),
    )
    op.create_index(
        "ix_external_identities_tenant_user_id",
        "external_identities",
        ["tenant_id", "user_id"],
    )
    op.create_index(
        "uq_external_identities_tenant_email_lower",
        "external_identities",
        ["tenant_id", sa.text("lower(normalized_email)")],
        unique=True,
    )

    op.create_table(
        "us_withholding_rate_configs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source_account_id", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("account_type", sa.Text(), nullable=False),
        sa.Column("confirmed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "account_type IN ('business', 'individual')",
            name="ck_us_withholding_rate_configs_account_type",
        ),
        sa.CheckConstraint(
            "length(source_account_id) > 0 "
            "AND source_account_id = trim(source_account_id) "
            "AND source_account_id NOT LIKE '%/%' "
            "AND source_account_id NOT LIKE '%?%' "
            "AND source_account_id NOT LIKE '%#%' "
            "AND replace(source_account_id, '%', '') = source_account_id",
            name="ck_us_withholding_rate_configs_source_account_id",
        ),
        sa.CheckConstraint(
            r"source_account_id !~ E'[\t\n\r\f\v]'",
            name="ck_us_withholding_rate_configs_account_ascii_whitespace_pg",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "instr(source_account_id, char(9)) = 0 "
            "AND instr(source_account_id, char(10)) = 0 "
            "AND instr(source_account_id, char(11)) = 0 "
            "AND instr(source_account_id, char(12)) = 0 "
            "AND instr(source_account_id, char(13)) = 0",
            name="ck_us_withholding_rate_configs_account_ascii_whitespace_sqlite",
        ).ddl_if(dialect="sqlite"),
        sa.CheckConstraint(
            "rate >= 0 AND rate <= 0.30",
            name="ck_us_withholding_rate_configs_rate",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_us_withholding_rate_configs_revision_positive",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "confirmed_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_us_withholding_rate_configs_confirmed_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "source_account_id",
            "effective_from",
            "revision",
            name="uq_us_withholding_rate_configs_account_effective_revision",
        ),
    )
    op.create_index(
        "ix_us_withholding_rate_configs_tenant_effective",
        "us_withholding_rate_configs",
        [
            "tenant_id",
            "source_account_id",
            sa.text("effective_from DESC"),
            sa.text("revision DESC"),
        ],
    )
    _configure_tenant_isolation(bind)


# ============================================================================
# Purpose: Remove the PR #228 schema additions and restore the pre-revision
#   users table without losing its functional email uniqueness index.
# Database/ORM: users, external_identities, us_withholding_rate_configs.
# Standards: Dependency-ordered DDL owned by this revision; rollback stops at
#   the irreversible 20260825_0002 security floor.
# Blast Radius: Drops all identity mappings/rate history created after upgrade.
# Connections:
#   - File: tests/db/test_external_identity_withholding_migration.py -> Round trip.
#   - File: Docs/24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md -> Rollback warning.
# ============================================================================
def downgrade() -> None:
    """Drop only this revision and stop at irreversible 20260825_0002."""
    bind = op.get_bind()
    op.drop_index(
        "ix_us_withholding_rate_configs_tenant_effective",
        table_name="us_withholding_rate_configs",
    )
    op.drop_table("us_withholding_rate_configs")
    op.drop_index(
        "uq_external_identities_tenant_email_lower",
        table_name="external_identities",
    )
    op.drop_index(
        "ix_external_identities_tenant_user_id",
        table_name="external_identities",
    )
    op.drop_table("external_identities")
    _drop_sqlite_user_email_index_before_batch(bind)
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_home_org_unit_id")
        batch.drop_constraint("fk_users_tenant_home_org_unit", type_="foreignkey")
        batch.drop_column("home_org_unit_id")
    _restore_sqlite_user_email_index(bind)
