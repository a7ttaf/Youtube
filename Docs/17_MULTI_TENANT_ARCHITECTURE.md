# Multi-Tenant Architecture

> Status: **planned** — to be implemented in Phase S2.
> Drives data-model design from day one so additional tenants (e.g. Rotana Holding) can onboard without a re-migration.

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
    primary_currency CHAR(3) NOT NULL DEFAULT 'USD',   -- ISO 4217
    status           TEXT NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE|SUSPENDED|ARCHIVED
    onboarding_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_tenants_slug_lower CHECK (slug = lower(slug)),
    CONSTRAINT ck_tenants_status CHECK (status IN ('ACTIVE','SUSPENDED','ARCHIVED'))
);
```

### Tenant-scoped tables

Every existing **operational** table receives a `tenant_id UUID NOT NULL` FK. Tables that are inherently platform-wide do not.

| Receives `tenant_id` | Stays platform-wide |
|---|---|
| `youtube_channels`, `channel_groups`, `channel_group_members` | `tenants`, `currencies` |
| `org_units`, `channel_mappings` | `permissions` (definition catalog) |
| `revenue_facts`, `manual_revenue_overrides`, `adsense_payments`, `bank_reconciliation_entries`, `finance_month_close` | `roles` (definition catalog) |
| `raw_report_files`, `number_explanations`, `export_jobs` | `platform_audit_logs` |
| `users`, `user_role_assignments`, `user_permission_grants`, `access_scopes` | |
| `audit_logs` (tenant-scoped audit) | |

> **Users sit inside a tenant.** A given email can have separate accounts in different tenants. SSO will map identity → tenant at login.

The existing global `uq_users_email_lower` constraint must be replaced by a tenant-scoped unique key on `(tenant_id, lower(email))` before this invariant is enabled.

### Row-Level Security policies

A short example for one table:

```sql
ALTER TABLE revenue_facts ENABLE ROW LEVEL SECURITY;

