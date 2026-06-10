# Spec: alembic/env.py URL-precedence footgun (isolated hardening)

**Date:** 2026-06-11
**Branch:** `spec/alembic-env-url-precedence` (off main `e92efd2`, #88)
**Status:** SPEC ONLY — no code change yet. This changes *which database migrations target*, so it
is deliberately isolated from the test-harness lock_timeout fix
(`fix/pg-migration-test-lock-timeout`) and needs its own review + validation before implementation.

## Problem

`backend/ums_smart_revenue/db/alembic/env.py:48-54`:
```python
def get_database_url() -> str:
    url = os.environ.get("UMS_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    ...
```
It reads the `UMS_DATABASE_URL` env var **first**, falling back to the alembic config's
`sqlalchemy.url` only when the env var is unset. So a caller that *explicitly* configures an Alembic
`Config` with a `sqlalchemy.url` can be **silently overridden** by an ambient `UMS_DATABASE_URL`,
running migrations against the wrong database.

This is latent (it did not cause the migration-test hang — a hijacked DB would *fail*, not hang),
but it is a real correctness/safety hazard:
- Every migration round-trip test builds `Config()` (no ini) and `set_main_option("sqlalchemy.url",
  test_url)`, expecting migrations to hit the disposable test DB. On any machine/CI where
  `UMS_DATABASE_URL` is exported (a dev/staging DB), those tests would instead drive **schema
  drops + migrations against that ambient DB** — data-destructive and nondeterministic.
- More generally, "I configured the URL but a stale env var won" is exactly the kind of
  least-surprise violation that produces hard-to-diagnose wrong-DB incidents.

## Why a naive precedence flip is unsafe (the real tension)

`alembic.ini` ships a **hardcoded, non-empty** placeholder:
```
sqlalchemy.url = postgresql+psycopg://ums:ums@localhost:5432/ums_smart_revenue
```
Production runs migrations via the ini (so `config.get_main_option("sqlalchemy.url")` returns that
localhost placeholder) and depends on `UMS_DATABASE_URL` **overriding** it to reach the real prod
DB. So simply flipping to "config url wins" would make production target the localhost placeholder —
a worse failure. The fix must honor BOTH contracts:
- **Production (ini-based config):** `UMS_DATABASE_URL` must win over the ini placeholder.
- **Tests / programmatic callers (Config built in code):** the explicitly-injected `sqlalchemy.url`
  must win over an ambient `UMS_DATABASE_URL`.

## Distinguishing signal

The two cases differ by `config.config_file_name`:
- Production: built from `alembic.ini` → `config.config_file_name` **is set**, `sqlalchemy.url` = the
  placeholder.
- Tests/programmatic: `Config()` with **no** ini path → `config.config_file_name is None`, and
  `sqlalchemy.url` was set in code (every migration-test fixture does exactly this, and deliberately
  omits the ini path to avoid `fileConfig` clobbering `caplog`).

## Approaches

**A. `config_file_name`-gated precedence (recommended).**
```python
def get_database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if config.config_file_name is None and configured:
        # Programmatic config (tests / embedded callers): the explicitly injected
        # URL wins so an ambient UMS_DATABASE_URL cannot silently retarget it.
        return configured
    # ini-based (production): env var overrides the ini placeholder, as today.
    return os.environ.get("UMS_DATABASE_URL") or configured  # + existing empty-guard
```
Pros: preserves the production env-var-wins contract exactly; fixes the test/programmatic footgun;
small, local change. Cons: relies on the convention "programmatic config ⇒ no ini path" (true for
the entire current codebase — every migration test builds `Config()` without an ini).

**B. Test-side injection (no env.py change).** Each migration test (or a shared fixture/conftest)
sets `os.environ["UMS_DATABASE_URL"]` to the configured test URL around `command.upgrade/downgrade`.
Pros: zero change to the production migration entry point. Cons: leaves the env.py footgun in place
for any future programmatic caller; touches many fixtures; mutates process env.

**C. Dedicated override channel.** Read a distinct, test-only var (e.g. `UMS_ALEMBIC_URL`) or an
`x-argument` with highest precedence. Pros: explicit. Cons: new surface; callers must adopt it;
doesn't fix the "I set sqlalchemy.url and it was ignored" surprise.

**Recommendation: A** — it fixes the actual surprise (explicit config ignored) while provably
preserving production behavior, and is the smallest contract-safe change.

## Blast radius

- `alembic/env.py` is the migration entry point for **both** production deploys and the test suite.
  Changing URL precedence changes *which DB migrations target* → highest-care.
- Must confirm the production migration invocation before merging: how is `command.upgrade` run in
  prod (ini-based?), and is `UMS_DATABASE_URL` always the sole real source there? (Evidence so far:
  ini ships a localhost placeholder, so prod must set `UMS_DATABASE_URL` — consistent with approach A
  keeping env-var-wins for ini-based config.)
- No schema/data change; pure URL-resolution logic. `No graph projection impact detected.`

## Validation plan (to run on this branch when implemented)

Unit tests for `get_database_url()` (or via a thin seam) proving all four quadrants:
1. **Programmatic Config + ambient `UMS_DATABASE_URL` set to a DECOY** → returns the programmatic
   `sqlalchemy.url` (the footgun is closed). This is the regression test for the bug.
2. **ini-based Config + `UMS_DATABASE_URL` set** → returns `UMS_DATABASE_URL` (prod contract intact).
3. **Programmatic Config, no env var** → returns the programmatic url (existing test behavior).
4. **ini-based Config, no env var** → returns the ini placeholder (existing prod-fallback behavior).
Plus: re-run a representative migration round-trip test with a decoy `UMS_DATABASE_URL` exported and
confirm it still targets the configured test DB (end-to-end proof the footgun is gone), and the full
PG suite on a fresh clean-room cluster.

## Out of scope

The test-harness lock_timeout hang fix (separate branch `fix/pg-migration-test-lock-timeout`,
commit `b9deb3e`). The two are independent: lock_timeout is test infrastructure; this is the
migration entry point's URL contract.
