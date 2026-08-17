# Groups View UI (PR-A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new Groups view in the SPA: group table with owner stamps, credential-fed content-owner picker, sync dry-run→apply stepper, clear-stamp and archive actions — plus the two additive backend fields it needs.

**Architecture:** Two small backend additions (`content_owner_id` in `to_api()`, `can_manage_groups` capability), then frontend bottom-up: types → hooks → shared stepper/table primitives → the view → AppShell wiring. Every mutating flow follows the API's dry-run-first, reason-required contracts.

**Tech Stack:** React 19 + Vite + Tailwind 4 (existing SPA, no router — `ViewKey` switch), vitest + @testing-library, bun as the runner; backend FastAPI + pytest via uv.

Spec: `Docs/superpowers/specs/2026-08-07-import-sync-ui-design.md`
Branch: `feat/groups-view-ui` off `origin/main` at `cc8892da`

---

## Environment (every task, non-negotiable)

- Backend Python via `uv run` only. Pytest needs
  `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/test_ums`;
  container `ums-mig-pg-test` must be Up (`docker ps`); no concurrent
  pytest against it. Backend line length 100 hard.
- Frontend commands run from `frontend/`: tests `bun run test`
  (vitest run), types `bunx tsc --noEmit`. bun 1.3.14 is installed.
- Commits: conventional message, NO trailers. Stage explicit paths only;
  never `git add -A` (tree carries dirt: `skills-lock.json`, `ci/` flags,
  untracked `.agents/`, `.superpowers/` — never stage them).
- No migration anywhere in this PR.

## Verified anchors (2026-08-07, at `cc8892da`)

- `GET /groups` → bare `list[group.to_api()]` (api/groups.py:185,
  per-group `_can_view_group` filter). `to_api()` at
  org/channel_groups.py:107 returns id/name/group_type/active/
  channel_ids/cms_group_id — NO content_owner_id yet.
- Clear route DELETE /groups/{id}/content-owner returns declared
  `ClearContentOwnerResponse` (api/groups.py:150) whose docstring says
  "to_api omits content_owner_id" — that sentence must be updated by
  Task 1. PATCH /groups/{id} takes body `GroupUpdateRequest` incl.
  `reason` and `active`.
- `POST /channels/groups/sync` request `{content_owner_id, dry_run,
  reason}`; response = `GroupSyncResult` (api/channels.py:230):
  `{dry_run, content_owner_id, counts: dict[str,int],
  unknown_channel_total, non_channel_member_count, groups:
  [GroupSyncGroupResult]}` with per-group `{cms_group_id, outcome, title,
  local_group_id, name_change: [from,to]|null, active_change:
  [bool,bool]|null, members_added: [..], members_removed: [..],
  unknown_channel_ids (≤50), unknown_channel_count,
  will_adopt_content_owner}`. Wire casing: snake_case (no alias
  generator).
- `GET /session/me` capabilities (api/session.py:64-78) are camelCase on
  the wire (`to_camel` alias). `can_manage_registry` derives from
  MANAGE_ORG_MAPPING (:157). No groups capability exists yet.
- `GET /connectors/credentials?connector_keys=...` exists; frontend
  `useConnectors.ts` already fetches it (`ConnectorCredentialListResponse`
  in types.ts; entries carry `connector_key`, `account_id`, `status`).
- Frontend nav: `ViewKey` union + `VIEW_COPY` + `NAV_GROUPS` in
  `src/lib/mock/data.ts` (:6/:17/:49); icons `NAV_ICONS` in
  `src/components/srcc/icons.tsx:12`; AppShell holds
  `useState<ViewKey>("command")` and renders `{view === "x" && <XView/>}`;
  capability mapping `capabilitiesToPermissions` (AppShell.tsx:109).