CREATE POLICY revenue_facts_tenant_isolation
    ON revenue_facts
    USING       (tenant_id = current_setting('app.current_tenant_id')::uuid)
    WITH CHECK  (tenant_id = current_setting('app.current_tenant_id')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON revenue_facts TO app_tenant;
```

The same policy template applies to all tenant-scoped tables. Migration files generate them with a helper function.

### The two Postgres roles

| Role | Use |
|---|---|
| `app_tenant` | The app runs as this role. All queries are subject to RLS. Cannot bypass tenant isolation. |
| `app_platform` | Reserved for platform-admin operations (tenant CRUD, cross-tenant reporting). Bypasses RLS by virtue of `BYPASSRLS`. Used by a small set of explicitly platform-level endpoints only. |

Connection-pool config: the app uses `app_tenant`. Platform endpoints route through a separate, narrowly-scoped session factory using `app_platform`.

---

## Request path

```
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
    `SET LOCAL app.current_tenant_id = '<uuid>'`
    once per transaction
    │
    ▼
Router → repository → ORM
    │
    ▼
Postgres applies RLS using
    `app.current_tenant_id` GUC
```

### Tenant resolver

Tenant slug resolution is **both header and subdomain based**. `X-UMS-Tenant` is accepted for internal service-to-service calls and local development; `tenant.{host}` is the browser-facing default. If both are present they must resolve to the same tenant slug, otherwise the resolver rejects the request with `400 Tenant mismatch`.

```python
# backend/ums_smart_revenue/tenancy/resolver.py (sketch)
class TenantResolverMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        slug = (
            request.headers.get("x-ums-tenant")
            or self._slug_from_subdomain(request.url.hostname)
        )
        if not slug:
            raise HTTPException(400, "Tenant not specified")
        tenant = await self._resolve(slug)
        request.state.tenant = tenant
        TENANT_CTX.set(tenant)
        return await call_next(request)
```

Tenant resolution is cached in Redis with a short TTL keyed by `slug`.

### Setting the GUC

A SQLAlchemy transaction-begin hook issues `SET LOCAL app.current_tenant_id = ...` once per transaction, using the tenant from the `contextvars.ContextVar`. Do **not** use `before_cursor_execute`: it is statement-scoped and would re-run a transaction-scoped setting on every query. `SET LOCAL` is cleared automatically on commit or rollback, and pooled connections must never retain tenant state outside the transaction.

---

## Authorization implications

### Roles become tenant-scoped

- The `roles` table stays platform-wide as the **definition** catalog. A `RoleKey` (e.g. `FINANCE_ADMIN`) is the same concept everywhere.
- `user_role_assignments` carries `tenant_id` because the same `RoleKey` is granted **per-tenant**. UMS's `FINANCE_ADMIN` ≠ Rotana's `FINANCE_ADMIN`.
- Scope objects (`access_scopes`) are also tenant-scoped (every concrete `company`/`sector`/`channel` value belongs to a single tenant).

### New role: `PLATFORM_ADMIN`

- Lives outside any tenant. Manages tenants (create, suspend, archive), provisions the first super-owner per tenant, reads platform audit logs.
- Cannot view tenant financial data without explicit per-tenant role assignment.
- Stored in `platform_admins` (separate table; not in `users`) so it is impossible to assign tenant-bound resources to it accidentally.

### Audit logs

- Two tables: `audit_logs` (tenant-scoped, used by tenant operations) and `platform_audit_logs` (cross-tenant, platform-admin actions).
- All sensitive-payload masking, reason-required rules, and append-only triggers apply equally to both.

---

## Migration plan

Implemented as two coordinated Alembic revisions in Phase S2:

### `20260520_0001_multi_tenant_foundation`

1. Create `tenants` table. Seed UMS as `('00000000-0000-0000-0000-000000000001', 'ums', 'UMS', 'USD')`.
2. Create `platform_admins` table.
3. For each tenant-scoped table:
   1. Add `tenant_id UUID NULL`.
   2. Backfill all existing rows to UMS's tenant_id in 5,000-10,000 row batches, committing each batch.
   3. Add the FK to `tenants(id)` ON DELETE RESTRICT with `NOT VALID`, then `VALIDATE CONSTRAINT`.
   4. Add `CHECK (tenant_id IS NOT NULL) NOT VALID`, then `VALIDATE CONSTRAINT`, before finally setting the column `NOT NULL` in a short lock window.
   5. Add composite indexes `(tenant_id, ...)` matching the existing access pattern with `CREATE INDEX CONCURRENTLY`.
   6. Set `lock_timeout` before every DDL step so the migration fails fast instead of blocking production traffic.
4. Replace the existing global `uq_users_email_lower` index with a tenant-scoped unique index on `(tenant_id, lower(email))`.
5. Create `app_tenant` and `app_platform` Postgres roles.
6. Enable RLS on each tenant-scoped table; install isolation policy.
7. Grant the right read/write surface to each role.

This migration is **reversible** in development but **destructive** in production after the NOT NULL step. Document the rollback caveat in the migration's docstring.

### Code changes alongside

1. Add `backend/ums_smart_revenue/tenancy/` package (`models.py`, `resolver.py`, `context.py`, `repository.py`).
2. Extend `UserPrincipal` with `tenant_id`.
3. Insert `TenantResolverMiddleware` into the app factory.
4. Add the transaction-begin `SET LOCAL app.current_tenant_id` hook in `db/session.py`.
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
| **Tenant onboarding CLI** | Phase 8 — `ums-admin tenant create <slug>` inserts the row, seeds default roles, provisions OAuth credential placeholders, configures FX provider, creates the bootstrap `SUPER_OWNER` user. < 30 minute end-to-end. |

---

## What this is NOT

- **Not** a customer-facing SaaS multi-tenant model. There is no self-service signup; tenants are onboarded by platform admin.
- **Not** a database-per-tenant strategy. Cost of N pgBackRest, N pgBouncer, N Alembic histories was rejected.
- **Not** a feature-flag system. Tenant-specific behavior changes should be configuration-driven (per-tenant settings table) rather than coded branches.

---

## Acceptance gates (Phase S2)

- Tenant-isolation test suite green: zero cross-tenant data reachable through any endpoint.
- `Second tenant can be seeded` — running `INSERT INTO tenants (slug, display_name) VALUES ('rotana', 'Rotana Holding')` followed by the user bootstrap is enough.
- All existing Phase 1 routes pass under both `headers` and `database` auth modes, with the resolved tenant carried through to the DB session.
- RLS verified at DB level (a direct SQL query as `app_tenant` from outside the app cannot read another tenant's rows).

---

## Open decisions (resolve before Phase S2 lands)

- Whether `platform_admins` reuses the same OIDC IdP as tenant users or has a separate one. (Recommend same with a dedicated `platform` group.)
- Where per-tenant settings (theme, default currency, brand assets) live. (Recommend a `tenant_settings` JSONB column on `tenants`.)
