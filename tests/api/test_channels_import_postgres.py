"""Postgres-tier proof for POST /channels/import: RLS isolation + atomicity.

The SQLite tier (``tests/api/test_channels_import_api.py``) cannot settle two
questions, because on SQLite every lane shares one session
(``_sqlite_platform_session_from_request``) and RLS does not exist at all:

1. **Tenant isolation.** ``youtube_channels`` carries ``FORCE ROW LEVEL
   SECURITY``; an imported roster must be invisible to another tenant's lane.
   The bare ``SELECT`` here has no ``WHERE tenant_id`` on purpose, mirroring
   ``tests/tenancy/test_isolation.py`` — RLS is the only filter under test.
2. **All-or-nothing including audit.** The import's audit sink
   (``PlatformLaneAuditSink``) writes ``audit_logs`` through the SAME tenant
   session as the channel writes, elevating to ``app_platform`` per append
   because ``audit_logs`` is platform-only writable. Channel rows and audit
   rows therefore share ONE transaction: a mid-apply failure rolls everything
   back, and a tenant commit that fails to persist takes its audit rows with
   it instead of leaving an independently committed platform session claiming
   an import that never happened.

``require_postgres_url()`` raises (never skips) when ``UMS_TEST_DATABASE_URL``
is unset, preserving the repository's no-skip policy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Annotated
from unittest.mock import patch
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.api.dependencies import current_db_session
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import AuditRecord
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.db.finance_models import FinanceMonthCloseORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.db.session import (
    build_platform_session_factory,
    build_session_factory,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry
from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry
from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry
from ums_smart_revenue.org.sql_channel_registry import SqlAlchemyChannelRegistry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
SECOND_ID = "UC3Dci3BzZXDo4jw4dU8KqWg"
CONTENT_OWNER = "TestOwnerAAAAAAAAAAAAA"
MALFORMED_ID = "not-a-channel-id"
GROUP_ID = "pg-import-group-1"
SECOND_GROUP_ID = "pg-import-group-2"

# Tenant A is the bootstrap UMS tenant every trusted-header request binds to.
# Tenant B is the isolation counterpart, seeded exactly as in tests/tenancy.
TENANT_A = UMS_TENANT_ID
TENANT_B = "00000000-0000-0000-0000-000000000002"

DEFAULT_HEADER = "youtube_channel_id,channel_name,view_revenue"
GROUP_HEADER = "youtube_channel_id,channel_name,group_id,view_revenue"

_UPGRADED_URLS: set[str] = set()


def _alembic_config(url: str) -> Config:
    """Build an Alembic config bound to ``url`` without an ini file.

    Mirrors the no-ini pattern in ``tests/tenancy/test_force_rls.py``: env.py
    only touches the logging tree when ``config_file_name`` is set, so
    configuring ``script_location`` + ``sqlalchemy.url`` directly avoids
    silencing other tests' ``caplog`` for the rest of the session.
    """
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", "backend/ums_smart_revenue/db/alembic")
    return cfg


def _ensure_upgraded(url: str) -> None:
    """Migrate the disposable Postgres database to head once per session."""
    if url in _UPGRADED_URLS:
        return
    command.upgrade(_alembic_config(url), "head")
    _UPGRADED_URLS.add(url)


def _tenant(tenant_id: str, slug: str) -> Tenant:
    """Build an ACTIVE tenant object for lane-scoped reads and requests."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Tenant(
        id=UUID(tenant_id),
        slug=slug,
        display_name=slug.upper(),
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def _seed_isolation_tenant(engine: sa.Engine) -> None:
    """Seed tenant B as the schema owner so its lane has a valid context."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, display_name, primary_currency) "
                "VALUES (:id, 'rotana', 'Rotana', 'USD') ON CONFLICT DO NOTHING"
            ),
            {"id": TENANT_B},
        )


def _purge_test_rows(engine: sa.Engine) -> None:
    """Remove this module's channel/group rows for every tenant.

    Runs as the ``postgres`` superuser connection, which bypasses RLS, so the
    purge reaches rows written under either tenant lane and reruns stay
    idempotent on the shared clean-room container.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM channel_group_members WHERE channel_id IN "
                "(SELECT id FROM youtube_channels WHERE youtube_channel_id = ANY(:ids))"
            ),
            {"ids": [CHANNEL_ID, SECOND_ID]},
        )
        conn.execute(
            sa.text("DELETE FROM channel_groups WHERE cms_group_id = ANY(:groups)"),
            {"groups": [GROUP_ID, SECOND_GROUP_ID]},
        )
        conn.execute(
            sa.text("DELETE FROM youtube_channels WHERE youtube_channel_id = ANY(:ids)"),
            {"ids": [CHANNEL_ID, SECOND_ID]},
        )


