# Fix: PG migration round-trip tests hang forever on lock contention

**Date:** 2026-06-11
**Branch:** `fix/pg-migration-test-lock-timeout` (off main `e92efd2`, #88)
**Reported by:** Codex — full `pytest` hangs ~56%, isolated to
`tests/db/test_adsense_payment_source_account_migration_postgres.py::test_source_account_migration_backfills_legacy_null_rows`;
"log stops after collection; Postgres idle after COMMIT, no active SQL lock."

## What I could and could not reproduce (honest)

- **Could NOT reproduce the hang on a truly fresh disposable Postgres** (the documented
  validation environment): the named test passes in **1.76s**; full `tests/db` = **179 passed**;
  the full backend suite is green (2046/2050/2051 on the three feature branches). Ambient
  `UMS_DATABASE_URL` / `UMS_TEST_DATABASE_URL` were both empty in my shell.
- **Could reproduce the hang MECHANISM deterministically.** With a lock held on any `public`
  object by a second connection, `DROP SCHEMA public CASCADE` is **still blocked after 5s with no
  result** (indefinite wait) because there is no `lock_timeout`. Setting `lock_timeout` converts it
  into a fast `LockNotAvailable` failure in ~3s.

## Root cause

Every PG migration round-trip test resets state with a `fresh_engine` fixture that runs
`DROP SCHEMA public CASCADE` with **no `lock_timeout`**. `DROP SCHEMA … CASCADE` needs
`ACCESS EXCLUSIVE` on the schema and every contained object; if **any** other connection holds a
conflicting lock — e.g. an **orphaned connection left `idle in transaction` by a prior killed/hung
pytest run on a shared/reused cluster** (the same class of dirty-cluster state that produced the 10
spurious migration failures documented earlier) — the reset waits **forever**. Postgres reports the
orphan as `idle in transaction` (reads as "idle"); the test process is stuck inside `DROP SCHEMA`,
producing exactly the "log stops, no progress" profile. On a genuinely fresh cluster there is no
orphan, so it does not reproduce — which is why my clean-room runs and single-test runs all pass.

This is the harness twin of the already-documented rule that these migration round-trip / downgrade
tests require a **pristine cluster per run** (PR #88 was validated on a fresh clean-room cluster for
the same reason).

## Fix

Add `SET LOCAL lock_timeout = '30s'` immediately before `DROP SCHEMA public CASCADE` in **all 9**
duplicated `fresh_engine` schema-reset fixtures (8 use `text`, the RLS test's `_drop_public_schema`
uses `sa.text`). `SET LOCAL` is transaction-scoped (reverts on the `engine.begin()` commit), so it
guards only the schema reset and does **not** alter the existing lock-blocking tests (which set
their own `statement_timeout='750ms'` on a contender connection).

Effect: a contended schema reset now **fails fast (~30s) with a self-explanatory
`LockNotAvailable: canceling statement due to lock timeout [SQL: DROP SCHEMA public CASCADE]`**
instead of hanging indefinitely — turning the worst CI failure mode (no output, no diagnosis) into
a clear, actionable error that names the operation and the cause. No test assertion is skipped,
xfailed, deleted, or loosened; only the harness reset gains a fail-fast bound.

Files changed (all test-harness fixtures; no product code, no migration):
- `tests/db/test_adsense_payment_source_account_migration_postgres.py`
- `tests/db/test_channel_account_map_migration_postgres.py`
- `tests/db/test_committed_allocation_migration_postgres.py`
- `tests/db/test_connector_runs_migration_postgres.py`
- `tests/db/test_deduction_components_migration_postgres.py`
- `tests/db/test_google_revenue_source_migration_postgres.py`
- `tests/db/test_raw_report_files_purge_migration.py`
- `tests/db/test_tenant_rls_migration.py`
- `tests/finance/test_google_source_normalizer_postgres.py`

## Validation

- **Under contention (actual named test):** held an `ACCESS EXCLUSIVE` lock on a `public` object,
  ran the test → now ERRORS in **30.65s** with
  `sqlalchemy.exc.OperationalError: (psycopg.errors.LockNotAvailable) canceling statement due to
  lock timeout [SQL: DROP SCHEMA public CASCADE]` — fast, diagnosable, not a hang.
- **No regression (fresh clean-room, no contention):** `tests/db` + the finance PG normalizer test
  = **184 passed**.
- `python -m ruff check` clean on all changed files; `git diff --check` clean.

## Alternatives considered (and why they were rejected)

- **`statement_timeout` instead of `lock_timeout`.** `statement_timeout` caps the total execution
  time of a statement, including the cascade object drops. On a large or contended schema, a valid
  `DROP SCHEMA … CASCADE` can legitimately take longer than the cap, producing a false-positive
  timeout. `lock_timeout` is narrowly scoped to the lock-wait phase, which is the only place the
  original hang was observable. Rejected: wrong scope, higher false-positive risk.
- **`pg_terminate_backend()` to kill orphan lock holders before the drop.** Would clear the
  contention source directly. Rejected: requires superuser / `pg_terminate_backend` privilege
  (the test DB role may not have it), and on a shared cluster it can kill legitimate sessions the
  test did not intend to disturb. It also masks a real connection-leak signal we want surfaced.
- **Centralised `reset_schema()` helper used by all 9 fixtures.** Would remove the 9-way
  duplication and make the timeout (and any future tuning) a single line. Rejected for this PR:
  the duplicate-fixture surface is a much broader refactor (it would also fold in the RLS helper
  `_drop_public_schema` and touch all 9 test imports). Tracked as a follow-up rather than
  bundled into a fail-fast fix.
- **Shorter `lock_timeout` (e.g. 5 s).** Would fail faster on real contention. Rejected: on slow
  or loaded CI hosts, a legitimate lock-wait of a few seconds is not abnormal; 5 s would convert
  ordinary contention into a flapping test. 30 s keeps the fail-fast bound while remaining well
  above the normal wait envelope.
- **Ephemeral per-test database / schema (no `DROP SCHEMA public` at all).** Removes the lock
  contention class entirely. Rejected for this PR: large fixture/infra change (per-test
  `create database` / `search_path` plumbing) that belongs in a dedicated PR; the fail-fast bound
  is sufficient for the immediate "log stops, no progress" CI failure mode.

## Recommendations (not done here — out of scope of this fix)

1. **Reset the shared `ums-mig-pg` / `test_ums` cluster** (drop the orphan `app_tenant`/`app_platform`
   roles + recreate the DB) and prefer a fresh container per full-suite run — this removes the
   orphan-lock source itself. (Documented in the postgres-test-container memory.)
2. **For a definitive root-cause on Codex's machine**, capture a `faulthandler`/`py-spy` stack of
   the hung process — it will confirm the stuck frame is `DROP SCHEMA` and show the lock holder.
3. **Latent (separate) bug — `alembic/env.py` URL precedence:** `get_database_url()` reads
   `UMS_DATABASE_URL` *before* the alembic config's `sqlalchemy.url`, so a test that configures an
   explicit test URL can be silently overridden by an ambient `UMS_DATABASE_URL`, running migrations
   against the wrong DB. Not the hang (it would fail, not hang), but worth a separate hardening.
