# Owner-Stamp Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the group-ownership lifecycle: the import can no longer adopt an existing owner-NULL group (Path A — row error directing the operator to sync), and a new `DELETE /groups/{id}/content-owner` admin action clears a wrong stamp so the correct owner's sync can re-adopt.

**Architecture:** One new predicate in the pure import planner + removal of the now-unreachable Path B disclosure (`will_adopt_content_owner`) end to end; a fail-closed write-boundary refusal in `channel_import_apply.py` for the mid-flight case; a new `clear_content_owner` store method (SQL `FOR UPDATE`); a thin DELETE route in `api/groups.py` gated at global `MANAGE_GROUPS` using the atomic audit sink.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.x, pytest, ruff (100-char). **No migration.**

**Spec:** `Docs/superpowers/specs/2026-08-06-owner-stamp-recovery-design.md`
**Branch:** `feat/owner-stamp-recovery` off `a84b885b` (spec committed `37360076`)

---

## Conventions (identical to the last two PRs)

- **All Python via `uv run`.** Bare `python -m pytest` FAILS.
- **Set `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/test_ums`
  for every pytest run** (Postgres-tier tests raise, never skip). Container:
  `docker run -d --name ums-mig-pg-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=test_ums -p 55432:5432 postgres:18-alpine`
- Test root `./tests`. Lines ≤ 100. `ruff check` + `ruff format --check` before each commit
  (format if needed, then re-run tests). **Commits trailer-free** — no AI attribution.
- Docstrings on public surface; OPUS block comments matching file neighbours.
- Binding #169 invariants: audit summary/response content comes from write-boundary outcomes,
  not plans; `str(exc)` never reaches HTTP details; **any route writing a domain row + an audit
  row in one request uses `current_atomic_audit_sink`** (importable from
  `ums_smart_revenue.api.channels`).

## Resolved facts (verified 2026-08-06 on `a84b885b` — do not re-derive)

- `org/channel_import.py`: `_blocked_group_reason(group_id, *, archived_group_ids,
  foreign_owner_group_ids)` at ~line 394 returns row-error reasons for archived and
  foreign-owner keys; the docstring says owner-NULL is "deliberately not blocked — it is
  adoptable". `plan_channel_import(...)` takes `adoptable_group_ids: frozenset[str]` (~line 431)
  and sets `will_adopt_content_owner` (~lines 491/515/560) on the plan-entry dataclass
  (~line 351).
- `org/channel_import_apply.py`: `plan_channel_import_with_stores(...)` threads
  `adoptable_group_ids=frozenset(groups.list_adoptable_cms_group_ids(group_ids))` (~line 137).
  The apply's group attachment performs the actual adoption (Path B). There is a typed
  `ChannelImportArchivedGroupError` precedent for write-boundary group refusals; the apply-side
  execution entries carry `adopted_content_owner` (rendered at `api/channels.py:1181`).
- `api/channels.py`: `will_adopt_content_owner` appears in two response models (~lines 191,
  231) and two renderers (~lines 856, 1181/1207).
- `org/channel_groups.py`: `require_adoptable_owner(current, incoming, *, group_id)` raises
  `ChannelGroupOwnerReassignmentError` on owner→different-owner; called from the in-memory
  store (~line 331) and the SQL store (`sql_channel_groups.py` ~line 486) inside
  `update_group(*, group_id, name, active, content_owner_id=None)` (adopt-only enforced
  in-store). `ChannelGroupEntry` has `content_owner_id`. `get_group_by_cms_id(...,
  for_update=...)` is the `FOR UPDATE` precedent.
- `api/groups.py`: routes use the plain `current_audit_sink` (pre-existing, predates the
  atomic invariant — do NOT rewire them in this PR; the NEW route uses the atomic sink).
  `remove_group_member` shows the reason-as-query-param pattern
  (`reason: Annotated[str, Query(min_length=1)]` + `_normalize_query_reason`);
  `_registry_not_found` maps store KeyError → 404; `_reject_synced_group_edit` exists.
  `AccessScope.global_scope()` + `Permission.MANAGE_GROUPS` + `has_permission` are the gate
  pieces (see the sync route in `api/channels.py` for the exact 403 shape).

## File map

| File | Change |
| --- | --- |
| `org/channel_import.py` | + adoptable predicate; − `will_adopt_content_owner` |
| `org/channel_import_apply.py` | − adoption execution; + write-boundary owner-NULL refusal; − `adopted_content_owner` |
| `api/channels.py` | − `will_adopt_content_owner` from models/renderers |
| `org/channel_groups.py` + `org/sql_channel_groups.py` | + `clear_content_owner` |
| `api/groups.py` | + `DELETE /groups/{group_id}/content-owner` |
| trackers | Docs/01 + Docs/15 |

