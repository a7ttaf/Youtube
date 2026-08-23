"""Helper for tests that require disposable PostgreSQL.

Tests that need PostgreSQL call :func:`require_postgres_url` from a fixture
(see ``test_google_revenue_source_migration_postgres.py``) rather than at
import time. It raises ``RuntimeError`` — never a skip, so the AST policy
gate's no-skip rule still holds — when ``UMS_TEST_DATABASE_URL`` is unset, so a
missing optional dependency errors only that suite instead of aborting pytest
collection for the entire repository.
"""

import os


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
    # FIX: treat blank/whitespace-only values as missing. `if not url` let a
    # value like "   " through, which then fails later with an opaque DB
    # connection error instead of this fail-fast setup contract.
    url = os.environ.get("UMS_TEST_DATABASE_URL")
    if url is None or not url.strip():
        raise RuntimeError(
            "UMS_TEST_DATABASE_URL required for PostgreSQL migration round-trip tests. "
            "Spin up disposable Postgres: "
            "`docker run --rm -d --name ums-mig-pg -p 55432:5432 "
            "-e POSTGRES_PASSWORD=ums -e POSTGRES_DB=test_ums postgres:18-alpine`, "
            "then set UMS_TEST_DATABASE_URL. PowerShell: "
            "`$env:UMS_TEST_DATABASE_URL = "
            "'postgresql+psycopg://postgres:ums@127.0.0.1:55432/test_ums'`. "
            "POSIX shell: "
            "`export UMS_TEST_DATABASE_URL="
            "postgresql+psycopg://postgres:ums@127.0.0.1:55432/test_ums`. "
            "The database name must match test_*/*_test (fixtures refuse destructive "
            "resets on any other name), and a cluster must only ever have migrations "
            "run against one database (roles are cluster-wide). "
            "SQLite is not a valid substitute for this test."
        )
    return url.strip()
