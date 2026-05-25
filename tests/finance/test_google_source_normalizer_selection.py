"""Pure-function tests for select_canonical_row + module constants.

No DB, no session. Verifies the rule wiring per source_system and the
frozen-mapping contract.
"""

import pytest

from ums_smart_revenue.finance.google_source_normalizer import (
    CANONICAL_METRIC_RULE,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactSourceKind


def test_source_system_to_source_kind_mapping_covers_three_supported_systems():
    assert dict(SOURCE_SYSTEM_TO_SOURCE_KIND) == {
        "youtube_reporting": RevenueFactSourceKind.YOUTUBE_CMS,
        "youtube_analytics": RevenueFactSourceKind.YOUTUBE_ANALYTICS,
        "adsense_management": RevenueFactSourceKind.ADSENSE,
    }


def test_canonical_metric_rule_mapping_is_frozen():
    with pytest.raises(TypeError):
        CANONICAL_METRIC_RULE["youtube_reporting"] = ("foo",)  # type: ignore[index]