@pytest.fixture(scope="module")
def pg_url() -> str:
    """Resolve the disposable Postgres URL, migrated to head with tenant B."""
    url = require_postgres_url()
    _ensure_upgraded(url)
    engine = sa.create_engine(url)
    try:
        _seed_isolation_tenant(engine)
    finally:
        engine.dispose()
    return url


@pytest.fixture
def owner_engine(pg_url: str) -> Iterator[sa.Engine]:
    """Yield an RLS-bypassing owner engine, purging test rows either side."""
    engine = sa.create_engine(pg_url)
    try:
        _purge_test_rows(engine)
        yield engine
        _purge_test_rows(engine)
    finally:
        engine.dispose()


@pytest.fixture
def client(pg_url: str) -> TestClient:
    """Build a Postgres-backed trusted-header client for tenant A."""
    return TestClient(create_app(database_url=pg_url, authz_source="headers"))


def auth_headers() -> dict[str, str]:
    """Build global super-owner headers accepted by the trusted gateway."""
    return {
        "x-user-id": "user-1",
        "x-user-email": "user@example.com",
        "x-role": "super_owner",
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def import_csv(*rows: str, header: str = DEFAULT_HEADER) -> str:
    """Assemble a CSV body from a header line and its data rows."""
    return "\n".join([header, *rows]) + "\n"


def post_import(client: TestClient, csv_text: str, **overrides: object):
    """POST one roster CSV to /channels/import as a global admin."""
    form: dict[str, object] = {
        "content_owner_id": CONTENT_OWNER,
        "cms_status": "INSIDE_CMS",
        "dry_run": "false",
        "reason": "Postgres-tier roster load",
    }
    form.update(overrides)
    return client.post(
        "/channels/import",
        headers=auth_headers(),
        files={"file": ("roster.csv", csv_text, "text/csv")},
        data=form,
    )


def _audit_log_count(engine: sa.Engine) -> int:
    """Count every audit_logs row through the RLS-bypassing owner engine."""
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT COUNT(*) FROM audit_logs")).scalar_one()


def _tenant_audit_log_count(engine: sa.Engine, tenant_id: str) -> int:
    """Count one tenant's audit_logs rows through the owner engine."""
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT COUNT(*) FROM audit_logs WHERE tenant_id = :tenant"),
            {"tenant": tenant_id},
        ).scalar_one()


def _channel_ids_visible_to_tenant(url: str, tenant_id: str, slug: str) -> set[str]:
    """Return the channel ids one tenant lane can see with NO tenant filter.

    The ``SELECT`` deliberately omits ``WHERE tenant_id`` so the only thing
    that can hide a row is the ``youtube_channels`` RLS policy, not the
    application-level filter in ``SqlAlchemyChannelRegistry``.
    """
    factory = build_session_factory(url)
    token = TENANT_CTX.set(_tenant(tenant_id, slug))
    try:
        with factory() as session:
            rows = session.execute(sa.text("SELECT youtube_channel_id FROM youtube_channels"))
            return set(rows.scalars())
    finally:
        TENANT_CTX.reset(token)


def _channel_row(engine: sa.Engine, channel_id: str) -> sa.Row | None:
    """Return the stored inventory columns for one channel, or None."""
    with engine.connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT tenant_id, channel_name, cms_status, content_owner_id, "
                "revenue_required FROM youtube_channels WHERE youtube_channel_id = :id"
            ),
            {"id": channel_id},
        ).first()


