# Multi-Tenant Architecture

> Status: **partially implemented** — Postgres RLS enforcement and the two
> Postgres roles are IMPLEMENTED (Track E, migration `20260608_0001`,
> 2026-06-08). The remaining tenant-resolver middleware / subdomain routing /
> platform-admin model below stay planned for Phase S2.
> Drives data-model design from day one so additional tenants (e.g. Rotana Holding) can onboard without a re-migration.

---

## Implementation status — RLS enforcement (Track E, IMPLEMENTED 2026-06-08)

RLS enforcement and the two Postgres roles landed in Track E. The realization
differs from the original dual-pool sketch below in a few important ways; this
section is authoritative where it conflicts with the planning text further down.

- **Roles created**: `app_tenant` and `app_platform` (both non-superuser,
  non-owner). Defined in `backend/ums_smart_revenue/db/rls.py`
  (`APP_TENANT_ROLE`, `APP_PLATFORM_ROLE`).
- **Tenant-scoped tables**: **25** tables carry `tenant_id` and receive an
  isolation policy (the older "18" figure is stale). The canonical allowlist is
  `TENANT_SCOPED_TABLES` in `db/rls.py`; the migration cross-checks it against
  the live `information_schema` so a new tenant table shipped without RLS is a
  loud migration failure, not a silent leak.
- **Isolation policies**: created by migration `20260608_0001` on all 25 tables,
  each named `<table>_tenant_isolation`, with `USING`/`WITH CHECK` on
  `tenant_id = app_current_tenant_id()`. The helper returns `NULL` when no
  trusted tenant is registered for the current backend, so the policy fails
  closed without trusting tenant-settable session state.
- **Single-pool `SET LOCAL ROLE` realization (not dual-pool)**: there is one
  connection pool. The lane is selected per session, not per pool. The platform
  lane uses `build_platform_session_factory` in `db/session.py`, which tags the
  sessionmaker `info` with the `app_platform` role; the default lane runs as
  `app_tenant`. The lane is realized at transaction begin via
  `SET LOCAL ROLE "<role>"` (transaction-scoped, auto-reset on commit/rollback),
  so a pooled connection never carries a role across requests.
- **Context-gated, Postgres-only hook**: the `after_begin` event listener
  `_apply_tenant_isolation` in `db/session.py` is a **no-op on SQLite** (it
  returns unless `dialect.name == "postgresql"`) and a **no-op on tenant-lane
  sessions opened without a resolved tenant** (no tenant in the
  `tenancy.context` contextvar => no trusted tenant row, no tenant-specific
  privileges). The platform lane always issues `SET LOCAL ROLE "app_platform"`
  and populates the trusted tenant context through backend-owned helper
  functions before switching to `app_tenant` when needed. This keeps the
  pre-S2.4 non-tenant paths and the SQLite test suite unaffected.
- **Runtime login membership requirement**: the application connects as a
  dedicated login role that must be granted membership in the lane roles with
  `GRANT app_tenant TO <login> WITH INHERIT FALSE, SET TRUE` and
  `GRANT app_platform TO <login> WITH INHERIT FALSE, SET TRUE`. `INHERIT FALSE`
  means the login does **not** passively hold either lane's privileges; `SET TRUE`
  lets it `SET ROLE` into the chosen lane per transaction. The login itself is
  non-owner and non-superuser, so it cannot bypass RLS by default.
- **Deploy precondition**: the migration/bootstrap user needs role-management
  privilege to create the roles and grants, **or** a DBA pre-creates the
  `app_tenant`/`app_platform` roles, their table grants, and the login
  membership out of band per the runbook. The migration is **idempotent** and
  **does not assume superuser** — it tolerates pre-existing roles/grants.

### Grant model — least-privilege: broad SELECT, narrow DML

The two app roles receive a **least-privilege** grant surface, not blanket CRUD:

- **Broad SELECT** across the whole `public` schema
  (`GRANT SELECT ON ALL TABLES IN SCHEMA public`) plus
  `GRANT USAGE, SELECT ON ALL SEQUENCES`. Reads are harmless under RLS and the
  app lane needs to read non-tenant platform catalogs, so SELECT is granted
  broadly.
