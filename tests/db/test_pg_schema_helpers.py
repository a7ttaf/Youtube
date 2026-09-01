# ============================================================================
# Purpose: Prove destructive PostgreSQL schema resets are restricted to
#   explicitly test-shaped database names before any connection is opened.
# Database/ORM: None; URL parsing and mocked connection boundary only.
# Standards: Fail-closed malformed/production URL coverage without secrets in
#   errors or a live database dependency.
# Blast Radius: Test-only validation of the shared destructive-reset helper.
# Connections:
#   - File: tests/db/_pg_schema_helpers.py -> guarded reset boundary.
#   - File: tests/db/_postgres_helpers.py -> disposable database URL contract.
# ============================================================================
"""Safety tests for the shared PostgreSQL schema-reset helper."""

from __future__ import annotations

import pytest
from tests.db import _pg_schema_helpers as schema_helpers


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://ums:secret@localhost/test_ums",
        "postgresql+psycopg://ums:secret@localhost/ums_test",
        "postgresql://ums:secret@localhost/TEST_REVENUE",
    ],
)
def test_disposable_database_names_are_accepted(url: str) -> None:
    """The documented prefix/suffix forms pass before the connection boundary."""
    schema_helpers._require_disposable_postgres_database(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://ums:do-not-leak@localhost/ums",
        "postgresql+psycopg://ums:do-not-leak@localhost/postgres",
        "postgresql+psycopg://ums:do-not-leak@localhost",
        "postgresql+psycopg://ums:do-not-leak@localhost/test_safe?dbname=production",
        "postgresql+psycopg://ums:do-not-leak@localhost/test_safe?service=production",
        "postgresql+psycopg://ums:do-not-leak@localhost/test_safe?servicefile=prod.conf",
        "sqlite:///test_ums.db",
        "not a database url",
    ],
)
def test_non_test_database_is_rejected_before_connection(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    """Unsafe and malformed URLs fail before create_engine can touch a server."""
    connected = False

    def unexpected_create_engine(*args: object, **kwargs: object) -> None:
        """Canary that records any create_engine call reaching this boundary."""
        nonlocal connected
        connected = True

    monkeypatch.setattr(schema_helpers.sa, "create_engine", unexpected_create_engine)
    with pytest.raises(RuntimeError, match="Refusing destructive public-schema reset") as exc_info:
        schema_helpers.reset_public_schema(url)
    assert connected is False
    assert "do-not-leak" not in str(exc_info.value)
