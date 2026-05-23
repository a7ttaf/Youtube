"""Parsers that translate Google source-report payloads into ParsedSourceRow.

No OAuth, no live API client, no live download. Each parser takes a
pre-recorded payload (loaded by tests from
tests/connectors/_fixtures/) and emits an iterable of ParsedSourceRow.
"""

from ums_smart_revenue.connectors.google_source_parsers.base import (
    ParserError,
    SourceRowParser,
)
from ums_smart_revenue.connectors.google_source_parsers.source_row_keys import (
    build_source_row_key,
)

# The following re-exports are uncommented as each module ships in Phase 4
# (Tasks 4.6, 4.9, 4.12). Keep this file importable until each lands.
# from ums_smart_revenue.connectors.google_source_parsers.adsense_management import (
#     AdSenseManagementParser,
# )
# from ums_smart_revenue.connectors.google_source_parsers.youtube_analytics import (
#     YouTubeAnalyticsParser,
# )
# from ums_smart_revenue.connectors.google_source_parsers.youtube_reporting import (
#     YouTubeReportingParser,
# )

__all__ = [
    # "AdSenseManagementParser",
    "ParserError",
    "SourceRowParser",
    # "YouTubeAnalyticsParser",
    # "YouTubeReportingParser",
    "build_source_row_key",
]
