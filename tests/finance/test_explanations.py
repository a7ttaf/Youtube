import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.explanation_models import (
    ExplanationBase,
    NumberExplanationORM,
)
from ums_smart_revenue.finance.allocation import AllocationLine
from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.explanations import (
    ADJUSTED_GROSS_REVENUE_METRIC,
    NET_REVENUE_METRIC,
    NumberExplanationEntry,
    NumberExplanationValidationError,
    SqlAlchemyNumberExplanationRepository,
    build_channel_month_revenue_explanation,
    map_net_confidence,
)
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
OTHER_TENANT_UUID = UUID("00000000-0000-0000-0000-000000094999")


def build_session() -> Session:
    """Create an in-memory SQLite session with the explanation schema."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ExplanationBase.metadata.create_all(engine)
    return Session(engine)


def tenant(tenant_id: UUID = OTHER_TENANT_UUID) -> Tenant:
    """Build a Tenant for ambient-context tests."""
    now = datetime.now(UTC)
    return Tenant(
        id=tenant_id,
        slug="other",
        display_name="Other Tenant",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def revenue_fact(
    *,
    source_kind: str,
    gross_revenue_usd: str,
    confidence_score: str = "0.9800",
    month: str = "2026-03",
    youtube_channel_id: str = "channel-tv-a",
    source_report_id: str | None = None,
    net_revenue_usd: str | None = None,
) -> RevenueFactEntry:
    """Build a RevenueFactEntry for test scenarios."""
    return RevenueFactEntry(
        id=f"fact-{source_kind}",
        month=month,
        youtube_channel_id=youtube_channel_id,
        source_kind=source_kind,
        source_report_id=source_report_id or f"report-{source_kind}",
        gross_revenue_usd=Decimal(gross_revenue_usd),
        net_revenue_usd=(Decimal(net_revenue_usd) if net_revenue_usd is not None else None),
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal(confidence_score),
        imported_by=None,
    )


def deduction_component(
    *,
    component_key: str,
    amount_usd: str,
    component_kind: str = "TAX",
    source_system: str = "adsense_management",
    scope_id: str = "channel-tv-a",
    month: str = "2026-03",
) -> DeductionComponent:
    """Build a channel-scoped DeductionComponent for test scenarios."""
    return DeductionComponent(
        id=f"dc-{component_key}",
        month=month,
        component_kind=component_kind,
        scope_kind="CHANNEL",
        scope_id=scope_id,
        amount_usd=Decimal(amount_usd),
        amount_native=None,
        currency_code="USD",
        source_system=source_system,
        source_table="deduction_components",
        source_id=None,
        source_key=None,
        source_report_id=None,
        raw_payload={},
        component_key=component_key,
    )


def allocation_line(
    *,
    component_key: str,
    allocated_amount_usd: str,
    adsense_account_id: str = "pub-1",
    youtube_channel_id: str = "channel-tv-a",
    component_kind: str = "DEDUCTION",
    source_system: str = "adsense_management",
    basis_source_kind: str = "ADSENSE",
    basis_share: str = "0.5",
    net_applicable: bool = True,
) -> AllocationLine:
    """Build an AllocationLine for test scenarios."""
    return AllocationLine(
        adsense_account_id=adsense_account_id,
        youtube_channel_id=youtube_channel_id,
        component_kind=component_kind,
        source_system=source_system,
        component_key=component_key,
        basis_source_kind=basis_source_kind,
        basis_amount_usd=Decimal("1000.000000"),
        basis_share=Decimal(basis_share),
        allocated_amount_usd=Decimal(allocated_amount_usd),
        net_applicable=net_applicable,
    )


def manual_override(
    *,
    status: str,
    adjustment_revenue_usd: str,
    month: str = "2026-03",
    youtube_channel_id: str = "channel-tv-a",
) -> RevenueManualOverrideEntry:
    """Build a RevenueManualOverrideEntry for test scenarios."""
    return RevenueManualOverrideEntry(
        id=f"override-{status}-{adjustment_revenue_usd}",
        month=month,
        youtube_channel_id=youtube_channel_id,
        adjustment_revenue_usd=Decimal(adjustment_revenue_usd),
        reason="Revenue correction",
        status=status,
        created_by="00000000-0000-0000-0000-000000009401",
        approved_by=("00000000-0000-0000-0000-000000009402" if status == "APPROVED" else None),
        approval_reason="Approved source correction" if status == "APPROVED" else None,
    )


# -----------------------------------------------------------------------------
# NumberExplanationEntry.to_api
# -----------------------------------------------------------------------------


def test_to_api_serializes_all_fields_and_normalizes_decimals():
    """to_api serializes every field and normalizes decimal values."""
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
    """to_api emits an integer-valued amount without a trailing decimal point."""
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
    """to_api preserves the negative sign on a negative value."""
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
    """to_api renders a zero value as a plain '0'."""
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
    """record_explanation inserts a new row stamped with the tenant and all fields."""
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
        assert json.loads(row.confidence) == {"label": "HIGH", "score": "0.98"}
        assert row.components == [{"key": "baseline", "value": "1000"}]
        assert row.warnings == [{"code": "PENDING"}]
        assert row.created_at is not None
        assert row.updated_at == row.created_at


def test_record_explanation_uses_ambient_tenant_context():
    """record_explanation resolves the tenant from ambient context when unset."""
    with build_session() as session:
        token = TENANT_CTX.set(tenant())
        try:
            repo = SqlAlchemyNumberExplanationRepository(session)
            repo.record_explanation(
                NumberExplanationEntry(
                    month="2026-03",
                    entity_type="channel",
                    entity_id="channel-tv-a",
                    metric=ADJUSTED_GROSS_REVENUE_METRIC,
                    value=Decimal("1125.50"),
                    currency="USD",
                    formula="baseline + override",
                    confidence={"label": "HIGH", "score": "0.98"},
                    components=[],
                    warnings=[],
                )
            )
            session.commit()
        finally:
            TENANT_CTX.reset(token)

        row = session.scalars(select(NumberExplanationORM)).one()
        assert row.tenant_id == OTHER_TENANT_UUID


def test_record_explanation_updates_existing_row_in_place():
    """record_explanation updates an existing row in place, preserving id and created_at."""
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
        assert json.loads(row.confidence) == {"label": "HIGH", "score": "0.98"}
        assert row.components == [
            {"key": "baseline", "value": "1000"},
            {"key": "override", "value": "125.5"},
        ]
        assert row.warnings == [{"code": "PENDING_MANUAL_OVERRIDES"}]


def test_record_explanation_separates_rows_by_composite_key():
    """record_explanation keeps rows distinct per (month, entity, metric) composite key."""
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
                entity_id="channel-a",
                metric="adjusted_gross_revenue_usd_alternate",
                **base_kwargs,
            )
        )
        session.commit()

        assert session.scalars(select(NumberExplanationORM)).all().__len__() == 4


def test_record_explanation_isolates_writes_between_tenants():
    """record_explanation isolates writes between tenants into separate rows."""
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
        other_repo = SqlAlchemyNumberExplanationRepository(session, tenant_id=other_tenant)
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
    """record_explanation never overwrites another tenant's row."""
    with build_session() as session:
        other_tenant = UUID("00000000-0000-0000-0000-000000061998")
        other_repo = SqlAlchemyNumberExplanationRepository(session, tenant_id=other_tenant)
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
    """Adjusted-gross explanation includes approved overrides and warns on pending ones."""
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
    assert entry.formula == ("baseline_gross_revenue_usd + approved_manual_override_total_usd")
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
                f"1 pending manual override is not included in {ADJUSTED_GROSS_REVENUE_METRIC}."
            ),
        }
    ]