class _FailingGroupStore:
    """Group store that delegates until the Nth ``get_group_by_cms_id`` call.

    Used to force a failure AFTER the import has already written channel rows
    and (platform-lane elevated) audit rows on the tenant session, which is
    the only way to exercise the mid-apply rollback path (the 422
    all-or-nothing check happens during planning, before any write).
    """

    def __init__(self, inner: SqlAlchemyChannelGroupRegistry, *, fail_on_call: int) -> None:
        """Wrap the real store and arm the failure for one lookup call."""
        self._inner = inner
        self._fail_on_call = fail_on_call
        self.calls = 0

    def transaction(self) -> AbstractContextManager[None]:
        """Delegate the boundary to the real store — this wrapper adds no writes.

        The apply enters ``groups.transaction()`` before anything else runs
        (review #184, C2), so a wrapper without this method would fail at the
        boundary's entry and this test's armed MID-apply failure would never
        be reached at all.
        """
        return self._inner.transaction()

    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        """Delegate the lookup until the armed call, then raise."""
        self.calls += 1
        if self.calls >= self._fail_on_call:
            raise RuntimeError("channel group store unavailable mid-apply")
        return self._inner.get_group_by_cms_id(cms_group_id, for_update=for_update)

    def list_archived_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Delegate the bulk archived-key lookup to the real store."""
        return self._inner.list_archived_cms_group_ids(cms_group_ids)

    def list_foreign_owner_cms_group_ids(
        self, cms_group_ids: set[str], *, content_owner_id: str
    ) -> set[str]:
        """Delegate the bulk cross-owner-key lookup to the real store."""
        return self._inner.list_foreign_owner_cms_group_ids(
            cms_group_ids, content_owner_id=content_owner_id
        )

    def list_adoptable_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Delegate the bulk owner-NULL-key lookup to the real store."""
        return self._inner.list_adoptable_cms_group_ids(cms_group_ids)

    def list_owned_cms_group_ids(
        self, cms_group_ids: set[str], *, content_owner_id: str
    ) -> set[str]:
        """Delegate the bulk this-owner-key lookup to the real store.

        Uncounted like its three bulk siblings: only the per-row APPLY lookup
        (``get_group_by_cms_id``) is armed, so counting a PLANNING read here
        would fire the failure before any write and turn this file's
        mid-apply rollback proof vacuous.
        """
        return self._inner.list_owned_cms_group_ids(
            cms_group_ids, content_owner_id=content_owner_id
        )

    def create_group(self, **kwargs: object) -> ChannelGroupEntry:
        """Delegate group creation to the real store."""
        return self._inner.create_group(**kwargs)

    def add_members(self, **kwargs: object) -> ChannelGroupEntry:
        """Delegate membership attachment to the real store."""
        return self._inner.add_members(**kwargs)


def test_import_persists_channels_on_postgres(client: TestClient, owner_engine: sa.Engine) -> None:
    """A two-row apply persists both channels with the imported inventory."""
    before_tenant = _tenant_audit_log_count(owner_engine, TENANT_A)
    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,Yes",
            f"{SECOND_ID},Beta News,No",
        ),
    )

    assert response.status_code == 200, response.text
    assert response.json()["counts"]["CREATE"] == 2
    # The single-transaction audit path COMMITS on success: two CHANNEL_CREATED
    # rows plus the CHANNEL_IMPORTED summary must be durably visible.
    assert _tenant_audit_log_count(owner_engine, TENANT_A) == before_tenant + 3

    listing = client.get("/channels", headers=auth_headers())
    assert listing.status_code == 200, listing.text
    by_id = {entry["youtube_channel_id"]: entry for entry in listing.json()}
    assert by_id[CHANNEL_ID]["channel_name"] == "Alpha News"
    assert by_id[CHANNEL_ID]["cms_status"] == "INSIDE_CMS"
    assert by_id[CHANNEL_ID]["content_owner_id"] == CONTENT_OWNER
    assert by_id[CHANNEL_ID]["revenue_required"] is True
    assert by_id[SECOND_ID]["cms_status"] == "INSIDE_CMS"
    assert by_id[SECOND_ID]["content_owner_id"] == CONTENT_OWNER
    assert by_id[SECOND_ID]["revenue_required"] is False

    stored = _channel_row(owner_engine, CHANNEL_ID)
    assert stored is not None
    assert str(stored.tenant_id) == TENANT_A
    assert stored.cms_status == "INSIDE_CMS"
    assert stored.content_owner_id == CONTENT_OWNER


