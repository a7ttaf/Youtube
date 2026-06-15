"""Lightweight SQLite-friendly assertions for migration revision metadata.

The full PostgreSQL upgrade -> downgrade -> upgrade round-trip lives at
tests/db/test_google_revenue_source_migration_postgres.py (Phase 8).
"""

import importlib


def test_revision_metadata() -> None:
    module = importlib.import_module(
        "ums_smart_revenue.db.alembic.versions.20260523_0001_google_revenue_source_foundation"
    )
    assert module.revision == "20260523_0001"
    assert module.down_revision == "20260521_0001"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_supported_v1_set_is_complete_in_migration_constant() -> None:
    module = importlib.import_module(
        "ums_smart_revenue.db.alembic.versions.20260523_0001_google_revenue_source_foundation"
    )
    assert module._SUPPORTED_V1_CODES == ("AED", "USD", "EUR", "GBP", "SAR", "EGP")
