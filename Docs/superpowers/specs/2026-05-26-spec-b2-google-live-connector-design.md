# Spec B2 - Live Google Connector - Design Spec

**Date:** 2026-05-26
**Owner:** Director Software Architect / Mahmoud
**Status:** Design lock - multi-PR live Google connector (B2.1 -> B2.6) feeding
`google_revenue_source_rows` (PR #43 substrate). Revenue facts flow via C1 (PR #44)
for YouTube Reporting and YouTube Analytics paths only; the AdSense path is
ingestion/audit evidence in this phase (C1 skips AdSense rows as
`MISSING_CHANNEL_ID` until a future account-to-channel allocation spec).
**Primary docs:**
`Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md`,
`Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md`,
`Docs/12_BACKEND_API_SPEC.md`,
`Docs/18_MULTI_CURRENCY_ENGINE.md`,
`backend/ums_smart_revenue/connectors/`,
`backend/ums_smart_revenue/finance/google_source_normalizer.py`

---

## 1. Problem statement

PR #43 (Spec B1) shipped the tenant-scoped `google_revenue_source_rows`
substrate with idempotent source-row upserts and three deterministic parsers
(`YouTubeReportingParser`, `YouTubeAnalyticsParser`,
`AdSenseManagementParser`). PR #44 (Spec C1) shipped the
`GoogleSourceNormalizer` bridge that turns those source rows into entries in
`MonthlyChannelRevenueFactORM`. Both shipped as scaffolding only - no PR yet
wires real Google API calls into the parsers, so the substrate is dark.

Spec B2 is the wiring. It adds the live OAuth/API connector that fetches
reports from Google, persists raw response bytes, hands the parsed payloads to
the existing parsers, and lets C1 convert YouTube source rows into revenue
facts. AdSense source rows persist in `google_revenue_source_rows` as ingestion
evidence but do not yet become revenue facts (C1's `MISSING_CHANNEL_ID` skip
applies because `AdSenseManagementParser` emits `youtube_channel_id=None`).

B2 ships as **six PRs** (B2.1 -> B2.6) rather than one because the work spans
five distinct concerns (credentials, blob storage, run tracking, three HTTP
clients, orchestration) plus a CLI, an audit wiring slice, and a mock
end-to-end ingestion validation gate. Each slice produces compilable,
mergeable, individually-testable code, and each slice has a well-bounded
review surface.

The strategic reason this slicing is correct: the substrate, parsers,
normalizer, audit infrastructure, credentials ORM, and raw-file ORM are
already in main. B2 plugs *into* that stack. A single mega-PR would touch six
distinct layers at once and be impossible to review carefully; the six-PR
slicing lets each layer be reviewed against the layer it actually changes.

## 2. Goals

- Ship a CLI-only Google connector that fetches YouTube Reporting, YouTube
  Analytics, and AdSense Management reports for a given (tenant, account,
  month) and persists them into the PR #43 substrate.
- Reuse existing infrastructure unchanged: `ApiConnectorCredentialORM`
  (PR #33/#34), `RawReportFileORM` (PR #32), the three parsers (PR #43),
  the source-row repository (PR #43), the normalizer (PR #44), audit events
  (`CONNECTOR_JOB_RUN`, `REPORT_IMPORTED`), and permissions
  (`RUN_CONNECTOR_JOBS`).
- Add only what's missing: secret resolver dispatch, blob storage, raw-file
  lifecycle helpers, `connector_runs` + `connector_run_raw_files` tables,
  `google-auth`/`httpx` HTTP base, three Google API clients, an
  orchestrator, a CLI, and audit wiring.
- Make every PR independently shippable, with a local-only validation gate
  that runs purely on `httpx.MockTransport`, `local-secret://`, and
  `file-store://`. No PR gate touches the live Google API.
- Lock fail-closed behavior at every boundary: unknown secret schemes raise
  typed errors; missing/inactive credentials fail before any run starts;
  OAuth refresh failures abort; HTTP errors map to a typed taxonomy; parser
  failures are isolated per-report and recorded as `FAILED` raw_files; runs
  finish with one of `SUCCEEDED`, `PARTIAL`, `FAILED` based on aggregate
  per-report outcomes.
- Preserve C1's contract: B2 calls `GoogleSourceNormalizer.normalize_month(...)`
  with the existing public surface; no edits to `select_canonical_row()`,
  `MonthCloseORM` locking, `MonthlyChannelRevenueFactORM`, or the month-close
  gate.
- Update planning docs per PR: every B2.x PR edits exactly
  `Docs/01_IMPLEMENTATION_PLAN.md` and `Docs/15_DELIVERY_BACKLOG.md` inline
  with `⏳`/`✅` markers per the project convention. No new tracker file.

## 3. Non-goals

- No browser-based OAuth consent flow, no `/authorize`, no `/callback`,
  no UI. Operator obtains and rotates refresh tokens out-of-band; the
  resolver reads the resulting secret payload from GCP Secret Manager.
- No FastAPI route for connector runs. CLI-only entrypoint.
- No scheduler, cron, or background worker. Manual CLI invocation per
  (tenant, account, month).
- No FX tables or display-currency conversion (B3 owns that, per `Docs/15`).
- No AdSense-specific currency pre-check at the B2 layer. B2 ingests
  evidence; non-USD source rows that land are C1's decision via the existing
  `SkipReason.NON_USD_CURRENCY` path.
- No changes to `GoogleSourceNormalizer` (C1). B2 calls the existing public
  surface only.
- No changes to existing parsers (`YouTubeReportingParser`,
  `YouTubeAnalyticsParser`, `AdSenseManagementParser`). B2 produces payloads
  shaped exactly the way the parsers already require.
- No changes to `RawReportFileORM`, `ApiConnectorCredentialORM`,
  `MonthlyChannelRevenueFactORM`, or `MonthCloseORM` column or behavior
  shape. B2.3's Alembic migration adds one **additive** schema element to
  `raw_report_files`: a `UNIQUE (tenant_id, id)` constraint required so that
  `connector_run_raw_files` can declare a composite tenant-aware FK to it
  (S3/RLS-readiness). The new constraint is safe because `id` alone is
  already PK-unique - the wider `(tenant_id, id)` constraint can never be
  violated by existing data and needs no backfill.
- No new `AuditEventType` values. Reuse `CONNECTOR_JOB_RUN` and
  `REPORT_IMPORTED` with a `lifecycle` discriminator in the structured
  payload.
- No new `Permission` values. Reuse `RUN_CONNECTOR_JOBS` from PR #33/#34.
- No Neo4j or graph projection touches (Neo4j retired in PR #12; B2 touches
  no graph module).
- No `account_id` in raw-file blob path; run context lives on
  `connector_runs`.
- No `auth_method` column on `ApiConnectorCredentialORM`. Deferred to a
  future B2.x or credential-lifecycle spec; **not** to S3 (which is tenant
  and storage hardening per `Docs/01`).
- No implementation of `aws-secretsmanager://`, `secret-manager://`,
  `vault://`, `kms://`, or `azure-keyvault://` resolvers. Only
  `gcp-secret-manager://` and `local-secret://` are implemented; other
  prefixes pass ORM validation but raise `UnsupportedSecretSchemeError` at
  resolve time.
- No `mark_downloaded` (FAILED -> DOWNLOADED) explicit-cycle helper and no
  `mark_quarantined` lifecycle helper. Retry recovery is supported
  implicitly: `mark_parsed` accepts FAILED -> PARSED (a previously-failed
  row succeeds on re-parse) and `mark_failed` is idempotent on FAILED ->
  FAILED (overwrites `error_class`/`error_summary`). A future PR can add
  an explicit `--retry-failed` CLI flag if operators need it; the
  `mark_downloaded` and `mark_quarantined` helpers themselves are deferred.
- No AdSense feeding the revenue-facts chain in this phase. AdSense rows are
  ingestion/audit evidence only. Revenue facts come from YouTube Reporting
  (B2.4) and YouTube Analytics (B2.5) only. AdSense-to-channel allocation
  is a future spec.
- No live Google API calls in any PR validation gate. Live smoke testing is
  operator-driven post-deploy.
- No CODEOWNERS, branch-protection, or other repo-governance edits.
- No deletion of mockups, planning docs, or other operator-owned assets.

## 4. Six-PR slicing and module layout

### 4.1 Slicing rationale

| PR | Scope | Why it ships independently |
|---|---|---|
| **B2.1** | Secret resolver dispatch + Google OAuth refresh wrapper | Pure credential layer; no DB writes, no runs, no audit. Reviewable as a self-contained module with mock secret backends. |
| **B2.2** | Blob storage backends + `raw_report_files` lifecycle helpers (`mark_parsed`, `mark_failed`) | Pure storage layer; reuses existing `RawReportFileORM` (PR #32) without schema change. Reviewable with a `file-store://` backend test. |
| **B2.3** | `connector_runs` + `connector_run_raw_files` ORM + repo + Alembic; **additive** `UNIQUE (tenant_id, id)` constraint on `raw_report_files` (required for the composite FK from `connector_run_raw_files`); operational indexes on both new tables | Pure run-tracking ORM + repo. Adds two new tables on `ReportBase.metadata` and one additive unique constraint on the existing `raw_report_files`. Reviewable with a SQLite repo test + PostgreSQL migration round-trip + cross-tenant FK rejection assertion. |
| **B2.4** | `google-auth` + `httpx` base client + YouTube Reporting client + report_type whitelist + `run_one()` orchestrator + CLI | First end-to-end slice: fetches a YT Reporting report, registers it as a DOWNLOADED raw file, calls the parser, upserts source rows, marks PARSED. All glue lives here. CLI is extensible via a `--connector` registry so later slices add clients without new CLI code. |
| **B2.5** | YouTube Analytics client (targeted channel ingestion incl. outside-CMS); extends CLI `--connector` registry | Targeted YouTube Analytics channel ingestion; reuses the B2.4 orchestrator and CLI. Reviewable as a client-only diff that targets channels from the `youtube_channels` registry (including outside-CMS channels). |
| **B2.6** | AdSense Management client (ingestion/audit evidence only); extends CLI `--connector` registry; AdSense ingestion validation gate; audit wiring | Final slice that wires audit events for run lifecycle and raw-file lifecycle, then runs the mock end-to-end ingestion gate. AdSense is the third source for ingestion; it does **not** produce revenue facts (C1 skips its rows as `MISSING_CHANNEL_ID`). |

Every slice ships compilable, mergeable, individually-testable code with a
local-mocked validation gate. No slice depends on a later slice to function;
each one's tests run green at the slice's HEAD commit.

### 4.2 Module layout

```text
backend/ums_smart_revenue/
  connectors/
    credentials/                       # existing (PR #33/#34) - no schema change
      __init__.py
      ...
    google/                            # B2.1, B2.4, B2.5, B2.6
      __init__.py
      secret_resolver.py               # B2.1 - dispatching resolver
      gcp_secret_manager.py            # B2.1 - GCP Secret Manager backend
      local_secret_resolver.py         # B2.1 - test backend
      oauth.py                         # B2.1 - google-auth refresh wrapper
      http_client.py                   # B2.4 - httpx base with retry policy
      report_type_whitelist.py         # B2.4 - YT Reporting supported types
      registry.py                      # B2.4 - CLI --connector dispatch
      youtube_reporting_client.py      # B2.4
      youtube_analytics_client.py      # B2.5
      adsense_management_client.py     # B2.6
      audit.py                         # B2.6 - service principal + emitters
    runs/                              # B2.3, B2.4
      __init__.py
      repository.py                    # B2.3 - start_run, finish_run, link_raw_file
      blob_storage.py                  # B2.2 - GCS + file-store backends
      raw_file_helpers.py              # B2.2 - mark_parsed, mark_failed (+ guards)
      orchestrator.py                  # B2.4 - run_one() public surface
  db/
    connector_models.py                # B2.3 - ConnectorRunORM, ConnectorRunRawFileORM
    alembic/versions/
      20260527_0001_connector_runs.py  # B2.3 - migration on ReportBase
scripts/
  run_google_connector.py              # B2.4 - CLI entrypoint
tests/
  connectors/
    google/                            # per-client tests, oauth, secret resolver
    runs/                              # repository, ingestion gate
  db/
    test_connector_runs_migration_postgres.py
```

Files NOT touched by any B2 PR (load-bearing for the "preserve C1 / preserve
parsers / preserve substrate" guarantee):

- `backend/ums_smart_revenue/connectors/google_source_parsers/*` (PR #43)
- `backend/ums_smart_revenue/connectors/google_source_rows/*` (PR #43)
- `backend/ums_smart_revenue/finance/google_source_normalizer.py` (PR #44)
- `backend/ums_smart_revenue/db/source_models.py` (PR #43)
- `backend/ums_smart_revenue/auth/audit.py`, `audit_service.py`,
  `sql_audit_sink.py`, `models.py` (existing audit infra)
- `backend/ums_smart_revenue/auth/permissions.py` (no new permission)
- `backend/ums_smart_revenue/db/report_models.py` (RawReportFileORM)
- `backend/ums_smart_revenue/db/security_models.py` (AuditLogORM, users)

## 5. Per-PR public surface

### 5.1 B2.1 - Credential foundation

**Scope:** secret resolver dispatch + Google OAuth refresh wrapper.

**New files:**
- `connectors/google/secret_resolver.py` - dispatching resolver
- `connectors/google/gcp_secret_manager.py` - GCP backend
- `connectors/google/local_secret_resolver.py` - test backend
- `connectors/google/oauth.py` - google-auth refresh wrapper

**Public surface:**

```python
# secret_resolver.py
class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str: ...

def resolve_secret(secret_ref: str) -> str:
    """Dispatch to the implemented resolver for the URI scheme.
    Implemented: gcp-secret-manager://, local-secret://.
    Other ORM-accepted prefixes raise UnsupportedSecretSchemeError."""

# oauth.py
def build_credentials_from_payload(payload: str) -> Credentials:
    """Parse a resolved JSON secret payload and build google-auth Credentials.
    Required fields: refresh_token, client_id, client_secret, token_uri.
    Missing/malformed JSON: MalformedSecretPayloadError."""

def refresh_credentials(credentials: Credentials) -> None:
    """Trigger google-auth refresh; raise OAuthRefreshError on failure."""
```

**Errors introduced (subclasses of `GoogleConnectorError`):**
`UnsupportedSecretSchemeError`, `MalformedSecretUriError`,
`SecretNotFoundError`, `SecretFetchError`, `MalformedSecretPayloadError`,
`OAuthRefreshError`.

**Invariants:**
- No DB writes; no audit events; no run state.
- `gcp-secret-manager://projects/{project}/secrets/{name}/versions/{v|latest}`
  is the only production URI shape.
- `local-secret://{name}` is backed by an injected mapping (test only).

### 5.2 B2.2 - Blob storage + raw_file lifecycle helpers

**Scope:** blob storage backends + `raw_report_files` lifecycle helpers.

**New files:**
- `connectors/runs/blob_storage.py` - GCS + file-store backends
- `connectors/runs/raw_file_helpers.py` - `mark_parsed`, `mark_failed`

**Public surface:**

```python
# blob_storage.py
class BlobStorageBackend(Protocol):
    def upload(self, *, storage_uri: str, content: bytes) -> None: ...
    def get_bytes(self, *, storage_uri: str) -> bytes: ...

def deterministic_blob_path(
    *, bucket: str, tenant_id: UUID, connector_key: str,
    report_type: str, month: str, checksum: str, ext: str,
) -> str:
    """Returns: gs://{bucket}/{tenant}/{connector}/{report_type}/{month}/{checksum}.{ext}
    Note: account_id is NOT in the path - run context lives on connector_runs."""

def upload_and_verify(
    *, backend: BlobStorageBackend, storage_uri: str, content: bytes,
) -> str:
    """Upload, re-read, verify SHA-256. Returns computed checksum.
    BlobUploadError on upload failure; BlobChecksumMismatchError on mismatch."""

# raw_file_helpers.py
def mark_parsed(
    session: Session, *, raw_file_id: UUID, tenant_id: UUID,
) -> None:
    """Accepts DOWNLOADED -> PARSED (success) and FAILED -> PARSED (retry
    recovery: a previously-failed row is re-parsed successfully). Refuses
    QUARANTINED. Raises RawFileAlreadyParsedError on PARSED -> PARSED
    (orchestrator should idempotency-skip first); RawFileLifecycleError on
    any other illegal transition."""

def mark_failed(
    session: Session, *, raw_file_id: UUID, tenant_id: UUID,
    error_class: str, error_summary: str,
) -> None:
    """Accepts DOWNLOADED -> FAILED and FAILED -> FAILED (idempotent re-fail:
    error_class and error_summary are overwritten with the new values).
    Refuses QUARANTINED, PARSED. error_summary truncated to 500 chars."""
```

**Errors introduced:** `BlobUploadError`, `BlobChecksumMismatchError`,
`RawFileLifecycleError`, `RawFileAlreadyParsedError`.

**Invariants:**
- Blob upload always happens **before** raw_file row registration on the
  first-attempt path. On first-attempt upload failure, no raw_file row exists.
- Retry-recovery path: on re-run, the orchestrator looks up an existing
  raw_file by `(tenant_id, source, report_type, report_month, checksum)`.
  If a FAILED row is found, it is reused (no re-insert); its blob is
  re-uploaded idempotently to the same deterministic path; a successful
  re-parse promotes FAILED -> PARSED via `mark_parsed`. A failed re-parse
  overwrites the FAILED error fields via `mark_failed` (FAILED -> FAILED
  idempotent).
- No `mark_downloaded` (FAILED -> DOWNLOADED) explicit-cycle helper or
  `mark_quarantined` lifecycle helper. QUARANTINED rows are refused by
  both `mark_parsed` and `mark_failed`; QUARANTINED state is set externally
  (admin / future PR).
- Deterministic path means same bytes always go to same path - idempotent
  re-uploads overwrite or hit existing object; either is acceptable.

### 5.3 B2.3 - Connector runs ORM + repo

**Scope:** new tables for run tracking, with a join table linking runs to
the raw files they produced.

**New files:**
- `db/connector_models.py` - `ConnectorRunORM`, `ConnectorRunRawFileORM`
- `db/alembic/versions/20260527_0001_connector_runs.py`
- `connectors/runs/repository.py` - `start_run`, `finish_run`, `link_raw_file`

**ORM contracts:**

```python
# ConnectorRunORM - on ReportBase.metadata
#   id                    UUID primary key
#   tenant_id             UUID NOT NULL (tenant-scoped)
#   connector_key         text NOT NULL (e.g., 'youtube-reporting')
#   account_id            text NOT NULL
#   report_month          text NOT NULL (YYYY-MM)
#   triggered_by_user_id  UUID nullable, composite FK on (tenant_id, user_id)
#   started_at            timestamptz NOT NULL
#   finished_at           timestamptz nullable
#   status                text NOT NULL ('RUNNING' | 'SUCCEEDED' | 'PARTIAL' | 'FAILED')
#   counts_json           jsonb NOT NULL (fixed shape; see below)
#   error_summary         text nullable (truncated to 500 chars)
#   UNIQUE (tenant_id, id)  -- required so the join table can FK on (tenant_id, id)

# B2.3 Alembic migration also ADDs UNIQUE (tenant_id, id) to raw_report_files
# (additive, safe: id alone is already PK-unique, so the wider constraint
# always holds; no backfill needed). With both parents carrying that
# constraint, the join table below declares its composite FKs cleanly.

# ConnectorRunRawFileORM - join table on ReportBase.metadata
#   id                    UUID primary key
#   tenant_id             UUID NOT NULL
#   connector_run_id      UUID NOT NULL, composite FK -> connector_runs (tenant_id, id)
#   raw_report_file_id    UUID NOT NULL, composite FK -> raw_report_files (tenant_id, id)
#   linked_at             timestamptz NOT NULL
#   ordering_index        int NOT NULL (deterministic order of linkage)
#   UNIQUE (tenant_id, connector_run_id, raw_report_file_id)
#     -- doubles as the index for run -> raw_files lookups (find all
#     -- raw files linked to a given run); PostgreSQL uses this UNIQUE
#     -- as a btree on the leading columns.
#   INDEX ix_connector_run_raw_files_tenant_raw_file (tenant_id, raw_report_file_id)
#     -- reverse lookup: find which runs touched a given raw file.
#     -- Required because PostgreSQL does NOT auto-index FK columns.

# ConnectorRunORM operational indexes (added in the same B2.3 migration):
#   INDEX ix_connector_runs_tenant_connector_month
#       (tenant_id, connector_key, report_month)
#     -- operator query: "find runs for connector X in month Y".
#   INDEX ix_connector_runs_tenant_started
#       (tenant_id, started_at DESC)
#     -- operator query: "show recent runs for this tenant".
```

`counts_json` fixed shape (every key present, defaults to 0):

```json
{
  "reports_attempted": int,
  "reports_succeeded": int,
  "reports_failed": int,
  "rows_upserted_total": int,
  "rows_upserted_created": int,
  "rows_upserted_updated": int,
  "rows_upserted_unchanged": int
}
```

**Repo public surface:**

```python
def start_run(
    session: Session, *,
    tenant_id: UUID, connector_key: str, account_id: str,
    report_month: str, triggered_by_user_id: UUID | None,
) -> ConnectorRunEntry:
    """Insert a RUNNING row. Committed alongside the CONNECTOR_JOB_RUN/STARTED
    audit event in the same transaction."""

def link_raw_file(
    session: Session, *,
    tenant_id: UUID, connector_run_id: UUID, raw_report_file_id: UUID,
    ordering_index: int,
) -> None:
    """Insert a tenant-scoped join row. report_type is derived from the
    loaded RawReportFileORM (NOT accepted as caller input)."""

def finish_run(
    session: Session, *,
    tenant_id: UUID, connector_run_id: UUID,
    status: Literal["SUCCEEDED", "PARTIAL", "FAILED"],
    counts: dict[str, int],
    error_summary: str | None,
) -> ConnectorRunEntry:
    """Set finished_at, status, counts_json, error_summary. Committed
    alongside the CONNECTOR_JOB_RUN/FINISHED audit event in the same
    transaction."""
```

**Invariants:**
- Both new tables live on `ReportBase.metadata`; `db/alembic/env.py` already
  imports `connector_models` for migration discovery (B2.3 adds the import).
- All FKs are composite tenant-aware - `(tenant_id, raw_report_file_id)`,
  `(tenant_id, user_id)`, `(tenant_id, connector_run_id)` - for S3 RLS
  readiness without B2 doing S3 work.
- `error_summary` is truncated to 500 chars at write time; never exposes
  secret material.

### 5.4 B2.4 - HTTP base + YouTube Reporting + orchestrator + CLI

**Scope:** the first end-to-end slice. Adds `google-auth`/`httpx` base, the
YouTube Reporting client, the `run_one()` orchestrator that wires every
prior slice together, and the CLI entrypoint with an extensible
`--connector` registry.

**New files:**
- `connectors/google/http_client.py` - httpx base with retry policy
- `connectors/google/report_type_whitelist.py` - YT Reporting supported types
- `connectors/google/registry.py` - CLI `--connector` dispatch
- `connectors/google/youtube_reporting_client.py`
- `connectors/runs/orchestrator.py` - `run_one()` public surface
- `scripts/run_google_connector.py` - CLI entrypoint

**Public surface:**

```python
# http_client.py
class GoogleHttpClient:
    def __init__(
        self, *, credentials: Credentials,
        transport: httpx.BaseTransport | None = None,  # test injection
    ) -> None: ...

    def request(
        self, *, method: str, url: str,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Pre-request: credentials.before_request(...). Apply retry policy
        (Section 6). Return parsed JSON. Raise typed GoogleApi*Error on
        failure."""

# youtube_reporting_client.py
SUPPORTED_REPORT_TYPES: frozenset[str] = frozenset({
    "channel_basic_a2", "channel_combined_a2",
    # ... (locked at B2.4 ship; outside list raises UnsupportedReportTypeError)
})

class YouTubeReportingClient:
    def __init__(self, *, http: GoogleHttpClient) -> None: ...

    def list_supported_jobs(self, *, account_id: str) -> list[ReportingJob]:
        """GET /v1/jobs - list the operator's reporting jobs filtered by
        SUPPORTED_REPORT_TYPES. Paginated via pageToken."""

    def list_reports_for_month(
        self, *, account_id: str, job_id: str, report_month: str,
    ) -> list[ReportingReport]:
        """GET /v1/jobs/{jobId}/reports with date filter:
            startTimeAtOrAfter=<month-first>T00:00:00Z
            startTimeBefore=<next-month-first>T00:00:00Z
        Paginated via pageToken. Returns ReportingReport entries that each
        carry a `downloadUrl` to pass into fetch_report()."""

    def fetch_report(self, *, download_url: str) -> bytes:
        """GET <downloadUrl> with the same Bearer auth used elsewhere.
        Google's downloadUrl is API-token-authenticated, not signed-URL-
        authenticated. Returns raw CSV bytes for blob upload and parser
        ingestion."""

# orchestrator.py
@dataclass(frozen=True)
class ConnectorRunOutcome:
    run: ConnectorRunEntry | None  # None for dry-run
    counts: dict[str, int]
    per_report_failures: list[tuple[str, str]]  # (report_type_id, error_class)

def run_one(
    session: Session, *,
    tenant_id: UUID,
    connector_key: str,        # 'youtube-reporting' | 'youtube-analytics' | 'adsense-management'
    account_id: str,
    report_month: str,         # YYYY-MM
    dry_run: bool = False,
    triggered_by_user_id: UUID | None = None,
) -> ConnectorRunOutcome:
    """Public orchestrator surface. Dispatch is via the connector registry."""

# registry.py
def register_connector(
    key: str, runner: Callable[..., None],
) -> None: ...

def dispatch_connector(
    key: str,
) -> Callable[..., None]:
    """Returns the registered runner for the connector_key.
    Unknown key raises ValueError at CLI argparse time."""
```

**CLI contract:**

```text
scripts/run_google_connector.py
    --tenant <UUID>
    --connector {youtube-reporting | youtube-analytics | adsense-management}
    --account <account-id>
    --month <YYYY-MM>
    [--dry-run]
```

**Errors introduced:**
`GoogleApiAuthError`, `GoogleApiClientError`, `GoogleApiRateLimitError`,
`GoogleApiServerError`, `GoogleApiResponseError`,
`UnsupportedReportTypeError`, `CredentialNotFoundError`,
`InactiveCredentialError`.

**Invariants:**
- Dry-run writes **nothing**: no `connector_runs` row, no raw_file row, no
  blob upload, no source-row upserts, no audit events. Returns
  `ConnectorRunOutcome(run=None, counts={...}, per_report_failures=[])` with
  counts that report what *would* have been written.
- Real run path is per Section 6 (failure model), Section 7 (retry
  policy), and the per-report try/except loop that produces `SUCCEEDED |
  PARTIAL | FAILED` aggregate status.
- Parsers are called with `.parse(payload, tenant_id=...)`, never
  `.parse_payload(...)`.
- Pre-request OAuth refresh: every `GoogleHttpClient.request()` call invokes
  `credentials.before_request(...)` before sending, so google-auth handles
  refresh-token rotation through its own state machine.
- CLI is the public *operator* surface; the orchestrator is the public
  *service* surface. Other future callers (FastAPI route, scheduler) call
  `run_one(...)` directly without going through the CLI.

### 5.5 B2.5 - YouTube Analytics (targeted channel ingestion)

**Scope:** add the YouTube Analytics client for targeted channel
ingestion. Channels are sourced from the `youtube_channels` registry
(PR #25), which already includes outside-CMS channels - this is how B2 ingests
revenue for channels that are not under CMS.

**New files:**
- `connectors/google/youtube_analytics_client.py`

**Modified files (additive):**
- `connectors/google/registry.py` - registers `youtube-analytics`
- `connectors/runs/orchestrator.py` - dispatch table extended (no signature
  change)

**Public surface:**

```python
# youtube_analytics_client.py
class YouTubeAnalyticsClient:
    def __init__(self, *, http: GoogleHttpClient) -> None: ...

    def fetch_channel_report(
        self, *, channel_id: str, report_month: str,
    ) -> dict[str, object]:
        """Fetch a single channel's monthly report. Returns the parser-ready
        payload dict (the existing YouTubeAnalyticsParser.parse() input shape)."""

def list_target_channels(
    session: Session, *, tenant_id: UUID, account_id: str,
) -> list[str]:
    """Read the youtube_channels registry for the tenant. Returns channel IDs
    where:
      - active = true
      - revenue_required = true
      - content_owner_id = account_id  OR  content_owner_id IS NULL
        (outside-CMS channels are always included for the tenant)
    Order is deterministic (youtube_channel_id ascending)."""
```

**Invariants:**
- B2.5 does not add a new CLI; it extends the B2.4 CLI's `--connector`
  registry with `youtube-analytics`.
- The channel list is sourced from the registry, never hardcoded.
- Per-channel HTTP failures are bucket-B per-report failures (the
  orchestrator continues to the next channel and may finish as PARTIAL).

### 5.6 B2.6 - AdSense Management + audit + ingestion validation gate

**Scope:** add the AdSense Management client (the third source for
ingestion), wire audit events for run and raw-file lifecycle, and run a
mock end-to-end ingestion gate that proves the entire pipeline
(credentials -> fetch -> blob -> raw_file -> parser -> source rows -> audit)
end-to-end on local mocks.

**New files:**
- `connectors/google/adsense_management_client.py`
- `connectors/google/audit.py` - service principal + audit emitters

**Modified files (additive):**
- `connectors/google/registry.py` - registers `adsense-management`
- `connectors/runs/orchestrator.py` - dispatch table extended; audit emitters
  wired at the lifecycle points (Section 7)

**Public surface:**

```python
# adsense_management_client.py
ADSENSE_REPORT_GENERATE_URL = (
    "https://adsense.googleapis.com/v2/accounts/{account}/reports:generate"
)
SUPPORTED_ADSENSE_REPORTS: frozenset[str] = frozenset({"monthly_account_earnings"})

class AdSenseManagementClient:
    def __init__(self, *, http: GoogleHttpClient) -> None: ...

    def fetch_monthly_report(
        self, *, account_id: str, report_month: str,
    ) -> dict[str, object]:
        """Fetch monthly_account_earnings. Query params include:
            dateRange=CUSTOM
            startDate.{year,month,day} / endDate.{year,month,day}
            dimensions=MONTH
            metrics=ESTIMATED_EARNINGS,PAID_AMOUNT
            currencyCode=USD
        Returns the parser-ready payload dict."""

def adsense_response_to_parser_payload(
    *, response_json: dict, account_id: str, report_month: str,
) -> dict[str, object]:
    """Wrap the AdSense API response for AdSenseManagementParser.parse().
    Output: {request, headers, rows, report_id}. report_id is a deterministic
    SHA-256 of (account_id, report_month, query_key) since the API does not
    return a stable report id."""

# audit.py
def build_connector_service_principal(*, tenant_id: UUID) -> UserPrincipal:
    """Construct a UserPrincipal for CLI/orchestrator-driven audit writes.
    user_id = settings.UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID (UUID provisioned
    in users table at deploy time, per tenant, with RUN_CONNECTOR_JOBS)."""

def emit_run_started(session, *, principal, run): ...
def emit_run_finished(session, *, principal, run): ...
def emit_raw_file_downloaded(session, *, principal, run, raw_file): ...
def emit_raw_file_parsed(session, *, principal, run, raw_file, count_upserted): ...
def emit_raw_file_failed(session, *, principal, run, raw_file, error_class): ...
```

**Invariants:**
- AdSense is **ingestion/audit evidence only** in this phase. C1 skips
  AdSense source rows as `MISSING_CHANNEL_ID` because
  `AdSenseManagementParser` emits `youtube_channel_id=None`. The revenue-
  facts chain remains YouTube Reporting (B2.4) + YouTube Analytics (B2.5).
- Audit events reuse the existing `AuditEventType.CONNECTOR_JOB_RUN` (for
  run lifecycle) and `AuditEventType.REPORT_IMPORTED` (for raw-file
  lifecycle) with a `lifecycle` discriminator in the payload (Section 8).
- No new `AuditEventType` values, no new `Permission` values.
- The mock end-to-end ingestion validation gate test exercises three mock
  backends (YT Reporting, YT Analytics, AdSense Management) through the
  same orchestrator + CLI, proving the ingestion substrate works on
  `httpx.MockTransport` + `local-secret://` + `file-store://`. It does
  not validate the revenue-facts chain for AdSense (AdSense rows skip in
  C1 as `MISSING_CHANNEL_ID`); YT-Reporting + YT-Analytics rows do flow
  through C1 to produce facts, and that assertion is part of the gate.

## 6. Failure model

All B2 errors subclass `GoogleConnectorError(Exception)`. The orchestrator
routes errors through three lifecycle-scoped handlers - A, B, C -
distinguishing pre-run, per-report partial failure, and terminal run
failure.

### 6.1 Handler A - Pre-`start_run`

Covers: all B2.1 secret errors
(`UnsupportedSecretSchemeError`, `MalformedSecretUriError`,
`SecretNotFoundError`, `SecretFetchError`, `MalformedSecretPayloadError`),
`CredentialNotFoundError`, `InactiveCredentialError`, and
`OAuthRefreshError` raised during initial token build.

Outcome:
- No `connector_runs` row exists; no raw_file row exists; no audit event
  emitted.
- Error bubbles to the caller / CLI (exit non-zero, `error_class` +
  truncated `error_summary` on stderr).

### 6.2 Handler B - Post-`start_run`, report-level handled failures

The orchestrator's per-report `try/except` inside the for-loop. Covers:

- All B2.4 HTTP errors (`GoogleApiAuthError`, `GoogleApiClientError`,
  `GoogleApiRateLimitError`, `GoogleApiServerError`,
  `GoogleApiResponseError`)
- `UnsupportedReportTypeError`
- `BlobUploadError`, `BlobChecksumMismatchError`
- `ParserError`, `RawFileLifecycleError`, `RawFileAlreadyParsedError`

Per-report handler:
- If a raw_file id is in scope at the failure point, mark raw_file `FAILED`
  via `mark_failed` and emit `REPORT_IMPORTED` (`lifecycle=FAILED`).
  Otherwise no per-file audit (no raw_file row to reference).
- Append the failure to the orchestrator's local outcome list, then
  **continue to the next report**.

After the for-loop completes, the orchestrator calls
`finish_run(status=...)`:

| Aggregate outcome | Terminal status | Audit emission |
|---|---|---|
| Every report parsed | `SUCCEEDED` | `CONNECTOR_JOB_RUN/FINISHED/SUCCEEDED` |
| Some parsed, some failed | `PARTIAL` | `CONNECTOR_JOB_RUN/FINISHED/PARTIAL` |
| Every report failed | `FAILED` | `CONNECTOR_JOB_RUN/FINISHED/FAILED` |

### 6.3 Handler C - Post-`start_run`, terminal/unhandled failures

Errors that escape handler B and abort the run before the for-loop
completes. Covers:

- Mid-run `OAuthRefreshError` (token rotation failed; the auth wrapper
  bubbles this above the per-report try/except since no further
  authenticated calls can succeed for this run).
- Any non-`GoogleConnectorError` exception (DB connection lost,
  `OperationalError`, unexpected `Exception`).

Outer handler:
- Mark any in-flight raw_file `FAILED` via `mark_failed` (if a raw_file id
  is in scope) and emit `REPORT_IMPORTED` (`lifecycle=FAILED`).
- Mark the `connector_runs` row `FAILED` with `error_class` + truncated
  `error_summary`.
- Emit `CONNECTOR_JOB_RUN` (`lifecycle=FINISHED, status=FAILED`).
- Re-raise to caller; CLI exits non-zero.

### 6.4 Raw_file scope by error class

Applies inside handler B; handler C uses the same scope check on its
in-flight raw_file id.

| Always in scope (raw_file exists; `mark_failed` fires) | Never in scope (no raw_file row; no per-file audit) | May or may not be in scope (handler checks before emitting) |
|---|---|---|
| `ParserError`, `RawFileLifecycleError`, `RawFileAlreadyParsedError` | All HTTP errors (`GoogleApi*Error`); `UnsupportedReportTypeError` | `BlobUploadError`, `BlobChecksumMismatchError` (first-attempt path: blob upload precedes raw_file register, so no raw_file in scope; retry-recovery path: orchestrator's checksum lookup may find a prior FAILED raw_file before re-uploading, so raw_file IS in scope) |

### 6.5 Full error taxonomy

| Class | Slice | Trigger | Truncated `error_summary` (≤500 chars) |
|---|---|---|---|
| `UnsupportedSecretSchemeError` | B2.1 | scheme has no registered resolver | `unsupported secret scheme: <scheme>` |
| `MalformedSecretUriError` | B2.1 | URI does not parse for chosen resolver | `malformed secret URI: <ref>` |
| `SecretNotFoundError` | B2.1 | resolver responded "no such secret" (404 / NotFound) | `secret not found: <ref>` |
| `SecretFetchError` | B2.1 | resolver call failed (network, 5xx, other provider error) | `secret fetch failed for <ref>: <provider_error_class>` |
| `MalformedSecretPayloadError` | B2.1 | resolved payload is bad JSON or missing required fields | `malformed secret payload: <field_missing or json_error>` |
| `CredentialNotFoundError` | B2.4 | no `ApiConnectorCredentialORM` for (tenant_id, connector_key, account_id) | `no credential for <connector_key>/<account_id>` |
| `InactiveCredentialError` | B2.4 | credential row exists but `status != "active"` | `credential <id> is <status>, not active` |
| `OAuthRefreshError` | B2.1 (initial) / B2.4 (mid-run) | google-auth `refresh()` or initial token build failed | `oauth refresh failed: <google_auth_error_class>` |
| `GoogleApiAuthError` | B2.4 | HTTP 401/403 *after* successful refresh | `<METHOD> <url>: HTTP <status>` |
| `GoogleApiClientError` | B2.4 | HTTP 400/404/422 (request rejected by server) | `<METHOD> <url>: HTTP <status>` |
| `GoogleApiRateLimitError` | B2.4 | HTTP 429 after exhausted retries | `<METHOD> <url>: HTTP 429 after <attempts> retries` |
| `GoogleApiServerError` | B2.4 | HTTP 5xx / timeout after exhausted retries | `<METHOD> <url>: HTTP <status> after <attempts> retries` |
| `GoogleApiResponseError` | B2.4 | response is not JSON, or schema mismatch | `<url>: response schema invalid (<reason>)` |
| `UnsupportedReportTypeError` | B2.4 | YT Reporting `report_type_id` not in whitelist | `report_type_id <id> not in supported set` |
| `BlobUploadError` | B2.2 | GCS / `file-store://` write failed | `blob upload failed for <storage_uri>: <provider_error_class>` |
| `BlobChecksumMismatchError` | B2.2 | re-read SHA-256 != computed SHA-256 | `checksum mismatch at <storage_uri>: computed=<a> read=<b>` |
| `RawFileLifecycleError` | B2.2 | invalid `parse_status` transition | `raw_file <id>: <current> -> <target> not permitted` |
| `RawFileAlreadyParsedError` | B2.2 | transition attempted on already-PARSED row | `raw_file <id> already parsed` |
| `ParserError` | (existing, B1 parsers) | parser rejected payload | passed through |

**Currency handling note:** B2 does **not** introduce an AdSense-specific
currency pre-check error. The AdSense client requests `currencyCode=USD`
to keep the default-supported path, but if the server rejects (4xx ->
`GoogleApiClientError`) or the response shape mismatches (`ParserError`
from the existing parser's `METRIC_CURRENCY` header check), the existing
taxonomy already covers it. Non-USD source rows that *do* land in
`google_revenue_source_rows` are C1's concern, not B2's: C1 skips them as
`SkipReason.NON_USD_CURRENCY` per existing finance-layer behavior.

## 7. Retry policy

All Google HTTP calls (B2.4 base, reused by YT Reporting / YT Analytics /
AdSense):

| HTTP / transport state | Policy |
|---|---|
| 200 OK | proceed |
| 400 / 404 / 422 | raise `GoogleApiClientError`, NO retry |
| 401 / 403 | raise `GoogleApiAuthError`, NO retry (pre-request refresh already ran) |
| 429 | exp backoff 1s/2s/4s/8s, max 4 attempts, honor `Retry-After` (clamp ≤ 64s) -> `GoogleApiRateLimitError` |
| 500 / 502 / 503 / 504 | exp backoff 1s/2s/4s/8s, max 4 attempts -> `GoogleApiServerError` |
| connect/read timeout | exp backoff 1s/2s/4s/8s, max 4 attempts -> `GoogleApiServerError` |
| DNS / TCP reset | exp backoff 1s/2s/4s, max 3 attempts -> `GoogleApiServerError` |
| non-JSON or schema-mismatched response | raise `GoogleApiResponseError`, NO retry (request succeeded; payload is wrong) |

Per-attempt timeout: connect 5s, read 60s.

## 8. Audit wiring (B2.6)

### 8.1 Imports and enum reuse

```python
from ums_smart_revenue.auth.audit import AuditEventType, AUDIT_EVENT_DEFINITIONS
from ums_smart_revenue.auth.audit_service import record_audit_event
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.models import UserPrincipal
```

**No new `AuditEventType` values.** Reuse `AuditEventType.CONNECTOR_JOB_RUN`
(for run lifecycle) and `AuditEventType.REPORT_IMPORTED` (for raw-file
lifecycle) with a `lifecycle` discriminator inside the structured payload.
Both events already require `Permission.RUN_CONNECTOR_JOBS`.

### 8.2 Lifecycle payload shapes

| Enum | `lifecycle` value | Payload fields (no secrets, no error_summary text) |
|---|---|---|
| `CONNECTOR_JOB_RUN` | `STARTED` | `lifecycle, run_id, connector_key, account_id, report_month, dry_run` |
| `CONNECTOR_JOB_RUN` | `FINISHED` | `lifecycle, run_id, status` (`SUCCEEDED|PARTIAL|FAILED`)`, counts, error_summary_present: bool` |
| `REPORT_IMPORTED` | `DOWNLOADED` | `lifecycle, run_id, raw_file_id, source, report_type, report_month, checksum, storage_uri` |
| `REPORT_IMPORTED` | `PARSED` | `lifecycle, run_id, raw_file_id, count_upserted` |
| `REPORT_IMPORTED` | `FAILED` | `lifecycle, run_id, raw_file_id, error_class` (class name only, not message body) |

### 8.3 Service principal

The orchestrator builds a service `UserPrincipal` via
`build_connector_service_principal(tenant_id=...)`. The principal carries:

- `user_id = settings.UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` (UUID
  provisioned at deploy time in `users` table, per tenant, with
  `RUN_CONNECTOR_JOBS`).
- `tenant_id = run.tenant_id`.
- `permissions = frozenset({Permission.RUN_CONNECTOR_JOBS})`.

The same UUID is used for `raw_report_files.downloaded_by`.
`connector_runs.triggered_by_user_id` stays nullable for CLI-only runs.

### 8.4 Audit transaction semantics

| Audit event | Commit alignment |
|---|---|
| `CONNECTOR_JOB_RUN/STARTED` | Committed alongside `start_run` (forensics; persists through later orchestrator crash). |
| `REPORT_IMPORTED/{DOWNLOADED,PARSED,FAILED}` | Staged in the main transaction with their corresponding lifecycle row writes; roll back together if the orchestrator aborts before commit. |
| `CONNECTOR_JOB_RUN/FINISHED` | Committed alongside `finish_run`. |

Dry-runs emit zero audit events (consistent with "dry-run writes nothing").

## 9. Testing posture and per-PR validation gates

### 9.1 Testing posture (all PRs)

Every PR validation gate runs purely on local mocks:

- `httpx.MockTransport` for HTTP
- `local-secret://` resolver backed by injected mappings for credentials
- `file-store://` backend for blobs

No PR gate calls the live Google API. Live smoke testing of the Google
connector against real Google APIs is operator-driven, performed after
deploy and after real OAuth refresh tokens are provisioned in GCP Secret
Manager. It is **not** a PR merge gate.

### 9.2 Per-PR validation gate

| PR | Command bundle |
|---|---|
| **B2.1** | `ruff check backend tests` · `pytest -q tests/connectors/google/test_secret_resolver.py tests/connectors/google/test_oauth.py` · `scripts/run_validation_gate.py` · `git diff --check` |
| **B2.2** | `ruff check backend tests` · `pytest -q tests/connectors/google/test_blob_storage.py tests/connectors/google/test_raw_file_helpers.py tests/reports/test_raw_files.py` · gate · `git diff --check` |
| **B2.3** | `ruff check backend tests` · `pytest -q tests/connectors/runs/test_repository.py tests/db/test_connector_runs_migration_postgres.py` · gate · `git diff --check` |
| **B2.4** | `ruff check backend tests scripts` · `pytest -q tests/connectors/google/test_http_client.py tests/connectors/google/test_youtube_reporting_client.py tests/connectors/google/test_orchestrator.py tests/connectors/google/test_run_one_cli.py` · gate · `git diff --check` |
| **B2.5** | `ruff check backend tests` · `pytest -q tests/connectors/google/test_youtube_analytics_client.py tests/connectors/google/test_orchestrator.py` · gate · `git diff --check` |
| **B2.6** | `ruff check backend tests scripts` · `pytest -q tests/connectors/google/ tests/connectors/runs/ tests/finance/test_google_source_normalizer_*.py tests/db/test_connector_runs_migration_postgres.py tests/auth/test_audit_service.py tests/auth/test_audit_tenant_scope.py` · gate · `git diff --check` |

Gate = `python scripts/run_validation_gate.py` (the full repo gate per
PR #38). Test filename convention follows the existing repo pattern -
SQLite default has no suffix, Postgres companion uses `_postgres.py` (e.g.
`tests/finance/test_google_source_normalizer_postgres.py`).

### 9.3 Test coverage by PR

**B2.1** (`test_secret_resolver.py`, `test_oauth.py`):
- Unknown scheme -> `UnsupportedSecretSchemeError`.
- Bad URI shape -> `MalformedSecretUriError`.
- Local backend miss -> `SecretNotFoundError`.
- Provider 500 -> `SecretFetchError`.
- Bad JSON / missing fields -> `MalformedSecretPayloadError`.
- Stub google-auth `refresh()` to raise -> `OAuthRefreshError`.

**B2.2** (`test_blob_storage.py`, `test_raw_file_helpers.py`):
- Deterministic path generation (every input shape).
- `file-store://` upload + re-read + checksum verify.
- `BlobUploadError` on injected write failure.
- `BlobChecksumMismatchError` on injected mismatch.
- `mark_parsed` happy paths: DOWNLOADED -> PARSED and FAILED -> PARSED
  (retry recovery). Refuses QUARANTINED; raises `RawFileAlreadyParsedError`
  on PARSED -> PARSED.
- `mark_failed` happy paths: DOWNLOADED -> FAILED and FAILED -> FAILED
  (idempotent overwrite of `error_class`/`error_summary`). Refuses
  QUARANTINED and PARSED.
- `RawFileLifecycleError` on any other illegal transition (e.g.
  DOWNLOADED -> DOWNLOADED, PARSED -> FAILED).

**B2.3** (`test_repository.py`, `test_connector_runs_migration_postgres.py`):
- `start_run` writes RUNNING with all fields, returns entry.
- `link_raw_file` derives `report_type` from the loaded raw_file
  (NOT from caller input); enforces tenant scope.
- `finish_run` updates status / counts_json / error_summary; truncates
  summary; rejects illegal statuses.
- `counts_json` always has the fixed shape (all keys present).
- PostgreSQL migration round-trip on disposable `postgres:18-alpine`
  (up + down clean), including:
  - `connector_runs` carries `UNIQUE (tenant_id, id)`.
  - `raw_report_files` gains the additive `UNIQUE (tenant_id, id)`
    constraint (no data backfill; constraint always holds because `id`
    is already PK-unique).
  - `connector_run_raw_files` declares composite FKs against
    `(tenant_id, id)` on both parent tables; rejects cross-tenant
    inserts at the DB level.
  - Indexes present (queried via `pg_indexes` after `upgrade()`):
    `ix_connector_run_raw_files_tenant_raw_file`,
    `ix_connector_runs_tenant_connector_month`,
    `ix_connector_runs_tenant_started`.

**B2.4** (`test_http_client.py`, `test_youtube_reporting_client.py`,
`test_orchestrator.py`, `test_run_one_cli.py`):
- HTTP client: every retry path with `httpx.MockTransport`; honors
  `Retry-After`; clamps backoff to 64s; pre-request refresh invoked.
- HTTP errors map to the right typed class (one test per row in
  Section 7).
- YT Reporting client:
  - `list_supported_jobs` filters by `SUPPORTED_REPORT_TYPES`; paginates
    correctly via `pageToken`.
  - `list_reports_for_month` uses correct `startTimeAtOrAfter` /
    `startTimeBefore` boundaries (first instant of month / first instant
    of next month, UTC); paginates via `pageToken`; returns reports in a
    deterministic order with their `downloadUrl` populated.
  - `fetch_report` downloads from the response's `downloadUrl` using the
    same Bearer-auth `GoogleHttpClient` path; returns raw CSV bytes.
  - Unsupported `report_type_id` -> `UnsupportedReportTypeError`.
- Orchestrator: end-to-end with mocked YT Reporting + injected secret +
  `file-store://`: produces source rows, marks raw_file PARSED, finishes
  run SUCCEEDED.
- Orchestrator dry-run: writes nothing; returns `ConnectorRunOutcome(run=None,
  counts=...)`.
- Partial-run: one report fails, one succeeds -> finishes `PARTIAL`.
- All-fail: every report fails -> finishes `FAILED`.
- Pre-run errors do not create a `connector_runs` row.
- Mid-run OAuth refresh failure marks run FAILED via handler C.
- CLI: argparse rejects unknown `--connector`; happy path dispatches.

**B2.5** (`test_youtube_analytics_client.py`,
`test_orchestrator.py` extensions):
- `list_target_channels` returns only the registered channels (incl.
  outside-CMS); deterministic order.
- Per-channel fetch with mocked transport.
- Orchestrator with `connector_key='youtube-analytics'` reuses the same
  three-bucket failure model; produces source rows for every active
  channel.
- Coverage extension: a fixture with N active channels + 1 non-USD channel
  proves the per-channel partial-failure path is exercised end-to-end.

**B2.6** (mock end-to-end ingestion gate +
`test_adsense_management_client.py` +
`test_audit_wiring.py`):
- AdSense client: query params match spec (Section 5.6); fetch returns
  parser-ready payload; adapter wraps response with deterministic
  `report_id`.
- Audit wiring: every lifecycle event emits the right
  `AuditEventType` + `lifecycle` discriminator + payload shape.
- Service principal: `build_connector_service_principal` produces a
  `UserPrincipal` with `RUN_CONNECTOR_JOBS` from the configured actor
  UUID.
- Audit transaction semantics: `STARTED`/`FINISHED` committed with their
  rows; per-raw-file events staged and roll back on orchestrator abort.
- Ingestion gate (`tests/connectors/runs/test_ingestion_gate.py`):
  - Three mock backends (YT Reporting + YT Analytics + AdSense).
  - Mock ingestion pipeline runs end-to-end on `httpx.MockTransport` +
    `local-secret://` + `file-store://`.
  - Asserts: source rows present for all three connectors; YT-Reporting +
    YT-Analytics rows produce facts via C1; AdSense rows skip in C1 as
    `SkipReason.MISSING_CHANNEL_ID` (B2.6 is ingestion/audit evidence only,
    not revenue-facts validation); audit log carries the expected event
    sequence with the right principal.
- Existing audit regression coverage (`tests/auth/test_audit_service.py`,
  `tests/auth/test_audit_tenant_scope.py`) runs green, proving B2.6's
  audit reuse did not regress existing audit behavior.

### 9.4 Explicit B2 non-tests

- No live Google API calls in any PR gate.
- No live GCS uploads in any PR gate (`gs://...` paths are constructed
  but `file-store://` is the runtime backend in tests).
- No live secret manager calls in any PR gate (`local-secret://` only).
- No frontend tests (no UI surface).
- No FastAPI route tests (no route).
- No scheduler / cron tests (no scheduler).

## 10. Blast radius, rollout, and operator tasks

### 10.1 Per-PR blast radius

| PR | Source-of-truth tables touched | Audit events emitted | Secret manager | Graph |
|---|---|---|---|---|
| B2.1 | none | none | yes (read-only by ref) | None detected |
| B2.2 | `raw_report_files` (lifecycle helpers) | none | none | None detected |
| B2.3 | `connector_runs` (new), `connector_run_raw_files` (new); additive `UNIQUE (tenant_id, id)` constraint added to existing `raw_report_files` (no data backfill - the constraint always holds because `id` is already PK-unique) | none | none | None detected |
| B2.4 | `google_revenue_source_rows` (upsert via PR #43 repo), `raw_report_files` (lifecycle), `connector_runs` (lifecycle) | none | yes (via B2.1) | None detected |
| B2.5 | same as B2.4 (targeted YT Analytics channel ingestion; reuses orchestrator + CLI) | none | yes (via B2.1) | None detected |
| B2.6 | same as B2.4 + `audit_log` (existing) | `CONNECTOR_JOB_RUN` {STARTED, FINISHED with status in {SUCCEEDED, PARTIAL, FAILED}}, `REPORT_IMPORTED` {DOWNLOADED, PARSED, FAILED} | yes | None detected |

**Authorization touch (all 6):** none. No change to `Permission`, `User`,
`Role`, `RolePermission`, scope checks, fail-closed behavior, or
trusted-gateway middleware. B2.6 only *reads* `RUN_CONNECTOR_JOBS` from
the existing service principal.

**Finance correctness touch (all 6):** none to the calculation surface.

- Revenue-facts chain (B2.4 + B2.5 only): YouTube Reporting and YouTube
  Analytics flow into `MonthlyChannelRevenueFactORM` via the existing
  PR #43 -> PR #44 chain because both YT parsers emit a real
  `youtube_channel_id`. B2.5 targets the channels registered in
  `youtube_channels`, including outside-CMS channels, so the
  YT-Analytics path expands coverage beyond the YT-Reporting CMS slice.
- B2.6 (AdSense) is ingestion/audit evidence only: AdSense rows land in
  `google_revenue_source_rows` (PR #43 substrate) but do **not** produce
  revenue facts in this phase. C1 skips them as
  `SkipReason.MISSING_CHANNEL_ID` because `AdSenseManagementParser`
  emits `youtube_channel_id=None`. AdSense-derived revenue facts
  require a future account-to-channel allocation/mapping spec.
- No edits to `select_canonical_row()`, `MonthCloseORM` locking,
  `MonthlyChannelRevenueFactORM` schema, or month-close gate.

**No graph projection impact detected.** Evidence: every PR's "Graph"
column above is `None detected`; Neo4j retired in PR #12; no graph module
touched by `connectors/`, `auth/audit`, or
`finance/google_source_normalizer.py`.

### 10.2 Rollout

PR order is strict: B2.1 -> B2.2 -> B2.3 -> B2.4 -> B2.5 -> B2.6.
Each PR is mergeable on its own; each one's validation gate runs green at
its HEAD commit. Per-PR plan updates: every B2.x PR edits exactly
`Docs/01_IMPLEMENTATION_PLAN.md` and `Docs/15_DELIVERY_BACKLOG.md` inline
with the appropriate `⏳`/`✅` marker. No new tracker file.

### 10.3 Operator deploy-time tasks (one-time, before live runs)

These are NOT part of any PR's code. They are operator actions taken
after B2.6 is merged and before the first live run.

- Provision a service-actor user row in `users` (per tenant or one
  platform-admin user) with `RUN_CONNECTOR_JOBS` permission. Note the
  user's UUID.
- Set `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID=<that UUID>` in the deploy
  environment.
- Provision OAuth refresh tokens in GCP Secret Manager at paths like
  `projects/<project>/secrets/google-<connector_key>-<account_id>/versions/latest`
  with payload `{refresh_token, client_id, client_secret, token_uri}`.
- For each `(tenant_id, connector_key, account_id)` triple to be
  ingested, insert a row in `api_connector_credentials` with
  `encrypted_secret_ref = "gcp-secret-manager://projects/<project>/secrets/<name>/versions/latest"`
  and `status = "active"`.
- Run the CLI smoke test once per (tenant, connector, account):
  `python scripts/run_google_connector.py --tenant <UUID> --connector youtube-reporting --account <id> --month 2026-05 --dry-run`,
  then drop `--dry-run` for the real first run.

### 10.4 Out-of-scope follow-ups

These are explicit deferrals; each is a candidate future spec.

- **Account-to-channel allocation spec** - unlocks AdSense rows -> revenue
  facts. Today, AdSense lands in `google_revenue_source_rows` but C1
  skips them as `MISSING_CHANNEL_ID`. A future spec defines how an
  AdSense account_id maps to one or more `youtube_channel_id`s (likely
  a registry + allocation rule), then C1 or a successor normalizer can
  produce facts.
- **Official non-USD finance-source policy/spec** - until then, non-USD
  source rows that land in `google_revenue_source_rows` skip in C1 as
  `SkipReason.NON_USD_CURRENCY`. B3 covers display-only currency
  conversion, not official non-USD ingestion.
- **Explicit retry-recovery tooling** - retry recovery via `mark_parsed`'s
  FAILED -> PARSED transition and `mark_failed`'s FAILED -> FAILED
  idempotence is already in B2 scope. What's deferred: a `mark_downloaded`
  (FAILED -> DOWNLOADED) explicit-cycle helper, a `mark_quarantined`
  lifecycle helper, and a `--retry-failed` CLI flag that explicitly targets
  prior FAILED rows.
- **FastAPI route + scheduler** - post-B2 work to expose
  `run_one(...)` over HTTP and trigger it on a cron.
- **`auth_method` column on `ApiConnectorCredentialORM`** - if a future
  spec adds non-OAuth credential types (e.g., service-account JSON), the
  ORM gains a discriminator column at that time. Not S3.
- **Additional resolver schemes** - `aws-secretsmanager://`,
  `vault://`, etc. Today they pass ORM URI validation but raise
  `UnsupportedSecretSchemeError` at resolve. A future credential-
  lifecycle spec implements them as needed.
