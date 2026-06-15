"""Postgres RLS proof for the connector run path (the heart of this PR).

On RLS-enforced Postgres, a connector run driven outside a FastAPI request must:

1. Resolve the credential on the tenant lane ONLY when ``TENANT_CTX`` is set
   (the CLI/executor context). Without it the run fails closed at
   ``_load_credential`` -- P1 in the lane-fix design.
2. Issue its platform-only writes (``audit_logs`` lifecycle emits,
   ``monthly_channel_revenue_facts`` fact upserts, ``finance_month_close`` row
   creation) under ``app_platform`` -- on the bare tenant lane these
   permission-deny (TENANT_PLATFORM_ONLY_WRITE_TABLES, migration 20260608_0001),
   which is P2.
3. Keep RLS tenant scoping intact: tenant A's run never reads or writes tenant
   B's rows.

These tests use the real migrated schema, real DB writes, and the role-marked
tenant-lane session factory (``build_session_factory``); only the network /
blob / OAuth layer is stubbed (mirroring ``test_ingestion_gate.py``'s
orchestrator-scope mock pattern). The RED obligations (lifecycle-emit denial,
normalization write-set denial) were observed failing before the
``platform_lane`` wiring landed -- run-to-fail evidence is recorded in the
implementation task.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.db._pg_schema_helpers import reset_public_schema
from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.connectors.google import (
    local_secret_resolver,
    secret_resolver,
)
from ums_smart_revenue.connectors.runs.orchestrator import run_one
from ums_smart_revenue.connectors.runs.tenant_context import (
    connector_tenant_context,
)
from ums_smart_revenue.db.lane import platform_lane
from ums_smart_revenue.db.session import build_session_factory

YT_REPORTING_KEY = "youtube-reporting"
ACCOUNT_ID = "cms-1"
REPORT_MONTH = "2026-04"
SERVICE_ACTOR_ID = "ddddeeee-ffff-0000-1111-2222009d0000"
CHANNEL_ID = "UC_rls_1"
RESOLVER_REF = "local-secret://rls-creds"

_UPGRADED: set[str] = set()


def _alembic_config(url: str) -> Config:
    """Alembic config bound to ``url`` without an ini path (no logging poison)."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", "backend/ums_smart_revenue/db/alembic")
    return cfg


def _ensure_fresh_head(url: str) -> None:
    """Reset the public schema and migrate to head once per test session."""
    if url in _UPGRADED:
        return
    reset_public_schema(url)
    command.upgrade(_alembic_config(url), "head")
    _UPGRADED.add(url)


@pytest.fixture(scope="module")
def pg_url() -> str:
    """Resolve the disposable Postgres URL and migrate it to head."""
    url = require_postgres_url()
    _ensure_fresh_head(url)
    return url


@pytest.fixture
def fresh_tenant() -> UUID:
    """A unique tenant id per test so the shared migrated schema stays clean.

    The tests share one migrated database (no per-test schema reset), so a
    fresh tenant id keeps each test's connector_run / source-row / fact / audit
    counts deterministic without truncating tables between tests.
    """
    return uuid4()


