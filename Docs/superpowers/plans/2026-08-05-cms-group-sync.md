# CMS Group Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /channels/groups/sync` makes local channel grouping mirror a YouTube CMS content owner exactly — real titles, real membership, adds and removals, deactivation of vanished groups — with a mandatory dry-run.

**Architecture:** A thin `YouTubeGroupsClient` fetches the CMS snapshot (`groups.list` + `groupItems.list`) through the existing `GoogleHttpClient`; a pure planner (`org/channel_group_sync.py`) diffs it against local synced groups; an apply-domain module (`org/channel_group_sync_apply.py`, mirroring `channel_import_apply.py`) executes through `ChannelGroupRegistryStore` in the single tenant transaction and audits from write-boundary outcomes. `api/groups.py` gains a synced-group lockdown so manual edits can't fight the mirror.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.x, pytest, ruff (100-char). **No migration** — `channel_groups.cms_group_id` shipped in PR #159.

**Spec:** `Docs/superpowers/specs/2026-08-05-cms-group-sync-design.md`
**Branch:** `feat/cms-group-sync` off `557ec1f0` (spec committed `eab868ed`)

---

## Conventions for every task

- **All Python commands run through `uv run`.** Bare `python -m pytest` FAILS.
- **Always set the Postgres URL for pytest** (Postgres-tier tests raise, never skip):
  `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/test_ums uv run python -m pytest ...`
  Container if missing: `docker run -d --name ums-mig-pg-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=test_ums -p 55432:5432 postgres:18-alpine`
- Test root is `./tests` at repo root. Lines ≤ 100 chars. `uv run python -m ruff check backend tests`
  and `uv run python -m ruff format --check <touched files>` before each commit.
- **Commits trailer-free** — no `Co-Authored-By`, no AI attribution; repo validation scans for it.
- Docstrings on public functions/classes. OPUS block comments on non-trivial routes/methods —
  read neighbours in the same file and match.
- **PR #159 review-round rules that bind this PR too:** audit summary counts are accumulated
  from actual write-boundary outcomes, never copied from the plan; never leak `str(exc)` from
  Google/credential errors into HTTP details (canned messages only); group writes require
  `MANAGE_GROUPS`.

## Resolved facts (verified 2026-08-05 — do not re-derive)

- Apply-domain pattern lives in `backend/ums_smart_revenue/org/channel_import_apply.py`
  (`apply_channel_import(plan, *, registry, groups, audit_sink, actor, scope, ...)`); the route in
  `api/channels.py` is thin. Mirror this split.
- `ChannelGroupRegistryStore` (in `org/channel_groups.py`, SQL impl in `org/sql_channel_groups.py`):
  `list_groups` and `list_groups_full` are **active-only**; `get_group_by_cms_id(cms_group_id, *,
  for_update=False)` has **no** active filter (finds inactive groups); `create_group(*, name,
  group_type, channel_ids, cms_group_id=None)`; `update_group(*, group_id, name, active)`;
  `add_members(*, group_id, channel_ids)`; `remove_member(*, group_id, channel_id)`.
  `ChannelGroupEntry` fields: `id, name, group_type, active, channel_ids (youtube_channel_id
  strings), cms_group_id`. KeyError from store methods on unknown group.
- `ChannelRegistryStore.list_channels_by_ids(set[str])` returns entries for known ids only.
- `api/groups.py` routes: `PATCH /groups/{group_id}` (`update_group`, `GroupUpdateRequest` with
  `name`, `active`, `reason`), `POST /groups/{group_id}/members`, `DELETE
  /groups/{group_id}/members/{channel_id}`. Helper `_require_manageable_group(...)` returns the
  group; `_audit_group_change(...)` writes `GROUP_UPDATED`.
- `AuditEventType.GROUP_UPDATED` has `reason_required=True, permission=MANAGE_GROUPS` in
  `AUDIT_EVENT_DEFINITIONS` (`auth/audit.py`). There is no `GROUPS_SYNCED` yet.
- Credential chokepoint: `resolve_connector_credentials(*, session, tenant_id, connector_key,
  account_id) -> Credentials` in `connectors/runs/orchestrator.py`; raises
  `CredentialNotFoundError`, `InactiveCredentialError`, `OAuthRefreshError`; stamps refresh
  telemetry. The connector-key for this sync is `"youtube-analytics"`.
- Route session dependency: `session: Annotated[Session, Depends(current_db_session)]` (see
  `api/connectors.py`). The error-taxonomy precedent (canned messages, no `str(exc)`) is the
  credential-test route in `api/connectors.py` around line 780.
- `GoogleHttpClient(*, credentials, transport=None, ...)` in `connectors/google/http_client.py`;
  `youtube_reporting_client.py` shows the house patterns: `_response_object_list`,
  `_next_page_token`, fail-closed shape validation.
- Route prefix: `api/channels.py` router is mounted at `/channels`; `@router.post("/groups/sync")`
  yields `POST /channels/groups/sync`. No dynamic POST route can shadow it (dynamic routes there
  are PATCH-only).
- `AccessScope.global_scope()` is the global scope constructor.

## File map

| File | Role |
| --- | --- |
| `backend/ums_smart_revenue/auth/audit.py` | + `GROUPS_SYNCED` event type + definition |
| `backend/ums_smart_revenue/org/channel_groups.py` | + `list_synced_groups` (Protocol + in-memory) |
| `backend/ums_smart_revenue/org/sql_channel_groups.py` | + `list_synced_groups` (SQL) |
| `backend/ums_smart_revenue/connectors/google/youtube_groups_client.py` | NEW — fetch snapshot |
| `backend/ums_smart_revenue/org/channel_group_sync.py` | NEW — pure planner |
| `backend/ums_smart_revenue/org/channel_group_sync_apply.py` | NEW — apply + audit |
| `backend/ums_smart_revenue/api/channels.py` | + thin `POST /groups/sync` route |
| `backend/ums_smart_revenue/api/groups.py` | + synced-group lockdown |
| `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` | trackers |

---

## Task 1: `GROUPS_SYNCED` audit event type

**Files:**
- Modify: `backend/ums_smart_revenue/auth/audit.py`
- Test: `tests/auth/test_groups_synced_audit_event.py`

- [ ] **Step 1: Write the failing test**