def test_imported_channels_are_tenant_isolated(
    client: TestClient, owner_engine: sa.Engine, pg_url: str
) -> None:
    """A roster imported by tenant A is invisible to tenant B's lane and API."""
    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))
    assert response.status_code == 200, response.text

    # Database boundary: bare SELECT, no WHERE tenant_id — RLS is the filter.
    assert CHANNEL_ID in _channel_ids_visible_to_tenant(pg_url, TENANT_A, "ums")
    assert CHANNEL_ID not in _channel_ids_visible_to_tenant(pg_url, TENANT_B, "rotana")

    # API boundary: the same app serving tenant B must not list the channel.
    with patch("ums_smart_revenue.app._bootstrap_tenant", lambda: _tenant(TENANT_B, "rotana")):
        other_tenant_listing = client.get("/channels", headers=auth_headers())
    assert other_tenant_listing.status_code == 200, other_tenant_listing.text
    assert all(entry["youtube_channel_id"] != CHANNEL_ID for entry in other_tenant_listing.json())

    # Control: the row really is there for tenant A (the absence above is real).
    own_listing = client.get("/channels", headers=auth_headers())
    assert own_listing.status_code == 200, own_listing.text
    assert any(entry["youtube_channel_id"] == CHANNEL_ID for entry in own_listing.json())


def test_failed_apply_rolls_back_every_row_on_postgres(
    client: TestClient, owner_engine: sa.Engine
) -> None:
    """One malformed row rejects the file and leaves the valid row unwritten."""
    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,Yes",
            f"{MALFORMED_ID},Beta News,Yes",
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["counts"]["ERROR"] == 1
    assert _channel_row(owner_engine, CHANNEL_ID) is None


def test_failed_apply_leaves_no_audit_rows_on_postgres(
    client: TestClient, owner_engine: sa.Engine
) -> None:
    """A rejected import writes nothing on the separate platform audit session."""
    before = _audit_log_count(owner_engine)

    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,Yes",
            f"{MALFORMED_ID},Beta News,Yes",
        ),
    )

    assert response.status_code == 422, response.text
    assert _audit_log_count(owner_engine) == before


