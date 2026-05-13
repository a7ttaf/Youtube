from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from pypdf import PdfReader

from ums_smart_revenue.finance.bank_reconciliation import (
    MonthBankReconciliationSummary,
)
from ums_smart_revenue.finance.net_revenue import (
    ChannelNetRevenueSummary,
    MonthNetRevenueSummary,
)
from ums_smart_revenue.finance.payment_matching import MonthlyPaymentMatchSummary
from ums_smart_revenue.finance.smart_alerts import MonthlySmartAlertSummary
from ums_smart_revenue.reports.executive_pdf import (
    EXECUTIVE_PDF_SECTION_NAMES,
    ExecutivePdfValidationError,
    build_executive_pdf_bytes,
    build_executive_pdf_report,
)
from ums_smart_revenue.reports.exports import ExportJobEntry


def test_executive_pdf_report_builds_section_manifest_from_source_summaries():
    report = build_executive_pdf_report(
        export_job=_export_job(export_type="EXECUTIVE_PDF"),
        net_revenue=_net_revenue_summary(),
        payment_match=_payment_match_summary(status="PAYMENT_MATCHED"),
        bank_reconciliation=_bank_summary(status="BANK_CONFIRMED"),
        smart_alerts=_smart_alert_summary(alert_count=0),
    )

    payload = report.to_api()

    assert payload["artifact_type"] == "EXECUTIVE_FINANCE_PDF"
    assert payload["status"] == "READY_FOR_GENERATION"
    assert [section["name"] for section in payload["sections"]] == list(
        EXECUTIVE_PDF_SECTION_NAMES
    )
    assert payload["executive_summary"]["total_net_revenue_usd"] == "930"
    assert payload["executive_summary"]["payment_match_status"] == "PAYMENT_MATCHED"
    assert payload["executive_summary"]["bank_reconciliation_status"] == (
        "BANK_CONFIRMED"
    )


def test_executive_pdf_rejects_non_pdf_export_type():
    with pytest.raises(ExecutivePdfValidationError) as exc_info:
        build_executive_pdf_report(
            export_job=_export_job(export_type="FINANCE_EXCEL"),
            net_revenue=_net_revenue_summary(),
            payment_match=_payment_match_summary(status="PAYMENT_MATCHED"),
            bank_reconciliation=_bank_summary(status="BANK_CONFIRMED"),
            smart_alerts=_smart_alert_summary(alert_count=0),
        )

    assert str(exc_info.value) == (
        "executive PDF report only supports EXECUTIVE_PDF exports"
    )


def test_executive_pdf_bytes_contain_expected_management_summary():
    report = build_executive_pdf_report(
        export_job=_export_job(export_type="EXECUTIVE_PDF"),
        net_revenue=_net_revenue_summary(),
        payment_match=_payment_match_summary(status="PAYMENT_MATCHED"),
        bank_reconciliation=_bank_summary(status="BANK_CONFIRMED"),
        smart_alerts=_smart_alert_summary(alert_count=0),
    )

    pdf_bytes = build_executive_pdf_bytes(report)
    text = _extract_pdf_text(pdf_bytes)

    assert pdf_bytes.startswith(b"%PDF-")
    assert "UMS Executive Finance Report" in text
    assert "2026-03" in text
    assert "Total Net Revenue USD" in text
    assert "930" in text
    assert "PAYMENT_MATCHED" in text
    assert "BANK_CONFIRMED" in text


def _extract_pdf_text(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _export_job(*, export_type: str) -> ExportJobEntry:
    return ExportJobEntry(
        id=str(uuid4()),
        export_type=export_type,
        scope_type="company",
        scope_id="company-a",
        month="2026-03",
        currency="USD",
        requested_by=str(uuid4()),
        status="QUEUED",
        file_url=None,
        month_lock_status="LOCKED",
        include_confidence_notes=True,
        include_manual_override_notes=True,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        completed_at=None,
    )


def _net_revenue_summary() -> MonthNetRevenueSummary:
    channel = ChannelNetRevenueSummary(
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        status="CALCULATED",
        primary_source_kind="YOUTUBE_CMS",
        baseline_gross_revenue_usd=Decimal("1000.00"),
        baseline_net_revenue_usd=Decimal("880.00"),
        approved_manual_override_total_usd=Decimal("50.00"),
        adjusted_gross_revenue_usd=Decimal("1050.00"),
        net_revenue_usd=Decimal("930.00"),
        deduction_amount_usd=Decimal("120.00"),
        deduction_percentage=Decimal("11.4286"),
        confidence="B_RECONCILED",
        approved_manual_override_count=1,
        pending_manual_override_count=0,
        issues=[],
    )
    return MonthNetRevenueSummary(
        month="2026-03",
        status="CALCULATED",
        channel_count=1,
        calculated_channel_count=1,
        missing_net_source_count=0,
        pending_manual_override_count=0,
        total_adjusted_gross_revenue_usd=Decimal("1050.00"),
        total_net_revenue_usd=Decimal("930.00"),
        total_deduction_amount_usd=Decimal("120.00"),
        channels=[channel],
    )


def _payment_match_summary(*, status: str) -> MonthlyPaymentMatchSummary:
    return MonthlyPaymentMatchSummary(
        month="2026-03",
        currency="USD",
        status=status,
        youtube_revenue_total_usd=Decimal("930.00"),
        adsense_paid_amount=Decimal("930.00"),
        payment_gap_usd=Decimal("0.00"),
        youtube_source_channel_count=1,
        missing_youtube_source_channel_count=0,
        payment_count=1,
        paid_payment_count=1,
        non_paid_payment_count=0,
        unsupported_payment_currency_count=0,
        tolerance_usd=Decimal("0.01"),
        issues=[],
    )


def _bank_summary(*, status: str) -> MonthBankReconciliationSummary:
    return MonthBankReconciliationSummary(
        month="2026-03",
        currency="USD",
        status=status,
        adsense_paid_amount_usd=Decimal("930.00"),
        bank_received_amount_usd=Decimal("930.00"),
        bank_gap_usd=Decimal("0.00"),
        transfer_fee_usd=Decimal("0.00"),
        fx_difference_usd=Decimal("0.00"),
        payment_count=1,
        paid_payment_count=1,
        non_paid_payment_count=0,
        unsupported_payment_currency_count=0,
        entry_count=1,
        tolerance_usd=Decimal("0.01"),
        issues=[],
        entries=[],
    )


def _smart_alert_summary(*, alert_count: int) -> MonthlySmartAlertSummary:
    assert alert_count == 0
    return MonthlySmartAlertSummary(
        month="2026-03",
        status="CLEAR",
        highest_severity=None,
        alerts=[],
    )
