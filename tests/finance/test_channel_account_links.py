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
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    FinanceBase,
    FinanceMonthCloseORM,
)
from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM
from ums_smart_revenue.finance.channel_account_links import (
    AccountOwnerLink,
    ChannelAccountLinkConflictError,
    ChannelAccountLinkLockedMonthError,
    ChannelAccountLinkNotFoundError,
    ChannelAccountLinkValidationError,
    SqlAlchemyChannelAccountLinkRepository,
    _account_owner_lock_key,
    _is_unique_violation,
    _ranges_overlap,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)
VERIFIER = UUID("00000000-0000-0000-0000-0000000c0001")


def _engine(tmp_path):
    """Return a fresh SQLite engine with the finance schema applied."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    FinanceBase.metadata.create_all(engine)
    return engine


def test_to_api_excludes_provenance_payload():
    """to_api() omits provenance_payload and any secrets it contains."""
    link = AccountOwnerLink(
        id="x",
        adsense_account_id="pub-1",
        content_owner_id="owner-1",
        verification_status="UNVERIFIED",
        provenance_kind="OPERATOR_ASSERTED",
        provenance_payload={"secret": "LEAK"},
        verified_by=None,
        verified_at=None,
        verification_reason=None,
        effective_month_start="2026-01",
        effective_month_end=None,
    )
    api = link.to_api()
    assert "provenance_payload" not in api
    assert "LEAK" not in str(api)
    assert api["adsense_account_id"] == "pub-1"
    assert api["verification_status"] == "UNVERIFIED"


def test_lock_key_is_deterministic_and_signed_bigint():
    """Lock key is stable across calls and fits in a PostgreSQL signed bigint."""
    a = _account_owner_lock_key(TENANT, "pub-1")
    b = _account_owner_lock_key(TENANT, "pub-1")
    assert a == b
    assert 0 <= a < 2**63  # fits PostgreSQL signed bigint


def test_lock_key_differs_by_account_and_tenant():
    """Lock key differs when account or tenant differs."""
    other_tenant = UUID("00000000-0000-0000-0000-0000000c00ff")
    assert _account_owner_lock_key(TENANT, "pub-1") != _account_owner_lock_key(TENANT, "pub-2")
    assert _account_owner_lock_key(TENANT, "pub-1") != _account_owner_lock_key(
        other_tenant, "pub-1"
    )


@pytest.mark.parametrize(
    ("sa", "ea", "sb", "eb", "expected"),
    [
        ("2026-01", "2026-06", "2026-03", None, True),  # B open, overlaps tail
        ("2026-01", "2026-02", "2026-03", "2026-04", False),  # disjoint
        ("2026-01", None, "2030-01", "2030-02", True),  # A open, covers everything later
        ("2026-05", "2026-05", "2026-05", "2026-05", True),  # same single month
        ("2026-01", "2026-02", "2026-02", "2026-03", True),  # touch at 2026-02
    ],
)
def test_ranges_overlap_truth_table(sa, ea, sb, eb, expected):
    """Parametrized truth table for _ranges_overlap boundary cases."""
    assert _ranges_overlap(sa, ea, sb, eb) is expected


def test_acquire_lock_is_noop_on_sqlite(tmp_path):
    """_acquire_account_owner_lock returns without error on SQLite."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        # On SQLite this must return without error (no advisory-lock primitive).
        repo._acquire_account_owner_lock("pub-1")


def test_propose_creates_unverified_link(tmp_path):
    """propose_account_owner_link inserts an UNVERIFIED row with correct fields."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = repo.propose_account_owner_link(
            adsense_account_id="pub-1",
            content_owner_id="owner-1",
            effective_month_start="2026-01",
            effective_month_end=None,
            provenance_kind="OPERATOR_ASSERTED",
            provenance_payload={"note": "manual"},
        )
        session.commit()
    assert link.verification_status == "UNVERIFIED"
    assert link.adsense_account_id == "pub-1"
    assert link.effective_month_end is None
    assert isinstance(link.id, str) and link.id
    assert link.provenance_payload == {"note": "manual"}


def test_propose_rejects_bad_month(tmp_path):
    """propose raises ChannelAccountLinkValidationError on a malformed month."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError):
            repo.propose_account_owner_link(
                adsense_account_id="pub-1",
                content_owner_id="owner-1",
                effective_month_start="2026-13",
                effective_month_end=None,
                provenance_kind="OPERATOR_ASSERTED",
                provenance_payload={},
            )


