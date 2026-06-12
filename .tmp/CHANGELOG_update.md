# Changelog

## 2026-06-12 - PR #95 (feat/connector-jobs-executor) review-thread cleanup

All 15 review threads (1 P1 + 11 P2 from codex-connector + 3 from kody-ai
+ qodo-code-review re-runs) are resolved. Validation gates: 1161+ tests
pass (api + connectors, both SQLite and PG tiers with
UMS_TEST_DATABASE_URL=postgres:18-alpine), ruff clean, tsc -b clean,
vitest 239/239, git diff --check clean.

### Behaviour changes

- `POST /connectors/jobs` now reserves an executor slot atomically
  (no check-then-act race) and only enqueues the worker after the
  route-owned audit row commits.
- A pre-flight 503 `service_principal_unavailable` closes the
  pre-start service-actor audit gap.
- Dry-run results are now persisted in a `job_dry_run_completed`
  audit row (counts + per-report failures) so the audit log is the
  durable record.
- The frontend spreads the run-history refetch over 0/1/3/5s after
  a 202 so the worker has time to commit `start_run`.