Create `tests/auth/test_groups_synced_audit_event.py`:

```python
"""The CMS group sync needs its own summary audit event type."""

from ums_smart_revenue.auth.audit import (
    AUDIT_EVENT_DEFINITIONS,
    AuditEventType,
)
from ums_smart_revenue.auth.roles import Permission


def test_groups_synced_event_type_exists() -> None:
    assert AuditEventType.GROUPS_SYNCED.value == "GROUPS_SYNCED"


def test_groups_synced_requires_reason_and_manage_groups() -> None:
    definition = AUDIT_EVENT_DEFINITIONS[AuditEventType.GROUPS_SYNCED]
    assert definition.reason_required is True
    assert definition.permission is Permission.MANAGE_GROUPS
```

Check the real import path of `Permission` first (`grep -n "from ums_smart_revenue" backend/ums_smart_revenue/auth/audit.py`) and match it.

- [ ] **Step 2: Run it — must FAIL** with `AttributeError: GROUPS_SYNCED`.

Run: `uv run python -m pytest tests/auth/test_groups_synced_audit_event.py -v`

- [ ] **Step 3: Implement**

In `auth/audit.py`: add `GROUPS_SYNCED = "GROUPS_SYNCED"` immediately after `GROUP_UPDATED` in the
enum, and add to `AUDIT_EVENT_DEFINITIONS` next to the `GROUP_UPDATED` entry:

```python
    AuditEventType.GROUPS_SYNCED: AuditEventDefinition(
        AuditEventType.GROUPS_SYNCED,
        reason_required=True,
        permission=Permission.MANAGE_GROUPS,
    ),
```

- [ ] **Step 4: Run test — PASS (2 passed).** Then `uv run python -m pytest tests/auth -q` — no regressions.

- [ ] **Step 5: Lint + commit**

```bash
git add backend/ums_smart_revenue/auth/audit.py tests/auth/test_groups_synced_audit_event.py
git commit -m "feat(auth): add GROUPS_SYNCED audit event type"
```

---

## Task 2: `list_synced_groups` on the channel-group store

Sync must see **inactive** synced groups (REACTIVATE) — both existing list methods are
active-only. New method: every group with `cms_group_id IS NOT NULL`, active or not, with FULL
membership.

**Files:**
- Modify: `backend/ums_smart_revenue/org/channel_groups.py`
- Modify: `backend/ums_smart_revenue/org/sql_channel_groups.py`
- Test: `tests/org/test_list_synced_groups.py` (+ SQL cases in `tests/org/test_sql_channel_groups.py`)

- [ ] **Step 1: Write the failing test**

Create `tests/org/test_list_synced_groups.py`:

```python
"""In-memory store: enumerate CMS-synced groups including inactive ones."""

from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry


def test_lists_only_groups_with_a_cms_key() -> None:
    registry = ChannelGroupRegistry()
    registry.create_group(name="Manual", group_type="CUSTOM_GROUP", channel_ids=[])
    synced = registry.create_group(
        name="TV Sector", group_type="SECTOR", channel_ids=[], cms_group_id="cms-tv"
    )
    assert [group.id for group in registry.list_synced_groups()] == [synced.id]


def test_includes_inactive_synced_groups() -> None:
    registry = ChannelGroupRegistry()
    group = registry.create_group(
        name="News", group_type="SECTOR", channel_ids=[], cms_group_id="cms-news"
    )
    registry.update_group(group_id=group.id, name=None, active=False)
    listed = registry.list_synced_groups()
    assert len(listed) == 1
    assert listed[0].active is False
```

- [ ] **Step 2: Run — must FAIL** with `AttributeError: ... 'list_synced_groups'`.

Run: `uv run python -m pytest tests/org/test_list_synced_groups.py -v`

- [ ] **Step 3: Implement**

`org/channel_groups.py` — Protocol:

```python
    def list_synced_groups(self) -> list[ChannelGroupEntry]:
        pass
```

In-memory `ChannelGroupRegistry`:

```python
    def list_synced_groups(self) -> list[ChannelGroupEntry]:
        """Return every CMS-keyed group, active or not, for sync planning."""
        return [group for group in self._groups.values() if group.cms_group_id is not None]
```

(Adapt to the class's real internal container — read it first; if entries are stored immutably
elsewhere, follow the existing accessor style.)

`org/sql_channel_groups.py` — mirror `list_groups_full`'s member-loading (the non-active-filtered
member helper) but **without** the `active.is_(True)` filter on groups and with
`ChannelGroupORM.cms_group_id.is_not(None)`:

```python
    def list_synced_groups(self) -> list[ChannelGroupEntry]:
        """Return every CMS-keyed group (active or not) with full membership.

        Sync planning must see deactivated synced groups so a CMS key that
        reappears upstream can REACTIVATE its original local group instead of
        creating a duplicate.
        """
        rows = self._session.scalars(
            select(ChannelGroupORM)
            .where(
                ChannelGroupORM.tenant_id == self._tenant_id,
                ChannelGroupORM.cms_group_id.is_not(None),
            )
            .order_by(ChannelGroupORM.name)
        ).all()
        group_ids = [row.id for row in rows]
        channel_ids_by_group = self._channel_ids_by_group(group_ids)
        return [
            self._to_entry(row, channel_ids=channel_ids_by_group.get(row.id, ()))
            for row in rows
        ]
```

(Verify the exact member-helper name — `_channel_ids_by_group` vs a review-round rename — by
reading `list_groups_full` first.)

- [ ] **Step 4: SQL tests** — append to `tests/org/test_sql_channel_groups.py`, following its
fixture style: (a) synced-only filtering, (b) inactive synced group included, (c) cross-tenant
isolation (tenant B sees nothing).

- [ ] **Step 5: Run** `UMS_TEST_DATABASE_URL=... uv run python -m pytest tests/org -q` — all pass.

- [ ] **Step 6: Lint + commit**

```bash
git add backend/ums_smart_revenue/org/channel_groups.py \
        backend/ums_smart_revenue/org/sql_channel_groups.py \
        tests/org/test_list_synced_groups.py tests/org/test_sql_channel_groups.py
git commit -m "feat(org): enumerate CMS-synced groups including inactive"
```

---

