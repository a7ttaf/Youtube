# PR TBD — Scheduled CMS Group Sync — Report

Branch: `feat/scheduled-group-sync`
Base: `origin/main` at `39523617` (PR #170 squash)
Date: 2026-08-06

## Scope

The import/sync/ownership arc left grouping converged only when an operator
curls `POST /channels/groups/sync`. This PR makes an enabled deployment
converge on its own: every N hours, for every ACTIVE tenant, for every active
`youtube-analytics` credential, a background job runs the same plan/apply the
manual route runs — same locking, same conflict refusals, same audit rules.
No new HTTP endpoints; the manual route is unchanged. No migration.

Three parts, matching the three things the repo did not have (celery is a
dependency pin with zero imports — nothing ticked):

1. **An HTTP-free sync core.** The route's inline sequence (resolve
   credentials → end the credential transaction → CMS fetch → plan →
   conflict refusal → apply) moved to
   `connectors/runs/group_sync.py::run_group_sync`, sibling of
   `orchestrator.py`. The route is now a shell: permission gate → validation
   → one call → error-to-HTTP mapping → rendering + its run-level summary
   audit. Typed errors replaced route-shaped ones at the boundary
   (`GroupSyncFetchError` keeps fetch-502 distinguishable from
   credential-503 by deliberately NOT subclassing `GoogleConnectorError`;
   `GroupSyncConflictRefusedError` carries the exact 409 detail).
2. **A second job kind on the executor.** Registry key
   `(tenant, "cms_group_sync", content_owner_id, "-")` — the sentinel
   connector-key makes collision with report pulls impossible, and dedup /
   `has_active_job` / shutdown handling work with zero special-casing.
   The worker runs the core on its own session under
   `connector_tenant_context`, with a `PlatformLaneAuditSink` on that same
   session — domain rows and audit rows share ONE commit, so the #169
   atomic invariant holds by construction. Actor: the connector service
   principal extended with `MANAGE_GROUPS@global` (audit rows carry the
   authority the action exercises). Failures land as one
   `CONNECTOR_JOB_RUN` row, `action="group_sync_job_failed"`,
   `error_class` = class name only, via a fresh-session sibling of the
   Bucket-A path (`_audit_failed_before_start` itself untouched — pull-job
   audit shape cannot drift).
3. **An in-process scheduler.** `GroupSyncScheduler`: one daemon thread,
   `Event.wait(interval)` loop, first tick one full interval after boot,
   prompt `close()`. Each tick enumerates ACTIVE tenants (the `tenants`
   table is deliberately outside RLS scoping — no new policy), then per
   tenant lists active `youtube-analytics` credentials and submits one job
   per content owner. **The credential list IS the schedule registry** —
   registering a credential opts an owner in; revoking unsubscribes. No new
   table. Per-tenant fault isolation; a poisoned tick can never kill the
   thread.

Fail-closed: `UMS_GROUP_SYNC_SCHEDULE_ENABLED` (default OFF) +
`UMS_GROUP_SYNC_INTERVAL_HOURS` (default 24, positive-int validated at
load). Boot refuses (`ValueError` naming the env vars) when the schedule is
enabled without the executor or without the service actor. Lifespan closes
scheduler first, then executor — the reverse order would let a tick submit
into a shutting-down pool.

## Audit taxonomy (the design's deliberate calls)

- Changes applied → per-group `GROUP_UPDATED` rows (`source="cms_group_sync"`)
  from the shared apply, plus one run-level `GROUPS_SYNCED` summary. The
  summary is **caller-owned**: the manual route writes it unconditionally
  (operator actions are always audited); the worker writes it **only when a
  non-UNCHANGED outcome exists**.
- Converged tick → **zero audit rows**. Liveness is a log line
  ("N submitted, M in-flight-skipped across T tenants"), not audit spam.
- Failures → `group_sync_job_failed` every tick until remedied — a wrong
  owner stamp keeps surfacing until the operator runs the #170 clear route,
  so the schedule itself becomes the nag.
- Concurrency is throttling, not correctness: a manual sync holds no
  registry slot and can race a scheduled job safely — the row locks from
  #169/#170 (locked re-reads, `FOR NO KEY UPDATE` adopt/clear) are the real
  guarantee; a lost race audits a typed conflict and the next tick
  converges.

## Build-time catches worth knowing about