- Hook patterns: auto-fetch `useChannels.ts` (useCallback + useAsync →
  `AsyncState<T>` = {data, loading, error, reload}); mutation
  `useChannelMappingAction` (returns a useCallback'd async fn). Hook
  tests: `renderHook` + `<TenantProvider initialSlug="ums">` wrapper +
  `vi.stubGlobal("fetch", vi.fn())` (see
  `src/lib/api/__tests__/useChannelIssues.test.tsx`).
- View tests live in `src/components/srcc/views/__tests__/`;
  `AppShell.test.tsx` in `src/components/srcc/__tests__/`.

## Sequencing

Task 1 (backend) → 2 (types) → 3 (hooks) → 4 (primitives) → 5 (view:
table+actions) → 6 (view: sync stepper) → 7 (AppShell wiring) → 8
(validation + trackers). Sequential; orchestrator reviews the committed
diff between tasks.

---

### Task 1: Backend — `content_owner_id` in group responses + `can_manage_groups` capability (sonnet)

**Files:**
- Modify: `backend/ums_smart_revenue/org/channel_groups.py:107` (to_api)
- Modify: `backend/ums_smart_revenue/api/groups.py:150` (docstring only)
- Modify: `backend/ums_smart_revenue/api/session.py` (capability field + derivation)
- Test: `tests/api/test_groups_api.py`, `tests/api/test_session_api.py`

- [ ] **Step 1: Write the failing tests**

In `tests/api/test_groups_api.py` (follow the file's existing client/seed
helpers — read its first ~150 lines first; `_create_group` exists at
:145):

```python
def test_list_groups_carries_content_owner_id(client_and_stores):
    client, _registry, groups = client_and_stores
    _create_group(groups, name="TV Sector", cms_group_id="deWjsA",
                  content_owner_id="COabc")
    _create_group(groups, name="Manual Group")
    payload = _get(client, "/groups")
    by_name = {g["name"]: g for g in payload}
    assert by_name["TV Sector"]["content_owner_id"] == "COabc"
    assert by_name["Manual Group"]["content_owner_id"] is None
```

(Adapt fixture/helper names to what the file actually uses — the
assertion body is the contract. If a nearby test asserts an exact
response key set, add `content_owner_id` to that expected set in the same
commit.)

In `tests/api/test_session_api.py`, mirror the file's existing
capability-derivation tests (find the `can_manage_registry` pair and copy
their shape):

```python
def test_can_manage_groups_true_with_manage_groups_grant(...):
    # principal fixture with Permission.MANAGE_GROUPS @ global
    assert payload["capabilities"]["canManageGroups"] is True

def test_can_manage_groups_false_without_grant(...):
    assert payload["capabilities"]["canManageGroups"] is False
```

- [ ] **Step 2: Run to verify both fail**

Run: `uv run pytest tests/api/test_groups_api.py -q -k content_owner_id` →
FAIL (KeyError `content_owner_id`).
Run: `uv run pytest tests/api/test_session_api.py -q -k manage_groups` →
FAIL (KeyError `canManageGroups`).

- [ ] **Step 3: Implement**

`org/channel_groups.py` to_api gains one line:

```python
            "cms_group_id": self.cms_group_id,
            "content_owner_id": self.content_owner_id,
```

`api/groups.py:150` `ClearContentOwnerResponse` docstring: replace the
sentence claiming `to_api` omits the field with: "``to_api`` now carries
``content_owner_id`` on every group response; this model still declares
it explicitly because the field is this route's entire outcome, alongside
the audit event."

`api/session.py`: add `can_manage_groups: bool` to `SessionCapabilities`
(keep field order grouped with the other `can_manage_*`), and in
`_derive_capabilities`:

```python
        can_manage_groups=_can(Permission.MANAGE_GROUPS),
```

- [ ] **Step 4: Run green + neighbours**

Run: `uv run pytest tests/api/test_groups_api.py tests/api/test_session_api.py tests/api/test_channels_import_api.py tests/api/test_channel_group_sync_api.py -q`
Expected: all pass (the sync/import suites prove no group-response
consumer broke).

- [ ] **Step 5: Lint + types + commit**

Run: `uv run ruff check backend tests && uv run ruff format --check backend tests && uv run mypy backend/ums_smart_revenue/org/channel_groups.py backend/ums_smart_revenue/api/session.py backend/ums_smart_revenue/api/groups.py`

```bash
git add backend/ums_smart_revenue/org/channel_groups.py backend/ums_smart_revenue/api/groups.py backend/ums_smart_revenue/api/session.py tests/api/test_groups_api.py tests/api/test_session_api.py
git commit -m "feat(api): expose group content_owner_id + can_manage_groups capability"
```

### Task 2: Frontend types (haiku/sonnet)

**Files:**
- Modify: `frontend/src/lib/api/types.ts`

- [ ] **Step 1: Locate the SessionCapabilities type and the group/connector types region** (read the file's contract-block conventions; place new types near related ones, update the file-top Connections block if it enumerates sources).

- [ ] **Step 2: Add the types** (snake_case for group/sync wire shapes; camelCase only inside SessionCapabilities):

```typescript
// One row of GET /groups (backend ChannelGroupEntry.to_api,
// org/channel_groups.py:107 — content_owner_id added 2026-08-07).
export type ChannelGroupApiEntry = {
  id: string;
  name: string;
  group_type: string;
  active: boolean;
  channel_ids: string[];
  cms_group_id: string | null;
  content_owner_id: string | null;
};

// POST /channels/groups/sync per-group result
// (backend GroupSyncGroupResult, api/channels.py:200).
export type GroupSyncGroupResult = {
  cms_group_id: string;
  outcome:
    | "CREATE"
    | "RENAME"
    | "MEMBERS_CHANGED"
    | "DEACTIVATE"
    | "REACTIVATE"
    | "UNCHANGED"
    | "CONFLICT";
  title: string | null;
  local_group_id: string | null;
  name_change: [string, string] | null;
  active_change: [boolean, boolean] | null;
  members_added: string[];
  members_removed: string[];
  unknown_channel_ids: string[];
  unknown_channel_count: number;
  will_adopt_content_owner: boolean;
};

// POST /channels/groups/sync response
// (backend GroupSyncResult, api/channels.py:230).
export type GroupSyncResult = {
  dry_run: boolean;
  content_owner_id: string;
  counts: Record<string, number>;
  unknown_channel_total: number;
  non_channel_member_count: number;
  groups: GroupSyncGroupResult[];
};
```

And in the existing `SessionCapabilities` type add (camelCase, matching
its siblings): `canManageGroups: boolean;`

- [ ] **Step 3: Compile-check**

Run (from `frontend/`): `bunx tsc --noEmit`
Expected: errors ONLY where SessionCapabilities is constructed in tests/
mocks missing the new key — fix each by adding `canManageGroups: false`
(or true where the fixture represents a manager). Re-run until clean.

- [ ] **Step 4: Run the frontend suite**

Run: `bun run test`
Expected: PASS (type-only change + fixture updates).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api/types.ts
# plus every fixture file tsc forced you to touch
git commit -m "feat(frontend): group + sync API types, canManageGroups capability"
```

### Task 3: Hooks — useGroups, useGroupSync, useClearOwnerStamp, useGroupArchive (sonnet)

**Files:**
- Create: `frontend/src/lib/api/useGroups.ts`
- Create: `frontend/src/lib/api/useGroupSync.ts`
- Test: `frontend/tests/lib/api/useGroups.test.tsx`
- Test: `frontend/tests/lib/api/useGroupSync.test.tsx`

All four hooks follow the two existing patterns EXACTLY — auto-fetch =
`useChannels.ts` shape; mutations = `useChannelMappingAction` shape
(a `useCallback`'d async fn over `useApiClient`). House contract blocks
on both files (copy the format from `useChannels.ts`).

- [ ] **Step 1: Write failing tests** (harness copied from
`__tests__/useChannelIssues.test.tsx`: TenantProvider wrapper +
`vi.stubGlobal("fetch", ...)`):

`useGroups.test.tsx` — resolves data:

```tsx
it("fetches GET /groups and returns entries", async () => {
  const GROUPS: ChannelGroupApiEntry[] = [{
    id: "g1", name: "TV Sector", group_type: "CUSTOM", active: true,
    channel_ids: ["UCa"], cms_group_id: "deWjsA", content_owner_id: "COabc",
  }];
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
    new Response(JSON.stringify(GROUPS), {
      status: 200, headers: { "Content-Type": "application/json" },
    }),
  );
  const { result } = renderHook(() => useGroups(), { wrapper });
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.data).toEqual(GROUPS);
  const [url] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
  expect(String(url)).toContain("/groups");
});
```

`useGroupSync.test.tsx` — three tests:
1. `runGroupSync` POSTs `/channels/groups/sync` with
   `{content_owner_id, dry_run: true, reason}` body (assert
   `JSON.parse(init.body)`), returns the typed result.
2. `clearOwnerStamp` DELETEs
   `/groups/g1/content-owner?reason=wrong%20stamp` (assert the reason is
   URL-encoded via `encodeURIComponent`).
3. `archiveGroup` PATCHes `/groups/g1` with `{active: false, reason}`.

- [ ] **Step 2: Run red**

Run: `bun run test -- useGroups useGroupSync`
Expected: FAIL — modules don't exist.

- [ ] **Step 3: Implement**

`useGroups.ts`:

```typescript
export function useGroups(): AsyncState<ChannelGroupApiEntry[]> {
  const client = useApiClient();
  const run = useCallback(
    () => client.get<ChannelGroupApiEntry[]>("/groups"),
    [client],
  );
  return useAsync(run);
}
```

`useGroupSync.ts` (three exported mutation hooks in one domain file):

```typescript
export function useGroupSyncAction(): (args: {
  contentOwnerId: string;
  dryRun: boolean;
  reason: string;
}) => Promise<GroupSyncResult> {
  const client = useApiClient();
  return useCallback(
    ({ contentOwnerId, dryRun, reason }) =>
      client.post<GroupSyncResult>("/channels/groups/sync", {
        content_owner_id: contentOwnerId,
        dry_run: dryRun,
        reason,
      }),
    [client],
  );
}