@pytest.fixture(autouse=True)
def _service_actor_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Set the connector service actor env for the lifecycle audit principal."""
    from ums_smart_revenue.config.settings import (
        GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
        load_app_settings,
    )

    monkeypatch.setenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, SERVICE_ACTOR_ID)
    load_app_settings.cache_clear()
    try:
        yield SERVICE_ACTOR_ID
    finally:
        load_app_settings.cache_clear()


@pytest.fixture
def _stub_secret_resolver() -> Iterator[None]:
    """Register a ``local-secret://rls-creds`` resolver for the run."""
    mapping = {
        "rls-creds": json.dumps(
            {
                "refresh_token": "rt",
                "client_id": "cid",
                "client_secret": "cs",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    }
    saved = dict(secret_resolver._REGISTRY)
    secret_resolver._REGISTRY.clear()
    secret_resolver.register_resolver(
        scheme="local-secret",
        resolver=local_secret_resolver.LocalSecretResolver(mapping=mapping),
    )
    try:
        yield
    finally:
        secret_resolver._REGISTRY.clear()
        secret_resolver._REGISTRY.update(saved)


def _seed_owner(url: str, *, tenant_id: UUID, with_channel: bool = True) -> None:
    """Seed tenant/currency/channel/credential rows as the schema owner.

    RLS is ENABLE (not FORCE), so the owner login bypasses the policies and can
    write every table directly; this mirrors how the migration round-trip tests
    seed fixtures. Idempotent: the tests share one migrated schema, so a
    per-tenant guard skips reseeding when the credential already exists.
    """
    engine = sa.create_engine(url)
    now = datetime.now(UTC)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO currencies (code, numeric_code, name, "
                    "minor_unit, is_supported, activated_at) VALUES "
                    "('USD','840','US Dollar',2,true,:now) "
                    "ON CONFLICT (code) DO NOTHING"
                ),
                {"now": now},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO tenants (id, slug, display_name) VALUES "
                    "(:id, :slug, :name) ON CONFLICT (id) DO NOTHING"
                ),
                {"id": tenant_id, "slug": f"t-{tenant_id}", "name": "T"},
            )
            already = conn.scalar(
                sa.text(
                    "SELECT count(*) FROM api_connector_credentials "
                    "WHERE tenant_id = :tid AND connector_key = :key "
                    "AND account_id = :acct"
                ),
                {"tid": tenant_id, "key": YT_REPORTING_KEY, "acct": ACCOUNT_ID},
            )
            if already:
                return
            actor_id = uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO users (id, tenant_id, email, display_name) "
                    "VALUES (:id, :tid, :email, :name)"
                ),
                {
                    "id": actor_id,
                    "tid": tenant_id,
                    "email": f"seed-{actor_id}@example.com",
                    "name": "Seed",
                },
            )
            if with_channel:
                conn.execute(
                    sa.text(
                        "INSERT INTO youtube_channels (id, tenant_id, "
                        "youtube_channel_id, channel_name, content_owner_id, "
                        "active, revenue_required, cms_status) VALUES "
                        "(:id, :tid, :cid, :name, :owner, true, true, "
                        "'INSIDE_CMS') ON CONFLICT "
                        "(tenant_id, youtube_channel_id) DO NOTHING"
                    ),
                    {
                        "id": uuid4(),
                        "tid": tenant_id,
                        "cid": CHANNEL_ID,
                        "name": "Chan",
                        "owner": ACCOUNT_ID,
                    },
                )
            conn.execute(
                sa.text(
                    "INSERT INTO api_connector_credentials (id, tenant_id, "
                    "connector_key, account_id, encrypted_secret_ref, status, "
                    "created_by, updated_by) VALUES (:id, :tid, :key, :acct, "
                    ":ref, 'active', :by, :by)"
                ),
                {
                    "id": uuid4(),
                    "tid": tenant_id,
                    "key": YT_REPORTING_KEY,
                    "acct": ACCOUNT_ID,
                    "ref": RESOLVER_REF,
                    "by": actor_id,
                },
            )
    finally:
        engine.dispose()


def _yt_reporting_csv() -> bytes:
    """One-row YouTube Reporting CSV the mock runner adapts + persists."""
    header = b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
    row = f"2026-04-15,{CHANNEL_ID},{ACCOUNT_ID},123.450000,USD\n".encode()
    return header + row


def _run_yt_reporting(session: Session, tenant_id: UUID):
    """Drive ``run_one`` for YT Reporting with the network/blob layer mocked."""
    from unittest.mock import patch

    with (
        patch("ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient") as yt_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None
        client = yt_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = _yt_reporting_csv()
        store: dict[str, bytes] = {}
        backend = local_cls.return_value
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]
        return run_one(
            session,
            tenant_id=tenant_id,
            connector_key=YT_REPORTING_KEY,
            account_id=ACCOUNT_ID,
            report_month=REPORT_MONTH,
        )