- **DML (INSERT/UPDATE/DELETE)** is granted on **only** three sets of tables:
  1. the **25 tenant-scoped tables** (per-table CRUD, isolated by RLS), and
  2. the enumerated **`NON_TENANT_WRITE_TABLES`** the app writes at runtime that
     carry no `tenant_id`: `currency_exchange_rates`,
   3. the platform-only operational tables `audit_logs`,
      `finance_month_close`, and `monthly_channel_revenue_facts`, plus the
      committed-allocation child evidence tables
      `committed_allocation_lines`, `committed_allocation_notes`,
      `committed_allocation_unallocated` (all constant in migration
      `20260608_0001`).

App DML therefore **cannot touch platform catalogs** (`permissions`, `roles`,
`role_permission_assignments`, `currencies`, ...): those get SELECT only. Tenant
isolation is still enforced by **RLS on the 25 tenant-scoped tables**.

The non-tenant, platform-shared tables that carry no `tenant_id` (and so have no
RLS policy) but that the app lane reads include:

- authz catalogs: `permissions`, `roles`, `role_permission_assignments`,
- `currencies`, `currency_exchange_rates`,
- the committed-allocation child tables `committed_allocation_lines`,
  `committed_allocation_notes`, `committed_allocation_unallocated`.

Under the restricted runtime login (non-owner/non-superuser), `app_tenant` only
holds what the migration grants it. Without broad SELECT those endpoints would
fail with `permission denied` once RLS is enforced. If a future endpoint writes
another non-tenant table, add it to the relevant platform-write allowlist
otherwise it `permission denies` under the restricted login — covered by
`tests/tenancy/test_rls_restricted_login.py`. Runtime authorization remains the
**application permission system**, not table grants.

### Resolver runs on the platform lane

The trusted-gateway tenant resolver middleware reads the `tenants` table to map
slug → tenant **before** tenant context (`TENANT_CTX`) is set. On the tenant
lane the `after_begin` session hook no-ops when no tenant is in context, so the
session would run as the **bare login** — which, under `INHERIT FALSE`, holds no
grants and gets `permission denied`. The resolver is therefore wired with
`build_platform_session_factory(...)` (`app.py`): the platform lane switches on
via `session.info` regardless of context and holds the grants needed to read
`tenants`. `tenants` is not an RLS table, so RLS is not a factor here — only
`app_platform`'s grant surface matters.

> **Follow-up (future spec):** the three `committed_allocation_*` child tables
> carry no `tenant_id` and are isolated only **transitively** via the `run_id`
> FK to `committed_allocation_runs` (which IS RLS-protected). A future spec
> should add `tenant_id` to these child tables for direct DB-level isolation.

### FORCE ROW LEVEL SECURITY follow-up — DELIVERED (2026-06-14, `feat/force-row-level-security`)

RLS is now **`FORCE`d** in addition to being enabled. Migration
`20260612_0002_force_tenant_rls` runs `ALTER TABLE ... FORCE ROW LEVEL SECURITY`
on **every** table in `TENANT_SCOPED_TABLES` (the same 25-table allowlist in
`db/rls.py`), reusing the ENABLE migration's drift primitive
(`discover_tenant_tables_sql` vs `TENANT_SCOPED_TABLES`, subtracting the
`app_tenant_context` backend-PID helper via `TENANT_CONTEXT_TABLE`) so a new
tenant table cannot ship un-`FORCE`d. It is Postgres-only (dialect-guarded
no-op off Postgres), idempotent, and rolls back with `NO FORCE` while leaving
ENABLE + the isolation policies in place.

What `FORCE` changes vs ENABLE: ENABLE already bound the non-owner app roles
(`app_tenant`/`app_platform` are non-superuser, non-`BYPASSRLS`, non-owner).
`FORCE` additionally binds the **non-superuser table owner**, which by Postgres
design bypasses RLS otherwise — so a non-superuser owner would silently read
across the policy. This is defense-in-depth that **completes Track-E** isolation
at the owner boundary. A **superuser** and any **`BYPASSRLS`** role still bypass
`FORCE` (unchanged Postgres semantics); in the disposable test DB the login is
the postgres superuser that owns the tables, so `FORCE` is invisible to it and
the existing PG RLS tests are unaffected. The behavioural proof lives in
`tests/tenancy/test_force_rls.py`, which stands up a throwaway non-superuser
owner and A/B-contrasts a `FORCE`d table against an ENABLE-only one.

