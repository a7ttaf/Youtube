# AdSense Payment Sync — Task 9 (Operator CLI) — Companion Plan

> **Companion to** `Docs/superpowers/plans/2026-05-29-spec-adsense-payment-sync.md`.
> This file holds the full literal Task 9 (CLI + tests). Execute it in sequence
> as Task 9 of the main plan, after Tasks 1–8 are green. Same hard gates apply:
> failing test first, minimal implementation, green, commit.

**Goal:** Add `scripts/run_adsense_payment_sync.py`, an operator CLI that drives
`AdSensePaymentSyncService` for one `(tenant, account)` with a stable exit-code
contract, mirroring `scripts/run_google_connector.py`.

**Source spec:** `Docs/superpowers/specs/2026-05-28-spec-adsense-payment-sync-design.md` §10.

---

## Task 9 — CLI `scripts/run_adsense_payment_sync.py`

**Files:**
- Create: `scripts/run_adsense_payment_sync.py`
- Create: `tests/scripts/test_run_adsense_payment_sync_cli.py`

**Contract (spec §10):** flags `--tenant <UUID>` (required), `--account` (required),
`--reason` (required, non-empty), `--dry-run` (optional). Resolve credentials
under `adsense-management` (handled inside the service). Build the connector
service principal (`build_connector_service_principal`, which raises `ValueError`
when `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` is unset) and a
`SqlAlchemyAuditSink`. Call `AdSensePaymentSyncService.sync(...)`. **Live mode
commits the session on success; dry-run prints counts and never commits.**

**Exit codes:** `0` on success (incl. clean dry-run and a `synced_count=0` no-op);
`2` on a typed pre-write/config failure — missing `UMS_DATABASE_URL`, a missing
service-actor `ValueError`, or any of `GoogleConnectorError` /
`AdSensePaymentError` (covers `AdSensePaymentValidationError` +
`AdSensePaymentLockedMonthError`) / `AdSensePaymentMappingError`. Untyped errors
propagate with a traceback (non-zero). argparse rejections (bad UUID / missing
flag) exit non-zero via argparse.

### Why `ValueError` is caught narrowly

`AdSensePaymentError` and `AdSensePaymentMappingError` both subclass `ValueError`,
so catching them is specific. The service-actor failure is a **bare** `ValueError`
from `build_connector_service_principal`; it is caught in its own `try` around
that call only, so an unrelated `ValueError` raised inside `service.sync` still
propagates (untyped errors are not masked).

- [ ] **Step 1 — Write the failing CLI tests** (new file
  `tests/scripts/test_run_adsense_payment_sync_cli.py`):

