# Google Credential Setup and Smoke Runbook

## Purpose

Prepare owner-approved Google connector credentials and run the first safe
smoke checks for YouTube Reporting, YouTube Analytics, and AdSense Management.
This runbook implements the PR #132 credential contract operationally and pairs
it with a credential-only CLI smoke command, without adding real credential
material to the repository.

This runbook does not authorize live ingestion by itself. A live pull still
requires owner-approved Google-issued credential material, approved scopes, a
passing credential probe, and a passing dry-run smoke.

## Hard boundaries

- Do not use Gmail passwords, browser cookies, saved browser sessions, or
  manual Gmail login automation as connector credentials.
- Do not commit refresh tokens, OAuth client secrets, API keys, secret refs, or
  Google account identifiers into the repository or PR artifacts.
- Do not store raw credential payloads in UMS database rows. UMS stores only an
  external secret reference and credential telemetry.
- Do not run `POST /connectors/jobs` with `dry_run: false` until the owner has
  approved the exact Google project, credential owner, connector scopes, account
  ids, and smoke evidence.
- Do not treat API-key-only access as valid for YouTube Reporting, private
  YouTube Analytics revenue/account queries, or AdSense account/payment data.

## Owner approval packet

Capture this packet in the secure operator tracker before registering a UMS
credential reference. The packet must not be copied into repo docs.

| Item | Required evidence |
|---|---|
| Google Cloud project | Project id and owner-approved purpose |
| Enabled APIs | Exact APIs needed by connector, such as YouTube Reporting, YouTube Analytics, YouTube Data, or AdSense Management |
| Authorization flow | Google-approved OAuth consent flow that produced the refresh token; no Gmail session shortcut |
| OAuth client | Client id owner, client type, and allowed token URI |
| Approved scopes | Narrow scopes per connector/account, copied from the Google approval record |
| Secret location | GCP Secret Manager secret name/version; do not paste the secret value into UMS |
| Runtime IAM | UMS runtime identity has read access only to the needed secret version |
| Tenant/account mapping | UMS tenant id, `connector_key`, and Google `account_id` or CMS content owner id |
| Service actor | `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` is set for live non-dry-run execution |
| Smoke month | A non-closed month or approved historical month to use for the first dry-run |

## Secret payload contract

The external secret payload must be UTF-8 JSON. The current Google OAuth wrapper
requires these fields:

```json
{
  "refresh_token": "<google-issued-refresh-token>",
  "client_id": "<google-oauth-client-id>",
  "client_secret": "<google-oauth-client-secret>",
  "token_uri": "<google-oauth-token-uri>",
  "scopes": ["<owner-approved-scope>"]
}
```

`scopes` is optional in the parser, but the owner approval packet must still
record the approved scopes for the credential. The payload must live in the
external secret store, not in UMS source, docs, database rows, logs, or tickets
that are not approved for secrets.

Current production resolver support:

- Use `secret-manager://projects/<project>/secrets/<secret>/versions/<version>`
  or `gcp-secret-manager://projects/<project>/secrets/<secret>/versions/<version>`
  for production smoke.
- The admin API allowlist also accepts `aws-secretsmanager://`,
  `azure-keyvault://`, `vault://`, and `kms://`, but those schemes are not
  registered production resolvers in this slice. A connector run using one of
  them fails closed with an unsupported resolver error.
- `local-secret://` is for tests and local mocks only. Do not use it for owner
  credential smoke.

## Setup sequence

1. Prepare Google outside UMS.

   - Confirm the Google Cloud project and required APIs.
   - Complete the approved Google OAuth authorization flow.
   - Store the OAuth payload in GCP Secret Manager.
   - Grant the UMS runtime identity read access only to that secret version.

2. Register the external reference in UMS.

```http
POST /connectors/credentials
```

```json
{
  "connector_key": "youtube_reporting",
  "account_id": "<content-owner-or-account-id>",
  "encrypted_secret_ref": "secret-manager://projects/<project>/secrets/<secret>/versions/latest",
  "reason": "Register owner-approved Google credential reference for smoke"
}
```

Expected result:

- Caller has `connectors.manage`.
- Response status is `201`.
- Response includes `has_secret_ref: true`.
- Response does not include `encrypted_secret_ref`, raw secret payload, refresh
  token, client secret, or API key.
- A `CONNECTOR_SETTINGS_CHANGED` audit row records the reference creation.

3. Read metadata and health.

```http
GET /connectors/credentials
GET /connectors/credentials/health?limit=50&offset=0
```

Expected result:

- Credential metadata is visible only to authorized connector admins/viewers.
- Health is derived from stored telemetry only. This endpoint does not resolve
  the secret and does not refresh a Google token.

4. Run the credential-only CLI smoke.

