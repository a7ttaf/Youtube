from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ums_smart_revenue.finance.bank_reconciliation import (
    MonthBankReconciliationSummary,
)
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.net_revenue import MonthNetRevenueSummary
from ums_smart_revenue.finance.payment_matching import MonthlyPaymentMatchSummary
from ums_smart_revenue.finance.smart_alerts import MonthlySmartAlertSummary
from ums_smart_revenue.reports.exports import ExportJobEntry

EXECUTIVE_PDF_SECTION_NAMES = (
    "Cover",
    "Executive Summary",
    "Gross vs Net Revenue",
    "Deductions Explanation",
    "Company Ranking",
    "Channel Ranking",
    "Problem Summary",
    "Recommendations",
)

_SECTION_SOURCES = {
    "Cover": "export_job_metadata",
    "Executive Summary": "computed_finance_summary",
    "Gross vs Net Revenue": (
        "monthly_revenue_facts_manual_overrides_deduction_components_and_account_allocations"
    ),
    "Deductions Explanation": (
        "source_net_revenue_manual_overrides_deduction_components_and_account_allocations"
    ),
    "Company Ranking": "channel_registry_and_revenue_facts",
    "Channel Ranking": "monthly_revenue_facts",
    "Problem Summary": "smart_alerts_payment_and_bank_reconciliation",
    "Recommendations": "confidence_and_close_status",
}


class ExecutivePdfValidationError(ValueError):
    """Raised when there is a validation error in generating the executive PDF report."""


@dataclass(frozen=True)
class ExecutivePdfSection:
    """Executive PDF section metadata."""

    name: str
    source: str
    status: str
    sensitive: bool

    def to_api(self) -> dict[str, object]:
        """Convert the section into an API dictionary."""
        return {
            "name": self.name,
            "source": self.source,
            "status": self.status,
            "sensitive": self.sensitive,
        }


@dataclass(frozen=True)
class ExecutivePdfReport:
    """Represents the executive PDF report, aggregating job info, sections,
    and summary data for generation.
    """

    export_job: ExportJobEntry
    sections: tuple[ExecutivePdfSection, ...]
    net_revenue: MonthNetRevenueSummary
    payment_match: MonthlyPaymentMatchSummary
    bank_reconciliation: MonthBankReconciliationSummary
    smart_alerts: MonthlySmartAlertSummary

    def to_api(self) -> dict[str, object]:
        """Convert the ExecutivePdfReport instance into an API payload dictionary."""
        return {
            "export_id": self.export_job.id,
            "export_type": self.export_job.export_type,
            "artifact_type": "EXECUTIVE_FINANCE_PDF",
            "status": "READY_FOR_GENERATION",
            "month": self.export_job.month,
            "currency": self.export_job.currency,
            "scope_type": self.export_job.scope_type,
            "scope_id": self.export_job.scope_id,
            "month_lock_status": self.export_job.month_lock_status,
            "sections": [section.to_api() for section in self.sections],
            "executive_summary": _executive_summary(
                export_job=self.export_job,
                net_revenue=self.net_revenue,
                payment_match=self.payment_match,
                bank_reconciliation=self.bank_reconciliation,
                smart_alerts=self.smart_alerts,
            ),
        }