def test_build_explanation_picks_primary_fact_by_source_priority():
    """The baseline component uses the highest-priority source fact."""
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
        (c for c in entry.components if c["key"] == "baseline_gross_revenue_usd"),
        None,
    )
    assert baseline_component is not None
    assert baseline_component["source_kind"] == "YOUTUBE_CMS"
    assert entry.value == Decimal("1000.00")


def test_build_explanation_warns_on_no_facts_and_yields_zero_value():
    """With no facts the explanation yields a zero value and a NO_REVENUE_FACTS warning."""
    entry = build_channel_month_revenue_explanation(
        facts=[],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-orphan",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.value == Decimal("0")
    baseline_component = next(
        (c for c in entry.components if c["key"] == "baseline_gross_revenue_usd"),
        None,
    )
    assert baseline_component is not None
    assert baseline_component["source_kind"] is None
    assert baseline_component["source_report_id"] is None
    warning_codes = [w["code"] for w in entry.warnings]
    assert "NO_REVENUE_FACTS" in warning_codes
    assert entry.confidence == {"label": "LOW", "score": "0"}


def test_build_explanation_preserves_null_primary_source_report_id():
    """A missing report identifier remains null in the provenance component."""
    fact = replace(
        revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00"),
        source_report_id=None,
    )

    entry = build_channel_month_revenue_explanation(
        facts=[fact],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    baseline_components = [
        component
        for component in entry.components
        if component["key"] == "baseline_gross_revenue_usd"
    ]
    assert len(baseline_components) == 1
    baseline_component = baseline_components[0]
    assert baseline_component["source_kind"] == "YOUTUBE_CMS"
    assert baseline_component["source_report_id"] is None


def test_build_explanation_pluralizes_pending_override_warning():
    """The pending-override warning is pluralized for multiple pending overrides."""
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

    pending_warnings = [w for w in entry.warnings if w["code"] == "PENDING_MANUAL_OVERRIDES"]
    assert pending_warnings == [
        {
            "code": "PENDING_MANUAL_OVERRIDES",
            "message": (
                f"2 pending manual overrides are not included in {ADJUSTED_GROSS_REVENUE_METRIC}."
            ),
        }
    ]


def test_build_explanation_keeps_combined_pending_and_no_fact_warnings():
    """Pending overrides and absent facts retain both warnings in stable order."""
    entry = build_channel_month_revenue_explanation(
        facts=[],
        manual_overrides=[
            manual_override(
                status="PENDING",
                adjustment_revenue_usd="50.00",
                youtube_channel_id="channel-orphan",
            )
        ],
        month="2026-03",
        youtube_channel_id="channel-orphan",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.value == Decimal("0")
    assert [warning["code"] for warning in entry.warnings] == [
        "PENDING_MANUAL_OVERRIDES",
        "NO_REVENUE_FACTS",
    ]
    assert entry.confidence == {"label": "LOW", "score": "0"}


def test_build_explanation_never_labels_a_warned_fact_high():
    """A warned fact caps at MEDIUM even at the manual-import default score of 1.

    Regression guard for the no-op confidence cap: the score clamp pinned a
    warned fact to exactly 0.9000, which the old ``score >= 0.9000`` label rule
    still called HIGH. The manual-import beta stores
    ``confidence_score=Decimal("1")`` on every fact, so the warning-aware label
    rule must keep a warning-bearing default fact out of the HIGH band.
    """
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="1",
            )
        ],
        manual_overrides=[manual_override(status="PENDING", adjustment_revenue_usd="50.00")],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert [w["code"] for w in entry.warnings] == ["PENDING_MANUAL_OVERRIDES"]
    assert entry.confidence == {"label": "MEDIUM", "score": "0.9"}


@pytest.mark.parametrize(
    ("confidence_score", "expected_confidence"),
    [
        ("0.9000", {"label": "MEDIUM", "score": "0.9"}),
        ("0.7000", {"label": "MEDIUM", "score": "0.7"}),
        ("0.6999", {"label": "LOW", "score": "0.6999"}),
    ],
)
def test_build_explanation_warned_confidence_band_boundaries(
    confidence_score: str, expected_confidence: dict[str, str]
):
    """Warnings preserve the medium floor and keep the HIGH badge unavailable."""
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score=confidence_score,
            )
        ],
        manual_overrides=[manual_override(status="PENDING", adjustment_revenue_usd="50.00")],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.confidence == expected_confidence


