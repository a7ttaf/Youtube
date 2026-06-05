# Channel Registry View — Design Reference

**Purpose:** Document the current mock semantics, existing backend assets, undefined concepts, and open questions
so the Registry page can be specced and built. This is a design exploration document, not a build plan.

**Current status:** Registry is the only remaining mock-only page. All other six screens are wired to real APIs
(PR #69, #70, #71). This document captures what is known, what needs deciding, and what needs building.

---

## 1. What the Mock Shows Today

The mock (`frontend/src/lib/mock/data.ts` → `REGISTRY_ROWS`) renders four rows. Each row has:

| Mock field | Example value | Type |
|---|---|---|
| `avatar` | `"DR"` | Initials computed from channel name |
| `name` | `"UMS Drama"` | Channel display name |
| `code` | `"UC-DRAMA-01"` | YouTube channel ID |
| `company` | `"United Studios"` | Company name (display string, not ID) |
| `sector` | `"TV"` | Sector name (display string, not ID) |
| `cms` | `{text: "Inside CMS", tone: "green"}` | CMS membership indicator |
| `source` | `"YouTube Reporting API"` | Revenue source label |
| `node` | `"channel:ums-drama"` | Trace-key — **semantics undefined** |
| `state` | `{text: "Approved", tone: "green"}` | Approval/mapping state — **semantics undefined** |
| `action` | `"Review"` | Action label — **semantics undefined** |

**Summary tiles** (`REGISTRY_SUMMARY`):
- Active channels: 318
- Outside CMS: 70 (allocation requires explicit source mapping)
- Unmapped revenue: $42.8K (held from export until mapped) — finance-gated
- Scoped changes: 9 (company and sector approvals required)

**Side panel** (`REGISTRY_CONTROLS`):
- Company scope enforced: managers cannot open unrelated company mappings
- Finance month impact: changes after lock require Finance Admin approval
- Registry lineage update: SQL mapping changes refresh trace-ready issue relationships

---

## 2. Existing Backend Assets

### `GET /channels` (already live)

Returns a flat list of `ChannelRegistryEntry.to_api()` objects. Response shape per item:

```json
{
  "youtube_channel_id": "UC-DRAMA-01",
  "channel_name": "UMS Drama",
  "primary_company_id": "united-studios",
  "cms_status": "INSIDE_CMS",
  "content_owner_id": "ams/content-owner-1",
  "revenue_required": true,
  "revenue_source_status": "YOUTUBE_REPORTING_API",
  "active": true
}
```

Permission gate: `VIEW_REVENUE@global` or scoped (`VIEW_REVENUE@channel`, `@company`, `@sector`).
Returns all channels if global, or only those within the caller's scope.

### `GET /channels/outside-cms`

Returns channels where `cms_status != INSIDE_CMS`. Includes an `issues` flag per channel
(`missing_official_revenue: bool`). Used by the outside-CMS monitor.

### `GET /channels/issues`

Returns per-channel issue list (scoped same as `GET /channels`). Each issue has `issue_type`, `severity`, `title`, `description`.

### `PATCH /channels/{youtube_channel_id}/mapping`

Changes `primary_company_id` for a channel. Requires `MANAGE_ORG_MAPPING@channel` + `MANAGE_ORG_MAPPING@company(target)`.
Audited with reason. Note: the endpoint does not currently enforce month-lock; a Finance Admin override path is not yet implemented.

### `POST /channels`

Creates a new channel registration. Requires `MANAGE_CHANNELS@global`.
Fields: `youtube_channel_id`, `channel_name`, `primary_company_id`.

### Org hierarchy (`OrgAccessIndex`, `OrgUnitORM`)

`OrgAccessIndex` has:
- `channel_company: dict[channel_id, company_id]`
- `channel_sector: dict[channel_id, sector_id]`
- `org_units: dict[unit_id, OrgUnitEntry]` — includes `name`, `type` ("sector", "company", "group")

**Implication:** company and sector names can be resolved from `primary_company_id` via the org index. No new DB join is needed in the route if the org index is already loaded — but the current `GET /channels` response returns IDs, not names. A "registry view" endpoint would need to enrich the channel list with names.

### Permissions relevant to Registry

| Permission | What it gates |
|---|---|
| `MANAGE_CHANNELS` | Create channels, manage the registry |
| `MANAGE_ORG_MAPPING` | Reassign channel → company mappings |
| `MANAGE_GROUPS` | Manage channel groups |
| `VIEW_REVENUE` | See channel list |

`canManageRegistry` in the SPA = `MANAGE_CHANNELS OR MANAGE_ORG_MAPPING OR MANAGE_GROUPS`.

---

## 3. What Maps Cleanly to the Backend

| Mock field | Mapped to | Notes |
|---|---|---|
| `avatar` | Computed client-side from `channel_name` | First letter of each word, up to 2 chars |
| `name` | `channel_name` | Direct |
| `code` | `youtube_channel_id` | Direct |
| `company` | `org_units[channel_company[id]].name` | Name lookup via org index |
| `sector` | `org_units[channel_sector[id]].name` | Name lookup via org index |
| `cms` | Derived from `cms_status` | `INSIDE_CMS` → green, `OUTSIDE_CMS` → amber, `UNMAPPED` / null → red |
| `source` | Derived from `revenue_source_status` | Label mapping (see Section 4) |

The `node` / trace-key, `state`, and `action` fields have **no backend equivalent today** — see Section 5.

---

## 4. Known Mappings: `revenue_source_status` → Source Label

Based on the backend enum values used in `channel_issues.py` and `channels.py`:

| `revenue_source_status` | Mock label equivalent |
|---|---|
| `YOUTUBE_REPORTING_API` | "YouTube Reporting API" |
| `YOUTUBE_ANALYTICS_API` | "YouTube Analytics API" |
| `OFFICIAL_MANUAL_IMPORT` | "Uploaded owner statement" |
| `MISSING_REVENUE_SOURCE` | "Not linked" |
| `PERFORMANCE_ONLY` | "Performance only (no revenue)" |

---

## 5. Undefined Semantics — Needs Your Decision

These three concepts exist in the mock but have no backend equivalent and no defined semantics.

### 5a. `state` — Channel approval / mapping state

The mock shows: `"Approved"` (green), `"Evidence due"` (amber), `"Export block"` (red).

**Two design approaches:**

**Option A — Derived from existing fields (no new DB column)**
Define the state as a function of existing fields:
- `"Export block"` → `revenue_required = true AND revenue_source_status = MISSING_REVENUE_SOURCE`
- `"Evidence due"` → `cms_status = OUTSIDE_CMS AND content_owner_id IS NULL` (no verified account link)
- `"Approved"` → everything else (has a revenue source, CMS status resolved)

This is purely computed — no migration, no new table. The rules can live in the frontend or in a new API field.

**Option B — Explicit `approval_state` column on `YouTubeChannelORM`**
Add a new `approval_state` field to the ORM (e.g., `"approved" | "evidence_due" | "export_blocked" | "pending_review"`).
An operator sets it explicitly via a new `PATCH /channels/{id}/approval-state` route.

**Trade-off:** Option A is zero-migration, automatically consistent with the underlying facts. Option B allows
operator-controlled workflow states (useful if "Approved" means "a human signed off", not just "fields are present").

**Question for you:** Is "Approved" a human approval step, or does it just mean "channel has all required fields filled in"?

### 5b. `action` — Row-level action button

The mock shows: `"Map"`, `"Assign"`, `"Review"`. These look like contextual actions tied to the row's `state`.

Proposed derivation:
- `"Map"` → channel is `UNMAPPED` or has no `primary_company_id` → opens a company-assignment modal (calls `PATCH /channels/{id}/mapping`)
- `"Assign"` → channel is `OUTSIDE_CMS` without `content_owner_id` → opens an account-assignment modal (no backend route yet)
- `"Review"` → channel is in an `"Approved"` or normal state → navigates to channel detail / trace view

**Question for you:** What does "Assign" open? Does it assign a content owner (AdSense account)? Is there a backend route for this, or is it the same as the channel↔account map from Spec 2b (`POST /revenue/channels/{id}/account-allocations/propose`)?

### 5c. `node` — Trace-key

The mock shows: `"channel:ums-drama"`, `"channel:sports-extra"`, `"channel:news-live"`, `"pending"`.

This looks like a Neo4j node reference label. However, Neo4j is a read-only projection and
cannot be the source of truth. The trace-key's value in the UI is:
- As a display label that lets operators identify the channel in trace/explain views
- A link target — clicking it would navigate to `view=trace` filtered to this channel

**Question for you:** Is the trace-key just `"channel:{youtube_channel_id}"` formatted?
Or does it reference something specific in the Neo4j schema (node type + internal ID)?
`"pending"` for unmapped channels implies the node doesn't exist in the graph yet — is that accurate?

---

## 6. What a Real "Registry View" Endpoint Needs

If we build a single enriched endpoint for the Registry table (e.g., `GET /channels/registry-view`), it needs to:

1. **Return channel list** — same as `GET /channels` but with org-level enrichment
2. **Join company + sector names** — resolve `primary_company_id` → `{company_name, sector_name}` via `OrgAccessIndex`
3. **Derive `cms` badge** — from `cms_status` enum
4. **Derive `source` label** — from `revenue_source_status` enum
5. **Derive `state` + `action`** — depends on Section 5a decision
6. **Compute `avatar`** — can be done client-side from `channel_name` (no API field needed)
7. **Return `node`/trace-key** — depends on Section 5c decision; simplest: `"channel:{youtube_channel_id}"`

**Alternative:** No new endpoint. The frontend calls `GET /channels` (already live) and enriches
client-side using the existing `canManageRegistry` capabilities. Company/sector names require a
separate `GET /org-units` call (or they're already available in the SPA from session or a prior fetch).

---

## 7. Summary Tiles — What's Live vs. Static

| Tile | Source | Status |
|---|---|---|
| Active channels (318) | `GET /channels` count | Computable from existing API |
| Outside CMS (70) | `GET /channels/outside-cms` count | Computable from existing API |
| Unmapped revenue ($42.8K) | `GET /revenue/months/{month}/net-revenue` | Requires finance-month context; currently hardcoded |
| Scoped changes (9) | Unknown — no backend concept | **Undefined; skip until defined** |

---

## 8. Open Questions Summary

Before writing an implementation plan, these need your answers:

1. **`state` derivation rule** (Section 5a): Is "Approved" human-approved, or field-complete?
   → This determines whether we need a migration (Option B) or can derive client-side (Option A).

2. **`action` for "Assign"** (Section 5b): Does it assign a content owner (AdSense account)?
   Is the target the `channel_account_links` API from Spec 2b, or a new route?

3. **`node` / trace-key** (Section 5c): Is it just `"channel:{youtube_channel_id}"`?
   What should "pending" channels show?

4. **Scope of the build**: Is the goal to wire the existing table to `GET /channels` + enrich
   client-side, or build a new enriched `GET /channels/registry-view` endpoint?

5. **Scoped changes tile**: What does "9 scoped changes" mean? Is it a count of pending
   `ChannelAccountLinkORM` proposals? Something else?

6. **Write paths**: The mock has "Bulk Import" and "Mapping Change" buttons (currently disabled for
   non-registry roles). What do they do? Bulk import of channel IDs via CSV? These would need new routes.

---

## 9. Recommendation

If the goal is the minimum useful Registry page:

**Phase 1 (no migration, minimal new code):**
- Wire existing `GET /channels` to the table (already live)
- Enrich client-side: compute `avatar`, `cms` badge, `source` label from `revenue_source_status`
- Company/sector names: add `GET /org-units` or embed in the channels response
- Derive `state` and `action` from existing fields (Option A from Section 5a)
- Set `node` = `"channel:{youtube_channel_id}"` for now
- Disable "Assign", "Map" actions until routes exist

**Phase 2 (write paths):**
- Wire company reassignment (`PATCH /channels/{id}/mapping`) to the "Map" modal
- Wire account assignment (define semantics per Section 5b question)

This gets the Registry off mock status without a migration, and makes the data honest.
