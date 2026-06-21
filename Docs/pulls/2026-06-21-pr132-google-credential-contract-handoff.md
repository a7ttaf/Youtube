# PR #132 - Google Credential Contract Reconciliation - Handoff

## Scope

Docs and public wording only. The purpose is to make the live Google connector
credential contract explicit before the owner provides API keys or
authorization-token credential material.

## Operator rules

- Do not connect UMS to a Gmail password, browser session, cookie, or manually
  logged-in Google account.
- Use API keys only for YouTube Data API public metadata where Google permits
  key-only access.
- Use official Google OAuth 2.0 authorization-token credentials and narrow
  scopes for private YouTube Reporting, YouTube Analytics, and AdSense account
  or revenue APIs.
- Store credentials through the existing external secret-reference contract.

## Validation

- `python -m ruff check backend tests scripts` passed.
- `python -m pytest tests/api/test_connectors_api.py tests/api/test_user_roles_api.py tests/auth/test_user_roles_repository.py tests/auth/test_policy.py tests/db/test_security_orm.py -q` passed with 91 tests.
- Full `python -m pytest -q` passed with 2389 tests and 14 Alembic deprecation warnings using a clean disposable Postgres database named `test_ums_pr132`.
- `git diff --check` passed.
- Targeted stale-wording and touched-file Claude attribution scans passed.

## Next PR recommendation

Choose the live credential material and scope set per connector, then wire the
operator setup/runbook around that decision without enabling direct Gmail
session linking.

## Rollback notes

Revert this PR to restore the previous docs and public wording. There is no
schema, data, or migration rollback.