## Task 3: `YouTubeGroupsClient`

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/youtube_groups_client.py`
- Test: `tests/connectors/google/test_youtube_groups_client.py`

Read `connectors/google/youtube_reporting_client.py` FIRST and reuse its idioms exactly:
`_response_object_list`-style fail-closed shape validation, typed errors from
`connectors/google/errors.py`, `GoogleHttpClient` for transport. Check how its tests fake the
HTTP layer (`tests/connectors/google/test_youtube_reporting_client.py`) and use the same
technique.

- [ ] **Step 1: Write the failing tests**

Create `tests/connectors/google/test_youtube_groups_client.py` covering, with a faked
`GoogleHttpClient` (same faking style as the reporting-client tests):

```python
"""YouTube Analytics groups/groupItems client."""

# Shape the fakes so each test declares the JSON bodies the client will see.
# Follow tests/connectors/google/test_youtube_reporting_client.py for the fake
# GoogleHttpClient technique used in this repo.

CO = "PlZrS5Fh56RMd9dmSL6XSA"


def test_list_groups_returns_id_title_and_item_count(...):
    # one page: {"items": [{"id": "g1", "snippet": {"title": "TV"},
    #                       "contentDetails": {"itemCount": "3"}}]}
    # expect [CmsGroup(cms_group_id="g1", title="TV")]
    ...


def test_list_groups_follows_next_page_token(...):
    # page1 has nextPageToken -> page2; both pages' items concatenated;
    # assert the second request carried pageToken=<token>
    ...


def test_list_groups_page_cap_fails_closed(...):
    # every page returns the same nextPageToken; expect a typed error
    # naming the cap, not an infinite loop
    ...


def test_list_group_items_returns_channel_ids_and_counts_non_channels(...):
    # items: two resource.kind=="youtube#channel" + one "youtube#video"
    # expect member ids ("UC..a", "UC..b") and non_channel_count == 1
    ...


def test_list_group_items_rejects_malformed_item(...):
    # item missing resource.id -> typed fail-closed error
    ...


def test_requests_carry_on_behalf_of_content_owner(...):
    # both endpoints called with onBehalfOfContentOwner == CO and mine=true
    # on groups.list
    ...
```

Write these as REAL tests (the `...` above marks intent, not deliverable — every test must be
executable). If the reporting-client tests use `httpx.MockTransport`, use that.

- [ ] **Step 2: Run — must FAIL** with `ModuleNotFoundError` on the new module.

- [ ] **Step 3: Implement**

`connectors/google/youtube_groups_client.py`:

```python
# ============================================================================
# Purpose: Read-only YouTube Analytics groups surface for CMS group sync:
#   groups.list (all groups a content owner owns) and groupItems.list (their
#   members). Returns typed snapshots; performs no writes anywhere.
# Database/ORM: None.
# Standards: GoogleHttpClient transport (auth + retry + JSON decode);
#   fail-closed shape validation; typed errors; explicit page cap so a
#   pathological pagination loop cannot hang a request.
# Blast Radius: None by itself — callers decide what to do with the snapshot.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> group sync route.
#   - File: backend/ums_smart_revenue/org/channel_group_sync.py -> planner input.
# ============================================================================
"""YouTube Analytics groups/groupItems client for CMS group sync."""

from dataclasses import dataclass

from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient

_BASE = "https://youtubeanalytics.googleapis.com/v2"
_MAX_PAGES = 500


@dataclass(frozen=True)
class CmsGroup:
    """One CMS group as YouTube reports it."""

    cms_group_id: str
    title: str


@dataclass(frozen=True)
class CmsGroupMembers:
    """Channel members of one CMS group, with the non-channel count."""

    channel_ids: tuple[str, ...]
    non_channel_count: int


class YouTubeGroupsClient:
    """Thin wrapper over groups.list / groupItems.list."""

    def __init__(self, *, http: GoogleHttpClient) -> None:
        """Bind the shared authenticated HTTP client."""
        self._http = http

    def list_groups(self, *, account_id: str) -> list[CmsGroup]:
        """Return every group the content owner owns, following pagination."""
        ...

    def list_group_items(self, *, group_id: str, account_id: str) -> CmsGroupMembers:
        """Return the group's channel members; count non-channel members."""
        ...
```

Fill the `...` bodies using the reporting client's request/validation idioms: `groups.list` params
`{"mine": "true", "onBehalfOfContentOwner": account_id}` plus `pageToken` when continuing;
`groupItems.list` params `{"groupId": group_id, "onBehalfOfContentOwner": account_id}` (follow
`nextPageToken` if present, same `_MAX_PAGES` cap). Group id from `item["id"]`, title from
`item["snippet"]["title"]` — missing/non-string → the same typed error class the reporting client
raises for malformed bodies (read `errors.py` and pick the existing class; do NOT invent a new
hierarchy). Member kind from `item["resource"]["kind"]`; only `"youtube#channel"` members
contribute `item["resource"]["id"]`.

- [ ] **Step 4: Run the client tests — all PASS.** Then
`UMS_TEST_DATABASE_URL=... uv run python -m pytest tests/connectors -q` — no regressions.

- [ ] **Step 5: Lint + commit**

```bash
git add backend/ums_smart_revenue/connectors/google/youtube_groups_client.py \
        tests/connectors/google/test_youtube_groups_client.py
git commit -m "feat(connectors): YouTube groups client for CMS group sync"
```

---

## Task 4: pure sync planner

**Files:**
- Create: `backend/ums_smart_revenue/org/channel_group_sync.py`
- Test: `tests/org/test_channel_group_sync_planner.py`

Pure module — no DB, no session, no I/O. Import only dataclasses/enums/typing plus
`ChannelGroupEntry` (no circular import: `channel_groups.py` imports nothing from this module).

- [ ] **Step 1: Write the failing tests**

Create `tests/org/test_channel_group_sync_planner.py`:

```python
"""Pure planning for CMS group sync."""

