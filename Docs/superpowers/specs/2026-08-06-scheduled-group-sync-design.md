# Scheduled CMS group sync — design

Date: 2026-08-06
Status: awaiting approval

## Context

The import/sync/ownership arc (#159 → #169 → #170) ends with grouping that
converges only when an operator curls `POST /channels/groups/sync`. The #169
spec deliberately rejected executor integration then — the sync had to exist
before it could be scheduled. It exists now, and it was built as a reusable
core for exactly this step.

What the repo has today:

- **A synchronous sync route** (`api/channels.py::sync_channel_groups`) that
  owns the whole sequence inline: credential resolution → transaction split →
  CMS fetch → plan → conflict pre-check → apply. Callable only via HTTP, on
  the request thread, with the Google fetch (groups.list + one groupItems.list
  per group) blocking the request.
- **A bounded in-process job executor** (`connectors/runs/executor.py`,
  PR #95) with everything a background sync needs: dedup registry, fail-closed
  enablement (`connector_job_executor_enabled`, default OFF), per-worker
  session + `connector_tenant_context` (re-establishing `TENANT_CTX` and the
  ACTIVE-tenant gate in the worker thread), and the Bucket-A failure-audit
  pattern (fresh session, `platform_lane`, minimal-tenant RLS bridge). It is
  hardwired to `run_one` (report pulls).
- **No scheduler anywhere.** Celery is a dependency pin only — zero imports.
  Nothing in the codebase ticks.

So "scheduled sync" decomposes into three genuinely missing pieces: a sync
core callable without HTTP, a sync job kind on the executor, and a thing that
ticks.

## Goal

A deployment with the feature enabled converges its group mirror
automatically: every N hours, for every ACTIVE tenant, for every active
`youtube-analytics` credential, a background sync job runs the same
plan/apply the manual route runs — same locking, same conflict refusals, same
audit rows. No operator curls. The manual route stays, unchanged, for
on-demand syncs.

## Non-goals

- **Scheduled import re-runs.** The option text floated it; excluded. An
  import consumes an operator-supplied CSV — there is no stored roster to
  re-run. Scheduling it would mean persisting rosters, a different feature.
- **A new HTTP surface for submitting sync jobs.** The scheduler is the only
  submitter. On-demand sync already has a synchronous route; adding a
  fire-and-forget variant is scope this chunk does not need. The executor
  method is public — a future route can add itself in one small PR if wanted.
- **A persistent queue or external scheduler** (celery beat, cron services).
  The executor precedent is in-process and bounded; the scheduler follows it.
  Jobs lost on restart re-converge on the next tick — syncs are idempotent
  by design (UNCHANGED writes nothing), so lost schedule state costs nothing.
- **Per-tenant cadence configuration.** One global interval. The deployment
  is effectively single-tenant today; a `sync_schedules` table can come when
  a second real tenant does.

## Part 1 — extract the sync core

New module `connectors/runs/group_sync.py` (sibling of `orchestrator.py`,
which is the same composition layer for pulls):

```python
def run_group_sync(
    session, *, tenant_id, content_owner_id, registry, groups,
    audit_sink, actor, reason, dry_run, client_factory,
) -> GroupSyncRunResult
```

It owns, in order, exactly what the route body owns today: resolve
credentials (`YOUTUBE_ANALYTICS_CONNECTOR`, `account_id=content_owner_id`) →
end the credential transaction before the fetch (the idle-in-transaction
guard moves with it) → fetch + snapshot via `client_factory` → plan →
dry-run early return → foreign-owner conflict refusal → `apply_group_sync`.
Typed errors propagate (`GoogleConnectorError` family, the conflict pair);
it never raises `HTTPException` — HTTP mapping stays in the route.

The route becomes a shell: permission gate → payload validation → one call →
error-to-status mapping → response rendering. This is the #170 finding-3
lesson applied one PR earlier this time, before review has to ask for it.

**Behaviour-preservation proof:** the existing sync route tests (SQLite +
Postgres tiers, including the atomicity and lockdown suites) pass unmodified.
Any test edit in that suite is a red flag in review, not a fixture chore.

## Part 2 — sync jobs on the executor

The registry key stays a 4-tuple; sync jobs use a reserved connector-key
sentinel so they can never collide with report pulls:

```
(tenant_id, "cms_group_sync", content_owner_id, "-")
```

New public method `submit_group_sync_if_absent(*, tenant_id,
content_owner_id, actor_identity)` reusing the existing reserve/activate
machinery, and a second worker body `_run_group_sync_job`:

- Own session → `connector_tenant_context(tenant_id, session=session)` —
  the ACTIVE-tenant gate replays in the worker, same as pulls.
- Actor: `build_connector_service_principal(tenant_id=...)` extended with
  `MANAGE_GROUPS@global` — the audit row must honestly carry the authority
  the action exercises (`GROUPS_SYNCED` declares `MANAGE_GROUPS`), matching
  the executor's fabricate-with-the-relevant-grant precedent.
- Audit sink: `SqlAlchemyAuditSink` on the worker's own session wrapped in
  `platform_lane` writes — domain rows and audit rows share one transaction
  and one commit, so the #169 atomic invariant holds by construction (the
  worker owns the whole transaction; there is no separate sink session to
  drift).
- Reason: the canned `"scheduled CMS group sync"` (GROUPS_SYNCED is
  reason-required).
- `dry_run=False`, always. A scheduler that only previews converges nothing.

**Audit taxonomy for the worker** (mirrors `_audit_failed_before_start`):

- Apply succeeds with changes → the per-group `GROUP_UPDATED` rows (details
  `source="cms_group_sync"`) come from `apply_group_sync` as with the manual
  route, PLUS one run-level `GROUPS_SYNCED` summary row. The summary row is
  **caller-owned**: the manual route writes it unconditionally after every
  apply (today's behaviour, unchanged — operator actions are always
  audited); the worker writes it only when the executed counts contain any
  non-UNCHANGED outcome.
- Apply finds everything UNCHANGED → **zero audit rows from the worker**
  (the #169 rule: UNCHANGED performs no write and no audit; and the worker
  skips the summary row). A converged fleet on a daily tick writes nothing.
  Liveness is a log line, not an audit row — audit rows are governance
  events, not heartbeats.
- Credential missing/inactive/refresh-failed, fetch failure, conflict
  refusal → one `CONNECTOR_JOB_RUN` row, action `"group_sync_job_failed"`,
  `error_class` = exception class name only (never `str(exc)` — it can embed
  secret locators), via the existing fresh-session platform-lane path.
  A wrong-stamp conflict therefore surfaces in the audit trail every tick
  until the operator runs the #170 remedy (`DELETE
  /groups/{id}/content-owner`, then the right owner's sync re-adopts).

**Concurrency truth, stated plainly:** the registry dedups *jobs*, but a
manual route sync holds no registry slot, so a scheduled job and a manual
sync can run concurrently. That is safe by design, not by throttling — #169
and #170 built the row-locking for exactly this (`FOR NO KEY UPDATE` on
adopt/clear, locked re-reads at apply, uniqueness races → typed conflicts).
Worst case the loser raises the typed conflict, audits a failure, and the
next tick converges. The registry is throttling, not correctness.

Sync jobs share the pull pool (`connector_job_max_workers`, default 1); a
long pull delays a sync tick's jobs. Acceptable at this scale and tunable by
the existing knob.

## Part 3 — the scheduler

New module `connectors/runs/scheduler.py`, `GroupSyncScheduler`:

- One daemon thread: `while not self._stop.wait(interval_seconds): self.tick()`
  — `threading.Event`-based so `close()` is prompt (set + join) and the first
  tick happens one full interval after boot (no thundering sync during a
  deploy restart). `weakref.finalize` GC backstop + explicit `close()`, the
  executor/resolver precedent.
- `tick()`: own session → enumerate ACTIVE tenants with a plain
  `select(TenantORM)` — the `tenants` table is deliberately outside
  `TENANT_SCOPED_TABLES` (the RLS guard query even excludes it), so this
  needs no tenant context and no new policy. Then per tenant:
  `connector_tenant_context` → `SqlAlchemyConnectorCredentialRepository
  .list_credentials` filtered to active `youtube-analytics` rows → per
  credential, `executor.submit_group_sync_if_absent(tenant_id,
  content_owner_id=credential.account_id)`. The active-credential list IS
  the target registry — a tenant opts a content owner into scheduled sync by
  registering its credential, and revoking the credential unsubscribes it.
  No new table.
- **Fault isolation per tenant:** each tenant's enumeration is wrapped; one
  tenant's failure (lifecycle error, DB hiccup) logs and moves on. A
  catch-all around the whole tick keeps the thread alive forever — the
  scheduler fails closed by *skipping a tick*, never by dying silently.
- Already-in-flight jobs → `submit_if_absent` returns None → skip, debug
  log. Overlapping ticks (interval shorter than a slow fleet's sync time)
  are therefore safe.
- Tests drive `tick()` directly — no clocks, no sleeps, fully deterministic.
  The thread loop itself gets one start/close lifecycle test.

## Settings and boot wiring

Two new env-backed settings, loader-validated like the executor's:

| Setting | Env var | Default |
| --- | --- | --- |
| `group_sync_schedule_enabled` | `UMS_GROUP_SYNC_SCHEDULE_ENABLED` | `False` |
| `group_sync_interval_hours` | `UMS_GROUP_SYNC_INTERVAL_HOURS` | `24` (positive int) |

Boot rules in `create_app`:

- Schedule enabled + executor disabled → `ValueError` at app construction.
  A scheduler with nothing to submit to is a misconfiguration; it fails fast
  at boot (the settings-loader "typo fails closed" precedent), not silently
  at first tick.
- Schedule enabled + no database URL → inert, matching the executor's own
  behaviour today (the whole block sits under `if resolved_database_url`).
- Service actor unset → the first worker raises `ValueError` from
  `build_connector_service_principal` and audits nothing; the scheduler
  requires it, so boot ALSO fails fast when schedule enabled +
  `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` unset — same fail-fast rationale,
  and it mirrors the jobs route's `service_actor_configured` preflight.
- Lifespan teardown order: scheduler first (stop ticking), then executor
  (drain workers).

## Error taxonomy (worker-side)

| Condition | Result |
| --- | --- |
| Tenant no longer ACTIVE at worker start | `TenantLifecycleError` → failure audit (existing pre-start path) |
| No active credential for the owner | failure audit, `error_class=CredentialNotFoundError` |
| Token refresh fails | failure audit, `error_class=OAuthRefreshError` |
| CMS fetch fails | failure audit, `error_class` = the `GoogleConnectorError` subclass |
| Foreign-owner conflict / lost race | failure audit, `error_class` = the conflict class |
| Everything UNCHANGED | no rows, log line only |
| Changes applied | per-group `GROUP_UPDATED` rows from the apply + one `GROUPS_SYNCED` summary row from the worker (the manual route writes the summary unconditionally; the worker only on change) |

## Testing

- **Extraction (Part 1):** existing sync-route suites pass unmodified —
  that is the proof, enforced by review. New unit tests for
  `run_group_sync`'s dry-run early-return and typed-error propagation.
- **Executor job kind (Part 2):** registry-key non-collision with pulls;
  dedup; worker happy path with a fake client factory (store rows land,
  GROUPS_SYNCED rows land, one commit); each failure row in the taxonomy
  table above (fake raising at each seam); UNCHANGED → zero audit rows
  (anti-vacuity: the same fixture with a change writes rows).
- **Scheduler (Part 3):** `tick()` with two ACTIVE + one suspended tenant
  and mixed credentials submits exactly the expected (tenant, owner) set;
  per-tenant fault isolation (first tenant raises, second still submits);
  in-flight dedup skip; start/close lifecycle.
- **Boot:** enabled-without-executor and enabled-without-service-actor both
  refuse to build the app; disabled builds no thread.
- **Postgres tier:** one end-to-end: seed tenant + credential + CMS fake →
  `tick()` → job runs → group rows + GROUPS_SYNCED rows on the real engine
  under RLS lanes; and one failure-path audit (credential missing) proving
  the Bucket-A row lands cross-lane. Existing atomicity/serialization proofs
  from #169/#170 already cover the apply internals — not re-proven here.

## Tracker updates (per-PR rule)

- `Docs/15_DELIVERY_BACKLOG.md`: extend the CMS-group-sync entry —
  scheduled convergence shipped (executor job kind + in-process scheduler,
  fail-closed OFF by default).
- `Docs/01_IMPLEMENTATION_PLAN.md`: one-line note on the group-mapping item.
- `Docs/12_BACKEND_API_SPEC.md`: sync section gains the scheduled-mode
  paragraph (settings, actor, audit taxonomy). No new HTTP endpoints.

## Rollback

Revert the branch. No migration. Both settings default OFF, so a revert (or
simply never setting the env vars) leaves exactly today's behaviour: manual
sync only. Jobs in flight at downgrade time complete or die with the
process; the next manual sync converges whatever they left.