def test_mid_apply_failure_rolls_back_channels_and_audit_on_postgres(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """A failure after the first row rolls back channels — and audit never lands.

    The armed store raises on the SECOND group lookup, so by the time the
    request fails the tenant session already holds two channel INSERTs and
    row 1's group writes. The shared transaction must roll back: no channel
    row afterwards. ``stores[0].calls == 2`` is the mid-apply proof — the
    failure came after real writes, not before them.

    The AUDIT half changed shape with the C2 boundary (review #184): the
    apply now buffers audit records and flushes them only after both passes
    succeed, so a mid-apply failure no longer even transiently INSERTs audit
    rows — ``audit_counts_in_flight == []`` is the new contract, strictly
    stronger than "inserted then rolled back". The anti-vacuity guard moves
    to the SUCCESS run at the end: the same roster through the same patched
    append, unarmed, must record one in-transaction count per audit row and
    commit exactly those rows — proving the probe was live all along and the
    flush really writes the platform-lane audit trail inside the same
    request transaction.
    """
    app = create_app(database_url=pg_url, authz_source="headers")
    stores: list[_FailingGroupStore] = []
    audit_counts_in_flight: list[int] = []
    original_append = SqlAlchemyAuditSink.append

    def recording_append(sink: SqlAlchemyAuditSink, record: AuditRecord) -> None:
        """Append through the real sink, then read the in-transaction count."""
        original_append(sink, record)
        audit_counts_in_flight.append(
            sink._session.execute(  # noqa: SLF001 — test probe of the audit lane
                sa.text("SELECT COUNT(*) FROM audit_logs")
            ).scalar_one()
        )

    def failing_group_store(
        session: Annotated[Session, Depends(current_db_session)],
    ) -> _FailingGroupStore:
        """Provide a group store armed to fail on row 2's APPLY lookup.

        Planning vets group keys via the bulk list_archived_cms_group_ids
        (uncounted); the apply looks each row's group up per row (row 1 =
        call 1, row 2 = call 2). Arming call 2 forces the failure after row
        1's channel+group writes and row 2's channel write have already been
        flushed.
        """
        store = _FailingGroupStore(SqlAlchemyChannelGroupRegistry(session), fail_on_call=2)
        stores.append(store)
        return store

    app.dependency_overrides[sql_group_registry_from_session] = failing_group_store
    failing_client = TestClient(app, raise_server_exceptions=False)
    before = _audit_log_count(owner_engine)
    before_tenant = _tenant_audit_log_count(owner_engine, TENANT_A)

    with patch.object(SqlAlchemyAuditSink, "append", recording_append):
        response = post_import(
            failing_client,
            import_csv(
                f"{CHANNEL_ID},Alpha News,{GROUP_ID},Yes",
                f"{SECOND_ID},Beta News,{SECOND_GROUP_ID},Yes",
                header=GROUP_HEADER,
            ),
        )

        assert response.status_code == 500, response.text
        assert stores and stores[0].calls == 2, (
            "the store must have failed mid-apply, not before it"
        )
        # The C2 buffer: NOT ONE audit INSERT ran before the failure. The
        # records existed only in the apply's buffer, which the raise
        # discarded — audit rows can no longer even transiently describe an
        # import that did not happen.
        assert audit_counts_in_flight == []
        assert _channel_row(owner_engine, CHANNEL_ID) is None
        assert _channel_row(owner_engine, SECOND_ID) is None
        assert _audit_log_count(owner_engine) == before

        # Anti-vacuity, still under the patch: disarm the failure and run a
        # one-row import through the SAME app. Every flushed audit row must
        # pass through the probe (counts ascend one per append, in the same
        # transaction the channel write holds) and then commit — the []
        # above was the buffer's doing, not a dead instrument.
        del failing_client.app.dependency_overrides[sql_group_registry_from_session]
        success = post_import(
            failing_client,
            import_csv(f"{CHANNEL_ID},Alpha News,{GROUP_ID},Yes", header=GROUP_HEADER),
        )
        assert success.status_code == 200, success.text
        assert audit_counts_in_flight == [
            before_tenant + offset
            for offset in range(1, len(audit_counts_in_flight) + 1)
        ]
        # At least the row's CHANNEL_CREATED, one GROUP_UPDATED, and the
        # CHANNEL_IMPORTED summary.
        assert len(audit_counts_in_flight) >= 3
    assert _audit_log_count(owner_engine) == before + len(audit_counts_in_flight)


def test_drifted_pre_state_rolls_the_bound_apply_back_on_postgres(
    client: TestClient, owner_engine: sa.Engine
) -> None:
    """A plan-bound apply whose row drifted leaves the CONCURRENT value stored.

    This is the durable half of the opt-in guard (review #184). The SQLite
    tier can show the 409 and the missing audit event, but its in-memory
    registry commits as it goes, so only here can "nothing was written" be
    read back from the database.

    The drift is a genuine committed write by another session: the patched
    ``update_inventory`` runs the owner-engine UPDATE BEFORE delegating, so it
    lands while the import holds no lock on the row, and the registry's own
    ``FOR UPDATE`` re-read is what observes it. Asserting the stored name is
    the OUTSIDE writer's — not the roster's — is what distinguishes a rollback
    from "the import wrote its value anyway".
    """
    seed = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))
    assert seed.status_code == 200, seed.text

    body = import_csv(f"{CHANNEL_ID},Renamed By Import,Yes")
    preview = post_import(client, body, dry_run="true").json()
    assert preview["rows"][0]["changes"]["channel_name"] == {
        "from": "Alpha News",
        "to": "Renamed By Import",
    }

    original_update = SqlAlchemyChannelRegistry.update_inventory
    drifted: list[str] = []

    def drift_then_update(
        registry: SqlAlchemyChannelRegistry,
        *,
        youtube_channel_id: str,
        channel_name: str,
        cms_status: str,
        content_owner_id: str | None,
        revenue_required: bool,
        require_pre_state: Callable[[ChannelRegistryEntry], None] | None = None,
    ) -> tuple[ChannelRegistryEntry, ChannelRegistryEntry]:
        """Commit an outside rename once, then run the real locked write."""
        if not drifted:
            drifted.append(youtube_channel_id)
            with owner_engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "UPDATE youtube_channels SET channel_name = :name "
                        "WHERE youtube_channel_id = :id"
                    ),
                    {"name": "Renamed Outside The Import", "id": youtube_channel_id},
                )
        return original_update(
            registry,
            youtube_channel_id=youtube_channel_id,
            channel_name=channel_name,
            cms_status=cms_status,
            content_owner_id=content_owner_id,
            revenue_required=revenue_required,
            require_pre_state=require_pre_state,
        )

    before = _audit_log_count(owner_engine)
    with patch.object(SqlAlchemyChannelRegistry, "update_inventory", drift_then_update):
        response = post_import(client, body, expected_plan_fingerprint=preview["plan_fingerprint"])

    assert response.status_code == 409, response.text
    assert drifted == [CHANNEL_ID], "the concurrent write must precede the locked read"
    assert "changed during the import" in response.json()["detail"]
    stored = _channel_row(owner_engine, CHANNEL_ID)
    assert stored is not None
    assert stored.channel_name == "Renamed Outside The Import"
    assert _audit_log_count(owner_engine) == before