def build_executive_pdf_report(
    *,
    export_job: ExportJobEntry,
    net_revenue: MonthNetRevenueSummary,
    payment_match: MonthlyPaymentMatchSummary,
    bank_reconciliation: MonthBankReconciliationSummary,
    smart_alerts: MonthlySmartAlertSummary,
) -> ExecutivePdfReport:
    """Build and validate an ExecutivePdfReport object from given summaries and export job."""
    if export_job.export_type != "EXECUTIVE_PDF":
        raise ExecutivePdfValidationError(
            "executive PDF report only supports EXECUTIVE_PDF exports"
        )
    _validate_same_month(
        export_job=export_job,
        net_revenue=net_revenue,
        payment_match=payment_match,
        bank_reconciliation=bank_reconciliation,
        smart_alerts=smart_alerts,
    )
    if export_job.currency != "USD":
        raise ExecutivePdfValidationError("executive PDF report requires USD currency")
    if payment_match.currency != "USD" or bank_reconciliation.currency != "USD":
        raise ExecutivePdfValidationError(
            "executive PDF report requires USD source summaries"
        )

    return ExecutivePdfReport(
        export_job=export_job,
        sections=tuple(
            ExecutivePdfSection(
                name=name,
                source=_SECTION_SOURCES[name],
                status="READY",
                sensitive=True,
            )
            for name in EXECUTIVE_PDF_SECTION_NAMES
        ),
        net_revenue=net_revenue,
        payment_match=payment_match,
        bank_reconciliation=bank_reconciliation,
        smart_alerts=smart_alerts,
    )


def build_executive_pdf_bytes(report: ExecutivePdfReport) -> bytes:
    """Generate PDF bytes for the given ExecutivePdfReport using reportlab.
    Return the PDF binary.
    """
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        rightMargin=48,
        leftMargin=48,
        topMargin=48,
        bottomMargin=42,
        title="UMS Executive Finance Report",
        author="UMS Smart Revenue Control Center",
    )
    styles = _pdf_styles()
    story = [
        Paragraph("UMS Executive Finance Report", styles["Title"]),
        Paragraph(
            f"Month: {report.export_job.month} | Scope: "
            f"{report.export_job.scope_type}"
            + (
                f" ({report.export_job.scope_id})"
                if report.export_job.scope_id is not None
                else ""
            ),
            styles["Small"],
        ),
        Spacer(1, 16),
        Paragraph("Executive Summary", styles["Heading2"]),
        _summary_table(report),
        Spacer(1, 14),
        Paragraph("Gross vs Net Revenue", styles["Heading2"]),
        _gross_net_table(report),
        Spacer(1, 14),
        Paragraph("Deductions Explanation", styles["Heading2"]),
        Paragraph(
            "Deductions are calculated from SQL monthly revenue facts plus approved "
            "manual overrides. The channel-direct portion is sourced from "
            "channel-level deduction components, and the account-allocated portion "
            "from account-level deduction allocations. Pending overrides are shown "
            "as risk, not revenue.",
            styles["BodyText"],
        ),
        Spacer(1, 10),
        Paragraph("Company Ranking", styles["Heading2"]),
        _ranking_table(report, ranking_label="Company scope"),
        Spacer(1, 14),
        Paragraph("Channel Ranking", styles["Heading2"]),
        _channel_ranking_table(report),
        Spacer(1, 14),
        Paragraph("Problem Summary", styles["Heading2"]),
        _problem_summary(report, styles),
        Spacer(1, 14),
        Paragraph("Recommendations", styles["Heading2"]),
        _recommendations(report, styles),
    ]
    document.build(story)
    return buffer.getvalue()


def _validate_same_month(
    *,
    export_job: ExportJobEntry,
    net_revenue: MonthNetRevenueSummary,
    payment_match: MonthlyPaymentMatchSummary,
    bank_reconciliation: MonthBankReconciliationSummary,
    smart_alerts: MonthlySmartAlertSummary,
) -> None:
    """Ensure all provided summaries use the export month."""
    months = {
        export_job.month,
        net_revenue.month,
        payment_match.month,
        bank_reconciliation.month,
        smart_alerts.month,
    }
    if len(months) != 1:
        raise ExecutivePdfValidationError(
            "executive PDF report source summaries must use the export month"
        )