export function useClearOwnerStampAction(): (args: {
  groupId: string;
  reason: string;
}) => Promise<ClearOwnerStampResponse> {
  const client = useApiClient();
  return useCallback(
    ({ groupId, reason }) =>
      client.delete<ClearOwnerStampResponse>(
        `/groups/${encodeURIComponent(groupId)}/content-owner?reason=${encodeURIComponent(reason)}`,
      ),
    [client],
  );
}

export function useGroupArchiveAction(): (args: {
  groupId: string;
  active: boolean;
  reason: string;
}) => Promise<ChannelGroupApiEntry> {
  const client = useApiClient();
  return useCallback(
    ({ groupId, active, reason }) =>
      client.patch<ChannelGroupApiEntry>(
        `/groups/${encodeURIComponent(groupId)}`,
        { active, reason },
      ),
    [client],
  );
}
```

`ClearOwnerStampResponse` goes in types.ts in this task (mirror
`ClearContentOwnerResponse`, api/groups.py:150: the group fields +
`content_owner_id: null` + `audit_event` — type the audit_event loosely
as `{ event_type: string; reason: string }` plus index signature only if
the backend model demands more; read the model first).

- [ ] **Step 4: Run green**

Run: `bun run test -- useGroups useGroupSync`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api/useGroups.ts frontend/src/lib/api/useGroupSync.ts frontend/src/lib/api/types.ts frontend/tests/lib/api/useGroups.test.tsx frontend/tests/lib/api/useGroupSync.test.tsx
git commit -m "feat(frontend): group list + sync/clear/archive hooks"
```

