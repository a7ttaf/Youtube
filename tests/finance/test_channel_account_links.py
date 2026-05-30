"""Channel↔account map: repository + helpers (SQLite unit tier).

NOTE — imports accrete across Tasks 4–10 in this file. Add each symbol in the
task that first uses it so every commit stays ruff-clean (no unused imports):
  Task 5 → `import pytest`; `from sqlalchemy.orm import Session`;
           add `SqlAlchemyChannelAccountLinkRepository, _account_owner_lock_key,
           _ranges_overlap` to the channel_account_links import.
  Task 6 → add `ChannelAccountLinkValidationError`.
  Task 7 → add `ChannelAccountLinkConflictError, ChannelAccountLinkNotFoundError`;
           `from datetime import UTC, datetime`.
  Task 9 → `from sqlalchemy import select`;
           `from ums_smart_revenue.db.finance_models import
           (AdsenseContentOwnerLinkORM, ContentOwnerChannelLinkORM)`;
           `from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM`.
"""
from uuid import UUID, uuid4

from sqlalchemy import create_engine

from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.finance.channel_account_links import AccountOwnerLink
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)
VERIFIER = UUID("00000000-0000-0000-0000-0000000c0001")


def _engine(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    FinanceBase.metadata.create_all(engine)
    return engine


def test_to_api_excludes_provenance_payload():
    link = AccountOwnerLink(
        id="x", adsense_account_id="pub-1", content_owner_id="owner-1",
        verification_status="UNVERIFIED", provenance_kind="OPERATOR_ASSERTED",
        provenance_payload={"secret": "LEAK"}, verified_by=None, verified_at=None,
        verification_reason=None, effective_month_start="2026-01",
        effective_month_end=None,
    )
    api = link.to_api()
    assert "provenance_payload" not in api
    assert "LEAK" not in str(api)
    assert api["adsense_account_id"] == "pub-1"
    assert api["verification_status"] == "UNVERIFIED"
