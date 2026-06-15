from dataclasses import dataclass
from enum import StrEnum

from ums_smart_revenue.auth.scopes import OrgAccessIndex
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry
from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry


class ChannelIssueType(StrEnum):
    MISSING_COMPANY = "MISSING_COMPANY"
    MISSING_SECTOR = "MISSING_SECTOR"
    OUTSIDE_CMS_REVENUE_REQUIRED = "OUTSIDE_CMS_REVENUE_REQUIRED"
    REVENUE_REQUIRED_NO_GROUP = "REVENUE_REQUIRED_NO_GROUP"


@dataclass(frozen=True)
class ChannelRegistryIssue:
    youtube_channel_id: str
    channel_name: str
    issue_type: ChannelIssueType
    severity: str
    message: str
    recommended_action: str
    primary_company_id: str | None = None

    def to_api(self) -> dict[str, object]:
        return {
            "youtube_channel_id": self.youtube_channel_id,
            "channel_name": self.channel_name,
            "primary_company_id": self.primary_company_id,
            "issue_type": self.issue_type.value,
            "severity": self.severity,
            "message": self.message,
            "recommended_action": self.recommended_action,
        }


def build_channel_registry_issues(
    *,
    channels: list[ChannelRegistryEntry],
    groups: list[ChannelGroupEntry],
    org_index: OrgAccessIndex,
) -> list[ChannelRegistryIssue]:
    grouped_channel_ids = {
        channel_id for group in groups if group.active for channel_id in group.channel_ids
    }
    issues: list[ChannelRegistryIssue] = []
    for channel in sorted(channels, key=lambda item: item.youtube_channel_id):
        issues.extend(
            _issues_for_channel(
                channel=channel,
                grouped_channel_ids=grouped_channel_ids,
                org_index=org_index,
            )
        )
    return issues


def summarize_channel_registry_issues(
    issues: list[ChannelRegistryIssue],
) -> dict[str, object]:
    issue_type_counts: dict[str, int] = {}
    channel_ids: set[str] = set()
    for issue in issues:
        channel_ids.add(issue.youtube_channel_id)
        issue_type_counts[issue.issue_type.value] = (
            issue_type_counts.get(issue.issue_type.value, 0) + 1
        )
    return {
        "total_issue_count": len(issues),
        "channel_count": len(channel_ids),
        "issue_type_counts": dict(sorted(issue_type_counts.items())),
    }


def _issues_for_channel(
    *,
    channel: ChannelRegistryEntry,
    grouped_channel_ids: set[str],
    org_index: OrgAccessIndex,
) -> list[ChannelRegistryIssue]:
    issues: list[ChannelRegistryIssue] = []
    if channel.primary_company_id is None:
        issues.append(
            _build_issue(
                channel=channel,
                issue_type=ChannelIssueType.MISSING_COMPANY,
                severity="high",
                message="Channel has no primary company mapping.",
                recommended_action="Assign the channel to the correct company.",
            )
        )
    elif channel.primary_company_id not in org_index.company_sector:
        issues.append(
            _build_issue(
                channel=channel,
                issue_type=ChannelIssueType.MISSING_SECTOR,
                severity="high",
                message="Channel primary company is not mapped to an active sector.",
                recommended_action="Map the company to the correct sector.",
            )
        )

    if channel.cms_status == "OUTSIDE_CMS" and channel.revenue_required:
        issues.append(
            _build_issue(
                channel=channel,
                issue_type=ChannelIssueType.OUTSIDE_CMS_REVENUE_REQUIRED,
                severity="medium",
                message="Revenue-required channel is outside CMS.",
                recommended_action=("Link the channel to CMS or maintain official manual import."),
            )
        )

    if channel.revenue_required and channel.youtube_channel_id not in grouped_channel_ids:
        issues.append(
            _build_issue(
                channel=channel,
                issue_type=ChannelIssueType.REVENUE_REQUIRED_NO_GROUP,
                severity="medium",
                message="Revenue-required channel is not in any active group.",
                recommended_action=("Add the channel to an operational or finance group."),
            )
        )

    return issues


def _build_issue(
    *,
    channel: ChannelRegistryEntry,
    issue_type: ChannelIssueType,
    severity: str,
    message: str,
    recommended_action: str,
) -> ChannelRegistryIssue:
    return ChannelRegistryIssue(
        youtube_channel_id=channel.youtube_channel_id,
        channel_name=channel.channel_name,
        primary_company_id=channel.primary_company_id,
        issue_type=issue_type,
        severity=severity,
        message=message,
        recommended_action=recommended_action,
    )
