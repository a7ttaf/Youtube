# PR TBD - Google Credential Setup Smoke - Changelog

## Added

- `scripts/check_google_connector_credential.py`, a credential-only operator
  smoke command for a single tenant/connector/account. It uses the existing
  tenant context and Google credential resolver, commits only credential refresh
  telemetry, and does not create connector runs or ingestion artifacts.
- `tests/connectors/google/test_credential_smoke_cli.py` covering:
  - unknown connector argparse rejection;
  - missing `UMS_DATABASE_URL`;
  - successful telemetry commit;
  - no connector-run creation;
  - tenant lifecycle typed failure handling;
  - OAuth refresh error redaction.
- `Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md` with:
  - hard boundaries against Gmail sessions, browser cookies, raw secret storage,
    and premature live execution;
  - owner approval packet requirements;
  - Google OAuth secret payload shape;
  - GCP Secret Manager reference contract;
  - UMS credential registration and health/probe sequence;
  - credential-only CLI smoke and later ingestion CLI `--dry-run` smoke;
  - live-run gate and rollback/rotation notes;
  - references to existing test coverage.
- Standard PR report, changelog, and handoff artifacts for this branch.

## Changed

- `Docs/00_README.md` now indexes the Google credential setup/smoke runbook.
- `Docs/05_CONNECTORS_YOUTUBE_ADSENSE.md` now points operators to the runbook
  and requires credential-only smoke, probe, and dry-run evidence before live
  jobs.
- `Docs/12_BACKEND_API_SPEC.md` now describes the setup/credential-smoke/probe/
  dry-run order and the current production resolver boundary.
- `Docs/15_DELIVERY_BACKLOG.md` now records the runbook slice while preserving
  the live credential blocker.
- `Docs/16_OPEN_DECISIONS.md` now references the runbook without closing the
  real Google report/data decisions.

## Removed

Nothing.

## Runtime impact

The new CLI can refresh a Google OAuth token when an operator supplies real
credentials and database configuration. It does not call Google data APIs,
create connector runs, write raw files, upsert source rows, change schema,
change permissions, or add secret material.