---

## Why multi-tenant

UMS is tenant #1. The system is being built so that **any additional holding** (Rotana Holding, future portfolio acquisitions, partner agencies) can be onboarded as a fully isolated tenant **without code changes or schema migrations**. Adding a tenant must be a configuration step, not a fork.

This page describes the chosen approach, the data model, the request path, the authorization implications, and the migration sequence to move the existing single-tenant code base.

---

## Approach: shared schema + row-level security

We evaluated three options:

| Option | Isolation | Operational cost | Verdict |
|---|---|---|---|
| **Schema-per-tenant** (separate Postgres schemas) | Strong | High — N schemas, N migrations, N connection pools | ❌ Too expensive for the planned scale (≤ 50 tenants) |
| **Database-per-tenant** | Strongest | Very high — N databases, N alembic histories | ❌ Overkill; loses cross-tenant platform reporting |
| **Shared schema + `tenant_id` column + RLS** | Strong with RLS | Low — one schema, one migration, one pool | ✅ Chosen |

Postgres **Row-Level Security** enforces tenant filtering at the database layer. The application **also** filters by tenant in code; RLS is defense in depth, not the sole gatekeeper.

---

## Tenant model

### `tenants` table

```sql
CREATE TABLE tenants (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug             TEXT NOT NULL UNIQUE,             -- 'ums', 'rotana'
    display_name     TEXT NOT NULL,
    primary_currency TEXT NOT NULL DEFAULT 'USD',      -- ISO 4217 display/default
    status           TEXT NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE|SUSPENDED|ARCHIVED
    onboarding_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_tenants_slug_lower CHECK (slug = lower(slug)),
    CONSTRAINT ck_tenants_primary_currency_format CHECK (
      length(primary_currency) = 3 AND primary_currency = upper(primary_currency)
    ),
    CONSTRAINT ck_tenants_status CHECK (status IN ('ACTIVE','SUSPENDED','ARCHIVED'))
);
```

### Tenant-scoped tables

Every existing **operational** table receives a `tenant_id UUID NOT NULL` FK. Tables that are inherently platform-wide do not.

| Receives `tenant_id` | Stays platform-wide |
|---|---|
| `youtube_channels`, `channel_groups`, `channel_group_members` | `tenants`, `currencies` |
| `org_units` | `permissions`, `role_permission_assignments` (definition catalog) |
| `monthly_channel_revenue_facts`, `revenue_manual_overrides`, `adsense_payments`, `bank_reconciliation_entries`, `finance_month_close` | `roles` (definition catalog) |
| `raw_report_files`, `number_explanations`, `export_jobs`, `api_connector_credentials` | _(none — `audit_logs` is tenant-scoped; no platform-wide audit table exists)_ |
| `users`, `user_role_assignments`, `user_permission_grants`, `access_scopes` | |
| `audit_logs` (tenant-scoped audit) | |

> **Users sit inside a tenant.** A given email can have separate accounts in different tenants. SSO will map identity → tenant at login.

The existing global `uq_users_email_lower` constraint must be replaced by a tenant-scoped unique key on `(tenant_id, lower(email))` before this invariant is enabled.

### Row-Level Security policies

A short example for one table:

```sql
ALTER TABLE monthly_channel_revenue_facts ENABLE ROW LEVEL SECURITY;

CREATE POLICY monthly_channel_revenue_facts_tenant_isolation
    ON monthly_channel_revenue_facts
    USING       (tenant_id = app_current_tenant_id())
    WITH CHECK  (tenant_id = app_current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON monthly_channel_revenue_facts TO app_tenant;
```

The same policy template applies to all tenant-scoped tables. Migration files generate them with a helper function. `app_current_tenant_id()` returns `NULL` when the backend has no trusted tenant context row, so the policy fails closed instead of leaking rows.

### The two Postgres roles

