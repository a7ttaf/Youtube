# PR #33 — Connector Credential Repository Tests — Report

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/33
**Branch:** `pr/s2-4b-connectors-credentials-tests`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables` (rolling integration head `bdc9e34`, after PR #29 and PR #30 merged)
**Head commit:** `b13222d` (first commit; this report and the changelog/handoff land in a second commit)
**Status:** Open, all gates green locally, awaiting review.

## What was requested

Item #3 of the three-PR test-coverage backfill sequence the user picked after PR #28/#29/#30 shipped: direct module-level tests for `backend/ums_smart_revenue/connectors/credentials.py`. At start of session, the module had only **indirect API-level coverage** through `tests/api/test_connectors_api.py` — its repository class, its tenant-isolation behavior, its duplicate-detection paths, and the `is_external_secret_ref` allowlist had no dedicated tests.

The module is security-sensitive: it owns the encrypted-secret-ref storage for every connector account (YouTube Reporting, AdSense, etc), so weakening its tenant filters, weakening the secret-ref allowlist, or accidentally exposing `encrypted_secret_ref` in API responses would be a high-impact regression.

## What was actually done

A single new test file, `tests/connectors/test_credentials.py`, with **38 focused tests** (the `tests/connectors/` subdirectory is created by this PR). The tests cover:

| Surface | Tests | Coverage |
|---|---|---|
| `is_external_secret_ref` allowlist | 2 parametrized (16 cases) + 1 strip + 1 const | Every allowed prefix; blank/whitespace/empty-suffix/unknown-prefix rejection; whitespace stripping; `SECRET_REF_PREFIXES` const matches documentation |
| `ConnectorCredentialEntry.to_api` | 2 | Every field serialized; secret material never in serialized output |
| `create_credential` happy path | 3 | tenant_id stamp + all fields; non-blank ref → `has_secret_ref=True`; blank ref → `has_secret_ref=False`; secret ref never leaked through `to_api()` |
| `create_credential` duplicate detection | 3 | Pre-check raises `Conflict`; distinct keys succeed; cross-tenant same key succeeds |
| `create_credential` input validation | 2 | Malformed actor UUID; empty actor UUID |
| `list_credentials` | 6 | Default page ordering by `(connector_key, account_id)`; pagination across 3 pages with `has_more`; bad limit/offset rejection; cross-tenant isolation; empty page; secret ref never in serialized output |
| ORM-level integrity (race-condition safety net) | 1 | Direct insert with duplicate `(tenant_id, connector_key, account_id)` raises `IntegrityError` |
| Repository defaults & constants | 3 | `_tenant_id == UUID(UMS_TENANT_ID)`; `MAX_CREDENTIAL_PAGE_SIZE` is a positive int; `CONNECTOR_CREDENTIAL_UNIQUE_CONSTRAINT` matches documented name |

## Phased execution

| Phase | Action | Pytest after | Notes |
|---|---|---|---|
| Baseline | Worktree off `origin/pr/s2-4a-tenant-id-on-operational-tables` (head `bdc9e34`) | 538 passed | 0 ruff errors; 1 pre-existing format-unclean file. |
| 1 | Read `connectors/credentials.py` (193 lines) | 538 passed | Confirmed tenant wiring on pre-check, INSERT, and list query. |
| 2 | Read `ApiConnectorCredentialORM` definition | 538 passed | Confirmed `(tenant_id, connector_key, account_id)` unique constraint. |
| 3 | Read `tests/api/test_connectors_api.py` | 538 passed | Adopted UUID and seed-shape conventions. |
| 4 | Create `tests/connectors/` directory; write `test_credentials.py` (~450 lines after format, 38 tests) | 576 passed | One E501 long-line fixed; one format reflow. |
| 5 | `ruff check` + `ruff format --check` | 576 passed | All clean. |
| 6 | Final full gate | 576 passed | Baseline preserved. |
| 7 | Commit `b13222d`, push, open PR #33 | — | — |

## Quality checks performed

- `python -m ruff check backend tests` — All checks passed.
- `python -m ruff check tests/connectors/test_credentials.py` — All checks passed.
- `python -m ruff format --check backend tests` — 1 file unclean (`tests/finance/test_adsense_payments_tenant_scope.py`, pre-existing; not modified).
- `python -m ruff format --check tests/connectors/test_credentials.py` — Already formatted.
- `python -m pytest -q` — **576 passed, 7 warnings in 31s** (538 base + 38 new).
- `python -m pytest -q tests/connectors/test_credentials.py` — 38 passed in 0.31s.
- `git diff --check` and `git diff --cached --check` — clean.
- Conflict-marker scan (tracked + working tree) — clean.
- Import smoke: 8 names from `ums_smart_revenue.connectors.credentials` — ok.
- Alembic linear history — single head `20260518_0001`.

## Architecture & quality posture

- **No source semantics change.**
- **No tenant scoping change.** Tests exercise the existing wiring.
- **No graph projection impact detected.** `api_connector_credentials` is PostgreSQL-only; Neo4j is read-only and downstream.
- **No authorization or audit behavior change.**
- **Security**: regression guards for five vectors: (a) `SECRET_REF_PREFIXES` allowlist (rejects `https://`, `file://`, blank, empty-suffix, leading-whitespace, unknown prefixes); (b) `to_api()` never exposing `encrypted_secret_ref` or its value; (c) cross-tenant duplicate-key behavior (must succeed across tenants, must fail within a tenant); (d) cross-tenant `list_credentials` isolation; (e) ORM-level unique constraint as a backstop against repo pre-check bypass.
- **Observability**: no logging change.
- **Testability**: +38 dedicated tests for a previously-indirect-only 193-line module.

## Blast-radius statement

*No graph projection impact detected.* No SQLAlchemy ORM change, no Alembic migration, no route, no service, no repository, no DI provider, no schema change. The PR adds one new test file (and creates `tests/connectors/`) and nothing else.

## Pre-existing baseline (NOT introduced by this PR)

Base `pr/s2-4a` at `bdc9e34`: **0 ruff errors**, **1 `ruff format` would-reformat file** (`tests/finance/test_adsense_payments_tenant_scope.py`). This PR adds **0** to both.

## Validation that could NOT be run

- The `ci/` self-hosted pre-push gate does not exist on this stack. All UMS-required gates plus "no by luck work" additions **were** run.

## Remaining risks

- **Code risk: zero.**
- **Test-flake risk: very low.** In-memory SQLite, no shared state, no time-dependent assertions.
- **Reviewer-flow risk: low.** One file, 448 lines, 38 tests, each short and focused.

## Follow-up recommendations

This PR closes the 3-PR test-coverage sequence. Remaining queued items (independent of this PR):

- One-off `ruff format` pass on `tests/finance/test_adsense_payments_tenant_scope.py`.
- SAWarnings cleanup (`uq_users_email_lower` SQLite reflection noise — small focused PR).
- Wider rebase / merge of the S2.4b stack onto `origin/main` (operator-led).

## Rollback notes

- Single-file PR. Revert is `git revert <merge-commit>` — removes one test file and the new `tests/connectors/` directory. Production unaffected.
