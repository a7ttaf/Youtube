# UMS YouTube Sentry Observability Design

## Status

Approved direction: production-grade, privacy-bounded Sentry integration for
the UMS Smart Revenue FastAPI backend and Vite/React frontend. This design is
independent from the OPUS implementation and must ship in its own repository
branch and PR.

## Problem

The `a7ttaf/Youtube` repository is visible to the GitHub integration in the
self-hosted Sentry organization, but repository synchronization alone does not
create Sentry projects, install SDKs, capture runtime failures, upload source
maps, or publish releases.

The current Sentry organization has no YouTube/UMS application project. The
repository contains no Sentry SDK configuration in either the FastAPI backend
or the Vite/React frontend.

## Goals

1. Create separate `youtube-backend` and `youtube-frontend` Sentry projects.
2. Capture FastAPI, Celery, connector executor, scheduler, and thread-boundary
   failures without changing finance, authorization, audit, or database logic.
3. Capture React runtime errors, navigation traces, and error-triggered replay.
4. Publish exact releases and Vite source maps to `https://validator.to`.
5. Link real frames to `a7ttaf/Youtube` and its `main` branch.
6. Give the private `sentry-validator` MCP enough runtime and source context
   for Codex diagnosis.
7. Prove the integration with controlled validation events.

## Non-goals

- Enabling hosted Sentry Seer.
- Changing financial calculations, audit data, tenancy, permissions, exports,
  SQLAlchemy models, Alembic migrations, or PostgreSQL data.
- Sending revenue values, report contents, Google credentials, trusted gateway
  tokens, request bodies, cookies, or authorization headers to Sentry.
- Making Sentry a dependency of application startup or request success.
- Sending source maps to a public CDN.

## Selected SDK versions

- Backend: `sentry-sdk` `2.68.1` with FastAPI and Celery integrations.
- Frontend: `@sentry/react` `10.73.0`.
- Source-map build plugin: `@sentry/vite-plugin` `5.4.0`.

These versions must be pinned in `pyproject.toml`, `uv.lock`,
`frontend/package.json`, and the frontend lockfile. Version changes require a
separate dependency review.

## Architecture

```text
Vite/React browser -----------------------> youtube-frontend
  errors, navigation spans, error replays

FastAPI/Celery/background workers --------> youtube-backend
  errors, request spans, task spans

Release workflow -------------------------> Sentry release API
  exact SHA, GitHub commits, Vite source maps

Codex <----- local read-only MCP <--------- validator.to
```

Separate projects keep browser and backend alerting, rates, ownership, and
source-map behavior understandable. They share the release identity
`youtube@<full-commit-sha>` so one deployment can be followed across both
stacks.

## Backend design

### Initialization

Add a focused observability module under
`backend/ums_smart_revenue/observability/`. It owns:

- Typed Sentry settings parsed from environment.
- One idempotent `init_sentry()` entry point.
- Event and transaction scrubbers.
- Safe release/environment tags.
- Test transport injection.

`create_app()` invokes initialization before constructing FastAPI only when a
non-empty DSN is present. SDK import or transport failure must be logged safely
and must not stop application construction.

Production defaults:

- Error sample rate: `1.0`.
- Trace sample rate: `0.10`.
- `send_default_pii=False`.
- Request bodies disabled.
- FastAPI, Starlette, SQLAlchemy, Redis, and Celery integrations enabled only
  when their dependencies are present.

### Background execution

Automatic framework capture does not cover every executor and scheduler
boundary. Explicit capture is allowed only where code already catches and
continues after an unexpected failure:

- `ConnectorJobExecutor` worker boundary.
- `GroupSyncScheduler` tick boundary.
- Celery task boundary when a task catches rather than re-raises.
- Lifespan shutdown failures after safe redaction.

Do not capture typed business, authorization, validation, or conflict errors
as unexpected exceptions. Do not duplicate-capture exceptions that propagate
to FastAPI or Celery automatic handlers.

### Backend privacy

The event scrubber must remove:

- Authorization, cookie, trusted-gateway, Google, and service-account headers.
- Request/response bodies and query strings.
- Revenue values, report/export payloads, spreadsheet content, and audit data.
- Database URLs, SQL parameters, tokens, credentials, and cloud resource names.
- Raw tenant/user identity. Stable safe IDs may be included only after a
  separate privacy review.

## Frontend design

Initialize `@sentry/react` in one browser entry module before React renders.
The SDK is enabled only when `VITE_SENTRY_DSN` is non-empty.

Production defaults:

- Browser trace sample rate: `0.10`.
- Session replay sample rate: `0`.
- Replay-on-error sample rate: `1.0`.
- Text and media masking enabled.
- Request bodies, authorization headers, trusted-gateway data, revenue table
  values, report previews, and export content excluded.

Add one React error boundary at the application root. Preserve existing user
error states; Sentry reporting must not replace or expose internal errors in
the UI.

## Source maps and releases

Configure `@sentry/vite-plugin` for production builds only.

The build must:

1. Use release `youtube@<full-commit-sha>`.
2. Upload debug-ID source maps to `https://validator.to`.
3. Associate the release with `a7ttaf/Youtube` commits.
4. Delete source maps from the public frontend artifact after upload.
5. Fail the release build if source-map upload was requested but incomplete.