def test_propose_rejects_end_before_start(tmp_path):
    """propose raises ValidationError when effective_month_end < effective_month_start."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError, match="effective_month_end"):
            repo.propose_account_owner_link(
                adsense_account_id="pub-1",
                content_owner_id="owner-1",
                effective_month_start="2026-06",
                effective_month_end="2026-01",
                provenance_kind="OPERATOR_ASSERTED",
                provenance_payload={},
            )


def _propose(repo, *, account="pub-1", owner="owner-1", start="2026-01", end=None):
    """Propose a link with default values; returns the AccountOwnerLink."""
    return repo.propose_account_owner_link(
        adsense_account_id=account,
        content_owner_id=owner,
        effective_month_start=start,
        effective_month_end=end,
        provenance_kind="OPERATOR_ASSERTED",
        provenance_payload={},
    )


def test_verify_marks_verified_and_stamps(tmp_path):
    """verify_account_owner_link transitions to VERIFIED and stamps verified_by/at/reason."""
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
    """verify raises ConflictError when a VERIFIED link already overlaps the range."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        first = _propose(repo, owner="owner-1", start="2026-01", end="2026-06")
        repo.verify_account_owner_link(first.id, verified_by=VERIFIER, reason="r1")
        second = _propose(repo, owner="owner-2", start="2026-03", end=None)
        with pytest.raises(ChannelAccountLinkConflictError):
            repo.verify_account_owner_link(second.id, verified_by=VERIFIER, reason="r2")


def test_verify_non_overlapping_succeeds(tmp_path):
    """verify succeeds when a VERIFIED link occupies a non-overlapping range."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        first = _propose(repo, owner="owner-1", start="2026-01", end="2026-06")
        repo.verify_account_owner_link(first.id, verified_by=VERIFIER, reason="r1")
        second = _propose(repo, owner="owner-2", start="2026-07", end=None)
        out = repo.verify_account_owner_link(second.id, verified_by=VERIFIER, reason="r2")
    assert out.verification_status == "VERIFIED"


def test_reject_then_reverify_competitor_succeeds(tmp_path):
    """Rejecting the VERIFIED link allows verifying a competing link."""
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
    """verify raises ChannelAccountLinkNotFoundError for an unknown link id."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkNotFoundError):
            repo.verify_account_owner_link(str(uuid4()), verified_by=VERIFIER, reason="x")


def test_get_account_owner_link_returns_and_raises(tmp_path):
    """get_account_owner_link returns the link or raises NotFoundError."""
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
    """A REJECTED link can be re-verified as the recovery path."""
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
    """list_account_owner_links filters by account, paginates, and reports total_count."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        _propose(repo, account="pub-1", owner="owner-1", start="2026-01")
        _propose(repo, account="pub-1", owner="owner-2", start="2026-02")
        _propose(repo, account="pub-2", owner="owner-9", start="2026-01")
        session.flush()
        page = repo.list_account_owner_links(adsense_account_id="pub-1", limit=1, offset=0)
        all_pub1 = repo.list_account_owner_links(adsense_account_id="pub-1", limit=50, offset=0)
    assert page.total_count == 2
    assert len(page.links) == 1
    assert {link.adsense_account_id for link in all_pub1.links} == {"pub-1"}


def test_list_status_and_month_filter(tmp_path):
    """status and month filters each independently restrict the result set."""
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
    """list raises ValidationError when limit < 1 or offset < 0."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError, match="limit"):
            repo.list_account_owner_links(limit=0, offset=0)
        with pytest.raises(ChannelAccountLinkValidationError, match="offset"):
            repo.list_account_owner_links(limit=10, offset=-1)


