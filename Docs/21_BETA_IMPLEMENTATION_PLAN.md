# 21 — First-Beta Implementation Plan

**Built from:** [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md)
(4 rounds, at `main` = `d8418cea2`).
**Target:** one operator, one Windows PC, `docker compose`, localhost only, real CMS
revenue entering by **manual import**.
**Method:** every item below was costed against the actual code — file, line, what
breaks, what it unblocks — not estimated from the finding text.

> ⚠️ **Freshness banner (2026-08-31, post-audit).** P0 **implementation** is tracked by
> current successor PRs **#221–#225 (P0-a…P0-e)**. PR #210 is historical: it merged on
> 2026-08-29 into the non-main `docs/deployment-readiness-audit` branch and is not the
> source of truth on `main`. At this check, #221 and #225 are open/BLOCKED, while
> #222–#224 are open/BEHIND; none is merged. This copy remains the
> **costing snapshot** at `main` = `d8418cea2`. Do not schedule open items from the
> hour tables alone until the successor PRs land on `main`.
>
> **Consolidation:** ships with Docs/20/23/24/25 in `docs/program-plans-consolidated`
> (supersedes closed drafts #209 / #218 / #219).

### Related plans (program bundle)

| Doc | Where | Role |
| --- | --- | --- |
| [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md) | this PR | Historical audit snapshot |
| P0 split PRs (P0-a…P0-e; #221–#225) | `main` | **Living** P0 implementation; supersedes historical #210 |
| [`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md) | this PR | Admin / access / config UI extension |
| [`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md) | this PR | US revenue + withholding estimate |
| [`25_PROGRAM_DEPENDENCY_GRAPH.md`](25_PROGRAM_DEPENDENCY_GRAPH.md) | this PR | Explicit execution DAG |

**Residual after P0-e/#225:** `/security` remains missing from Vite
`TENANT_SCOPED_ROUTES`; Docs/23 A2 owns that proxy addition.

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
2. **The biggest *visible* problem starts with one line, but the safe fix is ordered.**
   The app looks like a dead mockup largely because the dev identity ships with 2 of
   **26** permissions. A finance-only existing role is useful for a read-only smoke;
   the real manual-import identity must wait for P0-c/P0-e and the two-scope database
   principal contract below.
3. **EGP was deferred in this snapshot.** The section
   [The one decision only you can make](#the-one-decision-only-you-can-make) records the
   open question as of `d8418cea2`. The operator decision remains at
   `Docs/16_OPEN_DECISIONS.md:70-71`; Docs/24 records sequencing constraints only.
   Do not re-decide from this frozen text alone.

---

## Priorities at a glance

| | Band | What it buys | Hours |
| --- | --- | --- | --- |
| **W0** | [Post-bootstrap UI smoke](#w0--post-bootstrap-ui-smoke-1-hour-after-p0-c--p0-e) | You can finally *see* the product | **~1** |
| **P0** | [Don't lose the data](#p0--dont-lose-the-data) | Real money data is safe to enter | **9–12** |
| **P0** | [Be able to operate it](#p0--be-able-to-operate-it) | First run works; failures leave a trace | **11–20** |
| **P1** | [Stop looking like a mockup](#p1--stop-looking-like-a-mockup) | It reads as a product | **10–12** |
| **P1** | [Two correctness fixes worth the hours](#p1--two-correctness-fixes-worth-the-hours) | Confidence labels mean something | **2–3** |
| **P1** | [Runbook + rehearsal](#p1--runbook--rehearsal) | It survives a reboot | **4–6** |
| | **Beta total** | | **38–55** |
| **P2** | [After the beta runs](#p2--after-the-beta-runs) | Live connectors, polish | 25–40 |
| **P3** | [Explicitly not doing](#p3--explicitly-not-doing) | — | — |

---

## W0 — Post-bootstrap UI smoke (1 hour; after P0-c + P0-e)

Do **not** point `VITE_DEV_GATEWAY_ROLE` at a planned role before its catalog migration
exists. At this reviewed tree, `RoleKey` has no `beta_operator`; header parsing rejects
that value with HTTP 400 before any route runs (`auth/roles.py:21-37`,
`api/dependencies.py:102-108`). An immediate read-only diagnostic may use the existing
`finance_admin` role, but it cannot prove manual import because `POST /revenue/facts`
requires `connectors.run_jobs` at `connector:manual-upload` (`api/revenue.py:1027-1028`).

Run W0 only after P0-c and P0-e deliver this contract:

| # | Change | Owner | Time |
| --- | --- | --- | --- |
| W0.1 | Bootstrap one database principal with existing `finance_admin` at `global` **and a separate direct** `connectors.run_jobs` grant at `connector:manual-upload`; do not bundle connector authority into a global role | P0-c/#223 successor | prerequisite |
| W0.2 | Copy the bootstrap-returned UUID/email into repo-root `.env` as `VITE_DEV_GATEWAY_USER_ID` / `VITE_DEV_GATEWAY_USER_EMAIL`, set the gateway token, enable `UMS_AUTHZ_SOURCE=database`, and land the `/org-units` + `/users` proxies | P0-c + P0-e | prerequisite |
| W0.3 | Restart, verify `/session/me`, run one **fixture-only** `manual-upload` smoke, click through every view, and record what is still dead | runbook | 1 hour |

The split grant is mandatory. A role assignment applies every permission in that role at
the same scope (`auth/policy.py:55-60`). A global role containing
`connectors.run_jobs` therefore authorizes Google/scheduler connector keys too; it
cannot mean "manual-upload only." Header mode also carries only one role/scope tuple
(`api/dependencies.py:110-120`), so it cannot express global finance plus a connector-
scoped grant. Database principal loading is what combines the global finance assignment
and scoped direct grant (`auth/principals.py:142-198`).

**Acceptance criteria (W0):**
- [ ] No `beta_operator` global bundle: existing `finance_admin@global` plus direct `connectors.run_jobs@connector:manual-upload`
- [ ] `/session/me` resolves the bootstrap UUID and exposes the stored assignments/grant under `UMS_AUTHZ_SOURCE=database`
- [ ] Repo-root `.env` uses the exact printed UUID/email; the all-zero Vite fallback is not used
- [ ] Fixture-only `POST /revenue/facts` with the manual-upload connector identifier succeeds; a Google connector identifier under the same principal returns 403
- [ ] The write's `audit_logs.user_id` equals the bootstrap UUID (not NULL with only `details.actor_user_id` fallback)
- [ ] Finance views render (not 403); P0-e proxies `/org-units` and `/users`; A2 later proxies `/security`

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

- `pg_dump -Fc` to a **protected external/off-PC** destination, not merely a
  container path or another directory on the same disk as Docker's VHDX and the
  artifact bind mount. Keep retention there, and rehearse restore from that
  detached copy.
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

Fix with one explicit host-to-container contract:

- Container environment: `UMS_EXPORT_ARTIFACT_DIR=/var/lib/ums/artifacts` and
  `UMS_LOCAL_STORE_ROOT=/var/lib/ums/blobs`.
- Compose mount: host source `./data/ums:/var/lib/ums` on both `app` and `app-dev`;
  mount `migrate` only if it writes artifacts or blobs.
- Before the first write, anchor `/data/` in repo-root `.gitignore` and exclude `data`
  from `.dockerignore`. This directory will contain sensitive finance evidence and
  generated binaries; `git add .` and Docker build context collection must not see it.
- `./data/ums` is the host-side source path. Never put that relative host path in
  either environment variable inside a container; both values must remain absolute
  container targets. An externally managed Compose volume is an alternative only if
  it is mounted at `/var/lib/ums` and survives `docker compose down -v` cleanup.

`down -v` destroys `postgres-data` and `redis-data`; any ordinary co-located named
app volume is destroyed with them. Restore would leave export records pointing at
missing blobs.

**Permission/persistence smoke:** run as the runtime user in `app` and the
`app-dev` profile, verify read/write access to both configured directories, write
sentinels under `/var/lib/ums/artifacts` and `/var/lib/ums/blobs`, recreate with
`docker compose down` (without `-v`) plus `docker compose up` (and
`docker compose --profile dev up app-dev` for the dev service), and verify both
sentinels remain. Remove the sentinels after the check.

> 💡 There is an undocumented workaround for the 503 in the meantime: `request_export`
> has no dedup on scope+month (`reports/exports.py:383-433`), so the operator can
> simply request the export again. Note this in the runbook.

**Acceptance criteria (P0.2):**
- [ ] Repo-root `.gitignore` contains anchored `/data/`; `.dockerignore` excludes `data`, before any sentinel or real artifact is written
- [ ] `app` and `app-dev` use `./data/ums:/var/lib/ums`, with the two absolute container env targets above; `migrate` is mounted only if it writes
- [ ] `docker compose down` (without `-v`) preserves artifacts across container recreate
- [ ] Document that `down -v` wipes DB **and** must not be used when artifacts must survive
- [ ] Permission and persistence smokes pass for both targets under the runtime user

### P0.2a — USD-only, resumable manual-import gate — **required before real data**

`RevenueFactImportRequest` has `gross_revenue_usd` and related `*_usd` fields but no
source-currency field (`api/revenue.py:386-402`). The endpoint cannot distinguish a USD
amount from an EGP amount pasted into a USD-named field. "Manual" therefore does not
make currency provenance disappear.

The beta import runner and API boundary must fail closed as one contract:

1. The source manifest records month, explicit `source_currency=USD`, source report id
   and file hash, expected row count, expected active `revenue_required` channel ids,
   and gross control total. Missing, mixed, or non-USD source currency aborts **before
   the first POST**; no client-side conversion is allowed.
2. Add a typed `source_currency: Literal["USD"]` request field (or an equivalently
   strict typed boundary) so direct callers must make the unit assertion too. The
   runner verifies that assertion against the source report metadata; renaming an EGP
   column is not verification.
3. Use one stable `source_report_id` for provenance across the batch. Idempotency comes
   from the existing tenant + month + channel + source-kind upsert identity
   (`finance/revenue_facts.py:100-130`; `source_report_id` is **not** part of that key),
   so a retry replaces the same `MANUAL_UPLOAD` facts instead of duplicating them. A
   changed report id does not create a second row and must be treated as a provenance
   mismatch during verification, not as a new batch identity.
4. A corrected manifest that removes a channel needs an explicit open-month
   batch-replacement cleanup; row-by-row upsert alone cannot converge because no public
   delete path removes stale `MANUAL_UPLOAD` facts outside the new manifest. The runner
   must hold one tenant/month/source lock, require the same finance/manual-import
   authority and non-blank reason, refuse locked months, remove or supersede only prior
   manual-upload facts not present in the replacement manifest, write audited provenance
   for every removed/superseded fact, and make the replacement idempotent by manifest
   hash/report id. A retry after interruption repeats the same replacement and leaves
   one exact active set; a reused idempotency key with different content fails closed.
5. On interruption, rerun the complete manifest. A successful single 201 is only a
   row smoke, never a batch-success signal.
6. After the loop, take the complete active roster from the current, unpaginated
   `GET /channels` response and compare its `revenue_required` set with the manifest.
   Then read `GET /revenue/channels/{channel_id}/months/{month}/facts` for **every active
   channel**, not only a sample or the manifest members. Require the set of active
   channels carrying a `MANUAL_UPLOAD` fact to equal the manifest exactly, with one
   intended fact per channel and matching amounts/report id, row count, and control
   total. An active non-required channel with a stale manual fact is an extra and fails
   the batch. Roster drift between preflight and post-check also aborts and restarts the
   comparison. Any missing/extra/mismatched row exits non-zero; the month is not
   presented as complete or eligible for close.

**Acceptance criteria (P0.2a):**
- [x] Missing/mixed/EGP source metadata turns the preflight RED with zero facts written
  (implemented + tested: non-USD rejection and exactly-USD-literal tests)
- [x] Kill-after-N test leaves a partial month; rerun converges to the exact intended set without duplicates
  (runner-level test drives the real script over a TestClient socket:
  test_runner_converges_partial_month_and_rerun_is_idempotent)
- [x] Reduced-manifest test starts with an extra stale manual fact; audited open-month
  replacement removes/supersedes that extra and retry remains idempotent
  (implemented + tested: provenance-preserving removal, replay idempotency)
- [x] Post-import comparison covers the complete active roster, proves every active
  `revenue_required` channel is in the manifest, and rejects stale manual facts outside it
  (runner-level tests: missing revenue_required channel is preflight RED; the post-check
  fails on a stale manual fact and when verification cannot match)
- [x] The runner reports complete only when the exact set/amount/report-id/control-total
  comparison passes; absence of `CHANNELS_MISSING_REVENUE_FACTS` alone is never proof
  (runner-level wrong-total/verification-mismatch tests abort before completion;
  the battery — 7 tests over the real app — also caught and fixed two API-shape
  defects the service tests could not see: the channels roster is a bare array and
  facts list under `facts`)
- [x] Batch output records source hash/id and the attributed bootstrap UUID without storing secrets
  (implemented + tested: ledger-row fields, actor, completion, hash canonicalization)
- [x] Replacement/import idempotency is backed by an immutable PostgreSQL batch
  ledger (or an equivalent append-only audit ledger) recording batch key,
  manifest hash, report/source ids, status, actor, and completion time inside
  the replacement transaction; stale delayed retries must compare against that
  ledger before they can rewrite facts
  (implemented + tested: single-ledger-row replay, hash-conflict refusal,
  constrained migration + RLS)
- [x] The dependency graph and hard gate list include that ledger as part of the
  manual-import gate; the implementation cannot be marked unblocked by the
  runner checks alone (the hard-gate row in Docs/25 names the implementation)

**Migration/API impact (P0.2a):** the implementation PR requires a PostgreSQL
migration for the immutable import-batch ledger described above; no historical
backfill is required before real beta data because the gate must land first. Making
`source_currency="USD"` a required typed request field is an intentional API-contract
change; every direct caller, fixture, and generated client must be updated in the same
implementation PR. Existing USD rows remain valid; no historical currency is guessed.

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
and it is an FK prerequisite for assigning any role. Seed the existing catalog; do
not add a global `beta_operator` role that combines finance and connector authority.
P0-c must instead support the two-scope bootstrap contract in W0.

### P0.8 — Bootstrap script (`bootstrap_operator.py`) — **4–8h** ⚠️

Creates the first operator user, prints the stored UUID/email, assigns existing
`finance_admin` at global scope, and — only with an explicit manual-import flag —
creates an audited direct `connectors.run_jobs` grant at
`connector:manual-upload`. It must never grant that connector permission globally.
The trust root is the pre-login local process itself, so the bootstrap grants are
SELF-grants by the just-created account: a fresh database has no prior actor row, so
`assigned_by` is the account's own id, that same self-actor is stamped on every audit
row (the first privilege grant is never silent), and an audit-write failure rolls the
grant back rather than leaving an unaudited privileged write. This self-grant power
exists only inside the local script's direct tenant-lane database access before any
login exists — it is reachable through no HTTP route — and the script grants nothing
the operator did not explicitly request: global `finance_admin`, plus (with the flag)
the one scoped `connectors.run_jobs` grant, nothing more.
The returned identity is an output contract: P0-e installs it as
`VITE_DEV_GATEWAY_USER_ID` / `VITE_DEV_GATEWAY_USER_EMAIL` before database authz or
any real-revenue write is attempted.

> ⚠️ **This is the rabbit hole of the whole plan.** It looks like "insert one row." On
> Postgres, `SET LOCAL ROLE app_tenant` + `tenant_id = app_current_tenant_id()` will
> reject every insert unless `TENANT_CTX` is set first — and the script you would
> naturally copy from, `seed_demo_month.py`, **does not do it** (it is SQLite-correct
> only). If this costs you a day, that is why.

### P0.9 — Truthful org hierarchy bootstrap — **re-cost after manifest design**

A one-sector/one-company skeleton is acceptable for disposable demo data only. It is
not an executable real-data mapping plan: assigning every UMS channel to one placeholder
company would make company and sector revenue rollups false.

Keep the beta CLI path instead of building a general `POST /org-units`, but add an
operator-reviewed org manifest containing the real sectors, companies, and
channel→company relationships. The bootstrap validates parent types, duplicate ids,
unknown channels, and conflicting re-runs; it creates the real hierarchy idempotently,
then applies only mappings present in that manifest through the audited mapping path.
If the operator does not yet know a channel's owner, leave `primary_company_id` NULL,
keep its `MISSING_COMPANY` issue visible, and exclude/label company and sector rollups
as incomplete. Never clear an issue by inventing ownership.
Before real revenue is entered, the backend must either suppress company/sector
rollups while revenue-bearing channels remain unmapped or expose completeness metadata
on rankings responses, including an incomplete flag and excluded-channel count, so
partial totals cannot look authoritative.

> **Do not build a general `POST /org-units` for the beta.** A manifest-driven bootstrap
> is enough for one operator. The existing per-channel write remains
> `PATCH /channels/{youtube_channel_id}/mapping` (`api/channels.py:1425`); the script
> must compare the stored mapping back to the reviewed manifest after the loop.

**Setup authority is temporary and ordered.** The final split-grant finance principal
does not hold `registry.manage_channels` or `registry.manage_org_mapping`. After
bootstrap has created the stored user, a local privileged CLI creates the reviewed org
hierarchy through tenant-scoped repositories and reasoned audit events. Roster and
mapping writes then use either that CLI or a setup-only `corporate_admin` header carrying
the exact stored UUID/email, all before database-authz cutover. This setup must not
persist a `corporate_admin` assignment on the beta finance principal. No real revenue
is written in temporary header mode; every mapping audit row must resolve
`audit_logs.user_id` to the stored bootstrap UUID.

**Acceptance criteria (P0.8 + P0.9):**
- [ ] Bootstrap prints the stored operator UUID/email and creates the split finance/manual-import grants atomically
- [ ] Repo-root `.env` uses that UUID/email; `/session/me` and a write audit resolve the same non-null actor
- [ ] The fixture fact's `imported_by` and its audit row's `user_id` both equal that stored UUID
- [ ] Real data uses an operator-reviewed hierarchy/mapping manifest; the demo skeleton is refused for a real-data run
- [ ] Script maps only truthfully resolved channels and verifies exact stored channel→company equality
- [ ] Unresolved channels retain `MISSING_COMPANY`; company/sector totals are visibly incomplete, never silently attributed to a placeholder
- [ ] `POST /channels` succeeds only when the supplied real parent company exists

---

## P1 — Stop looking like a mockup

**10–12 hours** for the entire visible win. This band directly answers *"it's really a
landing page, but mockup."*

**Re-scope this band after W0.3.** With the database-loaded split-grant operator
instead of `assistant_analyst`, some of these panels will already render real data.

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

Must cover: first-run order (seed → bootstrap split grants and capture UUID/email →
setup-only hierarchy/roster/mapping with that stored actor → copy the UUID/email into
repo-root `.env` → enable database authz → proxy/session + fixture-write attribution
smoke → USD manifest preflight → resumable import → complete-roster whole-batch
verification), the reboot recovery path (nothing restarts itself), the
restore drill, **B1/B2 written down as accepted risks** justified by the localhost
binding, the truthful hierarchy manifest/unresolved-channel policy, the export-503
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
after the beta proves the rest works. The operator decision remains open at
`Docs/16_OPEN_DECISIONS.md:70-71`; Docs/24 records the U2/EGP sequencing constraint
only and is not the EGP implementation plan.

> ⚠️ **Do not let anyone "shortcut" this through `currency_exchange_rates`.** It looks
> like a 2-hour win and is rejected by an existing guard test and the surrounding
> finance contracts. The operator decision recorded in `Docs/16:70-71` remains open in
> this snapshot. If EGP is approved, the sanctioned route is Google's own server-side
> conversion (`currency=EGP`), never a UMS-derived rate. The quoted operator preference
> remains context, not a closed decision: *"i dont need to make it USD × 47.5, i need pure number."*

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
| Database-authz rollback/recovery drill | 2–4h | Minimal cutover moved into P0-c/W0 for the split-scope beta principal; rehearse wrong/disabled UUID and storage-failure rollback after P0.6 logging |
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
- **General `POST /org-units` UI/API** — 8–16h; the beta uses an operator-reviewed
  hierarchy manifest. A two-row placeholder is demo-only and must never stand in for
  real multi-company ownership.
- **react-router** — unnecessary for one operator.
- **Wiring the currency selector** — that is the EGP program wearing a dropdown.

---

## Suggested sequence

```
Day 0    Existing `finance_admin` read-only diagnostic only; no manual import
Day 1    P0.4 → P0.6 → P0.1 (backup + the rehearsed restore)
Day 2    P0.1 finish → P0.2 + P0.3 + P0.5   (one compose commit)
Day 3+   P0.7 → P0.8 → P0.9                  (re-cost truthful manifest before dates resume)
Next     P0-e → W0 (1h)                       (install UUID, database authz, fixture smoke)
Next     P1.5 + P1.6 + P1.1                  (correctness, then the error boundary)
Next     P1.2 → P1.3 → P1.4                  (mostly deletion)
Final    P1 runbook + USD preflight + resumable full import + post-import proof
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
