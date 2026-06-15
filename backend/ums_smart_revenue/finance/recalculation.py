from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

ALLOCATION_METHODS = frozenset(
    {
        "gross_revenue_proportional",
        "post_tax_revenue_proportional",
        "company_level",
        "manual",
        "no_allocation",
    }
)
NET_REVENUE_REQUIRED_METHODS = frozenset({"post_tax_revenue_proportional"})
COMPANY_MAPPING_REQUIRED_METHODS = frozenset({"company_level"})
# no_allocation withholds every component without reading facts, so a factless
# month must not block its preview (the commit engine ignores facts entirely).
# manual is NOT exempt: the recalculation route rejects dry_run=false/manual
# with 422 (see _validate_recalculation_write_request), so a manual dry-run
# that returns READY_FOR_REVIEW with no facts would be a false positive with
# no actionable commit path via this endpoint.
SOURCE_FACTS_OPTIONAL_METHODS = frozenset({"no_allocation"})


class RevenueRecalculationValidationError(ValueError):
    """Raised when recalculation input fails validation."""


@dataclass(frozen=True)
class RecalculationIssue:
    """A blocking or informational issue found during recalculation preview."""

    issue_type: str
    severity: str
    message: str

    def to_api(self) -> dict[str, str]:
        """Serialize to API dict."""
        return {
            "issue_type": self.issue_type,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True)
class RecalculationSourceSummary:
    """Counts of revenue sources and overrides for one month's recalculation."""

    revenue_fact_count: int
    source_channel_count: int
    manual_override_count: int
    pending_manual_override_count: int
    net_revenue_source_count: int
    missing_net_revenue_source_count: int

    def to_api(self) -> dict[str, int]:
        """Serialize to API dict."""
        return {
            "revenue_fact_count": self.revenue_fact_count,
            "source_channel_count": self.source_channel_count,
            "manual_override_count": self.manual_override_count,
            "pending_manual_override_count": self.pending_manual_override_count,
            "net_revenue_source_count": self.net_revenue_source_count,
            "missing_net_revenue_source_count": self.missing_net_revenue_source_count,
        }


@dataclass(frozen=True)
class RevenueRecalculationPreview:
    """Immutable result of a recalculation dry run."""

    month: str
    allocation_method: str
    scope_type: str
    scope_id: str | None
    currency: str
    dry_run: bool
    status: str
    write_status: str
    source_summary: RecalculationSourceSummary
    steps: tuple[str, ...]
    blocking_issues: tuple[RecalculationIssue, ...]

    def to_api(self) -> dict[str, object]:
        """Serialize to API response dict."""
        return {
            "month": self.month,
            "allocation_method": self.allocation_method,
            "scope": {
                "scope_type": self.scope_type,
                "scope_id": self.scope_id,
            },
            "currency": self.currency,
            "dry_run": self.dry_run,
            "status": self.status,
            "write_status": self.write_status,
            "source_summary": self.source_summary.to_api(),
            "steps": list(self.steps),
            "blocking_issues": [issue.to_api() for issue in self.blocking_issues],
        }


