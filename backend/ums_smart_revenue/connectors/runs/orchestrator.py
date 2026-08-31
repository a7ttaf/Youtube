# ============================================================================
# Purpose: Orchestrate one connector run from credential resolution through
#   report production, blob evidence, parsing, source-row upsert, lifecycle
#   audit, normalization, and terminal outcome reporting.
# Database/ORM: ConnectorRunORM, RawReportFileORM, GoogleRevenueSourceRowORM,
#   ApiConnectorCredentialORM, AuditLogORM, and org/finance projection reads.
# Standards: Typed connector outcomes/errors, transaction boundaries around
#   dependent writes, fail-closed tenant checks, and safe audit/error payloads.
# Blast Radius: Connector ingestion, audit trail, source-row persistence, and
#   finance projection inputs. No UI-side official finance calculation.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/audit.py -> lifecycle
#     audit emitters used by run finalization.
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py -> post
#     run fact projection and skipped/projection-failed audit signals.
# ============================================================================
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

import json
import logging
import os
import tempfile
import types as _types  # SimpleNamespace used for dry-run tenant_id proxy
from calendar import monthrange
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import AuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.config.settings import (
    GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
)
from ums_smart_revenue.connectors.google.adsense_management_client import (
    AdSenseManagementClient,
)
from ums_smart_revenue.connectors.google.audit import (
    build_connector_service_principal,
    emit_raw_file_downloaded,
    emit_raw_file_failed,
    emit_raw_file_parsed,
    emit_run_finished,
    emit_run_started,
)
from ums_smart_revenue.connectors.google.errors import (
    BlobStorageConfigurationError,
    ConnectorServicePrincipalUnavailableError,
    CredentialNotFoundError,
    GoogleApiResponseError,
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
from ums_smart_revenue.connectors.google.youtube_analytics_client import (
    YouTubeAnalyticsClient,
    calendar_month_end_iso,
    list_target_channels,
)
from ums_smart_revenue.connectors.google.youtube_analytics_client import (
    _build_query_request as _build_analytics_query_request,
)
from ums_smart_revenue.connectors.google.youtube_reporting_client import (
    YouTubeReportingClient,
)
from ums_smart_revenue.connectors.google_source_parsers import (
    AdSenseManagementParser,
    YouTubeAnalyticsParser,
    YouTubeReportingParser,
)
from ums_smart_revenue.connectors.google_source_rows import (
    ParsedSourceRow,
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.connectors.keys import (
    credential_key_candidates,
    source_system_for_connector,
)
from ums_smart_revenue.connectors.runs.blob_storage import (
    BlobStorageBackend,
    GcsBlobStorageBackend,
    LocalFileStoreBackend,
    compute_checksum,
    deterministic_blob_path,
    upload_and_verify,
)
from ums_smart_revenue.connectors.runs.normalization import (
    SqlAlchemyIngestedSourceRowNormalizationAdapter,
)
from ums_smart_revenue.connectors.runs.raw_file_helpers import (
    mark_failed,
    mark_parsed,
)
from ums_smart_revenue.connectors.runs.repository import (
    CONNECTOR_RUN_COUNT_KEYS,
    ConnectorRunEntry,
    ConnectorRunValidationError,
    finish_run,
    link_raw_file,
    start_run,
    validate_report_month,
)
from ums_smart_revenue.db.lane import platform_lane
from ums_smart_revenue.db.report_models import RawReportFileORM
from ums_smart_revenue.db.security_models import ApiConnectorCredentialORM

logger = logging.getLogger(__name__)

__all__ = [
    "AdSenseManagementRunner",
    "ConnectorRunOutcome",
    "ConnectorRunner",
    "ProducedReportSuccess",
    "YouTubeAnalyticsRunner",
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
_CSV_DEFAULT_CURRENCY_BY_REPORT_TYPE = {
    # Google's documented YouTube Reporting estimated-revenue bulk schema has
    # no currency field; this project ingests those Reporting amounts as USD
    # until exchange-rate support widens the finance pipeline.
    "content_owner_estimated_revenue_a1": "USD",
}


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

    ``analytics_cleanup_blocked`` is True only for YouTube Analytics runs
    whose deferred stale-row cleanup was blocked by a per-report sibling
    failure (``_DeferredAnalyticsStaleCleanupState.blocked``). Non-Analytics
    runs always carry False because they have no deferred cleanup. The
    post-run normalize stage uses this to skip PARTIAL Analytics runs whose
    cleanup did not complete -- normalizing them would let the normalizer
    pick canonical rows from stale data and rewrite facts with old revenue.
    """

    run: ConnectorRunEntry | None
    counts: dict[str, int]
    per_report_failures: list[tuple[str, str]]
    analytics_cleanup_blocked: bool = False


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
    raw_reports: tuple[_CsvReportDownload, ...] = ()


@dataclass(frozen=True)
class _CsvReportDownload:
    """Spool entry pairing a CSV ``report_id`` with its in-memory bytes or temp-file path."""

    report_id: str
    raw_bytes: bytes | None = None
    raw_path: Path | None = None

    def read_bytes(self) -> bytes:
        """Return the spooled CSV bytes, reading the temp file lazily if needed."""
        if self.raw_bytes is not None:
            return self.raw_bytes
        if self.raw_path is None:
            raise RuntimeError("CSV report download has no raw bytes")
        return self.raw_path.read_bytes()

    def cleanup(self) -> None:
        """Best-effort unlink the temp file backing this download, ignoring missing-file errors."""
        if self.raw_path is None:
            return
        try:
            self.raw_path.unlink()
        except FileNotFoundError:
            return


@dataclass(frozen=True)
class ProducedReportSuccess:
    """Successful producer result bundling the parser payload with its raw CSV downloads."""

    report_type: str
    parser_payload: dict[str, object]
    raw_reports: tuple[_CsvReportDownload, ...]


ProducedReport = (
    ProducedReportSuccess | tuple[str, dict[str, object], bytes] | ProducedReportFailure
)


@dataclass(frozen=True)
class _DeferredStaleCleanupPlan:
    """One per-scope stale-row cleanup plan deferred for analytics aggregation."""

    source_system: str
    report_type: str
    report_month: str
    source_account_id: str
    keep_source_row_keys: frozenset[str]


@dataclass
class _DeferredAnalyticsStaleCleanupState:
    """Accumulates per-channel keep-keys for one owner/month until flush time.

    ``blocked`` is set to True when any sibling channel in the run failed so the
    cleanup is skipped and previously-persisted rows for that scope are not
    deleted on a partial run.

    ``attempted_channel_ids`` records the youtube_channel_id of every channel
    that successfully produced a parsed payload in this run. Flush reads this
    set to preserve historical rows belonging to channels that were NOT part
    of the current target set (e.g. previously-active channels that were
    deactivated or removed from the revenue-required scope) — without this
    guard a content-owner/month cleanup would silently erase those rows.
    """

    blocked: bool = False
    keep_source_row_keys_by_scope: dict[tuple[str, str, str, str], set[str]] = field(
        default_factory=dict
    )
    attempted_channel_ids: set[str] = field(default_factory=set)


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
        """Yield each successful or failed produced report (per-report bucket-B contract)."""
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
    validate_report_month(report_month)
    outcome = _run_one_with_credentials(
        session=session,
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
        report_month=report_month,
        dry_run=dry_run,
        triggered_by_user_id=triggered_by_user_id,
        credentials=_credentials_for_run(
            session=session,
            tenant_id=tenant_id,
            connector_key=connector_key,
            account_id=account_id,
        ),
    )
    # The post-run normalize audit context (sink + actor) is built INSIDE
    # _normalize_ingested_source_rows so a dry-run path does not require
    # UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID to be set -- the dry-run
    # returns immediately and the audit context is unused.
    _normalize_ingested_source_rows(
        session=session,
        tenant_id=tenant_id,
        report_month=report_month,
        dry_run=dry_run,
        triggered_by_user_id=triggered_by_user_id,
        outcome=outcome,
    )
    return outcome


# ============================================================================
# Purpose: Post-run finance projection stage. After a live connector run
#          commits source rows, decide whether post-run finance projection
#          should run and delegate DB/transaction work to the normalization
#          adapter. The gate is keyed off the terminal run status
#          (SUCCEEDED/PARTIAL) and a no-op source-row mutation guard because
#          re-normalizing a month with no upserted or deleted source rows puts
#          a full-month projection on the connector hot path for no benefit.
#          The gate is not keyed off the per-report success count because the
#          per-report loop's
#          deferred stale-row cleanup for YouTube Analytics only flushes after
#          the entire loop completes; a FAILED run leaves source rows un-pruned
#          and normalizing those would let the normalizer pick canonical rows
#          from stale data.
# Database/ORM: MonthlyChannelRevenueFactORM (WRITE via the normalizer's
#               record_fact upsert). Reads google_revenue_source_rows,
#               youtube_channels, finance_month_close. No schema change.
# Standards: Orchestration-only gate. DB access and transaction control for
#            the projection live in SqlAlchemyIngestedSourceRowNormalizationAdapter.
#            No bare except and no secrets/PII logged.
# Blast Radius: FINANCE -- writes MonthlyChannelRevenueFactORM (the dashboard,
#               allocation, reconciliation, net-revenue, and exports all read
#               it). Locked-month facts are NEVER overwritten: a pure-SELECT
#               prefilter skips LOCKED months, and the normalizer's own upfront
#               + per-write fail-closed guard catches any lock acquired
#               mid-flight. ALLOCATION / MANUAL_UPLOAD facts use disjoint
#               source_kind values and are untouched. On non-lock normalize
#               failure, the run is rewritten to FAILED via
#               ``record_projection_failure`` so the run history reflects the
#               missing projection. No graph projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py ->
#     Adapter owns month-close checks, normalizer invocation, audit writes,
#     projection-failure recording, and commit/rollback.
#   - File: Docs/superpowers/specs/2026-06-10-ingestion-source-rows-to-facts-design.md
#     -> integration point, locked-month policy, blast-radius review.
# ============================================================================
def _normalize_ingested_source_rows(
    *,
    session: Session,
    tenant_id: UUID,
    report_month: str,
    dry_run: bool,
    triggered_by_user_id: UUID | None,
    outcome: ConnectorRunOutcome,
) -> None:
    """Project this run's ingested source rows into revenue facts (fail-closed on locks)."""
    # Skip on dry-run (no committed source rows) and on any run that did not
    # reach a terminal SUCCEEDED/PARTIAL status. The gate is intentionally
    # keyed off the terminal run status (not the per-report count) because
    # the per-report loop's deferred stale-row cleanup for YouTube Analytics
    # only flushes after the entire loop completes: a FAILED run leaves
    # source rows un-pruned, and normalizing those would let the normalizer
    # pick canonical rows from stale data and rewrite facts with old revenue.
    # This matches the design spec §"Failure handling" -- "run status FAILED
    # -> no normalize (no new committed source rows worth projecting; prior
    # month state intact)".
    if dry_run:
        return
    run = outcome.run
    if run is None or run.status not in ("SUCCEEDED", "PARTIAL"):
        return
    # PARTIAL runs with one or more failed report scopes are also unsafe
    # to normalize. The failed report's intended source rows were never
    # committed, so the month-wide normalize would project only the
    # committed (potentially stale) source rows from the successful
    # sibling reports; a partial YouTube Analytics run additionally has
    # blocked the deferred stale-row cleanup, leaving stale rows
    # eligible for canonical selection. Skip normalize to keep the
    # previous month's facts intact; the next SUCCEEDED run for the same
    # month will rewrite them.
    if run.status == "PARTIAL" and outcome.per_report_failures:
        logger.info(
            "ingestion normalize skipped (partial run with failed scopes) "
            "tenant_id=%s month=%s run_id=%s failed_scopes=%d",
            tenant_id,
            report_month,
            run.id,
            len(outcome.per_report_failures),
        )
        return
    # FIX: restore the analytics_cleanup_blocked gate read removed in a3a584a.
    # The ConnectorRunOutcome docstring promises this gate consumes the flag,
    # but the read was dropped, leaving a phantom guard: today's safety is
    # incidental (blocked=True co-occurs with a per_report_failures entry, so
    # the check above already returned). This makes the guard real and strictly
    # more conservative -- a PARTIAL Analytics run whose deferred stale-row
    # cleanup did not complete skips normalize even if per_report_failures is
    # empty, because the un-pruned stale rows could be picked as canonical and
    # rewrite facts with old revenue.
    if run.status == "PARTIAL" and outcome.analytics_cleanup_blocked:
        logger.info(
            "ingestion normalize skipped (analytics cleanup blocked) "
            "tenant_id=%s month=%s run_id=%s",
            tenant_id,
            report_month,
            run.id,
        )
        return
    rows_upserted_total = int(outcome.counts.get("rows_upserted_total") or 0)
    rows_deleted_stale = int(outcome.counts.get("rows_deleted_stale") or 0)
    if rows_upserted_total <= 0 and rows_deleted_stale <= 0:
        logger.info(
            "ingestion normalize skipped (no source row mutations) tenant_id=%s month=%s run_id=%s",
            tenant_id,
            report_month,
            run.id,
        )
        return

    # Audit actor context is built AFTER the dry-run / status / cleanup gates
    # pass so a dry-run path does not require UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID
    # to be set. Repository-backed normalization owns DB reads, writes,
    # transactions, and audit persistence behind the adapter boundary.
    if triggered_by_user_id is not None:
        actor_user_id = str(triggered_by_user_id)
        audit_actor: UserPrincipal = _principal_for_triggered_user(
            triggered_by_user_id=actor_user_id, tenant_id=tenant_id
        )
    else:
        audit_actor = _build_connector_service_principal_or_raise(tenant_id=tenant_id)
        actor_user_id = audit_actor.user_id

    SqlAlchemyIngestedSourceRowNormalizationAdapter(
        session, tenant_id=tenant_id
    ).normalize_after_run(
        report_month=report_month,
        run=run,
        actor_user_id=actor_user_id,
        audit_actor=audit_actor,
    )


def _principal_for_triggered_user(*, triggered_by_user_id: str, tenant_id: UUID) -> UserPrincipal:
    """Build a minimal UserPrincipal for the user that triggered the run.

    The post-run normalize audit rows are attributed to the triggering
    user (when present) so operators who inspect a normalized fact can
    see who triggered the ingest that produced it. The principal is a
    best-effort shell -- the SqlAlchemyAuditSink looks up the real
    ``users`` row, and if absent, stashes the raw actor UUID in
    ``details["actor_user_id"]`` and sets ``user_id=None`` on the row.
    This matches how the API-driven ``POST /facts`` import handles its
    actor.
    """
    return UserPrincipal(
        user_id=triggered_by_user_id,
        email=f"trigger:{triggered_by_user_id}",
        is_service_account=False,
        disabled=False,
        tenant_id=str(tenant_id),
    )


# ============================================================================
# Purpose: Wrap ``build_connector_service_principal`` so a missing
#          ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`` surfaces as the typed
#          ``ConnectorServicePrincipalUnavailableError`` (Bucket-A pre-start
#          family) instead of an untyped ``ValueError``. The executor's
#          ``except GoogleConnectorError`` branch then writes a
#          ``job_failed_before_start`` audit row; without this wrap, the
#          ValueError would land in the catch-all log-only branch and
#          operators would see a 202 with no run row, no failure audit,
#          and no reason the job never started.
# Database/ORM: None.
# Standards: Fail-closed in Bucket A. The env var name is carried on the
#            exception so the canned audit message is self-describing.
# Blast Radius: Audit log only; no finance, scope, or graph projection change.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/audit.py ->
#     build_connector_service_principal raises ValueError on missing env.
#   - File: backend/ums_smart_revenue/connectors/google/errors.py ->
#     ConnectorServicePrincipalUnavailableError.
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py::_run_job
#     -> catches GoogleConnectorError and audits as Bucket-A failure.
# ============================================================================
def _build_connector_service_principal_or_raise(*, tenant_id: UUID) -> UserPrincipal:
    """Build the service principal or raise a typed Bucket-A exception.

    Raises:
        ConnectorServicePrincipalUnavailableError: When
            ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`` is unset. Subclasses
            ``GoogleConnectorError`` so the executor's existing Bucket-A
            catch handles it.
    """
    try:
        return build_connector_service_principal(tenant_id=tenant_id)
    except ValueError as exc:
        raise ConnectorServicePrincipalUnavailableError(
            env_var=GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
        ) from exc


# ============================================================================
# Purpose: Dispatch a credential-resolved connector run to the dry-run or live
#          path while keeping the public run_one entrypoint branch-free.
# Database/ORM: None directly; delegated dry-run/live helpers own reads/writes.
# Standards: Private orchestration helper with typed parameters and no logging.
# Blast Radius: None detected; branch ownership only, no behavior change.
# Connections:
#   - Function: run_one -> loads credentials and calls this helper.
#   - Function: _run_dry_run -> read-only validation path.
#   - Function: _run_live -> stateful ingestion path.
# ============================================================================
def _run_one_with_credentials(
    *,
    session: Session,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    report_month: str,
    dry_run: bool,
    triggered_by_user_id: UUID | None,
    credentials: Credentials,
) -> ConnectorRunOutcome:
    """Run one connector slice once credentials are resolved (dispatches dry-run vs live)."""
    if dry_run:
        return _run_dry_run(
            session=session,
            tenant_id=tenant_id,
            connector_key=connector_key,
            account_id=account_id,
            report_month=report_month,
            credentials=credentials,
        )
    return _run_live(
        session=session,
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
        report_month=report_month,
        credentials=credentials,
        triggered_by_user_id=triggered_by_user_id,
    )


def resolve_connector_credentials(
    *,
    session: Session,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
) -> Credentials:
    """Resolve and validate the credential row for a given tenant/connector/account."""
    credential = _load_credential(
        session,
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
    )
    if credential is None:
        raise CredentialNotFoundError(connector_key=connector_key, account_id=account_id)
    if credential.status != "active":
        raise InactiveCredentialError(credential_id=str(credential.id), status=credential.status)

    ensure_default_resolvers()
    # FIX: Admin/API-created credentials may persist surrounding whitespace in
    # the secret URI. Normalize before resolver dispatch so valid refs do not
    # fail scheme lookup.
    payload = resolve_secret(credential.encrypted_secret_ref.strip())
    credentials = build_credentials_from_payload(payload)
    # ========================================================================
    # Purpose: Part 2 -- stamp credential refresh telemetry at the single
    #   chokepoint where the OAuth refresh outcome is known and the credential
    #   ORM row is in-session. SUCCESS rides the caller's commit (live run ->
    #   persisted at start_run commit; dry-run/CLI never commits -> not
    #   persisted, the intended dry-run semantics). FAILURE commits the stamp on
    #   THIS session (the only safe point: resolve runs BEFORE any run_one write,
    #   so nothing run-related is pending) then re-raises, leaving Bucket-A
    #   propagation intact (CLI exit 2 / test-route 200 / worker Bucket-A audit).
    # Database/ORM: ApiConnectorCredentialORM (UPDATE 4 telemetry columns;
    #   tenant-writable -> NO platform_lane needed).
    # Standards: error_class stores type(exc.inner or exc).__name__ only, never
    #   str(exc) (no message text). Invariant: resolve-runs-before-any-run-write
    #   -> the same-session failure commit is safe.
    # Blast Radius: Connector credential read surface. No finance, audit, or
    #   graph projection impact; OAuthRefreshError still propagates.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/oauth.py ->
    #     refresh_credentials populates credentials.expiry on success.
    # ========================================================================
    try:
        refresh_credentials(credentials)
    except OAuthRefreshError as exc:
        inner = getattr(exc, "inner", None)
        _stamp_credential_refresh(
            credential,
            status="failed",
            error_class=type(inner or exc).__name__,
            token_expiry=None,
        )
        session.commit()
        raise
    _stamp_credential_refresh(
        credential,
        status="succeeded",
        error_class=None,
        token_expiry=getattr(credentials, "expiry", None),
    )
    return credentials


def _stamp_credential_refresh(
    credential: ApiConnectorCredentialORM,
    *,
    status: str,
    error_class: str | None,
    token_expiry: datetime | None,
) -> None:
    """Mutate the in-session credential row's four telemetry columns.

    FIX: on a failed refresh, the prior ``token_expiry_at`` (recorded from
    the last successful refresh) is cleared so the credential-health read
    surface does not display a stale success expiry next to
    ``last_refresh_status='failed'``. Always assign; the in-session
    mutation rides the caller's commit.
    """
    credential.last_refresh_attempt_at = datetime.now(UTC)
    credential.last_refresh_status = status
    credential.last_refresh_error_class = error_class
    credential.token_expiry_at = token_expiry


# Backwards-compatible internal alias (existing call sites unchanged).
_credentials_for_run = resolve_connector_credentials


# ============================================================================
# Purpose: Spec §5.4 dry-run path -- list jobs, fetch report bytes, and parse
#          for row counts without creating connector_runs, raw files, source
#          rows, or blob uploads.
# Database/ORM: Read-only by contract; a SAVEPOINT rolls back accidental
#               runner/parser writes before returning.
# Standards: Bucket A credential validation already happened in run_one; one
#            bad report increments reports_failed and does not abort dry-run.
# Blast Radius: None. No finance, audit, Neo4j, export, or auth mutation.
# Connections:
#   - Function: run_one -> delegates dry-run after credential resolution.
#   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
#     §5.4 -> dry-run contract.
# ============================================================================
def _run_dry_run(
    *,
    session: Session,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    report_month: str,
    credentials: Credentials,
) -> ConnectorRunOutcome:
    """Execute the connector's produce/parse path inside a rolled-back savepoint."""
    counts = _zero_counts()
    per_report_failures: list[tuple[str, str]] = []
    runner = dispatch_connector(key=connector_key)
    parser = _parser_for_connector(connector_key)
    savepoint = session.begin_nested()
    try:
        # Pass a lightweight proxy that carries tenant_id so runners that
        # need it (e.g. YouTubeAnalyticsRunner) can read run.tenant_id
        # without requiring a live ConnectorRunEntry on the dry-run path.
        # FIX: str()-wrap tenant_id to mirror ConnectorRunEntry.tenant_id: str.
        # UUID(uuid_object) raises AttributeError on Python 3.14 because
        # UUID.__init__ expects a hex string, not a UUID instance.
        _dry_run_proxy = _types.SimpleNamespace(tenant_id=str(tenant_id))
        for produced in runner.produce_reports(
            session=session,
            run=_dry_run_proxy,  # type: ignore[arg-type]
            credentials=credentials,
            report_month=report_month,
            account_id=account_id,
        ):
            counts["reports_attempted"] += 1
            # FIX: extract the report_type from the produced report so a
            # per-report failure carries it in the returned outcome. Previously
            # the dry-run outcome always had an empty per_report_failures
            # list, so the executor's job_dry_run_completed audit row
            # (the only durable record of what the dry-run found) listed no
            # failures even when individual reports threw.
            report_type, _payload, _raws, _failure = _unpack_produced_report(produced)
            try:
                if isinstance(produced, ProducedReportFailure):
                    raise produced.error
                if isinstance(produced, ProducedReportSuccess):
                    parser_payload = produced.parser_payload
                else:
                    _report_type, parser_payload, _raw_bytes = produced
                parsed_rows = list(parser.parse(parser_payload, tenant_id=tenant_id))
                counts["rows_upserted_total"] += len(parsed_rows)
                counts["reports_succeeded"] += 1
            except Exception as exc:
                counts["reports_failed"] += 1
                per_report_failures.append((report_type, type(exc).__name__))
    finally:
        savepoint.rollback()
    return ConnectorRunOutcome(
        run=None,
        counts=counts,
        per_report_failures=per_report_failures,
    )


# ============================================================================
# Purpose: Execute the live connector run after Bucket A has passed, ensuring
#          all post-start failures finish the connector_runs row terminally.
# Database/ORM: ConnectorRunORM lifecycle, RawReportFileORM evidence, run/raw
#               join rows, GoogleRevenueSourceRowORM upserts, and commits.
# Standards: Per-report failures are contained; generator-level failures finish
#            the run FAILED; fail-safe cleanup prevents stuck RUNNING rows.
# Blast Radius: Finance source rows and operator-visible run state.
# Connections:
#   - Function: run_one -> delegates the non-dry-run path here.
#   - Function: _process_live_reports -> per-report processing loop.
# ============================================================================
def _run_live(
    *,
    session: Session,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    report_month: str,
    credentials: Credentials,
    triggered_by_user_id: UUID | None,
) -> ConnectorRunOutcome:
    """Open the live ``connector_runs`` row and drive the produce/parse/upsert loop end-to-end."""
    # ============================================================================
    # Purpose: Build the connector service principal BEFORE ``start_run`` so a
    #          missing ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`` fails closed in
    #          Bucket A (no half-created RUNNING row). The sink is built after
    #          ``start_run`` because it needs the same session that owns the run
    #          row and audit writes; both share one transaction for the STARTED
    #          edge per spec §8.4.
    # Database/ORM: None for the principal; ``SqlAlchemyAuditSink`` writes to
    #               ``AuditLogORM`` via the session below.
    # Standards: Fail-closed in Bucket A on missing env (typed
    #            ConnectorServicePrincipalUnavailableError bubbles to the
    #            caller before any connector_runs row is created; the executor
    #            catches it as a GoogleConnectorError and writes a
    #            job_failed_before_start audit row -- see
    #            connectors/runs/executor.py::_run_job).
    # Blast Radius: Audit log only.
    # ============================================================================
    audit_actor = _build_connector_service_principal_or_raise(tenant_id=tenant_id)

    audit_sink: AuditSink = SqlAlchemyAuditSink(session, tenant_id=tenant_id)
    # FIX: the STARTED edge writes audit_logs (TENANT_PLATFORM_ONLY_WRITE), so
    # elevate the start_run + emit + commit transaction to app_platform; on the
    # tenant lane (CLI / executor) the audit INSERT would permission-deny and
    # the run could not even open. No-op off Postgres.
    with platform_lane(session):
        run_entry = start_run(
            session,
            tenant_id=tenant_id,
            connector_key=connector_key,
            account_id=account_id,
            report_month=report_month,
            triggered_by_user_id=triggered_by_user_id,
        )
        # Spec §8.4: CONNECTOR_JOB_RUN/STARTED is committed with start_run -- the
        # STARTED row and the run row share one transaction, so a process crash
        # between the two writes cannot leave a RUNNING run without its STARTED
        # audit edge.
        emit_run_started(
            sink=audit_sink,
            actor=audit_actor,
            run=run_entry,
            dry_run=False,
        )
        session.commit()

    counts = _zero_counts()
    per_report_failures: list[tuple[str, str]] = []
    per_report_failure_details: list[tuple[str, str, str | None]] = []
    finished = False
    try:
        try:
            analytics_cleanup_blocked = _process_live_reports(
                session=session,
                tenant_id=tenant_id,
                connector_key=connector_key,
                account_id=account_id,
                report_month=report_month,
                credentials=credentials,
                triggered_by_user_id=triggered_by_user_id,
                run_entry=run_entry,
                counts=counts,
                per_report_failures=per_report_failures,
                per_report_failure_details=per_report_failure_details,
                audit_sink=audit_sink,
                audit_actor=audit_actor,
            )
        except Exception as exc:
            finished_run = _finish_failed_live_run(
                session=session,
                tenant_id=tenant_id,
                run_entry=run_entry,
                counts=counts,
                exc=exc,
                audit_sink=audit_sink,
                audit_actor=audit_actor,
            )
            finished = True
            return ConnectorRunOutcome(
                run=finished_run,
                counts=counts,
                per_report_failures=per_report_failures,
                analytics_cleanup_blocked=False,
            )

        finished_run = _finish_aggregate_live_run(
            session=session,
            tenant_id=tenant_id,
            run_entry=run_entry,
            counts=counts,
            per_report_failure_details=per_report_failure_details,
            audit_sink=audit_sink,
            audit_actor=audit_actor,
        )
        finished = True
        return ConnectorRunOutcome(
            run=finished_run,
            counts=counts,
            per_report_failures=per_report_failures,
            analytics_cleanup_blocked=analytics_cleanup_blocked,
        )
    finally:
        if not finished:
            _sweep_unfinished_live_run(
                session=session,
                tenant_id=tenant_id,
                run_entry=run_entry,
                counts=counts,
                audit_sink=audit_sink,
                audit_actor=audit_actor,
            )


def _process_live_reports(
    *,
    session: Session,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    report_month: str,
    credentials: Credentials,
    triggered_by_user_id: UUID | None,
    run_entry: ConnectorRunEntry,
    counts: dict[str, int],
    per_report_failures: list[tuple[str, str]],
    per_report_failure_details: list[tuple[str, str, str | None]],
    audit_sink: AuditSink,
    audit_actor: UserPrincipal,
) -> bool:
    """Drive the live run end-to-end and return whether Analytics cleanup was blocked.

    Returns True if the run used the deferred Analytics stale-row cleanup
    AND that cleanup was blocked by a per-report sibling failure
    (``_DeferredAnalyticsStaleCleanupState.blocked``). Non-Analytics runs
    always return False. The post-run normalize stage uses this flag to
    skip PARTIAL Analytics runs whose cleanup did not complete.
    """
    runner = dispatch_connector(key=connector_key)
    backend, scheme, bucket = _build_blob_backend()
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    parser = _parser_for_connector(connector_key)
    deferred_analytics_cleanup = (
        _DeferredAnalyticsStaleCleanupState()
        if isinstance(parser, YouTubeAnalyticsParser)
        else None
    )
    ordering_index = 0
    for produced in runner.produce_reports(
        session=session,
        run=run_entry,
        credentials=credentials,
        report_month=report_month,
        account_id=account_id,
    ):
        # FIX: each per-report transaction commits raw-file + source-row writes
        # (tenant-writable) TOGETHER with its DOWNLOADED / PARSED / FAILED audit
        # edges (audit_logs is TENANT_PLATFORM_ONLY_WRITE). Elevate the whole
        # per-report iteration so the audit INSERTs do not permission-deny on
        # the tenant lane. MEASURED semantics: the elevation spans the entire
        # iteration -- it persists across the iteration's internal commit AND
        # the failure recorder's mid-block rollback+commit (which then writes
        # the platform-only FAILED audit edge), because the after_begin hook
        # keeps re-elevating while the platform-lane flag is set. The elevation
        # ends only at block exit; the next iteration re-pins app_tenant via the
        # next after_begin (the flag was popped on exit).
        with platform_lane(session):
            raw_file_count = _handle_live_produced_report(
                session=session,
                tenant_id=tenant_id,
                connector_key=connector_key,
                account_id=account_id,
                report_month=report_month,
                triggered_by_user_id=triggered_by_user_id,
                run_entry=run_entry,
                backend=backend,
                scheme=scheme,
                bucket=bucket,
                parser=parser,
                repo=repo,
                produced=produced,
                ordering_index=ordering_index,
                counts=counts,
                per_report_failures=per_report_failures,
                per_report_failure_details=per_report_failure_details,
                deferred_analytics_cleanup=deferred_analytics_cleanup,
                audit_sink=audit_sink,
                audit_actor=audit_actor,
            )
        ordering_index += max(raw_file_count, 1)
    if deferred_analytics_cleanup is not None:
        counts["rows_deleted_stale"] += _flush_deferred_stale_cleanup_plans(
            repo=repo,
            tenant_id=tenant_id,
            deferred_cleanup=deferred_analytics_cleanup,
        )
    return bool(deferred_analytics_cleanup is not None and deferred_analytics_cleanup.blocked)


def _unpack_produced_report(
    produced: ProducedReport,
) -> tuple[
    str,
    dict[str, object] | None,
    tuple[_CsvReportDownload, ...] | None,
    Exception | None,
]:
    """
    Normalise a ``ProducedReport`` into its
    (report_type, parser_payload, raw_reports, failure).
    """
    if isinstance(produced, ProducedReportFailure):
        return (
            produced.report_type,
            None,
            produced.raw_reports or None,
            produced.error,
        )
    if isinstance(produced, ProducedReportSuccess):
        return produced.report_type, produced.parser_payload, produced.raw_reports, None
    report_type, parser_payload, raw_bytes = produced
    raw_report = _CsvReportDownload(
        report_id=_legacy_report_id(
            parser_payload=parser_payload,
            report_type=report_type,
        ),
        raw_bytes=raw_bytes,
    )
    return report_type, parser_payload, (raw_report,), None


def _legacy_report_id(*, parser_payload: dict[str, object], report_type: str) -> str:
    """
    Recover the connector's legacy report_id from
    ``parser_payload.report_metadata`` if present.
    """
    metadata = parser_payload.get("report_metadata")
    if isinstance(metadata, dict):
        report_id = metadata.get("report_id")
        if isinstance(report_id, str) and report_id.strip():
            return report_id.strip()
    return report_type


def _handle_live_produced_report(
    *,
    session: Session,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    report_month: str,
    triggered_by_user_id: UUID | None,
    run_entry: ConnectorRunEntry,
    backend: BlobStorageBackend,
    scheme: str,
    bucket: str,
    parser: YouTubeReportingParser | YouTubeAnalyticsParser | AdSenseManagementParser,
    repo: SqlAlchemyGoogleRevenueSourceRowRepository,
    produced: ProducedReport,
    ordering_index: int,
    counts: dict[str, int],
    per_report_failures: list[tuple[str, str]],
    per_report_failure_details: list[tuple[str, str, str | None]],
    deferred_analytics_cleanup: _DeferredAnalyticsStaleCleanupState | None,
    audit_sink: AuditSink,
    audit_actor: UserPrincipal,
) -> int:
    """Persist one produced report (raw upload, parse, upsert) and update counts."""
    report_type, parser_payload, raw_reports, produced_error = _unpack_produced_report(produced)
    counts["reports_attempted"] += 1
    report_state: dict[str, object] = {}
    try:
        if produced_error is not None:
            if raw_reports:
                _prepare_and_link_raw_reports(
                    context=_RawReportLinkContext(
                        session=session,
                        tenant_id=tenant_id,
                        run_entry=run_entry,
                        report=_RawReportDescriptor(
                            connector_key=connector_key,
                            source_system=source_system_for_connector(connector_key),
                            report_type=report_type,
                            report_month=report_month,
                        ),
                        storage=_RawReportStorageContext(
                            backend=backend,
                            scheme=scheme,
                            bucket=bucket,
                            triggered_by_user_id=triggered_by_user_id,
                        ),
                        ordering_index=ordering_index,
                        audit=_RawReportAuditContext(sink=audit_sink, actor=audit_actor),
                    ),
                    raw_reports=raw_reports,
                    report_state=report_state,
                )
            raise produced_error
        if parser_payload is None or raw_reports is None or not raw_reports:
            raise RuntimeError("connector runner yielded incomplete report")
        processed = _process_one_report(
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
            account_id=account_id,
            parser_payload=parser_payload,
            raw_reports=raw_reports,
            ordering_index=ordering_index,
            triggered_by_user_id=triggered_by_user_id,
            report_state=report_state,
            audit_sink=audit_sink,
            audit_actor=audit_actor,
        )
        session.commit()
        if deferred_analytics_cleanup is not None:
            _merge_deferred_stale_cleanup_plans(
                deferred_cleanup=deferred_analytics_cleanup,
                plans=processed.deferred_cleanup_plans,
                attempted_channel_id=_youtube_channel_id_from_parser_payload(parser_payload),
            )
        counts["reports_succeeded"] += 1
        counts["rows_upserted_total"] += processed.rows_total
        counts["rows_upserted_created"] += processed.rows_created
        counts["rows_upserted_updated"] += processed.rows_updated
        counts["rows_upserted_unchanged"] += processed.rows_unchanged
        counts["rows_deleted_stale"] += processed.rows_deleted_stale
        return processed.raw_file_count
    except Exception as exc:
        if deferred_analytics_cleanup is not None:
            deferred_analytics_cleanup.blocked = True
        _record_live_report_failure(
            session=session,
            tenant_id=tenant_id,
            report_type=report_type,
            report_state=report_state,
            exc=exc,
            counts=counts,
            per_report_failures=per_report_failures,
            per_report_failure_details=per_report_failure_details,
            run_entry=run_entry,
            audit_sink=audit_sink,
            audit_actor=audit_actor,
        )
        return _raw_file_count_from_state(report_state)


def _raw_file_count_from_state(report_state: dict[str, object]) -> int:
    """Return the number of raw_file ids attached to a per-report state entry."""
    raw_file_ids = report_state.get("raw_file_ids")
    if isinstance(raw_file_ids, list):
        return len(raw_file_ids)
    if report_state.get("raw_file_id") is not None:
        return 1
    return 0


def _record_live_report_failure(
    *,
    session: Session,
    tenant_id: UUID,
    report_type: str,
    report_state: dict[str, object],
    exc: Exception,
    counts: dict[str, int],
    per_report_failures: list[tuple[str, str]],
    per_report_failure_details: list[tuple[str, str, str | None]],
    run_entry: ConnectorRunEntry,
    audit_sink: AuditSink,
    audit_actor: UserPrincipal,
) -> None:
    """Persist a bucket-B per-report failure into ``connector_run_reports`` mid-run."""
    error_class = type(exc).__name__
    per_report_failures.append((report_type, error_class))
    per_report_failure_details.append((report_type, error_class, _safe_failure_detail(exc)))
    counts["reports_failed"] += 1
    in_flight_raw_file_ids = _raw_file_ids_from_state(report_state)
    if not in_flight_raw_file_ids:
        session.rollback()
        return
    try:
        for raw_file_id in in_flight_raw_file_ids:
            raw_file = session.get(RawReportFileORM, raw_file_id)
            if raw_file is None or raw_file.tenant_id != tenant_id:
                continue
            if raw_file.parse_status == "PARSED":
                continue
            mark_failed(
                session,
                raw_file_id=raw_file_id,
                tenant_id=tenant_id,
            )
            # Spec §8.4: each per-raw-file FAILED edge is staged in the same
            # transaction as the lifecycle write. The audit row commits with
            # the mark_failed row below so a process crash between the two
            # cannot leave a FAILED raw_file without its audit row.
            emit_raw_file_failed(
                sink=audit_sink,
                actor=audit_actor,
                run=run_entry,
                raw_file=raw_file,
                error_class=error_class,
            )
        session.commit()
    except Exception:
        # FIX: the swallow here previously logged nothing, against the repo's
        # error-handling rules -- a lane regression (e.g. the FAILED audit edge
        # permission-denying on the wrong DB role) would silently drop the
        # per-report FAILED state and the run would still terminate PARTIAL with
        # no diagnostic trail. Log before swallowing (run/report identifiers
        # only -- no secret values, no raw SQL values). Control flow is
        # unchanged on purpose: the run MUST still terminate PARTIAL.
        logger.exception(
            "Failed to persist per-report failure state for "
            "run_id=%s report_type=%s; rolling back the failure write.",
            run_entry.id,
            report_type,
        )
        session.rollback()


def _raw_file_ids_from_state(report_state: dict[str, object]) -> list[UUID]:
    """Return the list of attached raw_file UUIDs from a per-report state entry."""
    raw_file_ids = report_state.get("raw_file_ids")
    if isinstance(raw_file_ids, list):
        return [raw_file_id for raw_file_id in raw_file_ids if isinstance(raw_file_id, UUID)]
    raw_file_id = report_state.get("raw_file_id")
    if isinstance(raw_file_id, UUID):
        return [raw_file_id]
    return []


def _finish_failed_live_run(
    *,
    session: Session,
    tenant_id: UUID,
    run_entry: ConnectorRunEntry,
    counts: dict[str, int],
    exc: Exception,
    audit_sink: AuditSink,
    audit_actor: UserPrincipal,
) -> ConnectorRunEntry:
    """Close a live run as FAILED (bucket-A short-circuit) with the supplied error class."""
    session.rollback()
    # FIX: the FINISHED edge writes audit_logs (TENANT_PLATFORM_ONLY_WRITE);
    # elevate the finish_run + emit + commit so the terminal write does not
    # permission-deny on the tenant lane. The rollback above is left outside
    # the block so the elevation wraps a clean new transaction.
    with platform_lane(session):
        finished_run = finish_run(
            session,
            tenant_id=tenant_id,
            connector_run_id=UUID(run_entry.id),
            status="FAILED",
            counts=counts,
            error_summary=f"{type(exc).__name__}: {exc!s}",
        )
        # Spec §8.4: CONNECTOR_JOB_RUN/FINISHED is committed with finish_run so
        # the terminal state and its audit row share one transaction.
        emit_run_finished(sink=audit_sink, actor=audit_actor, run=finished_run)
        session.commit()
    return finished_run


def _finish_aggregate_live_run(
    *,
    session: Session,
    tenant_id: UUID,
    run_entry: ConnectorRunEntry,
    counts: dict[str, int],
    per_report_failure_details: list[tuple[str, str, str | None]],
    audit_sink: AuditSink,
    audit_actor: UserPrincipal,
) -> ConnectorRunEntry:
    """Close a live run as SUCCEEDED/PARTIAL based on the aggregated per-report counts."""
    status = _derive_terminal_status(counts)
    # FIX: elevate the finish_run + FINISHED emit + commit; the audit_logs write
    # is platform-only and would permission-deny on the tenant lane. No-op off
    # Postgres.
    with platform_lane(session):
        finished_run = finish_run(
            session,
            tenant_id=tenant_id,
            connector_run_id=UUID(run_entry.id),
            status=status,
            counts=counts,
            error_summary=_summarize_failures(per_report_failure_details),
        )
        # Spec §8.4: CONNECTOR_JOB_RUN/FINISHED commits with finish_run.
        emit_run_finished(sink=audit_sink, actor=audit_actor, run=finished_run)
        session.commit()
    return finished_run


def _sweep_unfinished_live_run(
    *,
    session: Session,
    tenant_id: UUID,
    run_entry: ConnectorRunEntry,
    counts: dict[str, int],
    audit_sink: AuditSink,
    audit_actor: UserPrincipal,
) -> None:
    """Best-effort sweep for the no-credentials early-out.

    Finishes any still-open ``run_entry`` left behind by the abort path.
    """
    session.rollback()
    try:
        # FIX: elevate the rescue finish + FINISHED emit + commit; audit_logs is
        # platform-only. A denial here would otherwise be swallowed by the
        # best-effort except, silently losing the lifecycle terminus on PG.
        with platform_lane(session):
            finished_run = finish_run(
                session,
                tenant_id=tenant_id,
                connector_run_id=UUID(run_entry.id),
                status="FAILED",
                counts=counts,
                error_summary="orchestrator aborted unexpectedly",
            )
            # Best-effort: even the fail-safe sweep emits FINISHED so the audit
            # trail records the lifecycle terminus. A sink error here is
            # swallowed by the outer except so the sweep stays best-effort.
            emit_run_finished(sink=audit_sink, actor=audit_actor, run=finished_run)
            session.commit()
    except Exception:
        session.rollback()


# ----------------------------------------------------------------------------
# Per-report inner block
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProcessedReportResult:
    """Per-report source-row counts and raw-file side effects."""

    # ============================================================================
    # Purpose: Carry per-report row-write outcomes from ``_process_one_report``
    #          back to ``_handle_live_produced_report`` so the caller can update
    #          every key in ``connector_runs.counts_json`` without unpacking a
    #          long anonymous tuple. The four upsert ints feed
    #          ``rows_upserted_total / _created / _updated / _unchanged`` and
    #          ``rows_deleted_stale`` records successful replacement cleanup.
    # Database/ORM: None directly; sourced from the repository's
    #               ``SourceRowUpsertResult`` for this report.
    # Standards: Sum invariant — rows_total == rows_created + rows_updated +
    #            rows_unchanged.
    # Blast Radius: connector_runs.counts_json accuracy only.
    # ============================================================================
    rows_total: int
    rows_created: int
    rows_updated: int
    rows_unchanged: int
    rows_deleted_stale: int
    raw_file_count: int
    deferred_cleanup_plans: tuple[_DeferredStaleCleanupPlan, ...]


def _process_one_report(
    *,
    session: Session,
    tenant_id: UUID,
    connector_key: str,
    run_entry: ConnectorRunEntry,
    backend: BlobStorageBackend,
    scheme: str,
    bucket: str,
    parser: YouTubeReportingParser | YouTubeAnalyticsParser | AdSenseManagementParser,
    repo: SqlAlchemyGoogleRevenueSourceRowRepository,
    report_type: str,
    report_month: str,
    account_id: str,
    parser_payload: dict[str, object],
    raw_reports: tuple[_CsvReportDownload, ...],
    ordering_index: int,
    triggered_by_user_id: UUID | None,
    report_state: dict[str, object],
    audit_sink: AuditSink,
    audit_actor: UserPrincipal,
) -> _ProcessedReportResult:
    """Run one report through blob -> raw_file -> parse -> upsert -> mark_parsed.

    Raises any ``GoogleConnectorError`` / ``ParserError`` / other exception
    from the blob / lifecycle / parser / repo so the outer per-report
    ``except`` in ``run_one`` can record the failure without aborting the
    whole run.

    ``report_state`` is a mutable handshake dict the caller owns. As each
    ``raw_report_files`` row is flushed, this function appends its id to
    ``report_state["raw_file_ids"]`` so the caller's bucket-B handler can
    mark every DOWNLOADED evidence row FAILED if a later step raises.
    Failures BEFORE the first flush leave the key absent, signalling "no
    raw_file in flight to mark FAILED".
    """
    source_system = source_system_for_connector(connector_key)
    deferred_cleanup_plans: tuple[_DeferredStaleCleanupPlan, ...] = ()
    rows_deleted_stale = 0
    raw_files = _prepare_and_link_raw_reports(
        context=_RawReportLinkContext(
            session=session,
            tenant_id=tenant_id,
            run_entry=run_entry,
            report=_RawReportDescriptor(
                connector_key=connector_key,
                source_system=source_system,
                report_type=report_type,
                report_month=report_month,
            ),
            storage=_RawReportStorageContext(
                backend=backend,
                scheme=scheme,
                bucket=bucket,
                triggered_by_user_id=triggered_by_user_id,
            ),
            ordering_index=ordering_index,
            audit=_RawReportAuditContext(sink=audit_sink, actor=audit_actor),
        ),
        raw_reports=raw_reports,
        report_state=report_state,
    )

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
        source_row_raw_file_id = _source_row_raw_file_id(raw_files)

        # Upsert. Returns a SourceRowUpsertResult with the persisted entries
        # plus the per-row classification counts (created / updated /
        # unchanged) the caller copies into ``connector_runs.counts_json``.
        upsert_result = repo.upsert_many(
            tenant_id,
            parsed_rows,
            raw_file_id=source_row_raw_file_id,
            imported_by=triggered_by_user_id,
            replace_raw_file_id=source_row_raw_file_id is None,
        )
        source_report_types = _fallback_source_report_types(
            parser=parser,
            default_report_type=report_type,
        )
        fallback_source_account_id = _fallback_source_account_id(
            parser_payload=parser_payload,
            default_account_id=account_id,
        )
        if isinstance(parser, YouTubeAnalyticsParser):
            # FIX: targeted analytics replaces ONE content-owner/month scope
            # across many per-channel payloads. Deleting stale rows here, one
            # channel at a time, lets later successes (or empty responses) erase
            # sibling channels and lets partial runs drop rows for failed
            # channels. Defer cleanup until the full owner-month key set is
            # known and only flush it when every channel payload in the run
            # succeeded.
            deferred_cleanup_plans = _build_deferred_stale_cleanup_plans(
                source_system=source_system,
                report_types=source_report_types,
                report_month=report_month,
                parsed_rows=parsed_rows,
                fallback_source_account_id=fallback_source_account_id,
            )
        else:
            rows_deleted_stale = _delete_stale_source_rows(
                repo=repo,
                tenant_id=tenant_id,
                source_system=source_system,
                report_types=source_report_types,
                report_month=report_month,
                parsed_rows=parsed_rows,
                fallback_source_account_id=fallback_source_account_id,
            )

        # Lifecycle transition: DOWNLOADED -> PARSED. Raises
        # ``RawFileAlreadyParsedError`` if called twice on the same file, which
        # would only happen on a re-entrant orchestrator bug.
        # Spec §8.4: each per-raw-file PARSED audit row is staged inside the
        # main transaction so the audit edge commits with the mark_parsed
        # write (and the upserted source rows) via the outer commit in
        # ``_handle_live_produced_report``. The audit row is only emitted
        # when the raw_file actually transitions on this run, mirroring the
        # idempotent-rerun guard above.
        for raw_file in raw_files:
            if raw_file.parse_status != "PARSED":
                mark_parsed(session, raw_file_id=raw_file.id, tenant_id=tenant_id)
                emit_raw_file_parsed(
                    sink=audit_sink,
                    actor=audit_actor,
                    run=run_entry,
                    raw_file=raw_file,
                    # Report-level total; see emit_raw_file_parsed docstring re:
                    # multi-raw-file aggregation contract.
                    count_upserted=len(upsert_result.entries),
                )

    # ``counts["reports_succeeded"] += 1`` lives in ``run_one``'s outer
    # loop AFTER ``session.commit()`` so a commit failure (e.g. DB
    # disconnect on the per-report flush) is recorded as a failure once,
    # not double-counted as both succeeded and failed.
    return _ProcessedReportResult(
        rows_total=len(upsert_result.entries),
        rows_created=upsert_result.created,
        rows_updated=upsert_result.updated,
        rows_unchanged=upsert_result.unchanged,
        rows_deleted_stale=rows_deleted_stale,
        raw_file_count=len(raw_files),
        deferred_cleanup_plans=deferred_cleanup_plans,
    )


# ============================================================================
# Purpose: Choose the source-row raw-file FK when exactly one persisted raw CSV
#          supports the parsed monthly row set.
# Database/ORM: RawReportFileORM ids only.
# Standards: Multi-file monthly aggregates intentionally return None so source
#            rows do not imply one raw file contains the whole aggregate.
# Blast Radius: Raw evidence lineage only; finance values stay in PostgreSQL
#               source rows and no graph projection impact is detected.
# Connections:
#   - Function: _process_one_report -> passes this value to source-row upsert.
#   - File: backend/ums_smart_revenue/connectors/google_source_rows/repository.py
#     -> optionally clears stale raw_file_id on aggregate replacement.
# ============================================================================
def _source_row_raw_file_id(raw_files: list[RawReportFileORM]) -> UUID | None:
    """Return the single raw-file id when a row aggregates exactly one raw payload."""
    if len(raw_files) == 1:
        return raw_files[0].id
    return None


# ============================================================================
# Purpose: Build the stale-row cleanup scopes implied by one successful parsed
#          replacement payload.
# Database/ORM: None directly; the returned plans scope later repository
#               deletes.
# Standards: Groups by parser-owned report_type + source_account_id and keeps
#            empty/partial replacement cleanup aligned with the persisted
#            analytics scope.
# Blast Radius: Source-of-truth source-row cleanup only.
# Connections:
#   - Function: _process_one_report -> defers analytics cleanup until the owner
#     scope is complete.
#   - Function: _delete_stale_source_rows -> reuses the same scope grouping for
#     immediate cleanup paths.
# ============================================================================
def _stale_source_row_keys_by_scope(
    *,
    report_types: Iterable[str],
    parsed_rows: Iterable[ParsedSourceRow],
    fallback_source_account_id: str,
) -> dict[tuple[str, str], set[str]]:
    """Group keep-keys by (report_type, source_account_id) for stale-row cleanup."""
    keys_by_scope: dict[tuple[str, str], set[str]] = {}
    for row in parsed_rows:
        keys_by_scope.setdefault((row.report_type, row.source_account_id), set()).add(
            row.source_row_key
        )
    normalized_report_types = tuple(
        report_type.strip() for report_type in report_types if report_type.strip()
    )
    source_account_id = fallback_source_account_id.strip()
    if source_account_id and (not keys_by_scope or len(normalized_report_types) > 1):
        # FIX: successful replacements need every PARSER-LEVEL report_type/account
        # scope, not only the scopes represented by newly parsed rows. AdSense
        # can replace a prior payment_report with an earnings-only payload; if
        # payment_report is not seeded with an empty keep-set, stale settled rows
        # survive the rerun.
        for normalized_report_type in normalized_report_types:
            keys_by_scope.setdefault((normalized_report_type, source_account_id), set())
    return keys_by_scope


# ============================================================================
# Purpose: Convert one report's stale-row cleanup scopes into deferred plans the
#          analytics run can merge across sibling channels.
# Database/ORM: None directly; plans are flushed later through the repository.
# Standards: Keeps per-scope keys additive so the final delete runs once per
#            owner/month after all successful channel payloads have contributed.
# Blast Radius: Source-of-truth source-row cleanup only.
# Connections:
#   - Function: _process_one_report -> uses for YouTubeAnalyticsParser only.
#   - Function: _flush_deferred_stale_cleanup_plans -> executes the merged plans.
# ============================================================================
def _build_deferred_stale_cleanup_plans(
    *,
    source_system: str,
    report_types: tuple[str, ...],
    report_month: str,
    parsed_rows: Iterable[ParsedSourceRow],
    fallback_source_account_id: str,
) -> tuple[_DeferredStaleCleanupPlan, ...]:
    """Convert one channel's keep-keys into deferred plans for the analytics flush."""
    return tuple(
        _DeferredStaleCleanupPlan(
            source_system=source_system,
            report_type=row_report_type,
            report_month=report_month,
            source_account_id=source_account_id,
            keep_source_row_keys=frozenset(keys),
        )
        for (row_report_type, source_account_id), keys in _stale_source_row_keys_by_scope(
            report_types=report_types,
            parsed_rows=parsed_rows,
            fallback_source_account_id=fallback_source_account_id,
        ).items()
    )


def _merge_deferred_stale_cleanup_plans(
    *,
    deferred_cleanup: _DeferredAnalyticsStaleCleanupState,
    plans: tuple[_DeferredStaleCleanupPlan, ...],
    attempted_channel_id: str | None = None,
) -> None:
    """Aggregate one channel's plans into the run-level deferred-cleanup state.

    ``attempted_channel_id`` is the youtube_channel_id whose parser payload
    contributed these plans; flush uses this set to avoid deleting historical
    rows for channels outside the current target set.
    """
    for plan in plans:
        deferred_cleanup.keep_source_row_keys_by_scope.setdefault(
            (
                plan.source_system,
                plan.report_type,
                plan.report_month,
                plan.source_account_id,
            ),
            set(),
        ).update(plan.keep_source_row_keys)
    if attempted_channel_id is not None and attempted_channel_id.strip():
        deferred_cleanup.attempted_channel_ids.add(attempted_channel_id.strip())


def _flush_deferred_stale_cleanup_plans(
    *,
    repo: SqlAlchemyGoogleRevenueSourceRowRepository,
    tenant_id: UUID,
    deferred_cleanup: _DeferredAnalyticsStaleCleanupState,
) -> int:
    """Execute the merged keep-key plans once the analytics run completes cleanly.

    Rows whose ``youtube_channel_id`` is NOT in ``attempted_channel_ids`` are
    preserved by augmenting ``keep_source_row_keys`` with their persisted
    ``source_row_key``. This prevents a channel that fell out of the target
    set (deactivated, ``revenue_required=False``, etc.) from losing its
    historical revenue when sibling channels are successfully replaced.

    Returns:
        The total number of stale source rows deleted across all deferred
        scopes. The orchestrator copies this value into
        ``counts["rows_deleted_stale"]`` so the post-run normalizer can
        decide whether the month needs re-projection.
    """
    if deferred_cleanup.blocked:
        return 0
    attempted = deferred_cleanup.attempted_channel_ids
    rows_deleted_stale = 0
    # FIX: cache repo.list() per (source_system, report_month). Multiple scopes
    # in a single run can share the same (source_system, report_month) but
    # different source_account_id, so hoisting the fetch keeps the flush at one
    # query per source/month even on multi-owner batches.
    existing_rows_cache: dict[tuple[str, str], list] = {}
    for (
        source_system,
        report_type,
        report_month,
        source_account_id,
    ), keys in deferred_cleanup.keep_source_row_keys_by_scope.items():
        preserved_keys = set(keys)
        cache_key = (source_system, report_month)
        if cache_key not in existing_rows_cache:
            existing_rows_cache[cache_key] = repo.list(
                tenant_id,
                report_month=report_month,
                source_system=source_system,
            )
        for row in existing_rows_cache[cache_key]:
            if (
                row.source_account_id == source_account_id
                and row.report_type == report_type
                and row.youtube_channel_id is not None
                and row.youtube_channel_id not in attempted
            ):
                preserved_keys.add(row.source_row_key)
        rows_deleted_stale += repo.delete_stale_for_scope(
            tenant_id,
            source_system=source_system,
            source_account_id=source_account_id,
            report_type=report_type,
            report_month=report_month,
            keep_source_row_keys=preserved_keys,
        )
    return rows_deleted_stale


# ============================================================================
# Purpose: Map the outer produced-report label to source-row report_type values
#          used for stale-row cleanup when a successful replacement yields no
#          parsed rows.
# Database/ORM: None directly; the returned value scopes repository deletes.
# Standards: Keeps the generic orchestrator aware of parser-owned report_type
#            normalization without hardcoding row writes here.
# Blast Radius: Source-of-truth stale-row deletion scope only.
# Connections:
#   - Function: _process_one_report -> provides fallback report_type scopes for
#     _delete_stale_source_rows.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     youtube_analytics.py -> emits ParsedSourceRow.report_type="reports.query".
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     adsense_management.py -> emits earnings_report and payment_report rows.
# ============================================================================
def _fallback_source_report_types(
    *,
    parser: YouTubeReportingParser | YouTubeAnalyticsParser | AdSenseManagementParser,
    default_report_type: str,
) -> tuple[str, ...]:
    """Return persisted report_type labels for empty-success stale cleanup."""
    if isinstance(parser, YouTubeAnalyticsParser):
        return ("reports.query",)
    if isinstance(parser, AdSenseManagementParser):
        return ("earnings_report", "payment_report")
    return (default_report_type,)


# ============================================================================
# Purpose: Extract the targeted youtube_channel_id from a parser payload's
#          query_request.filters string (``channel==<id>``). Used by the
#          analytics deferred-cleanup flush to record which channels were
#          attempted in this run so historical rows for non-attempted channels
#          can be preserved on flush.
# Database/ORM: None.
# Standards: Returns None when the filter is missing, non-string, malformed,
#            or the channel value is empty/whitespace so callers can ignore
#            unattributable payloads instead of polluting the attempted-set
#            with bad data.
# Blast Radius: Source-of-truth source-row cleanup only. A drift would either
#               erase rows for previously-active channels or leak stale rows.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/
#     youtube_analytics_client.py -> the canonical filters shape this helper
#     parses.
# ============================================================================
def _youtube_channel_id_from_parser_payload(
    parser_payload: dict[str, object],
) -> str | None:
    """Return the channel_id from a parser payload's ``filters=channel==<id>``."""
    query_request = parser_payload.get("query_request")
    if not isinstance(query_request, dict):
        return None
    filters = query_request.get("filters")
    if not isinstance(filters, str):
        return None
    for clause in filters.split(";"):
        key, sep, value = clause.partition("==")
        if sep != "==" or key.strip() != "channel":
            continue
        channel_id = value.strip()
        return channel_id if channel_id else None
    return None


# ============================================================================
# Purpose: Reuse the parsed payload's canonical account selector for stale-row
#          cleanup when a successful replacement report omits one or more
#          parser-level scopes.
# Database/ORM: None directly; the returned value scopes repository deletes.
# Standards: Prefer parser-owned canonical ids over external run selectors so
#            cleanup targets persisted source_row dimensions.
# Blast Radius: Source-of-truth stale-row deletion scope only. A mismatch here
#               can leave obsolete finance rows behind after a replacement run.
# Connections:
#   - Function: _process_one_report -> passes the result to
#     _delete_stale_source_rows after parser success.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     youtube_analytics.py -> source_account_id matches query_request.ids.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     adsense_management.py -> source_account_id strips request.accountId's
#     accounts/ prefix.
# ============================================================================
def _fallback_source_account_id(
    *,
    parser_payload: dict[str, object],
    default_account_id: str,
) -> str:
    """Return the canonical source_account_id used by the parser for cleanup scope."""
    query_request = parser_payload.get("query_request")
    if isinstance(query_request, dict):
        ids = query_request.get("ids")
        if isinstance(ids, str) and ids.strip():
            return ids.strip()
    request = parser_payload.get("request")
    if isinstance(request, dict):
        request_account_id = request.get("accountId")
        if isinstance(request_account_id, str):
            adsense_account_id = request_account_id.strip()
            if adsense_account_id.startswith("accounts/"):
                adsense_account_id = adsense_account_id.removeprefix("accounts/").strip()
                if adsense_account_id:
                    # FIX: AdSense can be invoked with a full
                    # ``accounts/<id>`` run selector, while the parser persists
                    # the stripped suffix. Cleanup must use the persisted axis
                    # or stale payment/earnings evidence survives reruns.
                    return adsense_account_id
    return default_account_id


# ============================================================================
# Purpose: Delete source rows that existed for a report scope but disappeared
#          from the successful replacement payload.
# Database/ORM: GoogleRevenueSourceRowORM via repository delete.
# Standards: Tenant/account/month/type scoped cleanup after parser success only;
#            no stale-row deletion occurs for failed or partial aggregates.
# Blast Radius: Source-of-truth source rows for the connector scope. No graph
#               projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google_source_rows/repository.py
#     -> delete_stale_for_scope enforces tenant and report-month validation.
# ============================================================================
def _delete_stale_source_rows(
    *,
    repo: SqlAlchemyGoogleRevenueSourceRowRepository,
    tenant_id: UUID,
    source_system: str,
    report_types: tuple[str, ...],
    report_month: str,
    parsed_rows: Iterable[ParsedSourceRow],
    fallback_source_account_id: str,
) -> int:
    """Delete source rows in each scope whose keys are absent from the new parse."""
    rows_deleted_stale = 0
    for (row_report_type, source_account_id), keys in _stale_source_row_keys_by_scope(
        report_types=report_types,
        parsed_rows=parsed_rows,
        fallback_source_account_id=fallback_source_account_id,
    ).items():
        rows_deleted_stale += repo.delete_stale_for_scope(
            tenant_id,
            source_system=source_system,
            source_account_id=source_account_id,
            report_type=row_report_type,
            report_month=report_month,
            keep_source_row_keys=keys,
        )
    return rows_deleted_stale


@dataclass(frozen=True)
class _RawReportDescriptor:
    connector_key: str
    source_system: str
    report_type: str
    report_month: str


@dataclass(frozen=True)
class _RawReportStorageContext:
    backend: BlobStorageBackend
    scheme: str
    bucket: str
    triggered_by_user_id: UUID | None


@dataclass(frozen=True)
class _RawReportAuditContext:
    sink: AuditSink
    actor: UserPrincipal


@dataclass(frozen=True)
class _RawReportLinkContext:
    session: Session
    tenant_id: UUID
    run_entry: ConnectorRunEntry
    report: _RawReportDescriptor
    storage: _RawReportStorageContext
    ordering_index: int
    audit: _RawReportAuditContext


# ============================================================================
# Purpose: Persist and link every raw CSV evidence file yielded for one logical
#          report before parsing/upsert work begins.
# Database/ORM: RawReportFileORM and ConnectorRunRawFileORM.
# Standards: Deduplicates raw files within one logical report, records ids in
#            report_state for bucket-B failure marking, and preserves ordering.
# Blast Radius: Raw evidence/run linkage only; finance rows are written later
#               and no graph projection impact is detected.
# Connections:
#   - Function: _handle_live_produced_report -> uses report_state on failures.
#   - Function: _prepare_raw_report_file -> uploads/verifies each raw payload.
# ============================================================================
def _prepare_and_link_raw_reports(
    *,
    context: _RawReportLinkContext,
    raw_reports: tuple[_CsvReportDownload, ...],
    report_state: dict[str, object],
) -> list[RawReportFileORM]:
    """Upload each ``raw_report`` and link raw_file rows to the active report."""
    raw_files: list[RawReportFileORM] = []
    seen_raw_file_ids: set[UUID] = set()
    newly_downloaded_raw_file_ids: set[UUID] = set()
    raw_file_ids: list[UUID] = []
    report_state["raw_file_ids"] = raw_file_ids

    for raw_report in raw_reports:
        raw_file, newly_downloaded = _prepare_raw_report_file(
            session=context.session,
            tenant_id=context.tenant_id,
            connector_key=context.report.connector_key,
            source_system=context.report.source_system,
            report_type=context.report.report_type,
            report_month=context.report.report_month,
            raw_bytes=raw_report.read_bytes(),
            backend=context.storage.backend,
            scheme=context.storage.scheme,
            bucket=context.storage.bucket,
            triggered_by_user_id=context.storage.triggered_by_user_id,
        )
        if raw_file.id in seen_raw_file_ids:
            continue
        seen_raw_file_ids.add(raw_file.id)
        if newly_downloaded:
            newly_downloaded_raw_file_ids.add(raw_file.id)
        raw_files.append(raw_file)
        raw_file_ids.append(raw_file.id)
        report_state.setdefault("raw_file_id", raw_file.id)

    if not raw_files:
        raise RuntimeError("connector runner yielded no raw report files")

    for offset, raw_file in enumerate(raw_files):
        link_raw_file(
            context.session,
            tenant_id=context.tenant_id,
            connector_run_id=UUID(context.run_entry.id),
            raw_report_file_id=raw_file.id,
            ordering_index=context.ordering_index + offset,
        )
        # Spec §8.4: stage the DOWNLOADED audit edge in the main transaction
        # alongside the link join, so the audit row commits with its evidence.
        # Only fresh inserts / FAILED-reopened rows emit DOWNLOADED -- an
        # idempotent reuse of a still-DOWNLOADED or already-PARSED raw_file
        # is not a new download lifecycle edge.
        #
        # Important: this emit happens OUTSIDE the per-report begin_nested()
        # savepoint (see _process_one_report below). When a per-report parse
        # raises, the savepoint rollback drops the upsert+mark_parsed work but
        # the DOWNLOADED audit row stays staged in the outer transaction, where
        # _record_live_report_failure then commits it alongside FAILED. This
        # is intentional: the audit trail must record "we landed bytes" even
        # if parsing fails afterward.
        if raw_file.id in newly_downloaded_raw_file_ids:
            emit_raw_file_downloaded(
                sink=context.audit.sink,
                actor=context.audit.actor,
                run=context.run_entry,
                raw_file=raw_file,
            )
    return raw_files


# ============================================================================
# Purpose: Persist one downloaded Google CSV as raw evidence before parsing.
# Database/ORM: RawReportFileORM.
# Standards: Blob round-trip is verified before DB evidence is created;
#            tenant-scoped raw-file reuse preserves idempotent retries.
# Blast Radius: Raw evidence and connector-run linkage only. Finance rows are
#               written later through the source-row repository; no graph
#               projection impact detected.
# Connections:
#   - Function: _process_one_report -> caller that links/parses/upserts.
#   - File: backend/ums_smart_revenue/connectors/runs/blob_storage.py ->
#     deterministic_blob_path / upload_and_verify / compute_checksum.
# ============================================================================
def _prepare_raw_report_file(
    *,
    session: Session,
    tenant_id: UUID,
    connector_key: str,
    source_system: str,
    report_type: str,
    report_month: str,
    raw_bytes: bytes,
    backend: BlobStorageBackend,
    scheme: str,
    bucket: str,
    triggered_by_user_id: UUID | None,
) -> tuple[RawReportFileORM, bool]:
    """Compute checksum, upload, and create/reopen the raw_file row for one CSV download.

    Returns ``(raw_file, newly_downloaded)``. ``newly_downloaded`` is True when
    the raw_file row was freshly inserted by this call or transitioned from
    FAILED -> DOWNLOADED (a retried payload), which are the two cases that
    represent a new DOWNLOADED lifecycle edge for the audit log. A reused
    PARSED or still-DOWNLOADED row is NOT a new edge and returns False so the
    caller skips the DOWNLOADED audit emit.
    """
    # 1. Checksum + deterministic URI: same bytes always map to the same path
    # so a retry overwrites or hits the existing object instead of creating a
    # second copy with the same content.
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
    # transaction. ``created_now`` is returned by the insert helper so the
    # audit lifecycle does not mistake a unique-key race winner for a fresh
    # download owned by this run.
    raw_file, created_now = _get_or_create_raw_file(
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
    failed_reopen = raw_file.parse_status == "FAILED"
    if failed_reopen:
        # FIX: a retry of the same Google payload must reopen the evidence
        # row before parsing and point it at the blob uploaded by this run.
        raw_file.parse_status = "DOWNLOADED"
        raw_file.file_url = storage_uri
        raw_file.downloaded_by = triggered_by_user_id
        session.flush()
    # Audit lifecycle: a NEW DOWNLOADED edge exists when the row was just
    # inserted (no pre-existing match) or when a previously-FAILED retry was
    # reopened. Reuse of a still-DOWNLOADED or already-PARSED row is not a
    # new download edge for audit purposes.
    newly_downloaded = created_now or failed_reopen
    return raw_file, newly_downloaded


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
    """Look up an existing raw_file row for an idempotent retry under the same scope+checksum."""
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
# Purpose: Create or reuse the raw report evidence row idempotently, returning
#          whether this call inserted the row. This includes the lookup/insert
#          race where another worker commits the same tenant/source/report/
#          month/checksum after our pre-insert lookup.
# Database/ORM: RawReportFileORM insert/read; uniqueness enforced by
#               uq_raw_report_files_source_type_month_checksum.
# Standards: SQLAlchemy savepoint contains the duplicate insert failure; no
#            broad rollback so the surrounding connector_runs transaction
#            remains usable. The returned bool drives DOWNLOADED audit
#            emission; typed lifecycle checks stay in the caller.
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
) -> tuple[RawReportFileORM, bool]:
    """Return ``(raw_file, created_now)`` while tolerating unique-key races."""
    raw_file = _find_existing_raw_file(
        session,
        tenant_id=tenant_id,
        source=source,
        report_type=report_type,
        report_month=report_month,
        checksum=checksum,
    )
    if raw_file is not None:
        return raw_file, False

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
            return raw_file, True
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
        return raw_file, False


# ----------------------------------------------------------------------------
# Connector runner: YouTube Reporting
# ----------------------------------------------------------------------------


class YouTubeReportingRunner:
    """B2.4 adapter that walks YouTube Reporting jobs/reports for one month.

    Each yielded success carries the report type, the parser-friendly monthly
    payload, and the exact downloaded Google CSV files that support that
    aggregate. The orchestrator stores each raw CSV separately; it never
    persists a synthetic monthly evidence bundle.

    The class references ``YouTubeReportingClient`` by bare name so tests
    that patch
    ``ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient``
    replace what the runner actually uses.
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
        """
        Drive the YouTube Reporting jobs path: discover jobs, fetch CSVs,
        yield per-job results.
        """
        # ``run`` is ``None`` on the T29 dry-run path; the runner body
        # never references it (the connector_runs lifecycle is owned by
        # ``run_one`` itself), so the widening is a pure type contract
        # change with no behavioural effect on the live path.
        _ = (session, run)
        client_type = getattr(self, "_client_type", YouTubeReportingClient)
        yield from _produce_youtube_reports(
            client_type=client_type,
            credentials=credentials,
            report_month=report_month,
            account_id=account_id,
        )


def _produce_youtube_reports(
    *,
    client_type: type[YouTubeReportingClient],
    credentials: Credentials,
    report_month: str,
    account_id: str,
) -> Iterator[ProducedReport]:
    """
    Iterate YouTube Reporting jobs, fetch each report's CSVs, and yield
    bucket-B successes/failures.
    """
    http = GoogleHttpClient(credentials=credentials)
    try:
        client = client_type(http=http)
        jobs = _deduplicate_youtube_jobs_by_report_type(
            client.list_supported_jobs(account_id=account_id)
        )
        for job in jobs:
            produced = _produce_youtube_job_report(
                client=client,
                job=job,
                report_month=report_month,
                account_id=account_id,
            )
            if produced is None:
                continue
            if isinstance(produced, ProducedReportFailure):
                try:
                    yield produced
                finally:
                    _cleanup_csv_report_downloads(produced.raw_reports)
                continue
            try:
                yield produced
            finally:
                _cleanup_csv_report_downloads(produced.raw_reports)
    finally:
        http.close()


# ============================================================================
# Purpose: Keep one YouTube Reporting job per report_type_id before report
#          listing/downloading.
# Database/ORM: None.
# Standards: Deterministic first-job-wins behavior prevents double ingestion
#            when Google exposes duplicate jobs for the same report type.
# Blast Radius: Connector API calls and raw evidence volume only. Finance rows
#               remain sourced from the single selected report type payload.
# Connections:
#   - Function: _produce_youtube_reports -> applies this before job iteration.
#   - File: backend/ums_smart_revenue/connectors/google/youtube_reporting_client.py
#     -> supplies validated job descriptor dictionaries.
# ============================================================================
def _deduplicate_youtube_jobs_by_report_type(
    jobs: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    """Collapse multiple jobs publishing the same report_type to the first occurrence."""
    seen_report_types: set[str] = set()
    unique_jobs: list[dict[str, object]] = []
    for job in jobs:
        report_type = _require_text(job, "reportTypeId")
        if report_type in seen_report_types:
            continue
        seen_report_types.add(report_type)
        unique_jobs.append(job)
    return unique_jobs


def _produce_youtube_job_report(
    *,
    client: YouTubeReportingClient,
    job: dict[str, object],
    report_month: str,
    account_id: str,
) -> ProducedReportSuccess | ProducedReportFailure | None:
    """
    Drive one YouTube Reporting job: list its reports for the month
    then download+aggregate them.
    """
    report_type = _require_text(job, "reportTypeId")
    job_id = _require_text(job, "id")
    reports = _list_youtube_reports_for_job(
        client=client,
        account_id=account_id,
        job_id=job_id,
        report_type=report_type,
        report_month=report_month,
    )
    if isinstance(reports, ProducedReportFailure):
        return reports
    if not reports:
        return _missing_youtube_report_failure(
            report_type=report_type,
            report_month=report_month,
        )
    return _build_youtube_report_success(
        client=client,
        reports=reports,
        report_type=report_type,
        report_month=report_month,
        account_id=account_id,
    )


def _list_youtube_reports_for_job(
    *,
    client: YouTubeReportingClient,
    account_id: str,
    job_id: str,
    report_type: str,
    report_month: str,
) -> list[dict[str, object]] | ProducedReportFailure:
    """Fetch the report metadata list for a single job in a single ``report_month``."""
    try:
        return client.list_reports_for_month(
            account_id=account_id,
            job_id=job_id,
            report_month=report_month,
        )
    except OAuthRefreshError:
        raise
    except GoogleConnectorError as exc:
        return ProducedReportFailure(report_type=report_type, error=exc)


# ============================================================================
# Purpose: Convert an empty YouTube Reporting report list into an explicit
#   per-report failure so a configured job that produces no monthly report is
#   visible in connector run status and downstream smart-alert audit signals.
# Database/ORM: None.
# Standards: Typed GoogleApiResponseError with a sanitized synthetic URL; no
#   report listing URL, credential, or upstream payload is exposed.
# Blast Radius: YouTube Reporting connector run status only. No finance rows,
#   raw files, or parser behavior change because there is no report to ingest.
# Connections:
#   - Function: _produce_youtube_job_report -> calls this before parser handoff.
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> failed runs
#     emitted by the live path can surface as CONNECTOR_RUNS_FAILED.
# ============================================================================
def _missing_youtube_report_failure(
    *,
    report_type: str,
    report_month: str,
) -> ProducedReportFailure:
    """Return a safe per-report failure for an expected but missing report list."""
    return ProducedReportFailure(
        report_type=report_type,
        error=GoogleApiResponseError(
            url="<youtube-reporting-report-list>",
            reason=f"missing YouTube Reporting report for {report_month}",
        ),
    )


def _build_youtube_report_success(
    *,
    client: YouTubeReportingClient,
    reports: list[dict[str, object]],
    report_type: str,
    report_month: str,
    account_id: str,
) -> ProducedReportSuccess | ProducedReportFailure | None:
    """Aggregate downloaded CSVs into a parser payload + raw-reports tuple for one job/month."""
    csv_reports: dict[str, _CsvReportDownload] = {}
    seen_checksums: set[str] = set()
    totals: dict[tuple[str, str | None, str], Decimal] = {}
    try:
        failure = _download_youtube_csv_reports(
            client=client,
            reports=reports,
            report_type=report_type,
            report_month=report_month,
            account_id=account_id,
            csv_reports=csv_reports,
            seen_checksums=seen_checksums,
            totals=totals,
        )
        if failure is not None:
            return _failure_with_downloaded_csv_reports(
                csv_reports=csv_reports,
                failure=failure,
            )
        if not csv_reports:
            return None
        raw_reports = tuple(csv_reports[report_id] for report_id in sorted(csv_reports))
        parser_payload = _parser_payload_from_csv_totals(
            totals=totals,
            report_ids=[raw_report.report_id for raw_report in raw_reports],
            report_type=report_type,
            report_month=report_month,
        )
        return ProducedReportSuccess(
            report_type=report_type,
            parser_payload=parser_payload,
            raw_reports=raw_reports,
        )
    except OAuthRefreshError:
        _cleanup_csv_report_downloads(tuple(csv_reports.values()))
        raise
    except GoogleConnectorError as exc:
        return ProducedReportFailure(
            report_type=report_type,
            error=exc,
            raw_reports=tuple(csv_reports.values()),
        )
    except Exception:
        _cleanup_csv_report_downloads(tuple(csv_reports.values()))
        raise


def _download_youtube_csv_reports(
    *,
    client: YouTubeReportingClient,
    reports: list[dict[str, object]],
    report_type: str,
    report_month: str,
    account_id: str,
    csv_reports: dict[str, _CsvReportDownload],
    seen_checksums: set[str],
    totals: dict[tuple[str, str | None, str], Decimal],
) -> ProducedReportFailure | None:
    """
    Download every CSV referenced by ``reports`` and return them as
    ``_CsvReportDownload`` entries.
    """
    for report in reports:
        failure = _download_youtube_csv_report(
            client=client,
            report=report,
            report_type=report_type,
            report_month=report_month,
            account_id=account_id,
            csv_reports=csv_reports,
            seen_checksums=seen_checksums,
            totals=totals,
        )
        if failure is not None:
            return failure
    return None


def _download_youtube_csv_report(
    *,
    client: YouTubeReportingClient,
    report: dict[str, object],
    report_type: str,
    report_month: str,
    account_id: str,
    csv_reports: dict[str, _CsvReportDownload],
    seen_checksums: set[str],
    totals: dict[tuple[str, str | None, str], Decimal],
) -> ProducedReportFailure | None:
    """
    Download a single YouTube Reporting CSV blob, spooling to disk if it
    exceeds the in-memory cap.
    """
    csv_report: _CsvReportDownload | None = None
    try:
        download_url = _require_text(report, "downloadUrl")
        report_id = _require_text(report, "id")
        raw_bytes = client.fetch_report(download_url=download_url)
        checksum = compute_checksum(raw_bytes)
        if report_id in csv_reports or checksum in seen_checksums:
            return None
        csv_report = _spool_csv_report(report_id=report_id, raw_bytes=raw_bytes)
        _accumulate_csv_report_bytes(
            totals=totals,
            raw_bytes=raw_bytes,
            report_id=report_id,
            report_type=report_type,
            report_month=report_month,
            default_content_owner=(
                account_id if report_type.startswith("content_owner_") else None
            ),
        )
        csv_reports[report_id] = csv_report
        seen_checksums.add(checksum)
        return None
    except OAuthRefreshError:
        raise
    except GoogleConnectorError as exc:
        raw_reports = (csv_report,) if csv_report is not None else ()
        return ProducedReportFailure(
            report_type=report_type,
            error=exc,
            raw_reports=raw_reports,
        )


def _failure_with_downloaded_csv_reports(
    *,
    csv_reports: dict[str, _CsvReportDownload],
    failure: ProducedReportFailure,
) -> ProducedReportFailure:
    """Re-emit a per-report failure with already-downloaded CSV evidence attached."""
    raw_reports = list(csv_reports.values())
    seen_report_ids = {raw_report.report_id for raw_report in raw_reports}
    for raw_report in failure.raw_reports:
        if raw_report.report_id in seen_report_ids:
            continue
        raw_reports.append(raw_report)
        seen_report_ids.add(raw_report.report_id)
    return ProducedReportFailure(
        report_type=failure.report_type,
        error=failure.error,
        raw_reports=tuple(raw_reports),
    )


def _require_text(mapping: dict[str, object], field_name: str) -> str:
    """Pull a non-blank string field from a Google API descriptor or fail.

    Google's REST envelopes are well-typed in practice, but a missing /
    blank / non-string value here would translate downstream to a confusing
    ``ParserError`` or path-builder ``ValueError``. Surface it as a typed
    ``GoogleConnectorError`` so the outer per-report ``except`` records
    ``GoogleApiResponseError`` against the right report.
    """
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise GoogleApiResponseError(
            url="<descriptor>", reason=f"missing or non-string {field_name!r}"
        )
    return value.strip()


def _csv_to_parser_payload(
    *,
    raw_bytes: bytes,
    report_id: str,
    report_type: str,
    month: str,
) -> dict[str, object]:
    """Convert YouTube Reporting CSV bytes to the parser-friendly dict shape.

    The estimated-revenue CSV is a daily export and can include lower-level
    breakdown dimensions (video, country, claimed status) plus non-revenue
    metric columns. The parser/repository layer models monthly channel
    revenue rows, so this adapter sums all requested-month breakdown rows by
    channel, optional content_owner, and currency before parser handoff.
    """
    return _csv_reports_to_parser_payload(
        csv_reports=[_CsvReportDownload(report_id=report_id, raw_bytes=raw_bytes)],
        report_type=report_type,
        report_month=month,
    )


def _csv_reports_to_parser_payload(
    *,
    csv_reports: list[_CsvReportDownload],
    report_type: str,
    report_month: str,
    default_content_owner: str | None = None,
) -> dict[str, object]:
    """Aggregate one or more daily YouTube Reporting CSVs to monthly rows."""
    totals: dict[tuple[str, str | None, str], Decimal] = {}
    for csv_report in csv_reports:
        _accumulate_csv_report_bytes(
            totals=totals,
            raw_bytes=csv_report.read_bytes(),
            report_id=csv_report.report_id,
            report_type=report_type,
            report_month=report_month,
            default_content_owner=default_content_owner,
        )

    return _parser_payload_from_csv_totals(
        totals=totals,
        report_ids=[csv_report.report_id for csv_report in csv_reports],
        report_type=report_type,
        report_month=report_month,
    )


# ============================================================================
# Purpose: Decode one Google CSV report, validate its header, and fold each
#          row into monthly parser totals.
# Database/ORM: None.
# Standards: UTF-8 and CSV-shape failures are typed as GoogleApiResponseError
#            so live runs persist downloaded evidence before marking failure.
# Blast Radius: Parser payload construction for YouTube Reporting revenue rows.
#               No graph projection impact detected.
# Connections:
#   - Function: _download_youtube_csv_report -> spools raw bytes before calling.
#   - Function: _csv_reports_to_parser_payload -> aggregates multiple raw CSVs.
# ============================================================================
def _accumulate_csv_report_bytes(
    *,
    totals: dict[tuple[str, str | None, str], Decimal],
    raw_bytes: bytes,
    report_id: str,
    report_type: str,
    report_month: str,
    default_content_owner: str | None,
) -> None:
    """Parse one CSV blob and fold its per-(channel, video, metric) totals into ``totals``."""
    import csv
    import io

    month_start, _month_end = _month_bounds(report_month=report_month, report_id=report_id)
    expected_month = f"{month_start.year:04d}-{month_start.month:02d}"
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _parser_payload_error(report_id=report_id, reason="csv is not valid utf-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    default_currency = _validate_csv_headers(
        reader.fieldnames, report_id=report_id, report_type=report_type
    )
    for csv_row in reader:
        _accumulate_csv_row(
            totals=totals,
            csv_row=csv_row,
            report_id=report_id,
            expected_month=expected_month,
            default_content_owner=default_content_owner,
            default_currency=default_currency,
        )


# ============================================================================
# Purpose: Validate that a downloaded CSV has the columns required to build
#          parser rows.
# Database/ORM: None.
# Standards: Empty/headerless/missing-column payloads become typed upstream
#            response errors instead of parser KeyError/None drift.
# Blast Radius: YouTube Reporting CSV acceptance only. No graph projection
#               impact detected.
# Connections:
#   - Function: _accumulate_csv_report_bytes -> calls this before row parsing.
# ============================================================================
def _validate_csv_headers(
    fieldnames: Sequence[str] | None, *, report_id: str, report_type: str
) -> str | None:
    """Confirm the CSV header set contains the metric column expected for ``report_type``."""
    if not fieldnames:
        raise _parser_payload_error(
            report_id=report_id,
            reason="csv missing header row",
        )
    headers = {
        field.strip().lower() for field in fieldnames if isinstance(field, str) and field.strip()
    }
    for group_name, aliases in (
        ("date", _CSV_DATE_COLUMNS),
        ("channel", _CSV_CHANNEL_COLUMNS),
        ("revenue", _CSV_REVENUE_COLUMNS),
    ):
        if not any(alias.lower() in headers for alias in aliases):
            raise _parser_payload_error(
                report_id=report_id,
                reason=f"csv missing {group_name} column",
            )
    if any(alias.lower() in headers for alias in _CSV_CURRENCY_COLUMNS):
        return None
    default_currency = _CSV_DEFAULT_CURRENCY_BY_REPORT_TYPE.get(report_type)
    if default_currency is not None:
        return default_currency
    raise _parser_payload_error(
        report_id=report_id,
        reason="csv missing currency column (expected one of: currency_code, currencyCode)",
    )


def _parser_payload_from_csv_totals(
    *,
    totals: dict[tuple[str, str | None, str], Decimal],
    report_ids: list[str],
    report_type: str,
    report_month: str,
) -> dict[str, object]:
    """Render the aggregated ``totals`` map into the parser payload shape used downstream."""
    report_id = _combined_report_id(report_ids)
    month_start, month_end = _month_bounds(report_month=report_month, report_id=report_id)

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


def _accumulate_csv_row(
    *,
    totals: dict[tuple[str, str | None, str], Decimal],
    csv_row: dict[str, str | None],
    report_id: str,
    expected_month: str,
    default_content_owner: str | None,
    default_currency: str | None,
) -> None:
    """
    Fold one CSV row's metric into the running ``totals`` map (validates
    non-negative decimals).
    """
    # Normalize the date column. YouTube Reporting CSV uses ``date`` or
    # ``day``. The row is daily, but the parser payload is monthly, so this
    # date is used only to ensure the row belongs to report_month.
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
                f"csv row date {date_value!r} outside requested report_month {expected_month!r}"
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
    content_owner = _first_present(csv_row, "content_owner") or default_content_owner

    # Pull the estimated revenue amount. Accept either Google's documented
    # ``estimated_partner_revenue`` / ``estimatedRevenue`` columns or the
    # test-fixture shorthand ``ad_revenue``. The aggregate is kept as Decimal
    # and stringified after summation so precision and trailing scale survive.
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
        if _row_has_any_column(csv_row, *_CSV_CURRENCY_COLUMNS):
            raise _parser_payload_error(
                report_id=report_id,
                reason="csv row blank currency value",
            )
        currency = default_currency
    if not currency:
        raise _parser_payload_error(
            report_id=report_id,
            reason="csv row missing currency column (expected one of: currency_code, currencyCode)",
        )

    key = (channel, content_owner, currency)
    totals[key] = totals.get(key, Decimal("0")) + amount_decimal


def _combined_report_id(report_ids: list[str]) -> str:
    """Return the report_id to record on disk: a single id or a ``combined:`` aggregate label."""
    if len(report_ids) == 1:
        return report_ids[0]
    return f"combined:{','.join(report_ids)}"


def _spool_csv_report(*, report_id: str, raw_bytes: bytes) -> _CsvReportDownload:
    """Persist a downloaded CSV either in memory or to a managed temp file based on size."""
    raw_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="ums-yt-report-",
            suffix=".csv",
            delete=False,
        ) as tmp:
            tmp.write(raw_bytes)
            raw_path = Path(tmp.name)
        return _CsvReportDownload(report_id=report_id, raw_path=raw_path)
    except Exception:
        if raw_path is not None:
            try:
                raw_path.unlink()
            except FileNotFoundError:
                pass
        raise


def _cleanup_csv_report_downloads(
    raw_reports: tuple[_CsvReportDownload, ...],
) -> None:
    """Best-effort unlink every temp file backing the supplied CSV downloads."""
    for raw_report in raw_reports:
        raw_report.cleanup()


def _month_bounds(*, report_month: str, report_id: str) -> tuple[date_cls, date_cls]:
    """Return the (first_day, last_day) calendar bounds for ``report_month`` (validates format)."""
    expected_month = report_month.strip()
    try:
        validate_report_month(expected_month)
    except ConnectorRunValidationError as exc:
        raise _parser_payload_error(
            report_id=report_id,
            reason=f"report_month {expected_month!r} not YYYY-MM",
        ) from exc
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


def _row_has_any_column(row: dict[str, str | None], *keys: str) -> bool:
    """True iff the CSV row carries any of the supplied column names (case-insensitive)."""
    lower_keys = {k.lower() for k in row if isinstance(k, str)}
    return any(key.lower() in lower_keys for key in keys)


def _parser_payload_error(*, report_id: str, reason: str) -> GoogleConnectorError:
    """Wrap CSV adapter failures in a typed connector error.

    Reuses ``GoogleApiResponseError`` because a malformed CSV from Google is,
    practically, an upstream response-shape problem: the orchestrator's
    per-report ``except`` already routes ``GoogleConnectorError`` into the
    failure list, so a CSV-shape failure ends up classed the same as a JSON
    list_reports failure.
    """
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
    for candidate_key in credential_key_candidates(connector_key):
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


def _zero_counts() -> dict[str, int]:
    """Mirror the B2.3 ``CONNECTOR_RUN_COUNT_KEYS`` shape.

    ``finish_run`` validates the exact key set on commit, so any drift
    between this and ``CONNECTOR_RUN_COUNT_KEYS`` raises
    ``ConnectorRunValidationError`` at finish_run time.
    """
    return {key: 0 for key in CONNECTOR_RUN_COUNT_KEYS}


def _derive_terminal_status(
    counts: dict[str, int],
) -> Literal["SUCCEEDED", "PARTIAL", "FAILED"]:
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
    ValueError pattern of ``source_system_for_connector`` so a missing
    entry for a future B2.5/B2.6 registration fails loudly instead of
    silently emitting ``.json``.
    """
    try:
        return _EXTENSIONS[connector_key]
    except KeyError as exc:
        raise ValueError(
            f"unknown connector_key for extension dispatch: {connector_key!r}"
        ) from exc


def _parser_for_connector(
    connector_key: str,
) -> YouTubeReportingParser | YouTubeAnalyticsParser | AdSenseManagementParser:
    """Return the source-row parser bound to a given connector key.

    B2.5 added YouTubeAnalyticsParser; B2.6 (T38) wires AdSenseManagementParser.
    The helper isolates connector-to-parser routing from ``run_one`` so future
    registrations only touch this mapping rather than the orchestrator body.
    """
    if connector_key in {"youtube-reporting", "youtube_reporting"}:
        return YouTubeReportingParser()
    if connector_key in {"youtube-analytics", "youtube_analytics"}:
        return YouTubeAnalyticsParser()
    if connector_key in {"adsense-management", "adsense_management"}:
        return AdSenseManagementParser()
    raise ValueError(f"no parser bound for connector_key: {connector_key!r}")


# ============================================================================
# Purpose: ConnectorRunner adapter for the YouTube Analytics v2 reports.query
#   endpoint (spec §5.5). Iterates the tenant's active+revenue_required
#   channels (via list_target_channels), issues one fetch_channel_report call
#   per channel, wraps each JSON response as a parser-ready payload, and
#   yields one (report_type, parser_payload, raw_bytes) tuple per channel so
#   the orchestrator can store, parse, and upsert each channel independently.
#   The orchestrator's existing per-report bucket-B handler catches any
#   exception that escapes produce_reports; this class does NOT swallow errors
#   inside the generator body so the run can be marked PARTIAL per channel.
# Database/ORM: YouTubeChannelORM (read via list_target_channels). No write.
# Standards: Implements ConnectorRunner Protocol; typed keyword-only args;
#   raw_bytes serialized as sorted-key JSON for deterministic blob checksums
#   across reruns; no bare except; fail-open exceptions propagate to the
#   orchestrator's bucket-B exception catch.
# Blast Radius: Finance ingestion scope — only channels returned by
#   list_target_channels are fetched. A per-channel fetch failure surfaces as
#   a FAILED report entry (bucket B) and continues the run for other channels
#   rather than aborting the entire connector run.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/youtube_analytics_client.py
#     -> YouTubeAnalyticsClient.fetch_channel_report + list_target_channels.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py
#     -> YouTubeAnalyticsParser consumes the parser_payload yielded here.
#   - File: Docs/superpowers/plans/2026-05-26-spec-b2-google-live-connector.md
#     §B2.5 Task 33 -> runner contract and per-channel yield shape.
# ============================================================================
# ============================================================================
# Purpose: Inject the `channel` dimension into a single-channel content-owner
#          response so YouTubeAnalyticsParser keeps its (channel, month)
#          row-key contract without seeing the wire-level `dimensions=month`
#          projection. The channel identity is known from the request's
#          `filters=channel==<id>` value, so the synthesised dimension is
#          deterministic per channel and matches what Google would have
#          returned had we been able to add `channel` to the dimension set.
# Database/ORM: None.
# Standards: Idempotent — a response that already carries a `channel` header
#            (e.g. fixture-style payloads) passes through unchanged so existing
#            mocks remain valid. Malformed shapes are forwarded unchanged so
#            the parser's typed ParserError continues to fire.
# Blast Radius: Finance ingestion shape for YouTube Analytics rows. A drift
#               here would mis-attribute revenue or trip a parser ParserError
#               on a valid Google response.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/
#     youtube_analytics_client.py -> request shape uses dimensions=month.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     youtube_analytics.py -> requires `channel` in dim_values for source row
#     keys.
# ============================================================================
def _synthesise_analytics_channel_dimension(
    *,
    response: dict[str, object],
    channel_id: str,
) -> dict[str, object]:
    """Prepend the `channel` DIMENSION header and value to a month-only response."""
    column_headers = response.get("columnHeaders")
    if not isinstance(column_headers, list):
        # Let the parser raise a typed ParserError on the malformed payload.
        return response
    if any(isinstance(h, dict) and h.get("name") == "channel" for h in column_headers):
        # Already carries the dimension (mocked fixtures); pass through.
        return response
    rows = response.get("rows")
    if rows is not None and not isinstance(rows, list):
        # Let the parser raise a typed ParserError on the malformed payload.
        return response
    new_headers: list[object] = [
        {"columnType": "DIMENSION", "name": "channel"},
        *column_headers,
    ]
    new_rows: list[object] = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                new_rows.append([channel_id, *row])
            else:
                # Preserve the malformed entry so the parser can fail closed.
                new_rows.append(row)
    return {
        **response,
        "columnHeaders": new_headers,
        "rows": new_rows if isinstance(rows, list) else rows,
    }


# ============================================================================
# Purpose: Declare the parser-visible Analytics dimensions after the runner
#          synthesises channel evidence into the wire response.
# Database/ORM: None.
# Standards: Derived only from the outbound request contract, never from the
#            returned headers, so response drift remains detectable by parser.
# Blast Radius: Finance source-row admission and replay metadata only; outbound
#               Google query parameters remain unchanged.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/youtube_analytics_client.py
#     -> Builds the month-only wire request.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     youtube_analytics.py -> Validates this declaration against response headers.
# ============================================================================
def _analytics_parser_dimension_declaration(query_request: dict[str, str]) -> str:
    """Return the expected post-synthesis dimension declaration for replay."""
    wire_dimensions = query_request["dimensions"]
    names = wire_dimensions.split(",")
    if "channel" in names:
        return wire_dimensions
    return ",".join(("channel", *names))


# ============================================================================
# Purpose: B2.5 adapter that fetches YouTube Analytics per-channel reports. One
#          ``reports.query`` GET per eligible CMS channel for the run's month;
#          each success is yielded as ``("youtube_analytics", payload, bytes)``
#          with the ``channel`` dimension synthesised into the response, because
#          the wire request uses ``dimensions=month`` only (content-owner
#          reports need a multi-value channel filter to add ``channel``, and
#          B2.5 issues one single-value request per channel).
# Database/ORM: Reads the channel registry through ``list_target_channels``;
#               writes nothing itself -- the orchestrator owns the blob,
#               raw_file, and source-row writes. Parsed analytics rows persist
#               to GoogleRevenueSourceRowORM with
#               source_system == "youtube_analytics".
# Standards: Typed keyword-only contract matching ``ConnectorRunner``; the class
#            references ``YouTubeAnalyticsClient`` and ``list_target_channels``
#            by bare name so tests that patch
#            ``ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient``
#            replace what the runner actually uses. ``OAuthRefreshError``
#            escapes for run-level handling; any other ``GoogleConnectorError``
#            raised per channel (including a query-request validation
#            rejection) is yielded as a ``ProducedReportFailure`` so the run is
#            marked PARTIAL and sibling channels still run.
# Blast Radius: Finance ingestion -- these rows do feed the revenue projection,
#               unlike the audit-only AdSense path. Tenancy is carried by
#               ``run.tenant_id`` into ``list_target_channels``, so that value
#               is what scopes which channels are fetched at all. The explicit
#               top-level rollback after the channel-list SELECT is
#               load-bearing: it leaves the orchestrator's per-report
#               ``platform_lane`` block to open its own transaction, instead of
#               holding an elevated one across the blob upload where a slow
#               backend can trip ``idle_in_transaction_session_timeout``. The
#               nested-transaction check keeps the dry-run SAVEPOINT intact.
# Connections: youtube_analytics_client.py (client + channel list),
#              google_source_parsers/youtube_analytics.py (parser), spec §5.5.
#   - File: backend/ums_smart_revenue/connectors/google/youtube_analytics_client.py
#     -> YouTubeAnalyticsClient.fetch_channel_report and list_target_channels.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py
#     -> YouTubeAnalyticsParser consumes the yielded triple and keys rows on
#     (channel, month).
#   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
#     §5.5 -> targeted-channel ingestion contract.
# ============================================================================
class YouTubeAnalyticsRunner:
    """B2.5 adapter that fetches YouTube Analytics per-channel reports.

    Each yielded success carries the ``"youtube_analytics"`` report type, a
    parser-friendly payload dict (the raw ``reports.query`` JSON body augmented
    with the ``query_request`` key the parser needs), and the raw JSON bytes
    for blob storage and replay. The orchestrator stores each channel's blob
    separately; it never synthesises a cross-channel bundle. Only channels
    attached to the current CMS account are fetched here; outside-CMS channels
    remain out of scope for B2.5.

    The class references ``YouTubeAnalyticsClient`` and ``list_target_channels``
    by bare name so tests that patch
    ``ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient``
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
        """Yield one parser-ready payload per eligible CMS channel for ``report_month``.

        Each iteration issues one ``reports.query`` GET via
        ``YouTubeAnalyticsClient`` for the next ``channel_id`` returned by
        ``list_target_channels``. A per-channel ``GoogleConnectorError`` (other
        than ``OAuthRefreshError``, which still escapes for run-level handling)
        is yielded as a ``ProducedReportFailure`` so the orchestrator marks the
        run PARTIAL and continues with the remaining channels.
        """
        # ``run`` carries the tenant_id we need for list_target_channels.
        # On the T29 dry-run path run is None; the orchestrator still passes
        # a ConnectorRunEntry-compatible object for dry-runs so tenant_id is
        # always present. The connector_runs lifecycle is owned by run_one
        # itself, consistent with YouTubeReportingRunner's widened contract.
        # ConnectorRunEntry.tenant_id is a str; UUID() converts it for
        # list_target_channels which requires a typed UUID boundary.
        tenant_id: UUID = UUID(str(run.tenant_id))  # type: ignore[union-attr]
        http = GoogleHttpClient(credentials=credentials)
        try:
            client = YouTubeAnalyticsClient(http=http)
            channel_ids = list_target_channels(
                session,
                tenant_id=tenant_id,
                account_id=account_id,
            )
            # FIX: end the read-only channel-list transaction now so the
            # orchestrator's per-report `with platform_lane(session):` block
            # enters with NO active transaction. Without this, the
            # channel-list transaction stays open through the for-loop, and
            # the per-report platform_lane entry EAGERLY elevates the
            # already-open tenant transaction (it only defers elevation
            # when no transaction is active). The first analytics report
            # then holds an elevated Postgres transaction across the
            # `_prepare_raw_report_file` blob upload_and_verify (network /
            # GCS I/O) before any raw-file/audit write, and a slow backend
            # can trip `idle_in_transaction_session_timeout` mid-upload.
            # Rollback is safe: the SELECT is read-only and the orchestrator
            # owns the per-report transaction via the platform_lane
            # `with` block (its first DB call autobegins a fresh
            # transaction that the after_begin hook elevates to
            # app_platform via the platform-lane flag).
            #
            # Only rollback when the active transaction is TOP-LEVEL: the
            # dry-run path wraps the runner in `session.begin_nested()`
            # (a SAVEPOINT) and ends with `savepoint.rollback()`; rolling
            # back the outer transaction here would close the savepoint
            # and surface as a ResourceClosedError on the
            # `savepoint.rollback()` finally clause.
            if session.in_transaction() and not session.in_nested_transaction():
                session.rollback()
            for channel_id in channel_ids:
                try:
                    # FIX: Build the query_request INSIDE the per-channel try so
                    # a MalformedAnalyticsSelectorError (or any other typed
                    # GoogleConnectorError raised by the validation in
                    # _build_query_request) is caught as a per-channel Bucket B
                    # failure and the run continues with sibling channels,
                    # matching the produce_reports docstring contract. Calling
                    # this BEFORE the try would abort the whole generator on a
                    # single bad registry row and skip later valid channels.
                    query_request = _build_analytics_query_request(
                        account_id=account_id,
                        channel_id=channel_id,
                        report_month=report_month,
                    )
                    response: dict[str, object] = client.fetch_channel_report(
                        account_id=account_id,
                        channel_id=channel_id,
                        report_month=report_month,
                    )
                except OAuthRefreshError:
                    raise
                except GoogleConnectorError as exc:
                    # FIX: A single targeted-channel fetch failure (or validation
                    # rejection) is a report-scoped problem, not a run-scoped
                    # abort. Yield a Bucket B failure so the orchestrator can
                    # mark the run PARTIAL and continue with the remaining
                    # channels.
                    yield ProducedReportFailure(
                        report_type="youtube_analytics",
                        error=exc,
                    )
                    continue
                # Synthesise the `channel` dimension into the response. The
                # wire request uses `dimensions=month` only because Google's
                # content-owner reports require a multi-value channel filter to
                # add `channel` as a dimension, and B2.5 issues one request per
                # channel (single-value filter). YouTubeAnalyticsParser still
                # keys rows on (channel, month), so we inject the known
                # channel_id from the filter back into columnHeaders / rows
                # here. Idempotent: a response that already carries a `channel`
                # header is passed through unchanged, which keeps mocked tests
                # that return a `channel,month` payload working.
                augmented_response = _synthesise_analytics_channel_dimension(
                    response=response,
                    channel_id=channel_id,
                )
                # FIX: stamp the parser-payload query_request with the calendar
                # month's last day as endDate. The wire request keeps the
                # first-of-month endDate (Google requires both ends to be the
                # first day when `dimensions=month`), but YouTubeAnalyticsParser
                # persists `endDate` directly as each source row's period_end.
                # Without this override every monthly-aggregate row would record
                # period_end = first-of-month, mis-recording the coverage window
                # for downstream auditing and revenue-fact normalisation.
                parser_query_request = {
                    **query_request,
                    "endDate": calendar_month_end_iso(report_month),
                    # FIX: The parser consumes the post-synthesis response, so
                    # its metadata must declare channel plus the wire-requested
                    # dimensions. Leaving this as wire-level `month` would make
                    # the injected channel look like undeclared response drift.
                    "dimensions": _analytics_parser_dimension_declaration(query_request),
                }
                # Augment the raw response with the query_request metadata the
                # parser needs to build row keys and validate the range. Reuse
                # the canonical contentOwner/channel scope so replay and
                # stale-row cleanup remain consistent.
                parser_payload: dict[str, object] = {
                    **augmented_response,
                    "query_request": parser_query_request,
                }
                # Stored blob is the AUGMENTED parser_payload (includes injected
                # query_request metadata), not the raw API response — this is a
                # deliberate divergence from YouTubeReportingRunner where raw_bytes
                # is the literal CSV from Google. Rationale: a single JSON blob can
                # be fully replayed through YouTubeAnalyticsParser with no runner
                # state.
                raw_bytes = json.dumps(parser_payload, sort_keys=True).encode("utf-8")
                yield ("youtube_analytics", parser_payload, raw_bytes)
        finally:
            http.close()


# ============================================================================
# Purpose: B2.6 adapter that fetches the AdSense Management v2 monthly
#          account-earnings report. AdSense is account-scoped (one report per
#          account per month), so each ``produce_reports`` call yields exactly
#          one parser-ready payload — no per-channel iteration, no dimension
#          synthesis (the T35 adapter wraps the wire response in the parser-
#          ready shape with a deterministic ``report_id`` already stamped).
# Database/ORM: None directly — orchestrator owns the blob/raw_file/source-row
#               writes. AdSense rows persist to GoogleRevenueSourceRowORM with
#               source_system == "adsense_management" and youtube_channel_id
#               NULL.
# Standards: Typed keyword-only contract matching ``ConnectorRunner``; the
#            class references ``AdSenseManagementClient`` by bare name so
#            tests can patch
#            ``ums_smart_revenue.connectors.runs.orchestrator.AdSenseManagementClient``
#            and replace what the runner actually uses. ``OAuthRefreshError``
#            still escapes for run-level handling; any other
#            ``GoogleConnectorError`` is yielded as a ``ProducedReportFailure``
#            so the orchestrator's Bucket B handler marks the run FAILED
#            (one-report run -> no PARTIAL semantics; no sibling reports
#            survive).
# Blast Radius: AdSense ingestion is audit/evidence only in B2 — C1 skips
#               AdSense rows as SkipReason.MISSING_CHANNEL_ID until a future
#               allocation/mapping spec, so finance totals do not move on this
#               connector. The runner still owes a clean source_report_id
#               provenance trail for the audit pipeline.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/adsense_management_client.py
#     -> AdSenseManagementClient.fetch_monthly_report returns the parser-ready
#     payload (request/headers/rows/report_id) this runner yields verbatim.
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/adsense_management.py
#     -> AdSenseManagementParser consumes ``("adsense_management", payload, bytes)``.
#   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
#     §5.6 -> AdSense ingestion contract.
# ============================================================================
class AdSenseManagementRunner:
    """B2.6 adapter that fetches the AdSense monthly account-earnings report.

    AdSense is account-scoped (one report per account per month), so each run
    yields exactly one parser-ready payload. The class references
    ``AdSenseManagementClient`` by bare name so tests can patch
    ``ums_smart_revenue.connectors.runs.orchestrator.AdSenseManagementClient``
    and replace what the runner actually instantiates.

    AdSense rows are ingestion / audit evidence only in B2; C1 skips them as
    ``SkipReason.MISSING_CHANNEL_ID`` until a future allocation/mapping spec.
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
        """Yield the single AdSense monthly report for the supplied (account, month).

        ``AdSenseManagementClient.fetch_monthly_report`` already wraps the wire
        response in the parser-ready shape (request/headers/rows/report_id), so
        the runner forwards it verbatim. ``OAuthRefreshError`` escapes for the
        orchestrator's run-level handling; any other ``GoogleConnectorError``
        is yielded as a ``ProducedReportFailure`` so the existing Bucket B
        handler counts the failure consistently with the other connector
        runners — this run has only one report, so a failure here marks the
        whole run FAILED (no sibling reports to keep PARTIAL).
        """
        http = GoogleHttpClient(credentials=credentials)
        try:
            client = AdSenseManagementClient(http=http)
            try:
                parser_payload = client.fetch_monthly_report(
                    account_id=account_id,
                    report_month=report_month,
                )
            except OAuthRefreshError:
                raise
            except GoogleConnectorError as exc:
                yield ProducedReportFailure(
                    report_type="adsense_management",
                    error=exc,
                )
                return
            # Stored blob is the parser-ready payload JSON: the adapter already
            # stamped a deterministic report_id on it, so a full replay through
            # AdSenseManagementParser needs no runner state. ``sort_keys`` keeps
            # the on-disk bytes byte-stable across reruns for the same inputs.
            raw_bytes = json.dumps(parser_payload, sort_keys=True).encode("utf-8")
            yield ("adsense_management", parser_payload, raw_bytes)
        finally:
            http.close()


# ----------------------------------------------------------------------------
# Module-load registration
# ----------------------------------------------------------------------------

# An instance (not the class) is registered: B2.5/B2.6 runners may carry
# per-connector config in their constructors, and the dispatch contract
# returns the registered value directly to the orchestrator.
register_connector(key="youtube-reporting", runner=YouTubeReportingRunner())
register_connector(key="youtube_reporting", runner=YouTubeReportingRunner())
register_connector(key="youtube-analytics", runner=YouTubeAnalyticsRunner())
register_connector(key="youtube_analytics", runner=YouTubeAnalyticsRunner())
register_connector(key="adsense-management", runner=AdSenseManagementRunner())
register_connector(key="adsense_management", runner=AdSenseManagementRunner())