# ---------------------------------------------------------------------------
# Obligation 4: CLI context helper -- fail-closed credential pin.
# ---------------------------------------------------------------------------
def test_run_without_tenant_context_fails_closed_at_credential(
    pg_url: str, fresh_tenant: UUID, _stub_secret_resolver: None
) -> None:
    """Without TENANT_CTX the run fails closed: the credential is invisible."""
    from ums_smart_revenue.connectors.google.errors import (
        CredentialNotFoundError,
    )

    _seed_owner(pg_url, tenant_id=fresh_tenant)
    factory = build_session_factory(pg_url)
    # No connector_tenant_context: TENANT_CTX is unset, the hook pins app_tenant
    # with no trusted context row, and the credential SELECT returns nothing.
    with factory() as session:  # skipcq: PTC-W0062
        with pytest.raises(CredentialNotFoundError):
            _run_yt_reporting(session, fresh_tenant)


def test_run_with_tenant_context_resolves_credential_and_succeeds(
    pg_url: str, fresh_tenant: UUID, _stub_secret_resolver: None
) -> None:
    """With connector_tenant_context the credential resolves and the run runs."""
    _seed_owner(pg_url, tenant_id=fresh_tenant)
    factory = build_session_factory(pg_url)
    with factory() as session:  # skipcq: PTC-W0062
        with connector_tenant_context(fresh_tenant, session=session):
            outcome = _run_yt_reporting(session, fresh_tenant)
    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Obligations 1 + 2 (GREEN after fix): lifecycle emits + normalization writes
# land on the platform lane; the run is durable and facts are projected.
# ---------------------------------------------------------------------------
def test_run_one_persists_lifecycle_facts_and_audit_on_postgres(
    pg_url: str, fresh_tenant: UUID, _stub_secret_resolver: None
) -> None:
    """A live run writes the run row, source rows, facts, and audit -- all on PG.

    The lifecycle audit emits (audit_logs), fact upserts
    (monthly_channel_revenue_facts), and the OPEN month-close row creation
    (finance_month_close) are all platform-only-write tables; before the
    platform_lane wiring these permission-denied on the tenant lane.
    """
    from ums_smart_revenue.db.connector_models import ConnectorRunORM
    from ums_smart_revenue.db.finance_models import MonthlyChannelRevenueFactORM
    from ums_smart_revenue.db.security_models import AuditLogORM
    from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM

    _seed_owner(pg_url, tenant_id=fresh_tenant)
    factory = build_session_factory(pg_url)
    with factory() as session:  # skipcq: PTC-W0062
        with connector_tenant_context(fresh_tenant, session=session):
            outcome = _run_yt_reporting(session, fresh_tenant)
    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"

    # Read back via the owner engine so the assertions are not themselves
    # subject to the RLS lane under test.
    engine = sa.create_engine(pg_url)
    try:
        with Session(engine) as read:
            run_rows = list(
                read.scalars(
                    select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == fresh_tenant)
                )
            )
            assert len(run_rows) == 1
            assert run_rows[0].status == "SUCCEEDED"

            source_rows = list(
                read.scalars(
                    select(GoogleRevenueSourceRowORM).where(
                        GoogleRevenueSourceRowORM.tenant_id == fresh_tenant
                    )
                )
            )
            assert source_rows, "expected ingested source rows"

            facts = list(
                read.scalars(
                    select(MonthlyChannelRevenueFactORM).where(
                        MonthlyChannelRevenueFactORM.tenant_id == fresh_tenant
                    )
                )
            )
            assert facts, "expected projected revenue facts"
            assert {f.youtube_channel_id for f in facts} == {CHANNEL_ID}

            lifecycles = [
                r.details.get("lifecycle")
                for r in read.scalars(
                    select(AuditLogORM)
                    .where(AuditLogORM.tenant_id == fresh_tenant)
                    .where(AuditLogORM.event_type == "CONNECTOR_JOB_RUN")
                    .order_by(AuditLogORM.created_at, AuditLogORM.id)
                )
            ]
            assert "STARTED" in lifecycles
            assert "FINISHED" in lifecycles
            # The REPORT_IMPORTED fact-audit row proves the normalization
            # write set committed on the platform lane.
            fact_audit = list(
                read.scalars(
                    select(AuditLogORM)
                    .where(AuditLogORM.tenant_id == fresh_tenant)
                    .where(AuditLogORM.event_type == "REPORT_IMPORTED")
                    .where(AuditLogORM.entity_type == "monthly_channel_revenue_fact")
                )
            )
            assert fact_audit, "expected a normalize REPORT_IMPORTED audit row"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Obligation 3: cross-tenant isolation.
