from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.explanation_models import (
    ExplanationBase,
    NumberExplanationORM,
)
from ums_smart_revenue.finance.explanations import (
    ADJUSTED_GROSS_REVENUE_METRIC,
    NumberExplanationEntry,
    NumberExplanationValidationError,
    SqlAlchemyNumberExplanationRepository,
    build_channel_month_revenue_explanation,
)
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


def build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ExplanationBase.metadata.create_all(engine)
    return Session(engine)


def revenue_fact(
    *,
    source_kind: str,
    gross_revenue_usd: str,
    confidence_score: str = "0.9800",
    month: str = "2026-03",
    youtube_channel_id: str = "channel-tv-a",
    source_report_id: str | None = None,
) -> RevenueFactEntry:
    return RevenueFactEntry(
        id=f"fact-{source_kind}",
        month=month,
        youtube_channel_id=youtube_channel_id,
        source_kind=source_kind,
        source_report_id=source_report_id or f"report-{source_kind}",
        gross_revenue_usd=Decimal(gross_revenue_usd),
        net_revenue_usd=None,
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal(confidence_score),
        imported_by=None,
    )


def manual_override(
    *,
    status: str,
    adjustment_revenue_usd: str,
    month: str = "2026-03",
    youtube_channel_id: str = "channel-tv-a",
) -> RevenueManualOverrideEntry:
    return RevenueManualOverrideEntry(
        id=f"override-{status}-{adjustment_revenue_usd}",
        month=month,
        youtube_channel_id=youtube_channel_id,
        adjustment_revenue_usd=Decimal(adjustment_revenue_usd),
        reason="Revenue correction",
        status=status,
        created_by="00000000-0000-0000-0000-000000009401",
        approved_by=(
            "00000000-0000-0000-0000-000000009402" if status == "APPROVED" else None
        ),
        approval_reason="Approved source correction" if status == "APPROVED" else None,
    )


# -----------------------------------------------------------------------------
# NumberExplanationEntry.to_api
# -----------------------------------------------------------------------------


def test_to_api_serializes_all_fields_and_normalizes_decimals():
    entry = NumberExplanationEntry(
        month="2026-03",
        entity_type="channel",
        entity_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
        value=Decimal("1125.500000"),
        currency="USD",
        formula="baseline_gross_revenue_usd + approved_manual_override_total_usd",
        confidence={"label": "HIGH", "score": "0.98"},
        components=[{"key": "baseline_gross_revenue_usd", "value": "1000"}],
        warnings=[{"code": "PENDING_MANUAL_OVERRIDES"}],
    )

    assert entry.to_api() == {
        "month": "2026-03",
        "entity_type": "channel",
        "entity_id": "channel-tv-a",
        "metric": ADJUSTED_GROSS_REVENUE_METRIC,
        "value": "1125.5",
        "currency": "USD",
        "formula": "baseline_gross_revenue_usd + approved_manual_override_total_usd",
        "confidence": {"label": "HIGH", "score": "0.98"},
        "components": [{"key": "baseline_gross_revenue_usd", "value": "1000"}],
        "warnings": [{"code": "PENDING_MANUAL_OVERRIDES"}],
    }


def test_to_api_emits_integer_value_without_decimal_point():
    entry = NumberExplanationEntry(
        month="2026-03",
        entity_type="channel",
        entity_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
        value=Decimal("1000.00"),
        currency="USD",
        formula="",
        confidence={"label": "HIGH", "score": "0.98"},
        components=[],
        warnings=[],
    )

    assert entry.to_api()["value"] == "1000"


def test_to_api_preserves_negative_sign():
    entry = NumberExplanationEntry(
        month="2026-03",
        entity_type="channel",
        entity_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
        value=Decimal("-50.50"),
        currency="USD",
        formula="",
        confidence={"label": "LOW", "score": "0"},
        components=[],
        warnings=[],
    )

    assert entry.to_api()["value"] == "-50.5"


