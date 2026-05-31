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
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import ContentOwnerChannelLinkORM, FinanceBase
from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM
from ums_smart_revenue.finance.channel_account_links import (
    AccountOwnerLink,
    ChannelAccountLinkConflictError,
    ChannelAccountLinkNotFoundError,
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
    assert isinstance(link.id, str) and link.id
    assert link.provenance_payload == {"note": "manual"}


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


def test_propose_rejects_end_before_start(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError, match="effective_month_end"):
            repo.propose_account_owner_link(
                adsense_account_id="pub-1", content_owner_id="owner-1",
                effective_month_start="2026-06", effective_month_end="2026-01",
                provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            )


def _propose(repo, *, account="pub-1", owner="owner-1", start="2026-01", end=None):
    return repo.propose_account_owner_link(
        adsense_account_id=account, content_owner_id=owner,
        effective_month_start=start, effective_month_end=end,
        provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
    )


def test_verify_marks_verified_and_stamps(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo)
        out = repo.verify_account_owner_link(
            link.id, verified_by=VERIFIER, reason="confirmed via contract"
        )
        session.commit()
    assert out.verification_status == "VERIFIED"
    assert out.verified_by == str(VERIFIER)
    assert out.verification_reason == "confirmed via contract"
    assert out.verified_at is not None


def test_verify_overlapping_verified_raises_conflict(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        first = _propose(repo, owner="owner-1", start="2026-01", end="2026-06")
        repo.verify_account_owner_link(first.id, verified_by=VERIFIER, reason="r1")
        second = _propose(repo, owner="owner-2", start="2026-03", end=None)
        with pytest.raises(ChannelAccountLinkConflictError):
            repo.verify_account_owner_link(second.id, verified_by=VERIFIER, reason="r2")


def test_verify_non_overlapping_succeeds(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        first = _propose(repo, owner="owner-1", start="2026-01", end="2026-06")
        repo.verify_account_owner_link(first.id, verified_by=VERIFIER, reason="r1")
        second = _propose(repo, owner="owner-2", start="2026-07", end=None)
        out = repo.verify_account_owner_link(second.id, verified_by=VERIFIER, reason="r2")
    assert out.verification_status == "VERIFIED"


def test_reject_then_reverify_competitor_succeeds(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        first = _propose(repo, owner="owner-1", start="2026-01", end=None)
        repo.verify_account_owner_link(first.id, verified_by=VERIFIER, reason="r1")
        second = _propose(repo, owner="owner-2", start="2026-01", end=None)
        repo.reject_account_owner_link(first.id, verified_by=VERIFIER, reason="superseded")
        out = repo.verify_account_owner_link(second.id, verified_by=VERIFIER, reason="r2")
    assert out.verification_status == "VERIFIED"


def test_verify_missing_id_raises_not_found(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkNotFoundError):
            repo.verify_account_owner_link(str(uuid4()), verified_by=VERIFIER, reason="x")


def test_get_account_owner_link_returns_and_raises(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo)
        got = repo.get_account_owner_link(link.id)
        assert got.id == link.id
        assert got.adsense_account_id == "pub-1"
        with pytest.raises(ChannelAccountLinkNotFoundError):
            repo.get_account_owner_link(str(uuid4()))


def test_reverify_rejected_same_link_succeeds(tmp_path):
    # A REJECTED link can be re-verified (deliberate recovery path): the
    # (tenant, account, owner, start) unique key blocks re-proposing an identical
    # row, so verify is the recovery route. The overlap invariant still guards it.
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo)
        repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="r1")
        repo.reject_account_owner_link(link.id, verified_by=VERIFIER, reason="mistake")
        out = repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="reinstated")
    assert out.verification_status == "VERIFIED"
    assert out.verification_reason == "reinstated"


def test_list_filters_paginates_and_counts(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        _propose(repo, account="pub-1", owner="owner-1", start="2026-01")
        _propose(repo, account="pub-1", owner="owner-2", start="2026-02")
        _propose(repo, account="pub-2", owner="owner-9", start="2026-01")
        session.flush()
        page = repo.list_account_owner_links(
            adsense_account_id="pub-1", limit=1, offset=0
        )
        all_pub1 = repo.list_account_owner_links(adsense_account_id="pub-1", limit=50, offset=0)
    assert page.total_count == 2
    assert len(page.links) == 1
    assert {link.adsense_account_id for link in all_pub1.links} == {"pub-1"}


def test_list_status_and_month_filter(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        a = _propose(repo, account="pub-1", owner="owner-1", start="2026-01", end="2026-03")
        _propose(repo, account="pub-1", owner="owner-2", start="2026-08", end=None)
        repo.verify_account_owner_link(a.id, verified_by=VERIFIER, reason="r")
        session.flush()
        verified = repo.list_account_owner_links(status="VERIFIED", limit=50, offset=0)
        feb = repo.list_account_owner_links(month="2026-02", limit=50, offset=0)
    assert {link.verification_status for link in verified.links} == {"VERIFIED"}
    assert all(link.effective_month_start <= "2026-02" for link in feb.links)
    assert feb.total_count == 1


def test_list_rejects_bad_bounds(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError, match="limit"):
            repo.list_account_owner_links(limit=0, offset=0)
        with pytest.raises(ChannelAccountLinkValidationError, match="offset"):
            repo.list_account_owner_links(limit=10, offset=-1)


def test_list_rejects_bad_month(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError):
            repo.list_account_owner_links(month="2026-13", limit=10, offset=0)


def test_list_month_filter_enforces_upper_bound(tmp_path):
    # A link whose range ENDED before the queried month is excluded. The link's
    # start (2026-01) still satisfies the lower bound at 2026-02, so only the
    # upper-bound leg (end >= month) can exclude it — this independently pins
    # that leg of the month-validity predicate.
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        _propose(repo, account="pub-1", owner="owner-1", start="2026-01", end="2026-01")
        session.flush()
        jan = repo.list_account_owner_links(month="2026-01", limit=50, offset=0)
        feb = repo.list_account_owner_links(month="2026-02", limit=50, offset=0)
    assert jan.total_count == 1
    assert feb.total_count == 0


def _source_row(session, *, owner, channel, account="pub-x", month="2026-04", key="k1"):
    session.add(
        GoogleRevenueSourceRowORM(
            id=uuid4(), tenant_id=TENANT, source_system="youtube_reporting",
            source_row_key=key.ljust(64, "0"), source_account_id=account,
            content_owner_id=owner,
            youtube_channel_id=channel, report_month=month,
            report_type="channel_basic_a2",
            period_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
            period_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
            metric_key="estimated_partner_revenue", value_kind="estimated",
            amount_native=0, currency_code="USD", raw_payload={},
        )
    )


def test_derivation_only_uses_rows_with_both_owner_and_channel(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _source_row(session, owner="owner-1", channel="chan-1", key="k1")
        _source_row(session, owner=None, channel="chan-2", key="k2")     # no owner
        _source_row(session, owner="owner-3", channel=None, key="k3")    # no channel
        session.commit()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        created = repo.upsert_owner_channel_links_from_source()
        session.commit()
        rows = session.scalars(select(ContentOwnerChannelLinkORM)).all()
    assert created == 1
    assert len(rows) == 1
    assert rows[0].content_owner_id == "owner-1"
    assert rows[0].youtube_channel_id == "chan-1"
    assert rows[0].provenance_kind == "SOURCE_ROW"
    assert rows[0].effective_month_start == "2026-04"
    assert rows[0].effective_month_end == "2026-04"


def test_derivation_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _source_row(session, owner="owner-1", channel="chan-1", key="k1")
        session.commit()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        repo.upsert_owner_channel_links_from_source()
        session.commit()
        again = repo.upsert_owner_channel_links_from_source()
        session.commit()
        rows = session.scalars(select(ContentOwnerChannelLinkORM)).all()
    assert again == 0
    assert len(rows) == 1


def test_derivation_groups_by_month_and_collapses_duplicate_rows(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        # Two source rows for the same owner/channel/month -> must collapse to one
        # link; provenance_source_id = min(source_row_key). Plus a second month.
        _source_row(session, owner="owner-1", channel="chan-1", month="2026-04", key="k-apr-2")
        _source_row(session, owner="owner-1", channel="chan-1", month="2026-04", key="k-apr-1")
        _source_row(session, owner="owner-1", channel="chan-1", month="2026-05", key="k-may-1")
        session.commit()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        created = repo.upsert_owner_channel_links_from_source()
        session.commit()
        rows = session.scalars(
            select(ContentOwnerChannelLinkORM).order_by(
                ContentOwnerChannelLinkORM.effective_month_start
            )
        ).all()
    assert created == 2  # one per month; the duplicate April row collapsed
    assert [r.effective_month_start for r in rows] == ["2026-04", "2026-05"]
    assert all(r.effective_month_start == r.effective_month_end for r in rows)
    # April link's provenance is the lexicographically smallest April source key.
    assert rows[0].provenance_source_id.startswith("k-apr-1")
