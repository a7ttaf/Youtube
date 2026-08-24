# 20 — Deployment Readiness Audit (First Beta, Single PC)

**Audited:** 2026-08-24, at `main` = `d8418cea2`.
**Target deployment:** first beta on one Windows PC via `docker compose`, **bound to
localhost only**, operator-run, with **real** YouTube CMS revenue data.
**Method:** read-only code audit. Every finding below carries `file:line` evidence.
Load-bearing conclusions were re-checked by an independent adversarial pass, and the
corrections that pass produced are folded in (three of the original findings were
downgraded or reversed — see *Corrections* at the end).

> **Scope note.** This audit answers one question: *what stands between the current
> `main` and a first beta on this PC?* It is not a security review of the product
> design, and it is not a judgement on the feature work — the finance engine, RLS,
> audit trail, and review discipline are in good shape. Everything below is about
> **running** it.

---

## Verdict

**The application is feature-ready; the deployment is not.** Nothing here requires
redesign — the gaps are packaging, bootstrap, and durability. There are **five
blockers**, and the most important one is architectural in nature even though its
fix is deployment work:

**UMS has no login of its own.** There is no password, session, cookie, or token
login anywhere in the backend (`db/security_models.py:38-78` — `UserORM` has no
password column; no login route exists). Every request's identity arrives as
*gateway-asserted headers* (`X-User-ID`, `X-Role`, `X-Scope-Type`, plus a shared
`X-UMS-Trusted-Gateway-Token`), and `/session/me` merely reflects what the caller
asserted. In development, the Vite dev proxy plays that gateway and injects a
fixed identity (`frontend/vite.config.ts:66-74`). **The compose stack ships no
gateway.** That is fine for a single-operator localhost beta and unacceptable the
moment a second person or a non-localhost address is involved.

