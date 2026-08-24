# 20 — Deployment Readiness Audit (First Beta, Single PC)

**Audited:** 2026-08-24, at `main` = `d8418cea2`, in two rounds.
**Target deployment:** first beta on one Windows PC via `docker compose`, **bound to
localhost only**, operator-run, with **real** YouTube CMS revenue data.
**Method:** read-only code audit. Every finding carries `file:line` evidence, and
**every round was re-checked by an independent adversarial pass** whose job was to
refute it. Those passes reversed or downgraded findings in both rounds; the
corrections are recorded rather than quietly dropped.

- **Round 1 — deployment surface:** auth, secrets, data lifecycle, bootstrap, config.
- **Round 2 — deep audit:** PC/host lifecycle, frontend completeness, observability,
  performance at scale, **data correctness**, and failure modes.

> **Scope note.** This audit answers one question: *what stands between the current
> `main` and a first beta on this PC?* Round 1 was about **running** it. Round 2
> asked the harder question — *if it runs, are the numbers right, and would anyone
> notice if they weren't?* — and the answer changed the verdict.

---

## Verdict

**Round 1 concluded "feature-ready, deployment not ready." Round 2 overturns that.**
There is a correctness defect on the live revenue path that matters more than every
deployment gap combined, and it is specific to this tenant's data:

### 🔴 The single most important finding: revenue currency is fabricated

`connectors/google_source_parsers/youtube_analytics.py:119-126`:

```python
def _analytics_currency(request: dict[str, object]) -> str:
    """Return the requested currency, defaulting omitted Analytics currency to USD."""
    if "currency" not in request:
        return "USD"
```

The currency is read from **our own outgoing request**, never from Google's
response — and `connectors/google/youtube_analytics_client.py` contains **zero**
occurrences of `currency`, so the request never carries one. The branch is therefore
unconditional: **every YouTube Analytics revenue row is stamped `USD`.**

This is not theoretical for this deployment. The content owner's YouTube Analytics
revenue is reported **natively in EGP**. Under the current code those EGP amounts are
recorded as USD — the number is preserved, the unit is wrong, and nothing downstream
can detect it because the currency was never observed in the first place.