# ---------------------------------------------------------------------------
def test_cross_tenant_isolation_under_run_context(pg_url: str, _stub_secret_resolver: None) -> None:
    """Tenant B's rows stay invisible/unwritable while tenant A's run executes."""
    from ums_smart_revenue.db.connector_models import ConnectorRunORM

    tenant_a = uuid4()
    tenant_b = uuid4()
    _seed_owner(pg_url, tenant_id=tenant_a)
    _seed_owner(pg_url, tenant_id=tenant_b)
    factory = build_session_factory(pg_url)
    with factory() as session:  # skipcq: PTC-W0062
        with connector_tenant_context(tenant_a, session=session):
            outcome = _run_yt_reporting(session, tenant_a)
            # While in tenant A's context, tenant B's credential is invisible.
            from ums_smart_revenue.db.security_models import (
                ApiConnectorCredentialORM,
            )

            visible_b = session.scalar(
                select(sa.func.count())
                .select_from(ApiConnectorCredentialORM)
                .where(ApiConnectorCredentialORM.tenant_id == tenant_b)
            )
            assert visible_b == 0
    assert outcome.run is not None and outcome.run.status == "SUCCEEDED"

    # Tenant A produced exactly one run row; tenant B produced none.
    engine = sa.create_engine(pg_url)
    try:
        with Session(engine) as read:
            a_runs = read.scalar(
                select(sa.func.count())
                .select_from(ConnectorRunORM)
                .where(ConnectorRunORM.tenant_id == tenant_a)
            )
            b_runs = read.scalar(
                select(sa.func.count())
                .select_from(ConnectorRunORM)
                .where(ConnectorRunORM.tenant_id == tenant_b)
            )
            assert a_runs == 1
            assert b_runs == 0
    finally:
        engine.dispose()