def test_build_explanation_labels_a_clean_fact_high():
    """A fact with no warnings keeps the HIGH badge at the default score of 1."""
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="1",
            )
        ],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.warnings == []
    assert entry.confidence == {"label": "HIGH", "score": "1"}


def test_build_explanation_labels_a_clean_fact_at_the_high_floor_high():
    """A clean fact sitting exactly on the 0.9000 HIGH floor still labels HIGH."""
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="0.9000",
            )
        ],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.warnings == []
    assert entry.confidence == {"label": "HIGH", "score": "0.9"}


def test_build_explanation_badge_alone_does_not_encode_warning_presence():
    """Clean and warned facts can share MEDIUM; warnings remain separate state."""
    clean_entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="0.8500",
            )
        ],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )
    warned_entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="0.9500",
            )
        ],
        manual_overrides=[manual_override(status="PENDING", adjustment_revenue_usd="50.00")],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert clean_entry.confidence == {"label": "MEDIUM", "score": "0.85"}
    assert clean_entry.warnings == []
    assert warned_entry.confidence == {"label": "MEDIUM", "score": "0.9"}
    assert [warning["code"] for warning in warned_entry.warnings] == ["PENDING_MANUAL_OVERRIDES"]


@pytest.mark.parametrize(
    ("confidence_score", "expected_confidence"),
    [
        ("0.7000", {"label": "MEDIUM", "score": "0.7"}),
        ("0.6999", {"label": "LOW", "score": "0.6999"}),
        ("0.8999", {"label": "MEDIUM", "score": "0.8999"}),
    ],
)
def test_build_explanation_clean_confidence_band_boundaries(
    confidence_score: str, expected_confidence: dict[str, str]
):
    """Clean scores honor the inclusive medium floor and exclusive high band."""
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score=confidence_score,
            )
        ],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.warnings == []
    assert entry.confidence == expected_confidence