### Task 4: Shared primitives — ActionStepper + OutcomeTable (sonnet)

**Files:**
- Create: `frontend/src/components/srcc/ActionStepper.tsx`
- Create: `frontend/src/components/srcc/OutcomeTable.tsx`
- Test: `frontend/tests/components/srcc/ActionStepper.test.tsx`
- Test: `frontend/tests/components/srcc/OutcomeTable.test.tsx`

Both are DUMB presentational components (flows own all state) — that is
what makes them reusable by PR-B's import stepper. Style with the
existing softdark utility classes — read `shared.tsx` (`Badge`, `Dot`)
and RegistryView's table markup first and reuse those class patterns; do
not invent a parallel visual language.

- [ ] **Step 1: Failing tests**

`ActionStepper.test.tsx`:

```tsx
it("renders step labels with the active one marked and fires onCancel", async () => {
  const onCancel = vi.fn();
  render(
    <ActionStepper
      steps={["Upload", "Preview", "Applied"]}
      activeIndex={1}
      onCancel={onCancel}
    >
      <p>step body</p>
    </ActionStepper>,
  );
  expect(screen.getByText("Preview").closest("[data-active=\"true\"]")).toBeTruthy();
  expect(screen.getByText("step body")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
  expect(onCancel).toHaveBeenCalledOnce();
});
```

