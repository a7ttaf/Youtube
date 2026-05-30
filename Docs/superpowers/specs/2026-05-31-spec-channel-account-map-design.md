# Channel↔Account Map — Design Spec

**Phase:** Phase 4 reconciliation — **Spec 2a** (the canonical channel/account map
substrate). The allocation engine that consumes it is **Spec 2b** (later, separate
spec). This spec is the carved-out prerequisite of the original "Spec 2 = allocation
rules" note in `2026-05-29-spec-deduction-components-design.md` §12.

**Status:** Designed 2026-05-31. Off `main` (`4a8c4b5`, which already has the MERGED
Phase 4 PR-A `deduction_components` substrate/ingestion and PR-B net-revenue
consumption + read endpoint). Branch `spec/channel-account-map`.

**Goal:** Persist a canonical, provenance-tracked, operator-verified map from AdSense
publisher accounts to YouTube channels — via content owners — so a later allocation
engine can distribute account-scoped AdSense evidence to channels using **only verified
mappings**, reproducibly per month.

**Architecture:** Two persisted layers. Layer 1 (`adsense_content_owner_links`) is the
genuinely-uncertain `adsense_account_id ↔ content_owner_id` link, established by an
audited operator decision (propose → verify/reject). Layer 2
(`content_owner_channel_links`) is the high-confidence `content_owner_id ↔
youtube_channel_id` link, idempotently derived from source rows. A single read contract,
`list_verified_adsense_account_channels(...)`, joins the two for a month. PostgreSQL is
the source of truth; no graph projection impact.

---

## 1. Context and problem

AdSense payment/deduction evidence is **account-scoped**: `deduction_components` rows
with `scope_kind == "ACCOUNT"` carry the AdSense publisher account in `scope_id` and have
no `youtube_channel_id`. To attribute that money to channels, allocation (Spec 2b) must
answer: *which channels belong to this AdSense account in month M?*

The data needed to answer splits into two links with very different confidence:

- **`content_owner_id ↔ youtube_channel_id` (high confidence).** YouTube source rows
  (`GoogleRevenueSourceRowORM` in `backend/ums_smart_revenue/db/source_models.py`) carry
  `content_owner_id` and `youtube_channel_id` together on the same row when both are
  present. This co-occurrence is reliable evidence and can be derived mechanically.
- **`adsense_account_id ↔ content_owner_id` (uncertain).** The AdSense publisher account
  namespace is **not** the YouTube CMS content-owner namespace. `Docs/01` (L422–423) and
  `Docs/18` deliberately left this link unbuilt. It must be **explicitly asserted and
  verified by an operator** — never inferred, and never assumed equal.

Critically, the `source_account_id` column on `GoogleRevenueSourceRowORM` is a YouTube
CMS-side account identifier; it **must not** be used to infer the AdSense
account↔owner link. That bridge is exactly the uncertain decision this spec puts behind
human verification.

## 2. Scope

In scope:

1. Two persisted tables (Layer 1 verified link, Layer 2 derived link) with provenance,
   verification state, and effective month ranges.
2. An idempotent derivation that upserts Layer 2 rows from source-row co-occurrence.
3. An audited, permission-gated API to **propose / verify / reject** Layer 1 links and to
   **list** links by status/account/owner/month.
4. The repository read contract `list_verified_adsense_account_channels(tenant_id, month,
   adsense_account_id) -> list[str]` for Spec 2b.
5. A fail-closed **effective-range overlap invariant** guaranteeing at most one verified
   owner per account per month (reproducible historical allocation).
6. An Alembic migration and full test coverage.

## 3. Non-goals (explicit)

- **No allocation math** and **no committed `/recalculate` write** — that is Spec 2b. This
  spec only produces the verified map and the read contract.
- **Never assume** `adsense_account_id == content_owner_id`, and **never** derive the
  account↔owner link from `source_account_id` or any other automatic source.
- **No inline derivation at allocation time.** Allocation reads persisted rows only.
- **No `provenance_payload` exposure** in API responses in this PR (mirrors the
  `deduction_components` raw-payload omission). A separate sensitive/debug endpoint can be
  designed later if operators need raw evidence.
- No FX / multi-currency. No Neo4j authority. No UI finance calculation.

## 4. Data model