---

## Task 1: Path A — planner refusal and Path B removal

**Files:**
- Modify: `backend/ums_smart_revenue/org/channel_import.py`
- Modify: `backend/ums_smart_revenue/org/channel_import_apply.py`
- Modify: `backend/ums_smart_revenue/api/channels.py`
- Tests: `tests/org/test_channel_import_planner.py`, `tests/org/` apply tests,
  `tests/api/test_channels_import_api.py` (all: update in place)

This is a **behaviour flip on existing, review-hardened code**. Read all three files' relevant
sections COMPLETELY before editing. Work test-first per change:

- [ ] **Step 1: Planner — failing tests first.** In `tests/org/test_channel_import_planner.py`,
  find the existing Path B tests (search `will_adopt`). Replace them with Path A expectations:
  a row whose `group_id` is in `adoptable_group_ids` produces outcome `ERROR` with a reason
  containing `exists without a content owner` and `POST /channels/groups/sync`; rows with no
  group or unknown-group keys are unaffected; archived/foreign predicates unchanged. Add a test
  asserting `ChannelImportPlanEntry` has no `will_adopt_content_owner` attribute
  (`assert not hasattr(entry, "will_adopt_content_owner")`). Run → must FAIL.

- [ ] **Step 2: Implement in `channel_import.py`.** Extend `_blocked_group_reason` with the
  third predicate (thread `adoptable_group_ids` and the request-level `content_owner_id` into
  it — match how the other two sets reach it):

```python
    if group_id in adoptable_group_ids:
        return (
            f"channel group {group_id} exists without a content owner; run "
            f"POST /channels/groups/sync for content owner {content_owner_id} "
            "to adopt it, or clear/archive the group if it is stale"
        )
```

  Remove `will_adopt_content_owner` from the plan-entry dataclass and every assignment
  (~lines 351/491/515/560). Update the module docstrings that describe Path B disclosure
  (search `adopt` in the file) so no comment claims the import adopts. Keep the
  `adoptable_group_ids` parameter — it now feeds the refusal.

- [ ] **Step 3: Apply layer.** In `channel_import_apply.py`: the plan-time refusal makes the
  adoption execution path unreachable for planned rows, but the WRITE BOUNDARY must fail closed
  for the mid-flight case (a group's stamp cleared between plan and apply, making it
  owner-NULL under the lock). Find where the apply attaches/creates groups under the locked
  re-read and, where it currently adopts an owner-NULL group, raise the typed error family the
  file already uses for archived groups (read `ChannelImportArchivedGroupError` and its route
  mapping; add a sibling `ChannelImportAdoptableGroupError` mapped the same way — 409 with a
  canned detail). Remove the `adopted_content_owner` execution field and its plumbing. Update
  the apply-layer tests (find them: `grep -rln "adopted_content_owner\|will_adopt" tests/`)
  to expect the refusal instead of adoption — never weaken unrelated assertions.

- [ ] **Step 4: Route render.** In `api/channels.py`, remove `will_adopt_content_owner` from
  both response models and both renderers (~191/231/856/1181/1207) and fix the two OPUS/doc
  comments that describe it (~176/834/1167). Update
  `tests/api/test_channels_import_api.py`: Path B assertions (search `will_adopt`) become
  Path A ones — dry-run shows the row error for an owner-NULL target; apply 422s; response
  JSON contains no `will_adopt_content_owner` key.

- [ ] **Step 5: Green + sweep.**
  `UMS_TEST_DATABASE_URL=... uv run python -m pytest tests/org tests/api -q` → all pass.
  Then repo-wide: `grep -rn "will_adopt\|adopted_content_owner" backend/ tests/ | grep -v pycache`
  → EMPTY (any straggler is a missed site; fix it).

- [ ] **Step 6: Lint + commit**

```bash
git add backend/ums_smart_revenue/org/channel_import.py \
        backend/ums_smart_revenue/org/channel_import_apply.py \
        backend/ums_smart_revenue/api/channels.py tests/
git commit -m "feat(org): refuse import adoption of owner-NULL groups (Path A)"
```

---

## Task 2: `clear_content_owner` store method

**Files:**
- Modify: `backend/ums_smart_revenue/org/channel_groups.py`
- Modify: `backend/ums_smart_revenue/org/sql_channel_groups.py`
- Tests: `tests/org/test_clear_content_owner.py` (new) + `tests/org/test_sql_channel_groups.py` (append)