```python
import importlib.util
from pathlib import Path

import pytest

from ums_smart_revenue.connectors.google.adsense_payment_mapping import (
    AdSensePaymentMappingError,
)
from ums_smart_revenue.connectors.google.errors import GoogleConnectorError
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentLockedMonthError,
    AdSensePaymentValidationError,
)

_CLI_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "run_adsense_payment_sync.py"
)
TENANT = "00000000-0000-0000-0000-000000031001"
BASE_ARGV = ["--tenant", TENANT, "--account", "pub-1", "--reason", "r"]


def _load_cli():
    # Load the standalone script by path (scripts/ is not an importable
    # package). exec_module runs its sys.path bootstrap + imports once per test
    # for clean monkeypatch isolation.
    spec = importlib.util.spec_from_file_location("run_adsense_payment_sync", _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSettings:
    def __init__(self, database_url="sqlite+pysqlite:///:memory:"):
        self.database_url = database_url


class _SpySession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False  # never suppress; untyped errors propagate


class _FakeResult:
    synced_count = 1
    skipped_balance_count = 0
    skipped_locked_count = 0
    months = ["2026-04"]


class _FakeServiceOK:
    def __init__(self, session, *, audit_sink, **kwargs):
        pass

    def sync(self, **kwargs):
        return _FakeResult()


def _patch_common(monkeypatch, module, *, session, service, settings=None):
    monkeypatch.setattr(
        module,
        "load_app_settings",
        lambda: settings if settings is not None else _FakeSettings(),
    )
    monkeypatch.setattr(module, "build_session_factory", lambda _url: (lambda: session))
    monkeypatch.setattr(
        module, "build_connector_service_principal", lambda *, tenant_id: object()
    )
    monkeypatch.setattr(
        module, "SqlAlchemyAuditSink", lambda _session, *, tenant_id=None: object()
    )
    monkeypatch.setattr(module, "AdSensePaymentSyncService", service)


def test_cli_live_success_commits_and_returns_0(monkeypatch, capsys):
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)
    rc = module.main(BASE_ARGV)
    assert rc == 0
    assert session.commits == 1                       # live mode commits
    out = capsys.readouterr().out
    assert "SYNCED" in out
    assert "synced=1" in out


def test_cli_dry_run_does_not_commit(monkeypatch, capsys):
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)
    rc = module.main(BASE_ARGV + ["--dry-run"])
    assert rc == 0
    assert session.commits == 0                       # dry-run never commits
    assert "DRY-RUN" in capsys.readouterr().out


def test_cli_missing_db_config_returns_2(monkeypatch, capsys):
    module = _load_cli()
    session = _SpySession()
    _patch_common(
        monkeypatch, module, session=session, service=_FakeServiceOK,
        settings=_FakeSettings(database_url=""),
    )
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert "UMS_DATABASE_URL" in capsys.readouterr().err
    assert session.commits == 0


@pytest.mark.parametrize("error", [
    GoogleConnectorError("boom"),
    AdSensePaymentValidationError("bad batch"),
    AdSensePaymentLockedMonthError("month locked"),
    AdSensePaymentMappingError("$ ambiguous"),
])
def test_cli_typed_failure_returns_2(monkeypatch, capsys, error):
    module = _load_cli()
    session = _SpySession()

    class _Raises:
        def __init__(self, session, *, audit_sink, **kwargs):
            pass

        def sync(self, **kwargs):
            raise error

    _patch_common(monkeypatch, module, session=session, service=_Raises)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert type(error).__name__ in capsys.readouterr().err
    assert session.commits == 0                       # no commit on failure


def test_cli_missing_service_actor_returns_2(monkeypatch, capsys):
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)

    def _no_actor(*, tenant_id):
        raise ValueError("UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID is required")

    monkeypatch.setattr(module, "build_connector_service_principal", _no_actor)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert "ValueError" in capsys.readouterr().err
    assert session.commits == 0


def test_cli_untyped_error_propagates(monkeypatch):
    module = _load_cli()
    session = _SpySession()

    class _Boom:
        def __init__(self, session, *, audit_sink, **kwargs):
            pass

        def sync(self, **kwargs):
            raise RuntimeError("unexpected non-typed error")

    _patch_common(monkeypatch, module, session=session, service=_Boom)
    with pytest.raises(RuntimeError):
        module.main(BASE_ARGV)


def test_cli_bad_tenant_uuid_is_argparse_error():
    module = _load_cli()
    # argparse type=UUID rejects before any settings/session work.
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--tenant", "not-a-uuid", "--account", "pub-1", "--reason", "r"])
    assert excinfo.value.code != 0


def test_cli_blank_reason_is_argparse_error():
    module = _load_cli()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--tenant", TENANT, "--account", "pub-1", "--reason", "   "])
    assert excinfo.value.code != 0
```

- [ ] **Step 2 — Run, expect FAIL** (script missing).
  Run: `python -m pytest -q tests/scripts/test_run_adsense_payment_sync_cli.py -x`
  Expected: collection/exec error — `FileNotFoundError` / `spec_from_file_location`
  cannot load a non-existent `scripts/run_adsense_payment_sync.py`.

- [ ] **Step 3 — Implement `scripts/run_adsense_payment_sync.py`** verbatim:

```python
#!/usr/bin/env python
"""CLI entrypoint for the AdSense live payment sync.

Usage:
    python scripts/run_adsense_payment_sync.py \
        --tenant <UUID> \
        --account <account-id or accounts/...> \
        --reason "<audit reason>" \
        [--dry-run]

Loads runtime config from ``UMS_*`` env vars, opens a SQLAlchemy session, builds
the connector service principal + audit sink, and runs
``AdSensePaymentSyncService.sync``. Live mode commits on success; dry-run prints
counts and never commits.

Exit codes (operator contract -- spec §10):
    0 -- success, including a clean dry-run and a synced_count=0 no-op.
    2 -- typed pre-write/config failure: missing UMS_DATABASE_URL, a missing
         service-actor ValueError, or GoogleConnectorError / AdSensePaymentError
         / AdSensePaymentMappingError. No session commit happened.
    !=0 -- argparse rejection (bad --tenant UUID, missing/blank required flag).
    (untyped errors propagate with a real traceback.)
"""
# ============================================================================
# Purpose: Operator CLI surface that drives one AdSense live payment sync for a
#          single (tenant, account). Translates argparse errors, typed
#          pre-write failures, and success/dry-run into stable exit codes for
#          cron/runbook callers.
# Database/ORM: Opens one Session; all SQL is owned by
#          AdSensePaymentSyncService / SqlAlchemyAdSensePaymentRepository /
#          SqlAlchemyAuditSink. The CLI commits the session in live mode only.
# Standards: Thin entrypoint -- argparse + session bootstrap + exit-code
#          translation. Typed connector/payment errors -> exit 2; untyped
#          errors propagate. No secret/token is printed.
# Blast Radius: Pure operator surface. No authorization, finance math, graph,
#          run_one, connector_runs, or source-row logic here.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/adsense_payment_sync.py
#     -> AdSensePaymentSyncService.sync (the whole §5 pipeline).
#   - File: backend/ums_smart_revenue/connectors/google/audit.py ->
#     build_connector_service_principal (RUN_CONNECTOR_JOBS service actor).
#   - File: backend/ums_smart_revenue/auth/sql_audit_sink.py ->
#     SqlAlchemyAuditSink (persists ADSENSE_PAYMENT_SYNCED).
#   - File: Docs/superpowers/specs/2026-05-28-spec-adsense-payment-sync-design.md
#     §10 -> CLI flags, exit codes, dry-run semantics.
# ============================================================================
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

# Bootstrap: make ``backend/`` importable when invoked directly. Mirrors
# scripts/run_google_connector.py; pyproject ``pythonpath = ["backend"]`` covers
# pytest collection but not direct script execution.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PATH = str(_PROJECT_ROOT / "backend")
if _BACKEND_PATH not in sys.path:
    sys.path.insert(0, _BACKEND_PATH)

from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink  # noqa: E402
from ums_smart_revenue.config.settings import load_app_settings  # noqa: E402
from ums_smart_revenue.connectors.google.adsense_payment_mapping import (  # noqa: E402
    AdSensePaymentMappingError,
)
from ums_smart_revenue.connectors.google.adsense_payment_sync import (  # noqa: E402
    AdSensePaymentSyncService,
)
from ums_smart_revenue.connectors.google.audit import (  # noqa: E402
    build_connector_service_principal,
)
from ums_smart_revenue.connectors.google.errors import (  # noqa: E402
    GoogleConnectorError,
)
from ums_smart_revenue.db.session import build_session_factory  # noqa: E402
from ums_smart_revenue.finance.adsense_payments import (  # noqa: E402
    AdSensePaymentError,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse + validate CLI args; argparse exits non-zero on rejection."""
    parser = argparse.ArgumentParser(
        description="Run the AdSense live payment sync for one (tenant, account).",
    )
    parser.add_argument(
        "--tenant", required=True, type=UUID, help="Tenant UUID for this sync."
    )
    parser.add_argument(
        "--account",
        required=True,
        help="AdSense account id (bare id or accounts/<id>).",
    )
    parser.add_argument(
        "--reason", required=True, help="Non-empty audit reason for the pull."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + validate + parse only: no DB writes, no audit, no commit.",
    )
    args = parser.parse_args(argv)
    if not args.reason.strip():
        # parser.error exits with code 2 (argparse convention).
        parser.error("--reason must not be blank")
    return args


def main(argv: list[str] | None = None) -> int:
    """Return the CLI exit code; ``__main__`` wraps this in ``SystemExit``."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    settings = load_app_settings()
    if not settings.database_url:
        print(
            "UMS_DATABASE_URL is required to run the AdSense payment sync",
            file=sys.stderr,
        )
        return 2
    session_factory = build_session_factory(settings.database_url)
    with session_factory() as session:
        try:
            # Service-actor resolution is fail-closed: a missing/blank
            # UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID raises ValueError here,
            # before any fetch or DB write. Caught narrowly so unrelated
            # ValueErrors inside service.sync still propagate.
            actor = build_connector_service_principal(tenant_id=args.tenant)
        except ValueError as exc:
            print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2

        audit_sink = SqlAlchemyAuditSink(session, tenant_id=args.tenant)
        service = AdSensePaymentSyncService(session, audit_sink=audit_sink)
        try:
            result = service.sync(
                tenant_id=args.tenant,
                account_id=args.account,
                actor=actor,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        except (
            GoogleConnectorError,
            AdSensePaymentError,
            AdSensePaymentMappingError,
        ) as exc:
            # Typed pre-write/parse/validation/locked failure. No commit; the
            # operator gets ``<ClassName>: <message>`` on stderr. (Untyped
            # errors are NOT caught here -- they propagate with a traceback.)
            print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2

        if args.dry_run:
            # Dry-run: print would-sync counts, do not commit.
            print(
                f"DRY-RUN would_sync={result.synced_count} "
                f"skipped_balances={result.skipped_balance_count} "
                f"skipped_locked={result.skipped_locked_count} "
                f"months={result.months}"
            )
            return 0

        # Live mode: persist the pull (sync_payments + audit already ran inside
        # service.sync within this transaction scope).
        session.commit()

    print(
        f"SYNCED synced={result.synced_count} "
        f"skipped_balances={result.skipped_balance_count} "
        f"skipped_locked={result.skipped_locked_count} "
        f"months={result.months}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4 — Run, expect PASS**
  Run: `python -m pytest -q tests/scripts/test_run_adsense_payment_sync_cli.py`
  Expected: all pass (live commit/exit-0; dry-run no-commit/exit-0; missing DB
  config exit-2; four typed failures exit-2 + no commit; missing service actor
  exit-2; untyped `RuntimeError` propagates; bad UUID + blank reason argparse).
  Run: `python -m ruff check backend tests scripts`
  Expected: clean.

- [ ] **Step 5 — Commit**
```bash
git add scripts/run_adsense_payment_sync.py tests/scripts/test_run_adsense_payment_sync_cli.py
git commit -m "feat(adsense-payments): operator CLI for live payment sync"
```

---

## Notes for the implementer

- `tests/scripts/` is a new directory; if test collection needs an
  `__init__.py` to match the repo's other test packages, check whether existing
  `tests/<area>/` dirs carry one and follow that convention. The importlib loader
  above does not require `scripts/` to be a package.
- The `# noqa: E402` markers on the first-party imports are intentional and
  mirror `scripts/run_google_connector.py`: the `sys.path` bootstrap must run
  before the `ums_smart_revenue.*` imports.
- Exit-2 is the union of "config/credential/validation/parse/locked" failures so
  cron callers can branch on "non-zero = investigate" while distinguishing a
  typed pre-write failure (2) from an argparse misuse (argparse's own code).
