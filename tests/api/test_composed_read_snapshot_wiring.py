# ============================================================================
# Purpose: Wiring pins proving EVERY composed finance read begins its
#   composed-read snapshot — payment-match, bank-reconciliation, smart-alerts,
#   gap-explanation, net-revenue, rankings, reconciliation-issues, the
#   channel-month summary, the reconciliation-preview (its single-select
#   exemption was disproven in review: the repository read is guard-then-
#   select, two statements), and the dry-run recalculation preview each call
#   begin_composed_read_snapshot exactly once before reading. The
#   recalculation WRITE branch and the explain POST stay READ COMMITTED by
#   the recorded write-path ruling. The snapshot ruling is platform-wide; a
#   route that stops calling the helper silently reverts to torn READ
#   COMMITTED composition on Postgres, so the presence of the call is pinned
#   per endpoint.
# Database/ORM: SQLite-tier TestClient over the real app factory; empty
#   finance month (the builders tolerate empty sources), so only the schema,
#   the audit actor row, and one active channel row (the channel-scoped
#   summary read 404s on unknown channels) are seeded.
# Standards: The helper is replaced with a counting fake (create=True so the
#   pin fails on a missing import with a countable zero, not an AttributeError)
#   — behavior of the real helper is pinned in tests/db/test_read_snapshot.py
#   and tests/api/test_composed_read_snapshot_postgres.py, not here.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> the eight routes under
#     test.
#   - File: backend/ums_smart_revenue/db/read_snapshot.py -> the helper whose
#     call is being counted.
# ============================================================================
"""Per-endpoint wiring pins for the composed-read snapshot call."""

from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.db.security_models import SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-00000000c401")
CHANNEL_ID = "channel-snapshot-wiring"

COMPOSED_READ_PATHS = [
    "/revenue/months/2026-03/payment-match",
    "/revenue/months/2026-03/bank-reconciliation",
    "/revenue/months/2026-03/smart-alerts",
    "/revenue/months/2026-03/gap-explanation",
    "/revenue/months/2026-03/net-revenue",
    "/revenue/months/2026-03/rankings",
    "/revenue/months/2026-03/reconciliation-issues",
    f"/revenue/channels/{CHANNEL_ID}/months/2026-03/summary",
    f"/revenue/channels/{CHANNEL_ID}/months/2026-03/reconciliation-preview",
]


def auth_headers() -> dict[str, str]:
    """Build finance-viewer trusted-gateway headers for the composed reads."""
    return {
        "x-user-id": str(USER_ID),
        "x-user-email": "snapshot-wiring@example.com",
        "x-role": "finance_viewer",
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_client(tmp_path: Path) -> TestClient:
    """Create the app over a seeded throwaway SQLite database."""
    database_url = f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"
    engine = create_engine(database_url)
    try:
        OrgBase.metadata.create_all(engine)
        SecurityBase.metadata.create_all(engine)
        FinanceBase.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all(
                [
                    UserORM(
                        id=USER_ID,
                        email="snapshot-wiring@example.com",
                        display_name="Snapshot Wiring User",
                    ),
                    YouTubeChannelORM(
                        id=uuid4(),
                        youtube_channel_id=CHANNEL_ID,
                        channel_name="Snapshot Wiring Channel",
                        active=True,
                    ),
                ]
            )
            session.commit()
    finally:
        engine.dispose()
    return TestClient(create_app(database_url=database_url, authz_source="headers"))


@pytest.mark.parametrize("path", COMPOSED_READ_PATHS)
def test_composed_read_begins_exactly_one_snapshot(tmp_path: Path, path: str) -> None:
    """Each composed finance read must begin its snapshot exactly once."""
    client = build_client(tmp_path)
    calls: list[object] = []
    with patch(
        "ums_smart_revenue.api.revenue.begin_composed_read_snapshot",
        create=True,
        side_effect=calls.append,
    ):
        response = client.get(path, headers=auth_headers())

    assert response.status_code == 200
    assert len(calls) == 1


def test_recalculation_dry_run_begins_exactly_one_snapshot(tmp_path: Path) -> None:
    """The dry-run recalculation preview is a composed read: one snapshot.

    The write branch (dry_run=false) deliberately stays READ COMMITTED (the
    recorded write-path ruling), so only the dry run is pinned here.
    """
    client = build_client(tmp_path)
    calls: list[object] = []
    with patch(
        "ums_smart_revenue.api.revenue.begin_composed_read_snapshot",
        create=True,
        side_effect=calls.append,
    ):
        response = client.post(
            "/revenue/recalculate",
            headers={**auth_headers(), "x-role": "finance_admin"},
            json={
                "month": "2026-03",
                "allocation_method": "gross_revenue_proportional",
                "scope_type": "global",
                "dry_run": True,
                "reason": "snapshot wiring pin",
            },
        )

    assert response.status_code == 200
    assert len(calls) == 1
