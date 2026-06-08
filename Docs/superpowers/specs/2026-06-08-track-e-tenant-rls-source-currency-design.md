# Track E — Tenant RLS Hardening + Source-Currency Read API (Design)

> Status: **approved for planning** — 2026-06-08.
> One combined infrastructure spec, one PR. Covers two independent-but-related
> infrastructure gaps: **S3** (database-level tenant isolation via Postgres
> Row-Level Security) and the remaining **B1** source-currency surface (a
> read-only Google source-rows API). Architecture references:
> `Docs/17_MULTI_TENANT_ARCHITECTURE.md` and `Docs/18_MULTI_CURRENCY_ENGINE.md`.

---

## 1. Why this exists, and what is already done

The gap table called these "not written / not started." That is only half true.
The **schema substrate already exists**; what is missing is the **enforcement
and read surfaces**. This spec is the *delta*, not a re-derivation of Docs/17/18.

### Already in place (do NOT rebuild)

**S3 substrate:**
- `tenants` table + `TenantORM`, seeded UMS tenant
  (`00000000-0000-0000-0000-000000000001`).
- `tenant_id UUID NOT NULL` + FK `ON DELETE RESTRICT` on all original 18
  operational tables (migration `20260517_0001`).
- Composite `(tenant_id, business_key)` FKs on 9 tables (org_units,
  youtube_channels, channel_group_members, monthly_channel_revenue_facts,
  revenue_manual_overrides, user_role_assignments, user_permission_grants,
  and the youtube-channel-identity composite from `20260518_0001`).
- `TenantResolverMiddleware` + immutable `TENANT_CTX` contextvar
  (`tenancy/resolver.py`, `tenancy/context.py`).
- `UserPrincipal.tenant_id`, sourced from `UserORM.tenant_id` in
  `auth/principals.py` (cross-tenant principal load already fails closed).

**B1 substrate:**
- `currencies` reference table + ISO seed + v1 supported set.
- `google_revenue_source_rows` source-of-truth table (migration `20260523_0001`).
- Idempotent `upsert_many` repository keyed on
  `(tenant_id, source_system, source_row_key)`.
- Google connectors (B2, merged) and `GoogleSourceNormalizer.normalize_month`
  (`finance/google_source_normalizer.py`) bridging source rows →
  `monthly_channel_revenue_facts` (currently USD-filtered).

### The actual missing delta (what this PR builds)

**S3 — database-level enforcement:**
1. `app_tenant` / `app_platform` Postgres roles.
2. RLS policies on every tenant-scoped table.
3. A per-transaction `SET LOCAL ROLE` + `SET LOCAL app.current_tenant_id`
   session hook, **Postgres-only** and **gated on tenant context presence**.
4. A privileged `app_platform` session lane.
5. Repository write-side defense-in-depth tenant assertions.
6. A Postgres-only isolation test matrix that runs **as `app_tenant`**.

**B1 — source-rows read API:**
1. `GET /revenue/source-rows?month=&source_system=` (list, paginated).
2. `GET /revenue/source-rows/{id}` (detail).
3. Finance revenue-visibility gate, tenant-scoped reads, `raw_payload`
   redacted by default.

### Explicitly OUT of scope
- **Paired-column `*_usd` → native amount + `currency_code` migration** on
  `monthly_channel_revenue_facts`. Docs/18 §"What happens to exchange rates"
  and §5 non-tests defer this to a separate approved spec. It touches every
  finance reader, export, explain/confidence path, and needs backfill/reset
  notes — bundling it with RLS would make the PR unreviewable.
- **`FORCE ROW LEVEL SECURITY`.** Deferred (see §2.6 follow-up note).
- Any FX-rate workflow (`fx_rates`, `MANAGE_FX_RATES`, `/fx/*`) — permanently
  out per Docs/18.
- Subdomain tenant resolution, Redis caching, platform-admin CRUD endpoints,
  tenant onboarding CLI — all separate Phase S2/Phase 8 work.

---

## 2. S3 — Row-Level Security enforcement

### 2.1 The two roles (chosen mechanism)

| Role | Attributes | Use |
|---|---|---|
| `app_tenant` | `NOLOGIN`, **no** `BYPASSRLS`, not table owner | Normal runtime lane. Every statement is subject to RLS. |
| `app_platform` | `NOLOGIN`, **`BYPASSRLS`** | Explicit privileged lane for platform-admin / cross-tenant reads. Used only by an allowlisted set of platform operations. |

