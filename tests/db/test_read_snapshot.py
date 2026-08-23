# ============================================================================
# Purpose: Dialect-contract pins for db.read_snapshot.begin_composed_read_snapshot
#   — the non-Postgres branch must be a transparent no-op (SQLite serializes
#   every lane through one shared StaticPool connection, so its reads are
#   already snapshot-consistent within a transaction and the REPEATABLE READ
#   token does not even exist for its dialect).
# Database/ORM: Real SQLite sessions only; the Postgres-side behavior (begin
#   REPEATABLE READ, reject active transactions, reset at checkin) is pinned
#   with a real server in tests/api/test_composed_read_snapshot_postgres.py.
# Standards: No mocks — real Session objects over a throwaway in-memory
#   engine; assertions observe transaction state, not implementation calls.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the helper under
#     test.
#   - File: tests/api/test_composed_read_snapshot_postgres.py -> the
#     Postgres-tier proof this SQLite tier cannot provide.
# ============================================================================
"""SQLite-tier contract pins for the composed-read snapshot helper."""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from ums_smart_revenue.db.read_snapshot import begin_composed_read_snapshot


def test_non_postgres_session_is_a_no_op() -> None:
    """On SQLite the helper neither begins a transaction nor raises."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        begin_composed_read_snapshot(session)
        assert session.in_transaction() is False


def test_non_postgres_active_transaction_is_tolerated() -> None:
    """The dialect gate precedes the transaction guard: SQLite lanes share
    one session, so an already-begun transaction must not be rejected there."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        assert session.in_transaction() is True
        begin_composed_read_snapshot(session)
        assert session.in_transaction() is True