def test_to_api_emits_zero_as_plain_zero():
    entry = NumberExplanationEntry(
        month="2026-03",
        entity_type="channel",
        entity_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
        value=Decimal("0.000"),
        currency="USD",
        formula="",
        confidence={"label": "LOW", "score": "0"},
        components=[],
        warnings=[],
    )

    assert entry.to_api()["value"] == "0"


# -----------------------------------------------------------------------------
# SqlAlchemyNumberExplanationRepository.record_explanation
# -----------------------------------------------------------------------------


def test_record_explanation_inserts_new_row_with_tenant_stamp_and_all_fields():
    with build_session() as session:
        repo = SqlAlchemyNumberExplanationRepository(session)
        entry = NumberExplanationEntry(
            month="2026-03",
            entity_type="channel",
            entity_id="channel-tv-a",
            metric=ADJUSTED_GROSS_REVENUE_METRIC,
            value=Decimal("1125.50"),
            currency="USD",
            formula="baseline + override",
            confidence={"label": "HIGH", "score": "0.98"},
            components=[{"key": "baseline", "value": "1000"}],
            warnings=[{"code": "PENDING"}],
        )

        result = repo.record_explanation(entry)
        session.commit()

        assert result is entry
        row = session.scalars(select(NumberExplanationORM)).one()
        assert row.tenant_id == DEFAULT_TENANT_UUID
        assert row.month == "2026-03"
        assert row.entity_type == "channel"
        assert row.entity_id == "channel-tv-a"
        assert row.metric == ADJUSTED_GROSS_REVENUE_METRIC
        assert row.value == Decimal("1125.50")
        assert row.currency == "USD"
        assert row.formula == "baseline + override"
        assert row.confidence == "HIGH"
        assert row.components == [{"key": "baseline", "value": "1000"}]
        assert row.warnings == [{"code": "PENDING"}]
        assert row.created_at is not None
        assert row.updated_at == row.created_at