- [ ] **Step 1: Failing tests.** New `tests/org/test_clear_content_owner.py` (in-memory store,
  follow the file style of `tests/org/test_list_synced_groups.py`):
  1. Clearing a stamped group returns the entry with `content_owner_id is None`; name,
     `cms_group_id`, members, `active` unchanged.
  2. Clearing an owner-NULL group raises the typed no-stamp error (define
     `ChannelGroupNoOwnerStampError(ValueError)` in `channel_groups.py` next to
     `ChannelGroupOwnerReassignmentError`, same docstring style).
  3. Unknown group raises `KeyError` (the store's existing unknown-group convention).
  4. A cleared group is adoptable again: `list_adoptable_cms_group_ids({key})` includes its key.
  Run → FAIL (`AttributeError: clear_content_owner`).

- [ ] **Step 2: Implement.** Protocol + in-memory + SQL:

```python
    def clear_content_owner(self, *, group_id: str) -> ChannelGroupEntry:
        """Erase a group's owner stamp, returning it to the adoptable pool.

        The adopt-only guard governs SETTING an owner; this is the one
        sanctioned eraser (admin recovery for a wrong stamp). Raises
        ChannelGroupNoOwnerStampError when there is nothing to clear.
        """
```

  SQL implementation: load the row by id, tenant-scoped, **`.with_for_update()`** (mirror how
  `get_group_by_cms_id(for_update=True)` does it) so a concurrent sync-adopt serializes against
  the clear; `KeyError` if missing; typed error if `content_owner_id is None`; else set to
  `None`, flush, return `_to_entry(...)` with full membership (match the file's other
  single-group returns). Do NOT route through `require_adoptable_owner`.

- [ ] **Step 3: SQL tests** appended to `tests/org/test_sql_channel_groups.py`: clear round-trip
  persists NULL; no-stamp typed error; cross-tenant clear raises `KeyError` (invisible row).

- [ ] **Step 4: Green** (`tests/org -q`), lint, commit:

```bash
git add backend/ums_smart_revenue/org/channel_groups.py \
        backend/ums_smart_revenue/org/sql_channel_groups.py \
        tests/org/test_clear_content_owner.py tests/org/test_sql_channel_groups.py
git commit -m "feat(org): clear a channel group's content-owner stamp"
```

---

## Task 3: `DELETE /groups/{group_id}/content-owner`

**Files:**
- Modify: `backend/ums_smart_revenue/api/groups.py`
- Test: `tests/api/test_groups_api.py` (append)

- [ ] **Step 1: Failing tests** (follow the file's fixtures; seed synced+stamped groups through
  the store):
  1. 403 without global `MANAGE_GROUPS` (a per-group-manageable-but-not-global principal if the
     fixtures support one; otherwise a no-grant principal).
  2. 404 unknown group id.
  3. 409 when `content_owner_id` is already NULL (both a synced owner-NULL group and a manual
     group — the route cares only about the stamp).
  4. Happy path: 200; response entry shows `content_owner_id: null`; name/members/`active`/
     `cms_group_id` unchanged; audit record present with `action == "content_owner_cleared"`,
     `previous_content_owner_id` equal to the old owner, and the request reason.
  5. Blank reason → 422 (existing query-reason behaviour).
  Run → FAIL with 404/405 (route absent).

- [ ] **Step 2: Implement.** In `api/groups.py`, next to `remove_group_member`:

```python
@router.delete("/{group_id}/content-owner")
def clear_group_content_owner(
    group_id: str,
    reason: Annotated[str, Query(min_length=1)],
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelGroupRegistryStore, Depends(sql_group_registry_from_session)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_atomic_audit_sink)],
) -> dict[str, object]:
    """Erase a group's content-owner stamp so the right owner's sync re-adopts it."""
    target_scope = AccessScope.global_scope()
    if not has_permission(user, Permission.MANAGE_GROUPS, target_scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_GROUPS.value}",
        )
    normalized_reason = _normalize_query_reason(reason)
    previous = registry.get_group(group_id)
    if previous is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown group")
    try:
        updated = registry.clear_content_owner(group_id=group_id)
    except KeyError as exc:
        raise _registry_not_found(exc) from exc
    except ChannelGroupNoOwnerStampError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"group {group_id} has no content-owner stamp to clear",
        ) from exc
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.GROUP_UPDATED,
        entity_type="channel_group",
        entity_id=updated.id,
        scope=target_scope,
        reason=normalized_reason,
        details={
            "action": "content_owner_cleared",
            "cms_group_id": updated.cms_group_id,
            "previous_content_owner_id": previous.content_owner_id,
        },
    )
    response = updated.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response
```

  Adapt to the file's real imports and audit helper: if `_audit_group_change` can carry the
  custom details/action cleanly, prefer it; otherwise call `record_audit_event` directly as
  above (import it the way `api/channels.py` does). NOTE: `get_group` may be active-only —
  verify; if it is, use the store read that sees inactive groups (a stamp on an archived group
  must still be clearable) and adjust the 404 path accordingly. Add an OPUS block comment
  matching the file's route style: purpose (the one sanctioned stamp eraser), the global-gate
  rationale (ownership governs which owner's sync controls the group — tenant-level
  governance), and the atomic-sink invariant. Check the sync route's model in
  `api/channels.py` for wording.

- [ ] **Step 3: Adoptability round-trip test** (same test file): stamped group → DELETE clears →
  a subsequent sync apply (drive the sync route with a fake snapshot containing the group's
  key, per `tests/api/test_channel_group_sync_api.py`'s fake-factory technique) re-adopts it
  with the new owner. This is the recovery loop end-to-end.

- [ ] **Step 4: Green + sweep** (`tests/api tests/org -q`), lint, commit:

```bash
git add backend/ums_smart_revenue/api/groups.py tests/api/test_groups_api.py
git commit -m "feat(api): admin action to clear a group's content-owner stamp"
```

---

## Task 4: Postgres tier

**Files:**
- Test: `tests/api/test_owner_stamp_recovery_postgres.py` (new)

Follow `tests/api/test_channel_group_sync_postgres.py` infrastructure verbatim (upgrade-once,
owner-engine purge, tenant seeding, app construction, `_tenant_audit_log_count`).

- [ ] **Step 1: Tests.**
  1. `test_clear_persists_and_group_is_readoptable_on_postgres` — stamp → DELETE →
     tenant-lane read shows NULL; a sync apply with the key re-adopts.
  2. `test_clear_serializes_against_concurrent_adopt_on_postgres` — two sessions: hold the
     row `FOR UPDATE` via one store's `clear_content_owner` path mid-transaction while a
     second session's adopt (`update_group(content_owner_id=...)`) blocks until commit;
     assert the final state matches commit order (use the two-session technique the PG-tier
     files already use for lock proofs; if none exists, a straightforward
     thread-with-timeout proof is acceptable — keep it deterministic).
  3. `test_clear_route_lost_commit_persists_no_audit_rows_on_postgres` — the #169 atomic-sink
     invariant applied to the new route: force the tenant commit to fail after a successful
     handler return; assert the stamp is unchanged AND no new audit rows survive (mirror
     `test_tenant_commit_failure_persists_no_audit_rows_on_postgres`).
- [ ] **Step 2: Run** the file → 3 passed; then the full PG sweep
  `tests/api tests/org tests/tenancy -q` → no regressions.
- [ ] **Step 3: Lint + commit**

```bash
git add tests/api/test_owner_stamp_recovery_postgres.py
git commit -m "test(api): postgres-tier clear-stamp serialization and atomicity"
```

---

## Task 5: Trackers

- [ ] In `Docs/15_DELIVERY_BACKLOG.md`, extend the CMS-group-sync ✅ entry: Path A shipped —
  the import now REFUSES adopting owner-NULL groups (row error naming the sync remedy); sync
  is the only stamp-writer on existing groups; `DELETE /groups/{id}/content-owner`
  (global `MANAGE_GROUPS`, atomic audit) is the sanctioned recovery for a wrong stamp.
- [ ] In `Docs/01_IMPLEMENTATION_PLAN.md`, append one line to the Phase 1 group-mapping item
  and refresh the Status header date to 2026-08-06.
- [ ] `git diff --check`, commit:

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(plan): mark owner-stamp recovery shipped"
```

---

## Task 6: Full-suite validation

- [ ] `uv run python -m ruff check backend tests scripts` — clean
- [ ] `git diff --name-only a84b885b...HEAD -- '*.py' | tr '\n' ' ' | xargs -r uv run python -m ruff format --check` — clean
- [ ] `git diff --name-only a84b885b...HEAD -- '*.py' | xargs -r awk 'length > 100 {print FILENAME":"FNR}'` — empty
- [ ] `UMS_TEST_DATABASE_URL=... uv run python -m pytest -q` — 0 failed
- [ ] `uv run python -m alembic -c alembic.ini heads` — single head `20260805_0001` (no migration in this PR)
- [ ] `git diff --check` — clean