`SENTRY_AUTH_TOKEN` is a CI build secret and must never enter the browser
bundle, Docker runtime image, repository, logs, or release artifacts.

## Sentry project and code-mapping contract

Create:

- `youtube-backend` using the Python/FastAPI platform.
- `youtube-frontend` using the JavaScript/React platform.

Both projects use repository `a7ttaf/Youtube` and default branch `main`.

Backend code mapping:

- Stack trace root: `/srv/app`.
- Source code root: empty.

The runtime path `/srv/app/backend/...` then maps to repository path
`backend/...`.

Frontend frames are resolved primarily by uploaded source maps. A code mapping
must be added only after a real source-mapped frame establishes its stable
stack root; an empty stack root must not be used speculatively.

## Configuration contract

| Variable | Consumer | Sensitivity | Purpose |
| --- | --- | --- | --- |
| `UMS_SENTRY_DSN` | FastAPI/workers | internal config | Backend event endpoint |
| `VITE_SENTRY_DSN` | frontend build | public DSN | Browser event endpoint |
| `SENTRY_ENVIRONMENT` | both runtimes | non-secret | stable environment name |
| `SENTRY_RELEASE` | both runtimes | non-secret | exact `youtube@<sha>` release |
| `SENTRY_AUTH_TOKEN` | CI build only | secret | release/source-map upload |
| `SENTRY_URL` | CI build | non-secret | `https://validator.to` |
| `SENTRY_ORG` | CI build | non-secret | `sentry` |

`.env.example`, Dockerfile, Compose, frontend build arguments, deployment docs,
and release workflows must remain mutually consistent. Public `VITE_*` values
must never contain an auth token or server-side secret.

## Security and privacy threat model

Assets:

- Revenue and financial calculations.
- Tenant, employee, channel, and company identities.
- Google credentials and connector payloads.
- Trusted gateway and database credentials.
- Private source and source maps.

| Threat | Control |
| --- | --- |
| Finance or export content enters an event | deny-by-default scrubber and regression tests |
| Replay records revenue data | mask text/media and error-only replay |
| Gateway or Google credential leaks | header/key denylist before transport |
| Source maps become public | authenticated upload and artifact deletion |
| Duplicate backend events | capture only at terminal ownership boundaries |
| Sentry outage affects finance operations | optional DSN and fail-open initialization |
| Wrong source/release association | full-SHA release identity and GitHub commit binding |

## Test strategy

Use test-first implementation with in-memory transports and no external Sentry
network calls.

Required tests:

1. Backend initialization enables only with a DSN and never blocks app startup.
2. Backend scrubbers remove request bodies, finance content, credentials,
   query strings, SQL values, and identity data.
3. FastAPI unexpected errors are captured once; typed expected errors are not.
4. Executor and scheduler terminal failures are captured once.
5. Frontend initialization enables only with a DSN.
6. Browser replay is error-only and privacy-masked.
7. Root error boundary reports without replacing existing UI behavior.
8. Vite release/source-map configuration binds to the exact SHA and removes
   public maps.
9. Docker and environment contracts do not expose build auth tokens.
10. Controlled backend and frontend events are readable through MCP and link
    to the expected GitHub source.

## Validation

At minimum:

- `uv sync --extra dev --extra test --extra lint`.
- Ruff, mypy if in the changed scope, targeted pytest, and full non-PostgreSQL
  pytest coverage.
- Disposable PostgreSQL suite with `UMS_TEST_DATABASE_URL` when database-backed
  startup contracts are exercised.
- Frontend install, typecheck, Vitest, and production Vite build.
- Docker build and health check.
- Source-map artifact inspection and secret scanning.
- Live controlled events, releases, code mapping, and MCP read-back.
- `git diff --check` and repository-required CI gates.

Baseline evidence before this work:

- Ruff passed on `origin/main` at `41b4953`.
- 474 tests passed before the run was stopped.
- 36 PostgreSQL tests errored because `UMS_TEST_DATABASE_URL` was not
  provisioned; this is an environment blocker, not a Sentry regression.

The implementation must not skip, xfail, suppress, or weaken those tests. It
must provision the documented disposable PostgreSQL service when the full
database suite is required.

## Rollout and rollback

Roll out in this order:

1. Land SDK code disabled by absent DSNs.
2. Create the two Sentry projects and backend code mapping.
3. Provision DSNs and CI build token outside the repository.
4. Enable backend capture in a validation environment and prove a controlled
   event.
5. Enable frontend errors/tracing and error-only replay.
6. Prove releases and source maps.
7. Promote the same immutable release artifact.

Rollback is configuration-first: blank the backend DSN and rebuild the
frontend without `VITE_SENTRY_DSN`. Finance, authorization, audit, database,
and application behavior remain independent. No migration or backfill is
required.

## References

- https://docs.sentry.io/platforms/python/integrations/fastapi/
- https://docs.sentry.io/platforms/javascript/guides/react/
- https://docs.sentry.io/platforms/javascript/sourcemaps/uploading/vite/
- https://docs.sentry.io/platforms/javascript/session-replay/
