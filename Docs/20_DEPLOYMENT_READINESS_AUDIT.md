# 20 — Deployment Readiness Audit (First Beta, Single PC)

**Audited:** 2026-08-24 – 2026-08-25, at `main` = `d8418cea2`, in five rounds.
**Target deployment:** first beta on one Windows PC via `docker compose`, **bound to
localhost only**, operator-run, with **real** YouTube CMS revenue data.
**Method:** rounds 1–4 were a read-only code audit; round 5 executed. Every finding
carries `file:line` evidence, and **every round was re-checked by an independent
adversarial pass** whose job was to refute it. Those passes reversed or downgraded
findings in every round; the corrections are recorded rather than quietly dropped.

> ⚠️ **Freshness banner (2026-08-28).** P0 **execution** and the living Docs/21 status
> table live on PR **#210** (`feat/beta-p0-durability`). This document is the
> **pre-execution snapshot** at `main` = `d8418cea2`. Do **not** schedule unchecked
> open items from this text alone — B0 (Postgres 18 `PGDATA`) and most of W0.2 /
> P0.1–P0.9 are already done on #210. For scheduling work, use Docs/21 as maintained
> on #210.

- **Round 1 — deployment surface:** auth, secrets, data lifecycle, bootstrap, config.
- **Round 2 — deep audit:** PC/host lifecycle, frontend completeness, observability,
  performance at scale, **data correctness**, and failure modes.
- **Round 3 — hands-on:** the operator's own report that no button worked, the data
  looked fake, and the app read as a landing-page mockup. All three traced to cause.
- **Round 4 — implementation scoping:** every finding costed against the code before
  planning. **This round retracted Round 2's headline CRITICAL** and corrected four
  other published claims.
