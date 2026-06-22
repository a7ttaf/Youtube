#!/usr/bin/env python
# ============================================================================
# Purpose: Operator CLI surface that drives one end-to-end connector_runs
#          lifecycle (or a counts-only dry-run) for a single
#          (tenant, connector_key, account_id, report_month). Translates
#          argparse parse errors, typed connector failures, and terminal
#          run statuses into stable exit codes for cron/runbook callers.
# Database/ORM: Reads ApiConnectorCredentialORM and writes ConnectorRunORM /
#               RawReportFileORM / ConnectorRunRawFileORM /
#               GoogleRevenueSourceRowORM transitively through ``run_one``;
#               this script owns no SQL of its own.
# Standards: Thin entrypoint -- argparse + session bootstrap + exit-code
#            translation only. All business logic stays in
#            ``ums_smart_revenue.connectors.runs.orchestrator``. Typed
#            ``GoogleConnectorError`` family is the only exception class the
#            CLI catches; untyped errors propagate so the operator sees the
#            real traceback.
# Blast Radius: Pure operator surface -- no authorization, audit, or finance
#               logic is implemented here. The orchestrator import below is
#               what triggers ``register_connector(...)`` at module load, so
#               removing it would empty ``registry.known_keys()`` and break
#               the ``--connector`` choices below.
# Connections:
#   - File: backend/ums_smart_revenue/config/settings.py ->
#     ``load_app_settings`` (UMS_DATABASE_URL et al.).
#   - File: backend/ums_smart_revenue/db/session.py ->
#     ``build_session_factory(database_url)``.
#   - File: backend/ums_smart_revenue/connectors/google/registry.py ->
#     ``known_keys()`` feeds argparse ``choices``.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     ``run_one``; the import side-effect registers the YouTube Reporting
#     runner keys (B2.5 / B2.6 will add analytics and AdSense runners).
#   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md
#     §5.4 -> CLI contract (flags, exit codes, dry-run semantics).
# ============================================================================
"""CLI entrypoint for the B2 live Google connector (T30).

Usage:
    python scripts/run_google_connector.py \
        --tenant <UUID> \
        --connector <registered-key> \
        --account <account-id> \
        --month <YYYY-MM> \
        [--dry-run]

Loads runtime config from ``UMS_*`` environment variables (see
``ums_smart_revenue.config.settings``), opens a SQLAlchemy session via the
shared session factory, dispatches to ``run_one(...)`` for the selected
connector, prints a one-line summary to stdout, and translates the
``GoogleConnectorError`` family into an operator-readable stderr line.

Exit codes (operator contract -- see Docs spec §5.4):
    0 -- SUCCEEDED, or DRY-RUN that completed without raising a typed error.
    1 -- run finished PARTIAL or FAILED (one or more reports failed but the
         orchestrator still wrote a terminal connector_runs row).
    2 -- Bucket A pre-start_run typed error (e.g. CredentialNotFoundError,
         InactiveCredentialError, OAuthRefreshError). No connector_runs row
         was created; the operator sees ``<ClassName>: <message>`` on stderr.
    !=0 -- argparse rejection (unknown --connector, bad UUID, malformed
           --month). Always non-zero with argparse's standard exit status.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

# Bootstrap: make ``backend/`` importable when the CLI is invoked directly
# (e.g. ``python scripts/run_google_connector.py ...``). Mirrors the pattern
# in scripts/run_validation_gate.py so the operator does not need to set
# PYTHONPATH manually. ``pyproject.toml``'s ``pythonpath = ["backend"]``
# covers pytest collection but does not apply to direct script execution.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PATH = str(_PROJECT_ROOT / "backend")
if _BACKEND_PATH not in sys.path:
    sys.path.insert(0, _BACKEND_PATH)

from sqlalchemy.orm import Session  # noqa: E402

from ums_smart_revenue.config.settings import load_app_settings  # noqa: E402
from ums_smart_revenue.connectors.credentials import (  # noqa: E402
    SqlAlchemyConnectorCredentialRepository,
    live_credential_rejection_detail,
)
from ums_smart_revenue.connectors.google import registry  # noqa: E402
from ums_smart_revenue.connectors.google.errors import (  # noqa: E402
    CredentialNotFoundError,
    CredentialSmokeRequiredError,
    GoogleConnectorError,
    InactiveCredentialError,
)

# The orchestrator import is load-bearing for argparse: its module-level
# ``register_connector(...)`` calls are what populate the dispatch registry,
# so ``registry.known_keys()`` only sees the YouTube Reporting keys AFTER
# this import runs. ``run_one`` is the
# public callable; the side-effect on ``registry`` is what
# ``_parse_args`` below depends on. Import order between ``registry`` and
# ``orchestrator`` does not matter for correctness -- both run at module
# load, before ``_parse_args`` is first called -- so this file lets ruff's
# I001 import sorter do its alphabetical thing.
from ums_smart_revenue.connectors.runs.orchestrator import (  # noqa: E402
    _credential_key_candidates,
    run_one,
)
from ums_smart_revenue.connectors.runs.tenant_context import (  # noqa: E402
    connector_tenant_context,
)
from ums_smart_revenue.db.session import build_session_factory  # noqa: E402

# Strict ``YYYY-MM`` with two-digit month in 01-12. argparse's ``type=`` can
# raise but its error message is ugly (``invalid <callable> value``); using
# ``parser.error`` below produces the standard ``argument --month: ...`` form
# operators are used to.
_MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Argparse layer: validates --connector against the live registry,
    --tenant as a UUID, and --month as strict ``YYYY-MM``.

    ``registry.known_keys()`` is evaluated at parser-construction time, so
    the orchestrator import above must have already populated the registry
    (it has -- the ``register_connector`` call runs at module import).
    Calling ``sorted(...)`` keeps the help text stable across runs.
    """
    parser = argparse.ArgumentParser(
        description="Run the B2 live Google connector for one (tenant, account, month).",
    )
    parser.add_argument("--tenant", required=True, type=UUID, help="Tenant UUID for this run.")
    parser.add_argument(
        "--connector",
        required=True,
        choices=sorted(registry.known_keys()),
        help="Connector key registered in connectors.google.registry.",
    )
    parser.add_argument("--account", required=True, help="Connector account identifier (string).")
    parser.add_argument(
        "--month",
        required=True,
        help="Report month in strict YYYY-MM form (e.g. 2026-05).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip writes: list/fetch/parse reports and print counts only. "
            "No connector_runs row, no raw_file row, no source-row upsert."
        ),
    )
    args = parser.parse_args(argv)
    if not _MONTH_PATTERN.match(args.month):
        # parser.error exits with code 2 (argparse convention). Keeping the
        # message format consistent with other --argument errors helps
        # runbook readability.
        parser.error(f"--month must be YYYY-MM, got {args.month!r}")
    return args