It also breaks two written contracts:
`Docs/05_CONNECTORS_YOUTUBE_ADSENSE.md:99-100` (*"the connector must preserve
Google's reported currency … it must not convert the amount"*) and
`Docs/18_MULTI_CURRENCY_ENGINE.md:181-182` (*"still store the currency returned by
Google"*).

**This must be fixed before any live connector ingest.** It does not block a beta
that uses the manual import path (H5), where the operator supplies the figures.

> The adversarial pass tried hardest to refute exactly this finding and could not:
> the CSV-parser half of the original claim *was* overstated (that path does read a
> real currency header and fails closed), which makes the Analytics half stand out
> as the genuine defect rather than a blanket accusation.

### The deployment verdict (Round 1) still holds

**Five blockers**, none requiring redesign — the most important is architectural
even though its fix is deployment work:

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
`POST /revenue/facts` accepts a `connector_key` of `manual-upload` (or
`manual_upload`) with `source_kind` `MANUAL_UPLOAD`
(`api/revenue.py:197-206,1016`) — no credential row,
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

### Path A — Single-operator beta, manual import (recommended first step)
The only user is the operator, on this PC, at `127.0.0.1`. No gateway is built; the
Vite dev server injects the fixed operator identity. **Revenue enters by manual
import, not by connector** — which sidesteps the currency defect entirely, because
the operator supplies the figures rather than Google's unread currency.

Required before real data: **B3** (backups), **B4** (artifact volume — note the
permanent-503 consequence), and the **logging fix** (one `basicConfig` call, without
which nothing is diagnosable). B1/B2 are *accepted risks* documented in the runbook
rather than fixed, justified solely by the localhost binding.

Also needed: compose `.env` template, the `security_seed.sql` step (H1), a first-user
recipe (H3), a reboot runbook (nothing restarts itself), and a written note that a
connector-only month cannot be locked.

### Path A+ — Path A plus live connector ingest
**Blocked on the currency fix.** Everything in Path A, plus: repair
`_analytics_currency` to record the currency Google actually returned, decide what
happens to non-USD amounts in the pipeline (today they are variously skipped, which
overstates net), add the connector-job startup sweep, and pass the worker env vars
through compose.

### Path B — Multi-user beta
Everything in Path A, **plus** a real authenticating front door: an OAuth2 proxy
(Google sign-in) that authenticates the human and injects identity headers, with
`UMS_AUTHZ_SOURCE=database` so roles come from SQL and cannot be self-asserted.
This makes the audit trail trustworthy. Also needs H2 (org units) if scoped access
is in scope, and a decision on H4 if live Google ingestion is wanted.

---

## Round 2 — deep audit

Round 1 audited whether the system would *run*. Round 2 audited whether it would be
*right*, and whether anyone would know when it wasn't. Findings are grouped by the
question they answer. Severities are the **post-refutation** ones.

### Are the numbers right?

- **🔴 CRITICAL — Analytics revenue currency is fabricated as USD.** See the Verdict.
  `google_source_parsers/youtube_analytics.py:119-126`.
- **HIGH — AdSense earnings rows never become revenue facts.**
  `connectors/google/adsense_management.py:218` sets `youtube_channel_id=None`, and
  the normalizer keeps only `estimatedRevenue`
  (`google_source_normalizer.py:84-88`). *Corrected from the round-2 draft's "100%
  discarded":* AdSense **payments** are a separate, fully-wired path
  (`api/adsense.py:134` → `finance/adsense_payments.py`) and do reach payment
  matching, bank reconciliation, and the gap narrative. The accurate claim is
  narrower: **no settled AdSense figure ever replaces an estimate in
  `monthly_channel_revenue_facts`.**
- **HIGH — the reconciliation-derived TAX component is structurally always zero.**
  `finance/reconciliation_workflow.py:39-45,123-132`. *Corrected:* real TAX/DEDUCTION
  components written by the CLI **do** reduce net (`NET_APPLICABLE_COMPONENT_KINDS`);
  only the workflow-derived value is fixed at 0.
- **HIGH — deductions have no write API.** The read endpoint
  (`api/revenue.py:2041-2044`) is read-only; components exist only if the operator
  remembers to run the ingestion CLI. Forget it, and net silently equals gross.
- **HIGH — the confidence cap is a no-op.** `finance/explanations.py:498-503` clamps a
  warned score to exactly `0.9000`, then labels `HIGH` when `score >= 0.9000`. A fact
  carrying warnings is label-indistinguishable from a clean one — the only mechanism
  intended to degrade confidence cannot change the label.
- **MEDIUM — month gross and month net are summed over different channel sets**
  (`finance/net_revenue.py`), so the two headline figures need not describe the same
  population.
- **MEDIUM — non-USD tax/deduction rows are silently skipped**
  (`finance/deduction_components.py:108-110`), which **overstates** net.
- **MEDIUM — the currency selector is inert, and offers currencies the pipeline
  rejects.** `AppShell.tsx:629-633` renders USD/EGP/AED with an uncontrolled
  `defaultValue` and no `onChange`, in a pipeline that skips non-USD source rows and
  non-USD deductions and hard-fails non-USD exports.
- **MEDIUM — split-brain confidence:** `finance/reconciliation.py:139-141` halves a
  single-source channel's preview score to 0.5 while `explanations.py:498` reports
  the stored `1.0` (HIGH) for the same channel-month. Both ship.
- **MEDIUM — a test-fixture column alias is live in the production CSV path.**
  `connectors/runs/orchestrator.py:2856-2867` accepts `ad_revenue` (documented in its
  own comment as *"the test-fixture shorthand"*) and relabels it as
  `estimatedRevenue`. Gross ad revenue is a different base from partner revenue;
  there is no prod/test gate.

### Would anyone notice a problem?

- **HIGH — there is no logging configuration at all.** No `logging.basicConfig`, no
  handler, no dictConfig anywhere in the backend — so **every `logger.*` call in the
  application is discarded before it is written.** All the careful exception handling
  writes to a logger with no handler. This is the highest-value single fix in the
  audit: one `basicConfig` call turns the whole codebase's diagnostics on.
- **MEDIUM — a connector run can report success while the month's facts were never
  rewritten**, and the only evidence is one of those dropped INFO lines.
- **MEDIUM — nothing reports whether the background workers are running**, and a
  scheduler that never ticks is indistinguishable from one ticking cleanly.
- **MEDIUM — alerts are pull-only**, fetched once on navigation; no polling, no push.
- **MEDIUM — `request_id` is a column, a repository field, and a CSV column that is
  never populated.**
- **MEDIUM — container stdout is the only log sink**, and a routine `docker compose
  up --force-recreate` destroys it.

### Does it survive this PC?

- **HIGH — a connector job killed mid-run 409-blocks that month for six hours.**
  `executor.close()` uses `shutdown(wait=False, cancel_futures=True)`
  (`executor.py:255`); Docker's default 10s stop grace then SIGKILLs the process
  while a Google pull can be in a 64s retry backoff. The transaction rolls back but
  `connector_runs.status` stays `RUNNING`, and resubmitting the same scope returns
  `409 duplicate_in_flight` until the row ages past `stale_running_hours` (default 6).
  **There is no startup sweep** — the lifespan prologue is empty (`app.py:132-137`).
  Sleep the PC mid-ingest and next morning that month refuses to re-run.
- **HIGH — nothing brings the stack back after a Windows Update reboot.**
  `restart: unless-stopped` only acts once the Docker daemon runs, and Docker Desktop
  starts at **user login**, not at boot — after an unattended 3am reboot the PC sits
  at the lock screen with nothing running. `app-dev` has no `restart` key at all
  (`docker-compose.yml:109-138`), and the beta UI (the Vite dev server) is a host
  process outside compose entirely. **`README.md` does not contain the string
  "docker" anywhere**, and nothing in the repo mentions Docker Desktop or WSL2.
- **MEDIUM — the group-sync scheduler can never fire on a PC that restarts daily.**
  The first tick fires one full interval (default 24h) *after* start
  (`scheduler.py:184-190`), and the scheduler persists nothing — no last-run
  timestamp exists — so every restart resets the timer and every missed window is
  skipped with no catch-up.
- **MEDIUM — compose passes none of the background-worker or storage env vars.**
  `x-app-env` omits `UMS_CONNECTOR_JOB_EXECUTOR_ENABLED`,
  `UMS_GROUP_SYNC_SCHEDULE_ENABLED`, `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`, and the
  storage roots — so the compose stack **cannot run a connector pull at all** without
  editing the compose file.
- **MEDIUM — no log rotation is configured on any service**, so container logs grow
  unbounded on the host disk.
- **MEDIUM — the 10s scheduler close budget equals Docker's entire stop grace**, so
  the shutdown audit of queued jobs often never runs. Set `stop_grace_period: 120s`.

### Will it hold up at real scale?

- **HIGH — after any container recreate, every COMPLETED export job returns a
  permanent 503.** Artifacts default to container `/tmp` with no volume, and
  `api/exports.py:1226-1230` deliberately does *not* rebuild a missing file — the
  storage error propagates. This is strictly worse than B4 described: not a re-paid
  build cost, a permanently broken download.
- **MEDIUM — `audit_logs` lacks an index supporting its own list query**
  (`(tenant_id, created_at DESC, id DESC)`), and nothing is ever purged.
- **MEDIUM — exports build inline in the request with no timeout at any layer.**
- **MEDIUM — both database lanes share one engine** with default pool sizing.
- **LOW — one genuine N+1** on the net-revenue path
  (`finance/allocation_inputs.py:118-123`, twin at `committed_allocation.py:450-455`);
  bounded by distinct AdSense accounts, so small at this scale.

### What can a beta user actually do in the browser?

- **The whole product is a single page.** There is no router — `package.json` lists
  only `react`, `react-dom`, and `tw-animate-css` — and `AppShell` switches views
  from component state (`const [view, setView] = useState<ViewKey>("command")`).
  One URL, no deep links, no bookmarking a month or channel, browser Back exits the
  app, and a refresh always returns to the Command view discarding in-view state.
- **HIGH — only four months exist in the entire UI, all in the past**, hardcoded.
- **HIGH — the most prominent status band shows fabricated numbers, unlabelled.**
- **HIGH — there is no error boundary**: a render throw blanks the whole app.
- **HIGH — six residual mock panels still render** alongside real ones, with nothing
  distinguishing them to the viewer.
- **HIGH — connector credentials, org units, and user management have no browser
  surface at all** — they are `curl`-only (org units are not even that; see H2).
- Net effect: the browser is a **read-mostly dashboard**. Every setup action and
  every write outside a few flows happens by API call or SQL.

### Ranked: most likely to bite in the first week

1. An ordinary database interruption becomes an unexplained HTTP 500 with no log
   line explaining it (because of the logging finding).
2. The container reports healthy while the database is dead (`/livez` checks nothing).
3. A partially-completed manual import leaves a month that looks complete but isn't.
4. A connector run interrupted by a reboot refuses to re-run for six hours.
5. Slow burn: nothing is purged, nothing is vacuumed, no log rotation.

---

## Round 3 — "the buttons don't work and it's a mockup"

Reported from hands-on use, then traced to root cause. All three observations are
correct, and they have three *different* causes — one of which is a one-line fix.

### Why no button works: the shipped dev identity has 2 of 28 permissions

The buttons are **not** unwired. Of 43 `<button>` elements across the UI, **39 carry
a handler**; the 4 that don't are the global chrome controls already recorded as
dead. They fail at the API, not in the DOM.

`frontend/vite.config.ts` injects a fixed dev identity, and its default role is
`assistant_analyst`:

```
["X-Role", "VITE_DEV_GATEWAY_ROLE", "assistant_analyst"],
```

`auth/seed.py` grants that role exactly two permissions — `VIEW_ANALYTICS` and
`VIEW_CONFIDENCE` — out of 28 defined. It is the second-weakest role in the system;
only `AUDIT_VIEWER` has fewer. For comparison: `FINANCE_ADMIN` has 15,
`CORPORATE_ADMIN` 12, `DATA_STEWARD` 5.

So out of the box every write action (import, sync, run connector, lock month,
export, manage users) is denied, and so is every read gated on `VIEW_REVENUE`,
`VIEW_FINALIZED_PAYMENTS`, or `VIEW_RAW_FILES`. **The product is being demonstrated
by its most restricted role.**

**Fix:** set `VITE_DEV_GATEWAY_ROLE` in `frontend/.env` to a role that can actually
operate — `finance_admin` for the finance surface, `corporate_admin` for setup. One
line, no code change. This should be the first thing any beta runbook says, and its
absence from the README is arguably the single highest-impact documentation gap in
this audit.

### Why the data looks fake: because some of it is

Two independent sources of fabricated numbers:

1. **`lib/mock/data.ts`** — 277 lines, 31 exported datasets, imported by 12 of 13 UI
   modules. What is still rendered from it:
   - `AppShell` — `NAV_GROUPS`, `VIEW_COPY`, `WORKFLOW_STEPS`: the navigation, the
     view descriptions, and the workflow stepper. **The application chrome itself is
     mock.**
   - `CommandView` (the landing screen) — `CLOSE_STEPS`, `EXPORT_READINESS`,
     `ISSUES`.
   - `RegistryView` — `REGISTRY_SUMMARY`, `REGISTRY_CONTROLS`;
     `CloseView` — `RECON_NOTES`; `ExportsView` — `EXPORTS_GUARDRAILS`.

   Good news in the same measurement: `ConnectorsView`, `GroupsView`,
   `GroupsSyncFlow`, `RegistryImportFlow`, `TraceView`, and all three Audit modules
   import **zero** mock symbols and run entirely on API hooks. And roughly nine mock
   datasets (`CHANNELS`, `KPIS`, `CLOSE_SUMMARY`, `CONNECTOR_HEALTH`,
   `CONNECTOR_JOBS`, `EXPORTS_ROWS`, `REGISTRY_ROWS`, `TRACE_*`) are now dead code —
   real data replaced them. The migration is genuinely most of the way done; what
   remains is concentrated in the chrome and the summary tiles, which is exactly the
   part a visitor sees first.

2. **`scripts/seed_demo_month.py`** — writes a complete fabricated finance month:
   org units, channels, revenue facts, and a committed allocation snapshot. It also
   ships `--demo-lock-bypass`, which flips a month to locked **writing no audit event
   and bypassing the production readiness gate** (its own docstring says so). If that
   was ever run against the working database, the money in it is invented.

### Why it feels like a landing page

Compounding: there is no router (one URL, views held in component state), the chrome
and the first screen are mock, only four hardcoded months exist, and the role you are
given cannot exercise the parts that *are* real. Each finding alone is modest; stacked,
they produce exactly the reported impression — **a polished landing page over a real
engine you are not allowed to reach.**

### Where these observations came from, and why it matters

The hands-on session that produced them ran on the operator's **Mac**, not on the
target PC. On the PC, `docker volume ls` shows no `ums-smart-revenue` volumes — the
compose stack has never been started on the machine this audit targets.

That split matters when reading the rest of this document:

- **Platform-independent findings still apply exactly.** The role/permission cause of
  the dead buttons, the mock chrome and panels, the missing router, the hardcoded
  months, and every data-correctness finding are code-level. They reproduce anywhere.
- **The host-lifecycle findings are Windows/PC-specific and are NOT what was
  observed.** Docker Desktop starting at user login rather than boot, the WSL2 bind
  mount, and the reboot-recovery gap describe the *target* environment, and remain
  unverified on real hardware — nobody has yet run the compose stack on the PC at all.

**Consequence for planning:** the first beta would be the first time this stack runs
on its target machine. The runbook work in Path A is therefore not just documentation
— it is the first real rehearsal, and it should be treated as one.

---

## Round 2 — claims the adversarial pass killed

Recorded so they are not re-raised. Each was a confident round-2 finding that did
**not** survive verification:

1. **"You can close a month whose data is incomplete" — refuted.** The lock is
   hard-blocked by a lock-time readiness recheck (`finance/month_close.py:117-118`),
   and in a connector-only month every channel has exactly one fact →
   `INSUFFICIENT_SOURCES` → HIGH blocker → 409. The *date-blindness* of the gate is
   real but currently unreachable. **The operational surprise is the opposite of the
   one first reported: in a connector-only beta a month cannot be locked at all.**
2. **"A manual correction is silently ignored" — refuted.** A >2% spread raises a
   VARIANCE issue that blocks close (`finance/reconciliation.py:129-134`). It does
   not move the headline number, which is a documentation matter, not silence.
3. **"`record_fact` has a read-then-write race" — refuted.** Every writer serializes
   on a `pg_advisory_xact_lock` plus `SELECT … FOR UPDATE` taken before the read
   (`revenue_facts.py:98` → `month_close_locks.py:74-80`).
4. **"AdSense revenue is 100% discarded" — overstated**, see above.
5. **"No N+1 anywhere in the heavy paths" — false**; the audit's own denial was its
   biggest error (see the LOW N+1 above).

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