def test_bound_apply_commits_when_the_pre_state_held_on_postgres(
    client: TestClient, owner_engine: sa.Engine
) -> None:
    """Anti-vacuity for the guard: an undisturbed bound apply still commits.

    Without this, the 409 above would also be produced by a guard that
    rejected every plan-bound apply.
    """
    seed = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))
    assert seed.status_code == 200, seed.text

    body = import_csv(f"{CHANNEL_ID},Renamed By Import,Yes")
    preview = post_import(client, body, dry_run="true").json()

    response = post_import(client, body, expected_plan_fingerprint=preview["plan_fingerprint"])

    assert response.status_code == 200, response.text
    stored = _channel_row(owner_engine, CHANNEL_ID)
    assert stored is not None
    assert stored.channel_name == "Renamed By Import"


def test_locked_month_revenue_flip_is_rejected_409_on_postgres(
    client: TestClient, owner_engine: sa.Engine, pg_url: str
) -> None:
    """Enabling view_revenue while a LOCKED month lacks facts is 409, not applied.

    The registry guard (ChannelRevenueRequirementLockedMonthError) fires
    mid-apply; the route translates it to 409 and the single-transaction
    wiring rolls the whole import back — flag unchanged, zero audit rows.
    """
    lock_month = "2099-09"
    with owner_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM finance_month_close WHERE month = :m"), {"m": lock_month})
    factory = build_platform_session_factory(pg_url)
    token = TENANT_CTX.set(_tenant(TENANT_A, "ums"))
    try:
        with factory() as session:
            session.add(FinanceMonthCloseORM(month=lock_month, status="LOCKED"))
            session.add(
                YouTubeChannelORM(
                    id=UUID("00000000-0000-0000-0000-00000000e159"),
                    youtube_channel_id=CHANNEL_ID,
                    channel_name="Alpha News",
                    cms_status="INSIDE_CMS",
                    content_owner_id=CONTENT_OWNER,
                    revenue_required=False,
                    revenue_source_status="PERFORMANCE_ONLY",
                    active=True,
                )
            )
            session.commit()
    finally:
        TENANT_CTX.reset(token)
    before = _audit_log_count(owner_engine)

    try:
        response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))

        assert response.status_code == 409, response.text
        assert "revenue_required cannot be enabled" in response.json()["detail"]
        assert lock_month in response.json()["detail"]
        stored = _channel_row(owner_engine, CHANNEL_ID)
        assert stored is not None and stored.revenue_required is False
        assert _audit_log_count(owner_engine) == before
    finally:
        with owner_engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM finance_month_close WHERE month = :m"), {"m": lock_month}
            )


