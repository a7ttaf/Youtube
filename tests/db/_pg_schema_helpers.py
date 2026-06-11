"""Shared helper for the PostgreSQL migration round-trip test fixtures.

The 9 ``fresh_engine`` / ``_drop_public_schema`` test fixtures all need the
exact same schema-reset behaviour:

  1. Open a short-lived SQLAlchemy engine.
  2. Inside a single transaction, set ``lock_timeout = '30s'`` so a
     contended ``DROP SCHEMA public CASCADE`` fails fast with
     ``LockNotAvailable`` instead of hanging indefinitely.
  3. ``DROP SCHEMA public CASCADE`` + ``CREATE SCHEMA public``.
  4. Dispose the engine via ``try/finally`` so a setup-time error does
     not leak the pool for the rest of the test session.

Extracting the body into :func:`reset_public_schema` keeps the timeout
bound (and any future tuning) to a single line, removes the 9-way
copy-paste, and gives every fixture the same disposal guarantees.
The per-fixture contract block (AGENTS.md) stays in the fixture; the
helper only owns the body.
"""

import sqlalchemy as sa
from sqlalchemy import text


# ============================================================================
# Purpose: Drop and recreate the ``public`` schema for one migration
#          round-trip test, with a transaction-scoped ``lock_timeout``
#          so a contended reset fails fast with ``LockNotAvailable``
#          instead of hanging indefinitely.
# Database/ORM: PostgreSQL `public` schema via raw SQLAlchemy `text()`.
# Standards: SET LOCAL is transaction-scoped (reverts on engine.begin()
#            commit), so existing lock-blocking tests that set their own
#            `statement_timeout='750ms'` on a contender connection are
#            unaffected. The `try/finally` wrapper guarantees
#            `engine.dispose()` runs even when the schema reset raises.
# Blast Radius: Test harness only — no product code, no migration, no
#               `alembic/env.py` change. AGENTS.md statement: None detected.
# Connections:
#   - File: tests/_postgres_helpers.py -> `require_postgres_url()` supplies
#     the disposable test DB URL.
#   - File: tests/db/test_tenant_rls_migration.py -> `_drop_public_schema`
#     is the original private version of this logic; the public helper
#     here is the de-duplicated one used by all 9 fixtures.
#   - File: AGENTS.md -> "Professional Commenting Standard" (this block).
# ============================================================================
def reset_public_schema(postgres_url: str) -> None:
    """Drop and recreate the ``public`` schema with a 30s lock_timeout.

    Uses a short-lived engine so the pool is fully disposed even on the
    schema-reset failure path (a contended reset still raises
    ``LockNotAvailable`` after ``lock_timeout``; the helper does not
    swallow that error).
    """
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