def _executive_summary(
    *,
    export_job: ExportJobEntry,
    net_revenue: MonthNetRevenueSummary,
    payment_match: MonthlyPaymentMatchSummary,
    bank_reconciliation: MonthBankReconciliationSummary,
    smart_alerts: MonthlySmartAlertSummary,
) -> dict[str, object]:
    """Generate a summary dictionary containing key metrics and statuses
    for the executive PDF report.
    """
    return {
        "month": export_job.month,
        "scope_type": export_job.scope_type,
        "scope_id": export_job.scope_id,
        "currency": export_job.currency,
        "month_lock_status": export_job.month_lock_status,
        "net_revenue_status": net_revenue.status,
        "payment_match_status": payment_match.status,
        "bank_reconciliation_status": bank_reconciliation.status,
        "smart_alert_status": smart_alerts.status,
        "smart_alert_count": len(smart_alerts.alerts),
        "total_adjusted_gross_revenue_usd": _decimal_to_api(
            net_revenue.total_adjusted_gross_revenue_usd
        ),
        "total_net_revenue_usd": _decimal_to_api(net_revenue.total_net_revenue_usd),
        "total_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_deduction_amount_usd
        ),
        # Deduction-split export contract: the channel-direct and
        # account-allocated totals SUPPLEMENT (never replace)
        # total_deduction_amount_usd. See the _gross_net_table contract block.
        "total_channel_direct_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_channel_direct_deduction_amount_usd
        ),
        "total_account_allocated_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_account_allocated_deduction_amount_usd
        ),
        "payment_gap_usd": _decimal_to_api(payment_match.payment_gap_usd),
        "bank_gap_usd": _decimal_to_api(bank_reconciliation.bank_gap_usd),
        "channel_count": net_revenue.channel_count,
        "calculated_channel_count": net_revenue.calculated_channel_count,
    }


def _summary_table(report: ExecutivePdfReport) -> Table:
    """Create a summary table with key executive summary metrics for display in the PDF."""
    summary = report.to_api()["executive_summary"]
    return _key_value_table(
        {
            "Month": summary["month"],
            "Total Net Revenue USD": summary["total_net_revenue_usd"],
            "Payment Match Status": summary["payment_match_status"],
            "Bank Reconciliation Status": summary["bank_reconciliation_status"],
            "Smart Alert Count": summary["smart_alert_count"],
            "Month Lock Status": summary["month_lock_status"],
        }
    )


# ============================================================================
# Purpose: Render the executive gross-vs-net table. Surfaces all three month
#   deduction totals as distinct rows — total, channel-direct, and
#   account-allocated — where the channel-direct + account-allocated split
#   SUPPLEMENTS (does not replace) the overall total deduction figure.
# Database/ORM: None (reads the already-computed MonthNetRevenueSummary).
# Standards: Typed serialization via _decimal_to_api; a None split total
#   renders as a blank cell, never a fabricated "0".
# Blast Radius: Finance export presentation only — no source-of-truth, auth,
#   audit, or Neo4j impact. Must stay numerically consistent with the XLSX
#   workbook / PPTX deduction-breakdown surfaces and the net-revenue API.
# Connections:
#   - File: backend/ums_smart_revenue/finance/net_revenue.py -> Source of the
#     total_*_deduction_amount_usd split aggregates rendered here.
#   - File: backend/ums_smart_revenue/finance/decimal_formatting.py -> Shared
#     decimal_to_api serialization contract (None -> blank).
# ============================================================================
def _gross_net_table(report: ExecutivePdfReport) -> Table:
    """Generate a table of gross and net revenue figures along with deductions and gaps."""
    return _key_value_table(
        {
            "Adjusted Gross Revenue USD": _decimal_to_api(
                report.net_revenue.total_adjusted_gross_revenue_usd
            ),
            "Total Net Revenue USD": _decimal_to_api(
                report.net_revenue.total_net_revenue_usd
            ),
            "Total Deduction Amount USD": _decimal_to_api(
                report.net_revenue.total_deduction_amount_usd
            ),
            "Channel-Direct Deduction USD": _decimal_to_api(
                report.net_revenue.total_channel_direct_deduction_amount_usd
            ),
            "Account-Allocated Deduction USD": _decimal_to_api(
                report.net_revenue.total_account_allocated_deduction_amount_usd
            ),
            "Payment Gap USD": _decimal_to_api(report.payment_match.payment_gap_usd),
            "Bank Gap USD": _decimal_to_api(report.bank_reconciliation.bank_gap_usd),
        }
    )


