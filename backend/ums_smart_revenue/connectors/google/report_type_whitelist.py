"""YouTube Reporting report_type_id whitelist (spec §5.4).

These are the monetary/revenue-relevant report types the existing
B1 parsers know how to consume. Outside this set raises
UnsupportedReportTypeError at orchestrator time.
"""
from __future__ import annotations

SUPPORTED_REPORT_TYPES: frozenset[str] = frozenset(
    {
        "content_owner_estimated_revenue_a1",
        "content_owner_ad_revenue_raw_a1",
        # Add additional locked-at-ship report_type_ids here as the
        # parser grows; each new addition needs a parser-side change too.
    }
)


def is_supported(report_type_id: str) -> bool:
    return report_type_id in SUPPORTED_REPORT_TYPES
