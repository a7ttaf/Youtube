"""Helper for tests that require disposable PostgreSQL.

Tests that need PostgreSQL call :func:`require_postgres_url` from a fixture
(see ``test_google_revenue_source_migration_postgres.py``) rather than at
import time. It raises ``RuntimeError`` — never a skip, so the AST policy
gate's no-skip rule still holds — when ``UMS_TEST_DATABASE_URL`` is unset, so a
missing optional dependency errors only that suite instead of aborting pytest
collection for the entire repository.
"""

import os

from scripts.ci_lane_runtime import assert_database_access_allowed

# Explicit CI contract: importing this helper is database-lane-only.
UMS_CI_DATABASE_REQUIRED = True


# ============================================================================
# Purpose: Resolve a non-empty PostgreSQL SQLAlchemy URL for the migration
#          round-trip tests, failing fast with actionable setup guidance when
#          UMS_TEST_DATABASE_URL is absent or blank.
# Database/ORM: None (returns a connection URL string; touches no models).
# Standards: No inputs; returns the trimmed URL str. Raises RuntimeError (never
#            a test skip) so the no-skip AST policy gate stays honoured. SQLite
#            is not a valid substitute for these PostgreSQL-native migrations.
# Blast Radius: Test harness only. No graph projection impact detected.
# Connections:
#   - File: tests/db/test_google_revenue_source_migration_postgres.py -> caller.
# ============================================================================
def require_postgres_url() -> str:
    # FIX: Required CI grants this capability only while executing an item in
    # the exact database manifest; fixture indirection cannot move it to fast.
    assert_database_access_allowed()
    # FIX: treat blank/whitespace-only values as missing. `if not url` let a
    # value like "   " through, which then fails later with an opaque DB
    # connection error instead of this fail-fast setup contract.
    url = os.environ.get("UMS_TEST_DATABASE_URL")
    if url is None or not url.strip():
        # FIX: State the database-name rule exactly as the fixtures apply it
        # (startswith("test_") or endswith("_test")) instead of the ambiguous
        # glob "test_*/*_test", and drop the claim that fixtures refuse resets
        # on other names — only one fixture enforces that guard today.
        raise RuntimeError(
            "UMS_TEST_DATABASE_URL required for PostgreSQL migration round-trip tests. "
            "Spin up a disposable PostgreSQL 18 cluster, create a dedicated test "
            "database, and set UMS_TEST_DATABASE_URL to its postgresql+psycopg URL. "
            "The database name must start with `test_` or end with `_test` "
            "(schema-reset fixtures destructively recreate the public schema), "
            "and a cluster must only ever have migrations run against one "
            "database (roles are cluster-wide). "
            "SQLite is not a valid substitute for this test."
        )
    return url.strip()
