# PR TBD - Google Credential Setup Smoke - Handoff

## Scope

Code-bearing follow-up to PR #132. The branch adds a credential-only operator
smoke CLI and turns the credential contract into an operator setup and smoke
sequence without supplying credentials or enabling live Google ingestion.

## Operator rules

- Use only Google-issued credential material approved by the owner.
- Use GCP Secret Manager references for this production smoke:
  `secret-manager://projects/<project>/secrets/<secret>/versions/<version>` or
  `gcp-secret-manager://projects/<project>/secrets/<secret>/versions/<version>`.
- Do not paste secret values or real secret refs into repo docs, PR artifacts,
  logs, or non-secret operator tickets.
- Run `scripts/check_google_connector_credential.py` first; it refreshes a
  Google token through the existing resolver, commits only credential refresh
  telemetry, and does not fetch report data.
- Run the credential test endpoint next when validating the API/auth/audit path;
  it refreshes a Google token but does not fetch report data.
- Run ingestion CLI `--dry-run` only after the credential-only CLI exits `0`
  and the credential probe returns `status: "ok"`.
- Do not run `dry_run: false` until the owner approves the setup packet and
  smoke evidence.

## Validation

- `python -m ruff check backend tests scripts` passed.
- `python -m pytest -q tests/connectors/google/test_credential_smoke_cli.py tests/connectors/google/test_run_one_cli.py tests/connectors/test_credentials.py tests/connectors/google/test_secret_resolver.py tests/connectors/google/test_gcp_secret_manager.py tests/connectors/google/test_oauth.py tests/api/test_connectors_api.py`
  passed with 150 tests.
- Direct CLI smoke with `UMS_DATABASE_URL` unset returned the expected exit `2`.
- `git diff --check` passed.
- Touched-file trailing-whitespace scan passed.
- Touched-doc Claude attribution scan passed with no matches.
- Touched-artifact Google credential/token pattern scan passed with no matches.
- Full `python -m pytest -q` was attempted: `2295 passed, 21 failed, 78 errors
  in 582.58s`, blocked by missing `UMS_TEST_DATABASE_URL` for PostgreSQL/RLS/
  migration tests.

## Next PR recommendation

After the owner supplies real Google project/scopes/account ids and Secret
Manager payload, execute the credential-only CLI, audited credential probe, and
ingestion dry-run in an operator-approved environment. Record sanitized smoke
evidence only. Keep that evidence separate from raw credential material and
secret refs.

## Rollback notes

Revert this branch to remove the CLI, tests, runbook, and linked docs. No
database, data, credential, or deployment rollback is required.