1. **Shared-session RLS pinning (caught in Sched 3, proven in Sched 5).**
   The scheduler shares one session across the tenant loop.
   `connector_tenant_context` only resets the transaction boundary when its
   lookup is the session's FIRST statement — without intervention, tenant
   #2's credential read would run inside tenant #1's still-open transaction
   with the RLS GUC pinned to the stale tenant: silently zero/wrong rows on
   Postgres while every SQLite test stays green. Fixed with explicit
   rollbacks after enumeration and after each tenant (ids extracted to a
   plain list first — `rollback()` expires loaded ORM rows). The PG tier
   proves it with a two-tenant tick test AND a mutant probe: with the
   per-tenant rollback deleted, the test fails `assert 0 == 2` (every
   tenant after the first sees zero credentials). Falsifiable, not
   decorative.
2. **Resolver injection on `run_group_sync`.** Both protected route suites
   patch `api.channels.resolve_connector_credentials`; a patch on that
   module attribute cannot reach a call made through the core's own import.
   The core therefore takes `resolver=` (defaulting to the real orchestrator
   function), and the route passes its own patchable module global. Job
   callers pass nothing. This is what let the extraction land with ZERO
   edits to the protected suites.
3. **Shutdown audit for queued-but-cancelled sync jobs** reuses the
   pull-shaped `job_failed_before_start` row on purpose: the key's sentinel
   connector-key + month make it fully attributable, and re-tagging a job
   that never STARTED with the run-time failure taxonomy would mislabel it.
   Decided, tested (`test_close_audits_queued_sync_job_with_before_start_row`),
   documented in `_audit_pending_on_shutdown`.

## Validation

All local against Postgres 18 (container `ums-mig-pg-test`), nothing else
touching the database during the run.

| Gate | Result |
| --- | --- |
| Full suite `uv run pytest -q` with Postgres | 2743 passed, 0 failed (exit 0, 8m04s) |
| `ruff check backend tests` | All checks passed |
| `ruff format --check backend tests scripts` | 475 files already formatted |
| `mypy` on the six touched backend modules | No issues found |
| 100-char guard over changed files | No violations |
| Alembic (no migration in PR) | single head `20260805_0001` |
| `git diff --check` | Clean |
| Protected sync-route suites vs origin/main | Byte-identical (zero edits) |

Per-task suite evidence during the build: `tests/connectors/runs/` 129
passed (Sched 3), full API tier 790 passed (Sched 4), the new PG tier 4
passed three times in a row incl. after mutant-revert (Sched 5), neighbours
`test_channel_group_sync_postgres.py` + `tests/connectors/runs/` 135 passed
after the new PG module ran (purge hygiene).

New tests by tier: core extraction 5 (incl. an AST guard that
`HTTPException` never enters the module), executor job kind 11 (key
non-collision, dedup, one-commit atomicity, UNCHANGED-zero-audit with its
anti-vacuity twin, five failure seams, shutdown), scheduler 7 (submission
set, pagination, fault isolation, in-flight skip, lifecycle, poisoned
tick), boot 4 + settings-loader 6, Postgres tier 4 (convergence,
two-tenant RLS isolation, idempotent re-tick + change twin, cross-lane
failure audit).

## Notes for reviewers

- The two protected suites (`test_channel_group_sync_api.py`,
  `test_channel_group_sync_postgres.py`) were the extraction's
  behaviour-preservation proof and are untouched — any diff there would
  have meant the extraction changed behaviour.
- The scheduler filters credentials on the bare literal `"active"` via a
  local `_CREDENTIAL_STATUS_ACTIVE` constant: the credential layer exposes
  no importable constant (three call sites all use the bare literal). The
  scheduler's comment cites them. Exporting a shared constant is a
  worthwhile one-line cleanup for a future PR, deliberately not smuggled
  into this one.
- `_SERVICE_ACCOUNT_EMAIL` is imported from `connectors/google/audit.py`
  despite the leading underscore — same identity the pull-job audit rows
  carry; a rename there fails loudly at import time.
- Sync jobs share the executor pool (`connector_job_max_workers`, default
  1); a long pull delays a tick's jobs. Tunable by the existing knob;
  acceptable at current scale.

## Rollback

Revert the branch. No migration. Both settings default OFF, so a revert —
or simply never setting the env vars — leaves exactly today's behaviour:
manual sync only. Jobs in flight at downgrade complete or die with the
process; the next manual sync converges whatever they left.
