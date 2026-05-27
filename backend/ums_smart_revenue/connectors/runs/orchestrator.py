"""B2.4 orchestrator: the public ``run_one(...)`` surface.

Happy path (this task, T27):
  1. ``_load_credential(session, tenant_id, connector_key, account_id)`` returns
     the ``ApiConnectorCredentialORM`` row or ``None``.
  2. ``resolve_secret(credential.encrypted_secret_ref)`` returns the payload
     string registered behind the URI scheme.
  3. ``build_credentials_from_payload(payload)`` returns a google-auth
     ``Credentials``.
  4. ``refresh_credentials(...)`` performs the initial token refresh; any
     ``OAuthRefreshError`` bubbles pre-``start_run`` so no connector_runs
     row is created.
  5. ``start_run(...)`` commits the ``RUNNING`` row (forensic durability for
     ``started_at`` even if the process dies mid-loop).
  6. ``dispatch_connector(key=connector_key)`` returns a ``ConnectorRunner``
     instance whose ``produce_reports`` yields downloaded report tuples or
     ``ProducedReportFailure`` entries for per-report Google/API failures.
  7. For each downloaded tuple:
        a. ``compute_checksum`` + ``deterministic_blob_path`` build the URI.
        b. ``upload_and_verify`` writes the blob and re-reads its SHA-256.
        c. A ``RawReportFileORM`` row is inserted with ``parse_status``
           ``DOWNLOADED``; ``session.flush()`` populates ``raw_file_id``.
        d. ``link_raw_file`` joins the raw file to the run with a per-run
           ``ordering_index``.
        e. The parser's ``parse(...)`` is consumed into a ``list`` so an
           early ``ParserError`` does not surface mid-upsert.
        f. Inside a savepoint, ``SqlAlchemyGoogleRevenueSourceRowRepository``
           upserts rows and ``mark_parsed`` transitions the raw file
           ``DOWNLOADED -> PARSED``.
        g. The outer commit succeeds before success and row counts advance.
  8. ``finish_run`` records the terminal status (SUCCEEDED / PARTIAL / FAILED)
     plus the accumulated counts and (optionally) an error summary, and
     ``session.commit()`` persists it.
  9. Returns an immutable ``ConnectorRunOutcome``.

Dry-run (T29): skips ``start_run`` entirely -- writes NOTHING to the
database (no connector_runs row, no raw_file, no upsert, no audit) and no
blob upload. Exercises the runner's ``produce_reports`` for API/CSV
shape validation and runs the parser for row counts, then returns
``ConnectorRunOutcome(run=None, counts=...)`` so an operator can sanity
check an account+month before scheduling the live run. A SAVEPOINT-rollback
guards against any future runner that accidentally writes to the session.
Bucket B/C handlers and per-report commit are wired by T28. Audit wiring
lands in B2.6 (T37). Existing files under
``connectors/google/`` (errors, http client, registry, secret resolvers,
oauth, blob storage, raw-file helpers, the YT client) and the existing
parser/repo (PR #43) are *not* touched here — T27 is additive.
"""
from __future__ import annotations