from ums_smart_revenue.org.channel_group_sync import (
    CmsGroupSnapshot,
    GroupSyncOutcome,
    plan_group_sync,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry

CH_A = "UCB6sc84dcg6VQGB_d89sx2g"
CH_B = "UC3Dci3BzZXDo4jw4dU8KqWg"
CH_UNKNOWN = "UCzzzzzzzzzzzzzzzzzzzzzz"
KNOWN = frozenset({CH_A, CH_B})


def _snapshot(**overrides: object) -> CmsGroupSnapshot:
    defaults: dict[str, object] = {
        "cms_group_id": "g1",
        "title": "TV Sector",
        "member_channel_ids": (CH_A,),
        "non_channel_member_count": 0,
    }
    defaults.update(overrides)
    return CmsGroupSnapshot(**defaults)  # type: ignore[arg-type]


def _local(**overrides: object) -> ChannelGroupEntry:
    defaults: dict[str, object] = {
        "id": "local-1",
        "name": "TV Sector",
        "group_type": "SECTOR",
        "active": True,
        "channel_ids": (CH_A,),
        "cms_group_id": "g1",
    }
    defaults.update(overrides)
    return ChannelGroupEntry(**defaults)  # type: ignore[arg-type]


def _plan(snapshot=(), local=(), known=KNOWN):
    return plan_group_sync(
        snapshot=tuple(snapshot), local_groups=tuple(local), known_channel_ids=known
    )


def test_new_cms_group_plans_create() -> None:
    plan = _plan(snapshot=[_snapshot()])
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.CREATE
    assert entry.members_added == (CH_A,)
    assert plan.counts["CREATE"] == 1


def test_identical_group_is_unchanged() -> None:
    plan = _plan(snapshot=[_snapshot()], local=[_local()])
    assert plan.entries[0].outcome is GroupSyncOutcome.UNCHANGED


def test_title_difference_plans_rename() -> None:
    plan = _plan(snapshot=[_snapshot(title="TV")], local=[_local()])
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.RENAME
    assert entry.name_change == ("TV Sector", "TV")


def test_membership_set_reconciles_adds_and_removals() -> None:
    plan = _plan(
        snapshot=[_snapshot(member_channel_ids=(CH_B,))],
        local=[_local(channel_ids=(CH_A,))],
    )
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.MEMBERS_CHANGED
    assert entry.members_added == (CH_B,)
    assert entry.members_removed == (CH_A,)


def test_group_absent_upstream_plans_deactivate() -> None:
    plan = _plan(snapshot=[], local=[_local()])
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.DEACTIVATE
    assert entry.active_change == (True, False)


def test_inactive_group_reappearing_plans_reactivate() -> None:
    plan = _plan(snapshot=[_snapshot()], local=[_local(active=False)])
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.REACTIVATE
    assert entry.active_change == (False, True)


def test_reactivate_dominates_rename_and_members_but_carries_both() -> None:
    plan = _plan(
        snapshot=[_snapshot(title="TV", member_channel_ids=(CH_B,))],
        local=[_local(active=False, channel_ids=(CH_A,))],
    )
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.REACTIVATE
    assert entry.name_change == ("TV Sector", "TV")
    assert entry.members_added == (CH_B,)
    assert entry.members_removed == (CH_A,)


def test_rename_dominates_members_changed() -> None:
    plan = _plan(
        snapshot=[_snapshot(title="TV", member_channel_ids=(CH_A, CH_B))],
        local=[_local()],
    )
    assert plan.entries[0].outcome is GroupSyncOutcome.RENAME
    assert plan.entries[0].members_added == (CH_B,)


def test_unknown_channels_are_skipped_and_counted() -> None:
    plan = _plan(snapshot=[_snapshot(member_channel_ids=(CH_A, CH_UNKNOWN))])
    entry = plan.entries[0]
    assert entry.members_added == (CH_A,)
    assert entry.unknown_channel_ids == (CH_UNKNOWN,)
    assert plan.unknown_channel_total == 1


def test_unknown_channel_never_causes_removal_churn() -> None:
    # Upstream has an unknown member; local group already mirrors the known set.
    plan = _plan(
        snapshot=[_snapshot(member_channel_ids=(CH_A, CH_UNKNOWN))],
        local=[_local(channel_ids=(CH_A,))],
    )
    assert plan.entries[0].outcome is GroupSyncOutcome.UNCHANGED


def test_deactivated_group_absent_upstream_is_unchanged() -> None:
    plan = _plan(snapshot=[], local=[_local(active=False)])
    assert plan.entries[0].outcome is GroupSyncOutcome.UNCHANGED


def test_manual_groups_are_invisible() -> None:
    # plan_group_sync receives synced groups only; guard that a None key raises.
    import pytest

    with pytest.raises(ValueError):
        _plan(snapshot=[], local=[_local(cms_group_id=None)])


def test_non_channel_members_are_totalled() -> None:
    plan = _plan(
        snapshot=[
            _snapshot(non_channel_member_count=2),
            _snapshot(cms_group_id="g2", title="News", non_channel_member_count=1),
        ]
    )
    assert plan.non_channel_member_count == 3


def test_entries_sorted_by_cms_group_id() -> None:
    plan = _plan(snapshot=[_snapshot(cms_group_id="g2", title="B"), _snapshot()])
    assert [entry.cms_group_id for entry in plan.entries] == ["g1", "g2"]


def test_counts_cover_every_outcome_key() -> None:
    plan = _plan(snapshot=[_snapshot()])
    assert set(plan.counts) == {
        "CREATE", "RENAME", "MEMBERS_CHANGED", "DEACTIVATE", "REACTIVATE", "UNCHANGED"
    }
```

- [ ] **Step 2: Run — must FAIL** with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`org/channel_group_sync.py`:

```python
# ============================================================================
# Purpose: Pure planning for CMS group sync. Diffs a YouTube CMS snapshot
#   against local synced groups into per-group outcomes the apply layer
#   executes. Full mirror, YouTube wins: renames overwrite, membership is
#   set-reconciled with removals, vanished groups deactivate, reappearing
#   keys reactivate their original local group.
# Database/ORM: None. No I/O, no session.
# Standards: Frozen dataclasses; deterministic ordering (cms_group_id);
#   unknown channels are skipped and surfaced, never created here — channel
#   creation belongs to POST /channels/import and its cms_status contract.
# Blast Radius: Channel-group naming/membership/active state only. No finance
#   totals; group-scope rollups change composition only as the CMS does.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py.
#   - File: backend/ums_smart_revenue/connectors/google/youtube_groups_client.py.
# ============================================================================
"""Pure planning for CMS group sync."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from ums_smart_revenue.org.channel_groups import ChannelGroupEntry


class GroupSyncOutcome(StrEnum):
    """Dominant label for what sync will do with one CMS group key."""

    CREATE = "CREATE"
    RENAME = "RENAME"
    MEMBERS_CHANGED = "MEMBERS_CHANGED"
    DEACTIVATE = "DEACTIVATE"
    REACTIVATE = "REACTIVATE"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True)
class CmsGroupSnapshot:
    """One CMS group as fetched: identity, title, and channel members."""

    cms_group_id: str
    title: str
    member_channel_ids: tuple[str, ...]
    non_channel_member_count: int


@dataclass(frozen=True)
class GroupSyncPlanEntry:
    """Planned changes for one CMS group key (full diff, dominant outcome)."""

    cms_group_id: str
    outcome: GroupSyncOutcome
    title: str | None
    local_group_id: str | None
    name_change: tuple[str, str] | None
    active_change: tuple[bool, bool] | None
    members_added: tuple[str, ...]
    members_removed: tuple[str, ...]
    unknown_channel_ids: tuple[str, ...]


@dataclass(frozen=True)
class GroupSyncPlan:
    """Every planned entry plus counts and skipped-member telemetry."""

    entries: tuple[GroupSyncPlanEntry, ...]
    counts: Mapping[str, int]
    unknown_channel_total: int
    non_channel_member_count: int


def plan_group_sync(
    *,
    snapshot: tuple[CmsGroupSnapshot, ...],
    local_groups: tuple[ChannelGroupEntry, ...],
    known_channel_ids: frozenset[str],
) -> GroupSyncPlan:
    """Diff the CMS snapshot against local synced groups into a plan."""
    # skipcq: SCT-A000 -- dict-comprehension target, not a credential value.
    local_by_key: dict[str, ChannelGroupEntry] = {}
    for group in local_groups:
        if group.cms_group_id is None:
            raise ValueError(f"manual group passed to sync planner: {group.id}")
        local_by_key[group.cms_group_id] = group
    upstream_keys = {item.cms_group_id for item in snapshot}

    entries: list[GroupSyncPlanEntry] = []
    unknown_total = 0
    non_channel_total = 0

    for item in sorted(snapshot, key=lambda entry: entry.cms_group_id):
        non_channel_total += item.non_channel_member_count
        wanted_known = tuple(
            channel_id
            for channel_id in item.member_channel_ids
            if channel_id in known_channel_ids
        )
        unknown = tuple(
            channel_id
            for channel_id in item.member_channel_ids
            if channel_id not in known_channel_ids
        )
        unknown_total += len(unknown)
        local = local_by_key.get(item.cms_group_id)
        if local is None:
            entries.append(
                GroupSyncPlanEntry(
                    cms_group_id=item.cms_group_id,
                    outcome=GroupSyncOutcome.CREATE,
                    title=item.title,
                    local_group_id=None,
                    name_change=None,
                    active_change=None,
                    members_added=wanted_known,
                    members_removed=(),
                    unknown_channel_ids=unknown,
                )
            )
            continue
        name_change = (local.name, item.title) if local.name != item.title else None
        active_change = (False, True) if not local.active else None
        current = set(local.channel_ids)
        wanted = set(wanted_known)
        added = tuple(sorted(wanted - current))
        removed = tuple(sorted(current - wanted))
        if active_change:
            outcome = GroupSyncOutcome.REACTIVATE
        elif name_change:
            outcome = GroupSyncOutcome.RENAME
        elif added or removed:
            outcome = GroupSyncOutcome.MEMBERS_CHANGED
        else:
            outcome = GroupSyncOutcome.UNCHANGED
        entries.append(
            GroupSyncPlanEntry(
                cms_group_id=item.cms_group_id,
                outcome=outcome,
                title=item.title,
                local_group_id=local.id,
                name_change=name_change,
                active_change=active_change,
                members_added=added,
                members_removed=removed,
                unknown_channel_ids=unknown,
            )
        )

    for group in sorted(local_groups, key=lambda entry: str(entry.cms_group_id)):
        if group.cms_group_id in upstream_keys:
            continue
        outcome = GroupSyncOutcome.DEACTIVATE if group.active else GroupSyncOutcome.UNCHANGED
        entries.append(
            GroupSyncPlanEntry(
                cms_group_id=str(group.cms_group_id),
                outcome=outcome,
                title=None,
                local_group_id=group.id,
                name_change=None,
                active_change=(True, False) if group.active else None,
                members_added=(),
                members_removed=(),
                unknown_channel_ids=(),
            )
        )

    entries.sort(key=lambda entry: entry.cms_group_id)
    counts = {outcome.value: 0 for outcome in GroupSyncOutcome}
    for entry in entries:
        counts[entry.outcome.value] += 1
    return GroupSyncPlan(
        entries=tuple(entries),
        counts=MappingProxyType(counts),
        unknown_channel_total=unknown_total,
        non_channel_member_count=non_channel_total,
    )
```

- [ ] **Step 4: Run planner tests — all PASS (15).** Fix implementation, never weaken a test.

- [ ] **Step 5: Lint + commit**

```bash
git add backend/ums_smart_revenue/org/channel_group_sync.py \
        tests/org/test_channel_group_sync_planner.py
git commit -m "feat(org): plan CMS group sync against local synced groups"
```

---

## Task 5: apply module

**Files:**
- Create: `backend/ums_smart_revenue/org/channel_group_sync_apply.py`
- Test: `tests/org/test_channel_group_sync_apply.py`

Read `org/channel_import_apply.py` FIRST — same shape: domain module owning writes + per-item
audit, returning actual counts for the route's summary event. Use the in-memory
`ChannelGroupRegistry` and the same in-memory audit-sink technique `tests/org/
test_channel_import_apply*.py` files use (find them; if apply-domain tests live elsewhere, follow
that location).

- [ ] **Step 1: Write the failing tests**

`tests/org/test_channel_group_sync_apply.py` — cover, with real executable tests:

1. CREATE entry → group exists with title/`cms_group_id`/`SECTOR`/members; one `GROUP_UPDATED`
   audit with the sync marker in details and the request reason.
2. RENAME entry → name overwritten; audit carries `name_change`.
3. MEMBERS_CHANGED → members added AND removed via the store; audit carries add/remove counts.
4. DEACTIVATE → `active is False`; membership rows untouched.
5. REACTIVATE (with combined rename+membership) → active True, name updated, membership
   reconciled; ONE audit event for the group.
6. UNCHANGED → no store write, no audit event.
7. Return value: actual counts dict accumulated from what was executed — assert it equals the
   expected outcome tally, and assert an UNCHANGED entry contributes to `"UNCHANGED"`.

- [ ] **Step 2: Run — must FAIL** (module missing).

- [ ] **Step 3: Implement**

`org/channel_group_sync_apply.py`:

```python
# ============================================================================
# Purpose: Execute a CMS group-sync plan through the channel-group store and
#   audit every changed group. Mirrors channel_import_apply's split: the route
#   stays thin; writes and per-item audit live here; the route's GROUPS_SYNCED
#   summary uses the ACTUAL counts this module returns, never the plan's
#   (a plan is a snapshot; the write boundary is the record).
# Database/ORM: ChannelGroupORM + ChannelGroupMemberORM via
#   ChannelGroupRegistryStore, inside the caller's single tenant transaction.
# Standards: One GROUP_UPDATED audit per changed group (reason required);
#   UNCHANGED performs no write and no audit; fail on first store error and
#   let the transaction roll everything back.
# Blast Radius: Group naming/membership/active state and audit rows.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> sync route.
#   - File: backend/ums_smart_revenue/org/channel_group_sync.py -> plan types.
# ============================================================================
"""Apply a CMS group-sync plan and audit each changed group."""

AUDIT_SOURCE_CMS_SYNC = "cms_group_sync"


def apply_group_sync(
    plan: GroupSyncPlan,
    *,
    groups: ChannelGroupRegistryStore,
    audit_sink: AuditSink,
    actor: UserPrincipal,
    scope: AccessScope,
    content_owner_id: str,
    reason: str,
) -> dict[str, int]:
    """Execute every non-UNCHANGED entry; return actual counts by outcome."""
    executed = {outcome.value: 0 for outcome in GroupSyncOutcome}
    for entry in plan.entries:
        if entry.outcome is GroupSyncOutcome.UNCHANGED:
            executed[entry.outcome.value] += 1
            continue
        if entry.outcome is GroupSyncOutcome.CREATE:
            created = groups.create_group(
                name=entry.title or entry.cms_group_id,
                group_type="SECTOR",
                channel_ids=list(entry.members_added),
                cms_group_id=entry.cms_group_id,
            )
            group_id = created.id
        else:
            group_id = entry.local_group_id
            assert group_id is not None
            name = entry.name_change[1] if entry.name_change else None
            active = entry.active_change[1] if entry.active_change else None
            if name is not None or active is not None:
                groups.update_group(group_id=group_id, name=name, active=active)
            if entry.members_added:
                groups.add_members(group_id=group_id, channel_ids=list(entry.members_added))
            for channel_id in entry.members_removed:
                groups.remove_member(group_id=group_id, channel_id=channel_id)
        executed[entry.outcome.value] += 1
        record_audit_event(
            sink=audit_sink,
            actor=actor,
            event_type=AuditEventType.GROUP_UPDATED,
            entity_type="channel_group",
            entity_id=group_id,
            scope=scope,
            reason=reason,
            details={
                "source": AUDIT_SOURCE_CMS_SYNC,
                "content_owner_id": content_owner_id,
                "cms_group_id": entry.cms_group_id,
                "outcome": entry.outcome.value,
                "name_change": list(entry.name_change) if entry.name_change else None,
                "active_change": list(entry.active_change) if entry.active_change else None,
                "members_added": len(entry.members_added),
                "members_removed": len(entry.members_removed),
            },
        )
    return executed
```

Add the real imports (match `channel_import_apply.py`'s import style for `record_audit_event`,
`AuditSink`, `UserPrincipal`, `AccessScope`, store Protocols). Replace the bare `assert` with the
typed-error style `channel_import_apply.py` uses for impossible states if it has one — read it and
match.

- [ ] **Step 4: Run apply tests — all PASS.** Then `uv run python -m pytest tests/org -q`.

- [ ] **Step 5: Lint + commit**

```bash
git add backend/ums_smart_revenue/org/channel_group_sync_apply.py \
        tests/org/test_channel_group_sync_apply.py
git commit -m "feat(org): apply CMS group-sync plans with per-group audit"
```

---

## Task 6: the sync route

**Files:**
- Modify: `backend/ums_smart_revenue/api/channels.py`
- Test: `tests/api/test_channel_group_sync_api.py`

Read the `import_channels` route and `tests/api/test_channels_import_api.py` FIRST — reuse the
fixture/app/override technique. The Google fetch must be substitutable in tests: inject the
groups-client factory as a FastAPI dependency (a module-level
`def current_groups_client_factory() -> ...` returning a callable that takes `Credentials` and
yields a `YouTubeGroupsClient`), so tests override it with a fake returning canned snapshots and
never touch the network. Credential resolution stays real in structure but is overridden in
SQLite tests the same way the connector-test route's tests do it (read
`tests/api/test_connectors_api.py` for the pattern).

- [ ] **Step 1: Write the failing tests**

`tests/api/test_channel_group_sync_api.py` — cover (executable, following the import-API test
style):

1. 403 without global `MANAGE_GROUPS` (a `MANAGE_CHANNELS`-only principal must be rejected).
2. 422 on blank `reason`, blank `content_owner_id`, missing `dry_run`.
3. Dry-run: returns the plan (counts/entries/unknowns), writes NO groups and NO audit rows.
4. Apply CREATE: CMS snapshot with one group + one known member channel → group created with
   title + `cms_group_id`, member attached; `GROUP_UPDATED` and `GROUPS_SYNCED` both present;
   summary details carry actual counts and `content_owner_id`.
5. Apply full-mirror round: pre-seed a synced group (rename + member churn + a vanished group
   → deactivate) → verify all three effects and re-sync returns all-UNCHANGED.
6. Unknown channels: snapshot member not in the registry → response
   `unknown_channel_ids`/total surfaced; no channel created.
7. Credential errors: override the credential resolver to raise `CredentialNotFoundError` /
   `OAuthRefreshError` / `GoogleConnectorError` → 503 / 503 / 502 with canned detail strings
   (assert the raw exception text does NOT appear).
8. Manual group untouched: a group without `cms_group_id` survives a sync that deactivates a
   synced sibling.

- [ ] **Step 2: Run — must FAIL** with 404 (route absent).

- [ ] **Step 3: Implement**

In `api/channels.py` (imports: `current_db_session`, `resolve_connector_credentials`, the three
credential error classes + `GoogleConnectorError` from `connectors/google/errors.py`,
`GoogleHttpClient`, `YouTubeGroupsClient`, planner + apply modules, `CmsGroupSnapshot`):

```python
class GroupSyncRequest(BaseModel):
    """Operator request to mirror a content owner's CMS groups."""

    content_owner_id: str
    dry_run: bool
    reason: str


def current_groups_client_factory() -> Callable[[Credentials], YouTubeGroupsClient]:
    """Build a live groups client from resolved credentials (test-overridable)."""

    def _factory(credentials: Credentials) -> YouTubeGroupsClient:
        return YouTubeGroupsClient(http=GoogleHttpClient(credentials=credentials))

    return _factory


# ============================================================================
# Purpose: Mirror a YouTube CMS content owner's groups into channel_groups —
#   titles, membership (adds AND removals), deactivation of vanished groups,
#   reactivation of reappearing keys. Mandatory dry-run; YouTube wins.
# Database/ORM: ChannelGroupORM/ChannelGroupMemberORM via the group store;
#   ApiConnectorCredentialORM read via resolve_connector_credentials.
# Standards: Global MANAGE_GROUPS fail-closed (group writes must not bypass
#   the group API's authorization); fetch completes before any write; single
#   tenant transaction so a mid-apply failure rolls groups + audit together;
#   GROUPS_SYNCED summary uses ACTUAL apply counts, never the plan's; canned
#   error details only — Google/credential exception text never reaches HTTP.
# Blast Radius: Group naming/membership/active state, audit. Finance
#   group-scope rollups change composition only as the CMS does. No channel
#   rows are ever created here (unknown members surface in the response).
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_group_sync.py -> planner.
#   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py -> apply.
#   - File: backend/ums_smart_revenue/connectors/google/youtube_groups_client.py.
# ============================================================================
@router.post("/groups/sync")
def sync_channel_groups(
    payload: GroupSyncRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    groups: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    client_factory: Annotated[
        Callable[[Credentials], YouTubeGroupsClient], Depends(current_groups_client_factory)
    ],
) -> dict[str, object]:
    """Mirror the content owner's CMS groups locally, previewing or applying."""
    target_scope = AccessScope.global_scope()
    if not has_permission(user, Permission.MANAGE_GROUPS, target_scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_GROUPS.value}",
        )
    content_owner_id = payload.content_owner_id.strip()
    reason = payload.reason.strip()
    if not content_owner_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="content_owner_id is required",
        )
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reason is required",
        )

    try:
        credentials = resolve_connector_credentials(
            session=session,
            tenant_id=_resolve_tenant_uuid(user),
            # skipcq: SCT-A000 -- connector registry key, not a credential value.
            connector_key="youtube-analytics",
            account_id=content_owner_id,
        )
    except (CredentialNotFoundError, InactiveCredentialError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "No active youtube-analytics credential for this content owner; "
                "register one before syncing groups."
            ),
        ) from exc
    except OAuthRefreshError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Google credential token refresh failed; "
                "check that the credential secret is current."
            ),
        ) from exc

    client = client_factory(credentials)
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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "YouTube groups fetch failed; "
                "check connector configuration and account access."
            ),
        ) from exc

    local_groups = tuple(groups.list_synced_groups())
    member_ids = {cid for item in snapshot for cid in item.member_channel_ids}
    known = frozenset(
        entry.youtube_channel_id for entry in registry.list_channels_by_ids(member_ids)
    )
    plan = plan_group_sync(
        snapshot=snapshot, local_groups=local_groups, known_channel_ids=known
    )
    payload_out = _group_sync_plan_to_api(
        plan, dry_run=payload.dry_run, content_owner_id=content_owner_id
    )
    if payload.dry_run:
        return payload_out

    executed = apply_group_sync(
        plan,
        groups=groups,
        audit_sink=audit_sink,
        actor=user,
        scope=target_scope,
        content_owner_id=content_owner_id,
        reason=reason,
    )
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.GROUPS_SYNCED,
        entity_type="channel_group_sync",
        entity_id=content_owner_id,
        scope=target_scope,
        reason=reason,
        details={
            "content_owner_id": content_owner_id,
            "counts": executed,
            "unknown_channel_total": plan.unknown_channel_total,
            "non_channel_member_count": plan.non_channel_member_count,
        },
    )
    return payload_out