| Role | Use |
|---|---|
| `app_tenant` | The app runs as this role. All queries are subject to RLS. Cannot bypass tenant isolation. |
| `app_platform` | Reserved for platform-admin operations and platform-only writes (audit logs, finance facts, month-close). Does **not** bypass RLS (`NOBYPASSRLS`); RLS still applies via the same trusted-context policies. Gains its wider write surface through explicit per-table DML grants, not a role-level RLS bypass. Used by a small set of explicitly platform-level endpoints only. |

Connection-pool config: the app uses `app_tenant`. Platform endpoints route through a separate, narrowly-scoped session factory using `app_platform`.

---

## Request path

```text
HTTP request
    │
    ▼
TenantResolver middleware           (reads X-UMS-Tenant header or subdomain
    │                                tenant.{host}; looks up `tenants.slug`)
    ▼
PrincipalResolver dependency        (validates user belongs to tenant)
    │
    ▼
Session opened on `app_tenant` ────▶ executes
    backend-owned trusted tenant context
    │
    ▼
Router → repository → ORM
    │
    ▼
Postgres applies RLS using
    `app_current_tenant_id()`
```

### Tenant resolver

Tenant slug resolution is **both header and subdomain based**. `X-UMS-Tenant` is accepted for internal service-to-service calls and local development; `tenant.{host}` is the browser-facing default. If both are present they must resolve to the same tenant slug, otherwise the resolver rejects the request with `400 Tenant mismatch`.

```python
# backend/ums_smart_revenue/tenancy/resolver.py (sketch)
class TenantResolverMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        header_slug = request.headers.get("x-ums-tenant")
        host_slug = self._slug_from_subdomain(request.url.hostname)
        if header_slug and host_slug and header_slug != host_slug:
            raise HTTPException(400, "Tenant mismatch")
        slug = header_slug or host_slug
        if not slug:
            raise HTTPException(400, "Tenant not specified")
        tenant = await self._resolve(slug)
        request.state.tenant = tenant
        TENANT_CTX.set(tenant)
        return await call_next(request)
```

Tenant resolution is cached in Redis with a short TTL keyed by `slug`.

### Setting the trusted tenant context

A SQLAlchemy transaction-begin hook populates the backend-owned tenant context once per transaction, using the tenant from the `contextvars.ContextVar`. Do **not** use `before_cursor_execute`: it is statement-scoped and would re-run a transaction-scoped setting on every query. The trusted setter runs while `app_platform` is active, then the transaction switches to `app_tenant` for normal tenant-scoped work. The context row is cleared automatically when the pooled connection has no tenant in context, so later requests do not inherit a previous tenant.

---

## Authorization implications

### Roles become tenant-scoped

- The `roles` table stays platform-wide as the **definition** catalog. A `RoleKey` (e.g. `FINANCE_ADMIN`) is the same concept everywhere.
- `user_role_assignments` carries `tenant_id` because the same `RoleKey` is granted **per-tenant**. UMS's `FINANCE_ADMIN` ≠ Rotana's `FINANCE_ADMIN`.
- Scope objects (`access_scopes`) are also tenant-scoped (every concrete `company`/`sector`/`channel` value belongs to a single tenant).
- Existing global uniqueness on `access_scopes` must be replaced with tenant-scoped uniqueness. The current singleton `global` scope becomes unique on `(tenant_id, scope_type)`; concrete scopes use `(tenant_id, scope_type, scope_id)` so every tenant can have its own `global` scope and its own channel/company/sector identifiers.

### New role: `PLATFORM_ADMIN`

- Lives outside any tenant. Manages tenants (create, suspend, archive), provisions the first super-owner per tenant, reads platform audit logs.
- Cannot view tenant financial data without explicit per-tenant role assignment.
- Stored in `platform_admins` (separate table; not in `users`) so it is impossible to assign tenant-bound resources to it accidentally.

### Audit logs

- One table: `audit_logs` (tenant-scoped; covers both tenant operations and platform-admin actions via the `app_platform` role).
- Sensitive-payload masking, reason-required rules, and append-only triggers apply to this table.

---

## Migration plan

