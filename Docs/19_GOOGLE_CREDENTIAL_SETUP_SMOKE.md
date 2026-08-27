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
| Service actor | A service-actor UUID **you provisioned**, holding `connectors.run_jobs`, recorded in the tracker. Never the placeholder `.env.example` ships. Under `docker compose` it cannot be supplied through `.env` — see [Supplying the service actor under `docker compose`](#supplying-the-service-actor-under-docker-compose) |
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
  "connector_key": "youtube-reporting",
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
uv run python scripts/check_google_connector_credential.py --tenant <tenant-uuid> --connector youtube-reporting --account <content-owner-id>
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
POST /connectors/credentials/youtube-reporting/<content-owner-or-account-id>/test
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
uv run python scripts/run_google_connector.py --tenant <tenant-uuid> --connector youtube-reporting --account <content-owner-id> --month <YYYY-MM> --dry-run
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

## Supplying the service actor under `docker compose`

Read this before signing the service-actor line in the live-run gate below. It is
the one checklist item that can be satisfied exactly as an operator would expect
and still leave every connector run refusing.

**`docker-compose.yml` does not forward `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`.**
Compose forwards plenty of other variables from `.env`, but not this one, so
putting it there has no effect on the compose `app` service. Every connector run
then fails closed with the variable reported as *unset*:

```text
ValueError: UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID must be set to a UUID before
connector audit emitters can build a service principal
```

— while the operator is looking at the line they set in `.env`. The
variable is withheld on purpose: `.env.example` currently ships it uncommented as
the public placeholder `00000000-0000-0000-0000-0000000000bb`, and
`connectors/google/audit.py` refuses only on *unset*, accepting any syntactically
valid UUID. Forwarding it would attribute the connector audit trail of every
operator who ran `cp .env.example .env` to one well-known id from a public
template. A refused run is recoverable; a mis-attributed audit trail is not.
See the comment block in `docker-compose.yml` for the two fixes that would let
the pass-through be restored.

To supply the value under compose, add an untracked `docker-compose.override.yml`
beside `docker-compose.yml`. Compose merges it automatically — no `-f` flag:

```yaml
services:
  app:
    environment:
      UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID: "<your provisioned service-actor uuid>"
```

Then prove the value actually reached the service before you sign anything:

```powershell
docker compose config | Select-String SERVICE_ACTOR
docker compose up -d app
```

If the first command prints nothing, the app will refuse every connector run no
matter what `.env` says. The override reaches **only** the service it names — add
a matching `app-dev:` block if you run the dev profile.

Two things that do **not** work, both worth knowing before you burn an hour:

- `docker compose run -e UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID=… app` — this
  affects a one-off container, not the long-running `app` service that serves
  `POST /connectors/jobs`.
- Setting it in `.env`, in any form. It is not forwarded; there is nothing to
  pick it up.

Outside compose there is no such gap: `uv run uvicorn …` and the
`scripts/run_google_connector.py` CLI read the variable from the environment
normally, which is why the CLI dry-run in the previous section works with a
plain `$env:` assignment.

## Live-run gate

Before the first `dry_run: false` job:

- Owner approval packet is complete.
- `scripts/check_google_connector_credential.py` exited `0` for the exact
  tenant/connector/account.
- `POST /connectors/credentials/{connector_key}/{account_id}/test` returned
  `status: "ok"` for the exact connector/account.
- CLI `--dry-run` passed for the exact tenant/connector/account/month.
- `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` is set to an active service actor with
  `connectors.run_jobs`, **and the process that will run the job has been shown
  to see it.** Sign this off against observed output, not against a line in
  `.env`: compose does not forward this particular variable, so
  `docker compose config | Select-String SERVICE_ACTOR` must print the variable.
  If it prints nothing, this item is NOT satisfied however the env file looks —
  see [Supplying the service actor under `docker compose`](#supplying-the-service-actor-under-docker-compose).
- The value is a service actor you provisioned, not the
  `00000000-0000-0000-0000-0000000000bb` placeholder from `.env.example`. The
  runtime check refuses only on *unset* and will accept the placeholder silently.
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

## Verified operator checklist for THIS deployment (2026-08-25)

Command-by-command path from the Desktop credential to a proven UMS credential row,
verified against the code: every flag was checked with `--help`, and the Step-3 SQL was
executed verbatim (rolled back) against a freshly migrated Postgres — `INSERT 0 1`, and
the re-run failed on exactly `uq_api_connector_credentials_connector_account`, which is
the intended idempotency signal.

> ⚠️ **Currency sequencing (decided 2026-08-25: main currency = EGP).** The connector
> currently ingests **USD**. Run at most ONE smoke month through this checklist as
> disposable plumbing proof, then **STOP — no multi-month backfill, no daily sync —
> until EGP program Phase 4** (`Docs/21`). Backfilled USD months can never match the
> EGP workbook acceptance baseline and would all be re-ingested.

**Step 0 — ROTATE the Google OAuth credential first (~1–2h incl. the consent round-trip).**
The current payload (`Desktop\cms-revenue-2026H1\secrets\google_oauth.json`, plus the
client-secret file in `Desktop\UMS report\`) has prior chat exposure. Mint a fresh client
secret and refresh token BEFORE any upload — enshrining a compromised credential in
Secret Manager is worse than the Desktop file. Consent screen must not be in "Testing"
status (7-day token expiry; see the get_revenue.py docstring's own warning).

**Step 1 — install the Google Cloud SDK, then upload.** `gcloud` is **not installed on
this PC** (verified) — install + `gcloud init` first (~0.5–1h), or use the Cloud Console
UI instead. Payload contract per this doc's "Secret payload contract" section: UTF-8
JSON with `refresh_token`, `client_id`, `client_secret`, `token_uri` (optional `scopes`).

```powershell
gcloud secrets create ums-google-oauth --replication-policy=automatic
gcloud secrets versions add ums-google-oauth --data-file="C:\<secure-path>\payload.json"
```

Delete the local payload file after the upload succeeds. (gcloud flag syntax needs
confirmation against current Google docs.)

**Step 2 — GCP auth for the resolver (the missing fourth piece).**

*Host CLI path (Steps 2/4):* `gcloud auth application-default login` on the
Windows host, or set host `GOOGLE_APPLICATION_CREDENTIALS` to a service-account
key. The identity needs `roles/secretmanager.secretAccessor`. Without this, the
host credential probe exits 2 with `SecretFetchError`.

*Compose `app` / in-process jobs:* host ADC is **not** visible inside the
container. `docker-compose.yml` does not mount ADC or forward
`GOOGLE_APPLICATION_CREDENTIALS`. Before treating Step 2 as sufficient for
Compose live runs, mount a service-account key (or ADC directory) into `app`
and set `GOOGLE_APPLICATION_CREDENTIALS` via an untracked
`docker-compose.override.yml` (same override pattern as
[Supplying the service actor under `docker compose`](#supplying-the-service-actor-under-docker-compose)).
Otherwise the host probe can pass while API credential tests and connector jobs
fail with `SecretFetchError` inside the container.

**Step 3 — the credential row** (superuser SQL bypasses RLS deliberately; the audited
alternative is `POST /connectors/credentials` per the Setup sequence above):

```powershell
docker compose exec postgres psql -U "<UMS_DB_USER>" -d "<UMS_DB_NAME>" -c "INSERT INTO api_connector_credentials (tenant_id, connector_key, account_id, encrypted_secret_ref) VALUES ('00000000-0000-0000-0000-000000000001', 'youtube-analytics', '<CONTENT_OWNER_ID>', 'gcp-secret-manager://projects/<project>/secrets/ums-google-oauth/versions/latest');"
```

(The `-U`/`-d` placeholders are quoted: unquoted angle brackets are parsed by
PowerShell as redirection operators, so the command failed before psql ever
ran. The placeholders inside the SQL string are safe — PowerShell does not
parse the contents of a quoted argument.)

Expected `INSERT 0 1`; a re-run fails on the unique constraint (correct — rotate the ref
with `UPDATE`, not a second `INSERT`).

**Step 4 — prove it** (host process; must target the Compose Postgres published on
localhost — Step 3 inserted into that container, and Compose's in-network hostname
`postgres` is unreachable from the host):

```powershell
# Use 127.0.0.1 and the published port (UMS_POSTGRES_PORT, default 5432), not hostname postgres.
$env:UMS_DATABASE_URL = "postgresql+psycopg://<UMS_DB_USER>:<UMS_DB_PASSWORD_URLENC>@127.0.0.1:<UMS_POSTGRES_PORT>/<UMS_DB_NAME>"
uv run python scripts/check_google_connector_credential.py --tenant 00000000-0000-0000-0000-000000000001 --connector youtube-analytics --account <CONTENT_OWNER_ID>
```

Expected exit 0 with `OK … token_expiry=<iso>`. This commits the telemetry that ARMS the
live-run gate for roughly one hour. Exit-2 first lines map to causes:
`SecretFetchError` → Step 2/IAM; `SecretNotFoundError` → the ref path;
`MalformedSecretPayloadError` → payload JSON; `OAuthRefreshError` → Step 0's token;
`CredentialNotFoundError` → Step 3.

**Step 5 — service actor (live runs only).** Provision a dedicated service
account (not a human operator): Super Owner `POST /users` with
`is_service_account: true`, grant `connectors.run_jobs`, then set
`UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` to that UUID. Do **not** reuse the UUID
printed by `bootstrap_operator.py` — that helper creates
`is_service_account=False` humans, and using it would attribute unattended
connector audit rows to the operator. Never use the public `.env.example`
placeholder. Under Compose, supply the UUID via the override documented in
[Supplying the service actor under `docker compose`](#supplying-the-service-actor-under-docker-compose).

**Step 6 — optional dry-run.** Costs the same ~54 API calls as a live run.
⚠️ *Not* free of side effects: the dry-run performs the full credential resolve, and a
transient `OAuthRefreshError` here commits a FAILED telemetry stamp that **disarms the
gate Step 4 just armed** — Step 7 then exits 2 even inside the hour. Recovery: re-run
Step 4.

**Step 7 — ONE live smoke month, within ~1h of Step 4.** First confirm the target month
is **not closed/locked** (this doc's own smoke-month rule): the locked-month prefilter
skips silently, so a locked month exits 0 `SUCCEEDED` with **zero facts written** —
reading as success while proving nothing. Expected on an open month: exit 0,
`SUCCEEDED … failures=[]`, facts written (in USD — disposable, per the sequencing box).
`PARTIAL` = zero facts, source rows only. Never run two copies concurrently (no
CLI-side lock). **Then stop until EGP Phase 4.**

Ops total: **~3–5.5h** including the SDK install and a realistic consent round-trip.
No UMS code is missing for this checklist — both scripts, the resolver, the schema and
the gates all exist and match.

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