def _group_sync_plan_to_api(
    plan: GroupSyncPlan, *, dry_run: bool, content_owner_id: str
) -> dict[str, object]:
    """Render a sync plan as the API response body (identical for both modes)."""
    return {
        "dry_run": dry_run,
        "content_owner_id": content_owner_id,
        "counts": dict(plan.counts),
        "unknown_channel_total": plan.unknown_channel_total,
        "non_channel_member_count": plan.non_channel_member_count,
        "groups": [
            {
                "cms_group_id": entry.cms_group_id,
                "outcome": entry.outcome.value,
                "title": entry.title,
                "local_group_id": entry.local_group_id,
                "name_change": list(entry.name_change) if entry.name_change else None,
                "active_change": list(entry.active_change) if entry.active_change else None,
                "members_added": list(entry.members_added),
                "members_removed": list(entry.members_removed),
                "unknown_channel_ids": list(entry.unknown_channel_ids[:50]),
                "unknown_channel_count": len(entry.unknown_channel_ids),
            }
            for entry in plan.entries
        ],
    }
```

Notes for the implementer:
- `_resolve_tenant_uuid` exists in `api/connectors.py` — check whether `api/channels.py` already
  has an equivalent; import/reuse rather than duplicating if one is importable, else replicate the
  small helper with a comment pointing at the original.
- `Credentials` type comes from wherever `resolve_connector_credentials` declares it (google
  oauth2). Match its import.
- Route placement: after `import_channels`, before the dynamic PATCH routes.

- [ ] **Step 4: Run the route tests — all PASS.** Then
`UMS_TEST_DATABASE_URL=... uv run python -m pytest tests/api tests/org -q` — no regressions.

- [ ] **Step 5: Lint + commit**

```bash
git add backend/ums_smart_revenue/api/channels.py tests/api/test_channel_group_sync_api.py
git commit -m "feat(api): add POST /channels/groups/sync CMS mirror route"
```

---

## Task 7: synced-group lockdown in the groups API

**Files:**
- Modify: `backend/ums_smart_revenue/api/groups.py`
- Test: append to `tests/api/test_groups_api.py`

- [ ] **Step 1: Write the failing tests** (follow `test_groups_api.py` fixtures):

1. `PATCH /groups/{id}` with `name` on a synced group → 409, detail contains
   `managed by CMS sync`.
2. `PATCH /groups/{id}` with **only** `active` on a synced group → 200.
3. `POST /groups/{id}/members` on a synced group → 409.
4. `DELETE /groups/{id}/members/{channel_id}` on a synced group → 409.
5. All four operations still work on a manual group (no `cms_group_id`) — guard against
   over-blocking.

Seed the synced group through the store (`create_group(..., cms_group_id="cms-x")`) the same way
the file seeds groups today.

- [ ] **Step 2: Run — must FAIL** (currently these return 200).

- [ ] **Step 3: Implement**

In `api/groups.py`, add one helper next to `_require_manageable_group`:

```python
def _reject_synced_group_edit(group: ChannelGroupEntry) -> None:
    """409 any rename/membership edit on a CMS-synced group (sync would revert it)."""
    if group.cms_group_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"synced group {group.id} is managed by CMS sync; "
                "edit it in YouTube Content Manager"
            ),
        )