ORM lives in `backend/ums_smart_revenue/db/finance_models.py` on `FinanceBase` (same base
as `DeductionComponentORM`). Both tables are tenant-scoped with a `tenant_id` FK, matching
the deduction-components hardening.

### 4.1 `adsense_content_owner_links` (Layer 1 — verified link)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid, PK | |
| `tenant_id` | uuid, FK → tenants, NOT NULL | |
| `adsense_account_id` | Text, NOT NULL | publisher account; matches `deduction_components.scope_id` (ACCOUNT) |
| `content_owner_id` | Text, NOT NULL | YouTube CMS content owner |
| `verification_status` | Text, NOT NULL | CHECK ∈ {`UNVERIFIED`, `VERIFIED`, `REJECTED`, `CONFLICT`}; default `UNVERIFIED` |
| `provenance_kind` | Text, NOT NULL | e.g. `OPERATOR_ASSERTED`, `CONNECTOR_HINT` |
| `provenance_payload` | JSON, nullable | raw evidence; **never serialized to API** |
| `verified_by` | uuid, nullable | principal id; set on verify/reject |
| `verified_at` | timestamptz, nullable | set on verify/reject |
| `verification_reason` | Text, nullable | required on verify/reject (audit reason) |
| `effective_month_start` | Text `YYYY-MM`, NOT NULL | inclusive |
| `effective_month_end` | Text `YYYY-MM`, nullable | inclusive; NULL = open-ended |
| `created_at`, `updated_at` | timestamptz, NOT NULL | |

Constraints/indexes:

- Unique `(tenant_id, adsense_account_id, content_owner_id, effective_month_start)` —
  prevents exact-start duplicates. **This is necessary but not sufficient** (see §6).
- Index `(tenant_id, adsense_account_id, verification_status)` for the list endpoint and
  the read contract.
- CHECK `effective_month_end IS NULL OR effective_month_end >= effective_month_start`.
- CHECK `effective_month_start ~ '^\d{4}-\d{2}$'` (and same for end when present).

### 4.2 `content_owner_channel_links` (Layer 2 — derived link)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid, PK | |
| `tenant_id` | uuid, FK → tenants, NOT NULL | |
| `content_owner_id` | Text, NOT NULL | |
| `youtube_channel_id` | Text, NOT NULL | |
| `provenance_kind` | Text, NOT NULL | CHECK ∈ {`SOURCE_ROW`, `CHANNEL_REGISTRY`, `MANUAL`} |
| `provenance_source_id` | Text, nullable | source-row reference where available |
| `active` | bool, NOT NULL, default true | |
| `effective_month_start` | Text `YYYY-MM`, NOT NULL | inclusive |
| `effective_month_end` | Text `YYYY-MM`, nullable | inclusive; NULL = open-ended |
| `created_at`, `updated_at` | timestamptz, NOT NULL | |

Constraints/indexes:

- Unique `(tenant_id, content_owner_id, youtube_channel_id, effective_month_start)` —
  makes the derivation idempotent (upsert key).
- Index `(tenant_id, content_owner_id, effective_month_start)` for the join.

An owner legitimately maps to **many** channels, so there is no "one channel per owner"
invariant on Layer 2 — only no duplicate `(owner, channel, start)` rows.

### 4.3 Effective-range semantics

A link is **valid for month M** iff
`effective_month_start <= M <= coalesce(effective_month_end, M)`, using lexicographic
comparison of zero-padded `YYYY-MM` strings (which orders correctly). Effective ranges
exist so that re-running allocation for a historical month M reads exactly the links that
were valid for M, independent of later edits — the reproducibility requirement.

## 5. Provenance and verification

### 5.1 Layer 1 state machine

```
                 propose
        (operator)  │
                    ▼
                UNVERIFIED ──verify──▶ VERIFIED
                    │  ▲                  │
                  reject│                 │reject
                    ▼  │                  ▼
                 REJECTED              REJECTED
```

- `UNVERIFIED` — proposed candidate; **not** consumable by allocation.
- `VERIFIED` — operator-confirmed truth; **the only** state allocation consumes (subject
  to §6 invariant).
- `REJECTED` — operator-declined; not consumable. A previously `VERIFIED` link may be
  rejected (a money-affecting change — same gate as verify).