def test_build_explanation_keeps_the_low_band_for_a_warned_low_score_fact():
    """A warned fact already below the MEDIUM floor keeps its LOW band and score."""
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="MANUAL_UPLOAD",
                gross_revenue_usd="1000.00",
                confidence_score="0.5000",
            )
        ],
        manual_overrides=[manual_override(status="PENDING", adjustment_revenue_usd="50.00")],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert [w["code"] for w in entry.warnings] == ["PENDING_MANUAL_OVERRIDES"]
    assert entry.confidence == {"label": "LOW", "score": "0.5"}


def test_build_explanation_does_not_clamp_when_score_already_below_ceiling():
    """Confidence is left unchanged when the score is already below the clamp ceiling."""
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="0.8500",
            )
        ],
        manual_overrides=[manual_override(status="PENDING", adjustment_revenue_usd="50.00")],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    assert entry.confidence == {"label": "MEDIUM", "score": "0.85"}


def test_build_explanation_labels_low_confidence_below_medium_band():
    """A score below the medium band is labeled LOW."""
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
    """An unsupported metric raises NumberExplanationValidationError."""
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
    """The approved-override component counts and totals only approved overrides."""
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
        (c for c in entry.components if c["key"] == "approved_manual_override_total_usd"),
        None,
    )
    assert approved_component is not None
    assert approved_component["count"] == 2
    assert approved_component["value"] == "75"