Both are created **idempotently** in the migration (guarded against
`pg_roles`), granted `USAGE` on schema `public`, and granted
`SELECT, INSERT, UPDATE, DELETE` on every tenant-scoped table. `app_platform`
additionally gets `BYPASSRLS`. Neither role owns any object (the migration
owner stays the table owner), so RLS applies to `app_tenant` naturally and the
owner/superuser maintenance path is unaffected.

Role switching is realized with transaction-scoped `SET LOCAL ROLE`, not two
physical connection pools. The application's login user must be a **non-owner,
non-superuser** member of both `app_tenant` and `app_platform`. `SET LOCAL ROLE
app_tenant` makes RLS bite; `SET LOCAL ROLE app_platform` activates `BYPASSRLS`.
`SET LOCAL` auto-resets on commit/rollback, so a pooled connection can never
leak a role or tenant into the next request.

> Reconciliation with Docs/17: Docs/17 sketches "one pool per role." We
> implement the same two-role isolation via `SET LOCAL ROLE` on a single pool
> for now. This is functionally equivalent for RLS (RLS is evaluated against the
> current role) and avoids dual-pool credential management. The dual-pool option
> remains available later without changing the policy/role model.

### 2.2 Which tables get RLS

The original 18 are not the full set anymore. Every table carrying a `tenant_id`
column must be protected, including those added after `20260517`:
`google_revenue_source_rows`, `channel_account_map`, the committed
account-allocation tables (`20260602_0001`), deduction-component tables
(`20260529_0002`), and any post-tax allocation tables.

**Drift guard (correctness requirement):** the migration enumerates tenant
tables from `information_schema.columns WHERE column_name = 'tenant_id'`
(excluding `tenants` itself), applies the policy to each via a helper, **and
asserts the discovered set equals an explicit `TENANT_SCOPED_TABLES` allowlist
constant**. If a table has a `tenant_id` column but is absent from the allowlist
(or vice-versa), the migration **fails** rather than silently leaving a tenant
table unprotected. This makes "a new tenant table shipped without RLS" a loud
migration failure in CI, not a silent leak.

### 2.3 The policy template

Applied per table via a helper (`_enable_tenant_rls(op, table)`):

```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;

CREATE POLICY <table>_tenant_isolation ON <table>
    USING      (tenant_id = current_setting('app.current_tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON <table> TO app_tenant;
```

`current_setting('app.current_tenant_id')` omits `missing_ok`: if the GUC is
unset, Postgres raises and the policy **fails closed** instead of leaking. The
`WITH CHECK` clause blocks `INSERT`/`UPDATE` from writing another tenant's id.

Downgrade drops policies, disables RLS, revokes grants, and drops the two roles
(guarded). The migration docstring documents that, like the `tenant_id NOT NULL`
work before it, the practical rollback is the schema-state reversal only.

### 2.4 The session hook (Postgres-only, context-gated)

In `db/session.py`, register a `Session` `after_begin` event listener:

```python
@event.listens_for(Session, "after_begin")
def _apply_tenant_guc(session, transaction, connection):
    if connection.dialect.name != "postgresql":
        return                                  # SQLite test suite untouched
    tenant = get_current_tenant()               # from TENANT_CTX, may be None
    if tenant is None:
        return                                  # non-tenant path: legacy/owner lane
    connection.exec_driver_sql("SET LOCAL ROLE app_tenant")
    connection.exec_driver_sql(
        "SET LOCAL app.current_tenant_id = %s", (str(tenant.id),)
    )
```

Two deliberate gates, each with a reason:

- **Postgres-only:** the ~1900-test SQLite suite has no RLS and no roles; the
  hook is a no-op there so existing tests are unaffected.
- **Context-gated:** during the pre-S2.4 window (and in existing Postgres
  tests that open sessions without resolving a tenant), `TENANT_CTX` is empty,
  so the hook does nothing and those sessions run on the connecting role exactly
  as today. This keeps the full Postgres suite green while turning enforcement
  **on for real requests**, which always carry tenant context once
  `TenantResolverMiddleware` is installed. A code path that *does* open an
  `app_tenant` session without context still fails closed (missing GUC → error).

`after_begin` (transaction-scoped), not `before_cursor_execute`
(statement-scoped), per Docs/17 §"Setting the GUC".

### 2.5 The platform lane