Two beta shapes follow from that, described concretely in
[Two viable beta paths](#two-viable-beta-paths).

---

## Blockers

### B1 — No authentication front door; the compose stack has no gateway
`backend/ums_smart_revenue/api/dependencies.py:77-120` builds the full
`UserPrincipal` — id, email, **role**, scope — from caller-supplied headers, gated
only by one shared secret. `docker-compose.yml` defines `postgres`, `redis`,
`migrate`, `app`, `app-dev` — no authenticating proxy. `Docs/11_ACCESS_CONTROL_SECURITY.md:52-57`
already labels this "bootstrap/development mode".

**Consequence:** anyone who can reach the port *and* holds the token is whoever
they say they are, at whatever role they choose.
**For a localhost beta:** acceptable **only** because every published port binds to
`127.0.0.1` (`docker-compose.yml:41,56,98,125`). Keep that binding. Do not expose
the app to LAN, Tailscale, or a tunnel without first doing B2 *and* putting a real
authenticating proxy in front.

### B2 — The default authz mode lets a caller assert their own role
`UMS_AUTHZ_SOURCE` defaults to `headers` (`config/settings.py:27,87-91`;
`docker-compose.yml:23` passes `${UMS_AUTHZ_SOURCE:-headers}`). In that mode the
`X-Role` header *is* the role — `X-Role: super_owner` grants super-owner. The safer
mode exists and is wired: `database` loads the authoritative role and permissions
from SQL and takes only identity from the header (`app.py:332`;
`api/dependencies.py:181-189`).

**Fix:** set `UMS_AUTHZ_SOURCE=database` for the beta. This has a prerequisite —
see H1 (the roles/permissions seed) — which is why it is not a one-line change.

### B3 — Real revenue data has no backup, and a documented command destroys it
No backup mechanism exists anywhere in the repo: `pg_dump` appears **only** as
prose in `Docs/17_MULTI_TENANT_ARCHITECTURE.md` (a manual tenant-slice procedure) —
there is no script in `scripts/`, `ci/`, or the `Makefile`.
`Docs/01_IMPLEMENTATION_PLAN.md:1205` states it plainly: *"Backup/export retention —
remaining: not started."* Meanwhile `docker-compose.yml:7` documents
`docker compose down -v` as an ordinary teardown command — that deletes the
`postgres-data` volume and every revenue fact in it, unrecoverably.

**Fix before any real data is ingested:** a scheduled `pg_dump -Fc` writing to a
**host** directory (not a container volume), plus one rehearsed restore. Until that
exists, treat the beta database as disposable and re-importable.

### B4 — Export artifacts and connector blobs live on ephemeral container paths
Generated workbooks/PDFs/slide packs default to the container's temp directory
(`reports/artifact_storage.py:13` — `tempfile.gettempdir()/ums-smart-revenue-export-artifacts`),
and connector raw-file blobs default to `cwd/_local_blob_store`
(`connectors/runs/orchestrator.py:3125`), which resolves to `/srv/app/_local_blob_store`
(`Dockerfile:109`). `docker-compose.yml` declares only `postgres-data` and
`redis-data` volumes — **neither path is mounted**. Both are wiped by any rebuild,
`--force-recreate`, or `down`.

**Fix:** set `UMS_EXPORT_ARTIFACT_DIR` and `UMS_LOCAL_STORE_ROOT` to paths inside a
new named volume, and mount it on `app` (and `migrate`, if it ever writes there).

### B5 — There is no browser app in any non-dev path
`frontend/` has no Dockerfile; compose has no frontend service; the backend mounts
no static files. The **only** thing that injects gateway headers is the Vite *dev
server* proxy (`frontend/vite.config.ts` `server.proxy`) — there is no
`preview.proxy`, so a built bundle served statically gets 401 on every call
(`api/dependencies.py:96-99`). Two further gaps in that proxy: its route list omits
`/users` and `/org-units` (`vite.config.ts:13-32`), and no CORS middleware exists
anywhere in the backend, so a cross-origin bundle would fail preflight
(`client.ts:90` sets `X-UMS-Tenant`, a non-simple header).

**Fix (beta-grade):** run the Vite dev server as the beta UI — it is the only
working configuration — or put a small reverse proxy in front of a built bundle
that injects the same header set. The dev-server route is honest for a
single-operator beta and requires no new code.

---

## High

### H1 — `roles`/`permissions` need a seed file that nothing tells you to run
A fresh database has no role or permission rows, which makes
`UMS_AUTHZ_SOURCE=database` (B2) unusable. The fix ships in-repo and is idempotent:
`backend/ums_smart_revenue/db/security_seed.sql`. The defect is purely
discoverability — **no migration, Makefile target, compose service, or README line
runs or mentions it.** An operator following the README will never find it.

**Fix:** run it once after migrations (`psql -f .../security_seed.sql`), and add it
to the runbook — ideally as a compose one-shot service beside `migrate`.

### H2 — `org_units` has no write path at all
The holding/sector/company hierarchy has **no API, no CLI, and no SQL file**. The
only writer in the repo is `scripts/seed_demo_month.py`, which cannot be run
skeleton-only — it also injects fabricated revenue facts and a committed allocation
snapshot (`seed_demo_month.py:414-455,527,1037-1072`). Setting up a real org
hierarchy therefore requires hand-written SQL today.

**Impact on beta scope:** company/sector-scoped views and scoped attribution depend
on these rows. A beta can run flat (global scope only) without them.

### H3 — First-user creation has a silent identity footgun
`POST /users` is satisfiable in headers mode by asserting `MANAGE_USERS` via the
header alone (`api/users.py:337,660-666`), and `create_user` requires no actor row
(`auth/users.py:139-182`). **But the new user's id is generated server-side
(`auth/users.py:161`, `uuid4()`)** — it will not match the `X-User-ID` you asserted.
Under `UMS_AUTHZ_SOURCE=database`, you must then use *that returned id* as your
`X-User-ID`, or the principal lookup fails.

### H4 — Live Google ingestion has exactly one implemented secret backend
Connector credentials are stored as *references only* — never secret material —
which is the correct design (`db/security_models.py:388-398`;
`api/connectors.py:427-431`). But of the six accepted URI schemes
(`connectors/credentials.py:30-37`), only `gcp-secret-manager://` and its
`secret-manager://` alias have a registered resolver
(`connectors/google/secret_resolver.py:70-84`); the rest fail closed.
`local-secret://` is **convention-only** test scoping — it ships in the production
package and `register_resolver()` has no pytest guard or env gate
(`connectors/google/local_secret_resolver.py`; `secret_resolver.py:50-54`) — and
`Docs/19` explicitly forbids using it for owner credentials (`Docs/19:76-77`).

**Consequence:** live Google/CMS ingestion in the beta needs a real GCP Secret
Manager project reachable from this PC. **This does not block the beta** — see H5.

> *Provenance note:* the June 2026 live smoke is recorded in session memory as having
> used `local-secret://` with an untracked in-process wrapper. No repo artifact
> confirms or contradicts this, and no repo artifact records GCP Secret Manager ever
> being used. Treat the memory claim as unverified-by-code.

### H5 — Real revenue can be ingested with no Google dependency (this is the beta path)
`POST /revenue/facts` accepts `connector_key: "manual-upload"` with
`source_kind: "MANUAL_UPLOAD"` (`api/revenue.py:197-206,1016`) — no credential row,
no resolver, no Google call. `MANUAL_UPLOAD` is fully wired downstream, not a stub:
reconciliation source priority (`finance/reconciliation.py:13`), explanation label
(`finance/reconciliation_explanation.py:27`), TAX deduction policy
(`finance/deduction_policy.py:23-25`), and the DB CHECK constraint
(`db/finance_models.py:186`). The channel roster loads from CSV without Google too
(`api/channels.py:666-680`).

**Limitation:** `/revenue/facts` is **one fact per request** — there is no bulk
revenue CSV endpoint. A beta must script the loop.

---

## Medium

### M1 — Health checks never touch the database; `/readyz` is a 404
`/health` and `/livez` return a hardcoded payload — no DB session, no dependency
check (`app.py:204-212`). `/readyz` appears in the tenancy bypass list
(`tenancy/resolver.py:73-80`) implying it should exist, but **no route registers
it**. The compose healthcheck targets `/livez` (`docker-compose.yml:99-104`), so the
container reports healthy with a dead database.

### M2 — Crashed connector jobs stay `RUNNING` forever
`UMS_CONNECTOR_JOB_STALE_RUNNING_HOURS` (default 6) is stored but never swept:
`executor.py:230` sets it and nothing else reads it. The only consumer is
`_supersede_or_block_running_runs` (`api/connectors.py:786,1050`), which flips a
stale row to `FAILED` **only when someone submits a new job for the exact same
connector+account+month**. Kill the app mid-run and that row shows `RUNNING`
indefinitely. A startup sweep is the fix.

### M3 — Nothing is ever purged
Audit rows and raw report files are retain-forever by design
(`api/reports.py:240`); purge is a manual one-file-at-a-time action
(`api/reports.py:252`). `Docs/15` records retention as not started. Low urgency at
beta volume; matters for disk on a long-running single PC.

### M4 — Compose advertises three protections that do not exist
`UMS_CORS_ALLOWED_ORIGINS`, `UMS_RATE_LIMIT_PER_MINUTE`, and `REDIS_URL`
(`docker-compose.yml:25-28`) are read by **no backend code, no test, and no
library** — verified by two independent search methods. There is no CORS
middleware, no rate limiting of any kind, and Redis is unused while remaining a
hard `service_healthy` startup dependency.

This is not a discovery so much as **the last artifact not yet updated to match a
settled decision** — `Docs/16_OPEN_DECISIONS.md:35-38` already records Redis/Celery
as pre-provisioned and dead. Note that `Docs/17:275` still asserts a Redis tenant
cache exists; any cleanup must correct both, and dropping the `pyproject` pins is
gated by `tests/test_version_baseline.py:32-33`.

**Practical impact for a localhost beta:** none for CORS (the dev-server path is
same-origin) and none for Redis. The absence of rate limiting means a runaway
script can saturate the app; tenant lookups queue behind a
`BoundedSemaphore(8)` with a 5s timeout (`tenancy/resolver.py:112`), so the failure
mode is 503s, not shedding.

### M5 — The compose path is undocumented, and `deploy/` does not exist
`docker-compose.yml` hard-requires `UMS_DB_USER`, `UMS_DB_PASSWORD`,
`UMS_DB_PASSWORD_URLENC`, `UMS_DB_NAME` (`:22,37-39`) and
`UMS_TRUSTED_GATEWAY_TOKEN` (`:24`) via `:?` syntax — none of which appear in
`.env.example`, and the README's documented first-run path is local Postgres +
uvicorn, not compose. Separately, both `README.md:180` and `docker-compose.yml:17`
point at `deploy/helm/` — **that directory does not exist.**

---

## Verified sound

These were checked and found correct — no action needed:

- **Localhost binding**: every published port binds `127.0.0.1` (`docker-compose.yml:41,56,98,125`).
- **Gateway token handling**: constant-time comparison (`secrets.compare_digest`,
  `api/dependencies.py:287`), fails closed with 503 when unconfigured, and compose
  refuses to start without it.
- **Credential secrecy**: no secret material in the database; API exposes only
  `has_secret_ref: bool`; the credential-probe route deliberately returns fixed
  messages to avoid leaking DB ids or API URLs (`api/connectors.py:876-891`).
- **No secrets in the frontend bundle**: the gateway token is read Node-side only
  and never aliased under `VITE_*`.
- **No committed secrets**: independent sweep for private keys, vendor token
  shapes, and JWT-shaped strings found only pattern definitions and one obviously
  fake test fixture.
- **Migrations**: all 40 versions define `downgrade()` (two deliberately
  irreversible with explicit guards); the whole upgrade runs in one transaction
  (`db/alembic/env.py:79-83`); `app` refuses to start if `migrate` fails
  (`service_completed_successfully`).
- **Bootstrap tenant**: the `ums` tenant is seeded idempotently by migration
  (`20260516_0001_tenants_foundation.py:135-145`) — no manual step needed.
- **Background work is off by default**: executor and scheduler both default `False`
  (`config/settings.py:65,76`), and enabling the scheduler without the executor
  fails fast at boot.
- **RLS** is applied via `FORCE ROW LEVEL SECURITY` across the tenant tables.
- **Audit integrity**: an actor id that matches no user row is not silently
  fabricated — the FK is nulled and the asserted value preserved in
  `details.actor_user_id` (`auth/sql_audit_sink.py:63-75`).

---

## Two viable beta paths

### Path A — Single-operator beta (recommended first step)
The only user is the operator, on this PC, at `127.0.0.1`. No gateway is built;
the Vite dev server injects the fixed operator identity.

Required before real data: **B3** (backups) and **B4** (artifact volume). B1/B2 are
*accepted risks* documented in the runbook rather than fixed, justified solely by
the localhost binding. Revenue enters via **H5** (manual import), so H4/GCP is out
of scope.

Remaining work: backup script + restore rehearsal, artifact volume, a compose
`.env` template, the `security_seed.sql` step (H1), a first-user recipe (H3), and a
written runbook stating the trust boundary in plain words.

### Path B — Multi-user beta
Everything in Path A, **plus** a real authenticating front door: an OAuth2 proxy
(Google sign-in) that authenticates the human and injects identity headers, with
`UMS_AUTHZ_SOURCE=database` so roles come from SQL and cannot be self-asserted.
This makes the audit trail trustworthy. Also needs H2 (org units) if scoped access
is in scope, and a decision on H4 if live Google ingestion is wanted.

---

## Corrections from the adversarial pass

Recorded because they changed conclusions, and because the original wording would
have misled:

1. **"Live ingestion is impossible without GCP Secret Manager" — reversed.** True
   for *Google connectors*, but real revenue enters fine through manual import
   (H5). The beta is not blocked on GCP.
2. **"`roles`/`permissions` require hand-written SQL" — downgraded.** A maintained,
   idempotent seed file ships in-repo (H1); the defect is that nothing references
   it.
3. **"Six hard stops in first-run" — corrected to three** (`org_units`, the browser
   app, and the Google secret backend); two of the rest were documentation
   failures, not missing mechanisms.

---

## Evidence index

| Area | Primary evidence |
| --- | --- |
| Gateway-asserted identity | `api/dependencies.py:77-120,139-158,181-189` |
| Authz mode default | `config/settings.py:27,87-91`; `docker-compose.yml:23` |
| No local auth | `db/security_models.py:38-78` (no password column) |
| Backups absent | `Docs/01_IMPLEMENTATION_PLAN.md:1205`; `docker-compose.yml:7` |
| Ephemeral artifacts | `reports/artifact_storage.py:13`; `orchestrator.py:3125`; `Dockerfile:109` |
| Frontend serving | `frontend/vite.config.ts` (`server.proxy` only, routes `:13-32`) |
| Roles seed | `backend/ums_smart_revenue/db/security_seed.sql` |
| Manual revenue import | `api/revenue.py:197-206,1016`; `finance/revenue_facts.py:32` |
| Secret resolvers | `connectors/credentials.py:30-37`; `connectors/google/secret_resolver.py:70-84` |
| Dead compose config | `docker-compose.yml:25-28`; `Docs/16_OPEN_DECISIONS.md:35-38` |
| Localhost binding | `docker-compose.yml:41,56,98,125` |
