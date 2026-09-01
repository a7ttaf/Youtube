"""Dialect-boundary tests for the analytics summary evidence fence."""

from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM
from ums_smart_revenue.reports.analytics_summary_csv import (
    AnalyticsSummaryCsvValidationError,
    _projecting_analytics_source_row_predicate,
)


def test_postgres_projection_fence_compiles_jsonb_key_existence_checks() -> None:
    """Production SQL distinguishes absent legacy tokens from JSON null keys."""
    dialect = postgresql.dialect()
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=dialect),
    )
    predicate = _projecting_analytics_source_row_predicate(
        session=session,  # type: ignore[arg-type]
        source_row=GoogleRevenueSourceRowORM,
    )
    sql = str(
        predicate.compile(
            dialect=dialect,
            compile_kwargs={"literal_binds": True},
        )
    )
    assert sql.count("jsonb_exists") == 3
    assert "jsonb_typeof" in sql
    assert "reports.query.country_evidence" not in sql
    assert "google_revenue_source_rows.report_type = 'reports.query'" in sql
    assert "'PROJECTING'" in sql


def test_unknown_database_dialect_fails_closed() -> None:
    """A future backend cannot silently omit the evidence exclusion."""
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="unknown")),
    )
    with pytest.raises(AnalyticsSummaryCsvValidationError, match="unknown"):
        _projecting_analytics_source_row_predicate(
            session=session,  # type: ignore[arg-type]
            source_row=GoogleRevenueSourceRowORM,
        )
