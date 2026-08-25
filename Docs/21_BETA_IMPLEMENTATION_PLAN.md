# 21 — First-Beta Implementation Plan

**Built from:** [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md)
(4 rounds, at `main` = `d8418cea2`).
**Target:** one operator, one Windows PC, `docker compose`, localhost only, real CMS
revenue entering by **manual import**.
**Method:** every item below was costed against the actual code — file, line, what
breaks, what it unblocks — not estimated from the finding text.
**Status:** updated in place as items land. **W0.2 and P0.0–P0.9 are done**, with the
single exception of the `.env.example` half of P0.3, which stays 🟡 — see
[P0.3](#p03--compose-env-vars--envexample--12h). The audit gained a **sixth blocker**
while the P0 band was being done — see
[P0.0](#p00--the-compose-stack-could-never-start--done-). What is genuinely still open
is **W0.1**, **W0.3**, the `.env.example` file itself, and the whole **P1** band.

---

## Status at a glance

| Item | State | Note |
| --- | --- | --- |
| **P0.0** *(new)* | ✅ **done** | Postgres 18 `PGDATA` mount. Found by running the stack; blocked everything else. |
| W0.1 | ⏳ open | Repo-root `.env` is still the operator's own step; `.env.example` is still incomplete (P0.3). |
| W0.2 | ✅ done | `/org-units` + `/users` proxied; guarded by a derived test. |
| W0.3 | ⏳ open | The re-walk has not happened, so P1 has not been re-scoped. |
| P0.1 | ✅ done | Backup + restore + rehearsal ship. Two known gate limits recorded, not closed. |
| P0.2 | ✅ done | `app-data` volume, verified through the real API dependency. |
| P0.3 | 🟡 partial | Compose forwards the vars (now **six**, incl. `UMS_LOG_LEVEL`); **`.env.example` is still not written** — the one P0 item not closed. |
| P0.4 | ✅ done | Log rotation on every service, verified at runtime. |
| P0.5 | ✅ done | `stop_grace_period: 120s`, verified at runtime. |
| P0.6 | ✅ done | `config/logging_config.py`, applied from the ASGI lifespan (`app.py:146-162`). Stdlib only; third-party loggers pinned at `WARNING`. |
| P0.7 | ✅ done | Migration `20260825_0001` seeds roles/permissions. Redefines a virgin database as **328 rows**, not 180. |
| P0.8 | ✅ done | `scripts/bootstrap_operator.py`, with the `TENANT_CTX` trap handled. |
| P0.9 | ✅ done | Folded into P0.8 as `--org-skeleton`, as planned. |
| P1 (all) | ⏳ open | — |

---

## The headline

**The remaining work is 38–55 hours. Six to eight focused days.**

That is the whole distance between today's `main` and a beta you can put real money
data into. Not another phase, not a rewrite. The application is genuinely built; what
is missing is the layer that lets one person *run* it and not lose data.

Three things are worth saying plainly before the table:

1. **Nothing on the critical path is a redesign.** The largest single item is a
   backup script. The second largest is a bootstrap script. The rest is configuration,
   deletion, and one logging call.
2. **The biggest *visible* problem is one line.** The app looks like a dead mockup
   largely because the dev identity ships with 2 of 26 permissions. Fixing that costs
   a minute and changes the entire impression of the product.
   *(Correction: this plan and the audit both published "2 of 28". The `Permission`
   enum has **26** members — `auth/permissions.py:5-31`, counted mechanically. The
   argument is unaffected; the denominator was wrong.)*
3. **The most expensive item in the audit is not on this plan at all.** EGP support
   is 3–6 weeks and is deliberately deferred — see [Decision D3](#the-one-decision-only-you-can-make).

> **A fourth thing, added after the first day of execution.** The estimate above was
> built from four rounds of reading. The first hour of *running* produced a blocker
> more severe than anything in it: the compose stack could not start at all
> ([P0.0](#p00--the-compose-stack-could-never-start--done-)). Treat "38–55 hours"
> as an estimate of the work that was **visible from the code** — the first execution
> of any un-executed path can still add to it.

---

## Priorities at a glance

| | Band | What it buys | Hours |
| --- | --- | --- | --- |
| **W0** | [Unblock yourself](#w0--unblock-yourself-1-hour) | You can finally *see* the product | **~1** |
| **P0** | [Don't lose the data](#p0--dont-lose-the-data) | Real money data is safe to enter | **9–12** |
| **P0** | [Be able to operate it](#p0--be-able-to-operate-it) | First run works; failures leave a trace | **11–20** |
| **P1** | [Stop looking like a mockup](#p1--stop-looking-like-a-mockup) | It reads as a product | **10–12** |
| **P1** | [Two correctness fixes worth the hours](#p1--two-correctness-fixes-worth-the-hours) | Confidence labels mean something | **2–3** |
| **P1** | [Runbook + rehearsal](#p1--runbook--rehearsal) | It survives a reboot | **4–6** |
| | **Beta total** | | **38–55** |
| **P2** | [After the beta runs](#p2--after-the-beta-runs) | Live connectors, polish | 25–40 |
| **P2.D** | [Daily live sync + API quota](#p2d--the-daily-live-sync-and-api-quota-coverage) | The sync runs itself, once a day | **15–25** |
| **P3** | [Explicitly not doing](#p3--explicitly-not-doing) | — | — |

---

## W0 — Unblock yourself (1 hour)

Do this before anything else, including reading the rest of this plan. Every other
item is easier to judge once you can actually operate the UI.

| # | Change | File | Time | State |
| --- | --- | --- | --- | --- |
| W0.1 | Create repo-root `.env`; set `VITE_DEV_GATEWAY_ROLE=finance_admin` and `UMS_TRUSTED_GATEWAY_TOKEN` | `.env` (new, repo root) | 15 min | ⏳ open |
| W0.2 | Add `"/org-units"` and `"/users"` to `TENANT_SCOPED_ROUTES` | `frontend/vite.config.ts:16-49` | 5 min | ✅ **done** |
| W0.3 | Restart the dev server, click through every view, write down what is still dead | — | 30 min | ⏳ open |

**W0.1 is the single highest-leverage change in this document.** The shipped default
role is `assistant_analyst` (`vite.config.ts:86`), which `auth/seed.py` grants exactly
two permissions — `VIEW_ANALYTICS` and `VIEW_CONFIDENCE` — out of 26. Every write
action and most reads are denied before they reach any logic. You have been demoing
the product through one of its two most-restricted roles (only `audit_viewer`, with
one permission, is weaker).

> **W0.2 — done, and it was bigger than 5 minutes' worth of consequence.** The array
> now spans `vite.config.ts:16-49` and carries `/org-units` and `/users`.
>
> Two things the costing got wrong, both discovered by running it:
> 1. **The mechanism.** This plan and the audit said Vite's SPA fallback answered the
>    unproxied request. It does not. `client.ts` sends `Accept: application/json`,
>    which the html fallback declines, so the dev server returns a **bare 404 with a
>    zero-length body and no `Content-Type`**. `index.html` comes back only for an
>    html-accepting address-bar navigation — which is why the route looked fine when
>    checked by hand.
> 2. **The blast radius.** It was not only unresolved Company/Sector *names*. The same
>    empty list feeds the Mapping Change Request company `<select>`
>    (`RegistryView.tsx:840-845` ← `:1247-1251`), so the operator **could not assign a
>    channel to a company from the UI at all.** That is a P0.9 dependency: the two
>    seeded org-unit rows are worthless if the picker that consumes them is empty.
>
> The route list is now guarded by `frontend/tests/devProxyRoutes.test.ts`, which
> **derives** the required set from the path literals under `frontend/src/lib/api/**`
> rather than comparing the list to a hand-copy of itself. The old test caught removal
> and could never have caught the omission that caused this defect.

> ⚠️ **The file is the repo-root `.env`, not `frontend/.env`.** `envDir` is pinned to
> the repo root (`vite.config.ts:41-51,93,151`) and the comment at `:38` records that
> resolving to `frontend/.env` was a bug someone already fixed. An earlier revision of
> the audit told you to use `frontend/.env`; that advice was wrong and would have
> silently done nothing.

**W0.3 matters.** Half of what looks broken today is the permission gate. Re-walking
the app afterwards tells you which P1 frontend items are real and which evaporate —
possibly saving several hours from the estimate below.

---

## P0 — Don't lose the data

**9–12 hours.** This band is non-negotiable: it is the difference between "a beta" and
"an incident." You are about to put real CMS revenue into a database that currently has
no backup of any kind.

### P0.0 — The compose stack could never start — **done** 🔴

*Not in the original plan. Added when the first `docker compose up` ever run against
this file put Postgres into a restart loop. Numbered `0` because it precedes every
other P0 item: while it stood there was no running database to back up (P0.1) and no
running container to mount a volume on (P0.2).*

`postgres:18-alpine` sets `PGDATA=/var/lib/postgresql/18/docker` and declares its own
`VOLUME` at the parent path; `docker-compose.yml` mounted `postgres-data` at the pre-18
path `/var/lib/postgresql/data`. Postgres 18 hard-errors on that unused mount
(`restarts=7`, `health=unhealthy`) and the image's log names the fix verbatim.

Worse than the restart loop: had it started anyway, `PGDATA` would have resolved
*outside* the volume, so the database would have lived in the container's writable
layer and been destroyed by the next rebuild — silently, with the stack reporting
healthy throughout.

**Fixed** in `docker-compose.yml`: the `postgres` service now mounts `postgres-data` at
`/var/lib/postgresql` (grep `postgres-data:/var/lib/postgresql`), with the rationale in
the comment block directly above that line. It trips on a **fresh, empty** volume, so
there is no stale data and no migration path to write.

**Verified, not assumed:** whole stack `Healthy` + `migrate` exit 0; `SHOW
data_directory` inside the mounted volume; a written row survives `down` (no `-v`) and
returns with its **original** timestamp under a different container id; `down -v` still
re-`initdb`s to 38 tables with both RLS roles. An independent pass attacked it from
five angles and it held.

**Skippable?** It is not an item; it is the precondition for the band.

### P0.1 — Database backup, restore, and one rehearsal — **4–6h** 🔴

The only item in this plan I would refuse to skip.

- `pg_dump -Fc` to a **host** directory (not a container path), on Task Scheduler.
- A restore script, and **one rehearsed restore into a throwaway container.**

> ⚠️ **`pg_dump` does not dump roles.** A restore into a fresh container fails on the
> RLS policies and grants that reference `app_tenant` / `app_platform`
> (`db/alembic/versions/20260608_0001_tenant_rls_enforcement.py:92-113`). Add
> `pg_dumpall --roles-only` alongside. **Without it your backups look perfect and are
> unrestorable** — the worst possible shape for this failure, and the reason the
> rehearsal is part of the estimate rather than optional.

**Skippable?** No.

> ✅ **Done.** `scripts/backup_database.py`, `scripts/restore_database.py`, 24 tests,
> and the runbook [`22_BACKUP_RESTORE_AND_REHEARSAL.md`](22_BACKUP_RESTORE_AND_REHEARSAL.md).
> The roles trap is closed and the rehearsal passes end to end (38/38 tables,
> 187/187 rows, privilege surface identical to the source). The estimate held.
>
> **Both limits recorded here have since been CLOSED (round 3).** They are kept
> visible because they show what the first two attempts got wrong.
> 1. ~~**The content gate's absolute floor is far too low.**~~ `MIN_ROWS = 1` was
>    justified in the code by "a freshly migrated database has one stamp row." That
>    was false here: migration `20260523_0001` seeds `ISO_4217_CURRENCIES_2026_05`,
>    **178 rows** — a virgin `alembic upgrade head` carries **180**, not 1. A database
>    truncated to nothing but `alembic_version` published a green `OK`.
>    **Fixed:** `MIN_ROWS` is gone, replaced by a *seed-table floor* —
>    `alembic_version`, `currencies` and `tenants` must each exist and be non-empty,
>    with no override. Existence rather than a hardcoded 178, because
>    `iso_4217_2026_05.py` is a dated snapshot and coupling to its size would turn a
>    routine ISO refresh into a box-wide backup outage.
> 2. ~~**That collapse check re-anchors every night.**~~ It compared only against the
>    immediately previous accepted run, so an 80%/night drain passed green three
>    nights running. **Fixed:** a persistent **per-table high-water mark** that never
>    follows the data down, stored in two homes and merged by max so losing either
>    rebuilds upward. `--accept-content-drop` lowers only the tables the suppressed
>    failure actually named, so one legitimate deletion cannot blanket-reset the rest.
>
> **Closed since that list was written** (rounds 4 and 5, each measured against the
> live CLI — see Docs/22's evidence tables): `_write_last_run`'s swallowed `OSError`
> now exits **7** and writes `last-run-<stamp>.json` beside the locked file; a junk
> manifest with no artifacts behind it contributes **nothing** to the watermark; an
> out-dir is now **bound to one database** by cluster `system_identifier`
> (`--adopt-database` to rebind deliberately); `--establish-watermark` over a
> **wholly** empty database is **refused** (tier 3b); and a future-dated directory is
> no longer permanently wedging (`_run_stamp` refuses it as history).
>
> **What is genuinely still open — accepted for the beta, not fixed.** These are
> bounded residuals recorded on purpose, with the operator-facing versions in
> [`22_BACKUP_RESTORE_AND_REHEARSAL.md`](22_BACKUP_RESTORE_AND_REHEARSAL.md):
>
> - **`--establish-watermark` remains the sharpest edge.** The *wholly* empty case is
>   closed; a **partially** populated one is not. A database holding 7 rows where it
>   should hold 700 clears every gate on a first run and is published as the permanent
>   reference. The flag is an acknowledgement of a printed number, not a check, and the
>   only barrier is the operator reading that number and knowing it is wrong.
>   ⚠️ **The number changed on 2026-08-25:** a virgin database is now **328** rows, not
>   the `rows=180` this list used to quote — P0.7 seeded 148 more. Read
>   **`non_seed_rows=`** rather than `rows=`; it excludes seeded rows and does not move
>   when a future migration seeds more.
> - **The future-stamp refusal is a deferral, not an immunity.** A future-dated
>   directory is inert only while the wall clock is behind its stamp. Once real time
>   passes it, the same directory becomes ordinary history and folds into the watermark
>   — exit `8`, recovered by one `--accept-content-drop` night, or **two** if the stamp
>   sits inside the 5-minute tolerance (there it is already read as history while its
>   name still sorts above tonight's run, so `reset_after` cannot exclude it). Docs/22's
>   round-5 row showing `0 / 0 / 0 / 0 / 0` is the far-future `20990101` case only.
> - **A plant on tonight's exact nightly slot costs one missed night.** It never
>   reaches the content gate: `_execute` raises `BackupError(EXIT_USAGE, "… already
>   exists")` first, so it surfaces as **exit `2`, a usage error**, with nothing in the
>   message about clocks or stamps — pointing the operator at their command line rather
>   than at the directory.
> - **Widening `SEED_TABLES` reclassifies runs published before the widening.** A
>   manifest predating the P0.7 roles seed has no `roles` key, and `counts.get(name, 0)
>   > 0` cannot tell *"held zero rows"* from *"written before the table existed"* —
>   both read `0`, i.e. "proven empty". Such a run loses its `--keep-min` slot and
>   becomes deletable by age, so widening the safety net deletes old backups.
>   **Unreachable today** (no backup directory predates this script's first release,
>   which ships alongside `20260825_0001`), and deliberately unpatched: the obvious fix
>   ("absent means unknown, not empty") swallows the dropped-schema case retention
>   invariant 2 depends on. **The recommendation on record is a schema-generation stamp
>   in `manifest.json`, decided with a reject→accept matrix over `_prune`** — enumerate
>   the states the rule must reject and those it must accept before writing it. Round 5
>   found two tests that passed with their guard deleted; that is what skipping the
>   matrix produces. Do this before a *seventh* name joins `SEED_TABLES`.
> - **~90% of a single night's rows can still vanish green** if no table is emptied and
>   no seed table shrinks. Cumulative loss is bounded by the watermark, which does not
>   follow the data down; **a single large deletion is not.**
> - **The `app-data` volume is not backed up** — a separate piece of work (a
>   `--include-volume` streaming `tar` plus its own restore side and rehearsal),
>   deliberately not smuggled into P0.1. Until it exists, re-requesting an export is the
>   only recovery.

### P0.2 — Artifact and blob volume — **2–3h**

Export artifacts and connector blobs live on ephemeral container paths
(`reports/artifact_storage.py:13`, `orchestrator.py:3125`, `Dockerfile:109`). A
container replacement discards them, and a requested export then **503s permanently**.

Fix by mounting a named volume — no code change required.

> 💡 There is an undocumented workaround for the 503 in the meantime: `request_export`
> has no dedup on scope+month (`reports/exports.py:383-433`), so the operator can
> simply request the export again. Note this in the runbook.

**Skippable?** Partial — the mount is mandatory, the code change is not.

> ✅ **Done, no code change needed.** Named volume `app-data` (declared under
> `volumes:` in `docker-compose.yml`) mounted at `/var/lib/ums` on **both** `app` and
> `app-dev`, with `UMS_EXPORT_ARTIFACT_DIR` / `UMS_LOCAL_STORE_ROOT` pointed inside it
> by the `x-app-storage-env` anchor — which is merged only into services that actually
> mount the volume, so a storage path cannot be configured without its volume behind it.
>
> **Verified at the path that matters.** A bare `FileSystemExportArtifactStore()`
> still resolves to the module default under `/tmp`; the API dependency uses
> `from_environment()` (`api/exports.py:245`), and *that* resolves to
> `/var/lib/ums/artifacts` in the running container. Directories exist, are owned by
> uid 10001 from the image, and are writable — so the permanent-503 mode is closed on
> the real path, not on a constructor that nothing calls.

### P0.3 — Compose env vars + `.env.example` — **1–2h**

Compose does not pass the storage vars, and there is no template. This is also what
W0.1 needs a canonical home for.

**Skippable?** The template is mandatory. Some vars can wait.

> 🟡 **Half done, and this is the one P0 item still open.** Compose now forwards the
> storage roots (the `x-app-storage-env` anchor) and **six** executor/scheduler/logging
> variables (the `x-app-env` anchor) — `UMS_LOG_LEVEL` joined the list with P0.6.
> **`.env.example` is untouched**, so `docker compose --env-file .env.example config`
> still exits 1 on `UMS_DB_USER` and `cp .env.example .env` is still not enough to boot
> the stack. Compose's header now carries the authoritative five-variable list and says
> so plainly instead of claiming the template is complete.
>
> **Why it is still open:** `.env*` is write-blocked to the sessions that did the rest
> of this band, so the file has to be created by the operator. Everything needed to do
> that is below — this is the action item, not a status note.

#### The `.env.example` content P0.3 needs (operator action)

Start from the tracked template (`git show HEAD:.env.example`) and make these changes.
The first block is what compose refuses to render without.

```bash
# --- Database (NEW — compose has no defaults for these; it exits 1 without them) ---
UMS_DB_USER=ums
UMS_DB_PASSWORD=change-me
UMS_DB_NAME=ums_smart_revenue
# Same password, percent-encoded for the app DSN. Identical to UMS_DB_PASSWORD
# unless it contains URI-reserved characters (@ : / ? # [ ] and friends).
UMS_DB_PASSWORD_URLENC=change-me

# --- Identity ---
# UMS_TRUSTED_GATEWAY_TOKEN is required. Set it in the environment; do not commit
# a real value into this file or into .env that is checked in.
UMS_AUTHZ_SOURCE=headers

# --- Google connector service principal -------------------------------------
# MUST ship COMMENTED OUT. Uncommented, this public placeholder is accepted by
# build_connector_service_principal (it refuses only on *unset*), so every operator
# who ran `cp .env.example .env` would attribute their connector audit trail to one
# well-known id from a public template. Uncomment ONLY with your own provisioned UUID.
# UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID=00000000-0000-0000-0000-0000000000bb

# --- Optional, and FAIL-FAST: a non-empty malformed value restart-loops the API ------
# Blank (`VAR=`) or absent is always safe and yields the documented default.
# A bad value raises during create_app; under `restart: unless-stopped` the container
# cycles silently and the traceback is only in `docker compose logs app` (last line).
# UMS_CONNECTOR_JOB_EXECUTOR_ENABLED=false
# UMS_CONNECTOR_JOB_MAX_WORKERS=1          # positive integer
# UMS_CONNECTOR_JOB_STALE_RUNNING_HOURS=6  # positive integer
# UMS_GROUP_SYNC_SCHEDULE_ENABLED=false
# UMS_GROUP_SYNC_INTERVAL_HOURS=24         # positive integer
# UMS_LOG_LEVEL=INFO                       # DEBUG|INFO|WARNING|ERROR|CRITICAL

# --- Docker log rotation (sizes the json-file driver, NOT the app's verbosity) -------
# UMS_LOG_MAX_SIZE=10m
# UMS_LOG_MAX_FILE=5
```

Keep the existing `VITE_*` block as it is — commented out, `assistant_analyst`. It is a
template, and the dev-proxy identity belongs to the operator's own file.

**Then, separately, in the repo-root `.env` (this is W0.1, not P0.3):**

```bash
VITE_DEV_GATEWAY_ROLE=finance_admin   # NOT assistant_analyst — 2 of 26 permissions
```

**Two things the template must get right, and both are in the block above.**

1. **`UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` must ship commented out.** It currently
   ships uncommented as the public placeholder `00000000-…-bb`, and
   `build_connector_service_principal` (`connectors/google/audit.py:90-94`) refuses only
   on `None` — any syntactically valid UUID is accepted. Every operator who ran
   `cp .env.example .env` would attribute their connector audit trail to one well-known
   id from a public template. Compose deliberately does not forward the variable while
   that is true.
2. **The forwarded optional variables are fail-fast**, so the template must say so.
   `UMS_CONNECTOR_JOB_MAX_WORKERS=two` raises at boot and restart-loops the API under
   `restart: unless-stopped`. A blank value is safe; a non-empty malformed one is not.
   The parsers are `_load_int`, `_load_bool` and `_load_log_level` in
   `config/settings.py` — named rather than cited by line, because an earlier revision
   of this bullet pinned `config/settings.py:216` for the `MAX_WORKERS` case and that
   line is `_load_bool`'s `must be one of` raise, not `_load_int`'s
   `must be a positive integer`. Same rot as the compose comment; same fix.

**Accept P0.3 when `docker compose --env-file .env.example config` exits `0`.** That is
the whole acceptance test, it takes seconds, and it is exactly the check that would have
stopped this item being called done while the template still failed.

### P0.4 — Log rotation in compose — **20 min** 🔴

Docker Desktop's VHDX grows and **does not shrink**. The baseline is already ~5,760
healthcheck access lines/day, and P0.5 raises volume further.

Twenty minutes, on a box that will run unattended for months. Do it **before** P0.5.

**Skippable?** No, and there is no excuse.

> ✅ **Done.** `x-logging` anchor on every service — `json-file`, `max-size 10m`,
> `max-file 5` (~50 MiB per service). Verified applied on a running container.

### P0.5 — `stop_grace_period` — **30 min**

One line, so in-flight work finishes instead of being killed mid-write.

> ✅ **Done.** `stop_grace_period: 120s`, verified at runtime as `StopTimeout=120`.

---

## P0 — Be able to operate it

**11–20 hours.** Without this band the first run fails at a step nothing documents,
and you cannot tell why.

### P0.6 — Logging configuration — **4–6h**

Downgraded from the audit's HIGH after correction, but still early work.

There is no `basicConfig`, no `dictConfig`, and no handler anywhere in the backend,
against 11 module loggers. The audit originally claimed *every* log line is discarded
— **that was wrong.** Python's `logging.lastResort` emits `WARNING`+ to stderr with no
configuration, so warnings, errors, and tracebacks already print. What you actually
lack is:

- **timestamps, logger names, and levels** on the lines that do print, and
- **all `INFO`/`DEBUG`** — which is where connector-run progress, tenant resolution,
  and the export lifecycle live.

So the real cost of doing nothing is: *a connector run that half-worked leaves no
trace, and nothing that does print can be placed in time.*

> ⚠️ **Two ways to get this wrong.**
> 1. A `dictConfig` can silently disable uvicorn's access logging, which currently
>    works — uvicorn checks `logging.getLogger("uvicorn.access").hasHandlers()`.
>    **Write the regression test.**
> 2. `tests/test_version_baseline.py:20-52` asserts exact-set-equality on
>    dependencies. **Stdlib formatter only — no new packages.**

A crude 3-line `basicConfig` is ~1h. The 4–6h figure is the version that clears ruff,
pytest, mypy, and DeepSource and is env-configurable.

> ✅ **Done.** `backend/ums_smart_revenue/config/logging_config.py`, applied once per
> process from the ASGI lifespan (`app.py:146-162`) and reverted by `restore_logging`.
> Stdlib only, so the exact-set dependency assertion in `test_version_baseline.py` is
> untouched. Configured by `UMS_LOG_LEVEL` (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`,
> case-insensitive; blank or absent → `INFO`), forwarded by compose and documented in
> `README.md`.
>
> **Both named traps were handled, and a third was found.**
> 1. Uvicorn access logging still works — the level goes on the **first-party**
>    `ums_smart_revenue` logger, never on root, and `basicConfig`/`dictConfig`'s
>    `disable_existing_loggers` is avoided entirely. Regression test written.
> 2. No new packages.
> 3. *(Not in the costing.)* Putting the operator's level on the **root** logger would
>    have turned `UMS_LOG_LEVEL=DEBUG` into a data-exposure change: 56 third-party
>    loggers go INFO-enabled, and `httpx2` logs the request line — which carries the
>    **CMS content-owner id** in its query string — on every Google API call. Root is
>    therefore held at `max(level, THIRD_PARTY_LOG_LEVEL)` with
>    `THIRD_PARTY_LOG_LEVEL = WARNING`, and a test walks the real logger tree of an
>    imported app to keep it that way. This is why `DEBUG` is safe to hand to an
>    operator.

### P0.7 — Roles/permissions seed as a migration — **2–4h**

`db/security_seed.sql` is maintained and idempotent, but nothing tells you to run it,
and it is an FK prerequisite for assigning any role.

> ✅ **Done.** Migration `20260825_0001_security_role_permission_seed.py`, with
> `tests/db/test_security_role_permission_seed_migration.py`. The seed is now part of
> `alembic upgrade head`, so "nothing tells you to run it" is closed by construction.
> The tables carry no `tenant_id`, so the revision needs no `TENANT_CTX` and sits
> outside every RLS policy.
>
> ⚠️ **It redefined "a virgin database", and that crossed lanes.** A fresh
> `alembic upgrade head` is now **38 tables / 328 rows** (was 180): `permissions` 26,
> `roles` 16, `role_permission_assignments` 106. That silently disabled the backup
> script's tier-3b empty-database refusal — `_non_seed_rows` on a virgin database read
> `148` instead of `0` — which is recorded as Round 5, finding 4 in
> [`22_BACKUP_RESTORE_AND_REHEARSAL.md`](22_BACKUP_RESTORE_AND_REHEARSAL.md) and is
> fixed there. **The number every earlier round of that document measured is 180 and is
> now stale**; the historical evidence tables are deliberately left as-run under a dated
> note rather than rewritten.

### P0.8 — Bootstrap script (`bootstrap_operator.py`) — **4–8h** ⚠️

Creates the first operator user, and — with `--org-skeleton` — one `SECTOR` plus one
`COMPANY` beneath it.

> ⚠️ **This is the rabbit hole of the whole plan.** It looks like "insert one row." On
> Postgres, `SET LOCAL ROLE app_tenant` + `tenant_id = app_current_tenant_id()` will
> reject every insert unless `TENANT_CTX` is set first — and the script you would
> naturally copy from, `seed_demo_month.py`, **does not do it** (it is SQLite-correct
> only). If this costs you a day, that is why.

> ✅ **Done.** `scripts/bootstrap_operator.py`, with `tests/scripts/
> test_bootstrap_operator_cli.py` and `tests/scripts/test_bootstrap_operator_postgres.py`
> (a real-Postgres test, because the trap above cannot be reproduced on SQLite). The
> `TENANT_CTX` hazard was handled rather than rediscovered — the script does not copy
> `seed_demo_month.py`'s SQLite-correct pattern.

### P0.9 — Org-unit skeleton — **+1–2h** (folded into P0.8)

~40 lines lifted almost verbatim from `seed_demo_month.py:414-455`, which is already
the repo's only org-unit writer and is clean and idempotent.

Two rows remove a HIGH issue (`MISSING_COMPANY` / `MISSING_SECTOR`) from every channel
on your first screen, and unblock `POST /channels`.

> **Do not build `POST /org-units` for the beta.** Router + writer repository +
> `MANAGE_ORG_MAPPING` gating + audit events + cycle validation + tests + a frontend
> that does not exist = 8–16h that buys one operator nothing.
>
> **Residual gap either way:** assigning channels to companies is one
> `PATCH /channels/{id}/mapping` per channel (`api/channels.py:1425`) — no bulk path,
> and the import CSV cannot carry it. For any real roster that is a scripted loop.
> **Put that in the runbook.**

> **Dependency discovered during W0.2, and it changes this item's value.** There *is*
> a UI path for a single mapping change — the Mapping Change Request panel — and its
> company `<select>` is populated from `GET /org-units`
> (`RegistryView.tsx:840-845` ← `:1247-1251`). Before W0.2 that request never left the
> dev server, so the dropdown held nothing but its placeholder and the panel was
> unusable. Two consequences: (a) the "scripted loop" above was, until W0.2, the
> **only** way to map a channel at all, not merely the only bulk way; (b) P0.9's two
> seeded rows are worth nothing without W0.2, so the two items are a pair.

> ✅ **Done, folded into P0.8 exactly as planned.** `bootstrap_operator.py
> --org-skeleton` creates one `SECTOR` and one `COMPANY` beneath it, idempotently, from
> a deterministic namespace. `POST /org-units` was **not** built, per the ruling above.
>
> **The residual gap is unchanged and still belongs in the runbook:** assigning channels
> to companies is still one `PATCH /channels/{id}/mapping` per channel, or the Mapping
> Change Request panel one channel at a time. There is no bulk path and the import CSV
> cannot carry the mapping. The script prints this caveat itself after it runs, so the
> operator meets it at the moment it matters rather than only here.

---

## P1 — Stop looking like a mockup

**10–12 hours** for the entire visible win. This band directly answers *"it's really a
landing page, but mockup."*

**Re-scope this band after W0.3.** With `finance_admin` instead of `assistant_analyst`,
some of these panels will already render real data.

Roughly **90% of this work is deletion.**

| # | Item | Where | Time |
| --- | --- | --- | --- |
| P1.1 | Error boundary — land **first**, so later mistakes degrade to a card, not a white page | new | 2–3h |
| P1.2 | Rolling month window replacing 4 hardcoded months | — | 1.5–2h |
| P1.3 | De-mock the chrome: `NAV_GROUPS`, `VIEW_COPY`, `WORKFLOW_STEPS` + remove 4 dead buttons + delete the inert currency selector | `AppShell.tsx` | 3–4h |
| P1.4 | De-mock `CLOSE_STEPS`, `EXPORT_READINESS`, `ISSUES`, `REGISTRY_SUMMARY`, `REGISTRY_CONTROLS`, `RECON_NOTES`, `EXPORTS_GUARDRAILS` | `CommandView`, `RegistryView`, `CloseView`, `ExportsView` | ~3h |

**The migration is further along than it looks.** `ConnectorsView`, `GroupsView`,
`GroupsSyncFlow`, `RegistryImportFlow`, `TraceView` and all three Audit modules import
**zero** mock symbols. About nine mock datasets are already dead code. What remains is
concentrated in the chrome and the summary tiles — which is exactly the part a visitor
sees first, and why the impression is so much worse than the reality.

**Delete the currency selector** (`AppShell.tsx:629-633`) rather than wiring it. It
offers USD/EGP/AED with no `onChange`, in a pipeline that rejects non-USD everywhere.
It is three lines, no test touches it, and it is the most actively misleading control
in the app — it advertises a capability that is 3–6 weeks away.

**Skip react-router.** State-based view switching is fine for one operator. Optional:
`sessionStorage` view persistence, 1–1.5h.

---

## P1 — Two correctness fixes worth the hours

**2–3 hours**, both tests-clean. Everything else in the correctness cluster is either
0 hours (deliberate, already signalled honestly) or belongs to live connectors.

### P1.5 — The confidence cap is a no-op — **1–2h** 🔴

`finance/explanations.py:498-503` clamps a warned score to exactly `0.9000`, then
labels `HIGH` when `score >= 0.9000`. **A fact carrying warnings is
label-indistinguishable from a clean one.**

This is the cheapest real-bug fix on the list, and it is on the path the browser
actually uses: in a manual-import beta `confidence_score` defaults to `Decimal("1")`
(`api/revenue.py:401`), so the confidence badge is the *only* signal that anything was
flagged. Today it never fires.

### P1.6 — Remove the `ad_revenue` CSV alias — **1h**

A test-fixture alias sitting in the production CSV path (`orchestrator.py:202`),
pre-authorising a schema the report-type whitelist explicitly holds out
(`report_type_whitelist.py:15-16`). Nothing breaks; no test asserts it. Unreachable in
a manual-import beta, but it arms itself the moment anyone widens
`SUPPORTED_REPORT_TYPES`. Cheapest risk reduction available.

---

## P1 — Runbook + rehearsal

**4–6 hours.** This is not documentation busywork.

**The compose stack has never been started on the target PC** — `docker volume ls`
shows no `ums-smart-revenue` volumes. Your hands-on session ran on the Mac. So the
first beta run *is* the first rehearsal, and should be treated as one.

Must cover: first-run order (seed → bootstrap → import), the reboot recovery path
(nothing restarts itself), the restore drill, **B1/B2 written down as accepted risks**
justified by the localhost binding, the per-channel mapping loop, the export-503
re-request workaround, and a note that a connector-only month cannot be locked at all.

---

## The one decision only you can make

Everything about currency hangs on one question that has been open since PR #42 and is
recorded, unanswered, at `Docs/16_OPEN_DECISIONS.md:70-71`:

> **Are USD facts acceptable for the beta, with the EGP bank settlement explained as FX
> variance — yes or no?**

**If yes** (recommended for the beta): nothing changes. The pipeline is internally
consistent and the numbers are real. Do P2.2 when convenient.

**If no:** that is the EGP program — 3–6 weeks, ~2,154 `*_usd` identifiers, and a
USD-only design that is *test-locked* by
`tests/finance/test_finance_no_fx_dependency.py:40-53`. It should be its own milestone
after the beta proves the rest works.

> ⚠️ **Do not let anyone "shortcut" this through `currency_exchange_rates`.** It looks
> like a 2-hour win and will be rejected by an existing guard test, four documents, and
> one closed decision. The sanctioned route to EGP is Google's own server-side
> conversion (`currency=EGP`), never a UMS-derived rate. Your own words are the reason
> it was closed: *"i dont need to make it USD × 47.5, i need pure number."*

---

## P2 — After the beta runs

**25–40 hours.** None of this blocks the beta; all of it is real.

| Item | Time | Note |
| --- | --- | --- |
| Real `/readyz` + repoint the healthcheck | 3–4h | Today the container reports healthy with a dead database |
| P2.2 — Explicit `currency` in the Analytics request | 2–3h | **Downstream no-op** — `source_row_key` hashes unchanged, no re-ingest |
| Same for the Reporting CSV default (`orchestrator.py:205-210`) | 1h | Same class, second site |
| Deduction write API | 8–14h | ⚠️ mind the `replace_source_tables=None` delete hazard |
| Gross/net summed over different channel sets | 3–5h | Additive fix — expose the population, don't change the sum |
| Split-brain confidence | 4–6h | 3–4h if bundled with P1.5 |
| Connector-job startup sweep (orphaned `RUNNING`) | 6–10h | Required for Path A+, not Path A |
| `UMS_AUTHZ_SOURCE=database` | 2–4h | ⚠️ **not before P0.6** — a wrong `X-User-ID` in database mode is a blank "Access denied" and an empty log |
| Scheduler first-tick on a daily-restarted PC | 2–3h | Needs a live Google credential anyway |
| `request_id` + duration middleware | 3–4h | Skip for one operator with one browser tab |
| Fix `test_export_preview_api.py:632` | 0.5h | It asserts a rehydration capability the repo does not have |
| Test the untested delete path in `deduction_ingestion.py:586` | 2h | Found in Round 4; no coverage today |
| ~~Raise the backup content gate's absolute floor~~ | — | ✅ **Done in round 3.** Replaced by a seed-table floor (`alembic_version`, `currencies`, `tenants` must each be non-empty, no override). The 1-row fixture in `tests/scripts/test_backup_content_gate.py` was corrected to the measured 38-table/180-row virgin state, not deleted. |
| ~~Give the collapse check a long-horizon watermark~~ | — | ✅ **Done in round 3.** Persistent per-table high-water mark in two homes, merged by max; losing either rebuilds upward, losing both hits the first-run refusal. Verified: a 5-night drain stays bounded and the mark never follows the data down. |
| ~~**`_write_last_run` swallows `OSError`**~~ | — | ✅ **Done in round 4.** A locked `last-run.json` no longer hides a result: a run that publishes but cannot record its status exits **7** (`BACKUP PUBLISHED, STATUS NOT RECORDED`) and writes `last-run-<stamp>.json` beside the held file; a *failing* run under the same lock still exits 8 and lands its `REJECTED` record. Measured with `FileShare.None`. |
| ~~**A junk manifest can inflate the watermark**~~ | — | ✅ **Done in round 4.** A planted manifest with no dump and no `roles.sql` behind it contributes nothing — a directory claiming `org_units: 1000000000` left `watermark.json` reading the real `org_units: 2`, exit `0`. Round 5 closed the future-dated variant of the same trick separately. |
| ~~**Bind an out-dir to one database identity**~~ | — | ✅ **Done in round 4.** The directory binds to the cluster's `system_identifier`; a second UMS database into the same out-dir is quarantined at exit `8` naming both ids, while a mere container-id change is correctly not treated as a database change. `--adopt-database` rebinds deliberately and is recorded as an override. |
| **Harden `--establish-watermark`** | 1–2h | 🔴 **Still the sharpest edge, but narrower than this row used to claim.** The *wholly* empty case is closed (tier 3b refuses the flag when `non_seed_rows=0`). What remains is the **partially** populated database: 7 rows where there should be 700 clears every gate on a first run and becomes the permanent reference. The flag is an acknowledgement of a printed number, not a check. ⚠️ Any instruction to expect `rows=180` is stale — P0.7 made a virgin database **328**; read `non_seed_rows=`. |
| ~~**Two tests ratify what they claim to prove**~~ | — | ✅ **Done in round 5.** Both were rewritten with discriminating fixtures and say so in their docstrings: the prune test now pairs an expired *real* run with a newer *unknown* one, so `--keep-min` cannot cover for the pin; the watermark test now renames a rejected run to a **run-shaped** name, so the verdict has to travel inside the manifest to refuse it. |
| **Schema-generation stamp in `manifest.json`** | 2–4h | Removes the last `SEED_TABLES` hazard: a manifest predating a seeded table has no key for it, and `counts.get(name, 0) > 0` reads that as *proven empty*, so widening `SEED_TABLES` makes old healthy runs deletable by age. Unreachable today, and the naive fix ("absent means unknown") swallows the dropped-schema case invariant 2 needs. **Decide it with a reject→accept matrix over `_prune`**, not by patching the predicate. Needed before a seventh name joins `SEED_TABLES`. |
| **Back up the `app-data` volume** | 4–6h | `--include-volume` streaming `tar` beside the dump, plus the restore side and its own rehearsal. `down -v` destroys it today and nothing covers it. |
| **Reject the `.env.example` placeholder service-actor id in the backend** | 1–2h | `build_connector_service_principal` should refuse `00000000-…-bb` explicitly alongside its `None` check. Compose cannot express this (interpolation has no pattern matching), and an `.env.example`-only fix leaves `docker run --env-file`, bare uvicorn, and future templates exposed. Landing it makes the compose pass-through safe to restore. |
| **Correct `Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md:44,226`** | 0.5h | 🔴 Its go-live **checklist** line can be signed off exactly as written while every connector run under compose still refuses, because compose does not forward `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`. `README.md` was corrected; that file was outside the compose change's scope and still says the opposite of what happens. |
| ~~Catch the `ValueError` in `_prune`~~ | — | ✅ **Done in round 3**, structurally rather than as a patch on one call: `_run_stamp` parse-validates and reports unparsable directories instead of dying, `_RunReport` writes `RUNNING` before any work and exactly one terminal record after (plus `INTERRUPTED` from a `finally`), and `main` gained a last-resort handler. The audit for the same shape found three more instances, including an `OSError` prune path where exit 7 also left a stale green. |

---

## P2.D — The daily live sync, and API quota coverage

**Requested by the operator: "cover API rate request … once per day", with the exact clock
time to be supplied.** Scoped against the code 2026-08-25. Read the first finding before
costing anything else — it changes what the item *is*.

> **Nothing in this section blocks the recommended beta**, which is manual-import and never
> calls Google. It becomes mandatory the day the daily live sync is switched on.

### 🔴 The item is not what it looks like: there is no daily revenue sync to schedule

`GroupSyncScheduler` (`connectors/runs/scheduler.py:129`) submits **CMS group-sync jobs
only**. It never touches revenue ingest. Revenue reaches Google exclusively through
`run_one` (`orchestrator.py:408`), driven by `POST /connectors/jobs` or
`scripts/run_google_connector.py` — and **nothing schedules either**.

So "add a time of day to the scheduler" would ship a feature nobody asked for and leave
revenue exactly as manual as it is today. The real item is *build the missing scheduled
revenue caller*.

### 🔴 The one that would bite on night two: the run refuses itself after ~1 hour

The live-run admission gate refuses any run whose stored `token_expiry_at` has passed.
Tonight's successful run stamps `token_expiry_at ≈ now + 1h`. Tomorrow night's run reads
that as expired and is refused at preflight — **422 on the route, exit 2 on the CLI, and no
`connector_runs` row on either path.**

**The first scheduled night works and every night after it fails**, leaving no run row to
explain why. This is the single most important finding in the section precisely because it
passes the first time you test it. ~1–3h to fix (re-stamp the credential in the wrapper,
with a test that pushes `token_expiry_at` into the past and demands the run still succeed).
An in-process scheduler sidesteps it entirely — `run_one` never calls the route's preflight.

*Derived from code, not executed. The ~1h access-token lifetime is Google's published OAuth
behaviour, not a repo fact.*

### 🔴 A daily-quota 403 is filed as an authentication error, and the reason is discarded

`http_client.py:57` — `_AUTH_STATUSES = frozenset({401, 403})`. Every 403, **including
daily-quota exhaustion**, goes to `GoogleApiAuthError` with no retry, and
`_terminal_response_or_raise` (`:271-285`) never calls `response.json()` — so Google's
`error.errors[].reason` (`quotaExceeded` / `dailyLimitExceeded` / `rateLimitExceeded`) is
thrown away before anyone sees it.

**Consequence:** quota exhaustion is indistinguishable from a revoked token. The operator
sees `54 report(s) failed: youtube_analytics:GoogleApiAuthError`, with no status and no
reason, truncated at 500 chars so roughly 12 of the 54 identical entries survive. One says
"wait until the quota resets"; the other says "re-authorize". Nothing on screen separates
them.

The fix is unusually cheap because the plumbing already exists: `_safe_failure_detail`
(`orchestrator.py:3069-3071`) already surfaces a `.reason` attribute, but
`_GoogleApiHttpError.__init__` never sets one, so it is always `None`. A new
`GoogleApiQuotaError` carrying `.reason` lands the real cause in
`connector_runs.error_summary` with **no orchestrator change**. **3–5h**, and the
reject→accept matrix must cover 401, 403-quota, 403-non-quota, and 403-with-unparseable-body
— `tests/connectors/google/test_http_client.py:167-181` currently parametrizes `[401, 403]`
together and asserts exactly the behaviour being changed.

### What one night actually costs

Because `dimensions=channel` is unsupported for content-owner revenue, there is no bulk
shape — it is one call per channel:

| Host | Calls per run | Notes |
| --- | --- | --- |
| youtubeanalytics (revenue) | **54** | one per channel; `orchestrator.py:3404` loops, `:3419` fetches |
| youtubeanalytics (groups) | 1 + G | G = channel-type CMS groups |
| youtubereporting | 2 + D | D = distinct report periods; one download each |
| adsense | 1 | one `reports:generate` |
| oauth2 | 4 | one forced refresh per `run_one` |

**Multipliers, all confirmed in code:** a 12-month backfill is 12 jobs and **648** Analytics
calls; a fully-degraded run can issue **216+ status attempts** because the retry budgets are
per-request with **no run-wide cap**; each report type added to `SUPPORTED_REPORT_TYPES`
adds its own downloads. There is **no inter-request pacing** — the only `time.sleep` calls
in `connectors/` are the four retry backoffs, and `connector_job_max_workers` defaults to 1,
so the 54 calls go back-to-back on one thread.

**And a dry run costs full price.** Preview a month, then apply it, and you have spent the
day's quota twice.

> ⚠️ **No Google quota figure appears anywhere in this repo.** A grep of `backend/` and
> `Docs/` for `quota` returns two prose sentences with no numbers. Any budget must be taken
> from Google's published documentation and confirmed — do not let a number invented here
> become load-bearing.

### Nothing counts API calls

`connector_runs` records reports and rows, never requests
(`db/connector_models.py:53-79`; `CONNECTOR_RUN_COUNT_KEYS` at `repository.py:21-30` is a
fixed 7-key set about reports and rows). A repo-wide search for
`quota|api_calls|call_count|budget|request_count` returns only complexity- and retry-budget
comments.

**The per-run call counter is the prerequisite for everything else here** — it is what turns
"the sync failed" into "the sync spent its budget". **4–6h.** Once
`counts_json["api_requests"]` exists, a pre-flight budget check needs no new table: sum that
key over runs since the last quota reset, compare against a new
`UMS_GOOGLE_DAILY_REQUEST_BUDGET` parsed by the existing `_load_int`, and refuse **before**
`start_run` so no half-created `RUNNING` row is left behind. **+3–5h.**

### Two failure shapes the runbook must state

- **One failed channel out of 54 means _zero_ revenue facts that night, not 53/54.** A
  PARTIAL run skips normalization entirely. Source rows land; facts do not. The dashboard
  shows nothing new and the Connectors view shows yellow. The honest beta answer is "the
  next SUCCEEDED run for that month rewrites the facts" (`orchestrator.py:522-523`) — which
  costs nothing to accept and is **mandatory to write down**, because otherwise a PARTIAL
  night reads as "it ran".
- **No resume.** A run that dies at channel 40 of 54 redoes all 54. Real resume is 6–10h and
  **high risk** — the stale-cleanup keep-set (`orchestrator.py:1719-1720,1766-1773`)
  preserves historical rows only for channels *not* attempted, so a naive skip either
  deletes the skipped channel's rows or permanently exempts it from cleanup. Any resume
  design needs a reject→accept matrix over `_flush_deferred_stale_cleanup_plans` before a
  line is written.

### Recommendation: Windows Task Scheduler + a thin CLI, mirroring P0.1

Four options were costed. The recommendation is **(c)**:

| Option | Cost | Verdict |
| --- | --- | --- |
| (a) time-of-day inside `GroupSyncScheduler` | 3–5h | **Skip** — schedules group sync, not revenue |
| (b) catch-up-on-start, in-process | +2–4h | Needs a durable marker the scheduler does not have; contradicts its documented no-thunder-on-restart rule |
| **(c) Task Scheduler + `scripts/run_daily_sync.py`** | **6–10h** | **Recommended** |
| (d) leave it manual | 0h | Honest fallback |

Why (c): you learn **one** mechanism, because P0.1's backup is already scheduled this way.
Both jobs then report through `Get-ScheduledTaskInfo` (`LastRunTime`, `LastTaskResult`), and
Windows' `-StartWhenAvailable` gives **catch-up after a missed trigger for free** — which is
the behaviour that actually survives a PC restarted daily.

> **Confirmed, and stronger than the earlier pass said:** with `interval_seconds=86400` the
> in-process scheduler does not merely become unreliable on this PC — it is *structurally
> unreachable*. The interval runs from process start and `scheduler.py` "writes NOTHING
> itself", so there is no last-tick marker to resume from. A box that is up 9 hours a day
> never reaches a 24-hour interval, ever.

The wrapper must inherit P0.1's bookkeeping exactly: `last-run.json` written `RUNNING`
before any work, one terminal record on the way out, an `INTERRUPTED` record from a
`finally`, and an exit code that cannot be `0` when the record did not land. **Under Task
Scheduler stderr goes nowhere** (`Docs/22:373`) — that lesson took five rounds to learn on
the backup; do not re-learn it here.

### Timezone: put the time in the trigger, not in the code

**Recommendation: the run time lives in the Task Scheduler trigger only** (`-Daily -At
HH:MM`, Windows local = Africa/Cairo), written into the runbook beside the backup's 02:00
with the timezone stated explicitly — which `Docs/22:796` does not currently do. **Add no
timezone setting to the codebase.**

> ⚠️ If an in-process scheduler is built instead, the setting **must** be named UTC
> (`UMS_..._RUN_AT_UTC=HH:MM`). The container has no TZ, so an operator who sets `03:00`
> expecting Cairo gets 03:00 UTC — **05:00 Cairo** — silently. A DST shift on an unlabelled
> time is the classic silent failure: the run keeps working and moves an hour.

Also note Google's daily quotas reset on **US/Pacific** midnight, which is a different
boundary again from both Cairo and UTC — so the budget window and the run time are not the
same clock. **Confirm the reset boundary against Google's documentation before implementing
the budget check.**

### Hard prerequisite, inherited from the P0 work

**Compose deliberately does not forward `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`** — the P0
band removed it because `.env.example` ships a public placeholder UUID that satisfied a
fail-closed check. Until the backend rejects that placeholder (a P2 row above), the operator
must supply a real provisioned actor via an untracked `docker-compose.override.yml`
(recipe at `docker-compose.yml:143-152`).

This blocks **every** option, including the manual one: `orchestrator.py:894` calls
`_build_connector_service_principal_or_raise` *before* `start_run`, so without it there is no
run row at all, on any path. **0.25–0.5h.**

### Minimum set to switch on a daily live sync

| # | Item | Hours |
| --- | --- | --- |
| 1 | `docker-compose.override.yml` with a real service-actor UUID | 0.25–0.5 |
| 2 | Stale `token_expiry_at` escape — *without this it works once and never again* | 1–3 |
| 3 | Per-run API call counter | 4–6 |
| 4 | `GoogleApiQuotaError` — separate quota from auth, keep the reason | 3–5 |
| 5 | `scripts/run_daily_sync.py` + one Task Scheduler task + runbook | 6–10 |
| 6 | Runbook lines: PARTIAL = zero facts; dry runs cost quota; timezone stated | 1 |
| | **Total** | **15–25h** |

Pre-flight budget enforcement (+3–5h) and resume (+6–10h) come after, and both depend on
item 3.

### Related: the *inbound* rate limit is dead, and so is Redis

Blocker M4 said compose advertises three protections that do not exist. **Confirmed for rate
limiting, and stronger than published:** `UMS_RATE_LIMIT_PER_MINUTE` appears in exactly one
place in the whole tracked repo — `docker-compose.yml:99` — read by no settings loader, no
middleware, no library and no test.

**Redis is equally dead** — `REDIS_URL` at `docker-compose.yml:101` only, and **zero**
`import redis` anywhere — yet it runs as a service with a volume, a healthcheck, and a hard
`service_healthy` gate that both `app` and `app-dev` block on. The P0 band just gave that
container log rotation. Worth deciding whether it should run at all.

For a single-operator localhost beta, the missing inbound limiter is defensible: every port
binds `127.0.0.1`. But note M4's mitigation sentence is wrong — the `BoundedSemaphore(8)` it
cites is installed only under `UMS_AUTHZ_SOURCE=database`, and the beta runs `headers`, so
**in the configured beta mode there is no inbound concurrency control at all.**

A double-click on "sync" *is* genuinely covered, by three layers (dedup, in-flight skip, and
advisory locks) — **but the CLI path bypasses all three.** Two CLI runs produce correct data
and two `connector_runs` rows for the same scope. Relevant because option (c) *is* a CLI
path: register the Task Scheduler task with `IgnoreNew` so Windows will not start a second
copy.

**Two stale citations to fold into the next `Docs/20` edit:** `Docs/20:409` cites
`docker-compose.yml:89-92`, now `:98`/`:99`/`:101` after the P0 commit; and
`FORWARDED_ALLOW_IPS` (`:100`) sits in the same block but is **not** one of the dead three —
it is a uvicorn variable and the Dockerfile passes `--proxy-headers`.

---

## P3 — Explicitly not doing

Recorded so they are not re-proposed:

- **EGP end to end** — 3–6 weeks. Its own milestone, gated on the decision above.
- **AdSense earnings → revenue facts** — 20–32h. Looks like "set a field the parser
  already has"; is really "introduce a second producer of channel revenue facts and
  defend it against double-counting CMS."
- **Reconciliation-derived TAX** — 12–20h *plus* an unbounded policy question. The
  hardcoded `0.30` is the no-treaty rate; **no code change answers what US withholding
  actually is for an Egyptian content owner.**
- **`POST /org-units`** — 8–16h; two seeded rows do the job.
- **react-router** — unnecessary for one operator.
- **Wiring the currency selector** — that is the EGP program wearing a dropdown.

---

## Suggested sequence

```
Day 0    W0 (1h)  ─ then re-walk the UI and re-scope P1
Day 1    P0.4 → P0.6 → P0.1 (backup + the rehearsed restore)
Day 2    P0.1 finish → P0.2 + P0.3 + P0.5   (one compose commit)
Day 3-4  P0.7 → P0.8 → P0.9                  (the TENANT_CTX trap lives here)
Day 5    P1.5 + P1.6 + P1.1                  (correctness, then the error boundary)
Day 6-7  P1.2 → P1.3 → P1.4                  (mostly deletion)
Day 8    P1 runbook + first real compose run on the PC
```

Two commits carry most of P0: one for backup/restore, one for compose
(P0.2/P0.4/P0.5/P0.3).

**Where execution actually is against that sequence.** Days 1–4 have landed, out of
order: the compose commit, P0.1, W0.2, the unplanned P0.0, and then P0.6 through P0.9
(logging, the roles-seed migration, the bootstrap script, and the org skeleton folded
into it). **The whole P0 band is closed except one file.**

What remains before the beta:

- **the `.env.example` half of P0.3** — the only P0 item still open, and the only one
  blocked on something other than time (the file is write-blocked to the sessions that
  did the rest of the band). The content it needs is written out
  [above](#the-envexample-content-p03-needs-operator-action); acceptance is
  `docker compose --env-file .env.example config` exiting `0`.
- **W0.1** — the repo-root `.env`, including `VITE_DEV_GATEWAY_ROLE=finance_admin`.
- **W0.3** — the re-walk, which is what re-scopes P1. It has still not happened, so
  P1's 10–12h remains the least trustworthy number in this plan.
- **the entire P1 band**, plus the P1 runbook and the first real compose run on the
  target PC.

Note that P2's `UMS_AUTHZ_SOURCE=database` was gated behind P0.6; **that gate is now
open**, though it is still P2 and still not beta work.

---

## Honest limits of this plan

*Updated after the first execution pass. Two of the four caveats below were resolved
by running things; the resolution of the second one is why P0.0 exists.*

- Estimates come from **reading** `main` at `d8418cea2`, with exact `file:line`
  evidence. The frontend suite was not run, no dev server was started, and no path was
  exercised in a browser. Test-breakage counts come from grepping `frontend/tests`, not
  from a red run.
  → **Partly resolved.** The frontend suite and typecheck have now been run, and a Vite
  dev server has been booted from the real config.
- ~~The `/org-units` proxy finding is reasoned from `client.ts:247-249` and Vite's
  SPA-fallback behaviour, **not confirmed by running it.**~~
  → **Confirmed by running it — and the reasoning was wrong.** The SPA fallback does
  not answer that request. With `Accept: application/json` the dev server returns a
  bare **404, zero-length body, no `Content-Type`**; `index.html` appears only for an
  html-accepting navigation. The finding was right; the mechanism published for it was
  not.
- The Windows-specific host findings (Docker Desktop starting at login rather than
  boot, WSL2 bind-mount behaviour, reboot recovery) remain **unverified on real
  hardware** — nobody has run the compose stack on the target PC yet.
  → **Still true.** Every compose result recorded in this plan came from throwaway
  projects on the development machine, and no scheduled task has been registered on
  either machine.
- P1's estimate is the item most likely to move, in your favour, once W0 lands.
  → **Still true, and still unmeasured** — W0.3 (the re-walk) has not happened, so P1
  has not been re-scoped.

**One caveat this plan did not have, and should have:**

- **"Not yet run" was treated as a documentation gap rather than an unopened box.**
  The plan recorded that the compose stack had never been started and moved on to
  costing the items around it. Starting it took under a minute and produced
  [P0.0](#p00--the-compose-stack-could-never-start--done-), a blocker more severe than
  anything four rounds of reading found. Any remaining un-executed path in this plan —
  the scheduled task, the reboot recovery, the first run on the target PC — should be
  read as carrying the same unknown, not as a caveat that has been safely noted.