```powershell
$env:UMS_DATABASE_URL = "<operator-approved-database-url>"
python scripts/check_google_connector_credential.py --tenant <tenant-uuid> --connector youtube-reporting --account <content-owner-id>
```

Expected result:

- The CLI exits `0`.
- Output starts with `OK connector=... account=... token_expiry=...`.
- The command resolves the external secret and performs an official Google
  token refresh only.
- The command commits only `api_connector_credentials` refresh telemetry.
- No connector run row, raw file, source row, finance fact, or audit event is
  created by the command.
- Typed tenant, credential, secret, or OAuth failures exit `2` with a
  redacted class/message line on stderr.

5. Run the audited API credential probe.

```http
POST /connectors/credentials/youtube_reporting/<content-owner-or-account-id>/test
```

```json
{
  "reason": "Smoke owner-approved Google credential token refresh"
}
```

Expected result:

- Caller has `connectors.manage`.
- The endpoint resolves the external secret and performs an official Google
  token refresh only.
- No YouTube, YouTube Analytics, or AdSense report data is fetched.
- Every probe writes a `CONNECTOR_TESTED` audit row.
- `status: "ok"` is required before any connector dry-run.

Failure handling:

| Response status | Machine status | Operator action |
|---|---|---|
| `404` | `not_found` | Register the credential reference for the exact connector/account pair |
| `200` | `inactive_credential` | Re-register or reactivate through an approved credential lifecycle action |
| `200` | `auth_failed` | Reissue the Google refresh token or correct OAuth client/scopes |
| `200` | `error` | Check secret-manager IAM, URI shape, payload JSON, and resolver support |

## First dry-run smoke

Run this only after the credential-only CLI smoke exits `0` and the audited
credential probe returns `status: "ok"`.

Preferred smoke path:

```powershell
$env:UMS_DATABASE_URL = "<operator-approved-database-url>"
python scripts/run_google_connector.py --tenant <tenant-uuid> --connector youtube-reporting --account <content-owner-id> --month <YYYY-MM> --dry-run
```

Expected result:

- The CLI exits `0`.
- Output includes `DRY-RUN counts=...`.
- No connector run row, raw file, source row, finance fact, or audit event is
  written by the CLI dry-run path.
- The smoke evidence records only command, exit code, high-level counts, and
  sanitized timestamps. Do not record secret values or secret refs.

API job dry-runs are allowed only when the owner explicitly wants to validate
the in-process executor path:

```http
POST /connectors/jobs
```

```json
{
  "connector_key": "youtube-reporting",
  "account_id": "<content-owner-id>",
  "report_month": "<YYYY-MM>",
  "dry_run": true,
  "reason": "Executor dry-run after credential probe passed"
}
```

This path submits an audited job through the executor and may call Google APIs.
It is therefore not the first smoke. Use it after the credential probe and CLI
dry-run pass.

## Live-run gate

Before the first `dry_run: false` job:

- Owner approval packet is complete.
- `scripts/check_google_connector_credential.py` exited `0` for the exact
  tenant/connector/account.
- `POST /connectors/credentials/{connector_key}/{account_id}/test` returned
  `status: "ok"` for the exact connector/account.
- CLI `--dry-run` passed for the exact tenant/connector/account/month.
- `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` is set to an active service actor
  with `connectors.run_jobs`.
- The target month is not locked unless the owner explicitly approves the
  behavior for a closed month.
- Operator has reviewed the expected source tables and rollback note below.

## Rollback and rotation

- Revoke or rotate the Google refresh token in Google Cloud or the Google
  account authorization surface.
- Disable or destroy the affected GCP Secret Manager version after confirming no
  other UMS environment uses it.
- Until a credential lifecycle API exists, disabling an already-registered UMS
  credential row requires an owner-approved database/admin operation against
  `api_connector_credentials.status`.
- Re-run the credential probe after any rotation. Do not reuse old smoke
  evidence for a new secret version.

## Validation references

Existing test coverage for this runbook:

- `tests/connectors/test_credentials.py` covers the external-secret reference
  allowlist and proves API serialization does not expose secret locators.
- `tests/api/test_connectors_api.py` covers credential creation, permission
  gates, audited credential probes, and safe machine-readable probe statuses.
- `tests/connectors/google/test_secret_resolver.py`,
  `tests/connectors/google/test_gcp_secret_manager.py`, and
  `tests/connectors/google/test_oauth.py` cover resolver dispatch, GCP Secret
  Manager URI validation, payload validation, and OAuth refresh wrapping.
- `tests/connectors/google/test_credential_smoke_cli.py` covers the
  credential-only CLI success path, telemetry commit, no-run boundary, tenant
  lifecycle failure, missing DB URL, argparse rejection, and OAuth error
  redaction.
- `tests/connectors/google/test_run_one_cli.py` covers CLI fail-closed behavior
  when database URL, credential rows, or tenant lifecycle state are invalid.
