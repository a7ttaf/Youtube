"""Resolver for the Alembic migration target database URL.

Extracted from ``alembic/env.py`` so the precedence logic can be unit-tested
without an Alembic runtime context (``env.py`` runs migrations at import time and
cannot be imported directly).
"""
from __future__ import annotations

import configparser
from collections.abc import Mapping

from alembic.config import Config


# ============================================================================
# Purpose: Recover the ``sqlalchemy.url`` declared ON DISK in the alembic ini
#          file (or None). ``Config.get_main_option`` cannot distinguish a value
#          loaded from the ini from one injected later via ``set_main_option``
#          (both live in the same in-memory parser), so the on-disk value is
#          re-read to detect deliberate in-code injection.
# Database/ORM: None — reads a config file, touches no models.
# Standards: ``interpolation=None`` so ``%(...)s`` logging formatters elsewhere
#            in the ini do not raise; missing/unreadable file returns None.
# Blast Radius: Migration URL resolution only. No graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/db/migration_url.py -> resolve_database_url
#     compares this against the effective configured url.
# ============================================================================
def _ini_declared_url(config: Config) -> str | None:
    path = config.config_file_name
    if not path:
        return None
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path, encoding="utf-8"):
        return None
    section = config.config_ini_section
    if not parser.has_option(section, "sqlalchemy.url"):
        return None
    return parser.get(section, "sqlalchemy.url") or None


# ============================================================================
# Purpose: Resolve which database Alembic migrations target, honoring BOTH the
#          production contract (UMS_DATABASE_URL overrides the alembic.ini
#          placeholder) and programmatic callers (tests/embedded code that
#          inject an explicit sqlalchemy.url must NOT be silently retargeted by
#          an ambient UMS_DATABASE_URL).
# Database/ORM: None — returns a connection URL string; touches no models.
# Standards: Deterministic resolver (re-reads the ini to classify the configured
#            url; never mutates env); typed boundaries; raises RuntimeError
#            (never silently returns "") when no URL is resolvable.
# Blast Radius: Migration entry point for BOTH prod deploys and the test suite —
#               highest care. No schema/data change. No graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/env.py -> get_database_url()
#     delegates here for both offline and online migration runs.
#   - File: Docs/superpowers/specs/2026-06-11-alembic-env-url-precedence-design.md
#     -> the approved spec (closing the wrong-DB footgun).
# ============================================================================
def resolve_database_url(config: Config, environ: Mapping[str, str]) -> str:
    """Return the database URL Alembic should migrate against.

    A configured ``sqlalchemy.url`` that differs from the ini file's own on-disk
    declaration was injected in code (every migration round-trip fixture, and any
    embedded caller) — it is honored over an ambient ``UMS_DATABASE_URL`` so a
    stale env var cannot silently retarget migrations to the wrong (possibly
    data-bearing) database. This holds whether the caller built ``Config()`` with
    no ini (``config_file_name`` is None ⇒ no on-disk url) or ``Config(ini)`` and
    then overrode the url. When the configured url equals the ini declaration
    (production: the alembic.ini placeholder, no in-code override),
    ``UMS_DATABASE_URL`` wins, preserving the existing production contract.
    """
    configured = config.get_main_option("sqlalchemy.url")
    declared_on_disk = _ini_declared_url(config)
    # Deliberate in-code injection (configured value not the ini's own) wins over
    # an ambient UMS_DATABASE_URL — this is the footgun fix.
    if configured and configured != declared_on_disk:
        return configured
    # No in-code override (production ini placeholder, or an ini-only run):
    # UMS_DATABASE_URL overrides the placeholder, preserving the prod contract.
    url = environ.get("UMS_DATABASE_URL") or configured
    if not url:
        raise RuntimeError(
            "Database URL not configured. Set UMS_DATABASE_URL or sqlalchemy.url in alembic.ini."
        )
    return url
