# Import + group sync UI — design

Date: 2026-08-07
Status: approved (design walked through visually + in terminal, 2026-08-07)

## Context

The backend operator loop is complete and merged (#159 import → #169 CMS
group sync → #170 owner-stamp recovery → #171 scheduled sync), but every
manual action is a curl: multipart CSV import, sync dry-run/apply, clear a
wrong owner stamp. The frontend (React 19 + Vite + Tailwind 4, no router —
an `AppShell` `ViewKey` switch over seven views) has a Registry view for
channels but no surface at all for grouping governance.

Decisions settled with Mahmoud in the visual round (browser mockups,
2026-08-07):

- **Placement: split by domain.** CSV import lives in Registry (it edits
  the channel roster); a NEW **Groups** view owns sync + ownership.
  (Revised from an earlier all-in-one-Groups pick — his push-back.)
- **Flows are inline steppers**, not modals/drawers — dry-run previews are
  wide tables and get the full content width.
- **Groups view is table-first** — the group table is the steady state;
  sync is a header action that runs the same stepper pattern as import.
- **Content owner is a picker fed by stored youtube-analytics
  credentials** (the same "credential list is the registry" rule the
  scheduler uses) — never free text.

## Goal

An operator runs the whole loop without curls: import a roster CSV with a
previewed, all-or-nothing apply; see every group with its CMS key and
owner stamp; sync a content owner with a previewed diff; recover from a
wrong stamp (clear → re-sync) — all permission-gated and reason-audited
exactly as the API requires.

## Non-goals

- **No scheduled-sync UI.** No API exposes scheduler state; inventing one
  is backend scope this arc did not sign up for. The schedule remains
  ops-configured and log/audit-observable.
- **No group creation/rename/membership editing UI** for synced groups —
  the lockdown 409s manual edits by design; the UI never offers what the
  API refuses. Manual (non-synced) group curation UI is also out: the
  operator loop being served is CMS-driven.
- **No new backend endpoints.** One additive response field (below) is the
  only backend touch in the arc.
- **No router migration, no design-system changes** — new view follows the
  existing `ViewKey`/softdark patterns.

## Delivery: two PRs

**PR-A — Groups view** (greenfield view + the one backend field + the
shared primitives). **PR-B — Import stepper in Registry** (reuses PR-A's
primitives inside the existing 970-line view). Greenfield first de-risks
the pattern before touching Registry; sync/ownership currently has zero UI
while import has a curl runbook. Each PR runs the standard loop (this spec
→ plan → subagent execution → validation → merge).

## PR-A — Groups view

### Backend touches (two, both additive — verified 2026-08-07)

1. `ChannelGroupEntry.to_api()` (org/channel_groups.py:107) gains
   `"content_owner_id": self.content_owner_id` — additive on every group
   response. The #170 clear route already uses a declared
   `ClearContentOwnerResponse` model (NOT a manual dict add — earlier
   drafts of this spec said otherwise), so it is unaffected except for its
   docstring, whose "to_api omits content_owner_id" sentence becomes
   false and gets updated. Tests: `GET /groups` list (bare `to_api()`
   array, api/groups.py:185) carries the field; any exact key-set
   assertions gain the key.
2. `GET /session/me` capabilities gain `can_manage_groups` (wire:
   `canManageGroups` via the model's `to_camel` alias), derived
   `_can(Permission.MANAGE_GROUPS)` — the existing capability set
   (api/session.py:64-78) has `can_manage_registry` (from
   MANAGE_ORG_MAPPING) and nothing groups-shaped; gating the UI on the
   registry capability would show controls that silently 403, the exact
   trap the session module's own comments warn about.

### View

- Nav: **Groups** item between Registry and Connectors in the `ViewKey`
  union + `AppShell` nav list + `VIEW_COPY`.
- **Table (steady state):** name · CMS group id (or "manual" when NULL) ·
  owner stamp (short CO id, visually distinct when NULL = adoptable) ·
  member count · active. Data: new `useGroups` hook → `GET /groups`.
- **Row actions** (capability-gated):
  - **Clear stamp** → confirm dialog with required reason →
    `DELETE /groups/{id}/content-owner?reason=…` (#170 route). Shown only
    when a stamp exists.
  - **Archive / Restore** → the existing active-only `PATCH /groups/{id}`
    (the one manual edit the synced-group lockdown permits), required
    reason.
- **Header:** content-owner picker (from `useConnectors` filtered to
  `youtube-analytics`, listing `account_id`s; disabled with explanation
  when no credential exists) + **Sync (dry-run)** button.
- **Sync stepper** (replaces the table while active, same pattern PR-B
  reuses): ① reason input → dry-run `POST /channels/groups/sync
  {content_owner_id, dry_run: true, reason}` → ② full-width diff table —
  one row per group, outcome chip (CREATE / RENAME / MEMBERS_CHANGED /
  DEACTIVATE / REACTIVATE / UNCHANGED / CONFLICT), name/member/active
  deltas, CONFLICT rows in warning color with the owning CO and the
  remedy named (clear the stamp, or sync under the right owner); apply
  button disabled while any CONFLICT exists (the API would 409) → ③ apply
  (`dry_run: false`) → result counts + unknown-channel list ("in CMS, not
  in registry — import them first", read-only) + reason echo → back to
  table, refetch.
- **Empty states:** no groups → "import a roster, then sync"; no
  credentials → picker disabled + pointer to Connectors view.

### Permissions

Mirror the `canManageRegistry` capability pattern: sync, clear, archive
require the new `canManageGroups` session capability (backend touch 2);
the view itself is readable to any signed-in role that can see Registry.
Buttons hidden (not disabled) without capability, matching existing
views.

## PR-B — Import stepper in Registry

- Registry header gains **Import CSV** (MANAGE_CHANNELS + MANAGE_GROUPS
  capabilities; hidden otherwise). Clicking swaps the Registry content
  area for the stepper; Cancel at any step restores the table untouched.
- **① Upload + reason:** file input (.csv), required reason, CSV contract
  stated inline — required `youtube_channel_id, channel_name`; optional
  `Group_ID, view_revenue`; unknown headers rejected by the API.
- **② Dry-run preview:** `POST /channels/import` (multipart via the
  existing client's FormData pass-through — zero client changes) with
  `dry_run=true`. Full-width per-row table: outcome (CREATE / UPDATE /
  UNCHANGED), group effect ("joins / creates …"), revenue flag, error rows
  with the backend's canned messages verbatim (including the Path-A
  "group exists without a content owner; run POST /channels/groups/sync…"
  refusal). **Apply disabled while any row errors** — the API is
  all-or-nothing and would 422; the UI states that rather than letting the
  attempt fail.
- **③ Applied:** counts + reason echo; "Back to Registry" refetches.
- Hook: `useChannelImport` (dry-run/apply, FormData body).

## Engineering shape (both PRs)

- **Hooks** in `lib/api/` per house pattern (one file per domain, typed
  against `types.ts` additions matching the real response models in
  Docs/12): `useGroups`, `useGroupSync`, `useClearOwnerStamp` (PR-A);
  `useChannelImport` (PR-B). Reuse `useConnectors` for the picker.
- **Shared primitives** in `components/srcc/` built in PR-A, reused in
  PR-B: `ActionStepper` (step header + content slot + back/cancel
  wiring) and `OutcomeTable` (chip-outcome rows with delta/error cells).
  Both dumb/presentational; flows own state.
- **State:** local component state via the existing `useAsync` patterns —
  no new state library.
- **Testing** per house style (vitest + testing-library, fetch mocked at
  the client boundary): hook tests in `lib/api/__tests__` (request shape
  incl. multipart/reason/dry_run; error propagation), view tests in
  `views/__tests__` (table render, capability gating, stepper flow
  happy/error paths, conflict-blocks-apply, empty states). Type additions
  compile-checked by the existing `tsc` gate.

## Error and edge taxonomy (UI mapping)

| API response | UI |
| --- | --- |
| 409 conflict (sync apply race, clear-vs-adopt, import mid-flight) | inline banner, backend detail verbatim + remedy pointer |
| 503 credential (missing/inactive/refresh) | banner linking to Connectors view |
| 502 fetch | banner "YouTube fetch failed — check connector access", retry stays available |
| 422 validation (row errors, blank/NUL reason) | blocked client-side where knowable (blank/NUL reason, no file); row errors rendered per-row |
| 403 | action hidden by capability gating; if it still occurs, the generic ApiError banner |
| Network / malformed JSON | existing `ApiError` surface, banner with status |

## Trackers (per-PR rule)

Each PR updates `Docs/01_IMPLEMENTATION_PLAN.md` (frontend track note) and
`Docs/15_DELIVERY_BACKLOG.md` (new dated entry). `Docs/12` is untouched —
no API contract changes beyond the additive field, which PR-A notes in the
groups section.

## Rollback

Frontend-only besides the additive field: revert the branch. The to_api
field is additive (no client depends on its absence); reverting PR-A after
PR-B would orphan PR-B's primitives — revert in reverse order if both are
in.