Implemented as coordinated Alembic revisions in Phase S2. Transactional DDL/data-shape changes stay inside normal Alembic transactions; long backfills and concurrent indexes are split into explicit autocommit/data revisions because PostgreSQL rejects `CREATE INDEX CONCURRENTLY` inside `context.begin_transaction()` and per-batch commits are not real inside one Alembic transaction.

Execution order is `20260516_0001_tenants_foundation` → `20260517_0001_tenant_id_on_operational_tables` → `20260518_0001_tenant_scoped_youtube_channel_identity`. Each revision's `down_revision` encodes the chain explicitly.

The revision IDs are the as-built chain, but the numbered steps under each
heading below reflect the original Phase-S2 plan groupings, not the literal
contents of each revision. As built: `20260516_0001` created the `tenants` and
`platform_admins` tables; `20260517_0001` added `tenant_id` columns and
tenant-scoped constraints/FKs to the operational tables; and `20260518_0001`
scoped YouTube channel identity. The `app_tenant` / `app_platform` Postgres roles
and the Row-Level Security isolation policies were **not** part of this chain —
they landed later in `20260608_0001_tenant_rls_enforcement` and were hardened with
`FORCE ROW LEVEL SECURITY` in `20260612_0002_force_tenant_rls`. Treat the
per-heading steps below as the historical plan rather than a description of each
revision.

### `20260516_0001_tenants_foundation`

1. Create `tenants` table. Seed UMS as `('00000000-0000-0000-0000-000000000001', 'ums', 'UMS', 'USD')`.
2. Create `platform_admins` table.
3. _(No separate platform audit table — all audit events write to the existing tenant-scoped `audit_logs` table via the `app_platform` role for platform-admin actions.)_
4. For each tenant-scoped table:
   1. Add `tenant_id UUID NULL`.
   2. Add the FK to `tenants(id)` ON DELETE RESTRICT with `NOT VALID`.
   3. Add `CHECK (tenant_id IS NOT NULL) NOT VALID`.
5. Create `app_tenant` and `app_platform` Postgres roles.
6. Grant the right read/write surface to each role. Do not enable tenant-isolation RLS yet; existing rows are still `tenant_id = NULL` until the backfill revision completes.

### `20260517_0001_tenant_id_on_operational_tables`

1. Runs as an explicit data migration outside the normal transaction wrapper.
2. Backfills all existing rows to UMS's tenant_id in 5,000-10,000 row batches, committing each batch.
3. Validates the `tenants(id)` FKs and `tenant_id IS NOT NULL` checks.
4. Sets `tenant_id` `NOT NULL` in short lock windows after validation succeeds.
5. Convert tenant-scoped inter-table FKs from single-column references to composite tenant-aware references. For example, `youtube_channels.primary_org_unit_id -> org_units(id)` becomes `(tenant_id, primary_org_unit_id) -> org_units(tenant_id, id)`, backed by a unique key on `org_units(tenant_id, id)`, so database RI cannot cross tenant boundaries even when RLS is bypassed. For nullable actor references that currently use `ON DELETE SET NULL`, use column-targeted `SET NULL` on the actor id only or keep an equivalent action that never tries to null the non-null `tenant_id`.
6. Add non-partial composite unique keys for tenant-aware FK targets, including `users(tenant_id, id)`, before any child table references them.
7. Re-key tenant-owned singleton tables. `finance_month_close` changes from a global primary key on `month` to a tenant-scoped key on `(tenant_id, month)` so multiple tenants can lock the same calendar month independently.
8. Set `lock_timeout` before every DDL step so the migration fails fast instead of blocking production traffic.
9. Replace the existing global `uq_users_email_lower` index with a tenant-scoped unique index on `(tenant_id, lower(email))`.
10. Replace global `access_scopes` uniqueness with tenant-scoped unique constraints as described above.
11. Replace `api_connector_credentials` uniqueness with tenant-scoped uniqueness on `(tenant_id, connector_key, account_id)` so two tenants can hold overlapping connector account identifiers safely.
12. Re-key every remaining tenant-owned unique constraint to include `tenant_id`; examples include `adsense_payments(month, payment_name)` -> `(tenant_id, month, payment_name)` and `bank_reconciliation_entries(month, bank_reference)` -> `(tenant_id, month, bank_reference)`.
13. Enable RLS on each tenant-scoped table and install the final isolation policy only after backfill, `NOT NULL`, and tenant-scoped keys are in place.