def test_list_rejects_bad_month(tmp_path):
    """list raises ValidationError on a malformed month filter."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError):
            repo.list_account_owner_links(month="2026-13", limit=10, offset=0)


def test_list_rejects_invalid_status(tmp_path):
    """list raises ValidationError for an unknown status filter value."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError, match="invalid status"):
            repo.list_account_owner_links(status="BOGUS", limit=10, offset=0)


def test_list_month_filter_enforces_upper_bound(tmp_path):
    """Month filter excludes links whose effective range ended before the queried month."""
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
    """Insert a GoogleRevenueSourceRowORM row for derivation tests."""
    session.add(
        GoogleRevenueSourceRowORM(
            id=uuid4(),
            tenant_id=TENANT,
            source_system="youtube_reporting",
            source_row_key=key.ljust(64, "0"),
            source_account_id=account,
            content_owner_id=owner,
            youtube_channel_id=channel,
            report_month=month,
            report_type="channel_basic_a2",
            period_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
            period_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
            metric_key="estimated_partner_revenue",
            value_kind="estimated",
            amount_native=0,
            currency_code="USD",
            raw_payload={},
        )
    )


def test_derivation_only_uses_rows_with_both_owner_and_channel(tmp_path):
    """upsert_owner_channel_links_from_source skips rows missing owner or channel."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _source_row(session, owner="owner-1", channel="chan-1", key="k1")
        _source_row(session, owner=None, channel="chan-2", key="k2")  # no owner
        _source_row(session, owner="owner-3", channel=None, key="k3")  # no channel
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


def test_derivation_skips_blank_source_identities(tmp_path):
    """upsert skips '' / whitespace-only owner or channel without aborting the job.

    Regression for PR #57 -V8b: the source identity columns are nullable Text with
    NO non-empty CHECK, so a row can carry '' or a whitespace-only owner/channel.
    The derivation only excluded NULLs, so a blank identity would hit
    content_owner_channel_links' length(...) >= 1 CHECK (sqlstate 23514 — not the
    unique 23505 the per-row SAVEPOINT swallows) and abort the whole derivation, or
    a whitespace-only value would persist a bogus active link. Both must be skipped,
    and only the one fully-populated row may derive a link.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _source_row(session, owner="owner-ok", channel="chan-ok", key="kok")
        _source_row(session, owner="", channel="chan-empty", key="kempty")  # empty owner
        _source_row(session, owner="owner-ws", channel="   ", key="kws")  # whitespace channel
        _source_row(session, owner="\t\n", channel="chan-tab", key="ktab")  # tab/newline owner
        session.commit()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        created = repo.upsert_owner_channel_links_from_source()  # must not raise
        session.commit()
        rows = session.scalars(select(ContentOwnerChannelLinkORM)).all()
    assert created == 1
    assert len(rows) == 1
    assert rows[0].content_owner_id == "owner-ok"
    assert rows[0].youtube_channel_id == "chan-ok"


def test_derivation_is_idempotent(tmp_path):
    """Running upsert twice does not create duplicate links."""
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


