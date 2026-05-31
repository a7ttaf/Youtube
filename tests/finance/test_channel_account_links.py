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

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.finance.channel_account_links import (
    AccountOwnerLink,
    ChannelAccountLinkValidationError,
    SqlAlchemyChannelAccountLinkRepository,
    _account_owner_lock_key,
    _ranges_overlap,
)
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


def test_lock_key_is_deterministic_and_signed_bigint():
    a = _account_owner_lock_key(TENANT, "pub-1")
    b = _account_owner_lock_key(TENANT, "pub-1")
    assert a == b
    assert 0 <= a < 2**63  # fits PostgreSQL signed bigint


def test_lock_key_differs_by_account_and_tenant():
    other_tenant = UUID("00000000-0000-0000-0000-0000000c00ff")
    assert _account_owner_lock_key(TENANT, "pub-1") != _account_owner_lock_key(TENANT, "pub-2")
    assert _account_owner_lock_key(TENANT, "pub-1") != _account_owner_lock_key(other_tenant, "pub-1")


@pytest.mark.parametrize(
    ("sa", "ea", "sb", "eb", "expected"),
    [
        ("2026-01", "2026-06", "2026-03", None, True),    # B open, overlaps tail
        ("2026-01", "2026-02", "2026-03", "2026-04", False),  # disjoint
        ("2026-01", None, "2030-01", "2030-02", True),     # A open, covers everything later
        ("2026-05", "2026-05", "2026-05", "2026-05", True),   # same single month
        ("2026-01", "2026-02", "2026-02", "2026-03", True),   # touch at 2026-02
    ],
)
def test_ranges_overlap_truth_table(sa, ea, sb, eb, expected):
    assert _ranges_overlap(sa, ea, sb, eb) is expected


def test_acquire_lock_is_noop_on_sqlite(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        # On SQLite this must return without error (no advisory-lock primitive).
        repo._acquire_account_owner_lock("pub-1")


def test_propose_creates_unverified_link(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = repo.propose_account_owner_link(
            adsense_account_id="pub-1", content_owner_id="owner-1",
            effective_month_start="2026-01", effective_month_end=None,
            provenance_kind="OPERATOR_ASSERTED", provenance_payload={"note": "manual"},
        )
        session.commit()
    assert link.verification_status == "UNVERIFIED"
    assert link.adsense_account_id == "pub-1"
    assert link.effective_month_end is None


def test_propose_rejects_bad_month(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError):
            repo.propose_account_owner_link(
                adsense_account_id="pub-1", content_owner_id="owner-1",
                effective_month_start="2026-13", effective_month_end=None,
                provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            )
