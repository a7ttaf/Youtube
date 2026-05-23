"""Helper for tests that require disposable PostgreSQL.

Tests that import this module MUST be runnable only when
UMS_TEST_DATABASE_URL is set. The module raises at import time if the
variable is missing — matching the AST policy gate's no-skip rule.
"""

import os
from typing import Final


def require_postgres_url() -> str:
    url = os.environ.get("UMS_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "UMS_TEST_DATABASE_URL required for PostgreSQL migration round-trip tests. "
            "Spin up disposable Postgres: "
            "`docker run --rm -d --name ums-mig-pg -p 55432:5432 "
            "-e POSTGRES_PASSWORD=ums postgres:18-alpine`, then "
            "`$env:UMS_TEST_DATABASE_URL = "
            "'postgresql+psycopg://postgres:ums@localhost:55432/postgres'`. "
            "SQLite is not a valid substitute for this test."
        )
    return url


POSTGRES_URL: Final[str] = require_postgres_url()