### `20260518_0001_tenant_scoped_youtube_channel_identity`

1. Runs outside a transaction/autocommit block.
2. Adds read-path indexes `(tenant_id, ...)` with `CREATE INDEX CONCURRENTLY`.
3. Contains no data backfill, no FK validation, and no `ALTER TABLE ... SET NOT NULL`.

The index migration is reversible in isolation with `DROP INDEX CONCURRENTLY`. Once the foundation/backfill migrations apply the production `tenant_id NOT NULL` constraints, the overall tenant_id schema change is destructive and cannot be fully reversed by simply dropping columns. Document that rollback caveat in the migration docstrings.

### Code changes alongside

1. Add `backend/ums_smart_revenue/tenancy/` package (`models.py`, `resolver.py`, `context.py`, `repository.py`).
2. Extend `UserPrincipal` with `tenant_id`.
3. Insert `TenantResolverMiddleware` into the app factory.
4. Add the transaction-begin trusted-context hook in `db/session.py`.
5. Update every repository to assert `tenant_id == principal.tenant_id` on writes (defense-in-depth even though RLS will block).
6. Add `tests/tenancy/test_isolation.py` — a full matrix proving tenant A cannot access tenant B via any endpoint.

---

## Operational concerns

| Concern | Approach |
|---|---|
| **Connection pooling** | One pool per role (`app_tenant`, `app_platform`). SQLAlchemy's async engine, with pool size sized for tenant concurrency, not tenant count. |
| **Backups** | Single physical backup. For tenant-slice restores, export schema with `pg_dump --schema-only -t <table_name> -h <host> -U <user> -d <dbname>` and export rows with `psql -h <host> -U <user> -d <dbname> -c "COPY (SELECT * FROM <table_name> WHERE tenant_id = '<tenant_uuid>') TO STDOUT"`; restore tooling replays schema first, then `COPY` data per tenant-scoped table. |
| **Per-tenant disable** | `tenants.status = 'SUSPENDED'`: tenant resolver returns 423 Locked. Data stays in place. |
| **Per-tenant offboarding** | `tenants.status = 'ARCHIVED'` → blocks new connectors, exports, and writes; reads still allowed for finance reconciliation completion. |
| **Cross-tenant reporting** | Only platform-admin endpoints; explicit allowlist; audited. |
| **Tenant onboarding CLI** | Phase 8 — `ums-admin tenant create <slug>` inserts the row, seeds default roles, provisions Google OAuth credential placeholders, records the primary display currency, creates the bootstrap `SUPER_OWNER` user. < 30 minute end-to-end. |

---

## What this is NOT

- **Not** a customer-facing SaaS multi-tenant model. There is no self-service signup; tenants are onboarded by platform admin.
- **Not** a database-per-tenant strategy. Cost of N pgBackRest, N pgBouncer, N Alembic histories was rejected.
- **Not** a feature-flag system. Tenant-specific behavior changes should be configuration-driven (per-tenant settings table) rather than coded branches.

---

## Acceptance gates (Phase S2)

- Tenant-isolation test suite green: zero cross-tenant data reachable through any endpoint.
- `Second tenant can be seeded` — running `INSERT INTO tenants (slug, display_name) VALUES ('rotana', 'Rotana Holding')` plus the bootstrap checklist is enough: create the first `SUPER_OWNER` user in `users`, grant the tenant-scoped `SUPER_OWNER` role, seed default access scopes for that tenant, and verify login resolves the new tenant.
- All existing Phase 1 routes pass under both `headers` and `database` auth modes, with the resolved tenant carried through to the DB session.
- RLS verified at DB level (a direct SQL query as `app_tenant` from outside the app cannot read another tenant's rows).

---

## Open decisions (resolve before Phase S2 lands)

- Whether `platform_admins` reuses the same OIDC IdP as tenant users or has a separate one. (Recommend same with a dedicated `platform` group.)
- Where per-tenant settings (theme, default currency, brand assets) live. (Recommend a `tenant_settings` JSONB column on `tenants`.)