- `CONFLICT` — explicit marker that a candidate competes with another claim or that
  evidence is contradictory; **not** consumable. Distinct from the hard invariant in §6:
  the invariant guarantees two links are never simultaneously `VERIFIED` over an
  overlapping range; `CONFLICT` is the human-facing flag for unresolved competition among
  *non-verified* candidates.

`verified_by` / `verified_at` / `verification_reason` are written on every verify and
reject transition; `verification_reason` is required (carried into the audit event).

### 5.2 Layer 2 trust

Layer 2 rows are trusted by provenance — `SOURCE_ROW` co-occurrence is high-confidence —
and use the `active` + effective-range model rather than human verification. They are
created/maintained by the derivation in §7.

## 6. Effective-range overlap invariant (the reproducibility guarantee)

**Invariant.** For a fixed `(tenant_id, adsense_account_id)`, the set of `VERIFIED`
Layer-1 links must have **pairwise non-overlapping** effective month ranges. Two ranges
`[s1, e1]` and `[s2, e2]` (end NULL = open) overlap iff
`s1 <= coalesce(e2, "9999-12") AND s2 <= coalesce(e1, "9999-12")`.

This invariant is what makes `list_verified_adsense_account_channels(tenant, M, account)`
resolve to **exactly one owner (or none)** for any month M — i.e. reproducible historical
allocation. A unique constraint on `(tenant, account, owner, effective_month_start)` does
**not** enforce it: two verified links with different start months (e.g. `2026-01..2026-06`
and `2026-03..open`) pass the unique key yet overlap for `2026-03..06`.

**Enforcement (authoritative): repository-level, fail-closed.** On the
`UNVERIFIED → VERIFIED` transition (and on any effective-range edit of a `VERIFIED` link),
the repository, inside the same transaction, selects existing `VERIFIED` links for
`(tenant, account)` and checks the candidate's range against each. Any overlap raises a
typed `ChannelAccountLinkConflictError`, translated at the route boundary to **HTTP 409**.
The operator resolves by adjusting ranges or rejecting the competing link first.

**Why not a DB constraint alone.** A portable unique/CHECK constraint cannot express range
overlap. PostgreSQL could enforce it with `EXCLUDE USING gist`, but the unit-test suite
runs on SQLite (the migration round-trip runs on Postgres), and SQLite cannot honor an
`EXCLUDE` constraint — so a DB-only approach would diverge between test tiers. The
repository check is therefore authoritative and is exercised on SQLite. An optional
Postgres-only partial exclusion constraint MAY be added in the Alembic migration as
defense-in-depth, but only if it is applied in the migration (not in the shared SQLAlchemy
table metadata), so `FinanceBase.metadata.create_all` on SQLite is unaffected.

## 7. Layer 2 derivation from source rows

An idempotent derivation (`backend/ums_smart_revenue/finance/channel_account_links.py`,
repository method or a small ingestion entrypoint) upserts Layer 2 rows from
`GoogleRevenueSourceRowORM`:

- Select rows **where `content_owner_id IS NOT NULL AND youtube_channel_id IS NOT NULL`**.
  Rows missing either are skipped (no partial links).
- For each observed `(tenant_id, content_owner_id, youtube_channel_id, report_month)`,
  upsert one row with `provenance_kind = "SOURCE_ROW"`, `provenance_source_id` referencing
  the source row where available, `active = true`, and
  `effective_month_start = effective_month_end = report_month` (one row per observed
  month; range-coalescing is a possible later optimization, intentionally omitted now).
- The upsert key is the Layer-2 unique constraint, so a re-run produces identical rows
  (idempotent). No source-row `source_account_id` is read for any account↔owner inference.

Property: the map records only **observed** owner↔channel evidence per month. If a channel
has no YouTube source row in month M, no Layer-2 link exists for M and allocation will not
attribute that account's evidence to it for M. How allocation treats months with no
observed link is a Spec 2b policy decision, deliberately out of scope here — the map stays
honest to the evidence.

## 8. Repository contracts

In `backend/ums_smart_revenue/finance/channel_account_links.py`:

- `list_verified_adsense_account_channels(tenant_id, month, adsense_account_id) -> list[str]`
  — returns `youtube_channel_id`s where a Layer-1 link for `(tenant, account)` is
  `VERIFIED` and valid for `month`, joined to Layer-2 links for that owner that are
  `active` and valid for `month`. Returns `[]` when the account is unmapped/unverified for
  the month (Spec 2b turns `[]` into UNALLOCATED + a blocking issue). Pure read; no writes;
  no derivation.
- `propose_account_owner_link(...)`, `verify_account_owner_link(...)`,
  `reject_account_owner_link(...)` — state transitions; verify/reject enforce the §6
  invariant and stamp `verified_by/at/reason`.
- `list_account_owner_links(*, status=None, adsense_account_id=None, content_owner_id=None,
  month=None, limit, offset)` — filtered, paginated read for the GET endpoint; fail-closed
  on `limit < 1` / `offset < 0` (matches `list_month_components_page`).
- `upsert_owner_channel_links_from_source(...)` — the §7 derivation.
- Typed errors: `ChannelAccountLinkValidationError` (bad month/filter),
  `ChannelAccountLinkConflictError` (§6 overlap), `ChannelAccountLinkNotFoundError`.

## 9. API endpoints

New module `backend/ums_smart_revenue/api/channel_account_links.py` exposing `router`,
mounted via `app.include_router(channel_account_links_router)` in `app.py`. `revenue.py`
is **not** modified.

| Method / path | Purpose | Notes |
|---|---|---|
| `GET /revenue/channel-account-links` | list Layer-1 links (global-scoped management view) | filters: `status`, `adsense_account_id`, `content_owner_id`, `month` (result filter only — links valid for M); paginated; **excludes `provenance_payload`** (returns `provenance_kind` + safe summary); malformed `month` → 422 |
| `POST /revenue/channel-account-links` | propose `UNVERIFIED` link | body: account, owner, effective range, provenance, reason |
| `POST /revenue/channel-account-links/{id}/verify` | `→ VERIFIED` | reason required; §6 overlap → 409 |
| `POST /revenue/channel-account-links/{id}/reject` | `→ REJECTED` | reason required |

Pydantic models at the boundary; reads never trigger derivation or writes; all error paths
fail closed with safe messages (no SQL values, no `provenance_payload`).

## 10. Authorization and audit

Permissions verified against `backend/ums_smart_revenue/auth/permissions.py`; scope types
against `auth/user_permissions.py`; audit pattern against `auth/audit.py`.

| Action | Permission gate (all fail-closed) | Audit event(s) |
|---|---|---|
| `GET` list | `VIEW_REVENUE` **and** `VIEW_FINALIZED_PAYMENTS`, both on `global_scope()` — a cross-month management view; the AdSense account id is finalized-payment context and is always present, and `VIEW_FINALIZED_PAYMENTS` permits a GLOBAL grant (`_FINANCE_DATA_SCOPE_TYPES = {GLOBAL, FINANCE_MONTH}`). `month` is a result filter, not part of the auth scope | `REVENUE_VIEWED` + `PAYMENT_VIEWED` |
| `POST` propose | `MANAGE_ORG_MAPPING` on `global_scope()` — a proposal is a mapping assertion, not money-affecting until verified | `CHANNEL_ACCOUNT_LINK_PROPOSED` (reason-required) |
| `POST` verify / reject | **BOTH** `MANAGE_ORG_MAPPING` on `global_scope()` **and** `CHANGE_ALLOCATION_RULE` on `finance_month(effective_month_start)` (a GLOBAL `change_allocation_rule` grant also satisfies it) — combines ownership-mapping trust with allocation authority for the money-gating transition | `CHANNEL_ACCOUNT_LINK_VERIFIED` / `CHANNEL_ACCOUNT_LINK_REJECTED` (reason-required, sensitive) |

New `AuditEventType` members and `AUDIT_EVENT_DEFINITIONS` entries follow the existing
pattern: `reason_required=True`; `permission=CHANGE_ALLOCATION_RULE` for verify/reject
(mirroring `ALLOCATION_RULE_CHANGED`), `permission=MANAGE_ORG_MAPPING` for propose
(mirroring `CHANNEL_UPDATED`). Both gate permissions are `sensitive=True`.