def test_derivation_swallows_concurrent_duplicate_insert(tmp_path, monkeypatch):
    """A duplicate key landing between the probe and flush is swallowed (idempotent).

    Simulates a parallel derivation worker that inserted the same
    (owner, channel, month) key AFTER our existence probe but BEFORE our flush:
    force the fast-path probe to miss while the row already exists, then assert the
    per-row SAVEPOINT swallows uq_content_owner_channel_links_key instead of
    failing the whole derivation job. Without the savepoint guard this raises
    IntegrityError out of upsert_owner_channel_links_from_source.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        # The key already exists — a concurrent worker won the race to insert it.
        session.add(
            ContentOwnerChannelLinkORM(
                tenant_id=TENANT,
                content_owner_id="owner-1",
                youtube_channel_id="chan-1",
                provenance_kind="SOURCE_ROW",
                active=True,
                effective_month_start="2026-04",
                effective_month_end="2026-04",
            )
        )
        _source_row(session, owner="owner-1", channel="chan-1", month="2026-04", key="k1")
        session.commit()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        # Force the fast-path probe to miss so the insert path runs and collides.
        monkeypatch.setattr(repo, "_owner_channel_link_exists", lambda *a, **k: False)
        created = repo.upsert_owner_channel_links_from_source()  # must NOT raise
        session.commit()
        rows = session.scalars(select(ContentOwnerChannelLinkORM)).all()
    assert created == 0  # collision swallowed, not counted as a new insert
    assert len(rows) == 1  # no duplicate row inserted


def test_derivation_groups_by_month_and_collapses_duplicate_rows(tmp_path):
    """Derivation groups source rows by (owner, channel, month) into one link each."""
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


def test_read_contract_returns_verified_month_scoped_channels(tmp_path):
    """list_verified_adsense_account_channels returns channels for verified, in-range links."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, account="pub-1", owner="owner-1", start="2026-01", end=None)
        repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="r")
        _source_row(session, owner="owner-1", channel="chan-1", month="2026-04", key="s1")
        session.commit()
        repo.upsert_owner_channel_links_from_source()
        session.commit()
        got = repo.list_verified_adsense_account_channels(
            tenant_id=TENANT, month="2026-04", adsense_account_id="pub-1"
        )
        wrong_month = repo.list_verified_adsense_account_channels(
            tenant_id=TENANT, month="2026-05", adsense_account_id="pub-1"
        )
    assert got == ["chan-1"]
    assert wrong_month == []  # owner-channel link only observed for 2026-04


def test_read_contract_excludes_unverified_account(tmp_path):
    """UNVERIFIED account links return no channels."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        _propose(
            repo, account="pub-1", owner="owner-1", start="2026-01", end=None
        )  # never verified
        _source_row(session, owner="owner-1", channel="chan-1", month="2026-04", key="s1")
        session.commit()
        repo.upsert_owner_channel_links_from_source()
        session.commit()
        got = repo.list_verified_adsense_account_channels(
            tenant_id=TENANT, month="2026-04", adsense_account_id="pub-1"
        )
    assert got == []


def test_read_contract_is_tenant_isolated(tmp_path):
    """Channels for one tenant are not visible to another tenant."""
    # A different tenant sharing the same content_owner_id string must not leak.
    other_tenant = UUID("00000000-0000-0000-0000-0000000c00ff")
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, account="pub-1", owner="owner-1", start="2026-01", end=None)
        repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="r")
        _source_row(session, owner="owner-1", channel="chan-A", month="2026-04", key="sa")
        session.commit()
        repo.upsert_owner_channel_links_from_source()
        # Other tenant: same owner_id string, different channel, valid for the month.
        session.add(
            ContentOwnerChannelLinkORM(
                tenant_id=other_tenant,
                content_owner_id="owner-1",
                youtube_channel_id="chan-B",
                provenance_kind="MANUAL",
                active=True,
                effective_month_start="2026-04",
                effective_month_end=None,
            )
        )
        session.commit()
        got = repo.list_verified_adsense_account_channels(
            tenant_id=TENANT, month="2026-04", adsense_account_id="pub-1"
        )
    assert got == ["chan-A"]  # other tenant's chan-B excluded


def test_read_contract_excludes_inactive_owner_channel_link(tmp_path):
    """Inactive owner↔channel links are excluded from the read contract."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, account="pub-1", owner="owner-1", start="2026-01", end=None)
        repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="r")
        session.add(
            ContentOwnerChannelLinkORM(
                tenant_id=TENANT,
                content_owner_id="owner-1",
                youtube_channel_id="chan-x",
                provenance_kind="MANUAL",
                active=False,
                effective_month_start="2026-04",
                effective_month_end=None,
            )
        )
        session.commit()
        got = repo.list_verified_adsense_account_channels(
            tenant_id=TENANT, month="2026-04", adsense_account_id="pub-1"
        )
    assert got == []  # inactive owner-channel link excluded


