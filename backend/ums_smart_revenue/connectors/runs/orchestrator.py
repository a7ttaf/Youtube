"""B2.4 orchestrator: the public ``run_one(...)`` surface.

Happy path (this task, T27):
  1. ``_load_credential(session, tenant_id, connector_key, account_id)`` returns
     the ``ApiConnectorCredentialORM`` row or ``None``.
  2. ``resolve_secret(credential.encrypted_secret_ref)`` returns the payload
     string registered behind the URI scheme.
  3. ``build_credentials_from_payload(payload)`` returns a google-auth
     ``Credentials``.
  4. ``refresh_credentials(...)`` performs the initial token refresh; any
     ``OAuthRefreshError`` escapes pre-``start_run`` (bucket A, T28).
  5. ``start_run(...)`` commits the ``RUNNING`` row (forensic durability for
     ``started_at`` even if the process dies mid-loop).
  6. ``dispatch_connector(key=connector_key)`` returns a ``ConnectorRunner``
     instance whose ``produce_reports`` yields ``(report_type, parser_payload,
     raw_bytes)`` tuples — one per Google report for the requested month.
  7. For each tuple:
        a. ``compute_checksum`` + ``deterministic_blob_path`` build the URI.
        b. ``upload_and_verify`` writes the blob and re-reads its SHA-256.
        c. A ``RawReportFileORM`` row is inserted with ``parse_status``
           ``DOWNLOADED``; ``session.flush()`` populates ``raw_file_id``.
        d. ``link_raw_file`` joins the raw file to the run with a per-run
           ``ordering_index``.
        e. The parser's ``parse(...)`` is consumed into a ``list`` so an
           early ``ParserError`` does not surface mid-upsert.
        f. ``SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many(...)``
           upserts the rows; the returned list length feeds
           ``rows_upserted_total``.
        g. ``mark_parsed`` transitions the raw file ``DOWNLOADED -> PARSED``.
  8. ``finish_run`` records the terminal status (SUCCEEDED / PARTIAL / FAILED)
     plus the accumulated counts and (optionally) an error summary, and
     ``session.commit()`` persists it.
  9. Returns an immutable ``ConnectorRunOutcome``.

Failure handlers (buckets B/C beyond the inner per-report ``except``) and
dry-run land in T28 / T29; audit wiring lands in B2.6 (T37). Existing files
under ``connectors/google/`` (errors, http client, registry, secret resolvers,
oauth, blob storage, raw-file helpers, the YT client) and the existing
parser/repo (PR #43) are *not* touched here — T27 is additive.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import (
    CredentialNotFoundError,
    GoogleConnectorError,
    InactiveCredentialError,
)
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.oauth import (
    build_credentials_from_payload,
    refresh_credentials,
)
from ums_smart_revenue.connectors.google.registry import (
    dispatch_connector,
    register_connector,
)
from ums_smart_revenue.connectors.google.secret_resolver import resolve_secret
from ums_smart_revenue.connectors.google.youtube_reporting_client import (
    YouTubeReportingClient,
)
from ums_smart_revenue.connectors.google_source_parsers import (
    YouTubeReportingParser,
)
from ums_smart_revenue.connectors.google_source_rows import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.connectors.runs.blob_storage import (
    BlobStorageBackend,
    GcsBlobStorageBackend,
    LocalFileStoreBackend,
    compute_checksum,
    deterministic_blob_path,
    upload_and_verify,
)
from ums_smart_revenue.connectors.runs.raw_file_helpers import mark_parsed
from ums_smart_revenue.connectors.runs.repository import (
    CONNECTOR_RUN_COUNT_KEYS,
    ConnectorRunEntry,
    finish_run,
    link_raw_file,
    start_run,
)
from ums_smart_revenue.db.report_models import RawReportFileORM
from ums_smart_revenue.db.security_models import ApiConnectorCredentialORM

__all__ = [
    "ConnectorRunOutcome",
    "ConnectorRunner",
    "YouTubeReportingRunner",
    "run_one",
]

# Columns the CSV-to-parser-payload adapter consumes directly (as date,
# channel attribution, or metrics). Anything else flows into the parser's
# ``dimensions`` dict so the source_row_key dedup picks it up. Lower-cased
# for case-insensitive header matching.
_RESERVED_CSV_COLUMNS = frozenset(
    {
        "date",
        "day",
        "channel",
        "channel_id",
        "content_owner",
        "estimated_partner_revenue",
        "estimatedrevenue",
        "ad_revenue",
        "currency_code",
        "currencycode",
    }
)


# ----------------------------------------------------------------------------
# Public surface
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectorRunOutcome:
    """Immutable orchestrator return value.

    ``run`` is the finished ``ConnectorRunEntry`` (None is reserved for the
    T29 dry-run branch). ``counts`` mirrors the B2.3 ``CONNECTOR_RUN_COUNT_KEYS``
    shape that ``finish_run`` validates. ``per_report_failures`` lists
    ``(report_type_id, error_class_name)`` for each report that failed inside
    the per-report ``except`` handler; the happy path returns an empty list.
    """

    run: ConnectorRunEntry | None
    counts: dict[str, int]
    per_report_failures: list[tuple[str, str]]


class ConnectorRunner(Protocol):
    """Per-connector adapter contract.

    Each runner owns the API-client/credential bridge for its source system
    (B2.4 wires YouTube Reporting; B2.5/B2.6 will register YouTube Analytics
    and AdSense Management). ``produce_reports`` yields one tuple per Google
    report and stays decoupled from blob storage, raw-file lifecycle,
    upserts, and the connector_runs lifecycle — the orchestrator owns those
    uniformly across all three connectors.
    """

    def produce_reports(
        self,
        *,
        session: Session,
        run: ConnectorRunEntry,
        credentials: Credentials,
        report_month: str,
        account_id: str,
    ) -> Iterator[tuple[str, dict[str, object], bytes]]:
        ...


# ============================================================================
# Purpose: Drive one connector_runs lifecycle end-to-end for a single
#          (tenant, connector_key, account_id, report_month). Loads the
#          stored credential, refreshes OAuth, starts the run, iterates the
#          dispatched runner's reports (blob upload + raw-file insert +
#          parse + upsert + mark_parsed for each), and finishes the run.
# Database/ORM: ApiConnectorCredentialORM (read), ConnectorRunORM /
#               RawReportFileORM / ConnectorRunRawFileORM (write via
#               repository / lifecycle helpers), GoogleRevenueSourceRowORM
#               (write via SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many).
# Standards: Typed-error contract via GoogleConnectorError subclasses.
#            Two explicit session.commit() points: one after start_run for
#            forensic ``started_at`` durability, one after finish_run for
#            terminal status durability. The inner per-report block catches
#            GoogleConnectorError so a single bad report doesn't abort the
#            whole run; the outer try/except routes terminal failures into a
#            FAILED finish_run (bucket C; full handler depth is T28).
# Blast Radius: This is the ONLY production path that writes connector_runs
#               rows, attaches raw_report_files to them, and upserts Google
#               source rows during a live ingestion. Authorization, audit,
#               finance, exports, and the Neo4j projection are unaffected:
#               source-of-truth writes go through the existing tenant-scoped
#               repository which keeps PostgreSQL authoritative.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/repository.py ->
#     start_run / finish_run / link_raw_file / ConnectorRunEntry.
#   - File: backend/ums_smart_revenue/connectors/runs/blob_storage.py ->
#     deterministic_blob_path / compute_checksum / upload_and_verify.
#   - File: backend/ums_smart_revenue/connectors/runs/raw_file_helpers.py ->
#     mark_parsed (DOWNLOADED -> PARSED transition).
#   - File: backend/ums_smart_revenue/connectors/google/secret_resolver.py ->
#     resolve_secret (B2.1 secret-URI dispatch).
#   - File: backend/ums_smart_revenue/connectors/google/oauth.py ->
#     build_credentials_from_payload / refresh_credentials.
#   - File: backend/ums_smart_revenue/connectors/google/registry.py ->
#     register_connector / dispatch_connector (T26 dispatch).
#   - File: backend/ums_smart_revenue/connectors/google_source_rows/repository.py
#     -> SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many.
#   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
#     §5.4 -> orchestrator contract (load → start → loop → finish).
# ============================================================================
def run_one(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    report_month: str,
    dry_run: bool = False,
    triggered_by_user_id: UUID | None = None,
) -> ConnectorRunOutcome:
    """See module docstring. Returns an immutable outcome."""
    # Bucket A: pre-start_run errors. No connector_runs row is created, so
    # these surface to the caller and are recorded only at the CLI/audit
    # layer (B2.6, T37). The orchestrator never half-creates a run.
    credential = _load_credential(
        session,
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
    )
    if credential is None:
        raise CredentialNotFoundError(
            connector_key=connector_key, account_id=account_id
        )
    if credential.status != "active":
        raise InactiveCredentialError(
            credential_id=str(credential.id), status=credential.status
        )

    payload = resolve_secret(credential.encrypted_secret_ref)
    credentials = build_credentials_from_payload(payload)
    refresh_credentials(credentials)

    if dry_run:
        # T29 owns the dry-run path: list jobs / list reports but skip
        # download / blob / raw_file / parse / upsert / lifecycle. Keep the
        # placeholder so an early caller can't accidentally drive a real run
        # against production via dry_run=True.
        raise NotImplementedError("dry_run handled in task 29")

    # Bucket B/C scope starts here: every failure past start_run goes through
    # finish_run with a terminal status so the run row reflects reality.
    run_entry = start_run(
        session,
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
        report_month=report_month,
        triggered_by_user_id=triggered_by_user_id,
    )
    # ``session.commit()`` here is intentional: if the process dies during
    # the per-report loop the connector_runs row stays RUNNING with a real
    # ``started_at`` instead of being rolled back to nothing. T28 will add
    # a sweeper for orphaned RUNNING rows.
    session.commit()

    runner = dispatch_connector(key=connector_key)
    counts = _zero_counts()
    per_report_failures: list[tuple[str, str]] = []
    backend = _build_blob_backend()
    bucket = _bucket_for_backend()
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    parser = _parser_for_connector(connector_key)
    ordering_index = 0

    try:
        for report_type, parser_payload, raw_bytes in runner.produce_reports(
            session=session,
            run=run_entry,
            credentials=credentials,
            report_month=report_month,
            account_id=account_id,
        ):
            counts["reports_attempted"] += 1
            try:
                _process_one_report(
                    session=session,
                    tenant_id=tenant_id,
                    connector_key=connector_key,
                    run_entry=run_entry,
                    backend=backend,
                    bucket=bucket,
                    parser=parser,
                    repo=repo,
                    report_type=report_type,
                    report_month=report_month,
                    parser_payload=parser_payload,
                    raw_bytes=raw_bytes,
                    ordering_index=ordering_index,
                    triggered_by_user_id=triggered_by_user_id,
                    counts=counts,
                )
            except GoogleConnectorError as exc:
                # Per-report containment: a single bad report -> PARTIAL run,
                # not a terminal FAILED that loses the other reports' rows.
                # Full bucket-B handler depth (mark_failed wiring, error
                # summary aggregation) is T28; happy-path tests don't reach
                # this branch.
                per_report_failures.append((report_type, type(exc).__name__))
                counts["reports_failed"] += 1
            ordering_index += 1
    except GoogleConnectorError as exc:
        # Bucket C: a non-per-report failure (runner.produce_reports itself
        # blew up before yielding, an unexpected typed error escaped the
        # inner block, etc.) terminates the run as FAILED. Re-raise so the
        # caller (CLI) sees the typed error too.
        finish_run(
            session,
            tenant_id=tenant_id,
            connector_run_id=UUID(run_entry.id),
            status="FAILED",
            counts=counts,
            error_summary=f"{type(exc).__name__}: {exc!s}",
        )
        session.commit()
        raise

    # Bucket B aggregate finish. Status reflects per-report outcomes:
    # - all OK and at least one succeeded   -> SUCCEEDED
    # - none succeeded                      -> FAILED
    # - mixed                                -> PARTIAL
    status = _derive_terminal_status(counts)
    finished = finish_run(
        session,
        tenant_id=tenant_id,
        connector_run_id=UUID(run_entry.id),
        status=status,
        counts=counts,
        error_summary=_summarize_failures(per_report_failures),
    )
    session.commit()
    return ConnectorRunOutcome(
        run=finished, counts=counts, per_report_failures=per_report_failures
    )


# ----------------------------------------------------------------------------
# Per-report inner block
# ----------------------------------------------------------------------------


def _process_one_report(
    *,
    session: Session,
    tenant_id: UUID,
    connector_key: str,
    run_entry: ConnectorRunEntry,
    backend: BlobStorageBackend,
    bucket: str,
    parser: YouTubeReportingParser,
    repo: SqlAlchemyGoogleRevenueSourceRowRepository,
    report_type: str,
    report_month: str,
    parser_payload: dict[str, object],
    raw_bytes: bytes,
    ordering_index: int,
    triggered_by_user_id: UUID | None,
    counts: dict[str, int],
) -> None:
    """Run one report through blob → raw_file → parse → upsert → mark_parsed.

    Raises any ``GoogleConnectorError`` from the blob / lifecycle / parser /
    repo so the outer per-report ``except`` in ``run_one`` can record the
    failure without aborting the whole run.
    """
    # 1. Checksum + deterministic URI: same bytes always map to the same path
    # so a retry overwrites or hits the existing object instead of creating a
    # second copy with the same content.
    checksum = compute_checksum(raw_bytes)
    storage_uri = deterministic_blob_path(
        bucket=bucket,
        tenant_id=tenant_id,
        connector_key=connector_key,
        report_type=report_type,
        month=report_month,
        checksum=checksum,
        ext=_extension_for_connector(connector_key),
    )

    # 2. Upload then re-read+hash to guarantee the blob round-trips.
    # ``upload_and_verify`` raises ``BlobChecksumMismatchError`` (typed) if a
    # backend silently truncates the payload.
    upload_and_verify(backend=backend, storage_uri=storage_uri, content=raw_bytes)

    # 3. Insert the raw_report_files row in DOWNLOADED. ``source`` mirrors the
    # B1 parser convention (youtube_reporting / youtube_analytics /
    # adsense_management); flush populates the id without committing so the
    # link join can use it within the same transaction.
    raw_file = RawReportFileORM(
        id=uuid4(),
        tenant_id=tenant_id,
        source=_source_system_for_connector(connector_key),
        report_type=report_type,
        report_month=report_month,
        file_url=storage_uri,
        checksum=checksum,
        parse_status="DOWNLOADED",
        downloaded_by=triggered_by_user_id,
    )
    session.add(raw_file)
    session.flush()
    raw_file_id = raw_file.id

    # 4. Join the raw file to the run with a deterministic ordering_index so
    # later reads (e.g. operator console) can replay the run in order.
    link_raw_file(
        session,
        tenant_id=tenant_id,
        connector_run_id=UUID(run_entry.id),
        raw_report_file_id=raw_file_id,
        ordering_index=ordering_index,
    )

    # 5. Parse. ``list(...)`` forces the generator so a parser failure surfaces
    # here (typed ``ParserError``) instead of mid-upsert. ParserError is *not*
    # a GoogleConnectorError, so it propagates out and the outer except in
    # ``run_one`` (which catches GoogleConnectorError only) wouldn't trap it
    # — T28 will add an explicit translator. For the happy-path test the
    # parser succeeds.
    parsed_rows = list(parser.parse(parser_payload, tenant_id=tenant_id))

    # 6. Upsert. Returns the persisted entries (one per ParsedSourceRow);
    # the precise created-vs-updated split needs a pre-read of existing
    # source_row_keys, which the happy-path test doesn't exercise. T28+
    # can wire that count if downstream consumers need it; the run-level
    # ``rows_upserted_total`` (sum of writes across all reports) is what
    # the test asserts on.
    written = repo.upsert_many(
        tenant_id,
        parsed_rows,
        raw_file_id=raw_file_id,
        imported_by=triggered_by_user_id,
    )
    counts["rows_upserted_total"] += len(written)
    counts["rows_upserted_created"] += len(written)

    # 7. Lifecycle transition: DOWNLOADED -> PARSED. Raises
    # ``RawFileAlreadyParsedError`` if called twice on the same file, which
    # would only happen on a re-entrant orchestrator bug.
    mark_parsed(session, raw_file_id=raw_file_id, tenant_id=tenant_id)
    counts["reports_succeeded"] += 1


# ----------------------------------------------------------------------------
# Connector runner: YouTube Reporting
# ----------------------------------------------------------------------------


class YouTubeReportingRunner:
    """B2.4 adapter that walks YouTube Reporting jobs/reports for one month.

    Each yielded tuple is ``(report_type_id, parser_payload, raw_bytes)``:
    - ``report_type_id`` is from the job descriptor and is already whitelist-
      filtered by ``list_supported_jobs``.
    - ``parser_payload`` is the parser-friendly dict the existing
      ``YouTubeReportingParser`` expects (``report_metadata`` + ``rows``);
      this runner converts the downloaded CSV bytes to that shape.
    - ``raw_bytes`` is the unmodified CSV body that goes to blob storage so
      the on-disk evidence matches what Google returned.

    The class references ``YouTubeReportingClient`` by bare name so tests
    that patch
    ``ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient``
    replace what the runner actually uses.
    """

    def produce_reports(
        self,
        *,
        session: Session,
        run: ConnectorRunEntry,
        credentials: Credentials,
        report_month: str,
        account_id: str,
    ) -> Iterator[tuple[str, dict[str, object], bytes]]:
        http = GoogleHttpClient(credentials=credentials)
        try:
            client = YouTubeReportingClient(http=http)
            jobs = client.list_supported_jobs(account_id=account_id)
            for job in jobs:
                report_type = _require_text(job, "reportTypeId")
                job_id = _require_text(job, "id")
                reports = client.list_reports_for_month(
                    account_id=account_id,
                    job_id=job_id,
                    report_month=report_month,
                )
                for report in reports:
                    download_url = _require_text(report, "downloadUrl")
                    raw_bytes = client.fetch_report(download_url=download_url)
                    report_id = _require_text(report, "id")
                    parser_payload = _csv_to_parser_payload(
                        raw_bytes=raw_bytes,
                        report_id=report_id,
                        report_type=report_type,
                        report_month=report_month,
                    )
                    yield report_type, parser_payload, raw_bytes
        finally:
            http.close()


def _require_text(mapping: dict[str, object], field: str) -> str:
    """Pull a non-blank string field from a Google API descriptor or fail.

    Google's REST envelopes are well-typed in practice, but a missing /
    blank / non-string value here would translate downstream to a confusing
    ``ParserError`` or path-builder ``ValueError``. Surface it as a typed
    ``GoogleConnectorError`` so the outer per-report ``except`` records
    ``GoogleApiResponseError`` against the right report.
    """
    from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError

    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise GoogleApiResponseError(
            url="<descriptor>", reason=f"missing or non-string {field!r}"
        )
    return value.strip()


def _csv_to_parser_payload(
    *,
    raw_bytes: bytes,
    report_id: str,
    report_type: str,
    report_month: str,
) -> dict[str, object]:
    """Convert YouTube Reporting CSV bytes to the parser-friendly dict shape.

    The YouTube Reporting CSV header maps to the parser's dimensions/metrics
    dicts. This adapter is intentionally permissive about column naming so
    it accepts both Google's documented column names (``date``, ``channel``,
    ``content_owner``, ``estimated_partner_revenue``, ``currency_code``) and
    the convenience short names test fixtures use (``day``, ``channel_id``,
    ``ad_revenue``). Unknown extra columns flow into the row's
    ``dimensions`` so the parser's dedup key sees them.
    """
    import csv
    import io

    text = raw_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, object]] = []
    for line_index, csv_row in enumerate(reader):
        # Normalize the date column. YouTube Reporting CSV uses ``date`` or
        # ``day``; either is interpreted as the row's single calendar day
        # (period_start == period_end) because the report rows are daily.
        # The parser's date_range bucketing requires the row to fall within
        # one calendar month, which a single day always does.
        date_value = _first_present(csv_row, "date", "day")
        if not date_value:
            raise _parser_payload_error(
                report_id=report_id,
                reason="csv row missing date/day column",
            )
        try:
            row_date = date_cls.fromisoformat(date_value)
        except ValueError as exc:
            raise _parser_payload_error(
                report_id=report_id,
                reason=f"csv row date {date_value!r} not ISO YYYY-MM-DD",
            ) from exc

        # Channel + optional content_owner. Build the dimensions dict from
        # whatever the CSV provides; the parser only requires ``channel``
        # to be a non-blank string and treats ``content_owner`` as optional.
        channel = _first_present(csv_row, "channel", "channel_id")
        if not channel:
            raise _parser_payload_error(
                report_id=report_id,
                reason="csv row missing channel/channel_id column",
            )
        dimensions: dict[str, object] = {"channel": channel}
        content_owner = _first_present(csv_row, "content_owner")
        if content_owner:
            dimensions["content_owner"] = content_owner
        # Forward any other columns as opaque dimensions so the parser's
        # source_row_key dedup picks them up. Excludes the date/metric
        # columns to avoid duplicating them under dimensions.
        for key, value in csv_row.items():
            if key is None:
                # csv.DictReader puts trailing un-named columns under None.
                # Discard them; the parser doesn't accept None-keyed dicts.
                continue
            if key.lower() in _RESERVED_CSV_COLUMNS:
                continue
            if value is None or not isinstance(value, str):
                continue
            stripped = value.strip()
            if stripped:
                dimensions[key] = stripped

        # Pull the estimated revenue amount. Accept either Google's
        # documented ``estimated_partner_revenue`` /
        # ``estimatedRevenue`` columns or the test-fixture shorthand
        # ``ad_revenue``. The parser requires a *string* for Decimal
        # precision (Decimal("1.23")), so pass through as text.
        amount = _first_present(
            csv_row,
            "estimated_partner_revenue",
            "estimatedRevenue",
            "estimatedrevenue",
            "ad_revenue",
        )
        if amount is None:
            raise _parser_payload_error(
                report_id=report_id,
                reason="csv row missing revenue column "
                "(expected one of: estimated_partner_revenue, "
                "estimatedRevenue, ad_revenue)",
            )

        # Currency defaults to USD when the CSV omits it. YouTube Reporting
        # CSVs from a content-owner-scoped job typically include a column;
        # daily channel CSVs sometimes don't, and the spec settles such
        # rows in the partner's settlement currency, which is USD for the
        # public sample. The parser rejects a blank string, so a default
        # is required.
        currency = _first_present(csv_row, "currency_code", "currencyCode")
        if not currency:
            currency = "USD"

        rows.append(
            {
                "line_index": line_index,
                "date_range": {
                    "start": row_date.isoformat(),
                    "end": row_date.isoformat(),
                },
                "dimensions": dimensions,
                "metrics": {
                    "estimatedRevenue": amount,
                    "currencyCode": currency,
                },
            }
        )

    return {
        "report_metadata": {
            "report_id": report_id,
            "report_type": report_type,
        },
        "rows": rows,
    }


def _first_present(row: dict[str, str | None], *keys: str) -> str | None:
    """Return the first non-blank string value among the given column keys.

    csv.DictReader keys preserve the header's case, so this helper checks
    each candidate case-insensitively against the row's actual keys.
    """
    lower_map = {k.lower(): k for k in row if isinstance(k, str)}
    for key in keys:
        actual = lower_map.get(key.lower())
        if actual is None:
            continue
        value = row.get(actual)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parser_payload_error(*, report_id: str, reason: str) -> GoogleConnectorError:
    """Wrap CSV adapter failures in a typed connector error.

    Reuses ``GoogleApiResponseError`` because a malformed CSV from Google is,
    practically, an upstream response-shape problem: the orchestrator's
    per-report ``except`` already routes ``GoogleConnectorError`` into the
    failure list, so a CSV-shape failure ends up classed the same as a JSON
    list_reports failure.
    """
    from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError

    return GoogleApiResponseError(url=f"report:{report_id}", reason=reason)


# ----------------------------------------------------------------------------
# Private helpers
# ----------------------------------------------------------------------------


def _load_credential(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
) -> ApiConnectorCredentialORM | None:
    """Tenant-scoped credential row lookup for ``run_one``.

    Kept private to the orchestrator because the existing repository
    (``SqlAlchemyConnectorCredentialRepository``) does not expose a
    by-(connector_key, account_id) lookup — it serves the admin paging API
    and the create flow. Adding a new public method to that repo would
    drag in actor/audit semantics this internal load doesn't need.
    """
    return session.scalar(
        sa.select(ApiConnectorCredentialORM).where(
            ApiConnectorCredentialORM.tenant_id == tenant_id,
            ApiConnectorCredentialORM.connector_key == connector_key,
            ApiConnectorCredentialORM.account_id == account_id,
        )
    )


def _zero_counts() -> dict[str, int]:
    """Mirror the B2.3 ``CONNECTOR_RUN_COUNT_KEYS`` shape.

    ``finish_run`` validates the exact key set on commit, so any drift
    between this and ``CONNECTOR_RUN_COUNT_KEYS`` raises
    ``ConnectorRunValidationError`` at finish_run time.
    """
    return {key: 0 for key in CONNECTOR_RUN_COUNT_KEYS}


def _derive_terminal_status(counts: dict[str, int]) -> str:
    """Pick the connector_runs terminal status from per-report counters.

    ``finish_run`` accepts only ``{SUCCEEDED, PARTIAL, FAILED}``. ``RUNNING``
    is not a terminal status and would be rejected with
    ``ConnectorRunValidationError``.
    """
    if counts["reports_attempted"] == 0:
        # No reports yielded at all: nothing to attribute revenue to.
        # ``finish_run`` accepts only terminal statuses; record FAILED so the
        # operator console flags the run instead of leaving it RUNNING.
        return "FAILED"
    if counts["reports_failed"] == 0:
        return "SUCCEEDED"
    if counts["reports_succeeded"] == 0:
        return "FAILED"
    return "PARTIAL"


def _summarize_failures(failures: list[tuple[str, str]]) -> str | None:
    """Pack per-report failures into an operator-safe summary string.

    Bounded by ``finish_run``'s 500-char truncation so the connector_runs
    column never overflows; this helper just builds the candidate text.
    """
    if not failures:
        return None
    items = ", ".join(f"{report_type}:{error_class}" for report_type, error_class in failures)
    return f"{len(failures)} report(s) failed: {items}"


def _build_blob_backend() -> BlobStorageBackend:
    """Construct the blob backend selected by ``UMS_BLOB_BACKEND``.

    Defaults to ``file-store`` (LocalFileStoreBackend) so tests and local
    runs don't accidentally try to reach GCS. ``LocalFileStoreBackend`` and
    ``GcsBlobStorageBackend`` are imported at module scope so test patches
    on ``ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend``
    replace what this helper actually instantiates.
    """
    backend_name = os.getenv("UMS_BLOB_BACKEND", "file-store")
    if backend_name == "file-store":
        root = Path(os.getenv("UMS_LOCAL_STORE_ROOT", str(Path.cwd() / "_local_blob_store")))
        return LocalFileStoreBackend(root=root)
    if backend_name == "gcs":
        # Lazy import the GCS client only on the gcs branch so SQLite test
        # runs don't pay the cost of constructing one.
        from google.cloud.storage import Client as GcsClient  # type: ignore[import-untyped]

        return GcsBlobStorageBackend(client=GcsClient())
    raise ValueError(
        f"unknown UMS_BLOB_BACKEND={backend_name!r} (expected 'file-store' or 'gcs')"
    )


def _bucket_for_backend() -> str:
    """Return the bucket/namespace token for the deterministic blob URI.

    ``deterministic_blob_path`` emits ``gs://{bucket}/...`` regardless of
    which backend is in use (the helper is shape-only, not transport-aware).
    The bucket token then namespaces the path; for the GCS backend it must
    match a real bucket, for the file-store backend it's a logical prefix.
    Bridging file-store + ``gs://`` URIs is a known gap the spec author
    expects a later slice to address; T27 keeps the URI shape consistent
    with what ``deterministic_blob_path`` produces today.
    """
    return os.getenv("UMS_GCS_BUCKET", "ums-smart-revenue-raw")


def _source_system_for_connector(connector_key: str) -> str:
    """Map the connector_key to the ALLOWED_SOURCE_SYSTEMS member.

    Keeps the orchestrator decoupled from B1's source_system string set
    while still letting the raw_report_files row carry the value the
    existing PR #43 repository expects.
    """
    mapping = {
        "youtube-reporting": "youtube_reporting",
        "youtube-analytics": "youtube_analytics",
        "adsense-management": "adsense_management",
    }
    try:
        return mapping[connector_key]
    except KeyError as exc:
        raise ValueError(
            f"unknown connector_key for source_system mapping: {connector_key!r}"
        ) from exc


def _extension_for_connector(connector_key: str) -> str:
    """File extension used inside the deterministic blob URI.

    YouTube Reporting downloads are CSV; YouTube Analytics (B2.5) and
    AdSense (B2.6) return JSON. Centralised here so the path-builder stays
    consistent across slices.
    """
    if connector_key == "youtube-reporting":
        return "csv"
    return "json"


def _parser_for_connector(connector_key: str) -> YouTubeReportingParser:
    """Return the source-row parser bound to a given connector key.

    B2.5/B2.6 will widen this beyond YouTubeReportingParser; the helper
    isolates that future change from ``run_one`` itself. For now any non-
    YouTube-Reporting key would not reach this point because the runner
    isn't registered.
    """
    if connector_key == "youtube-reporting":
        return YouTubeReportingParser()
    raise ValueError(f"no parser bound for connector_key: {connector_key!r}")


# ----------------------------------------------------------------------------
# Module-load registration
# ----------------------------------------------------------------------------

# An instance (not the class) is registered: B2.5/B2.6 runners may carry
# per-connector config in their constructors, and the dispatch contract
# returns the registered value directly to the orchestrator.
register_connector(key="youtube-reporting", runner=YouTubeReportingRunner())