`OutcomeTable.test.tsx` — rows render outcome chip + cells; a row with
`tone="warn"` carries a distinguishable attribute
(`data-tone="warn"`); empty rows array renders the provided empty label.

Component contracts (props):

```typescript
// ActionStepper
{ steps: string[]; activeIndex: number; onCancel: () => void;
  children: ReactNode }

// OutcomeTable
{ columns: string[];
  rows: Array<{ key: string; tone?: "warn" | "error";
                cells: ReactNode[] }>;
  emptyLabel: string }
```

- [ ] **Step 2: Run red** — `bun run test -- ActionStepper OutcomeTable` → FAIL (missing modules).

- [ ] **Step 3: Implement** both components minimally to the contracts
above (table = semantic `<table>` with the Registry table's class names;
chips via the existing `Badge` component from `shared.tsx`).

- [ ] **Step 4: Run green** — same command, PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/srcc/ActionStepper.tsx frontend/src/components/srcc/OutcomeTable.tsx frontend/tests/components/srcc/ActionStepper.test.tsx frontend/tests/components/srcc/OutcomeTable.test.tsx
git commit -m "feat(frontend): ActionStepper + OutcomeTable primitives"
```

### Task 5: GroupsView — table, empty states, row actions (opus)

**Files:**
- Create: `frontend/src/components/srcc/views/GroupsView.tsx`
- Test: `frontend/tests/components/srcc/views/GroupsView.test.tsx`

Props: `{ canManageGroups: boolean }`. House contract block at top.
Structure (keep the file under ~450 lines by extracting row/dialog
subcomponents within the file, RegistryView-style):

- `useGroups()` for data; loading row / error row exactly like
  `RegistryTableMessageRow` does it (read RegistryView:361 first).
- Table columns: Name · CMS id (render "manual" when `cms_group_id` is
  null) · Owner (short stamp; when null render an "unstamped" badge
  with `data-testid="owner-unstamped"`) · Members (`channel_ids.length`)
  · Status (active/archived) · Actions.
- Row actions (render ONLY when `canManageGroups`):
  - **Clear stamp** (only when `content_owner_id` is non-null): opens an
    inline confirm panel (component-local state, no portal) with a
    required reason input; confirm calls `useClearOwnerStampAction`,
    then `reload()`; ApiError → banner with `error.body?.detail ??
    error.message` verbatim.
  - **Archive / Restore**: same confirm-with-reason pattern via
    `useGroupArchiveAction({ active: !group.active })`.
  - Client-side validation: reason non-blank and free of ` ` before
    submit (mirror the backend 422 contract); the confirm button stays
    disabled otherwise.
- Empty states: no groups → the spec's "import a roster, then sync" line.

- [ ] **Step 1: Failing tests** (harness: copy the fetch-stub +
TenantProvider wrapper pattern from an existing view test — read
`__tests__/ConnectorsView.test.tsx` first; queue one fetch Response per
expected request):
  1. renders rows from GET /groups (owner stamp shown; "manual" for null
     cms id; unstamped badge for null owner),
  2. `canManageGroups={false}` hides every action button,
  3. clear-stamp flow: click → reason required (button disabled on blank)
     → confirm → DELETE fired with encoded reason → table refetched,
  4. archive flow PATCHes `{active:false, reason}`,
  5. ApiError 409 on clear → banner shows backend detail text verbatim,
  6. empty list → empty-state copy.

- [ ] **Step 2: Run red** — `bun run test -- GroupsView` → FAIL.
- [ ] **Step 3: Implement** to the structure above.
- [ ] **Step 4: Run green** — `bun run test -- GroupsView` → PASS; then full `bun run test` + `bunx tsc --noEmit`.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/srcc/views/GroupsView.tsx frontend/tests/components/srcc/views/GroupsView.test.tsx
git commit -m "feat(frontend): GroupsView table with clear-stamp and archive actions"
```