# ============================================================================
# Purpose: Fail closed before a live CLI run when the selected credential has
#          not passed the owner-approved smoke path or its persisted smoke
#          telemetry is already stale.
# Database/ORM: Reads ApiConnectorCredentialORM through
#               SqlAlchemyConnectorCredentialRepository only; no writes.
# Standards: Alias-aware lookup, typed operator errors, inactive credentials
#            retain InactiveCredentialError, and short-lived but unexpired
#            Google tokens are allowed because run_one refreshes credentials.
# Blast Radius: Live CLI admission control only. Dry-run and API route behavior
#               remain separately guarded at their own boundaries.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/credentials.py ->
#     ``live_credential_rejection_detail`` shared admission rule.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     ``_credential_key_candidates`` alias compatibility and ``run_one``.
# ============================================================================
def _enforce_live_credential_smoke(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
) -> None:
    repository = SqlAlchemyConnectorCredentialRepository(session, tenant_id=tenant_id)
    credential = None
    for candidate_key in _credential_key_candidates(connector_key):
        credential = repository.get_credential(
            session,
            tenant_id=tenant_id,
            connector_key=candidate_key,
            account_id=account_id,
        )
        if credential is not None:
            break
    if credential is None:
        raise CredentialNotFoundError(connector_key=connector_key, account_id=account_id)
    if credential.status != "active":
        raise InactiveCredentialError(credential_id=credential.id, status=credential.status)
    rejection_detail = live_credential_rejection_detail(credential, as_of=datetime.now(UTC))
    if rejection_detail is not None:
        raise CredentialSmokeRequiredError(detail=rejection_detail)