**Deployment note (intentional dual control).** Verify/reject requires a principal holding
**both** `finance.change_allocation_rule` and `registry.manage_org_mapping`. In
`security_seed.sql` **no single non-owner role holds both** by design — `super_owner`
(every permission, break-glass) is the only role that does. Normal operation grants the
pair to a specific operator via an explicit **direct grant**; the seed is **not** changed
by this PR (adding `change_allocation_rule` to a registry/ops role would broaden it into
money-affecting authority). This dual-control friction is the intended posture for a
money-gating ownership decision.

## 11. Migration and blast radius

- **Alembic migration** under `backend/ums_smart_revenue/db/alembic/versions/` adds the two
  tables with the §4 constraints/indexes/CHECKs. Optional Postgres-only exclusion-constraint
  hardening (§6) is added in the migration only, never in shared SQLAlchemy metadata.
- Validated by the disposable-Postgres round-trip (container `ums-mig-pg-test`,
  `postgresql+psycopg://postgres:ums@localhost:55432/test_ums`).

Blast-radius answers:

- **Tables/ORM affected:** two **new** tables only; no existing model changes.
- **PostgreSQL still source of truth:** yes.
- **Could existing migrations/tests/seed/docs break:** no — additive schema; new audit
  enum members are additive; no seed change.
- **Neo4j projection:** *No graph projection impact detected* — no projection reads these
  tables; allocation/source-of-truth stay in PostgreSQL.
- **Authorization/audit more permissive:** no — strictly **adds** fail-closed gates and
  sensitive, reason-required audit events.
- **Finance results / month locks / overrides / payment matching changed:** none in this
  spec; this is map substrate. Allocation consumption is Spec 2b.
- **Migration reversibility:** standard additive up/down; pre-alpha data is disposable.

## 12. Testing

- **Models/migration:** Postgres round-trip; constraints, indexes, CHECKs; unique keys.
- **Verification state machine:** propose → verify/reject transitions; `verified_by/at/reason`
  stamped; reason required.
- **§6 overlap invariant:** verify that would overlap an existing `VERIFIED` link →
  `ChannelAccountLinkConflictError` (409); non-overlapping verify succeeds; open-ended
  ranges handled; re-verify after rejecting the competitor succeeds.
- **Layer 2 derivation:** only rows with both `content_owner_id` and `youtube_channel_id`
  produce links; `source_account_id` never used; provenance `SOURCE_ROW`; re-run
  idempotent (identical rows).
- **Read contract:** `list_verified_adsense_account_channels` returns verified-only,
  month-scoped channels; unmapped/unverified → `[]`; one owner per account per month holds.
- **API auth matrix:** each required permission missing → 403 (fail-closed); verify/reject
  require both permissions; super_owner can perform both.
- **API shape:** filters/pagination; malformed month → 422; **no `provenance_payload`** in
  any response; audit rows recorded (`REVENUE_VIEWED`/`PAYMENT_VIEWED` on read;
  `CHANNEL_ACCOUNT_LINK_*` on writes).

## 13. Affected files (principal)

- Modify: `backend/ums_smart_revenue/db/finance_models.py` (+2 ORM classes, alongside the
  existing `DeductionComponentORM`).
- Create: `backend/ums_smart_revenue/finance/channel_account_links.py` (read models,
  repository, derivation, typed errors).
- Create: `backend/ums_smart_revenue/api/channel_account_links.py` (router + Pydantic).
- Modify: `backend/ums_smart_revenue/app.py` (mount router).
- Modify: `backend/ums_smart_revenue/auth/audit.py` (3 new `AuditEventType` +
  `AUDIT_EVENT_DEFINITIONS` entries).
- Create: Alembic migration (two tables).
- Create: tests under `tests/finance/` and `tests/api/`.
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md` and/or `Docs/15_DELIVERY_BACKLOG.md` (Phase 4
  status: map substrate shipped; allocation = Spec 2b remaining).

## 14. Decomposition note

**Spec 2b (later):** the allocation engine. Consumes
`list_verified_adsense_account_channels(...)`, distributes ACCOUNT/PAYMENT
`deduction_components` evidence across the verified channel set, leaves unmapped/unverified
accounts UNALLOCATED with a blocking issue, and turns `recalculation.py`'s dry-run preview
into a committed, month-lock-guarded write gated by `CHANGE_ALLOCATION_RULE`.
