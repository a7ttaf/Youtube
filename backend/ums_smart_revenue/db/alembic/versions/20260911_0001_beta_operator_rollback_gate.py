# ============================================================================
# Purpose: Rollback gate — refuse to leave this revision while an ACTIVE
#   beta_operator role assignment exists. ``beta_operator`` was introduced by
#   the security catalog revisions; a rolled-back binary's RoleKey cannot
#   parse it and the principal loader would deny those operators access.
# Database/ORM: user_role_assignments — SHARE ROW EXCLUSIVE lock plus an
#   ACTIVE-count read on PostgreSQL (serializes against concurrent writers);
#   a plain count on other dialects. No catalog or tenant rows are written.
# Standards: Fail closed — LiveBetaOperatorAssignmentError on live rows or an
#   unverifiable row-security read. Published revisions are never rewritten:
#   this gate lives in a NEW revision per repository history rules, replacing
#   the in-place edit of 20260825_0001 that PR review rejected.
# Blast Radius: Downgrade path only; briefly holds writes to
#   user_role_assignments until the migration transaction ends.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260825_0002_beta_operator_authorization_repair.py -> parent revision
#     (its own downgrade refuses unconditionally).
#   - File: backend/ums_smart_revenue/auth/principals.py -> active-only loader.
#   - File: tests/db/test_security_role_permission_seed_migration.py -> guards.
# ============================================================================
"""Gate rollbacks on live beta_operator assignments.

Revision ID: 20260911_0001
Revises: 20260825_0002
Create Date: 2026-09-11

Why this revision exists
------------------------
``beta_operator`` is a role key the parent revisions' ``RoleKey`` enum cannot
parse. Rolling the schema back past the security catalog while an ACTIVE
``beta_operator`` row remains in ``user_role_assignments`` leaves the
principal loader raising ``PrincipalDataValidationError`` and denying those
operators access. A conditional refusal must therefore run before the schema
steps back.

Published revision files are immutable in this repository, so the guard
cannot live inside ``20260825_0001.downgrade`` — it sits here as the first
step of any rollback chain that would reach the unguarded historical
downgrades. ``20260825_0002.downgrade`` already refuses unconditionally
(irreversible repair), so this gate additionally gives operators a precise,
actionable refusal for the assignment-rooted case at the head boundary.
"""

import sqlalchemy as sa
from alembic import op

from ums_smart_revenue.db.session import apply_statement_bounds

revision = "20260911_0001"
down_revision = "20260825_0002"
branch_labels = None
depends_on = None

_USER_ROLE_ASSIGNMENTS = sa.table(
    "user_role_assignments",
    sa.column("role_key", sa.Text()),
    sa.column("active", sa.Boolean()),
)


class LiveBetaOperatorAssignmentError(RuntimeError):
    """Refuse a downgrade that would orphan a live ``beta_operator`` assignment."""


class RollbackGateVerificationError(RuntimeError):
    """The guard could not inspect ``user_role_assignments`` — real DB error."""


# ============================================================================
# Purpose: Non-destructive forward step — the revision exists purely to gate
#   rollback, so upgrade only stamps the version row.
# Database/ORM: None — touches the bind so the step is an intentional no-op
#   rather than an empty body.
# Standards: Idempotent by construction; no data changes.
# Blast Radius: None.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260825_0002_beta_operator_authorization_repair.py -> parent.
# ============================================================================
def upgrade() -> None:
    """No-op forward migration; this revision only gates rollback."""
    _ = op.get_bind()


# ============================================================================
# Purpose: Fail-closed rollback gate — refuse while an ACTIVE beta_operator
#   assignment remains in user_role_assignments, because a rolled-back
#   binary cannot parse the role and would deny those operators access.
# Database/ORM: user_role_assignments — SHARE ROW EXCLUSIVE lock plus an
#   ACTIVE-count read on PostgreSQL; a plain count on other dialects.
# Standards: Fail closed on live rows AND on an unverifiable row-security
#   read; emits no writes.
# Blast Radius: Downgrade path only; briefly holds writes to
#   user_role_assignments until the migration transaction ends.
# Connections:
#   - File: backend/ums_smart_revenue/auth/principals.py -> active-only loader.
#   - File: tests/db/test_security_role_permission_seed_migration.py -> guard.
# ============================================================================
def downgrade() -> None:
    """Refuse while an active ``beta_operator`` assignment exists.

    Raises:
        LiveBetaOperatorAssignmentError: when an ACTIVE ``beta_operator``
            row remains in ``user_role_assignments``, or when the login lacks
            the privilege to prove none remain (row-security denial, missing
            lock grant). The operator revokes/migrates the assignments (or
            re-runs as a superuser/BYPASSRLS role) and retries the downgrade.
        RollbackGateVerificationError: when the inspection itself fails for a
            reason other than privilege — lock timeout, deadlock, connection
            loss, missing table. The message carries the real driver error
            class so operators investigate the database, not their grants.
    """
    bind = op.get_bind()
    try:
        if bind.dialect.name == "postgresql":
            # Bound every blocking point first: a conflicting write lock must
            # fail with an actionable refusal, not hang the migration.
            apply_statement_bounds(
                bind, lock_timeout="10s", statement_timeout="10s"
            )
            # An operator-visible login may be row-security-bounded; disable
            # RLS for this read so the guard can prove the absence of live
            # rows rather than trusting a filtered count.
            bind.execute(sa.text("SET LOCAL row_security = off"))
            # Serialise the check against concurrent writers for the rest of
            # this transaction — a snapshot-only count would otherwise let a
            # role assignment slip in behind the downgrade.
            bind.execute(
                sa.text(
                    "LOCK TABLE user_role_assignments IN SHARE ROW EXCLUSIVE MODE"
                )
            )
        live = bind.execute(
            sa.select(sa.func.count())
            .select_from(_USER_ROLE_ASSIGNMENTS)
            .where(
                _USER_ROLE_ASSIGNMENTS.c.role_key == "beta_operator",
                _USER_ROLE_ASSIGNMENTS.c.active.is_(True),
            )
        ).scalar_one()
    except sa.exc.SQLAlchemyError as exc:
        # SQLAlchemy 2.0.52 has no InsufficientPrivilegeError wrapper — the
        # driver's SQLSTATE arrives via exc.orig (pgcode/sqlstate). 42501
        # covers RLS-forced denials (row_security=off makes a NOBYPASSRLS
        # owner's read fail rather than bypass on the FORCE-RLS table),
        # missing LOCK grants, and refused SETs — all get privileged-rerun
        # guidance. Anything else is a real database failure and must surface
        # its own cause.
        pgcode = getattr(getattr(exc, "orig", None), "sqlstate", None) or getattr(
            getattr(exc, "orig", None), "pgcode", None
        )
        if pgcode == "42501":
            raise LiveBetaOperatorAssignmentError(
                "downgrade could not verify active beta_operator assignments "
                "(a row-security-bounded login cannot read across tenants); "
                "re-run as a superuser/BYPASSRLS role after revoking or "
                "migrating beta_operator assignments"
            ) from exc
        raise RollbackGateVerificationError(
            "downgrade could not inspect user_role_assignments "
            f"({type(exc).__name__}: {exc}); resolve the underlying database "
            "error or lock contention, then retry"
        ) from exc
    if live:
        raise LiveBetaOperatorAssignmentError(
            f"downgrade would strand {live} active beta_operator assignment(s): "
            "the parent revisions' RoleKey cannot parse them and the principal "
            "loader would deny those operators access; revoke or migrate the "
            "assignments, then re-run the downgrade"
        )