def test_build_explanation_round_trip_through_to_api_serializes_full_shape():
    """Building then serializing via to_api produces the full adjusted-gross shape."""
    entry = build_channel_month_revenue_explanation(
        facts=[revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00")],
        manual_overrides=[manual_override(status="APPROVED", adjustment_revenue_usd="125.50")],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=ADJUSTED_GROSS_REVENUE_METRIC,
    )

    api = entry.to_api()
    assert api == {
        "month": "2026-03",
        "entity_type": "channel",
        "entity_id": "channel-tv-a",
        "metric": ADJUSTED_GROSS_REVENUE_METRIC,
        "value": "1125.5",
        "currency": "USD",
        "formula": "baseline_gross_revenue_usd + approved_manual_override_total_usd",
        "confidence": {"label": "HIGH", "score": "0.98"},
        "components": [
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
        ],
        "warnings": [],
    }


# -----------------------------------------------------------------------------
# End-to-end: build then persist via repository
# -----------------------------------------------------------------------------


def test_build_then_record_persists_full_explanation_into_database():
    """Building then recording persists the full explanation into the database."""
    with build_session() as session:
        entry = build_channel_month_revenue_explanation(
            facts=[revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00")],
            manual_overrides=[manual_override(status="APPROVED", adjustment_revenue_usd="125.50")],
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
        assert json.loads(row.confidence) == {"label": "HIGH", "score": "0.98"}
        assert row.components[0]["key"] == "baseline_gross_revenue_usd"
        assert row.components[1]["key"] == "approved_manual_override_total_usd"
        assert row.warnings == []


def test_repository_default_tenant_id_matches_constant():
    """The repository defaults to the UMS bootstrap tenant id."""
    with build_session() as session:
        repo = SqlAlchemyNumberExplanationRepository(session)
        assert repo._tenant_id == UUID(UMS_TENANT_ID)  # noqa: SLF001


def test_repository_rejects_malformed_tenant_id():
    """A malformed tenant id is rejected with NumberExplanationValidationError."""
    with (
        build_session() as session,
        pytest.raises(NumberExplanationValidationError, match="tenant_id must be a valid UUID"),
    ):
        SqlAlchemyNumberExplanationRepository(session, tenant_id="not-a-uuid")


# -----------------------------------------------------------------------------
# SqlAlchemyNumberExplanationRepository.get_explanation -- confidence round-trip
# -----------------------------------------------------------------------------


def test_get_explanation_round_trips_full_confidence_dict_with_score():
    """record_explanation + get_explanation preserves the full confidence dict.

    Regression for chatgpt-codex-connector PRRT_kwDOSZIgN86IT-bT: the previous
    writer stored only the label string, so the reader reconstructed
    ``{"label": "..."}`` and dropped the score that build_reconciliation_explanation
    had computed for the persisted explanation.
    """
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
            confidence={"label": "MEDIUM", "score": "0.80"},
            components=[{"key": "baseline", "value": "1000"}],
            warnings=[],
        )
        repo.record_explanation(entry)
        session.commit()

        loaded = repo.get_explanation(
            month="2026-03",
            entity_type="channel",
            entity_id="channel-tv-a",
            metric=ADJUSTED_GROSS_REVENUE_METRIC,
        )
        assert loaded is not None
        assert loaded.confidence == {"label": "MEDIUM", "score": "0.80"}
        # The to_api serialization must also surface the score, since that is
        # what the GET reconciliation endpoint emits to operators.
        assert loaded.to_api()["confidence"] == {"label": "MEDIUM", "score": "0.80"}


def test_get_explanation_reads_legacy_label_only_rows_without_crashing():
    """Rows persisted before the score fix stay readable as label-only dicts.

    Pre-fix rows wrote ``row.confidence = "HIGH"`` (label string). The reader
    must still serve those rows instead of 500-ing on the GET path.
    """
    with build_session() as session:
        repo = SqlAlchemyNumberExplanationRepository(session)
        repo.record_explanation(
            NumberExplanationEntry(
                month="2026-03",
                entity_type="channel",
                entity_id="channel-tv-a",
                metric=ADJUSTED_GROSS_REVENUE_METRIC,
                value=Decimal("1000.00"),
                currency="USD",
                formula="baseline",
                confidence={"label": "HIGH", "score": "0.95"},
                components=[],
                warnings=[],
            )
        )
        session.commit()

        # Simulate a legacy row by overwriting the on-disk label to plain text.
        row = session.scalars(select(NumberExplanationORM)).one()
        row.confidence = "LOW"
        session.commit()

        loaded = repo.get_explanation(
            month="2026-03",
            entity_type="channel",
            entity_id="channel-tv-a",
            metric=ADJUSTED_GROSS_REVENUE_METRIC,
        )
        assert loaded is not None
        assert loaded.confidence == {"label": "LOW", "score": "0"}


def test_get_explanation_returns_none_for_missing_row():
    """A missing explanation row returns None (route turns that into 404)."""
    with build_session() as session:
        repo = SqlAlchemyNumberExplanationRepository(session)
        assert (
            repo.get_explanation(
                month="2026-03",
                entity_type="channel",
                entity_id="absent",
                metric=ADJUSTED_GROSS_REVENUE_METRIC,
            )
            is None
        )


# -----------------------------------------------------------------------------
# map_net_confidence
# -----------------------------------------------------------------------------


def test_map_net_confidence_pins_each_net_label_and_defaults_low():
    """map_net_confidence pins each net label and defaults unknown labels to LOW."""
    assert map_net_confidence("B_RECONCILED") == {"label": "HIGH", "score": "0.95"}
    assert map_net_confidence("D_ESTIMATED") == {"label": "MEDIUM", "score": "0.80"}
    assert map_net_confidence("E_MISSING") == {"label": "LOW", "score": "0"}
    # Defensive default for any unexpected/future label.
    assert map_net_confidence("SOMETHING_NEW") == {"label": "LOW", "score": "0"}


# -----------------------------------------------------------------------------
# build_channel_month_revenue_explanation -- net_revenue_usd metric
# -----------------------------------------------------------------------------


def test_net_explanation_source_net_path_single_source_deduction_component():
    """Source-net path emits a single source-reported deduction component."""
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                net_revenue_usd="900.00",
            )
        ],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=NET_REVENUE_METRIC,
    )
    assert entry.metric == NET_REVENUE_METRIC
    assert entry.value == Decimal("900.00")
    assert entry.confidence == {"label": "HIGH", "score": "0.95"}  # B_RECONCILED
    keys = [c["key"] for c in entry.components]
    assert "source_reported_deduction_usd" in keys
    assert "account_allocated_deduction_usd" not in keys  # None on source-net path
    src = next(
        (c for c in entry.components if c["key"] == "source_reported_deduction_usd"),
        None,
    )
    assert src is not None
    assert src["value"] == "100"  # 1000 - 900
    assert src["source_kind"] == "YOUTUBE_CMS"
    baseline = next(
        (c for c in entry.components if c["key"] == "baseline_gross_revenue_usd"),
        None,
    )
    assert baseline is not None
    assert baseline["source_report_id"] == "report-YOUTUBE_CMS"  # §5.5 parity w/ gross