# ============================================================================
# Purpose: CLI entrypoint. Parses argv, opens a session, dispatches to
#          ``run_one``, and translates the outcome (or typed error) into
#          the operator exit-code contract documented in this module's
#          docstring.
# Database/ORM: Opens one ``Session`` against the configured database; all
#               SQL is owned by ``run_one`` and its helpers.
# Standards: Fail-closed on typed connector errors (exit 2); pass-through
#            on untyped errors (real traceback for the operator). The
#            session is opened in a ``with`` block so a mid-run process
#            kill still flushes the underlying connection back to the pool.
# Blast Radius: This function is what every cron job and on-call runbook
#               for the B2 connector eventually shells out to; its exit
#               codes are the public contract. Changing them is a breaking
#               change for downstream automation.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     ``run_one`` (returns ``ConnectorRunOutcome``).
#   - File: backend/ums_smart_revenue/db/session.py ->
#     ``build_session_factory(database_url)``.
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Return the CLI's exit code; ``__main__`` wraps this in ``SystemExit``.

    Accepting ``argv`` as a parameter (rather than reading ``sys.argv``
    directly) keeps the function testable in-process via ``main([...])``
    without subprocess overhead.
    """
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    settings = load_app_settings()
    if not settings.database_url:
        # FIX: missing DB config is an operator contract error, not a downstream
        # SQLAlchemy TypeError. Keep it explicit and in the CLI's exit-2 class.
        print("UMS_DATABASE_URL is required to run the Google connector", file=sys.stderr)
        return 2
    session_factory = build_session_factory(settings.database_url)
    # FIX: set TENANT_CTX for the run. The connector CLI runs outside any
    # FastAPI request, so without this the per-transaction RLS session hook
    # (db/session.py) finds no tenant, clears the trusted context row, and pins
    # the restricted app_tenant lane -> every tenant-table policy denies all
    # rows and the run dies fail-closed at the credential read (a misleading
    # CredentialNotFoundError) before any ingest. The previous code built the
    # tenant-lane session but never set the context -- the fail-closed-empty gap.
    # The CLI also passes its open session so the helper loads the tenant by id
    # and enforces the ACTIVE-only lifecycle gate (replaying the gate
    # TenantResolverMiddleware applies on web requests). A UUID pointing at a
    # suspended / archived / missing tenant raises TenantLifecycleError which
    # is caught below and translated to exit 2.
    #
    # Kody review (PR #94 thread PRRT_kwDOSZIgN86I-yFZ): the
    # connector_tenant_context context manager's __enter__ runs BEFORE this
    # try block when both are listed in a single ``with`` statement, so a
    # TenantLifecycleError raised during the tenant lookup would propagate
    # uncaught through main() and surface as a raw Python traceback with
    # exit 1 (violating the documented exit-2 contract for Bucket A typed
    # errors). Nest the context manager INSIDE the try block so the existing
    # ``except GoogleConnectorError`` translates the lookup failure into the
    # documented exit 2.
    with session_factory() as session:
        try:
            with connector_tenant_context(args.tenant, session=session):
                if not args.dry_run:
                    _enforce_live_credential_smoke(
                        session,
                        tenant_id=args.tenant,
                        connector_key=args.connector,
                        account_id=args.account,
                    )
                outcome = run_one(
                    session,
                    tenant_id=args.tenant,
                    connector_key=args.connector,
                    account_id=args.account,
                    report_month=args.month,
                    dry_run=args.dry_run,
                )
        except GoogleConnectorError as exc:
            # Bucket A: pre-start_run typed error. No connector_runs row was
            # created. Exit 2 distinguishes this from a finished-but-FAILED
            # run (exit 1) so cron jobs can branch on "config / credential
            # problem" vs "operator should check the run row".
            print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2
    if outcome.run is None:
        # Dry-run: ``ConnectorRunOutcome(run=None, counts=..., ...)``. No DB
        # writes happened (the orchestrator's SAVEPOINT rollback enforces
        # this even if a future runner regresses). Always exit 0; a failed
        # report inside dry-run is reflected in ``counts['reports_failed']``,
        # which the operator can read off the printed line.
        print(f"DRY-RUN counts={outcome.counts}")
        return 0
    # Live run: print the terminal status, the canonical counts dict, and
    # the per-report failures list so the operator's first signal is on one
    # line. Exit 0 only on SUCCEEDED; PARTIAL and FAILED both exit 1 so
    # cron's "non-zero = paging condition" stays correct.
    print(f"{outcome.run.status} counts={outcome.counts} failures={outcome.per_report_failures}")
    return 0 if outcome.run.status == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