def test_tenant_commit_failure_persists_no_audit_rows_on_postgres(
    pg_url: str, owner_engine: sa.Engine
) -> None:
    """Audit rows share the tenant transaction's fate: no commit, no audit.

    This is the commit-order race the review flagged: the handler succeeds,
    then the tenant transaction fails to persist (e.g. a serialization or
    connection error at commit time — simulated here by a dependency that
    rolls back where the wired dependency would commit). Because the import's
    audit rows join the SAME transaction as the channel writes, they must
    vanish with them. Before the single-session fix, the independently
    committed platform audit session had already durably recorded
    CHANNEL_CREATED + CHANNEL_IMPORTED rows for an import that never happened.
    """
    app = create_app(database_url=pg_url, authz_source="headers")
    factory = build_session_factory(pg_url)

    def rollback_instead_of_commit() -> Iterator[Session]:
        """Yield a real tenant-lane session but never let it commit."""
        with factory() as session:
            try:
                yield session
            finally:
                session.rollback()

    app.dependency_overrides[current_db_session] = rollback_instead_of_commit
    client = TestClient(app)
    before = _audit_log_count(owner_engine)

    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))

    # The handler itself succeeded — only the commit was lost.
    assert response.status_code == 200, response.text
    assert _channel_row(owner_engine, CHANNEL_ID) is None
    assert _audit_log_count(owner_engine) == before


def test_duplicate_cms_group_key_is_a_typed_conflict_on_postgres(
    owner_engine: sa.Engine,
) -> None:
    """The real PG unique constraint surfaces as the typed conflict, not a 500.

    Exercises the psycopg ``diag.constraint_name`` branch of
    ``_is_duplicate_cms_group_integrity_error`` — the SQLite-tier test only
    covers the message-text fallback, so a renamed constraint would otherwise
    turn the documented retryable 409 back into a raw IntegrityError.
    Nothing commits: the rollback discards both inserts.
    """
    from ums_smart_revenue.org.channel_groups import ChannelGroupConflictError

    with Session(owner_engine) as session:
        store = SqlAlchemyChannelGroupRegistry(session)
        store.create_group(
            name="PG TV", group_type="SECTOR", channel_ids=[], cms_group_id="pg-cms-dup"
        )

        with pytest.raises(ChannelGroupConflictError, match="pg-cms-dup"):
            store.create_group(
                name="PG TV Again",
                group_type="SECTOR",
                channel_ids=[],
                cms_group_id="pg-cms-dup",
            )

        session.rollback()


def test_membership_writers_serialize_on_the_group_row_on_postgres(
    owner_engine: sa.Engine,
) -> None:
    """remove_member blocks on the parent-group row lock the import holds.

    The import's skip-the-add decision reads membership under
    ``get_group_by_cms_id(for_update=True)``; every membership writer must
    take the SAME parent-row lock or a concurrent ``remove_member`` could
    commit mid-import and silently void the roster's requested membership.
    Proven by lock wait: session 2's remove times out while session 1 holds
    the row (review #159 r3713841264).
    """
    from sqlalchemy.exc import OperationalError

    with Session(owner_engine) as seed_session:
        seed_store = SqlAlchemyChannelGroupRegistry(seed_session)
        seed_session.execute(
            sa.text(
                "INSERT INTO youtube_channels "
                "(id, tenant_id, youtube_channel_id, channel_name, cms_status, "
                " revenue_required, revenue_source_status, active) "
                "VALUES (gen_random_uuid(), :tenant, :cid, 'Lock Test', 'INSIDE_CMS', "
                " FALSE, 'PERFORMANCE_ONLY', TRUE)"
            ),
            {"tenant": "00000000-0000-0000-0000-000000000001", "cid": CHANNEL_ID},
        )
        seed_store.create_group(
            name="Lock Group",
            group_type="SECTOR",
            channel_ids=[CHANNEL_ID],
            cms_group_id=GROUP_ID,
        )
        seed_session.commit()

    holder = Session(owner_engine)
    waiter = Session(owner_engine)
    try:
        holder_store = SqlAlchemyChannelGroupRegistry(holder)
        locked = holder_store.get_group_by_cms_id(GROUP_ID, for_update=True)
        assert locked is not None and CHANNEL_ID in locked.channel_ids

        waiter.execute(sa.text("SET LOCAL lock_timeout = '500ms'"))
        waiter_store = SqlAlchemyChannelGroupRegistry(waiter)
        with pytest.raises(OperationalError):
            waiter_store.remove_member(group_id=locked.id, channel_id=CHANNEL_ID)
    finally:
        waiter.rollback()
        waiter.close()
        holder.rollback()
        holder.close()