def test_net_explanation_source_net_path_reconciles_with_approved_override():
    """Source-net + approved override: value and formula reconcile from emitted components."""
    entry = build_channel_month_revenue_explanation(
        facts=[
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                net_revenue_usd="900.00",
            )
        ],
        manual_overrides=[manual_override(status="APPROVED", adjustment_revenue_usd="125.50")],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=NET_REVENUE_METRIC,
    )
    # Persisted net = source-reported net + approved override (900.00 + 125.50).
    assert entry.value == Decimal("1025.50")
    assert entry.confidence == {"label": "HIGH", "score": "0.95"}  # B_RECONCILED
    # The formula must be expressed purely in terms of emitted component keys so the
    # snapshot reconciles to its own value once an approved override is present.
    assert entry.formula == (
        "net_revenue_usd = baseline_gross_revenue_usd "
        "+ approved_manual_override_total_usd "
        "- source_reported_deduction_usd"
    )
    by_key = {c["key"]: c for c in entry.components}
    baseline = by_key["baseline_gross_revenue_usd"]
    approved = by_key["approved_manual_override_total_usd"]
    src = by_key["source_reported_deduction_usd"]
    assert baseline["value"] == "1000"
    assert approved["value"] == "125.5"
    assert approved["count"] == 1
    assert src["value"] == "100"  # 1000 gross - 900 source-reported net
    assert src["source_kind"] == "YOUTUBE_CMS"
    # No-drift: the stated formula reconstructs the persisted value exactly.
    assert (
        Decimal(baseline["value"]) + Decimal(approved["value"]) - Decimal(src["value"])
        == entry.value
    )


