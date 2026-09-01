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
| Secret location | GCP Secret Manager secret name and immutable numeric version; never use `latest` or paste the secret value into UMS |
| Runtime IAM | Grant `roles/secretmanager.secretAccessor` on the Secret resource. If approval requires version-only access, add an IAM Condition matching the approved numeric SecretVersion; otherwise record that the grant covers every version of that one secret |
| Tenant/account mapping | UMS tenant id, `connector_key`, and Google `account_id` or CMS content owner id |
| Service actor plan | Approved tenant, owner, and `system_integration_user` assignment. Provision it through the audited `/users` APIs only after Step 2 registers the credential reference, then record its UUID; never use a placeholder. The runtime currently validates UUID syntax but does not load that account. Under `docker compose` the value cannot be supplied through `.env` alone — see [Supplying the service actor under `docker compose`](#supplying-the-service-actor-under-docker-compose) |
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
  for production smoke, where `<version>` is the numeric immutable version id.
  The resolver can parse `latest`, but this runbook forbids that mutable alias:
  approval, registration, smoke evidence, and rollback must name the same version.
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
   - Record the created numeric version id. Grant the UMS runtime identity
     `roles/secretmanager.secretAccessor` on that specific secret rather than
     project-wide — the predefined role's lowest grant level is a secret.
     SecretVersion resources do not accept IAM bindings directly; if approval
     requires access to only that version, add and verify an owner-approved IAM
     Condition matching its resource name. Otherwise record that the grant covers
     every version of that secret. Independently, the immutable numeric version
     in the UMS reference pins the version UMS requests.

2. Verify the operator, then register the external reference in UMS.

For the credential-registration request, the trusted `X-User-ID` must be the UUID
of an existing operator row in the same tenant; a syntactically valid but absent
UUID fails with `422 actor_user_id does not reference an existing user`.
Header-bootstrap mode enforces same-tenant actor existence at the credential
repository boundary, while database auth additionally fails closed for disabled
principals before the route runs. On a freshly migrated local database, provision
an active human operator through the audited `POST /users` flow before this
request. The detailed local sequence below creates the row, assigns
`connector_admin`, and only then registers the credential.

```http
POST /connectors/credentials
```

```json
{
  "connector_key": "youtube-reporting",
  "account_id": "<content-owner-or-account-id>",
  "encrypted_secret_ref": "secret-manager://projects/<project>/secrets/<secret>/versions/<numeric-version>",
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
- The stored reference ends in the exact numeric version approved and uploaded
  for this smoke. Although the resolver supports the moving `latest` alias, this
  runbook forbids registering it because a later rotation would bypass the
  reference-change audit and fresh-smoke boundary.

This registration precedes service-actor provisioning. Creating a service actor
does not register connector credentials and is not a substitute for this audited
write; provision the actor later through `POST /users` as the live-run gate requires.

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
uv run python scripts/check_google_connector_credential.py --tenant "<tenant-uuid>" --connector youtube-reporting --account "<content-owner-id>"
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
uv run python scripts/run_google_connector.py --tenant "<tenant-uuid>" --connector youtube-reporting --account "<content-owner-id>" --month "<YYYY-MM>" --dry-run
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

— while the operator is looking at the line they set in `.env`. This is a
deployment gap, not an intentional safety mechanism. `.env.example` therefore
keeps the variable commented and supplies no UUID. `connectors/google/audit.py`
refuses only on *unset*, accepting any syntactically valid UUID, so use only the
service-account UUID recorded after audited provisioning.

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
- `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` is set to the recorded service-account
  UUID, **and the process that will run the job has been shown to see it.** Sign
  this off against observed output, not against a line in `.env`: Compose does
  not forward this particular variable, so
  `docker compose config | Select-String SERVICE_ACTOR` must print the variable.
  If it prints nothing, this item is NOT satisfied however the env file looks —
  see [Supplying the service actor under `docker compose`](#supplying-the-service-actor-under-docker-compose).
- The service account was created and assigned through the audited `/users`
  API. This is a governance check: the current runtime builds an in-memory
  principal carrying `connectors.run_jobs` and validates only that the configured
  value is a UUID; it does not verify an active SQL user or role assignment.
- The value is the service actor you provisioned, not a copied or public UUID.
  `.env.example` deliberately supplies no value because the runtime check refuses
  only on *unset* and accepts any syntactically valid UUID.
- The target month is not locked unless the owner explicitly approves the
  behavior for a closed month.
- Operator has reviewed the expected source tables and rollback note below.

## Rollback and rotation

- Revoke or rotate the Google refresh token in Google Cloud or the Google
  account authorization surface.
- Disable or destroy the affected GCP Secret Manager version after confirming no
  other UMS environment uses it.
- Until a credential lifecycle API exists, disabling an already-registered UMS
  credential row requires an owner-approved recovery-only database/admin
  operation against `api_connector_credentials.status`; use the break-glass
  boundary below, not ad-hoc setup SQL.
- Re-run the credential probe after any rotation. Do not reuse old smoke
  evidence for a new secret version.

## Operator checklist for a local smoke

This checklist is limited to capabilities present in this branch. Keep
workstation paths, credential payloads, account ids, and secret references in
the approved operator tracker or secret store, not in repository docs.

1. Rotate any credential that may have been exposed, then upload the JSON payload
   described above to GCP Secret Manager. Record the resulting numeric version;
   never register the mutable `latest` alias. Verify current `gcloud` syntax
   against the installed SDK or use the Cloud Console.
2. Give the runtime identity `roles/secretmanager.secretAccessor` on the Secret
   resource. SecretVersion resources do not accept IAM bindings directly. When
   the approval is version-specific, add and verify an IAM Condition matching
   the recorded numeric version; otherwise document that the binding covers all
   versions of that one secret. Host ADC is not automatically visible in Compose;
   mount and configure container credentials through an untracked override when
   the API container will resolve the secret.
3. Register the external reference through audited
   `POST /connectors/credentials` with an authorized principal. Expected status
   is `201`, `has_secret_ref: true`, and a `CONNECTOR_SETTINGS_CHANGED` audit
   event. A duplicate returns `409`. **Do not use superuser SQL:** it bypasses
   gateway authentication, RBAC, tenant RLS, actor stamping, and the required
   audit write.
4. If the audited API is unavailable, stop. Database recovery is a break-glass
   incident, not an alternate credential setup path. This runbook intentionally
   provides no copy-paste privileged SQL.
5. Run `scripts/check_google_connector_credential.py` against the operator-
   approved database URL and exact tenant/connector/account. A Compose database
   is reached from the host through `127.0.0.1` and its published port, not the
   container-only hostname `postgres`.
6. After credential registration succeeds, provision a service account through
   audited `POST /users`, then assign the
   `system_integration_user` role through `POST /users/{user_id}/roles`. Set
   `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` to that account UUID using the Compose
   override above. Record the current runtime limitation: connector execution
   validates only UUID syntax and fabricates its in-memory permission; the SQL
   account/role is operator governance, not yet a runtime-enforced lookup.
7. Run the credential probe, then the CLI dry-run. A dry-run still resolves the
   credential and can update refresh telemetry; if it records a failed refresh,
   re-run the credential probe before the live gate.
8. Run at most one owner-approved live smoke month, confirm the month is open,
   verify facts and audit rows, and stop before any backfill. The current finance
   path is USD-only; do not represent the smoke as EGP-ready.

## Verified operator workflow for a local smoke

This checklist is deliberately limited to the code and dependencies present on this
branch. Keep credential payloads and operator-specific paths in the approved secret
store and operator tracker, not in this repository. Confirm command flags against the
installed tools before a live run.

> ⚠️ **Currency sequencing (decided 2026-08-25: main currency = EGP).** The connector
> currently ingests **USD**. Run at most ONE smoke month through this checklist as
> disposable plumbing proof, then **STOP — no multi-month backfill, no daily sync —
> until the separately approved multi-currency work lands. Backfilled USD months can
> never match an EGP workbook acceptance baseline and would all be re-ingested.

**Step 0 — ROTATE the Google OAuth credential first (~1–2h incl. the consent round-trip).**
If a credential payload or client-secret file may have been exposed, mint a fresh client
secret and refresh token BEFORE any upload — enshrining a compromised credential in
Secret Manager is worse than retaining it locally. Keep local filenames and workstation
paths out of tickets and repository docs. Consent screen must not be in "Testing" status
(7-day token expiry; see the get_revenue.py docstring's own warning).

**Step 1 — install the Google Cloud SDK, then upload.** Install + `gcloud init` when the
SDK is unavailable, or use the Cloud Console UI instead. Verify the command syntax
against the installed SDK. Payload contract per this doc's "Secret payload contract"
section: UTF-8 JSON with `refresh_token`, `client_id`, `client_secret`, `token_uri`
(optional `scopes`).

```powershell
$projectId = "<project-id>"
$secretId = "ums-google-oauth"

# Run create only on first setup. If the secret already exists in this exact
# project, verify it with `gcloud secrets describe` and add a new version below.
gcloud secrets create $secretId --project=$projectId --replication-policy=automatic

$secretVersionResource = gcloud secrets versions add $secretId `
  --project=$projectId `
  --data-file="C:\<secure-path>\payload.json" `
  --format="value(name)"
if ($LASTEXITCODE -ne 0) {
  throw "Secret Manager version upload failed"
}
$secretVersionResource = "$secretVersionResource".Trim()
if ($secretVersionResource -notmatch '^projects/[^/]+/secrets/[^/]+/versions/(?<version>[1-9][0-9]*)$') {
  throw "Secret Manager did not return one numeric version resource name"
}
$approvedSecretVersion = $Matches.version
$approvedSecretRef = "secret-manager://$secretVersionResource"
```

Record `$projectId`, `$secretId`, `$approvedSecretVersion`, and
`$approvedSecretRef` in the secure operator tracker. These values are metadata,
not the payload, but the approval must still identify them exactly. Never replace
the numeric suffix with `latest`: a new version would then become live without a
credential-reference audit event or a fresh probe. Delete the local payload file
after the upload succeeds.

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

**Step 3 — provision the administrator and operator, then register through the
audited APIs.** The fresh local database has no user row, and credential creation
rejects an unknown actor before writing anything. For this local smoke only, start
the API in `UMS_AUTHZ_SOURCE=headers` bootstrap mode and use the trusted gateway
token once to create a durable bootstrap administrator row. That administrator
then creates the human connector operator and assigns `connector_admin`.
Production/database mode must instead start from an already provisioned SQL
principal; never switch production back to header role claims to run this
checklist.

```powershell
$bootstrapActorId = [guid]::NewGuid().ToString()
$bootstrapHeaders = @{
  "X-User-ID" = $bootstrapActorId
  "X-User-Email" = "<bootstrap-admin-email>"
  "X-Role" = "super_owner"
  "X-Scope-Type" = "global"
  "X-UMS-Trusted-Gateway-Token" = $env:UMS_TRUSTED_GATEWAY_TOKEN
  "X-UMS-Tenant" = "<tenant-slug>"
}
$bootstrapAdminPayload = @{
  email = "<bootstrap-admin-email>"
  display_name = "<bootstrap-admin-display-name>"
  reason = "Provision bootstrap administrator for local credential smoke"
} | ConvertTo-Json
$bootstrapAdmin = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/users" `
  -Headers $bootstrapHeaders -ContentType "application/json" -Body $bootstrapAdminPayload
if ($bootstrapAdmin.status -ne "active" -or $bootstrapAdmin.is_service_account -or
    $bootstrapAdmin.audit_event.event_type -ne "USER_ACCOUNT_CHANGED") {
  throw "Audited bootstrap-administrator provisioning did not complete"
}
$bootstrapAdminUserId = [guid]::Parse($bootstrapAdmin.id).ToString()

$bootstrapAdminHeaders = $bootstrapHeaders.Clone()
$bootstrapAdminHeaders["X-User-ID"] = $bootstrapAdminUserId
$bootstrapAdminHeaders["X-User-Email"] = "<bootstrap-admin-email>"

$operatorPayload = @{
  email = "<operator-email>"
  display_name = "<operator-display-name>"
  reason = "Provision connector operator for local credential smoke"
} | ConvertTo-Json
$operator = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/users" `
  -Headers $bootstrapAdminHeaders -ContentType "application/json" -Body $operatorPayload
if ($operator.status -ne "active" -or $operator.is_service_account -or
    $operator.audit_event.event_type -ne "USER_ACCOUNT_CHANGED") {
  throw "Audited human-operator provisioning did not complete"
}
$operatorUserId = [guid]::Parse($operator.id).ToString()

# The administrator actor exists, so this role write has a real actor FK and audit event.
$rolePayload = @{
  role_key = "connector_admin"
  scope_type = "global"
  reason = "Grant connector administration for local credential smoke"
} | ConvertTo-Json
$roleAssignment = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/users/$operatorUserId/roles" `
  -Headers $bootstrapAdminHeaders -ContentType "application/json" -Body $rolePayload
if ($roleAssignment.role_key -ne "connector_admin" -or
    $roleAssignment.audit_event.event_type -ne "USER_ROLE_CHANGED") {
  throw "Audited connector-admin assignment did not complete"
}
```

Record `$bootstrapActorId` with the named bootstrap administrator in the secure
operator tracker. The first `USER_ACCOUNT_CHANGED` event retains that external
gateway actor in its details because no local user row existed yet. The operator
creation and role-assignment events use `$bootstrapAdminUserId` as their
database-backed actor FK; only credential-registration events use
`$operatorUserId`.

If the bootstrap administrator or human operator already exists, do not create a
duplicate. Use an authorized `GET /users` lookup, verify that the row is active,
human, and in this tenant, and retain its exact UUID. The create block above is
the fresh-database path.

Only after the operator row and role assignment exist, register the credential.
Do not insert it with a database superuser. Reuse `$approvedSecretRef` from Step 1;
if the shell changed, reconstruct it from the recorded numeric version, never from
`latest`:

```powershell
$headers = @{
  "X-User-ID" = $operatorUserId
  "X-User-Email" = "<operator-email>"
  "X-Role" = "connector_admin"
  "X-Scope-Type" = "global"
  "X-UMS-Trusted-Gateway-Token" = $env:UMS_TRUSTED_GATEWAY_TOKEN
  "X-UMS-Tenant" = "<tenant-slug>"
}
if ($approvedSecretRef -notmatch '^secret-manager://projects/[^/]+/secrets/[^/]+/versions/[1-9][0-9]*$') {
  throw "Step 1 exact numeric Secret Manager version reference is required; latest is forbidden"
}
$youtubeAnalyticsConnector = "youtube-analytics"
$payload = @{
  connector_key = $youtubeAnalyticsConnector
  account_id = "<content-owner-or-account-id>"
  encrypted_secret_ref = $approvedSecretRef
  reason = "Register owner-approved Google credential reference for smoke"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/connectors/credentials" `
  -Headers $headers -ContentType "application/json" -Body $payload
```

Expected status `201`, `has_secret_ref: true`, and a `CONNECTOR_SETTINGS_CHANGED`
audit event. The response must not contain the external secret reference or raw
credential payload. A duplicate returns `409`; use the approved credential lifecycle
operation for rotation rather than a second insert.

**Recovery-only database intervention — not normal setup.** If the audited API is
unavailable, stop and open an approved break-glass incident before touching Postgres.
A database operator may restore or reconcile an already-approved credential row only
with the exact tenant, connector, account, and external reference recorded in that
incident. Direct database access bypasses gateway authentication, RBAC, RLS, actor
stamping, and the `CONNECTOR_SETTINGS_CHANGED` audit write; it is therefore not a
credential-registration path. Reconcile the missing audit evidence through the
approved incident process and verify the row through the API before any connector run.
No copy-paste SQL is provided here intentionally.

**Step 4 — prove it** (host process; target the Compose Postgres published on localhost
when the API and database run in the compose stack):

```powershell
# Use 127.0.0.1 and the published port (UMS_POSTGRES_PORT, default 5432), not hostname postgres.
$env:UMS_DATABASE_URL = "postgresql+psycopg://<UMS_DB_USER>:<UMS_DB_PASSWORD_URLENC>@127.0.0.1:<UMS_POSTGRES_PORT>/<UMS_DB_NAME>"
uv run python scripts/check_google_connector_credential.py --tenant 00000000-0000-0000-0000-000000000001 --connector youtube-analytics --account "<CONTENT_OWNER_ID>"
```

Expected exit 0 with `OK … token_expiry=<iso>`. This commits the telemetry that arms the
live-run gate for roughly one hour. Exit-2 first lines map to causes:
`SecretFetchError` → Step 2/IAM; `SecretNotFoundError` → the ref path;
`MalformedSecretPayloadError` → payload JSON; `OAuthRefreshError` → Step 0's token;
`CredentialNotFoundError` → Step 3.

**Step 5 — service actor (live runs only).** Provision a dedicated service
account (not a human operator): Super Owner `POST /users` with
`is_service_account: true`, grant the service-only `system_integration_user`
role (which carries `connectors.run_jobs`), then set
`UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` to that UUID. Do **not** reuse the human
`$operatorUserId` provisioned in Step 3; using it would attribute unattended
connector audit rows to the operator. Never substitute a placeholder UUID.
Under Compose, supply the UUID via the override documented in
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
The scripts, resolver, schema, and gates referenced here exist on this branch. Durable
application storage, deployment-readiness planning, and structured logging are separate
prerequisites and are not implied by this checklist.

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