def build_recalculation_preview(
    *,
    month: str,
    allocation_method: str,
    scope_type: str,
    scope_id: str | None,
    currency: str,
    dry_run: bool,
    facts: Iterable[RevenueFactEntry],
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    channel_company: Mapping[str, str] | None = None,
    verified_channel_ids: frozenset[str] | None = None,
) -> RevenueRecalculationPreview:
    """Build a dry-run recalculation preview for one month + scope.

    channel_company (channel id -> company org-unit id) is only consulted for
    company_level: a scoped fact channel missing from the mapping mirrors the
    commit engine's fail-closed COMPANY_UNMAPPED path as a blocking issue. A
    None mapping counts every channel as unmapped (fail-closed), so a caller
    that forgets the org index cannot see READY.

    verified_channel_ids is an optional superset of channels to check for
    company mapping under company_level. When provided, the preview checks the
    union of fact-derived channels AND verified_channel_ids, so verified
    channels that have no revenue facts this month are also checked against
    channel_company. This prevents a false READY_FOR_REVIEW when a verified
    account channel lacks a company mapping but has no fact row.

    The builder NEVER writes; it is the pre-flight gate for both dry-run preview
    and the committed write path. dry_run is echoed onto the returned preview so
    the route can serialize it faithfully; the route itself drives the commit
    (and overrides write_status) after this gate passes.

    Raises:
        RevenueRecalculationValidationError: If allocation_method or currency is
            invalid.
    """
    normalized_method = normalize_allocation_method(allocation_method)
    normalized_currency = normalize_recalculation_currency(currency)

    fact_list = [fact for fact in facts if fact.month == month]
    override_list = [override for override in manual_overrides if override.month == month]
    source_channel_ids = {fact.youtube_channel_id for fact in fact_list}
    source_keys = {(fact.youtube_channel_id, fact.source_kind) for fact in fact_list}
    # A (channel, source_kind) key is missing-net if ANY fact in that group has
    # null net (mirrors the commit engine's fail-closed null-net omission), so
    # the dry-run cannot report READY while commit would go UNALLOCATED.
    null_net_keys = {
        (fact.youtube_channel_id, fact.source_kind)
        for fact in fact_list
        if fact.net_revenue_usd is None
    }
    net_revenue_source_keys = source_keys - null_net_keys
    missing_net_revenue_count = len(null_net_keys)
    source_summary = RecalculationSourceSummary(
        revenue_fact_count=len(fact_list),
        source_channel_count=len(source_channel_ids),
        manual_override_count=len(override_list),
        pending_manual_override_count=sum(
            1 for override in override_list if override.status == "PENDING"
        ),
        net_revenue_source_count=len(net_revenue_source_keys),
        missing_net_revenue_source_count=missing_net_revenue_count,
    )
    unmapped_company_channel_count = 0
    if normalized_method in COMPANY_MAPPING_REQUIRED_METHODS:
        mapping = channel_company or {}
        # Union fact-derived channels with any extra verified channels supplied
        # by the caller (e.g. all channels in scope from the org index). This
        # catches verified channels that have no fact rows for the month but
        # would still be checked by the commit engine's COMPANY_UNMAPPED guard.
        channels_to_check = source_channel_ids | (verified_channel_ids or frozenset())
        unmapped_company_channel_count = sum(
            1 for channel_id in channels_to_check if channel_id not in mapping
        )
    blocking_issues = tuple(
        _build_blocking_issues(
            month=month,
            allocation_method=normalized_method,
            source_channel_count=len(source_channel_ids),
            missing_net_revenue_source_count=missing_net_revenue_count,
            unmapped_company_channel_count=unmapped_company_channel_count,
        )
    )
    return RevenueRecalculationPreview(
        month=month,
        allocation_method=normalized_method,
        scope_type=scope_type,
        scope_id=scope_id,
        currency=normalized_currency,
        dry_run=dry_run,
        status="BLOCKED" if blocking_issues else "READY_FOR_REVIEW",
        write_status="NO_WRITES_PERFORMED",
        source_summary=source_summary,
        steps=(
            "validate_permissions",
            "validate_allocation_method",
            "load_scoped_revenue_sources",
            "load_manual_overrides",
            "build_preview_without_financial_writes",
        ),
        blocking_issues=blocking_issues,
    )


def normalize_allocation_method(value: str) -> str:
    """Validate and normalize the allocation method string.

    Raises:
        RevenueRecalculationValidationError: If the method is not in ALLOCATION_METHODS.
    """
    normalized = value.strip().lower()
    if normalized not in ALLOCATION_METHODS:
        allowed = ", ".join(sorted(ALLOCATION_METHODS))
        raise RevenueRecalculationValidationError(
            f"Unknown allocation_method: {value}. Allowed methods: {allowed}"
        )
    return normalized


def normalize_recalculation_currency(value: str) -> str:
    """Validate and normalize the currency to USD.

    Raises:
        RevenueRecalculationValidationError: If the currency is not USD.
    """
    normalized = value.strip().upper()
    if normalized != "USD":
        raise RevenueRecalculationValidationError(
            "currency must be USD until exchange-rate support is implemented"
        )
    return normalized


def _build_blocking_issues(
    *,
    month: str,
    allocation_method: str,
    source_channel_count: int,
    missing_net_revenue_source_count: int,
    unmapped_company_channel_count: int = 0,
) -> list[RecalculationIssue]:
    """Collect blocking RecalculationIssues for the given method and source counts."""
    issues: list[RecalculationIssue] = []
    if source_channel_count == 0 and allocation_method not in SOURCE_FACTS_OPTIONAL_METHODS:
        issues.append(
            RecalculationIssue(
                issue_type="NO_REVENUE_FACTS",
                severity="HIGH",
                message=f"No scoped revenue facts are available for {month}.",
            )
        )
    if allocation_method in NET_REVENUE_REQUIRED_METHODS and missing_net_revenue_source_count:
        issues.append(
            RecalculationIssue(
                issue_type="NET_REVENUE_SOURCE_MISSING",
                severity="HIGH",
                message=(
                    f"{missing_net_revenue_source_count} scoped "
                    f"(channel, source kind) source(s) in {month} have no net "
                    f"revenue for {allocation_method}."
                ),
            )
        )
    # Non-zero only for COMPANY_MAPPING_REQUIRED_METHODS (the caller computes
    # the count method-gated); mirrors the commit engine's COMPANY_UNMAPPED.
    if unmapped_company_channel_count:
        issues.append(
            RecalculationIssue(
                issue_type="COMPANY_MAPPING_MISSING",
                severity="HIGH",
                message=(
                    f"{unmapped_company_channel_count} scoped source channel(s) "
                    f"in {month} have no company mapping for {allocation_method}."
                ),
            )
        )
    return issues
