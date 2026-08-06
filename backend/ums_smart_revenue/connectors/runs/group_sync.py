# ============================================================================
# Purpose: Compose one CMS group-sync run WITHOUT HTTP — resolve credentials,
#   fetch the CMS snapshot, plan against stored state, and either preview
#   (dry-run) or apply. The manual route (api/channels.py) and the scheduled
#   worker (Sched 2's executor) both drive grouping through THIS function, so
#   the resolve/transaction-split/fetch/plan/refuse/apply sequence and the audit
#   ownership live in one place instead of being reimplemented per caller.
# Database/ORM: reads ApiConnectorCredentialORM via the injected resolver;
#   ChannelGroupORM/ChannelGroupMemberORM via the group store and
#   YouTubeChannelORM via the channel registry (through plan/apply); AuditLogORM
#   rows via the caller's AuditSink, inside the caller's single transaction.
# Standards: NEVER raises HTTPException — typed domain errors propagate and the
#   caller maps them. A credential-layer GoogleConnectorError stays raw; a fetch
#   failure is wrapped in GroupSyncFetchError so the two stay distinguishable
#   (503 vs 502); the foreign-owner refusal is a typed GroupSyncConflictRefused-
#   Error carrying the route's exact 409 detail. The run-level GROUPS_SYNCED
#   summary is the CALLER's to write, never this module's: per-group
#   GROUP_UPDATED rows come from apply_group_sync; the summary is a governance
#   event each caller audits by its own rule.
# Blast Radius: Group naming/membership/active state and the audit trail. No
#   channel rows are ever created here; no finance totals.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> the manual sync route
#     that authorizes, validates, calls this, maps errors, writes the summary.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     resolve_connector_credentials (the default injected resolver).
#   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py ->
#     plan_group_sync_with_stores + apply_group_sync.
#   - File: backend/ums_smart_revenue/connectors/google/youtube_groups_client.py
#     -> the live client the default factory builds.
# ============================================================================
"""Compose a CMS group-sync run (credential -> fetch -> plan -> apply) for HTTP and jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import AuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.connectors.google.errors import GoogleConnectorError
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.youtube_groups_client import YouTubeGroupsClient
from ums_smart_revenue.connectors.keys import YOUTUBE_ANALYTICS_CONNECTOR
from ums_smart_revenue.connectors.runs.orchestrator import resolve_connector_credentials
from ums_smart_revenue.org.channel_group_sync import (
    CmsGroupSnapshot,
    GroupSyncOutcome,
    GroupSyncPlan,
)
from ums_smart_revenue.org.channel_group_sync_apply import (
    GroupSyncExecution,
    apply_group_sync,
    plan_group_sync_with_stores,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistryStore
from ums_smart_revenue.org.channel_registry import ChannelRegistryStore

# A credential resolver has the shape of ``resolve_connector_credentials``
# (keyword-only session/tenant/connector/account -> Credentials). It is injected
# so the manual route can pass its OWN module attribute — which its tests patch
# via ``patch("...api.channels.resolve_connector_credentials")`` — while job
# callers rely on the default and this module never imports ``api``.
CredentialResolver = Callable[..., Credentials]

# The production factory used by both the manual route and the scheduled worker.
GroupsClientFactory = Callable[[Credentials], YouTubeGroupsClient]


@dataclass(frozen=True)
class GroupSyncRunResult:
    """The outcome of one composed sync run.

    ``execution`` is ``None`` exactly for a dry run (preview only, already rolled
    back); an apply carries the write boundary's record. The caller renders its
    response from ``plan`` for a preview and from ``execution`` for an apply,
    and — for an apply — writes the run-level GROUPS_SYNCED summary itself.
    """

    plan: GroupSyncPlan
    execution: GroupSyncExecution | None


class GroupSyncFetchError(Exception):
    """The CMS fetch phase failed.

    Wraps the underlying ``GoogleConnectorError`` raised while listing groups or
    group items so a caller can tell a FETCH failure (the route maps it to 502)
    apart from a credential-resolution ``GoogleConnectorError`` (mapped to 503).
    Deliberately NOT a ``GoogleConnectorError`` subclass: after this extraction
    both phases propagate from ``run_group_sync``, and keeping the fetch failure
    off that tree is what lets the route's broad ``except GoogleConnectorError``
    mean "credential layer" unambiguously.
    """

    def __init__(self, *, inner: GoogleConnectorError) -> None:
        """Carry the underlying connector error (also chained via ``raise from``)."""
        super().__init__(f"cms group fetch failed: {type(inner).__name__}")
        self.inner = inner


class GroupSyncConflictRefusedError(Exception):
    """A plan carried CMS keys already owned by a DIFFERENT content owner.

    The foreign-owner refusal the mandatory dry run already previewed as
    CONFLICT. Carries the exact operator-facing ``detail`` the route surfaces as
    a 409, so the HTTP contract is unchanged by the extraction.
    """

    def __init__(self, *, detail: str) -> None:
        """Carry the exact 409 ``detail`` string for the caller to surface."""
        super().__init__(detail)
        self.detail = detail


def default_groups_client_factory(credentials: Credentials) -> YouTubeGroupsClient:
    """Build a live groups client from resolved credentials.

    The production client factory, used by both the manual sync route (via
    ``current_groups_client_factory``) and the scheduled worker. It lives in
    ``connectors.runs`` so job code can build the real client without importing
    ``api`` — the layering rule this module exists to keep.
    """
    return YouTubeGroupsClient(http=GoogleHttpClient(credentials=credentials))


# ============================================================================
# Purpose: HTTP-free core that resolves one content owner's CMS groups,
#   fetches the remote grouping state, plans against stored state, and
#   previews (dry run) or applies the changes -- the ONE implementation
#   shared by the manual sync route and the scheduled executor worker.
# Database/ORM: drives the channel registry, group registry, and audit sink
#   stores on the CALLER's session; the caller owns the transaction boundary
#   (domain rows + per-group GROUP_UPDATED rows commit together).
# Standards: typed domain errors (GoogleConnectorError / GroupSyncFetchError
#   / GroupSyncConflictRefusedError) propagate for the caller to map; this
#   module NEVER raises HTTPException and NEVER writes the run-level
#   GROUPS_SYNCED summary -- both stay caller-owned.
# Blast Radius: Group naming/membership/active state and per-group audit
#   rows; no finance math, no authorization changes.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> the manual sync
#     route (POST /channels/groups/sync) drives this core.
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py ->
#     _run_group_sync_job drives it from the scheduled worker.
# ============================================================================
def run_group_sync(
    session: Session,
    *,
    tenant_id: UUID,
    content_owner_id: str,
    registry: ChannelRegistryStore,
    groups: ChannelGroupRegistryStore,
    audit_sink: AuditSink,
    actor: UserPrincipal,
    reason: str,
    dry_run: bool,
    client_factory: GroupsClientFactory,
    resolver: CredentialResolver = resolve_connector_credentials,
) -> GroupSyncRunResult:
    """Resolve, fetch, plan, and preview-or-apply one content owner's CMS groups.

    In exact route order: resolve the youtube-analytics credential (typed
    ``GoogleConnectorError`` failures propagate for the caller to map) -> end the
    credential transaction before the network fetch -> fetch groups and their
    members, snapshot them, closing the client either way (a fetch-phase error is
    re-raised as ``GroupSyncFetchError``) -> plan against stored state -> on a
    dry run roll back and return with ``execution=None`` -> otherwise refuse a
    foreign-owner conflict, then apply.

    ``resolver`` is injected so the manual route can keep resolving through its
    own module attribute (which its tests patch); it defaults to the real
    orchestrator resolver, so job callers pass nothing. This module NEVER raises
    ``HTTPException`` and NEVER writes the run-level GROUPS_SYNCED summary — both
    stay with the caller.
    """
    credentials = resolver(
        session=session,
        tenant_id=tenant_id,
        connector_key=YOUTUBE_ANALYTICS_CONNECTOR,
        account_id=content_owner_id,
    )

    _end_credential_transaction(session, dry_run=dry_run)

    client = client_factory(credentials)
    try:
        try:
            cms_groups = client.list_groups(account_id=content_owner_id)
            snapshot = tuple(
                CmsGroupSnapshot(
                    cms_group_id=group.cms_group_id,
                    title=group.title,
                    member_channel_ids=members.channel_ids,
                    non_channel_member_count=members.non_channel_count,
                )
                for group in cms_groups
                for members in (
                    client.list_group_items(
                        group_id=group.cms_group_id, account_id=content_owner_id
                    ),
                )
            )
        except GoogleConnectorError as exc:
            # Distinguish the fetch phase from credential resolution: both raise
            # GoogleConnectorError, but only the fetch failure becomes a 502.
            raise GroupSyncFetchError(inner=exc) from exc
    finally:
        client.close()

    plan = plan_group_sync_with_stores(
        snapshot,
        registry=registry,
        groups=groups,
        content_owner_id=content_owner_id,
    )
    if dry_run:
        # A dry run persists nothing. The credential-transaction split already
        # rolled back the refresh telemetry; this covers any read since — the
        # same belt-and-braces the route relied on before the extraction.
        session.rollback()
        return GroupSyncRunResult(plan=plan, execution=None)

    _reject_foreign_owner_conflicts(plan, session=session)

    execution = apply_group_sync(
        plan,
        groups=groups,
        audit_sink=audit_sink,
        actor=actor,
        scope=AccessScope.global_scope(),
        content_owner_id=content_owner_id,
        reason=reason,
    )
    return GroupSyncRunResult(plan=plan, execution=execution)


def _end_credential_transaction(session: Session, *, dry_run: bool) -> None:
    """Close the credential transaction before the external fetch begins.

    Mode-dependent on purpose. ``resolve_connector_credentials`` returns a
    Credentials object built from the resolved secret, not an ORM-bound row, so
    it survives either call; what differs is the refresh-telemetry stamp that
    resolution leaves pending:

    - dry run -> roll back, because "a dry run persists nothing" includes that
      stamp (the credential helper's own documented dry-run contract).
    - apply   -> commit it, which is where that stamp was always meant to land.

    Safe at exactly this point for the same reason the helper's own failure
    path commits here: nothing else is pending yet. The group writes and their
    audit rows still share ONE later transaction, so the audit-atomicity
    invariant this route already proved on Postgres is untouched.

    The non-obvious part is RLS. Tenant isolation is applied by an
    ``after_begin`` Session hook (db/session.py) using SET LOCAL, which a
    COMMIT resets — but because the hook fires on EVERY transaction begin, the
    next statement starts a fresh transaction that re-enters the app_tenant
    role and re-writes the trusted tenant-context row. Ending the transaction
    here therefore cannot strand the rest of the request outside RLS. Proven,
    not assumed: ``test_sync_persists_groups_on_postgres`` and
    ``test_create_stamps_content_owner_id_on_postgres`` drive the full apply
    through this route and assert durable rows, and every write they make now
    happens after this commit.
    """
    if dry_run:
        session.rollback()
    else:
        session.commit()


def _reject_foreign_owner_conflicts(plan: GroupSyncPlan, *, session: Session) -> None:
    """Fail a conflicted plan closed BEFORE any write lands.

    The dry run already showed these entries as CONFLICT; this is the apply
    refusing exactly what the preview declined to promise, rather than getting
    part-way through the mirror and hitting the unique key. Rolls the pending
    credential telemetry back with the refusal and raises the typed conflict the
    caller surfaces as a 409 with this exact detail.
    """
    conflicts = sorted(
        entry.cms_group_id for entry in plan.entries if entry.outcome is GroupSyncOutcome.CONFLICT
    )
    if not conflicts:
        return
    session.rollback()
    raise GroupSyncConflictRefusedError(
        detail=(
            "CMS group keys already belong to another content owner: "
            f"{', '.join(conflicts)}; import or sync them under their own content owner"
        )
    )