def test_net_explanation_component_derived_path_with_full_provenance_and_sum_identity():
    """Component-derived path emits full provenance whose amounts sum to the totals."""
    components = [
        deduction_component(component_key="cd-1", component_kind="TAX", amount_usd="30.00")
    ]
    allocations = [
        allocation_line(
            component_key="acct-1",
            youtube_channel_id="channel-tv-a",
            adsense_account_id="pub-1",
            component_kind="DEDUCTION",
            source_system="adsense_management",
            basis_source_kind="ADSENSE",
            basis_share="0.5",
            allocated_amount_usd="100.00",
            net_applicable=True,
        )
    ]
    entry = build_channel_month_revenue_explanation(
        facts=[revenue_fact(source_kind="ADSENSE", gross_revenue_usd="1000.00")],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=NET_REVENUE_METRIC,
        deduction_components=components,
        account_allocations=allocations,
    )
    assert entry.value == Decimal("870.00")  # 1000 - 30 - 100
    assert entry.confidence == {"label": "MEDIUM", "score": "0.80"}  # D_ESTIMATED
    by_key = {c["key"]: c for c in entry.components}
    cd = by_key["channel_direct_deduction_usd"]
    assert cd["value"] == "30"
    assert cd["count"] == 1
    assert cd["components"] == [
        {
            "component_kind": "TAX",
            "source_system": "adsense_management",
            "component_key": "cd-1",
            "amount_usd": "30",
        }
    ]
    aa = by_key["account_allocated_deduction_usd"]
    assert aa["value"] == "100"
    assert aa["count"] == 1
    assert aa["allocations"] == [
        {
            "adsense_account_id": "pub-1",
            "component_kind": "DEDUCTION",
            "source_system": "adsense_management",
            "component_key": "acct-1",
            "basis_source_kind": "ADSENSE",
            "basis_share": "0.5",
            "allocated_amount_usd": "100",
        }
    ]
    # No-drift: provenance amounts sum to the component values.
    assert Decimal(cd["value"]) == sum(Decimal(x["amount_usd"]) for x in cd["components"])
    assert Decimal(aa["value"]) == sum(
        Decimal(x["allocated_amount_usd"]) for x in aa["allocations"]
    )


def test_net_explanation_indeterminate_net_raises():
    """An indeterminate (E_MISSING) net raises rather than fabricating a value."""
    with pytest.raises(NumberExplanationValidationError):
        build_channel_month_revenue_explanation(
            facts=[],  # NO_FACTS -> net None -> E_MISSING
            manual_overrides=[],
            month="2026-03",
            youtube_channel_id="channel-tv-a",
            metric=NET_REVENUE_METRIC,
        )


def test_gross_metric_unchanged_when_net_params_omitted():
    """The gross metric path is unchanged when the net-only params are omitted."""
    entry = build_channel_month_revenue_explanation(
        facts=[revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00")],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric="adjusted_gross_revenue_usd",
    )
    assert entry.metric == "adjusted_gross_revenue_usd"
    assert [c["key"] for c in entry.components][0] == "baseline_gross_revenue_usd"
