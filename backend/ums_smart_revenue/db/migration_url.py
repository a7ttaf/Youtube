"""Pure, importable resolver for the Alembic migration target database URL.

Extracted from ``alembic/env.py`` so the precedence logic can be unit-tested
without an Alembic runtime context (``env.py`` runs migrations at import time and
cannot be imported directly).
"""
from __future__ import annotations

from collections.abc import Mapping

from alembic.config import Config


# ============================================================================
# Purpose: Resolve which database Alembic migrations target, honoring BOTH the
#          production contract (UMS_DATABASE_URL overrides the alembic.ini
#          placeholder) and programmatic callers (tests/embedded code that
#          inject an explicit sqlalchemy.url must NOT be silently retargeted by
#          an ambient UMS_DATABASE_URL).
# Database/ORM: None — returns a connection URL string; touches no models.
# Standards: Pure function (no env mutation, no I/O); typed boundaries; raises
#            RuntimeError (never silently returns "") when no URL is resolvable.
# Blast Radius: Migration entry point for BOTH prod deploys and the test suite —
#               highest care. No schema/data change. No graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/env.py -> get_database_url()
#     delegates here for both offline and online migration runs.
#   - File: Docs/superpowers/specs/2026-06-11-alembic-env-url-precedence-design.md
#     -> the approved spec (Approach A: config_file_name-gated precedence).
# ============================================================================
def resolve_database_url(config: Config, environ: Mapping[str, str]) -> str:
    """Return the database URL Alembic should migrate against.

    ``config.config_file_name`` is the discriminator: it is ``None`` only when a
    ``Config`` was built in code without an ini path (every migration round-trip
    fixture, and any embedded caller). Production runs Alembic from
    ``alembic.ini``, so ``config_file_name`` is set there.
    """
    configured = config.get_main_option("sqlalchemy.url")
    # Programmatic config (tests / embedded callers): the explicitly injected
    # sqlalchemy.url wins so an ambient UMS_DATABASE_URL cannot silently
    # retarget migrations to the wrong (and possibly data-bearing) database.
    if config.config_file_name is None and configured:
        return configured
    # ini-based config (production): UMS_DATABASE_URL overrides the alembic.ini
    # placeholder, preserving the existing production contract.
    url = environ.get("UMS_DATABASE_URL") or configured
    if not url:
        raise RuntimeError(
            "Database URL not configured. Set UMS_DATABASE_URL or sqlalchemy.url in alembic.ini."
        )
    return url