def test_record_explanation_updates_existing_row_in_place():
    with build_session() as session:
        repo = SqlAlchemyNumberExplanationRepository(session)
        initial = NumberExplanationEntry(
            month="2026-03",
            entity_type="channel",
            entity_id="channel-tv-a",
            metric=ADJUSTED_GROSS_REVENUE_METRIC,
            value=Decimal("1000.00"),
            currency="USD",
            formula="baseline",
            confidence={"label": "MEDIUM", "score": "0.80"},
            components=[{"key": "baseline", "value": "1000"}],
            warnings=[],
        )
        repo.record_explanation(initial)
        session.commit()
        original_row = session.scalars(select(NumberExplanationORM)).one()
        original_id = original_row.id
        original_created_at = original_row.created_at

        revised = NumberExplanationEntry(
            month="2026-03",
            entity_type="channel",
            entity_id="channel-tv-a",
            metric=ADJUSTED_GROSS_REVENUE_METRIC,
            value=Decimal("1125.50"),
            currency="USD",
            formula="baseline + override",
            confidence={"label": "HIGH", "score": "0.98"},
            components=[
                {"key": "baseline", "value": "1000"},
                {"key": "override", "value": "125.5"},
            ],
            warnings=[{"code": "PENDING_MANUAL_OVERRIDES"}],
        )
        repo.record_explanation(revised)
        session.commit()

        rows = session.scalars(select(NumberExplanationORM)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == original_id
        assert row.created_at == original_created_at
        assert row.value == Decimal("1125.50")
        assert row.formula == "baseline + override"
        assert row.confidence == "HIGH"
        assert row.components == [
            {"key": "baseline", "value": "1000"},
            {"key": "override", "value": "125.5"},
        ]
        assert row.warnings == [{"code": "PENDING_MANUAL_OVERRIDES"}]


def test_record_explanation_separates_rows_by_composite_key():
    with build_session() as session:
        repo = SqlAlchemyNumberExplanationRepository(session)
        base_kwargs = {
            "currency": "USD",
            "formula": "baseline",
            "confidence": {"label": "HIGH", "score": "0.98"},
            "components": [],
            "warnings": [],
            "value": Decimal("100.00"),
        }
        repo.record_explanation(
            NumberExplanationEntry(
                month="2026-03",
                entity_type="channel",
                entity_id="channel-a",
                metric=ADJUSTED_GROSS_REVENUE_METRIC,
                **base_kwargs,
            )
        )
        repo.record_explanation(
            NumberExplanationEntry(
                month="2026-04",
                entity_type="channel",
                entity_id="channel-a",
                metric=ADJUSTED_GROSS_REVENUE_METRIC,
                **base_kwargs,
            )
        )
        repo.record_explanation(
            NumberExplanationEntry(
                month="2026-03",
                entity_type="company",
                entity_id="channel-a",
                metric=ADJUSTED_GROSS_REVENUE_METRIC,
                **base_kwargs,
            )
        )
        repo.record_explanation(
            NumberExplanationEntry(
                month="2026-03",
                entity_type="channel",
                entity_id="channel-b",
                metric=ADJUSTED_GROSS_REVENUE_METRIC,
                **base_kwargs,
            )
        )
        session.commit()

        assert session.scalars(select(NumberExplanationORM)).all().__len__() == 4


def test_record_explanation_isolates_writes_between_tenants():
    with build_session() as session:
        primary_repo = SqlAlchemyNumberExplanationRepository(session)
        primary_repo.record_explanation(
            NumberExplanationEntry(
                month="2026-03",
                entity_type="channel",
                entity_id="channel-tv-a",
                metric=ADJUSTED_GROSS_REVENUE_METRIC,
                value=Decimal("1000.00"),
                currency="USD",
                formula="baseline",
                confidence={"label": "HIGH", "score": "0.98"},
                components=[],
                warnings=[],
            )
        )

        other_tenant = UUID("00000000-0000-0000-0000-000000061999")
        other_repo = SqlAlchemyNumberExplanationRepository(session)
        other_repo._tenant_id = other_tenant  # noqa: SLF001
        other_repo.record_explanation(
            NumberExplanationEntry(
                month="2026-03",
                entity_type="channel",
                entity_id="channel-tv-a",
                metric=ADJUSTED_GROSS_REVENUE_METRIC,
                value=Decimal("2500.00"),
                currency="USD",
                formula="baseline",
                confidence={"label": "HIGH", "score": "0.98"},
                components=[],
                warnings=[],
            )
        )
        session.commit()

        rows = session.scalars(
            select(NumberExplanationORM).order_by(NumberExplanationORM.tenant_id)
        ).all()
        assert len(rows) == 2
        tenant_values = {row.tenant_id: row.value for row in rows}
        assert tenant_values[DEFAULT_TENANT_UUID] == Decimal("1000.00")
        assert tenant_values[other_tenant] == Decimal("2500.00")


def test_record_explanation_does_not_update_other_tenants_row():
    with build_session() as session:
        other_tenant = UUID("00000000-0000-0000-0000-000000061998")
        other_repo = SqlAlchemyNumberExplanationRepository(session)
        other_repo._tenant_id = other_tenant  # noqa: SLF001
        other_repo.record_explanation(
            NumberExplanationEntry(
                month="2026-03",
                entity_type="channel",
                entity_id="channel-tv-a",
                metric=ADJUSTED_GROSS_REVENUE_METRIC,
                value=Decimal("9999.99"),
                currency="USD",
                formula="foreign",
                confidence={"label": "HIGH", "score": "0.95"},
                components=[],
                warnings=[],
            )
        )
        session.commit()

        primary_repo = SqlAlchemyNumberExplanationRepository(session)
        primary_repo.record_explanation(
            NumberExplanationEntry(
                month="2026-03",
                entity_type="channel",
                entity_id="channel-tv-a",
                metric=ADJUSTED_GROSS_REVENUE_METRIC,
                value=Decimal("125.50"),
                currency="USD",
                formula="local",
                confidence={"label": "MEDIUM", "score": "0.80"},
                components=[],
                warnings=[],
            )
        )
        session.commit()

        rows = session.scalars(select(NumberExplanationORM)).all()
        assert len(rows) == 2
        by_tenant = {row.tenant_id: row for row in rows}
        assert by_tenant[other_tenant].value == Decimal("9999.99")
        assert by_tenant[other_tenant].formula == "foreign"
        assert by_tenant[DEFAULT_TENANT_UUID].value == Decimal("125.50")
        assert by_tenant[DEFAULT_TENANT_UUID].formula == "local"


# -----------------------------------------------------------------------------
# build_channel_month_revenue_explanation
# -----------------------------------------------------------------------------


def test_build_explanation_happy_path_with_approved_and_pending_overrides():
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00"),
            revenue_fact(source_kind="ADSENSE", gross_revenue_usd="930.00"),
        ],
        manual_overrides=[
            manual_override(status="APPROVED", adjustment_revenue_usd="125.50"),
            manual_override(status="PENDING", adjustment_revenue_usd="-50.00"),
        ],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.month == "2026-03"
    assert entry.entity_type == "channel"
    assert entry.entity_id == "channel-tv-a"
    assert entry.metric == ADJUSTED_GROSS_REVENUE_METRIC
    assert entry.value == Decimal("1125.50")
    assert entry.currency == "USD"
    assert entry.formula == (
        "baseline_gross_revenue_usd + approved_manual_override_total_usd"
    )
    assert entry.components == [
        {
            "key": "baseline_gross_revenue_usd",
            "label": "Baseline gross revenue",
            "value": "1000",
            "source_kind": "YOUTUBE_CMS",
            "source_report_id": "report-YOUTUBE_CMS",
        },
        {
            "key": "approved_manual_override_total_usd",
            "label": "Approved manual overrides",
            "value": "125.5",
            "count": 1,
        },
    ]
    assert entry.warnings == [
        {
            "code": "PENDING_MANUAL_OVERRIDES",
            "message": (
                "1 pending manual override is "
                f"not included in {ADJUSTED_GROSS_REVENUE_METRIC}."
            ),
        }
    ]


