# ============================================================================
# Purpose: Check the database dependency for the unauthenticated readiness
#   probe without mixing SQLAlchemy work into the FastAPI route handler.
# Database/ORM: Executes ``SELECT 1`` through the configured SessionFactory;
#   no application table, row, or schema is changed.
# Standards: Typed readiness failure with a safe public message; connection
#   errors are never returned to an unauthenticated probe.
# Blast Radius: Operational health reporting only. No authorization, finance,
#   audit, tenancy, or export behavior.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> /readyz route boundary.
#   - File: backend/ums_smart_revenue/db/session.py -> session factory.
#   - File: docker-compose.yml -> container readiness healthcheck.
# ============================================================================
"""Layer-appropriate dependency checks for process readiness."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ums_smart_revenue.db.session import SessionFactory


class ReadinessUnavailableError(RuntimeError):
    """Raised when a required application dependency cannot be reached."""


def check_database_readiness(session_factory: SessionFactory | None) -> None:
    """Verify that the configured database can accept a trivial read.

    ``None`` is an intentional not-ready state: an app created without a
    database is useful for route/unit tests and liveness, but it cannot serve
    operational API traffic backed by PostgreSQL/SQLite.
    """
    if session_factory is None:
        raise ReadinessUnavailableError("database is not configured")
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise ReadinessUnavailableError("database is unavailable") from exc
    except Exception as exc:
        # Drivers can raise non-SQLAlchemy connection errors (for example an
        # OS-level socket failure). Preserve the same safe readiness contract.
        raise ReadinessUnavailableError("database is unavailable") from exc