- **Round 5 — execution.** The plan's W0.2 and P0.1–P0.5 items were implemented and
  the stack was **started for the first time**. That single act produced the largest
  finding in the whole audit — see [B0](#b0--the-compose-stack-could-never-have-started-resolved).
  Rounds 1–4 all described the stack as *unrehearsed*; it was in fact **broken**, and
  four rounds of reading could not have found it.

> **Scope note.** This audit answers one question: *what stands between the current
> `main` and a first beta on this PC?* Round 1 was about **running** it. Round 2
> asked the harder question — *if it runs, are the numbers right, and would anyone
> notice if they weren't?*
>
> **Read the Round 4 corrections before acting on any Round 2 finding.** Costing the
> fixes turned out to be the most effective adversarial pass of the four: an
> estimate forces you to name the exact line you would change, and two findings did
> not survive that.

**The plan built from this audit is [`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md).**

### Related plans (program triad)

| Doc | PR | Role |
| --- | --- | --- |
| Docs/21 (living status) | [#210](https://github.com/a7ttaf/Youtube/pull/210) | P0 execution + current schedule source of truth |
| Docs/22 backup rehearsal | [#210](https://github.com/a7ttaf/Youtube/pull/210) | Backup/restore runbook shipped with P0 |
| Docs/23 Admin access plan | [#218](https://github.com/a7ttaf/Youtube/pull/218) | Admin / access / config UI (Docs/21 is silent here) |
| Docs/24 US withholding plan | [#219](https://github.com/a7ttaf/Youtube/pull/219) | US revenue slice + withholding estimate (fills P3 rate gap) |

**Residual proxy note:** after #210 adds `/org-units` and `/users`, `/security` is
still missing from `TENANT_SCOPED_ROUTES` — required by Docs/23 A2.

---

## Verdict

**Round 1 concluded "feature-ready, deployment not ready." That verdict stands.**
Round 2 published a CRITICAL correctness finding on top of it; Round 4 scoping
**retracted that finding as wrong**. See the retraction immediately below — it is
kept in place rather than deleted, because it was acted on and circulated.

### ✅ RETRACTED — "revenue currency is fabricated" was incorrect

**What Round 2 claimed:** that `_analytics_currency`
(`connectors/google_source_parsers/youtube_analytics.py:119-126`) stamps `USD` on
revenue the content owner actually earns in EGP, corrupting the unit on every live
Analytics row.

**Why that is wrong.** `currency` on the YouTube Analytics `reports.query` endpoint
is a **request parameter that selects the output currency**, not a field describing
what the account natively earns. Omit it and Google *returns* USD; send `EGP` and
Google *converts server-side and returns* EGP. Three independent pieces of evidence
in this repo:

1. **Google's response carries no currency field at all.** The recorded fixture
   `tests/connectors/_fixtures/youtube_analytics/sample_query_response_2026_04.json`
   has top-level keys `query_request`, `kind`, `columnHeaders`, `rows`. Its only
   `"currency": "USD"` lives inside `query_request` — *our own echoed request*.
   `columnHeaders` entries carry `name`/`columnType`/`dataType` and nothing else.
   "Read the currency Google returned" is therefore not a thing the code could do.
2. **The repo's own test documents the default.**
   `tests/connectors/google_source_parsers/test_youtube_analytics_parser.py`,
   `test_missing_currency_defaults_to_usd`: *"`currency` is an optional
   reports.query request parameter; the YouTube Analytics API defaults financial
   metrics to USD when it is omitted."*
3. **The live smoke figures are USD-shaped.** The 2026-06-22 live run returned
   ≈$79,057.76 across 25 channels. EGP would be roughly 47× larger.

So the label matches what Google actually sent. **No data is mis-denominated, and
nothing needs re-ingesting or migrating.**

**Root cause of the error:** a note reading "currency=EGP is native" was read as
"this account reports EGP by default." It actually meant the *parameter* is accepted
for this content owner. Round 2's adversarial pass did not catch it because it
attacked the CSV half of the claim and left the Analytics half standing.

### 🟡 What is actually true, at the right severity

**D1 — MEDIUM: the USD label is assumed, not asserted.** The outgoing request never
sends `currency`, so the correct label depends on an undocumented Google default
holding forever. `Docs/05:99-100` and `Docs/18:181-182` both ask for the currency to
be explicit. Fix: send `"currency": "USD"` in `_build_query_request`
(`youtube_analytics_client.py:108-115`). **Byte-identical downstream** — the parser
returns `"USD"` either way, so `source_row_key` hashes are unchanged. ~2h.

**D1b — same class, second site.** `connectors/runs/orchestrator.py:205-210` maps
`content_owner_estimated_revenue_a1 → "USD"` when a Reporting bulk CSV has no
currency column (applied at `:2756-2758`), failing closed only for report types
*absent* from that dict. Round 2's adversarial pass cleared the CSV path as
"fails closed" — that was half right. The default is documented in its own comment
and is consistent with Google's Reporting schema, so this is an assumption to make
explicit, **not** a fabrication.

**D2 — the real commercial gap: UMS cannot represent EGP at all.** There is no
currency column on the finance path and ~2,154 `*_usd` identifiers; USD-only is a
*designed, test-locked* property, not an oversight. If settlement and banking are in
EGP, that gap is real — and it is **3–6 weeks**, not a bug fix.

**D3 — it is a recorded open decision, not an accident.**
`Docs/16_OPEN_DECISIONS.md:70-71` has never been answered. The one question that
sizes everything above: *"USD facts, with the EGP bank settlement explained as FX
variance — acceptable for the beta, yes or no?"* The code has assumed "yes" since
PR #42 without anyone saying so.

**None of D1/D1b/D2 blocks the recommended beta**, which uses manual import (H5) and
never calls Google.

### The deployment verdict (Round 1) still holds — and Round 5 made it stronger

Round 5 added a sixth blocker, [B0](#b0--the-compose-stack-could-never-have-started-resolved),
which is more severe than any of the original five and which **no amount of reading
would have found**. `docker compose up` had never been run against this file. When it
finally was, Postgres restart-looped and every dependent service was blocked. The
honest restatement of the Round 1 verdict is therefore not "feature-ready, deployment
not ready" but **"feature-ready, deployment never once executed."**

**Six blockers**, none requiring redesign — the most important of the original five is
architectural even though its fix is deployment work:

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

### B0 — The compose stack could never have started (RESOLVED)

*Numbered `0` because it was found last and precedes all the others causally: while it
stood, B1–B5 were unreachable — there was no running stack to have a gateway in front
of, no database to back up, and no container to mount an artifact volume on.*

**Found:** Round 5, on the first `docker compose up` ever run against this file.
**Severity:** blocker, and strictly worse than anything rounds 1–4 published.
**Status:** fixed and verified — `docker-compose.yml:226`, rationale at `:198-225`.

`postgres:18-alpine` sets `PGDATA=/var/lib/postgresql/18/docker` and declares its own
`VOLUME` at the **parent** path. `docker-compose.yml` mounted the named volume at the
pre-18 path `/var/lib/postgresql/data`. Postgres 18 hard-errors on that unused mount
and restart-loops. Reproduced at the unmodified tree (`restarts=7`, `health=unhealthy`),
and the image's own log names the fix:

```text
umslanea-postgres-1  restarting  Restarting (1) 4 seconds ago
postgres-1 | Counter to that, there appears to be PostgreSQL data in:
postgres-1 |   /var/lib/postgresql/data (unused mount/volume)
postgres-1 | The suggested container configuration for 18+ is to place a single mount
postgres-1 | at /var/lib/postgresql which will then place PostgreSQL data in a
postgres-1 | subdirectory ...
```

```text
$ docker image inspect postgres:18-alpine --format '{{json .Config.Env}}'
[..., "PG_MAJOR=18", "PG_VERSION=18.4", ..., "PGDATA=/var/lib/postgresql/18/docker"]
$ docker image inspect postgres:18-alpine --format '{{json .Config.Volumes}}'
{"/var/lib/postgresql":{}}
```

**Two distinct failures, not one.** The visible one is that nothing started. The
invisible one is worse: had Postgres started anyway, `PGDATA` would have resolved
*outside* the mounted volume, so the entire database would have lived in the
container's writable layer and been destroyed by the next `--build`, `--force-recreate`
or `down`. A stack in that state looks healthy and loses everything on a routine
rebuild.

The failure trips on a **fresh, empty** volume — the check fires on the legacy path
merely *being* a mountpoint — so this was never a stale-data problem and there is no
migration path to write.

#### Operator note: existing volumes and fresh stacks

Pulling the mount fix is safe for a stack that already ran the broken file, and the
reason is mechanical, not hopeful. Every revision of `docker-compose.yml` in this
repository's history that shipped the legacy mount already shipped a Postgres **18**
image (`31620750e`, the first compose stack here, was already `postgres:18-alpine`),
and the 18 entrypoint exits 1 before `initdb` whenever the legacy path is a mount —
empty or not (`docker_error_old_databases`, reached from `_main` on every fresh
start). A `postgres-data` volume created under the broken config therefore cannot
contain a cluster. Per case:

- **Existing volume from the broken config — nothing to migrate, and this is the
  only case this repository can produce.** The entrypoint log names the empty
  mount: `/var/lib/postgresql/data (unused mount/volume)`. Do nothing but pull the
  fix: the new mount ignores the legacy path, `initdb` runs into
  `/var/lib/postgresql/18/docker` inside the same volume, and the data is durable
  from the first `up` on. A clean slate is exactly equivalent, if preferred:

  ```bash
  docker compose down && docker volume rm ums-smart-revenue_postgres-data && docker compose up -d
  ```

- **Existing volume holding a real cluster — impossible from this repository's
  history; reachable only via a hand-run container or a pre-18 image.** The
  entrypoint then names a bare directory *without* the `(unused mount/volume)`
  marker and keeps refusing until the cluster is migrated or discarded. Data here
  is disposable pre-alpha (Docs/22), so discarding is the documented default
  (`docker volume rm ums-smart-revenue_postgres-data`, then `up -d`). To keep the
  data instead, go through this repository's own backup/restore scripts — a raw
  `pg_dump` is NOT enough, because it does not carry the cluster-global
  `app_tenant` / `app_platform` roles, their memberships or the RLS surface the
  application `SET ROLE`s into; the backup's `roles.sql` does. The legacy volume
  is therefore dumped and then discarded — never moved in place: a pre-18
  cluster created at the legacy mount path occupies the volume ROOT, so any
  in-place move would land it where the fresh-stack entrypoint finds and refuses
  it again (`UMS_DB_USER` / `UMS_DB_NAME` / `UMS_DB_PASSWORD_URLENC` come from
  your `.env`; the temporary container uses the OLD major image at the legacy
  mount path):

  ```bash
  # 1. boot the old cluster once more, from the OLD major image at the legacy path
  docker run -d --name ums-pre18-migrate \
    -v ums-smart-revenue_postgres-data:/var/lib/postgresql/data \
    -e POSTGRES_USER="$UMS_DB_USER" -e POSTGRES_DB="$UMS_DB_NAME" \
    -e POSTGRES_PASSWORD="$UMS_DB_PASSWORD_URLENC" \
    postgres:16-alpine
  # 2. back it up with this repo's script: database.dump AND roles.sql
  #    (roles, memberships, RLS policies, object owners) — the first run exits 8
  #    and prints the row counts; re-run adding --establish-watermark as told
  python scripts/backup_database.py --container ums-pre18-migrate --out-dir D:/UMS-Backups
  # 3. the run directory now holds everything: discard the legacy volume
  docker rm -f ums-pre18-migrate
  docker volume rm ums-smart-revenue_postgres-data
  # 4. boot the fresh 18 stack and restore the run directory into it
  docker compose up -d postgres
  python scripts/restore_database.py --backup-dir D:/UMS-Backups/ums-backup-<stamp>Z \
    --container ums-smart-revenue-postgres-1
  docker compose up -d
  ```

  If the data matters, rehearse first: `--rehearse` restores the same run
  directory into a throwaway container and verifies every table against the
  manifest before anything real is touched
  ([22_BACKUP_RESTORE_AND_REHEARSAL.md](22_BACKUP_RESTORE_AND_REHEARSAL.md)).

- **Fresh stack.** No volume exists yet: compose creates `postgres-data`, mounts it
  at `/var/lib/postgresql`, and `initdb` writes `/var/lib/postgresql/18/docker`
  inside it — which is what the "Data survives a container replacement" row of the
  verification table below measures.

#### What is now verified (this is measured, not assumed)

| Claim | Evidence |
|---|---|
| The whole stack comes up | `up -d --build --wait` → `postgres/redis/app` **Healthy**, `migrate` **Exited (0)** |
| `PGDATA` is inside the volume | `SHOW data_directory` = `/var/lib/postgresql/18/docker`; `/proc/mounts` shows the volume at `/var/lib/postgresql` |
| Data survives a **container replacement** | row written, `docker compose down` (no `-v`), `up` → different container id, row returns carrying its **original** `written_at`; `alembic upgrade head` re-ran and exited 0 |
| The fix does not merely mask `-v` | `down -v` → volumes removed, fresh `initdb`, full migration chain, 38 tables, `app_tenant` + `app_platform` present |
| The `app` mount works | `/var/lib/ums`, `/var/lib/ums/artifacts`, `/var/lib/ums/blobs` all owned by uid 10001, write OK — so B4's "every export 503s" mode is genuinely closed |
| `redis` has no equivalent bug | `redis:7-alpine` declares `VOLUME /data` and compose mounts `redis-data:/data`. Correct as-is. |

An independent adversarial pass attacked the fix from five angles (fresh boot,
container replacement, `--force-recreate` + rebuild, `down -v`, and recycling the
volume left behind by the broken config) and it held every time. It also proved the
app *serves* rather than merely reporting healthy: from the host, `/livez` → 200 and
`/openapi.json` → 200 (201,966 bytes).

#### What this reframes

- **B3 was real but incomplete.** "No backup" understated it: there was also **nothing
  durable to back up**. Both halves are now closed —
  see [`22_BACKUP_RESTORE_AND_REHEARSAL.md`](22_BACKUP_RESTORE_AND_REHEARSAL.md).
- **Round 3's "the compose stack has never been started on this PC"** was recorded as
  an untested-environment caveat. It was not a caveat. It was the only thing hiding a
  blocker, and the phrasing implied the stack *would* have started. It would not have.
- **The general lesson**, worth more than the fix: four rounds of increasingly
  adversarial reading, including one round whose entire method was costing changes
  against exact lines, did not find this. Executing it found it in under a minute.

### B1 — No authentication front door; the compose stack has no gateway
`backend/ums_smart_revenue/api/dependencies.py:77-120` builds the full
`UserPrincipal` — id, email, **role**, scope — from caller-supplied headers, gated
only by one shared secret. `docker-compose.yml` defines `postgres`, `redis`,
`migrate`, `app`, `app-dev` — no authenticating proxy. `Docs/11_ACCESS_CONTROL_SECURITY.md:52-57`
already labels this "bootstrap/development mode".

**Consequence:** anyone who can reach the port *and* holds the token is whoever
they say they are, at whatever role they choose.
**For a localhost beta:** acceptable **only** because every published port binds to
`127.0.0.1` (`docker-compose.yml:196,240,284,333`). Keep that binding. Do not expose
the app to LAN, Tailscale, or a tunnel without first doing B2 *and* putting a real
authenticating proxy in front.

### B2 — The default authz mode lets a caller assert their own role
`UMS_AUTHZ_SOURCE` defaults to `headers` (`config/settings.py:27,87-91`;
`docker-compose.yml:87` passes `${UMS_AUTHZ_SOURCE:-headers}`). In that mode the
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
remaining: not started."* Meanwhile `docker-compose.yml:7` (as it stood at
`d8418cea2`) documented `docker compose down -v` as an ordinary teardown command —
that deletes the `postgres-data` volume and every revenue fact in it, unrecoverably.

**Fix before any real data is ingested:** a scheduled `pg_dump -Fc` writing to a
**host** directory (not a container volume), plus one rehearsed restore. Until that
exists, treat the beta database as disposable and re-importable.

> **Correction (Round 5) — this finding was true but understated.** "There is no
> backup" assumed there was something durable to back up. There was not: while
> [B0](#b0--the-compose-stack-could-never-have-started-resolved) stood, the cluster
> would have lived in the container's writable layer. The two findings compound —
> a rebuild, not just `down -v`, would have destroyed the data, and no backup existed
> to recover from either.
>
> **Status: closed.** `scripts/backup_database.py` + `scripts/restore_database.py`
> ship with a rehearsed restore; the runbook is
> [`22_BACKUP_RESTORE_AND_REHEARSAL.md`](22_BACKUP_RESTORE_AND_REHEARSAL.md).
> `docker-compose.yml:10-22` now carries an explicit `down -v` warning instead of
> documenting it as ordinary teardown. Two limits are recorded rather than hidden:
> the `app-data` volume is **not** in the backup set, and the backup's content gate
> has a known-weak absolute floor (Docs/22, *What a green run does not guarantee*).

### B4 — Export artifacts and connector blobs live on ephemeral container paths
Generated workbooks/PDFs/slide packs default to the container's temp directory
(`reports/artifact_storage.py:13` — `tempfile.gettempdir()/ums-smart-revenue-export-artifacts`),
and connector raw-file blobs default to `cwd/_local_blob_store`
(`connectors/runs/orchestrator.py:3125`), which resolves to `/srv/app/_local_blob_store`
(`Dockerfile:109`). At the time of audit `docker-compose.yml` declared only
`postgres-data` and `redis-data` volumes — **neither path was mounted**. Both were
wiped by any rebuild, `--force-recreate`, or `down`.

**Fix:** set `UMS_EXPORT_ARTIFACT_DIR` and `UMS_LOCAL_STORE_ROOT` to paths inside a
new named volume, and mount it on `app` (and `migrate`, if it ever writes there).

> **Status update (Round 5) — closed, and this sentence is now stale.** A third
> volume, `app-data` (`docker-compose.yml:362`), is declared and mounted at
> `/var/lib/ums` on both `app` (`:286`) and `app-dev` (`:339`), with
> `UMS_EXPORT_ARTIFACT_DIR` / `UMS_LOCAL_STORE_ROOT` pointed inside it. Verified in a
> running container: `/var/lib/ums/artifacts` and `/var/lib/ums/blobs` exist, are owned
> by uid 10001, and are writable. The adversarial pass went one step further than the
> implementation did and exercised the **real** dependency —
> `current_export_artifact_store()` via `from_environment()` (`api/exports.py:245`)
> resolves to `/var/lib/ums/artifacts`, where a bare `FileSystemExportArtifactStore()`
> would still resolve to the module default under `/tmp`. So the permanent-503 mode is
> closed on the path the API actually uses, not merely on a constructor.
>
> **Not closed:** `app-data` is not covered by the database backup. See
> [`22_BACKUP_RESTORE_AND_REHEARSAL.md`](22_BACKUP_RESTORE_AND_REHEARSAL.md),
> *Open items*.

### B5 — There is no browser app in any non-dev path
`frontend/` has no Dockerfile; compose has no frontend service; the backend mounts
no static files. The **only** thing that injects gateway headers is the Vite *dev
server* proxy (`frontend/vite.config.ts` `server.proxy`) — there is no
`preview.proxy`, so a built bundle served statically gets 401 on every call
(`api/dependencies.py:96-99`). Two further gaps in that proxy: its route list omitted
`/users` and `/org-units`, and no CORS middleware exists anywhere in the backend, so a
cross-origin bundle would fail preflight (`client.ts:90` sets `X-UMS-Tenant`, a
non-simple header).

> **Status update (Round 5).** The route-list half is fixed: `TENANT_SCOPED_ROUTES`
> now carries `/org-units` and `/users` and spans `vite.config.ts:16-49`. The list is
> guarded by `frontend/tests/devProxyRoutes.test.ts`, which **derives** the required
> set from the path literals in `frontend/src/lib/api/**` rather than comparing the
> list to a hand-copy of itself. The CORS half is unchanged and remains a non-issue
> for the same-origin dev-server beta path.

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
it**. The compose healthcheck targets `/livez` (`docker-compose.yml:288`), so the
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
(`docker-compose.yml:89-92`) are read by **no backend code, no test, and no
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
(as they stood at `d8418cea2`) point at `deploy/helm/` — **that directory does not
exist.**

> **Status update (Round 5) — the `deploy/helm/` half is closed, and the claim was
> weaker than published.** The directory does not merely *not exist now*: it has
> **never existed in this repository.** Verified two ways —
> `git ls-files deploy` returns nothing, and `git log --all -- deploy` returns **zero
> commits**. Every reference was aspirational text that was never backed by a file.
> All four are corrected: `docker-compose.yml:54-59` (says plainly there is no Helm
> chart and no `deploy/` directory), `README.md` (the layout tree no longer lists it,
> and the "secrets layer" sentence no longer implies a cluster exists), and
> `SECURITY.md` (the vulnerability-scope list and the hardening posture). The only
> surviving mentions are `.gitignore:223-224`, two ignore patterns for Helm chart
> caches left by PR #28 — harmless, and left alone because `.gitignore` was outside
> the scope of this change.
>
> **The env-template half is NOT closed.** `.env.example` still predates the database
> variables, so `docker compose --env-file .env.example config` exits 1 on
> `UMS_DB_USER`; `docker-compose.yml:32-52` now carries the authoritative list and an
> explicit warning instead. Completing the template is plan item **P0.3**.

---

## Verified sound

These were checked and found correct — no action needed:

- **Localhost binding**: every published port binds `127.0.0.1`
  (`docker-compose.yml:196,240,284,333`). Re-verified at runtime in Round 5 — all
  three published ports render `host_ip: 127.0.0.1` in `docker compose config`.
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
import, not by connector** — so Google is never called and the currency questions
(D1/D2) do not arise: the operator supplies the figures directly.

Required before real data: **B0** (the stack must actually start), **B3** (backups),
**B4** (artifact volume — note the permanent-503 consequence), and the **logging fix**
(one `basicConfig` call, so that INFO-level connector progress is recorded and what
already prints can be placed in time). B1/B2 are *accepted risks* documented in the
runbook rather than fixed, justified solely by the localhost binding.

> **Round 5 status:** B0, B3 and B4 are closed and verified. The logging fix (P0.6)
> is **not** done. Neither is the runbook that writes B1/B2 down as accepted risks —
> until that exists, "accepted risk" is a phrase in an audit rather than a decision
> the operator has actually been shown.

Also needed: compose `.env` template, the `security_seed.sql` step (H1), a first-user
recipe (H3), a reboot runbook (nothing restarts itself), and a written note that a
connector-only month cannot be locked.

### Path A+ — Path A plus live connector ingest
*No longer blocked on a correctness defect* (see the retraction). Everything in
Path A, plus: make the requested currency explicit (D1/D1b — ~3h, a downstream
no-op), answer the open decision in `Docs/16:70-71` about USD facts vs EGP
settlement, decide what happens to non-USD amounts in the pipeline (today they are
variously skipped, which overstates net), add the connector-job startup sweep, and
pass the worker env vars through compose.

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
- **MEDIUM — non-USD tax/deduction rows are skipped**
  (`finance/deduction_components.py:108-110`), which **overstates** net.
  *Round 4 correction: not "silently."* The skip is counted, returned, audited
  (`deduction_ingestion.py:604,611`) and printed by the CLI
  (`scripts/run_deduction_ingestion.py:196,204`). It is invisible in the API and UI
  only — still worth surfacing, but for a narrower reason than first stated.
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

- **MEDIUM — there is no logging configuration** (downgraded from HIGH in Round 4).
  There is genuinely no `basicConfig`, no `dictConfig`, and no handler anywhere in
  the backend, against 11 module loggers. But the original claim that **"every
  `logger.*` call is discarded"** was wrong: Python's `logging.lastResort` handler
  emits `WARNING` and above to stderr with no configuration at all. The accurate
  statement is:
  - `logger.warning` / `.error` / `.exception` **do** reach the container log,
    tracebacks included — but with **no timestamp, no logger name, no level**.
  - `logger.info` / `.debug` are discarded, which is where connector-run progress,
    tenant resolution, and the export lifecycle live.

  Still worth fixing early and still cheap, but the reason is "a connector run that
  half-worked leaves no trace, and nothing that *does* print can be placed in time"
  — not "nothing is diagnosable."
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
  at the lock screen with nothing running. `app-dev` (`docker-compose.yml:317`) has
  no `restart` key at all, and the beta UI (the Vite dev server) is a host process
  outside compose entirely. **`README.md` does not contain the string "docker"
  anywhere**, and nothing in the repo mentions Docker Desktop or WSL2.

  > **Partial status update (Round 5).** The README now has a compose section, and
  > `Docs/22` documents the Docker-Desktop-starts-at-login constraint and builds the
  > backup's Task Scheduler registration around it. `app-dev` still has no `restart`
  > key — deliberately, it is a `--profile dev` opt-in, not part of the beta stack —
  > and **nothing yet brings the whole stack back after a reboot**. That is the
  > runbook item in P1, and it remains open.
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

  > **Status update (Round 5) — mostly closed, with one deliberate omission.** The
  > storage roots and the executor/scheduler variables are now forwarded
  > (`docker-compose.yml:143-147`, `:169+`). `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`
  > is **deliberately still not forwarded** (`docker-compose.yml:103-141`), because
  > `.env.example` ships it uncommented as the public placeholder
  > `00000000-…-bb` and `connectors/google/audit.py:90-94` refuses only on `None`,
  > accepting any syntactically valid UUID. Forwarding it would mean every operator
  > who ran `cp .env.example .env` silently attributed their connector audit trail to
  > one well-known id published in a public template. A refusal is recoverable; a
  > mis-attributed audit trail is not.
  >
  > **Two costs of that choice, stated plainly.** (1) Google connector ingestion in
  > the long-running `app` service is off until either the backend rejects the
  > placeholder explicitly or P0.3 lands. Not a beta blocker — the recommended beta
  > path is manual import (H5) and never calls Google. (2) The operator-facing docs
  > that told you to set this variable became actively wrong. `README.md` is
  > corrected; **`Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md:44,226` is not** — see the
  > [Round 5 section](#round-5--what-executing-the-plan-found), which also carries the
  > recommended durable fix.
  >
  > **New exposure introduced by the same change, recorded rather than discovered
  > later.** Optional variables that were previously inert in `.env` now reach the app
  > and hit `settings.py`'s fail-fast parsers. A typo takes the API down:
  > `UMS_CONNECTOR_JOB_MAX_WORKERS=two` →
  > `ValueError: UMS_CONNECTOR_JOB_MAX_WORKERS must be a positive integer`
  > (`config/settings.py:216`), which under `restart: unless-stopped` restart-loops
  > rather than starting. This is settings.py's stated fail-fast contract and the
  > message lands in `docker compose logs app`, so it is a **behaviour change, not a
  > defect** — but it is worth knowing before you edit those lines. The adjacent
  > hypothesis was checked and **refuted**: a blank value is safe (`VAR=` parses to the
  > default), so only a malformed value bites.
- **MEDIUM — no log rotation is configured on any service**, so container logs grow
  unbounded on the host disk.

  > **Closed (Round 5).** Every service carries the `x-logging` anchor
  > (`json-file`, `max-size 10m`, `max-file 5` → ~50 MiB per service), verified applied
  > at runtime on a live container.
- **MEDIUM — the 10s scheduler close budget equals Docker's entire stop grace**, so
  the shutdown audit of queued jobs often never runs. Set `stop_grace_period: 120s`.

  > **Closed (Round 5).** `stop_grace_period: 120s`, verified at runtime as
  > `StopTimeout=120` on the running container.

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

> **Round 5 re-rank.** Item 5's log-rotation half is closed. Everything else on this
> list stands, and one item should be added at the top that no reading round could
> have placed there: **the stack does not start.** It was rank 0 the whole time.

---

## Round 3 — "the buttons don't work and it's a mockup"

Reported from hands-on use, then traced to root cause. All three observations are
correct, and they have three *different* causes — one of which is a one-line fix.

### Why no button works: the shipped dev identity has 2 of 26 permissions

> **Correction (Round 5): this section previously said "2 of 28", and 28 is wrong.**
> The `Permission` enum (`backend/ums_smart_revenue/auth/permissions.py:5-31`) has
> **26** members — counted mechanically, not by eye:
> `len(list(Permission))` → `26`. The ratio, and everything the section concludes from
> it, is otherwise unchanged; only the denominator was inflated. The same wrong number
> appeared in `Docs/21` and `Docs/01` and is corrected in both.
> (`SECURITY.md:65` already said 26 and was right all along.)

The buttons are **not** unwired. Of 43 `<button>` elements across the UI, **39 carry
a handler**; the 4 that don't are the global chrome controls already recorded as
dead. They fail at the API, not in the DOM.

`frontend/vite.config.ts` injects a fixed dev identity, and its default role is
`assistant_analyst` (`vite.config.ts:86`):

```
["X-Role", "VITE_DEV_GATEWAY_ROLE", "assistant_analyst"],
```

`auth/seed.py` grants that role exactly two permissions — `VIEW_ANALYTICS` and
`VIEW_CONFIDENCE` — out of **26** defined (`auth/permissions.py` Permission enum).
Only `audit_viewer` (1) has fewer; it ties with
`system_integration_user` for second-weakest. For comparison, measured the same way:
`finance_admin` 15, `corporate_admin` 12, `finance_approver` 11, `data_steward` 5,
`super_owner` 26.

So out of the box every write action (import, sync, run connector, lock month,
export, manage users) is denied, and so is every read gated on `VIEW_REVENUE`,
`VIEW_FINALIZED_PAYMENTS`, or `VIEW_RAW_FILES`. **The product is being demonstrated
by its most restricted role.**

**Fix:** set `VITE_DEV_GATEWAY_ROLE` to a role that can actually operate —
`finance_admin` for the finance surface, `corporate_admin` for setup
(including `POST /users` — `finance_admin` holds `roles.assign` but **not**
`users.manage`; see Docs/23). One line, no code change. This should be the first
thing any beta runbook says, and its absence from the README is arguably the
single highest-impact documentation gap here.

> **Correction (Round 4).** An earlier revision of this section said to put that
> line in `frontend/.env`. **That file is not read.** `vite.config.ts:41-51` pins
> `envDir` to the **repo root** and calls `loadEnv(mode, REPO_ROOT, "")`
> (`:93`, `:151`) — and the comment at `:38` records that resolving to
> `frontend/.env` was a past bug, deliberately fixed. The variable belongs in the
> **repo-root `.env`**, which does not currently exist on this machine. Following
> the original instruction would have silently changed nothing.

### A second reason parts of the Registry look broken: `/org-units` was not proxied

`RegistryView` calls `useOrgUnits()` unconditionally on mount
(`RegistryView.tsx:1216` → `GET /org-units`), but `TENANT_SCOPED_ROUTES` listed
`/tenants`, `/session`, `/revenue`, `/finance-close`, `/exports`, `/connectors`,
`/adsense`, `/channels`, `/groups`, `/audit` — **no `/org-units`, no `/users`**.
Since the beta UI *is* the Vite dev server (B5), that request never reached the
backend.

> **Correction (Round 5) — the mechanism published here was wrong, and so was the
> status.** This section said *"Vite's SPA fallback answers it"* and labelled the
> finding *"not verified by running"*. It has now been run, by booting the real
> `vite.config.ts` through Vite's `createServer` and issuing the exact request the
> client issues. The SPA fallback does **not** answer it:
>
> ```text
> # Accept: application/json  — what frontend/src/lib/api/client.ts sends
> {"path":"/reports/raw-files","status":404,"contentType":null,"bodyBytes":0}
> {"path":"/exchange-rates",  "status":404,"contentType":null,"bodyBytes":0}
>
> # Accept: text/html,...     — what a browser address-bar navigation sends
> {"path":"/reports/raw-files","status":200,"contentType":"text/html","bodyBytes":750}
>
> # a PROXIED route, for contrast (no backend was running)
> {"path":"/org-units","status":502,"contentType":"text/plain","bodyBytes":0}
> ```
>
> `client.ts` defaults `Accept: application/json` (`buildHeaders`, `client.ts:76-78`).
> Vite's html fallback **declines** that Accept, so the dev server returns a bare
> **404 with a zero-length body and no `Content-Type` header at all**. `index.html`
> comes back only for an html-accepting navigation. The difference matters for
> diagnosis: an operator who pastes the URL into the address bar sees the app render
> and concludes the route is fine, while the app's own fetch has been getting an empty
> 404 the whole time.

`RegistryView` degrades gracefully (`orgUnitState.data ?? []`, showing raw ids
instead of names), so it is not a crash — but the Company and Sector columns never
resolve to names, and it reads as a backend bug.

> **Beyond what the plan costed: this also broke a write path.** The same empty
> `companies` list (`RegistryView.tsx:1247-1251`, `orgUnitState.data` filtered to
> `type === "COMPANY"`) feeds the **Mapping Change Request** company `<select>`
> (`RegistryView.tsx:840-845`). With `/org-units` unproxied that dropdown contained
> nothing but its `Select company…` placeholder, so **the operator could not assign a
> channel to a company from the UI at all** — the one UI-reachable write on that
> screen. That is a functional gap, not a cosmetic one, and it belongs to plan item
> **P0.9** (the org-unit skeleton), whose two seeded rows are worthless if the picker
> that consumes them is empty.

**Status: fixed.** `TENANT_SCOPED_ROUTES` now carries `/org-units` and `/users`
(`vite.config.ts:16-49`), guarded by `frontend/tests/devProxyRoutes.test.ts`.

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

> **Correction (Round 5) — "never been started" was the observation; "could not have
> started" is the fact.** This paragraph, and every paragraph derived from it, treated
> the absence of volumes as an *untested-environment caveat*: the implication was that
> the stack would come up and simply had not been asked to. It would not have.
> `docker compose up` against this file put Postgres into a restart loop on any
> machine, Mac or PC — see
> [B0](#b0--the-compose-stack-could-never-have-started-resolved). The absent volumes
> were not a gap in the evidence; they were the evidence, and this audit read them as
> the wrong thing for four rounds.

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

## Round 4 — corrections found while costing the fixes

The costing pass produced five corrections. Two invalidate published findings; one
invalidates published *advice*; two are new findings the earlier rounds missed.

1. **🔴 RETRACTED — "revenue currency is fabricated."** The Round 2 headline was
   wrong. `currency` is a `reports.query` **request** parameter selecting the output
   currency; Google's response carries no currency field; omitting it returns USD.
   Full reasoning and evidence in the Verdict. Severity drops from CRITICAL/blocker
   to MEDIUM (D1: make the default explicit, ~2h, downstream no-op), plus a separate
   **feature gap** (D2: UMS cannot represent EGP at all, 3–6 weeks) and an
   **unmade decision** (D3: `Docs/16:70-71`).
2. **"Every `logger.*` call is discarded" — corrected.** `logging.lastResort` emits
   `WARNING`+ to stderr with no configuration. `WARNING`/`ERROR`/`EXCEPTION` already
   print with tracebacks, just without timestamps; `INFO`/`DEBUG` are lost.
   Downgraded HIGH → MEDIUM, and the fix's justification changed.
3. **The Round 3 fix instruction was wrong.** "Set `VITE_DEV_GATEWAY_ROLE` in
   `frontend/.env`" — that file is not read. `envDir` is pinned to the repo root
   (`vite.config.ts:41-51,93,151`), and `:38` records that as a deliberate past fix.
   Following the original instruction would have changed nothing. The correct file is
   the repo-root `.env`, which does not exist on this machine.
4. **NEW — `/org-units` is not in the dev proxy**, while `RegistryView` calls it on
   mount. Company/Sector names will never resolve in the beta. One line.
   *Round 5: fixed and confirmed by running it. The costing under-scoped it, though —
   it also emptied the Mapping Change Request company picker, so it was a broken write
   path, not only unresolved names. See the corrected section above.*
5. **NEW — a second, untested delete path.** A full `run_deduction_ingestion.py` run
   (no `--source`) deletes the reconciliation workflow's components for that month
   (`deduction_ingestion.py:586` → `:269-284`). No test covers it.

Two further items worth carrying, found while costing but not defects in the audit:

- **`pg_dump` does not dump roles.** A restore into a fresh container fails on the
  RLS policies referencing `app_tenant`/`app_platform`
  (`20260608_0001_tenant_rls_enforcement.py:92-113`). Without an accompanying
  `pg_dumpall --roles-only`, backups look fine and are **unrestorable** — the worst
  possible failure shape for B3.
- **A test asserts a capability that does not exist.**
  `test_export_preview_api.py:632` promises the operator "can rehydrate the artifact
  out of band"; the artifact store has exactly one writer (`api/exports.py:245`).
  Fix the docstring or build the mechanism.

---

## Round 5 — what executing the plan found

Rounds 1–4 read. Round 5 ran W0.2 and P0.1–P0.5 and then attacked the result. Two
findings came out of it that no reading pass produced, and three published claims died.

### The headline: reading found five blockers, running found the sixth in a minute

[B0](#b0--the-compose-stack-could-never-have-started-resolved) is the whole lesson of
this round. It was not subtle, not conditional, and not environment-specific — the
image printed the fix in its own log — and four rounds of audit, including one whose
entire method was naming the exact line a fix would touch, walked past it. The
distinguishing feature of B0 is not that it was hard to see; it is that **seeing it
required starting the thing.**

Worth carrying forward: a "has never been run" line in an audit is not a caveat to be
noted and worked around. It is an unopened box, and it should be opened before the
findings around it are trusted.

### Published claims Round 5 killed

1. **"The compose stack has never been started"** — true, but the conclusion drawn
   from it ("therefore the first beta run is the first rehearsal") was too gentle. The
   stack *could not* start. Corrected in three places above.
2. **"Vite's SPA fallback answers the unproxied request"** — refuted by running it. It
   returns a bare 404 with a zero-length body and no `Content-Type`; `index.html` only
   comes back for an html-accepting navigation. Corrected in the Round 3 section.
3. **"2 of 28 permissions"** — the enum has 26. Corrected here, in `Docs/21` and in
   `Docs/01`.

Two more claims were corrected inside the implementation itself rather than here:
`docker-compose.yml`'s header no longer says `.env.example` "lists every variable this
file requires" (it does not — `--env-file .env.example config` exits 1), and it no
longer points at a `deploy/helm/` that has never existed.

### The one thing Round 5 shipped half of

`UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` was removed from the compose pass-through for
a good reason (see the status note under *Does it survive this PC?*), but the change
initially landed with its documentation half missing — `README.md`'s env table and the
`Docs/19` go-live checklist both told the operator to set a variable that the app would
then report as unset. The episode is recorded because the failure shape is worth
remembering: **a correct, deliberate trade is indistinguishable from a bug to the
person who hits it, if the only record of the decision lives in a file they never
open.**

- ✅ `README.md` is corrected: the env table flags the variable as not forwarded by
  compose, and a note explains why, what it costs, and the
  `docker-compose.override.yml` escape hatch (verified working).
- ❌ **`Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md` is still wrong and was not touched
  here.** `Docs/19:44` ("`UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` is set for live
  non-dry-run execution") and `Docs/19:226` (a go-live **checklist** line —
  "`…SERVICE_ACTOR_ID` is set to an active service actor with `connectors.run_jobs`")
  can both be satisfied exactly as written while every connector run under compose
  still refuses. That file was outside this change's scope; it needs the same
  correction, and until it lands the checklist can be signed off on a stack that
  cannot run a connector.

> **Recommended durable fix, not yet implemented (backend work).** Compose interpolation
> has only `:-` / `:?` / `:+` / `-` — no pattern matching — so a compose-side refusal of
> a *specific* value is not constructible, and an `.env.example`-only fix would still
> leave `docker run --env-file .env`, a bare uvicorn, and any future template exposed.
> The complete fix is in `build_connector_service_principal`: reject
> `00000000-0000-0000-0000-0000000000bb` explicitly alongside the existing `None`
> check, with its own message. That closes it at the boundary that already owns the
> fail-closed contract, and makes the compose pass-through safe to restore.

### Backup: P0.1 landed, and its own adversarial pass found real holes

`scripts/backup_database.py`, `scripts/restore_database.py`, 24 tests, and
[`22_BACKUP_RESTORE_AND_REHEARSAL.md`](22_BACKUP_RESTORE_AND_REHEARSAL.md) ship. The
roles trap (B3's worst shape — backups that look perfect and are unrestorable) is
closed and rehearsed end to end: 38/38 tables, 187/187 rows, and a privilege surface
that matches the source exactly.

Two defects the first implementation shipped were caught and one was fixed:

- **Fixed — retention could evict the only backup containing data.** Seven content-free
  nights filled the `--keep-min` window and pushed the real backup out of it. Retention
  is now content-aware, and proven on the exact fixture that produced the failure.
- **Not fixed — the content gate's absolute floor is far too low.** `MIN_ROWS = 1`
  is justified in the code by "a freshly migrated database has one stamp row." That
  premise is false for this application: migration `20260523_0001` seeds
  `ISO_4217_CURRENCIES_2026_05`, which is **178 rows** (verified by importing it), so a
  virgin `alembic upgrade head` carries 180 rows, not 1. A database truncated to
  nothing but `alembic_version` therefore still passes the floor and publishes a green
  `OK`. The relative collapse check is the only real protection, and it re-anchors
  every night, so a slow drain passes too.

Both limits are written into Docs/22 rather than left implicit — see *What a green run
does not guarantee* there. They are recorded as **open**, not closed, and they are the
first thing to fix after the beta boots.

### What Round 5 verified that nobody had tested before

- Data survives a container replacement (row returns with its original timestamp).
- `down -v` still genuinely re-initialises: fresh `initdb`, full migration chain, 38
  tables, `app_tenant` and `app_platform` present.
- The app **serves** rather than merely reporting healthy: `/livez` 200 and
  `/openapi.json` 200 from the host on the published port.
- Export artifacts resolve to the mounted volume **through the real API dependency**
  (`from_environment()`, `api/exports.py:245`), not just through a bare constructor.
- The dev proxy's route list is now guarded by a test that derives its expectation from
  `frontend/src/lib/api/**` instead of comparing the list to a copy of itself.

### Still not verified, after Round 5

- **Nothing has run on the target Windows PC.** Every compose result above came from
  throwaway projects on the development machine. Docker Desktop's start timing, WSL2
  behaviour, and the reboot-recovery path remain unverified on the real hardware.
- **No scheduled task has been registered**, on either machine.
- **No real CMS revenue has been backed up or restored.** The reference database is a
  seeded schema.
- **`.env.example` is still incomplete** (P0.3), so `cp .env.example .env` is still not
  enough to boot the stack.

---

## Evidence index

| Area | Primary evidence |
| --- | --- |
| Gateway-asserted identity | `api/dependencies.py:77-120,139-158,181-189` |
| Authz mode default | `config/settings.py:27,87-91`; `docker-compose.yml:87` |
| No local auth | `db/security_models.py:38-78` (no password column) |
| Backups absent (now closed) | `Docs/01_IMPLEMENTATION_PLAN.md:1205`; `scripts/backup_database.py`; `Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md` |
| Postgres 18 volume mount (B0) | `docker-compose.yml:226`, rationale `:198-225`; image `PGDATA=/var/lib/postgresql/18/docker`, `Config.Volumes={"/var/lib/postgresql":{}}` |
| Ephemeral artifacts (now mounted) | `reports/artifact_storage.py:13`; `orchestrator.py:3125`; `Dockerfile:109`; volume `docker-compose.yml:362`, mounts `:286,339` |
| Frontend serving | `frontend/vite.config.ts` (`server.proxy` only, routes `:16-49`) |
| Dev-proxy route guard | `frontend/tests/devProxyRoutes.test.ts` (derived from `frontend/src/lib/api/**`) |
| Permission count (26, not 28) | `auth/permissions.py:5-31`; `auth/seed.py`; `SECURITY.md:65` |
| `deploy/helm/` never existed | `git ls-files deploy` → empty; `git log --all -- deploy` → 0 commits |
| Roles seed | `backend/ums_smart_revenue/db/security_seed.sql` |
| Manual revenue import | `api/revenue.py:197-206,1016`; `finance/revenue_facts.py:32` |
| Secret resolvers | `connectors/credentials.py:30-37`; `connectors/google/secret_resolver.py:70-84` |
| Dead compose config | `docker-compose.yml:89-92`; `Docs/16_OPEN_DECISIONS.md:35-38` |
| Localhost binding | `docker-compose.yml:196,240,284,333` |