def test_build_explanation_picks_primary_fact_by_source_priority():
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(source_kind="ADSENSE", gross_revenue_usd="500.00"),
            revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00"),
            revenue_fact(source_kind="YOUTUBE_ANALYTICS", gross_revenue_usd="450.00"),
        ],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    baseline_component = next(
        c for c in entry.components if c["key"] == "baseline_gross_revenue_usd"
    )
    assert baseline_component["source_kind"] == "YOUTUBE_CMS"
    assert entry.value == Decimal("1000.00")


def test_build_explanation_warns_on_no_facts_and_yields_zero_value():
    entry = build_channel_month_revenue_explanation(
        facts=[],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-orphan",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.value == Decimal("0")
    baseline_component = next(
        c for c in entry.components if c["key"] == "baseline_gross_revenue_usd"
    )
    assert baseline_component["source_kind"] is None
    assert baseline_component["source_report_id"] is None
    warning_codes = [w["code"] for w in entry.warnings]
    assert "NO_REVENUE_FACTS" in warning_codes
    assert entry.confidence == {"label": "LOW", "score": "0"}


def test_build_explanation_pluralizes_pending_override_warning():
    entry = build_channel_month_revenue_explanation(
        facts=[revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00")],
        manual_overrides=[
            manual_override(status="PENDING", adjustment_revenue_usd="50.00"),
            manual_override(status="PENDING", adjustment_revenue_usd="75.00"),
        ],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    pending_warnings = [
        w for w in entry.warnings if w["code"] == "PENDING_MANUAL_OVERRIDES"
    ]
    assert pending_warnings == [
        {
            "code": "PENDING_MANUAL_OVERRIDES",
            "message": (
                "2 pending manual overrides are "
                f"not included in {ADJUSTED_GROSS_REVENUE_METRIC}."
            ),
        }
    ]


def test_build_explanation_clamps_high_confidence_when_warnings_present():
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="0.9800",
            )
        ],
        manual_overrides=[
            manual_override(status="PENDING", adjustment_revenue_usd="50.00")
        ],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.confidence == {"label": "HIGH", "score": "0.9"}


def test_build_explanation_does_not_clamp_when_score_already_below_ceiling():
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="0.8500",
            )
        ],
        manual_overrides=[
            manual_override(status="PENDING", adjustment_revenue_usd="50.00")
        ],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.confidence == {"label": "MEDIUM", "score": "0.85"}


