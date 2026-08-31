"""Shared helper for the PostgreSQL migration round-trip test fixtures.

The migration ``fresh_engine`` / ``_drop_public_schema`` fixtures all need the
exact same schema-reset behaviour:

  1. Open a short-lived SQLAlchemy engine.
  2. Inside a single transaction, set ``lock_timeout = '30s'`` so a
     contended ``DROP SCHEMA public CASCADE`` fails fast with
     ``LockNotAvailable`` instead of hanging indefinitely.
  3. ``DROP SCHEMA public CASCADE`` + ``CREATE SCHEMA public``.
  4. Dispose the engine via ``try/finally`` so a setup-time error does
     not leak the pool for the rest of the test session.

Extracting the body into :func:`reset_public_schema` keeps the timeout
bound (and any future tuning) to a single line, removes duplicated
copy-paste, and gives every fixture the same disposal guarantees.
The per-fixture contract block (AGENTS.md) stays in the fixture; the
helper only owns the body.
"""

import sqlalchemy as sa
from sqlalchemy import text

_DATABASE_SELECTION_QUERY_KEYS = frozenset({"database", "dbname", "service", "servicefile"})


# ============================================================================
# Purpose: Drop and recreate the ``public`` schema for one migration
#          round-trip test, with a transaction-scoped ``lock_timeout``
#          so a contended reset fails fast with ``LockNotAvailable``
#          instead of hanging indefinitely.
# Database/ORM: PostgreSQL `public` schema via raw SQLAlchemy `text()`.
# Standards: SET LOCAL is transaction-scoped (reverts on engine.begin()
#            commit), so existing lock-blocking tests that set their own
#            `statement_timeout='750ms'` on a contender connection are
#            unaffected. Database-name validation runs before connection. The
#            `try/finally` wrapper guarantees
#            `engine.dispose()` runs even when the schema reset raises.
# Blast Radius: Test harness only. Destructive reset is refused unless the
#               PostgreSQL database name is explicitly test-shaped.
# Connections:
#   - File: tests/db/_postgres_helpers.py -> `require_postgres_url()` supplies
#     the disposable test DB URL.
#   - File: tests/db/test_tenant_rls_migration.py -> `_drop_public_schema`
#     is the original private version of this logic; the public helper
#     here is the de-duplicated boundary used by migration fixtures.
#   - File: AGENTS.md -> "Professional Commenting Standard" (this block).
# ============================================================================
def _require_disposable_postgres_database(postgres_url: str) -> None:
    """Reject a destructive schema reset unless the URL names a test database."""
    try:
        parsed = sa.engine.make_url(postgres_url)
    except (sa.exc.ArgumentError, ValueError) as exc:
        raise RuntimeError(
            "Refusing destructive public-schema reset: invalid PostgreSQL URL."
        ) from exc
    query_keys = {key.casefold() for key in parsed.query}
    if query_keys & _DATABASE_SELECTION_QUERY_KEYS:
        raise RuntimeError(
            "Refusing destructive public-schema reset: database-selection query "
            "parameters are not allowed."
        )
    database = (parsed.database or "").strip().casefold()
    if not parsed.drivername.startswith("postgresql") or not (
        database.startswith("test_") or database.endswith("_test")
    ):
        raise RuntimeError(
            "Refusing destructive public-schema reset: PostgreSQL database name "
            "must start with 'test_' or end with '_test'."
        )


def reset_public_schema(postgres_url: str) -> None:
    """Drop and recreate the ``public`` schema with a 30s lock_timeout.

    Uses a short-lived engine so the pool is fully disposed even on the
    schema-reset failure path (a contended reset still raises
    ``LockNotAvailable`` after ``lock_timeout``; the helper does not
    swallow that error).
    """
    # FIX: The shared reset previously trusted any non-empty environment URL;
    # path-only validation also let libpq's dbname/service query parameters
    # redirect a test-shaped URL to a non-test database.
    _require_disposable_postgres_database(postgres_url)
    engine = sa.create_engine(postgres_url)
    try:
        with engine.begin() as conn:
            # Fail fast (don't hang) if a stray connection holds a
            # public-schema lock: without lock_timeout, DROP SCHEMA waits
            # indefinitely (e.g. an orphan `idle in transaction` connection
            # left by a prior killed/hung run on a shared/reused cluster).
            conn.execute(text("SET LOCAL lock_timeout = '30s'"))
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