```

Wire it:
- `update_group`: capture the group returned by `_require_manageable_group` (it returns the
  group — verify and adapt if the current call discards it) and call
  `_reject_synced_group_edit(group)` **only when `payload.name is not None`** — an `active`-only
  PATCH stays allowed.
- `add_group_members`: call it on the captured group unconditionally.
- `remove_group_member`: capture the group and call it unconditionally.

- [ ] **Step 4: Run** `uv run python -m pytest tests/api/test_groups_api.py -q` — all pass
(new + pre-existing).

- [ ] **Step 5: Lint + commit**

```bash
git add backend/ums_smart_revenue/api/groups.py tests/api/test_groups_api.py
git commit -m "feat(api): lock CMS-synced groups against manual edits"
```

---

## Task 8: Postgres-tier tests

**Files:**
- Test: `tests/api/test_channel_group_sync_postgres.py`

Follow `tests/api/test_channels_import_postgres.py` EXACTLY — same `require_postgres_url`,
upgrade-once, owner-engine purge, tenant seeding, and app-construction patterns. Override the
groups-client factory dependency with a fake snapshot (no network on the Postgres tier either).

- [ ] **Step 1: Write the tests**

1. `test_sync_persists_groups_on_postgres` — apply a snapshot (one group, one known member);
   read back group + membership + `cms_group_id` through the tenant lane.
2. `test_synced_groups_are_tenant_isolated` — tenant B's lane sees nothing (bare SELECT, no
   WHERE tenant_id — the RLS-only pattern).
3. `test_mid_apply_failure_rolls_back_groups_and_audit_on_postgres` — snapshot with two groups;
   wrap the group store (dependency override delegating to the real
   `SqlAlchemyChannelGroupRegistry`) so the SECOND `create_group` raises after the first group
   and its audit row are written; assert: request fails, NEITHER group exists, `audit_logs`
   count unchanged. Include the anti-vacuity guard from the import PG tests (assert in-flight
   rows existed before the failure) — read how `test_channels_import_postgres.py` does it and
   reuse the mechanism.

- [ ] **Step 2: Run**

`UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/test_ums uv run python -m pytest tests/api/test_channel_group_sync_postgres.py -v`
Expected: 3 passed. If the rollback test reveals audit rows surviving, that is a REAL finding —
keep the correct assertion, report it, do not weaken.

- [ ] **Step 3: Regression sweep**

`UMS_TEST_DATABASE_URL=... uv run python -m pytest tests/api tests/org tests/tenancy tests/connectors -q`

- [ ] **Step 4: Lint + commit**

```bash
git add tests/api/test_channel_group_sync_postgres.py
git commit -m "test(api): postgres-tier isolation and rollback for group sync"
```

---

## Task 9: trackers

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1:** In `Docs/01_IMPLEMENTATION_PLAN.md`, Phase 1 "Company/sector/group mapping"
item: append a note that CMS group sync SHIPPED (2026-08-05) — `POST /channels/groups/sync`
mirrors YouTube CMS groups (titles, membership incl. removals, deactivate/reactivate), YouTube is
now the grouping source of truth for synced groups, and the groups API locks synced groups
against manual edits. Update the Status header date to 2026-08-05 with a one-line note.

- [ ] **Step 2:** In `Docs/15_DELIVERY_BACKLOG.md`, add a ✅ entry near the Registry/grouping
items describing the sync (route, mirror semantics, lockdown, unknown-channel surfacing) and
noting the import's `group_id` CSV column is now legacy-but-working (sync converges whatever it
created). Reference the spec path.

- [ ] **Step 3:** `git diff --check` then commit:

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(plan): mark CMS group sync shipped"
```

---

## Task 10: full-suite validation

- [ ] `uv run python -m ruff check backend tests scripts` — clean
- [ ] `git diff --name-only 557ec1f0...HEAD -- '*.py' | tr '\n' ' ' | xargs -r uv run python -m ruff format --check` — clean
- [ ] `git diff --name-only 557ec1f0...HEAD -- '*.py' | xargs -r awk 'length > 100 {print FILENAME":"FNR}'` — empty
- [ ] `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/test_ums uv run python -m pytest -q` — 0 failed (baseline was 2543 on main; expect ~2600+)
- [ ] `git diff --check` — clean

ONE migration in this PR: `20260805_0001_channel_group_content_owner` (additive
nullable `channel_groups.content_owner_id`, revises `20260803_0001`). Run
`uv run python -m alembic -c alembic.ini upgrade head` BEFORE exercising the app
— the sync and import paths both read and write that column — and verify
`uv run python -m alembic -c alembic.ini heads` reports the single head
`20260805_0001`. Rollback is `downgrade 20260803_0001` after reverting the code.