### Task 6: GroupsView — owner picker + sync stepper (opus)

**Files:**
- Modify: `frontend/src/components/srcc/views/GroupsView.tsx`
- Test: extend `frontend/tests/components/srcc/views/GroupsView.test.tsx`

- Header: owner picker `<select>` fed by `useConnectors` filtered to
  `connector_key === "youtube-analytics"` and ACTIVE status entries
  (read `useConnectors.ts` + `ConnectorCredentialListResponse` first for
  exact field names/status literal — reuse whatever constant/literal that
  file uses). Empty credential list → picker disabled + the spec's
  pointer copy toward Connectors. **Sync (dry-run)** button disabled
  without a selection or without `canManageGroups`.
- Clicking Sync swaps the table for the stepper (`ActionStepper`,
  steps `["Reason", "Preview", "Applied"]`):
  - **Reason step:** required reason input (same non-blank/no-NUL rule);
    "Run dry-run" calls `useGroupSyncAction({dryRun:true})`.
  - **Preview step:** `OutcomeTable` — one row per `groups[]` entry:
    outcome chip; title/name_change rendered as "old → new"; member
    delta "+N/−M"; active_change as "archived→active" etc.; CONFLICT
    rows `tone="warn"` and a remedy line naming the clear-stamp action /
    correct-owner sync. Below the table: `unknown_channel_total` line
    ("N in CMS but not in registry — import them first") when > 0.
    **Apply button disabled while any entry.outcome === "CONFLICT"**
    (the API would 409; say so in the disabled tooltip/title attr).
  - **Applied step:** calls `useGroupSyncAction({dryRun:false})`, shows
    `counts` summary + reason echo; "Back to groups" → close stepper +
    `reload()`.
  - Any ApiError in either call → banner with backend detail verbatim
    (503 credential detail gets the Connectors pointer appended, keyed on
    `error.status === 503`); stepper stays on the failed step, retry
    allowed.

- [ ] **Step 1: Failing tests** (queue fetch responses in order:
credentials list → groups list → sync dry-run → sync apply → groups
refetch):
  1. picker lists only active youtube-analytics account ids; disabled
     state + copy when none,
  2. dry-run POST carries `{content_owner_id, dry_run:true, reason}`,
  3. preview renders one row per group with outcome chips; CONFLICT row
     tone=warn and apply disabled,
  4. conflict-free preview → apply POST `dry_run:false` → applied step
     shows counts → back-to-groups refetches,
  5. 503 on dry-run → banner contains backend detail + Connectors
     pointer; stepper still on Reason step.