import os
from calendar import monthrange
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import (
    BlobStorageConfigurationError,
    CredentialNotFoundError,
    GoogleConnectorError,
    InactiveCredentialError,
    OAuthRefreshError,
    RawFileLifecycleError,
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
from ums_smart_revenue.connectors.google.secret_resolver import (
    ensure_default_resolvers,
    resolve_secret,
)
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
from ums_smart_revenue.connectors.runs.raw_file_helpers import (
    mark_failed,
    mark_parsed,
)
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

# CSV adapter column aliases. YouTube Reporting estimated-revenue reports are
# daily and may include extra breakdown dimensions / metric columns (video,
# country, ad_impressions, CPM fields, etc.). The C1 normalizer consumes
# monthly channel totals, so the adapter consumes only the identity + money
# columns it needs and aggregates every breakdown row into that monthly shape.
_CSV_DATE_COLUMNS = ("date", "day")
_CSV_CHANNEL_COLUMNS = ("channel", "channel_id")
_CSV_REVENUE_COLUMNS = (
    "estimated_partner_revenue",
    "estimatedRevenue",
    "estimatedrevenue",
    "ad_revenue",
)
_CSV_CURRENCY_COLUMNS = ("currency_code", "currencyCode")


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


@dataclass(frozen=True)
class ProducedReportFailure:
    """One report-type-scoped failure from inside a connector runner.

    The YouTube Reporting runner downloads and CSV-normalizes reports before
    it can yield the normal tuple consumed by ``run_one``. If those pre-yield
    per-report steps fail, this sentinel lets the orchestrator count the
    failure in Bucket B instead of letting the generator exception escape to
    the run-level Bucket C handler.
    """

    report_type: str
    error: Exception


ProducedReport = tuple[str, dict[str, object], bytes] | ProducedReportFailure


class ConnectorRunner(Protocol):
    """Per-connector adapter contract.

    Each runner owns the API-client/credential bridge for its source system
    (B2.4 wires YouTube Reporting; B2.5/B2.6 will register YouTube Analytics
    and AdSense Management). ``produce_reports`` yields one tuple per Google
    report or a ``ProducedReportFailure`` for a report-type-scoped pre-yield
    failure. Blob storage, raw-file lifecycle, upserts, and the connector_runs
    lifecycle stay owned by the orchestrator uniformly across all connectors.
    """

    def produce_reports(
        self,
        *,
        session: Session,
        run: ConnectorRunEntry | None,
        credentials: Credentials,
        report_month: str,
        account_id: str,
    ) -> Iterator[ProducedReport]:
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
#            Transactional model -- explicit session.commit() points:
#              - after start_run: forensic durability for the started_at
#                marker even if the process dies mid-loop;
#              - after each successful _process_one_report: per-report
#                durability so a later report's pre-flush failure can't
#                wipe earlier successes via the no-raw-file rollback (M6);
#              - after mark_failed in bucket B: FAILED state persistence
#                per raw_file;
#              - after finish_run (terminal status): durability of the
#                final run state, whether SUCCEEDED, PARTIAL, FAILED
#                (typed bucket-C), or FAILED (fail-safe rescue).
#            The inner per-report block catches Exception (T28 widened
#            from GoogleConnectorError) so a single bad report -- typed
#            OR untyped (e.g. ParserError) -- doesn't abort the whole
#            run; the outer try/except (also widened) routes generator-
#            level failures into a FAILED finish_run via bucket C.
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

    ensure_default_resolvers()
    payload = resolve_secret(credential.encrypted_secret_ref)
    credentials = build_credentials_from_payload(payload)
    refresh_credentials(credentials)

    if dry_run:
        # ====================================================================
        # Purpose: Spec §5.4 dry-run path -- list jobs / list reports / fetch
        #          report bytes / parse for row counts, but write NOTHING to
        #          the database (no connector_runs row, no raw_file row, no
        #          source-row upsert, no audit) and perform NO blob upload.
        #          Returns counts that *would* have been written so an
        #          operator can sanity-check an account+month before
        #          scheduling the live run.
        # Database/ORM: Read-only by contract. The SAVEPOINT below is a
        #               belt-and-suspenders rollback so any unflushed writes
        #               a future ConnectorRunner accidentally makes inside
        #               its ``produce_reports`` (the current YT runner does
        #               not) are reverted before the function returns.
        # Standards: Bucket A still gates dry-run -- a missing or inactive
        #            credential raises BEFORE this branch, identical to the
        #            live path. The inner per-report try/except mirrors the
        #            live Bucket B handler so one bad report doesn't abort
        #            the dry-run -- the operator sees an honest count of
        #            attempted vs succeeded vs failed reports.
        # Blast Radius: None. By design, the dry-run path cannot create a
        #               connector_runs row, attach a raw_report_file, or
        #               upsert a source row. The SAVEPOINT rollback
        #               guarantees this even if a future runner regresses.
        # Connections:
        #   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
        #     §5.4 -> dry-run contract (counts only, no writes).
        # ====================================================================
        counts = _zero_counts()
        runner = dispatch_connector(key=connector_key)
        parser = _parser_for_connector(connector_key)
        # SAVEPOINT defence-in-depth: any writes the runner might make
        # (the current YT runner does not, but a future B2.5/B2.6 runner
        # might accidentally) are scoped to this nested transaction and
        # rolled back in the ``finally`` so the dry-run leaves zero rows
        # behind. The ``try/finally`` ensures the rollback runs even if
        # the for-loop itself raises (e.g. ``list_supported_jobs`` raises
        # a transport error before the first yield).
        savepoint = session.begin_nested()
        try:
            for produced in runner.produce_reports(
                session=session,
                run=None,
                credentials=credentials,
                report_month=report_month,
                account_id=account_id,
            ):
                counts["reports_attempted"] += 1
                # Per-report containment mirrors the live Bucket B handler:
                # ParserError (untyped, subclass of ValueError) or any
                # GoogleConnectorError on a single report increments
                # reports_failed and continues so the dry-run still
                # produces useful counts for the remaining reports.
                try:
                    if isinstance(produced, ProducedReportFailure):
                        raise produced.error
                    _report_type, parser_payload, _raw_bytes = produced
                    rows = list(parser.parse(parser_payload, tenant_id=tenant_id))
                    counts["reports_succeeded"] += 1
                    counts["rows_upserted_total"] += len(rows)
                    # rows_upserted_{created,updated,unchanged} stay 0 in
                    # dry-run because no upsert is performed -- the
                    # category split would require a pre-read of
                    # existing source_row_keys, which is itself a write
                    # path we are deliberately not entering here.
                except Exception:
                    counts["reports_failed"] += 1
        finally:
            # Revert any unflushed writes from the runner / parser / future
            # regression. ``ConnectorRunOutcome(run=None, ...)`` is the
            # spec-required dry-run shape; the empty per_report_failures
            # list keeps the outcome dataclass total -- per-report failure
            # detail is not currently surfaced for dry-run since the
            # operator's primary signal is the counts dict.
            savepoint.rollback()
        return ConnectorRunOutcome(
            run=None, counts=counts, per_report_failures=[]
        )

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
    # ``started_at`` instead of being rolled back to nothing. Future work
    # (no current task): a sweeper for orphaned RUNNING rows from crashed
    # processes.
    session.commit()

    counts = _zero_counts()
    per_report_failures: list[tuple[str, str]] = []
    per_report_failure_details: list[tuple[str, str, str | None]] = []
    ordering_index = 0
    # Sentinel flipped to True ONLY after a finish_run + session.commit()
    # succeeds (bucket-B or bucket-C path). If both paths are short-circuited
    # by an untyped exception (e.g. ParserError, RuntimeError) the outer
    # ``finally`` below sweeps the connector_runs row from RUNNING to FAILED
    # so the operator console never sees a row stuck in RUNNING forever.
    finished = False

    try:
        try:
            runner = dispatch_connector(key=connector_key)
            backend, scheme, bucket = _build_blob_backend()
            repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
            parser = _parser_for_connector(connector_key)
            for produced in runner.produce_reports(
                session=session,
                run=run_entry,
                credentials=credentials,
                report_month=report_month,
                account_id=account_id,
            ):
                if isinstance(produced, ProducedReportFailure):
                    report_type = produced.report_type
                    parser_payload: dict[str, object] | None = None
                    raw_bytes: bytes | None = None
                    produced_error: Exception | None = produced.error
                else:
                    report_type, parser_payload, raw_bytes = produced
                    produced_error = None
                counts["reports_attempted"] += 1
                # ``report_state`` is the per-report mutable handshake with
                # ``_process_one_report``: after the raw_file row is flushed
                # (step c) ``_process_one_report`` writes the id here so this
                # except clause can mark it FAILED if a later step raises.
                # Fresh dict per iteration -- a previous report's id must
                # never leak into the next report's bucket B handler.
                report_state: dict[str, object] = {}
                try:
                    if produced_error is not None:
                        raise produced_error
                    if parser_payload is None or raw_bytes is None:
                        raise RuntimeError("connector runner yielded incomplete report")
                    rows_upserted = _process_one_report(
                        session=session,
                        tenant_id=tenant_id,
                        connector_key=connector_key,
                        run_entry=run_entry,
                        backend=backend,
                        scheme=scheme,
                        bucket=bucket,
                        parser=parser,
                        repo=repo,
                        report_type=report_type,
                        report_month=report_month,
                        parser_payload=parser_payload,
                        raw_bytes=raw_bytes,
                        ordering_index=ordering_index,
                        triggered_by_user_id=triggered_by_user_id,
                        report_state=report_state,
                    )
                    # M6 fix: commit each successful report BEFORE the next
                    # iteration can run a session.rollback(). The no-raw-file
                    # branch of bucket B (later in this except) calls
                    # session.rollback() to clear half-flushed state from a
                    # pre-flush failure; without this per-report commit, that
                    # rollback would also wipe prior reports' flushed-but-
                    # uncommitted writes (raw_file + link + source_rows +
                    # PARSED transition) and the run row would lie to
                    # operators with reports_succeeded > 0 but no on-disk
                    # evidence. Transactional model: each successful report
                    # is its own transaction; the terminal finish_run is its
                    # own separate commit at the end.
                    session.commit()
                    # Increment AFTER the commit so a commit failure
                    # propagates into the except handler and is recorded
                    # as a failure once, not double-counted as both
                    # succeeded and failed. ``_process_one_report`` does
                    # NOT increment ``reports_succeeded`` itself for the
                    # same reason -- this is the single source of truth.
                    counts["reports_succeeded"] += 1
                    counts["rows_upserted_total"] += rows_upserted
                except Exception as exc:
                    # Bucket B: per-report containment. Widened from
                    # ``GoogleConnectorError`` (T27) to ``Exception`` (T28) so
                    # the inner try/except also catches non-typed failures
                    # like ParserError (subclass of ValueError, not
                    # GoogleConnectorError). A single bad report -> PARTIAL
                    # run, not a terminal FAILED that loses the other
                    # reports' rows.
                    error_class = type(exc).__name__
                    per_report_failures.append((report_type, error_class))
                    per_report_failure_details.append(
                        (report_type, error_class, _safe_failure_detail(exc))
                    )
                    counts["reports_failed"] += 1
                    # If the failure happened AFTER the raw_file row was
                    # flushed (parse / upsert / mark_parsed), mark that
                    # row DOWNLOADED -> FAILED so it doesn't sit
                    # DOWNLOADED forever. Failures BEFORE the flush
                    # (checksum / blob upload) leave the key absent and
                    # there's nothing to mark.
                    in_flight_raw_file_id = report_state.get("raw_file_id")
                    if in_flight_raw_file_id is not None:
                        try:
                            # mark_failed only accepts DOWNLOADED|FAILED ->
                            # FAILED. If by some race the row was already
                            # marked PARSED before the exception fired,
                            # mark_failed would raise RawFileLifecycleError.
                            # In practice mark_parsed is the LAST step of
                            # _process_one_report, so a PARSED-then-fail
                            # race is not reachable on the YT path -- the
                            # try/except here is belt-and-suspenders.
                            mark_failed(
                                session,
                                raw_file_id=in_flight_raw_file_id,
                                tenant_id=tenant_id,
                            )
                            session.commit()
                        except Exception:
                            # Cleanup must not mask the per-report failure
                            # we just appended to per_report_failures. If
                            # mark_failed itself raises (DB disconnect, race
                            # with QUARANTINED), roll the session back to a
                            # clean state and continue: the finish_run sweep
                            # at the bottom of the loop will still record
                            # the run-level error_summary correctly.
                            session.rollback()
                    else:
                        # No raw_file in flight: roll back any
                        # half-flushed state from the early-failure path
                        # (checksum/upload exceptions) so the next
                        # iteration starts clean.
                        session.rollback()
                ordering_index += 1
        except Exception as exc:
            # Bucket C: any escaping exception (typed GoogleConnectorError
            # *or* untyped like a runtime error from runner.produce_reports
            # itself) terminates the run as FAILED. Widened from
            # ``GoogleConnectorError`` (T27) to ``Exception`` (T28) so
            # ParserError and other non-typed errors that escape the
            # generator (e.g. from list_supported_jobs) get a proper FAILED
            # row written instead of relying on the fail-safe finally.
            # The inner Bucket B handler is also widened to Exception, so
            # "in-flight raw_file" handling lives there; Bucket C primarily
            # catches generator-level failures where no raw_file exists.
            # Roll back any partial unflushed state so finish_run runs
            # against a clean session.
            session.rollback()
            finished_run = finish_run(
                session,
                tenant_id=tenant_id,
                connector_run_id=UUID(run_entry.id),
                status="FAILED",
                counts=counts,
                error_summary=f"{type(exc).__name__}: {exc!s}",
            )
            session.commit()
            finished = True
            return ConnectorRunOutcome(
                run=finished_run,
                counts=counts,
                per_report_failures=per_report_failures,
            )

        # Bucket B aggregate finish. Status reflects per-report outcomes:
        # - all OK and at least one succeeded   -> SUCCEEDED
        # - none succeeded                      -> FAILED
        # - mixed                                -> PARTIAL
        status = _derive_terminal_status(counts)
        finished_run = finish_run(
            session,
            tenant_id=tenant_id,
            connector_run_id=UUID(run_entry.id),
            status=status,
            counts=counts,
            error_summary=_summarize_failures(per_report_failure_details),
        )
        session.commit()
        finished = True
        return ConnectorRunOutcome(
            run=finished_run, counts=counts, per_report_failures=per_report_failures
        )
    finally:
        if not finished:
            # An untyped error (e.g. ParserError -- subclass of ValueError,
            # not GoogleConnectorError -- or a generic RuntimeError) escaped
            # both the inner per-report ``except`` and the bucket-C ``except``.
            # Without this sweep the connector_runs row would sit in RUNNING
            # forever, which is strictly worse than FAILED for operator
            # forensics. T28 widened bucket B/C to ``except Exception`` so
            # ParserError is caught typed-ly; this fail-safe defends the
            # residual case where ``finish_run`` itself fails (e.g. DB
            # disconnect during cleanup).
            #
            # rollback() first so any partial inner-loop state (e.g. a
            # ``RawReportFileORM`` that was added but not yet linked) is
            # cleared before finish_run runs against a clean session.
            session.rollback()
            try:
                finish_run(
                    session,
                    tenant_id=tenant_id,
                    connector_run_id=UUID(run_entry.id),
                    status="FAILED",
                    counts=counts,
                    error_summary="orchestrator aborted unexpectedly",
                )
                session.commit()
            except Exception:
                # Best-effort cleanup: if the rollback + finish_run path itself
                # fails (e.g. DB disconnect), swallow it so the original
                # exception still propagates out of the ``finally`` to the
                # caller. Masking the primary error here would hide the real
                # root cause from CLI and audit.
                session.rollback()


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
    scheme: str,
    bucket: str,
    parser: YouTubeReportingParser,
    repo: SqlAlchemyGoogleRevenueSourceRowRepository,
    report_type: str,
    report_month: str,
    parser_payload: dict[str, object],
    raw_bytes: bytes,
    ordering_index: int,
    triggered_by_user_id: UUID | None,
    report_state: dict[str, object],
) -> int:
    """Run one report through blob → raw_file → parse → upsert → mark_parsed.

    Raises any ``GoogleConnectorError`` / ``ParserError`` / other exception
    from the blob / lifecycle / parser / repo so the outer per-report
    ``except`` in ``run_one`` can record the failure without aborting the
    whole run.

    ``report_state`` is a mutable handshake dict the caller owns. As soon
    as the ``raw_report_files`` row is flushed (step 3), this function
    populates ``report_state["raw_file_id"]`` so the caller's bucket-B
    handler can mark the row DOWNLOADED -> FAILED if a later step raises.
    Failures BEFORE the flush (checksum / blob upload) leave the key
    absent, signalling "no raw_file in flight to mark FAILED".
    """
    # 1. Checksum + deterministic URI: same bytes always map to the same path
    # so a retry overwrites or hits the existing object instead of creating a
    # second copy with the same content.
    # FIX: thread ``scheme`` (resolved alongside the backend in
    # ``_build_blob_backend``) into ``deterministic_blob_path``. The previous
    # code emitted ``gs://...`` regardless of which backend was selected, so
    # the default ``LocalFileStoreBackend`` (file-store://) rejected every URI
    # at upload time with ``ValueError("LocalFileStoreBackend only handles
    # file-store:// URIs, got 'gs://...'")``. Real local-store ingestion was
    # broken; only tests that patched ``LocalFileStoreBackend`` with a
    # MagicMock masked it.
    checksum = compute_checksum(raw_bytes)
    storage_uri = deterministic_blob_path(
        scheme=scheme,
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

    # 3. Insert or reuse the raw_report_files row in DOWNLOADED. ``source``
    # mirrors the B1 parser convention (youtube_reporting /
    # youtube_analytics / adsense_management); flush populates the id
    # without committing so the link join can use it within the same
    # transaction. The lookup is load-bearing for retries: the table has a
    # unique key over source/report_type/month/checksum, so a re-run of the
    # same Google payload must reuse that evidence row instead of trying a
    # second insert.
    source_system = _source_system_for_connector(connector_key)
    raw_file = _get_or_create_raw_file(
        session,
        tenant_id=tenant_id,
        source=source_system,
        report_type=report_type,
        report_month=report_month,
        checksum=checksum,
        storage_uri=storage_uri,
        downloaded_by=triggered_by_user_id,
    )
    if raw_file.parse_status == "QUARANTINED":
        raise RawFileLifecycleError(
            raw_file_id=str(raw_file.id),
            current=raw_file.parse_status,
            target="PARSED",
        )
    raw_file_id = raw_file.id
    # Hand the new raw_file_id back to ``run_one``'s per-report try/except
    # via the mutable state dict. From this point on, any exception that
    # escapes ``_process_one_report`` leaves a DOWNLOADED row in the DB;
    # bucket B uses the id to flip it to FAILED instead of orphaning it.
    report_state["raw_file_id"] = raw_file_id

    # 4. Join the raw file to the run with a deterministic ordering_index so
    # later reads (e.g. operator console) can replay the run in order.
    link_raw_file(
        session,
        tenant_id=tenant_id,
        connector_run_id=UUID(run_entry.id),
        raw_report_file_id=raw_file_id,
        ordering_index=ordering_index,
    )

    if raw_file.parse_status == "PARSED":
        return 0

    # 5-7. Parse, upsert, and mark_parsed inside a SAVEPOINT. The raw_file
    # evidence row and run link are outside this savepoint so Bucket B can mark
    # the raw file FAILED if any downstream step raises. The finance source
    # rows and PARSED transition are inside it so a post-upsert lifecycle
    # failure cannot leave persisted source rows attached to a FAILED raw file.
    with session.begin_nested():
        # Parse. ``list(...)`` forces the generator so a parser failure surfaces
        # here (typed ``ParserError``) instead of mid-upsert. ParserError is
        # caught by the widened Bucket B/C ``except Exception`` in ``run_one``.
        parsed_rows = list(parser.parse(parser_payload, tenant_id=tenant_id))

        # Upsert. Returns the persisted entries (one per ParsedSourceRow); the
        # precise created-vs-updated split needs a pre-read of existing
        # source_row_keys, which the happy-path test doesn't exercise. Future
        # work (no current task) can wire that count if downstream consumers
        # need it; the run-level ``rows_upserted_total`` (sum of writes across
        # successful reports) is what the tests assert on.
        written = repo.upsert_many(
            tenant_id,
            parsed_rows,
            raw_file_id=raw_file_id,
            imported_by=triggered_by_user_id,
        )

        # Lifecycle transition: DOWNLOADED -> PARSED. Raises
        # ``RawFileAlreadyParsedError`` if called twice on the same file, which
        # would only happen on a re-entrant orchestrator bug.
        mark_parsed(session, raw_file_id=raw_file_id, tenant_id=tenant_id)

    # Future work (no current task): accurate created/updated/unchanged
    # split via a pre-read of existing source_row_keys. Until then, the
    # per-category split fields stay at 0 rather than over-claiming
    # everything as 'created' (which would lie on the second ingest of an
    # already-seen month).
    # ``counts["reports_succeeded"] += 1`` lives in ``run_one``'s outer
    # loop AFTER ``session.commit()`` so a commit failure (e.g. DB
    # disconnect on the per-report flush) is recorded as a failure once,
    # not double-counted as both succeeded and failed.
    return len(written)


# ============================================================================
# Purpose: Locate an existing raw report evidence row for idempotent reruns.
# Database/ORM: RawReportFileORM.
# Standards: Tenant-scoped lookup by source/report/month/checksum; no writes.
# Blast Radius: Raw evidence reuse only. Finance facts, authorization, audit,
#               Neo4j, and exports are unaffected by the lookup itself.
# Connections:
#   - File: backend/ums_smart_revenue/db/report_models.py ->
#     RawReportFileORM unique evidence identity.
#   - File: backend/ums_smart_revenue/connectors/runs/raw_file_helpers.py ->
#     Existing rows are lifecycle-checked before PARSED reuse.
# ============================================================================
def _find_existing_raw_file(
    session: Session,
    *,
    tenant_id: UUID,
    source: str,
    report_type: str,
    report_month: str,
    checksum: str,
) -> RawReportFileORM | None:
    return session.scalar(
        sa.select(RawReportFileORM).where(
            RawReportFileORM.tenant_id == tenant_id,
            RawReportFileORM.source == source,
            RawReportFileORM.report_type == report_type,
            RawReportFileORM.report_month == report_month,
            RawReportFileORM.checksum == checksum,
        )
    )


# ============================================================================
# Purpose: Create the raw report evidence row idempotently, including the
#          lookup/insert race where another worker commits the same
#          tenant/source/report/month/checksum after our pre-insert lookup.
# Database/ORM: RawReportFileORM insert/read; uniqueness enforced by
#               uq_raw_report_files_source_type_month_checksum.
# Standards: SQLAlchemy savepoint contains the duplicate insert failure; no
#            broad rollback so the surrounding connector_runs transaction
#            remains usable. Typed lifecycle checks stay in the caller.
# Blast Radius: Raw evidence creation only. Finance rows are written later
#               through the source-row repository; no graph projection impact
#               detected.
# Connections:
#   - Function: _process_one_report -> caller that links/parses/upserts.
#   - File: backend/ums_smart_revenue/db/report_models.py -> raw file unique
#     evidence identity.
# ============================================================================
def _get_or_create_raw_file(
    session: Session,
    *,
    tenant_id: UUID,
    source: str,
    report_type: str,
    report_month: str,
    checksum: str,
    storage_uri: str,
    downloaded_by: UUID | None,
) -> RawReportFileORM:
    raw_file = _find_existing_raw_file(
        session,
        tenant_id=tenant_id,
        source=source,
        report_type=report_type,
        report_month=report_month,
        checksum=checksum,
    )
    if raw_file is not None:
        return raw_file

    try:
        # FIX: the pre-insert lookup is not a lock. If another worker inserts
        # the same raw evidence row before this flush, contain the unique
        # violation in a SAVEPOINT, then re-read and reuse the winning row.
        with session.begin_nested():
            raw_file = RawReportFileORM(
                id=uuid4(),
                tenant_id=tenant_id,
                source=source,
                report_type=report_type,
                report_month=report_month,
                file_url=storage_uri,
                checksum=checksum,
                parse_status="DOWNLOADED",
                downloaded_by=downloaded_by,
            )
            session.add(raw_file)
            session.flush()
            return raw_file
    except sa.exc.IntegrityError:
        raw_file = _find_existing_raw_file(
            session,
            tenant_id=tenant_id,
            source=source,
            report_type=report_type,
            report_month=report_month,
            checksum=checksum,
        )
        if raw_file is None:
            raise
        return raw_file


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

    @staticmethod
    def produce_reports(
        *,
        session: Session,
        run: ConnectorRunEntry | None,
        credentials: Credentials,
        report_month: str,
        account_id: str,
    ) -> Iterator[ProducedReport]:
        # ``run`` is ``None`` on the T29 dry-run path; the runner body
        # never references it (the connector_runs lifecycle is owned by
        # ``run_one`` itself), so the widening is a pure type contract
        # change with no behavioural effect on the live path.
        http = GoogleHttpClient(credentials=credentials)
        try:
            client = YouTubeReportingClient(http=http)
            jobs = client.list_supported_jobs(account_id=account_id)
            for job in jobs:
                report_type = _require_text(job, "reportTypeId")
                job_id = _require_text(job, "id")
                try:
                    reports = client.list_reports_for_month(
                        account_id=account_id,
                        job_id=job_id,
                        report_month=report_month,
                    )
                except OAuthRefreshError:
                    raise
                except GoogleConnectorError as exc:
                    yield ProducedReportFailure(report_type=report_type, error=exc)
                    continue
                for report in reports:
                    try:
                        download_url = _require_text(report, "downloadUrl")
                        raw_bytes = client.fetch_report(download_url=download_url)
                        report_id = _require_text(report, "id")
                        parser_payload = _csv_to_parser_payload(
                            raw_bytes=raw_bytes,
                            report_id=report_id,
                            report_type=report_type,
                            report_month=report_month,
                        )
                    except OAuthRefreshError:
                        raise
                    except GoogleConnectorError as exc:
                        yield ProducedReportFailure(report_type=report_type, error=exc)
                        continue
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

    The estimated-revenue CSV is a daily export and can include lower-level
    breakdown dimensions (video, country, claimed status) plus non-revenue
    metric columns. The parser/repository layer models monthly channel
    revenue rows, so this adapter sums all requested-month breakdown rows by
    channel, optional content_owner, and currency before parser handoff.
    """
    import csv
    import io

    month_start, month_end = _month_bounds(
        report_month=report_month, report_id=report_id
    )
    expected_month = f"{month_start.year:04d}-{month_start.month:02d}"
    text = raw_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    totals: dict[tuple[str, str | None, str], Decimal] = {}
    for line_index, csv_row in enumerate(reader):
        # Normalize the date column. YouTube Reporting CSV uses ``date`` or
        # ``day``. The row is daily, but the parser payload below is monthly,
        # so this date is used only to ensure the row belongs to report_month.
        date_value = _first_present(csv_row, *_CSV_DATE_COLUMNS)
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
        row_month = f"{row_date.year:04d}-{row_date.month:02d}"
        if row_month != expected_month:
            raise _parser_payload_error(
                report_id=report_id,
                reason=(
                    f"csv row date {date_value!r} outside requested "
                    f"report_month {expected_month!r}"
                ),
            )

        # Channel + optional content_owner are the monthly attribution axes.
        # Lower-level official dimensions (video_id, country_code, etc.) are
        # deliberately NOT forwarded as parser dimensions because that would
        # create multiple source_row_keys for one monthly channel total.
        channel = _first_present(csv_row, *_CSV_CHANNEL_COLUMNS)
        if not channel:
            raise _parser_payload_error(
                report_id=report_id,
                reason="csv row missing channel/channel_id column",
            )
        content_owner = _first_present(csv_row, "content_owner")

        # Pull the estimated revenue amount. Accept either Google's
        # documented ``estimated_partner_revenue`` /
        # ``estimatedRevenue`` columns or the test-fixture shorthand
        # ``ad_revenue``. The aggregate is kept as Decimal and stringified
        # after summation so precision and trailing scale are preserved.
        amount = _first_present(csv_row, *_CSV_REVENUE_COLUMNS)
        if amount is None:
            raise _parser_payload_error(
                report_id=report_id,
                reason="csv row missing revenue column "
                "(expected one of: estimated_partner_revenue, "
                "estimatedRevenue, ad_revenue)",
            )
        try:
            amount_decimal = Decimal(amount)
        except InvalidOperation as exc:
            raise _parser_payload_error(
                report_id=report_id,
                reason=f"csv row revenue {amount!r} not a valid decimal",
            ) from exc
        if not amount_decimal.is_finite():
            raise _parser_payload_error(
                report_id=report_id,
                reason=f"csv row revenue {amount!r} not finite",
            )

        currency = _first_present(csv_row, *_CSV_CURRENCY_COLUMNS)
        if not currency:
            raise _parser_payload_error(
                report_id=report_id,
                reason="csv row missing currency column "
                "(expected one of: currency_code, currencyCode)",
            )

        key = (channel, content_owner, currency)
        totals[key] = totals.get(key, Decimal("0")) + amount_decimal

    rows: list[dict[str, object]] = []
    for line_index, ((channel, content_owner, currency), total) in enumerate(
        sorted(totals.items(), key=lambda item: (item[0][0], item[0][1] or "", item[0][2]))
    ):
        dimensions: dict[str, object] = {"channel": channel}
        if content_owner:
            dimensions["content_owner"] = content_owner
        rows.append(
            {
                "line_index": line_index,
                "date_range": {
                    "start": month_start.isoformat(),
                    "end": month_end.isoformat(),
                },
                "dimensions": dimensions,
                "metrics": {
                    "estimatedRevenue": str(total),
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


def _month_bounds(*, report_month: str, report_id: str) -> tuple[date_cls, date_cls]:
    expected_month = report_month.strip()
    try:
        year_text, month_text = expected_month.split("-", 1)
        year = int(year_text)
        month = int(month_text)
        last_day = monthrange(year, month)[1]
        month_start = date_cls(year, month, 1)
        month_end = date_cls(year, month, last_day)
    except ValueError as exc:
        raise _parser_payload_error(
            report_id=report_id,
            reason=f"report_month {expected_month!r} not YYYY-MM",
        ) from exc
    return month_start, month_end


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
    for candidate_key in _credential_key_candidates(connector_key):
        row = session.scalar(
            sa.select(ApiConnectorCredentialORM).where(
                ApiConnectorCredentialORM.tenant_id == tenant_id,
                ApiConnectorCredentialORM.connector_key == candidate_key,
                ApiConnectorCredentialORM.account_id == account_id,
            )
        )
        if row is not None:
            return row
    return None


def _credential_key_candidates(connector_key: str) -> tuple[str, ...]:
    candidates = [connector_key]
    source_key = _source_system_for_connector(connector_key)
    if source_key != connector_key:
        candidates.append(source_key)
    return tuple(dict.fromkeys(candidates))


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


def _safe_failure_detail(exc: Exception) -> str | None:
    """Return operator-safe detail for per-report summaries.

    ``per_report_failures`` intentionally remains a compact
    ``(report_type, error_class)`` API shape. The durable ``error_summary`` can
    carry schema reasons, such as missing CSV currency metadata, without
    including URLs, secret refs, storage paths, or arbitrary exception text.
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return None


def _summarize_failures(failures: list[tuple[str, str, str | None]]) -> str | None:
    """Pack per-report failures into an operator-safe summary string.

    Bounded by ``finish_run``'s 500-char truncation so the connector_runs
    column never overflows; this helper just builds the candidate text.
    """
    if not failures:
        return None
    items = ", ".join(
        f"{report_type}:{error_class}{f' ({detail})' if detail else ''}"
        for report_type, error_class, detail in failures
    )
    return f"{len(failures)} report(s) failed: {items}"


# ============================================================================
# Purpose: Construct (backend, scheme, bucket) for the configured blob store.
#          Returning the triple keeps scheme/bucket selection co-located with
#          backend construction so the orchestrator never has to ask
#          ``isinstance(backend, ...)`` to know what URI shape to emit.
# Database/ORM: None.
# Standards: Lazy-import the GCS client so SQLite test runs don't pay the cost
#            of constructing one. ``LocalFileStoreBackend`` and
#            ``GcsBlobStorageBackend`` are imported at module scope so test
#            patches on the bare symbol on this module replace what gets
#            instantiated here.
# Blast Radius: None — backend selection is internal; finance, auth, audit,
#               Neo4j projection, and exports are unaffected.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/blob_storage.py ->
#     LocalFileStoreBackend / GcsBlobStorageBackend / deterministic_blob_path.
# ============================================================================
def _build_blob_backend() -> tuple[BlobStorageBackend, str, str]:
    """Construct (backend, scheme, bucket) for the configured blob store.

    Selected by ``UMS_BLOB_BACKEND``:
      * ``file-store`` (default): LocalFileStoreBackend rooted at
        ``UMS_LOCAL_STORE_ROOT``. Scheme is ``file-store``; bucket is a
        logical path segment under root (``UMS_LOCAL_BLOB_BUCKET`` default
        ``"local"``) so deterministic URIs have the same shape as the GCS
        branch.
      * ``gcs``: GcsBlobStorageBackend with a real google-cloud-storage
        client. Scheme is ``gs``; bucket is ``UMS_GCS_BUCKET``.

    Returning the triple keeps scheme/bucket selection co-located with
    backend construction — the orchestrator never has to ask
    ``isinstance(backend, ...)`` to know what URI shape to emit.
    """
    backend_name = os.getenv("UMS_BLOB_BACKEND", "file-store")
    if backend_name == "file-store":
        root = Path(os.getenv("UMS_LOCAL_STORE_ROOT", str(Path.cwd() / "_local_blob_store")))
        bucket = os.getenv("UMS_LOCAL_BLOB_BUCKET", "local")
        return LocalFileStoreBackend(root=root), "file-store", bucket
    if backend_name == "gcs":
        # Lazy import the GCS client only on the gcs branch so SQLite test
        # runs don't pay the cost of constructing one.
        try:
            from google.cloud.storage import Client as GcsClient  # type: ignore[import-untyped]
        except Exception as exc:
            raise BlobStorageConfigurationError(
                backend=backend_name,
                detail=f"google-cloud-storage client import failed: {type(exc).__name__}",
            ) from exc

        bucket = os.getenv("UMS_GCS_BUCKET", "ums-smart-revenue-raw")
        try:
            client = GcsClient()
        except Exception as exc:
            raise BlobStorageConfigurationError(
                backend=backend_name,
                detail=f"google-cloud-storage client construction failed: {type(exc).__name__}",
            ) from exc
        return GcsBlobStorageBackend(client=client), "gs", bucket
    raise BlobStorageConfigurationError(
        backend=backend_name,
        detail="expected 'file-store' or 'gcs'",
    )


def _source_system_for_connector(connector_key: str) -> str:
    """Map the connector_key to the ALLOWED_SOURCE_SYSTEMS member.

    Keeps the orchestrator decoupled from B1's source_system string set
    while still letting the raw_report_files row carry the value the
    existing PR #43 repository expects.
    """
    mapping = {
        "youtube-reporting": "youtube_reporting",
        "youtube_reporting": "youtube_reporting",
        "youtube-analytics": "youtube_analytics",
        "youtube_analytics": "youtube_analytics",
        "adsense-management": "adsense_management",
        "adsense_management": "adsense_management",
    }
    try:
        return mapping[connector_key]
    except KeyError as exc:
        raise ValueError(
            f"unknown connector_key for source_system mapping: {connector_key!r}"
        ) from exc


_EXTENSIONS: dict[str, str] = {
    "youtube-reporting": "csv",
    "youtube_reporting": "csv",
    "youtube-analytics": "json",
    "youtube_analytics": "json",
    "adsense-management": "json",
    "adsense_management": "json",
}


def _extension_for_connector(connector_key: str) -> str:
    """File extension used inside the deterministic blob URI.

    YouTube Reporting downloads are CSV; YouTube Analytics (B2.5) and
    AdSense (B2.6) return JSON. Centralised here so the path-builder stays
    consistent across slices. Mirrors the explicit-mapping + KeyError ->
    ValueError pattern of ``_source_system_for_connector`` so a missing
    entry for a future B2.5/B2.6 registration fails loudly instead of
    silently emitting ``.json``.
    """
    try:
        return _EXTENSIONS[connector_key]
    except KeyError as exc:
        raise ValueError(
            f"unknown connector_key for extension dispatch: {connector_key!r}"
        ) from exc


def _parser_for_connector(connector_key: str) -> YouTubeReportingParser:
    """Return the source-row parser bound to a given connector key.

    B2.5/B2.6 will widen this beyond YouTubeReportingParser; the helper
    isolates that future change from ``run_one`` itself. For now any non-
    YouTube-Reporting key would not reach this point because the runner
    isn't registered.
    """
    if connector_key in {"youtube-reporting", "youtube_reporting"}:
        return YouTubeReportingParser()
    raise ValueError(f"no parser bound for connector_key: {connector_key!r}")


# ----------------------------------------------------------------------------
# Module-load registration
# ----------------------------------------------------------------------------

# An instance (not the class) is registered: B2.5/B2.6 runners may carry
# per-connector config in their constructors, and the dispatch contract
# returns the registered value directly to the orchestrator.
register_connector(key="youtube-reporting", runner=YouTubeReportingRunner())
register_connector(key="youtube_reporting", runner=YouTubeReportingRunner())
