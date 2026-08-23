# ============================================================================
# Purpose: Begin a composed finance read's request transaction at REPEATABLE
#   READ so every source read that follows shares ONE MVCC snapshot. This is
#   the platform ruling for the PR #197 follow-up (codex): under Postgres's
#   default READ COMMITTED, each statement takes its own snapshot, so a writer
#   committing between the sequential source reads of payment-match,
#   bank-reconciliation, smart-alerts, or gap-explanation could compose totals
#   that never coexisted — including suppressing MONTH_NOT_LOCKED against
#   pre-lock totals when a month close committed mid-read (proven live in
#   tests/api/test_composed_read_snapshot_postgres.py before this fix).
# Database/ORM: No tables of its own; governs the platform-lane Session that
#   every composed-read repository (facts, payments, bank entries, overrides,
#   month close) and the revenue audit sink share within one request.
# Standards: The isolation override rides the exact mechanism
#   auth/principals.py already uses for its SERIALIZABLE principal reads —
#   Session.connection(execution_options=...) on a not-yet-begun transaction —
#   and db/session.py's after_begin hook re-arms the RLS role and trusted
#   tenant context on the new transaction, so tenancy behavior is unchanged.
#   REPEATABLE READ deliberately, not SERIALIZABLE: a read-plus-append-only
#   transaction needs no predicate locks and must never abort concurrent
#   finance writers; RR snapshot reads cannot raise serialization failures.
#   Rejected alternatives from the ruling: (b) version/compare-and-retry
#   columns re-implement MVCC in application code across five tables for a
#   weaker guarantee; (c) documenting READ COMMITTED as accepted would leave
#   the smart-alerts LOCKED-mispairing live. Fail LOUDLY on an active
#   transaction — silently degrading to READ COMMITTED would be an invisible
#   guard loss. Non-Postgres dialects no-op: SQLite lanes share one StaticPool
#   connection (reads are already transaction-consistent) and its dialect has
#   no REPEATABLE READ token. Documented residual: smart-alerts' audit-derived
#   signals read through the TENANT-lane session and stay READ COMMITTED
#   relative to this snapshot — their laning is an authorization boundary
#   (VIEW_AUDIT_LOG + tenant RLS) that must not move to the platform lane for
#   a consistency nicety.
# Blast Radius: Consistency of every composed finance read. The isolation
#   override resets at pool checkin (pinned by the postgres-tier test), so no
#   later transaction on the pooled connection inherits it.
# Connections:
#   - File: backend/ums_smart_revenue/auth/principals.py -> the SERIALIZABLE
#     principal-read precedent this mechanism mirrors.
#   - File: backend/ums_smart_revenue/db/session.py -> the after_begin hook
#     that re-arms role + tenant context on the snapshot transaction.
#   - File: backend/ums_smart_revenue/api/revenue.py -> the composed-read
#     loaders (_load_* — not the route handlers, which never touch the
#     session) that begin this snapshot after their route's permission gates
#     on every composed revenue read, including the channel-month facts
#     listing and the reconciliation-preview (both ride one shared loader;
#     the single-select exemption was disproven: the repository read is
#     guard-then-select) and the dry-run recalculation preview.
#   - File: Docs/12_BACKEND_API_SPEC.md -> the read-consistency contract.
# ============================================================================
"""Composed-read snapshot: one REPEATABLE READ transaction per composed finance read."""

from sqlalchemy.orm import Session


class ComposedReadSnapshotError(RuntimeError):
    """Raised when a composed read cannot start its isolated snapshot transaction."""


def begin_composed_read_snapshot(session: Session) -> None:
    """Begin the composed read's transaction at REPEATABLE READ (Postgres only)."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if session.in_transaction():
        raise ComposedReadSnapshotError(
            "Composed finance reads require a session without an active transaction"
        )
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