def _ranking_table(report: ExecutivePdfReport, *, ranking_label: str) -> Table:
    """
    Build a table displaying company ranking information based on net
    revenue, labeled by the given ranking label.
    """
    return _styled_table(
        [["Ranking", "Channels", "Total Net Revenue USD"]]
        + [
            [
                ranking_label,
                str(report.net_revenue.channel_count),
                _decimal_to_api(report.net_revenue.total_net_revenue_usd),
            ]
        ]
    )


def _channel_ranking_table(report: ExecutivePdfReport) -> Table:
    """Generate a styled table of top channels by net revenue."""
    rows = [
        ["Channel", "Net Revenue USD", "Deduction USD", "Confidence"],
        *[
            [
                channel.youtube_channel_id,
                _decimal_to_api(channel.net_revenue_usd),
                _decimal_to_api(channel.deduction_amount_usd),
                channel.confidence,
            ]
            for channel in sorted(
                report.net_revenue.channels,
                key=lambda item: (
                    item.net_revenue_usd
                    if item.net_revenue_usd is not None
                    else Decimal("-Infinity")
                ),
                reverse=True,
            )[:10]
        ],
    ]
    return _styled_table(rows)


def _problem_summary(
    report: ExecutivePdfReport, styles: dict[str, ParagraphStyle]
) -> Paragraph:
    """Create a paragraph summarizing detected smart alerts or indicate that none were generated."""
    if not report.smart_alerts.alerts:
        return Paragraph(
            "No smart alerts were generated for this export scope.", styles["BodyText"]
        )
    messages = "; ".join(alert.message for alert in report.smart_alerts.alerts[:5])
    return Paragraph(messages, styles["BodyText"])


def _recommendations(
    report: ExecutivePdfReport, styles: dict[str, ParagraphStyle]
) -> Paragraph:
    """Build recommendations from finance status summaries."""
    if report.payment_match.status != "PAYMENT_MATCHED":
        return Paragraph(
            "Review AdSense payment matching before finance sign-off.",
            styles["BodyText"],
        )
    if report.bank_reconciliation.status != "BANK_CONFIRMED":
        return Paragraph(
            "Confirm bank reconciliation receipt rows before month close.",
            styles["BodyText"],
        )
    if report.smart_alerts.alerts:
        return Paragraph(
            "Review smart alerts and document resolution before distributing.",
            styles["BodyText"],
        )
    return Paragraph(
        "No blocking finance issues detected for this scope.", styles["BodyText"]
    )


def _key_value_table(values: dict[str, object]) -> Table:
    """Construct a key-value styled table from a dictionary of metrics and their values."""
    rows = [["Metric", "Value"], *[[key, value] for key, value in values.items()]]
    return _styled_table(rows)


def _styled_table(rows: list[list[object]]) -> Table:
    """Apply consistent styling to a table, including fonts, colors,
    padding, and grid layout, for use in the PDF.
    """
    table = Table(rows, hAlign="LEFT", repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2933")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D9DEE6")),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#F7F9FC")],
                ),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _pdf_styles() -> dict[str, ParagraphStyle]:
    """Define paragraph styles for PDF elements."""
    styles = getSampleStyleSheet()
    styles["Title"].fontName = "Helvetica"
    styles["Title"].fontSize = 20
    styles["Title"].leading = 24
    styles["Heading2"].fontName = "Helvetica"
    styles["Heading2"].fontSize = 12
    styles["Heading2"].leading = 15
    styles["BodyText"].fontName = "Helvetica"
    styles["BodyText"].fontSize = 9
    styles["BodyText"].leading = 12
    styles.add(
        ParagraphStyle(
            name="Small",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#4A5563"),
        )
    )
    return styles