def test_read_contract_dedups_channel_reachable_twice(tmp_path):
    """A channel reachable via two owner paths appears only once in results."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, account="pub-1", owner="owner-1", start="2026-01", end=None)
        repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="r")
        # Same channel via two distinct owner-channel rows, both valid for 2026-04
        # (different effective_month_start so the unique key permits both).
        session.add(
            ContentOwnerChannelLinkORM(
                tenant_id=TENANT,
                content_owner_id="owner-1",
                youtube_channel_id="chan-1",
                provenance_kind="SOURCE_ROW",
                active=True,
                effective_month_start="2026-04",
                effective_month_end="2026-04",
            )
        )
        session.add(
            ContentOwnerChannelLinkORM(
                tenant_id=TENANT,
                content_owner_id="owner-1",
                youtube_channel_id="chan-1",
                provenance_kind="MANUAL",
                active=True,
                effective_month_start="2026-01",
                effective_month_end=None,
            )
        )
        session.commit()
        got = repo.list_verified_adsense_account_channels(
            tenant_id=TENANT, month="2026-04", adsense_account_id="pub-1"
        )
    assert got == ["chan-1"]  # deduped to a single entry


def test_verify_locked_month_raises_error(tmp_path):
    """verify_account_owner_link raises ChannelAccountLinkLockedMonthError on a LOCKED month."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month="2026-01", status="LOCKED"))
        session.flush()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, start="2026-01")
        with pytest.raises(ChannelAccountLinkLockedMonthError, match="locked"):
            repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="x")


def test_verify_locked_month_inside_bounded_range_raises(tmp_path):
    """verify rejects when a LOCKED month sits inside the link's bounded range.

    The link starts in an OPEN month (2026-01) but spans a LOCKED month
    (2026-02); a VERIFIED link feeds allocation for every covered month, so
    verify must reject the whole range — not only check the start month.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month="2026-02", status="LOCKED"))
        session.flush()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, start="2026-01", end="2026-03")
        with pytest.raises(ChannelAccountLinkLockedMonthError, match="2026-02"):
            repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="x")


def test_verify_open_ended_rejects_when_later_month_locked(tmp_path):
    """An open-ended link is rejected when a LOCKED month at/after start exists."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month="2026-05", status="LOCKED"))
        session.flush()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, start="2026-01", end=None)
        with pytest.raises(ChannelAccountLinkLockedMonthError, match="2026-05"):
            repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="x")


def test_reject_verified_link_in_locked_range_raises(tmp_path):
    """Rejecting a VERIFIED link covering a LOCKED month is blocked (closed-month guard)."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, start="2026-01", end="2026-03")
        repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="ok")
        # Lock a covered month AFTER the link is VERIFIED to model a close that
        # lands later. verify no longer materializes every covered month, so the
        # 2026-02 close row is inserted here directly.
        session.add(FinanceMonthCloseORM(month="2026-02", status="LOCKED"))
        session.flush()
        with pytest.raises(ChannelAccountLinkLockedMonthError, match="2026-02"):
            repo.reject_account_owner_link(link.id, verified_by=VERIFIER, reason="undo")


def test_verify_far_future_range_does_not_materialize_months(tmp_path):
    """A bounded link to a far-future end must not create one close row per month.

    verify screens the covered range with a single bounded SELECT, so an
    authorized far-future ``effective_month_end`` cannot insert (and
    advisory-lock) a finance_month_close row for every covered month — only the
    start month's row is materialized.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, start="2026-01", end="9999-12")
        repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="ok")
        session.flush()
        close_rows = session.scalar(select(func.count()).select_from(FinanceMonthCloseORM))
    assert close_rows == 1  # only the start month, not ~96k rows


