# PR TBD - Google Credential Setup Smoke - Report

**Date:** 2026-06-21
**Branch:** `codex/google-credential-smoke`
**Base:** `origin/main` at `5f1fbbf2` (PR #133 merge)
**Status:** Operator credential-only smoke CLI plus setup/runbook updates for
owner-approved Google credentials.

## What was requested

Continue from the desktop gaps status file after PR #132. The next safe slice
was to add owner-approved credential setup and smoke coverage without starting
real live Google execution before required Google-issued credentials are
provided and approved. Per owner direction, this is now a real code PR, not a
standalone Markdown-only PR.

## What changed

- Added `scripts/check_google_connector_credential.py`, a credential-only
  operator CLI that validates tenant context, resolves the stored external
  secret through the existing Google credential resolver, refreshes the OAuth
  token, commits only credential refresh telemetry, and never starts ingestion.
- Added `tests/connectors/google/test_credential_smoke_cli.py` covering
  argparse rejection, missing database URL, success telemetry commit, no
  connector-run creation, tenant lifecycle failure handling, and OAuth error
  redaction.
- Added `Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md`.
- Linked the runbook from `Docs/00_README.md`.
- Updated `Docs/05_CONNECTORS_YOUTUBE_ADSENSE.md` with the runbook gate and
  acceptance check.
- Updated `Docs/12_BACKEND_API_SPEC.md` with the exact setup/credential-smoke/
  probe/dry-run sequence and current resolver-support boundary.
- Updated `Docs/15_DELIVERY_BACKLOG.md` with the runbook slice and remaining
  live-credential blocker.
- Updated `Docs/16_OPEN_DECISIONS.md` to point to the setup/smoke process while
  keeping report/data questions open.
- Added standard PR report, changelog, and handoff artifacts for this branch.

## Non-goals

- No Google credentials, API keys, refresh tokens, client secrets, or secret
  references were added.
- No live Google data API execution was performed. The new CLI is designed to
  contact only Google's OAuth token endpoint when real credentials are supplied.
- No connector ingestion runtime, database schema, migration, permission, or UI
  code changed.
- No `dry_run: false` connector job is enabled or recommended by this branch.

## Quality notes

- The runbook keeps API key use limited to Google-permitted public metadata and
  keeps private revenue/account APIs on official Google OAuth credential
  material.
- The runbook separates admin API accepted secret-reference prefixes from the
  currently registered production resolver schemes. Operators should use GCP
  Secret Manager for this smoke unless a future credential-lifecycle PR adds
  another resolver.
- The first smoke path is now the credential-only CLI, then the audited API
  credential probe, then ingestion CLI `--dry-run`; API job dry-runs are called
  out as later executor validation because they are audited jobs and may call
  Google APIs.
- The CLI uses the same `resolve_connector_credentials` path as ingestion, so
  credential smoke cannot drift from the live runner's secret/OAuth contract.
- CLI error output is intentionally redacted for typed credential/secret/OAuth
  failures and does not print raw secret refs, token strings, or OAuth inner
  error text.

## Validation

- `python -m ruff check backend tests scripts`
  passed.
- `python -m pytest -q tests/connectors/google/test_credential_smoke_cli.py tests/connectors/google/test_run_one_cli.py tests/connectors/test_credentials.py tests/connectors/google/test_secret_resolver.py tests/connectors/google/test_gcp_secret_manager.py tests/connectors/google/test_oauth.py tests/api/test_connectors_api.py`
  passed with 150 tests in 48.70s.
- Direct CLI smoke with `UMS_DATABASE_URL` unset returned the expected exit `2`
  and message.
- `git diff --check` passed.
- Touched-file trailing-whitespace scan passed.
- Touched-doc Claude attribution scan passed with no matches.
- Touched-artifact secret-pattern scan passed with no matches for common Google
  API key, OAuth access token, client-secret, or non-placeholder Secret Manager
  URI patterns.
- Full `python -m pytest -q` was attempted and did not pass in this local
  environment: `2295 passed, 21 failed, 78 errors in 582.58s`. The displayed
  failures/errors were the PostgreSQL/RLS/migration suites calling
  `require_postgres_url()` because `UMS_TEST_DATABASE_URL` is not set.

## Risks and follow-up

- The CLI/runbook do not prove real owner credentials because no credentials
  were supplied.
- The runbook documents that `aws-secretsmanager://`, `azure-keyvault://`,
  `vault://`, and `kms://` are admin-accepted but not registered production
  resolvers in this slice.
- Next implementation still requires owner-supplied Google project, scopes,
  account ids, Secret Manager payload, and smoke execution evidence.

## Rollback notes

Revert this branch to remove the CLI, tests, runbook, and linked docs. There is
no schema, data, credential, or migration rollback.