def test_build_explanation_labels_low_confidence_below_medium_band():
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="MANUAL_UPLOAD",
                gross_revenue_usd="1000.00",
                confidence_score="0.5000",
            )
        ],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.confidence == {"label": "LOW", "score": "0.5"}


def test_build_explanation_rejects_unsupported_metric():
    with pytest.raises(NumberExplanationValidationError) as excinfo:
        build_channel_month_revenue_explanation(
            facts=[],
            manual_overrides=[],
            month="2026-03",
            youtube_channel_id="channel-tv-a",
            metric="some_unknown_metric",
        )
    assert "Unsupported explanation metric: some_unknown_metric" in str(excinfo.value)


def test_build_explanation_counts_only_approved_overrides_in_component():
    entry = build_channel_month_revenue_explanation(
        facts=[revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00")],
        manual_overrides=[
            manual_override(status="APPROVED", adjustment_revenue_usd="50.00"),
            manual_override(status="APPROVED", adjustment_revenue_usd="25.00"),
            manual_override(status="PENDING", adjustment_revenue_usd="-10.00"),
        ],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    approved_component = next(
        c for c in entry.components if c["key"] == "approved_manual_override_total_usd"
    )
    assert approved_component["count"] == 2
    assert approved_component["value"] == "75"


def test_build_explanation_round_trip_through_to_api_serializes_full_shape():
    entry = build_channel_month_revenue_explanation(
        facts=[revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00")],
        manual_overrides=[
            manual_override(status="APPROVED", adjustment_revenue_usd="125.50")
        ],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    api = entry.to_api()
    assert api["metric"] == ADJUSTED_GROSS_REVENUE_METRIC
    assert api["value"] == "1125.5"
    assert api["currency"] == "USD"
    assert api["confidence"] == {"label": "HIGH", "score": "0.98"}
    assert api["warnings"] == []
    assert api["formula"] == (
        "baseline_gross_revenue_usd + approved_manual_override_total_usd"
    )


# -----------------------------------------------------------------------------
# End-to-end: build then persist via repository
# -----------------------------------------------------------------------------


def test_build_then_record_persists_full_explanation_into_database():
    with build_session() as session:
        entry = build_channel_month_revenue_explanation(
            facts=[
                revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00")
            ],
            manual_overrides=[
                manual_override(status="APPROVED", adjustment_revenue_usd="125.50")
            ],
            month="2026-03",
            youtube_channel_id="channel-tv-a",
            metric=ADJUSTED_GROSS_REVENUE_METRIC,
        )
        repo = SqlAlchemyNumberExplanationRepository(session)
        repo.record_explanation(entry)
        session.commit()

        row = session.scalars(select(NumberExplanationORM)).one()
        assert row.tenant_id == DEFAULT_TENANT_UUID
        assert row.metric == ADJUSTED_GROSS_REVENUE_METRIC
        assert row.value == Decimal("1125.50")
        assert row.confidence == "HIGH"
        assert row.components[0]["key"] == "baseline_gross_revenue_usd"
        assert row.components[1]["key"] == "approved_manual_override_total_usd"
        assert row.warnings == []


def test_repository_default_tenant_id_matches_constant():
    with build_session() as session:
        repo = SqlAlchemyNumberExplanationRepository(session)
        assert repo._tenant_id == UUID(UMS_TENANT_ID)  # noqa: SLF001
