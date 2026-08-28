# 21 — First-Beta Implementation Plan

**Built from:** [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md)
(4 rounds, at `main` = `d8418cea2`).
**Target:** one operator, one Windows PC, `docker compose`, localhost only, real CMS
revenue entering by **manual import**.
**Method:** every item below was costed against the actual code — file, line, what
breaks, what it unblocks — not estimated from the finding text.

> ⚠️ **Freshness banner (2026-08-28, post-audit).** P0 **implementation** is **not** on
> merge-ready #210 (blocked, wrong base). Track execution on **restacked P0 split PRs**
> (P0-a…P0-e — see [`25_PROGRAM_DEPENDENCY_GRAPH.md`](25_PROGRAM_DEPENDENCY_GRAPH.md)).
> This copy is the **costing snapshot** at `main` = `d8418cea2`. Do not schedule open
> items from the hour tables alone until P0 split PRs land on `main`.
>
> **Consolidation:** ships with Docs/20/23/24/25 in `docs/program-plans-consolidated`
> (supersedes closed drafts #209 / #218 / #219).

### Related plans (program triad)

| Doc | Where | Role |
| --- | --- | --- |
| [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md) | this PR | Historical audit snapshot |
| P0 split PRs (P0-a…P0-e) | `main` (TBD numbers) | **Living** P0 implementation — replaces blocked #210 |
| [`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md) | this PR | Admin / access / config UI extension |
| [`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md) | this PR | US revenue + withholding estimate |
| [`25_PROGRAM_DEPENDENCY_GRAPH.md`](25_PROGRAM_DEPENDENCY_GRAPH.md) | this PR | Explicit execution DAG |

**Residual after P0-e:** `/security` still missing from Vite `TENANT_SCOPED_ROUTES` until
P0-e merges (Docs/23 A2).

---

## The headline

**At snapshot time the remaining work was 38–55 hours. Six to eight focused days.**

That was the distance between `main` @ `d8418cea2` and a beta you can put real money
data into. Not another phase, not a rewrite. The application is genuinely built; what
was missing is the layer that lets one person *run* it and not lose data. **P0
implementation is restacked onto `main` as P0-a…P0-e split PRs** — treat the tables
below as the original costing, not the live backlog.

Three things are worth saying plainly before the table:

1. **Nothing on the critical path is a redesign.** The largest single item is a
   backup script. The second largest is a bootstrap script. The rest is configuration,
   deletion, and one logging call.
2. **The biggest *visible* problem is one line.** The app looks like a dead mockup
   largely because the dev identity ships with 2 of **26** permissions. Fixing that
   costs a minute and changes the entire impression of the product.
3. **EGP was deferred in this snapshot.** The section
   [The one decision only you can make](#the-one-decision-only-you-can-make) records the
   open question as of `d8418cea2`. EGP program sequencing is tracked separately once
   P0 split PRs are on `main` — do not re-decide from this frozen text alone.

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
| **P3** | [Explicitly not doing](#p3--explicitly-not-doing) | — | — |

---

## W0 — Unblock yourself (1 hour)

Do this before anything else, including reading the rest of this plan. Every other
item is easier to judge once you can actually operate the UI.

| # | Change | File | Time |
| --- | --- | --- | --- |
| W0.1 | Create repo-root `.env`; set `VITE_DEV_GATEWAY_ROLE=beta_operator` and `UMS_TRUSTED_GATEWAY_TOKEN` | `.env` (new, repo root) | 15 min |
| W0.2 | Add `"/org-units"`, `"/users"`, and `"/security"` to `TENANT_SCOPED_ROUTES` | `frontend/vite.config.ts:13-32` | 5 min |
| W0.3 | Restart the dev server, click through every view, write down what is still dead | — | 30 min |

**W0.1 is the single highest-leverage change in this document.** The shipped default
role is `assistant_analyst` (`vite.config.ts:69`), which `auth/seed.py` grants exactly
two permissions — `VIEW_ANALYTICS` and `VIEW_CONFIDENCE` — out of **26**. Every write
action and most reads are denied before they reach any logic. You have been demoing
the product through its second-most-restricted role.

> **Beta operator role (P0-c):** `finance_admin` alone **cannot** manual-import — `POST
> /revenue/facts` requires `connectors.run_jobs` (`api/revenue.py:1028`) and
> `FINANCE_ADMIN` does not hold it (`auth/seed.py`). The prescribed beta identity is
> **`beta_operator`**: finance read/write surface **plus** `connectors.run_jobs` scoped
> to `manual-upload` only (least privilege — no connector scheduler, no Google lanes).
> Bootstrap: `bootstrap_operator.py --role beta_operator`. Use `corporate_admin` only
> for user-creation sessions (`users.manage`); use `finance_admin` only when demoing
> finance UI without import.

**Acceptance criteria (W0):**
- [ ] `.env` at repo root with `VITE_DEV_GATEWAY_ROLE=beta_operator` and gateway token
- [ ] Manual import (`POST /revenue/facts`, `connector_key=manual-upload`) succeeds (201)
- [ ] Finance views render real data (not 403) under `beta_operator`
- [ ] `/org-units`, `/users`, `/security` proxied in dev (no 404 on Registry / Admin matrix)

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

### P0.2 — Artifact and blob volume — **2–3h**

Export artifacts and connector blobs live on ephemeral container paths
(`reports/artifact_storage.py:13`, `orchestrator.py:3125`, `Dockerfile:109`). A
container replacement discards them, and a requested export then **503s permanently**.

Fix by mounting a **host bind mount** (e.g. `./data/artifacts:/var/lib/ums/artifacts`) —
**not** a Compose-managed named volume. `docker compose down -v` destroys
`postgres-data` and `redis-data`; any co-located named app volume is destroyed with them.
Restore would leave export records pointing at missing blobs.

> 💡 There is an undocumented workaround for the 503 in the meantime: `request_export`
> has no dedup on scope+month (`reports/exports.py:383-433`), so the operator can
> simply request the export again. Note this in the runbook.

**Acceptance criteria (P0.2):**
- [ ] Artifact path is a host bind mount outside Compose named volumes
- [ ] `docker compose down` (without `-v`) preserves artifacts across container recreate
- [ ] Document that `down -v` wipes DB **and** must not be used when artifacts must survive

### P0.3 — Compose env vars + `.env.example` — **1–2h**

Compose does not pass the storage vars, and there is no template. This is also what
W0.1 needs a canonical home for.

**Skippable?** The template is mandatory. Some vars can wait.

### P0.4 — Log rotation in compose — **20 min** 🔴

Docker Desktop's VHDX grows and **does not shrink**. The baseline is already ~5,760
healthcheck access lines/day, and P0.5 raises volume further.

Twenty minutes, on a box that will run unattended for months. Do it **before** P0.5.

**Skippable?** No, and there is no excuse.

### P0.5 — `stop_grace_period` — **30 min**

One line, so in-flight work finishes instead of being killed mid-write.

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

### P0.7 — Roles/permissions seed as a migration — **2–4h**

`db/security_seed.sql` is maintained and idempotent, but nothing tells you to run it,
and it is an FK prerequisite for assigning any role.

### P0.8 — Bootstrap script (`bootstrap_operator.py`) — **4–8h** ⚠️

Creates the first operator user, and — with `--org-skeleton` — one `SECTOR` plus one
`COMPANY` beneath it.

> ⚠️ **This is the rabbit hole of the whole plan.** It looks like "insert one row." On
> Postgres, `SET LOCAL ROLE app_tenant` + `tenant_id = app_current_tenant_id()` will
> reject every insert unless `TENANT_CTX` is set first — and the script you would
> naturally copy from, `seed_demo_month.py`, **does not do it** (it is SQLite-correct
> only). If this costs you a day, that is why.

### P0.9 — Org-unit skeleton — **+1–2h** (folded into P0.8)

~40 lines lifted almost verbatim from `seed_demo_month.py:414-455`, which is already
the repo's only org-unit writer and is clean and idempotent.

Two rows seed sector/company org units and unblock `POST /channels`, but **do not**
clear per-channel registry issues — `_issues_for_channel` still emits `MISSING_COMPANY`
until each channel has `primary_company_id` set.

> **Do not build `POST /org-units` for the beta.** Router + writer repository +
> `MANAGE_ORG_MAPPING` gating + audit events + cycle validation + tests + a frontend
> that does not exist = 8–16h that buys one operator nothing.
>
> **Mandatory follow-up:** assigning channels to companies is one
> `PATCH /channels/{id}/mapping` per channel (`api/channels.py:1425`) — no bulk path.
> **P0.9 acceptance requires the scripted mapping loop**, not just the two-row skeleton.

**Acceptance criteria (P0.8 + P0.9):**
- [ ] Bootstrap creates operator + optional org skeleton (sector + company)
- [ ] Scripted loop maps every imported channel to a company (`PATCH …/mapping`)
- [ ] Registry shows zero `MISSING_COMPANY` for mapped channels after loop completes
- [ ] `POST /channels` succeeds for new channels once parent company exists

---

## P1 — Stop looking like a mockup

**10–12 hours** for the entire visible win. This band directly answers *"it's really a
landing page, but mockup."*

**Re-scope this band after W0.3.** With `beta_operator` instead of `assistant_analyst`,
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

> ⚠️ **Status vs P0 split.** This section is the **snapshot-time** open question at
> `d8418cea2`. EGP program sequencing is tracked after P0 split PRs land on `main`.

Everything about currency hung on one question that had been open since PR #42 and is
recorded at `Docs/16_OPEN_DECISIONS.md:70-71`:

> **Are USD facts acceptable for the beta, with the EGP bank settlement explained as FX
> variance — yes or no?**

**If yes** (was recommended for the beta at snapshot time): nothing changes. The
pipeline is internally consistent and the numbers are real. Do P2.2 when convenient.

**If no:** that is the EGP program — 3–6 weeks, ~2,154 `*_usd` identifiers, and a
USD-only design that is *test-locked* by
`tests/finance/test_finance_no_fx_dependency.py:40-53`. It should be its own milestone
after the beta proves the rest works. **EGP path adopted in separate program doc.**

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

---

## P3 — Explicitly not doing

Recorded so they are not re-proposed:

- **EGP end to end** — 3–6 weeks. Its own milestone, gated on the decision above.
- **AdSense earnings → revenue facts** — 20–32h. Looks like "set a field the parser
  already has"; is really "introduce a second producer of channel revenue facts and
  defend it against double-counting CMS."
- **Reconciliation-derived TAX** — 12–20h *plus* an unbounded policy question. The
  hardcoded `0.30` is the no-treaty rate in
  `finance/reconciliation_workflow.py`. The **rate ruling and display-estimate
  program** (15% treaty copyright royalty; never arm recon from the estimate alone)
  now live in [`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md).
  Keep recon dormant until a separate ruling.
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

---

## Honest limits of this plan

- Estimates come from **reading** `main` at `d8418cea2`, with exact `file:line`
  evidence. The frontend suite was not run, no dev server was started, and no path was
  exercised in a browser. Test-breakage counts come from grepping `frontend/tests`, not
  from a red run.
- The `/org-units` proxy finding is reasoned from `client.ts:247-249` and Vite's
  SPA-fallback behaviour, **not confirmed by running it.**
- The Windows-specific host findings (Docker Desktop starting at login rather than
  boot, WSL2 bind-mount behaviour, reboot recovery) remain **unverified on real
  hardware** — nobody has run the compose stack on the target PC yet.
- P1's estimate is the item most likely to move, in your favour, once W0 lands.
