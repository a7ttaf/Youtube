# PR #132 - Google Credential Contract Reconciliation - Report

**Date:** 2026-06-21
**Branch:** `codex/google-credential-contract`
**Base:** `origin/main` at `aa55b626` (PR #131 merge)
**Status:** Credential-contract documentation and public wording cleanup.

## What was requested

Clarify the Google connector credential plan after the owner decision that UMS
must not rely on direct Gmail account linking. The docs needed to separate
API-key-only access from official Google authorization-token credentials and
avoid implying that private revenue APIs can run with only an API key.

## What changed

- `Docs/05_CONNECTORS_YOUTUBE_ADSENSE.md` now has a dedicated Google credential
  contract section with official Google documentation links.
- `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md`,
  `Docs/16_OPEN_DECISIONS.md`, `Docs/18_MULTI_CURRENCY_ENGINE.md`, and
  `Docs/12_BACKEND_API_SPEC.md` now describe the live blocker as
  owner-approved Google connector credential material.
- `Docs/security/ROLE_PERMISSION_MODEL.md` and
  `Docs/security/PERMISSION_MATRIX.md` now use Google/API connector credential
  wording for admin permissions.
- `backend/ums_smart_revenue/api/connectors.py`,
  `backend/ums_smart_revenue/auth/roles.py`, and
  `backend/ums_smart_revenue/db/security_seed.sql` now use safer public/admin
  wording while preserving the existing internal Google token-refresh behavior.

## Credential contract

- YouTube Data API public metadata can use an API key where Google permits it.
- YouTube Reporting API, YouTube Analytics API private revenue/account queries,
  and AdSense Management API account/payment data require official Google OAuth
  2.0 authorization tokens with narrow scopes.
- UMS must not store Gmail passwords, reuse browser cookies, automate Gmail
  login, or link a personal Gmail session as a credential shortcut.
- UMS stores external secret references and telemetry, not raw credential
  material in API responses.

## Non-goals

- No live Google credentials were added.
- No live Google authorization or consent implementation was added.
- No connector execution path, parser, finance calculation, permission check, or
  database schema changed.
- No migration or backfill is required.

## Validation

- `python -m ruff check backend tests scripts` passed.
- `python -m pytest tests/api/test_connectors_api.py tests/api/test_user_roles_api.py tests/auth/test_user_roles_repository.py tests/auth/test_policy.py tests/db/test_security_orm.py -q` passed with 91 tests.
- `$env:UMS_TEST_DATABASE_URL = 'postgresql+psycopg://postgres:ums@localhost:55436/test_ums_pr132'; python -m pytest -q` passed with 2389 tests and 14 Alembic deprecation warnings on a clean disposable Postgres container.
- `git diff --check` passed.
- Targeted stale-wording and touched-file Claude attribution scans passed.

## Risks and follow-up

- Existing databases will keep the prior seeded Connector Admin description
  until the seed is rerun; this is label metadata only.
- The next implementation slice still needs an owner-approved Google Cloud
  project credential decision before any real live pull is enabled.