def test_cross_tenant_platform_write_denied_under_elevation(
    pg_url: str,
) -> None:
    """RLS WITH CHECK rejects an elevated cross-tenant / no-context platform write.

    The central invariant of the lane fix: elevating to ``app_platform`` widens
    only which DB role executes already-trusted writes; it does NOT bypass RLS
    (``app_platform`` is NOBYPASSRLS and the trusted-context policies still
    apply). So inside ``connector_tenant_context(A)`` + ``platform_lane`` a
    platform-only-write INSERT for tenant B is rejected by the WITH CHECK
    policy, and with NO tenant context set the same elevated INSERT is rejected
    too (``app_current_tenant_id()`` is NULL, so the WITH CHECK is never TRUE).
    Mechanics mirror .tmp-review/probe_c_message.py + probe_e_nocontext.py: each
    failed transaction is rolled back before the lane block exits, so the
    clean-exit restore sees no in-flight transaction.
    """
    tenant_a = uuid4()
    tenant_b = uuid4()
    _seed_owner(pg_url, tenant_id=tenant_a, with_channel=False)
    _seed_owner(pg_url, tenant_id=tenant_b, with_channel=False)
    factory = build_session_factory(pg_url)

    insert_close = sa.text(
        "INSERT INTO finance_month_close (tenant_id, month, status) VALUES (:tid, :month, 'OPEN')"
    )

    # (a) cross-tenant write under tenant-A context is rejected by WITH CHECK.
    with factory() as session:  # skipcq: PTC-W0062
        with connector_tenant_context(tenant_a, session=session):
            with platform_lane(session):
                with pytest.raises(Exception) as cross_tenant_exc:
                    session.execute(
                        insert_close,
                        {"tid": tenant_b, "month": "2026-05"},
                    )
                    session.flush()
                # Roll the aborted txn back inside the block so the lane's
                # clean-exit restore sees no active transaction.
                session.rollback()
    assert "row-level security" in str(
        getattr(cross_tenant_exc.value, "orig", cross_tenant_exc.value)
    )

    # (b) with NO tenant context the same elevated INSERT is also rejected.
    with factory() as session:  # skipcq: PTC-W0062
        with platform_lane(session):
            with pytest.raises(Exception) as no_context_exc:
                session.execute(
                    insert_close,
                    {"tid": tenant_a, "month": "2026-06"},
                )
                session.flush()
            session.rollback()
    assert "row-level security" in str(getattr(no_context_exc.value, "orig", no_context_exc.value))

    # Neither rejected INSERT left a durable row (read back via the owner login).
    engine = sa.create_engine(pg_url)
    try:
        with engine.connect() as read:
            leaked = read.scalar(
                sa.text(
                    "SELECT count(*) FROM finance_month_close WHERE month IN ('2026-05', '2026-06')"
                )
            )
            assert leaked == 0
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Obligation 5: projection-failure path persists FAILED + PROJECTION_FAILED.
# ---------------------------------------------------------------------------
def test_projection_failure_rewrites_run_failed_and_persists_audit(
    pg_url: str, fresh_tenant: UUID, _stub_secret_resolver: None
) -> None:
    """A forced normalize error rewrites the run FAILED and persists the audit.

    This is the P2 compounding break: when the fact write denies, the
    projection-failure audit emit also denied (audit_logs is platform-only),
    the FAILED rewrite rolled back, and the durable run stayed SUCCEEDED with
    zero facts. With the platform_lane wiring the rewrite + PROJECTION_FAILED
    audit row commit on the platform lane.
    """
    from unittest.mock import patch

    from ums_smart_revenue.connectors.runs import normalization
    from ums_smart_revenue.db.connector_models import ConnectorRunORM
    from ums_smart_revenue.db.security_models import AuditLogORM

    # Force a deterministic normalize error so the FAILED-rewrite +
    # PROJECTION_FAILED audit path is the thing under test (independent of any
    # data-shape edge case in the normalizer).
    _seed_owner(pg_url, tenant_id=fresh_tenant, with_channel=True)
    factory = build_session_factory(pg_url)

    class _BoomError(RuntimeError):
        pass

    with factory() as session:  # skipcq: PTC-W0062
        with connector_tenant_context(fresh_tenant, session=session):
            with patch.object(
                normalization,
                "GoogleSourceNormalizer",
            ) as normalizer_cls:
                normalizer_cls.return_value.normalize_month.side_effect = _BoomError(
                    "forced normalize failure"
                )
                with pytest.raises(_BoomError, match="forced normalize failure"):
                    _run_yt_reporting(session, fresh_tenant)

    engine = sa.create_engine(pg_url)
    try:
        with Session(engine) as read:
            run_rows = list(
                read.scalars(
                    select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == fresh_tenant)
                )
            )
            assert len(run_rows) == 1
            assert run_rows[0].status == "FAILED"
            assert run_rows[0].error_summary is not None
            assert "normalize failed" in run_rows[0].error_summary

            projection_failed = list(
                read.scalars(
                    select(AuditLogORM)
                    .where(AuditLogORM.tenant_id == fresh_tenant)
                    .where(AuditLogORM.event_type == "CONNECTOR_JOB_RUN")
                )
            )
            lifecycles = [r.details.get("lifecycle") for r in projection_failed]
            assert "PROJECTION_FAILED" in lifecycles
    finally:
        engine.dispose()


