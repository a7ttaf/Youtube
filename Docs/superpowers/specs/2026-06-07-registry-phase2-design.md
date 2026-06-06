# Registry Phase 2 — Org-Unit Names + Live Map/Assign/Review Actions

**Status:** Approved (Mahmoud, 2026-06-07, one PR).
**Branch:** `feat/registry-phase2` off main `d19b1c8` (post-#76).
**Predecessor:** Phase 1 (PR #73) wired `GET /channels`; this phase closes the Registry gap
table except bulk-import and the "Scoped changes" tile (both definition-blocked).
**Base design reference:** `2026-06-05-registry-view-design.md` (Sections 5b, 6, 9 Phase 2).

## Approach decision

Chosen: **new read-only `GET /org-units` + client-side enrichment + wire all three row
actions in one PR.** Rejected: a server-side enriched `GET /channels/registry-view`
(duplicates a live endpoint; display concerns server-side) and embedding names in
`GET /channels` (the Map modal needs the company LIST to pick from — an org-units listing
is required regardless). Deciding fact: `primary_company_id` IS the org-unit UUID
(`_parse_optional_uuid` in `org/channel_registry.py`; FK `youtube_channels.primary_org_unit_id
→ org_units.id`), so the name join is a direct id lookup.

## 1. Backend — `GET /org-units` (only backend change; no migration)

- New `backend/ums_smart_revenue/org/org_units_read.py`: frozen `OrgUnitEntry` dataclass
  (`id, parent_id, type, name, active` + `to_api()`) and `SqlAlchemyOrgUnitReader(session)`
  with `list_active_units()` — tenant-scoped (`require_current_tenant().id`), active-only,
  ordered by `(type, name)` for deterministic output. Mirrors the
  `load_org_access_index_from_session` query shape (`org/access_index.py:64`).
- New `backend/ums_smart_revenue/api/org_units.py`: `APIRouter(prefix="/org-units")`,
  one GET route. Gate: `VIEW_ANALYTICS` via the same fail-closed pattern as
  `_require_analytics_view_permission` (`api/channels.py:189`) — disabled principal or no
  granted scope → 403. Org-unit names are structure metadata with the same audience as the
  channel list; scoped analytics viewers get the full active list (names are needed to
  label channels that map outside the caller's scope; IDs were already visible via
  `GET /channels`).
- Registered in `app.py` beside the channels router. Repository injected per request
  (SQL-only, like `current_channel_account_link_repository`).
- **Active-only is deliberate:** authorization (`OrgAccessIndex`) only sees active units; a
  channel mapped to a deactivated unit shows its raw ID in the UI (honest fallback, never
  an invented name).

Response shape per item:
```json
{"id": "<uuid>", "parent_id": "<uuid|null>", "type": "SECTOR", "name": "TV", "active": true}
```

## 2. Frontend — data layer

- `OrgUnit` type in `lib/api/types.ts` matching the shape above.
- `useOrgUnits()` hook in `lib/api/useOrgUnits.ts` — exact `useChannels` pattern
  (`useApiClient` + `useCallback` + `useAsync`), GET `/org-units`.
- `useChannelMappingAction()` in `lib/api/useChannelMapping.ts` — imperative PATCH
  `/channels/{id}/mapping` with `{primary_company_id, reason}` (the `useMonthCloseActions`
  pattern: `useCallback` + `useMemo`).
- `useProposeAccountLinkAction()` in `lib/api/useChannelAccountLinks.ts` — imperative POST
  `/revenue/channel-account-links` with `{adsense_account_id, content_owner_id,
  effective_month_start, effective_month_end: null, provenance_kind: "OPERATOR_ASSERTED",
  provenance_payload: {}, reason}`.

## 3. Name resolution (Company / Sector columns)

In `RegistryView`: call `useOrgUnits()` once at the root (beside `useChannels`), build
`Map<id, OrgUnit>`. Company cell: `units.get(primary_company_id)?.name ?? primary_company_id
?? "—"`. Sector cell: `units.get(units.get(primary_company_id)?.parent_id)?.name ?? "—"`.
Org-units fetch failure degrades to raw IDs (table still renders; no blocking).

## 4. Map action — `PATCH /channels/{id}/mapping`

- Row "Map" button (already derived: channel without `primary_company_id`) opens the
  existing `MappingChangeRequestPanel`, now real: channel pre-filled (read-only) from the
  clicked row, company `<select>` of active COMPANY units (label = name, value = id),
  required reason input.
- The mock "Effective month" select and "Save Draft" button are REMOVED — the backend has
  no draft or effective-month concept on this route. One "Submit mapping change" button.
- Mutation flow = CloseView's `runAction` pattern verbatim: trim+require reason,
  synchronous in-flight ref latch (one POST per click burst → one audit event), busy
  state, on success clear form + `reload()` channels, typed inline errors via
  `ApiError.status` (403 incl. the unmapped-channel auth dead-zone — company-scoped
  stewards need global `MANAGE_ORG_MAPPING` to map an unmapped channel; 404; 409; 422).
- Enabled by `canManageRegistry`; the backend dual `MANAGE_ORG_MAPPING` check (channel-side
  + target-company-side, `api/channels.py:368`) stays the authority.

## 5. Assign action — `POST /revenue/channel-account-links` (propose)

- For "Evidence due" rows (OUTSIDE_CMS, no content owner): a new `AccountLinkProposalPanel`
  in the side column — fields `adsense_account_id`, `content_owner_id`,
  `effective_month_start` (`<input type="month">`, default current month), `reason`.
  `provenance_kind` is fixed `OPERATOR_ASSERTED`; payload `{}` (not user-editable).
- Honest copy: this PROPOSES an UNVERIFIED account↔owner link; verification is the
  existing dual-gated admin flow (NOT in this PR). Success state shows
  "Link proposed (UNVERIFIED)" + reloads channels.
- Gate: `canManageRegistry` in UI; backend `MANAGE_ORG_MAPPING@global` is the authority.

## 6. Review action — navigation only (no backend)

- "Review" (default action for healthy rows) navigates to the Trace view preselected on
  the channel: `RegistryView` gains optional `onOpenTrace?: (channelId: string) => void`;
  AppShell supplies it (`setView("trace")` + stores `traceChannelId`); `TraceView` gains
  optional `presetChannelId?: string` used as the initial `selectedChannelId` (existing
  resolution logic keeps working when the preset is absent from the month's channel list).

## 7. Row-action derivation (unchanged rules, now live)

`Map` ← no `primary_company_id`; `Assign` ← OUTSIDE_CMS and no `content_owner_id`;
`Review` ← otherwise. Buttons disabled (with current styling) when `!canManageRegistry`
except Review, which is read-only navigation and stays enabled for all viewers.

## Non-goals (explicit)

- Bulk channel inventory import (format undefined — needs its own definition round).
- "Scoped changes" summary tile (no backend concept).
- Verify/reject UI for proposed links (existing admin API flow remains the path).
- Month-lock enforcement on `PATCH /channels/{id}/mapping` — pre-existing backend gap
  (route ignores finance-month locks; design doc §2). Recorded as a named follow-up; NOT
  silently fixed here.
- Scoped-steward session capabilities (canManageRegistry stays global-scope-only; scoped
  403s surface inline).

## Error handling

Backend: existing typed translations untouched. New org-units route: 403 fail-closed only
(read-only, no writes, no sensitive values in errors). Frontend: every mutation surfaces
`ApiError` status + detail inline next to the form; never silently swallowed; failures
leave form state intact for retry.

## Testing

- Backend `tests/api/test_org_units_api.py` (gateway-header style like
  `test_channels_api.py`): 200 list shape + ordering, active-only filtering, 403 for
  no-permission/disabled, scoped analytics viewer 200, tenant isolation (second tenant's
  units invisible).
- Frontend: `useOrgUnits` hook tests (mount fetch, error, reload); RegistryView tests —
  company/sector names render, raw-id fallback when org-units fails or unit missing, Map
  flow (happy 200 + body assertion, 403 copy, 409 copy, same-tick double-click → exactly
  one PATCH), Assign flow (happy 201 + body assertion incl. fixed provenance_kind, 422),
  Review invokes `onOpenTrace` with the channel id, forms disabled when
  `!canManageRegistry`, single fetch per mount for both GETs.
- Gates: `python -m ruff check backend tests scripts`; `python -m pytest -q` (+ PG tier
  via `UMS_TEST_DATABASE_URL`); `npx vitest run`; `npx tsc --noEmit`; `git diff --check`;
  ≤100-char lines in touched Python files.

## Execution

Wave 1 (parallel, disjoint files): backend org-units unit; frontend data-layer unit.
Wave 2 (single writer — all files shared): RegistryView/AppShell/TraceView wiring + tests.
Wave 3: docs (01 + 15), full gates, adversarial review fan-out, fixes, final gate.