- [ ] **Step 2: Run red** — extended cases FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run green** — `bun run test -- GroupsView`, then full `bun run test` + `bunx tsc --noEmit`.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/srcc/views/GroupsView.tsx frontend/tests/components/srcc/views/GroupsView.test.tsx
git commit -m "feat(frontend): group sync stepper with credential-fed owner picker"
```

### Task 7: AppShell + nav wiring (sonnet)

**Files:**
- Modify: `frontend/src/lib/mock/data.ts` (:6 ViewKey, :17 VIEW_COPY, :49 NAV_GROUPS)
- Modify: `frontend/src/components/srcc/icons.tsx` (NAV_ICONS entry)
- Modify: `frontend/src/components/srcc/AppShell.tsx` (import, gate shape, render)
- Test: extend `frontend/tests/components/srcc/AppShell.test.tsx`

- [ ] **Step 1: Failing test** — extend AppShell.test.tsx (mirror how it
asserts existing nav items/views; read it first): clicking the "CMS
Groups" nav item renders the Groups view heading; the nav item exists in
the Workspace group after Registry.

- [ ] **Step 2: Run red.**

- [ ] **Step 3: Implement:**

`data.ts`: add `| "groups"` to `ViewKey` (after `"registry"`); add to
`VIEW_COPY`:

```typescript
  groups: {
    title: "CMS Groups",
    subtitle: "Content-owner group mirror, ownership stamps, and sync",
  },
```

Add to `NAV_GROUPS` Workspace items after registry:

```typescript
      { key: "groups", label: "CMS Groups", count: "CMS", icon: "groups" },
```

`icons.tsx`: add a `groups` entry to NAV_ICONS (simple folder/stack
glyph in the file's existing stroke style — copy the `wrap()` usage).

`AppShell.tsx`: import GroupsView; extend the permissions/gate shape
(where `canManageRegistry: boolean;` lives at :42) with
`canManageGroups: boolean;` derived in `capabilitiesToPermissions` from
`capabilities.canManageGroups`; render
`{view === "groups" && <GroupsView canManageGroups={permissions.canManageGroups} />}`
alongside the sibling views.

- [ ] **Step 4: Run green** — `bun run test`, `bunx tsc --noEmit`: all pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/mock/data.ts frontend/src/components/srcc/icons.tsx frontend/src/components/srcc/AppShell.tsx frontend/tests/components/srcc/AppShell.test.tsx
git commit -m "feat(frontend): Groups nav item + view wiring"
```

### Task 8: Validation + trackers (orchestrator)

- [ ] Frontend: `bun run test` (full) → 0 failures; `bunx tsc --noEmit` →
  clean; `bun run build` → succeeds.
- [ ] Backend: full `uv run pytest -q` with `UMS_TEST_DATABASE_URL` set,
  container up, nothing else on it → exit 0; `uv run ruff check backend
  tests`; `uv run ruff format --check backend tests scripts`; `uv run
  mypy` on the three touched backend modules; 100-char guard on changed
  backend files; `git diff origin/main --check`.
- [ ] Trackers (per-PR rule): `Docs/15_DELIVERY_BACKLOG.md` new dated
  entry (Groups view shipped: table/stamps/sync stepper/clear/archive +
  the two additive backend fields); `Docs/01_IMPLEMENTATION_PLAN.md`
  frontend-track note. Commit
  `docs: record Groups view UI in trackers`.
- [ ] PR report doc `Docs/pulls/2026-08-07-pr-tbd-groups-view-ui-report.md`
  in the house format; then the ONE batched push+PR ask to Mahmoud.

## Self-review (done at write time)

- Spec coverage: backend touches → Task 1; table/actions → Task 5;
  picker+stepper → Task 6; primitives → Task 4; hooks/types → Tasks 2-3;
  nav → Task 7; empty states → Tasks 5-6; error taxonomy → Tasks 5-6
  banner steps; trackers → Task 8. No-scheduled-sync-UI is a non-goal
  (nothing to build). PR-B items (import stepper, `can_import_channels`)
  are deliberately absent — separate plan.
- Types consistent: `ChannelGroupApiEntry`/`GroupSyncResult`/
  `GroupSyncGroupResult`/`ClearOwnerStampResponse` defined Task 2/3 and
  consumed Tasks 3/5/6; `canManageGroups` defined Task 1 (wire) / Task 2
  (type) / consumed Task 7.
- No placeholders: every code step carries real code or names the exact
  file+lines to mirror.