def test_verify_far_future_range_still_detects_locked_month(tmp_path):
    """The single-SELECT range scan still catches a LOCKED month deep in a long range."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month="2030-06", status="LOCKED"))
        session.flush()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, start="2026-01", end="9999-12")
        with pytest.raises(ChannelAccountLinkLockedMonthError, match="2030-06"):
            repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="x")


def test_reject_reloads_link_status_after_acquiring_account_lock(tmp_path, monkeypatch):
    """reject re-reads the row under the per-account lock (TOCTOU guard).

    Models the race window: the row is loaded UNVERIFIED, then another
    transaction verifies it and locks a covered month while reject waits on the
    advisory lock. reject must observe the committed VERIFIED status (so the
    locked-month guard fires), not the stale UNVERIFIED state it first loaded.
    """
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, start="2026-01", end="2026-03")
        session.flush()

        def _concurrent_verify_and_close(_account_id):
            """Stand in for the lock wait: commit a concurrent verify + month close.

            Flips the row to VERIFIED and locks a covered month via a Core UPDATE
            WITHOUT touching the already-loaded ORM instance (no session sync), so
            reject's in-memory verification_status stays stale until refresh().
            """
            session.execute(
                update(AdsenseContentOwnerLinkORM)
                .where(AdsenseContentOwnerLinkORM.adsense_account_id == "pub-1")
                .values(verification_status="VERIFIED")
                .execution_options(synchronize_session=False)
            )
            session.add(FinanceMonthCloseORM(month="2026-02", status="LOCKED"))
            session.flush()

        monkeypatch.setattr(repo, "_acquire_account_owner_lock", _concurrent_verify_and_close)
        with pytest.raises(ChannelAccountLinkLockedMonthError, match="2026-02"):
            repo.reject_account_owner_link(link.id, verified_by=VERIFIER, reason="undo")


def test_reject_unverified_link_in_locked_month_succeeds(tmp_path):
    """An UNVERIFIED proposal in a LOCKED month is still rejectable (not in read contract)."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month="2026-01", status="LOCKED"))
        session.flush()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, start="2026-01")
        out = repo.reject_account_owner_link(link.id, verified_by=VERIFIER, reason="bad")
    assert out.verification_status == "REJECTED"


def test_derivation_skips_locked_months(tmp_path):
    """Derivation does not insert owner↔channel links for a LOCKED month."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month="2026-04", status="LOCKED"))
        _source_row(session, owner="owner-1", channel="chan-1", month="2026-04", key="k-locked")
        _source_row(session, owner="owner-1", channel="chan-2", month="2026-05", key="k-open")
        session.commit()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        created = repo.upsert_owner_channel_links_from_source()
        session.commit()
        rows = session.scalars(select(ContentOwnerChannelLinkORM)).all()
    assert created == 1  # only the OPEN month's link is derived
    assert [(r.youtube_channel_id, r.effective_month_start) for r in rows] == [
        ("chan-2", "2026-05"),
    ]


def test_is_unique_violation_classifies_correctly():
    """_is_unique_violation returns True only for UNIQUE constraint violations."""
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    class _PG23505:
        """Stand-in DBAPI cause with the psycopg2-style unique-violation pgcode."""

        pgcode = "23505"

    class _PG23000:
        """Stand-in DBAPI cause with a non-unique psycopg2 integrity pgcode."""

        pgcode = "23000"

    class _Psycopg3Unique:
        """Stand-in psycopg3 cause: SQLSTATE via `sqlstate`, no `pgcode`."""

        sqlstate = "23505"

    class _Psycopg3NotNull:
        """Stand-in psycopg3 cause with a non-unique SQLSTATE."""

        sqlstate = "23502"

    assert _is_unique_violation(SAIntegrityError("x", {}, _PG23505())) is True
    assert _is_unique_violation(SAIntegrityError("x", {}, _PG23000())) is False
    # psycopg3 (the pinned driver) exposes SQLSTATE via `sqlstate`, not `pgcode`.
    assert _is_unique_violation(SAIntegrityError("x", {}, _Psycopg3Unique())) is True
    assert _is_unique_violation(SAIntegrityError("x", {}, _Psycopg3NotNull())) is False
    assert (
        _is_unique_violation(
            SAIntegrityError("x", {}, Exception("UNIQUE constraint failed: t.col"))
        )
        is True
    )
    assert (
        _is_unique_violation(SAIntegrityError("x", {}, Exception("NOT NULL constraint failed")))
        is False
    )
    assert _is_unique_violation(SAIntegrityError("x", {}, None)) is False