def _run_yt_reporting_two_reports_second_parse_fails(session: Session, tenant_id: UUID):
    """Drive ``run_one`` with 2 reports; the parser fails on the second.

    Mirrors the SQLite bucket-B scenario
    (tests/connectors/google/test_orchestrator.py:
    test_bucket_b_parser_error_on_second_report_marks_raw_file_failed...) on
    real RLS-enforced Postgres: the failure recorder (which commits/rolls back
    mid-block and then writes a platform-only FAILED audit edge) must still
    land its row under the persisted elevation.
    """
    from unittest.mock import patch

    from ums_smart_revenue.connectors.google_source_parsers import (
        YouTubeReportingParser as RealParser,
    )
    from ums_smart_revenue.connectors.google_source_parsers.base import (
        ParserError,
    )

    real_parser = RealParser()
    call_state = {"n": 0}

    class _FlakyParser:
        """Pass the first report, raise ParserError on the second."""

        @staticmethod
        def parse(payload, *, tenant_id):
            """Return real rows once, then raise on the second call."""
            call_state["n"] += 1
            if call_state["n"] == 2:
                raise ParserError("simulated parser failure on report 2")
            return list(real_parser.parse(payload, tenant_id=tenant_id))

    header = b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
    csv_a = header + (f"2026-04-01,{CHANNEL_ID},{ACCOUNT_ID},10.000000,USD\n".encode())
    csv_b = header + (f"2026-04-02,{CHANNEL_ID},{ACCOUNT_ID},20.000000,USD\n".encode())

    with (
        patch("ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient") as yt_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator._parser_for_connector",
            return_value=_FlakyParser(),
        ),
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None
        client = yt_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"},
            {"id": "job-2", "reportTypeId": "content_owner_basic_a3"},
        ]
        reports_by_job = {
            "job-1": [{"id": "r1", "downloadUrl": "https://yt/r1"}],
            "job-2": [{"id": "r2", "downloadUrl": "https://yt/r2"}],
        }
        client.list_reports_for_month.side_effect = lambda *, account_id, job_id, report_month: (
            reports_by_job[job_id]
        )
        bytes_by_url = {"https://yt/r1": csv_a, "https://yt/r2": csv_b}
        client.fetch_report.side_effect = lambda *, download_url: bytes_by_url[download_url]
        store: dict[str, bytes] = {}
        backend = local_cls.return_value
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]
        return run_one(
            session,
            tenant_id=tenant_id,
            connector_key=YT_REPORTING_KEY,
            account_id=ACCOUNT_ID,
            report_month=REPORT_MONTH,
        )


# ---------------------------------------------------------------------------
# Obligation 6 (Finding 5): per-report failure path on PG keeps the FAILED
# raw_file row AND its platform-only FAILED audit edge under the elevation.
# ---------------------------------------------------------------------------
def test_per_report_failure_persists_failed_raw_file_and_audit_on_postgres(
    pg_url: str, fresh_tenant: UUID, _stub_secret_resolver: None
) -> None:
    """Bucket-B on PG: report #2 parser fails -> PARTIAL + FAILED edge durable.

    The failure recorder (_record_live_report_failure) rolls back / commits
    mid-block and then emits the FAILED raw-file audit edge -- a platform-only
    write (audit_logs). A lane regression there would silently drop that edge
    while the run still finished PARTIAL, so this pins all three: run status
    PARTIAL, the failed raw_file row status FAILED, and the FAILED audit row
    present for the context tenant.
    """
    from ums_smart_revenue.db.connector_models import ConnectorRunORM
    from ums_smart_revenue.db.report_models import RawReportFileORM
    from ums_smart_revenue.db.security_models import AuditLogORM

    _seed_owner(pg_url, tenant_id=fresh_tenant, with_channel=True)
    factory = build_session_factory(pg_url)
    with factory() as session:  # skipcq: PTC-W0062
        with connector_tenant_context(fresh_tenant, session=session):
            outcome = _run_yt_reporting_two_reports_second_parse_fails(session, fresh_tenant)
    assert outcome.run is not None
    assert outcome.run.status == "PARTIAL"
    assert outcome.counts["reports_failed"] == 1
    assert outcome.counts["reports_succeeded"] == 1

    engine = sa.create_engine(pg_url)
    try:
        with Session(engine) as read:
            run_rows = list(
                read.scalars(
                    select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == fresh_tenant)
                )
            )
            assert len(run_rows) == 1
            assert run_rows[0].status == "PARTIAL"

            raw_files = list(
                read.scalars(
                    select(RawReportFileORM).where(RawReportFileORM.tenant_id == fresh_tenant)
                )
            )
            statuses = {rf.report_type: rf.parse_status for rf in raw_files}
            assert statuses.get("channel_basic_a2") == "PARSED"
            assert statuses.get("content_owner_basic_a3") == "FAILED"

            # The platform-only FAILED audit edge (the row a lane regression
            # would silently lose) must exist for the context tenant.
            failed_edges = list(
                read.scalars(
                    select(AuditLogORM)
                    .where(AuditLogORM.tenant_id == fresh_tenant)
                    .where(AuditLogORM.event_type == "REPORT_IMPORTED")
                    .where(AuditLogORM.entity_type == "raw_report_file")
                )
            )
            failed_lifecycles = [r.details.get("lifecycle") for r in failed_edges]
            assert "FAILED" in failed_lifecycles
    finally:
        engine.dispose()