Add `build_platform_session_factory(...)` (or a `role="app_platform"` parameter
on the existing builder) whose sessions issue `SET LOCAL ROLE app_platform` in
the same `after_begin` hook and do **not** set the tenant GUC. This is the only
sanctioned cross-tenant read path; it is not wired into any tenant-facing route
in this PR (no platform endpoints exist yet) but is provided so future
platform-admin work has a vetted lane instead of reaching for a superuser
connection.

### 2.6 Repository write-side defense-in-depth

Add a shared helper `assert_tenant_match(row_tenant_id, principal_tenant_id)`
that raises a typed `TenantIsolationError` on mismatch, translated at the route
boundary to `403`. Apply it on the tenant-scoped **write** paths:
`SqlAlchemyRevenueFactRepository.record_fact`, manual-override writes,
account-allocation commit/recalc writes, and the Google source-row upsert. RLS
already blocks cross-tenant writes at the DB; this catches the bug *before* the
round-trip and produces a clear domain error instead of a raw DB exception.

> **Follow-up note (not this PR):** Evaluate `FORCE ROW LEVEL SECURITY` after
> the `app_tenant`/`app_platform` rollout, the Alembic role strategy, seed
> scripts, and the Postgres test fixtures are updated to make privileged data
> work explicit.

### 2.7 Isolation test matrix (Postgres-only)

`tests/tenancy/test_isolation.py`, gated on `UMS_TEST_DATABASE_URL`
(`require_postgres_url`, never skips silently). Each test connects and issues
`SET ROLE app_tenant` so it exercises the **real DB boundary** with app-level
filters deliberately bypassed (raw `SELECT *`, no `WHERE tenant_id`):

1. Seed tenant A and tenant B rows in a representative tenant table.
2. With `app.current_tenant_id = A`, a bare `SELECT *` returns **only** A's rows.
3. An `INSERT`/`UPDATE` of a B-owned `tenant_id` while GUC = A is **rejected**
   by `WITH CHECK`.
4. With the GUC **unset**, any query **errors** (fail-closed, not empty).
5. `app_platform` (`SET ROLE app_platform`) reads across tenants (BYPASSRLS).
6. The allowlist/`information_schema` drift guard matches (every `tenant_id`
   table has a policy).

---

## 3. B1 — Source-rows read API

### 3.1 Routes

```http
GET /revenue/source-rows?month=YYYY-MM&source_system=<enum>&cursor=&limit=
GET /revenue/source-rows/{id}
```

- `month`: required on list, validated `YYYY-MM`.
- `source_system`: optional filter; one of
  `youtube_reporting | youtube_analytics | adsense_management`.
- Pagination: cursor over `(ingested_at DESC, id DESC)`, both-or-neither cursor
  halves → `422`, `limit` capped (reuse the established audit/connector-run
  cursor pattern: fetch `limit+1`, `has_more`, `next_cursor`).
- Envelope mirrors existing list routes:
  `{items, pagination:{limit, returned, has_more, next_cursor}}`.

### 3.2 Authorization

Gated on the **finance revenue-read permission** — the same gate guarding the
existing `GET /revenue` facts reads. (Implementation confirms the exact
`Permission` enum against `api/revenue.py` and mirrors it precisely; do not
invent a new permission.) Fail-closed at the route boundary.

### 3.3 Tenant scoping + redaction

- Reads filter by `tenant_id` from the principal (and run under RLS once §2 is
  live — defense-in-depth on both layers).
- `to_api()` **withholds `tenant_id`** (matches `ConnectorRunEntry.to_api`).
- `raw_payload` is **redacted by default**: list never returns it; detail
  returns `raw_payload_redacted: true` and omits the payload body. If an
  existing higher-sensitivity permission pattern is present (mirror the audit
  `details`/`sensitive` model), honor it; otherwise default-redact for all
  callers in this PR (no new sensitive-view permission invented here).
- Cross-tenant `{id}` lookup returns **`404`**, not `403`, to avoid leaking
  existence across tenants.

### 3.4 Repository read

Add a tenant-scoped read (`list_source_rows` / `get_source_row`) — either on the
existing `connectors/google_source_rows/repository.py` or a finance-facing read
module — returning a frozen page/entry dataclass with `to_api()`. No writes.

---

## 4. Data flow

```text
Request → TenantResolverMiddleware sets TENANT_CTX
       → PrincipalResolver binds UserPrincipal.tenant_id
       → route permission gate
       → Session opens; after_begin hook (PG only, ctx present):
             SET LOCAL ROLE app_tenant
             SET LOCAL app.current_tenant_id = <uuid>
       → repository read/write (app-level tenant filter + assert)
       → Postgres enforces RLS via the GUC
```

---

## 5. Blast radius (CLAUDE.md required answers)

- **Tables/ORM affected:** every tenant-scoped table gets RLS policies +
  role grants (no column/shape change). `google_revenue_source_rows` gains a
  read path (no schema change). No `*_usd` change.
- **PostgreSQL still source of truth:** yes. RLS hardens it; nothing moves.
- **Could existing migrations/tests/seed break?** The session hook is
  Postgres-only and context-gated specifically so the existing SQLite suite and
  non-tenant Postgres sessions are unaffected. Seed scripts that connect as
  owner/superuser are unaffected (owner is not RLS-restricted). **Risk:** the
  app's runtime DB login user must become a non-owner member of both roles; a
  deploy that keeps connecting as a superuser/owner would silently *not* enforce
  RLS — the isolation test (run as `app_tenant`) is the guard, plus an explicit
  runbook note.
- **Could Neo4j over-trust?** No graph projection impact detected (Neo4j retired
  from the active architecture).
- **Could authz/audit become more permissive?** No — strictly more restrictive
  (DB-level deny added) plus a read API behind an existing finance gate.
- **Finance results / locks / overrides change?** No numbers change. Reads gain
  a tenant boundary; writes gain a pre-flight tenant assert.
- **Backward compatible / destructive?** Additive at the schema level (policies,
  roles, grants). Practically irreversible cleanly only in the same sense the
  prior `tenant_id NOT NULL` work was; documented in the migration docstring.
- **Rollback/reset note required?** Downgrade drops policies/roles/grants;
  documented. No data reseed required.

Statement: **`No graph projection impact detected.`** (Neo4j is retired;
PostgreSQL remains the sole financial source of truth.)

---

## 6. Testing contract

**S3:**
- `tests/tenancy/test_isolation.py` (Postgres-only) — the §2.7 matrix.
- Session-hook unit test: SQLite session triggers no `SET` statements;
  context-absent Postgres session triggers none; context-present sets both.
- Repository write-assert tests: cross-tenant write raises
  `TenantIsolationError` → `403`.
- Migration round-trip on Postgres; drift-guard assertion exercised.

**B1:**
- Permission denial (no finance-read permission → 403).
- Tenant isolation: A's principal cannot list/detail B's source rows
  (app filter + RLS); cross-tenant `{id}` → 404.
- Filters honored: `month`, `source_system`.
- Pagination: half-cursor → 422; `has_more`/`next_cursor` correct.
- `raw_payload` redaction: never present in list; detail flagged redacted.
- Round-trip read returns source provenance fields (no `tenant_id` leak).

**Full gate before push:** `python -m ruff check backend tests scripts`,
`git diff --check`, full `python -m pytest` against the disposable
`ums-mig-pg-test` Postgres container (`UMS_TEST_DATABASE_URL`) so the
Postgres-only RLS + migration tests actually execute.

---

## 7. Docs to update with the implementation

- `Docs/17_MULTI_TENANT_ARCHITECTURE.md` — mark RLS enforcement + the two roles
  as implemented; note the `SET LOCAL ROLE` single-pool realization and the
  context-gated hook; record the FORCE-RLS follow-up.
- `Docs/18_MULTI_CURRENCY_ENGINE.md` — mark the source-rows read API as built;
  document redaction behavior; restate that the paired-column migration remains
  a separate future spec.
- `Docs/12_BACKEND_API_SPEC.md` — document `GET /revenue/source-rows` + `/{id}`.
- `Docs/01_IMPLEMENTATION_PLAN.md` + `Docs/15_DELIVERY_BACKLOG.md` — per-PR
  status: S3 RLS done, B1 read API done, paired-column migration still pending.

---

## 8. Open decisions (resolve during implementation, not blocking)

- Exact runtime DB login user / credential wiring for the two-role membership
  (settings + deploy runbook). The code path is role-switch via `SET LOCAL
  ROLE`; the only external dependency is that the login user is a non-owner
  member of both roles.
- Exact finance read `Permission` enum for the source-rows API (mirror the
  existing facts-read gate; confirm in `api/revenue.py`).
- Whether a future sensitive-payload view permission is warranted for
  `raw_payload` (default-redact ships now; expansion is additive later).
