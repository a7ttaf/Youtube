#!/usr/bin/env python
"""End-to-end smoke check for the UMS Smart Revenue June-14 MVP dashboard.

Proves that ONE seeded demo month flows through every backend endpoint the six
wired dashboard screens consume, plus the production session-hydration path
(GET /session/me) the SPA bootstrap now calls. It:

1. Seeds a fully-populated demo month (scripts/seed_demo_month.py) into a
   throwaway SQLite file with ``--create-schema``. The seed exercises the real
   production lock gate first (which refuses on the single-source demo month)
   and reports its blockers, then ``--demo-lock-bypass`` forces the demo LOCKED
   state so the LOCKED-month read paths are covered.
2. Spins the FastAPI app up IN-PROCESS via TestClient (no live uvicorn), behind
   the real trusted-gateway header auth path (the demo principal headers an
   operator / Vite dev proxy would inject).
3. Asserts HTTP 200 + the key contract fields for EACH MVP screen's endpoint.
4. Prints a per-endpoint PASS/FAIL table and exits non-zero on any failure.
5. Cleans up the throwaway database.

Run: ``python scripts/smoke_mvp.py``  (optionally ``--month 2026-03``).

This is a verification harness, NOT a product feature: it imports + drives the
existing seed and app and never reimplements finance math or auth.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PATH = str(_PROJECT_ROOT / "backend")

_DEFAULT_MONTH = "2026-03"
# A throwaway, never-shared token: the smoke run sets it in its OWN process env
# and sends the same value in the demo headers, so the real trusted-gateway
# check (compare_digest) is exercised without weakening anything.
_SMOKE_GATEWAY_TOKEN = "smoke-mvp-trusted-gateway-token"
# The seed's deterministic committer user id, derived from the demo namespace
# AND the default UMS tenant id via _demo_tenant_uuid so it matches the row the
# seed inserts after the multi-tenant UUID fix (commit 9369d91):
#   uuid5(NS, UMS_TENANT_ID.hex + "|user|committer") == fc3d2f68-…
_DEMO_USER_ID = "fc3d2f68-a0e5-5525-b821-c372313c11eb"
_DEMO_USER_EMAIL = "demo-seed@ums.local"
# super_owner (global, all permissions) so the smoke proves EVERY screen's
# endpoint returns 200 in one pass — including GET /connectors/credentials,
# which needs MANAGE_CONNECTORS (a finance_admin does NOT carry that). The
# dashboard itself still gates connector actions behind the finance role; this
# is the smoke harness exercising the full read surface, not an auth change.
_DEMO_ROLE = "super_owner"
_FIRST_DEMO_CHANNEL = "demo-channel-alpha"


def _ensure_backend_path() -> None:
    """Make the local backend package importable for direct script execution."""
    if _BACKEND_PATH not in sys.path:
        sys.path.insert(0, _BACKEND_PATH)
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))


@dataclass
class _CheckResult:
    """One endpoint check outcome for the PASS/FAIL table."""

    screen: str
    name: str
    method: str
    path: str
    status_code: int | None = None
    passed: bool = False
    detail: str = ""


@dataclass
class _Check:
    """A single endpoint smoke check: how to call it and how to validate it."""

    screen: str
    name: str
    method: str
    path: str
    # Returns "" on success or a human-readable failure reason on a contract miss.
    validate: Callable[[Any], str]
    expected_status: int = 200
    # Optional JSON body for POST checks; None sends no body.
    json_body: dict[str, Any] | None = None


def _require(condition: bool, message: str) -> str:
    """Return an empty string when the condition holds, else the failure message."""
    return "" if condition else message


def _validate_net_revenue(body: Any) -> str:
    """Assert the net-revenue summary carries its key Command Center fields."""
    if not isinstance(body, Mapping):
        return "response is not a JSON object"
    return (
        _require(isinstance(body.get("status"), str), "missing status")
        or _require(
            isinstance(body.get("channel_count"), int) and body["channel_count"] >= 1,
            "channel_count must be >= 1",
        )
        or _require(
            isinstance(body.get("total_net_revenue_usd"), str),
            "total_net_revenue_usd must be a string (money-as-string)",
        )
        or _require(
            body.get("allocation_source")
            in {"committed_snapshot", "live_compute", "live_fallback"},
            f"unexpected allocation_source: {body.get('allocation_source')!r}",
        )
        or _require(isinstance(body.get("channels"), list), "channels must be a list")
        or _require(isinstance(body.get("currency"), str), "missing currency")
    )


def _validate_finance_close(body: Any) -> str:
    """Assert the finance-close status carries month + lock state."""
    if not isinstance(body, Mapping):
        return "response is not a JSON object"
    return (
        _require(isinstance(body.get("month"), str), "missing month")
        or _require(
            body.get("status") in {"OPEN", "LOCKED"},
            f"unexpected close status: {body.get('status')!r}",
        )
    )


def _validate_lock_refused(body: Any) -> str:
    """Assert a readiness-refused lock returns a FastAPI 409 detail envelope."""
    if not isinstance(body, Mapping):
        return "response is not a JSON object"
    return _require("detail" in body, "expected 'detail' key in 409 body")


def _validate_readiness(body: Any) -> str:
    """Assert the close readiness carries the ready flag + blockers list."""
    if not isinstance(body, Mapping):
        return "response is not a JSON object"
    return (
        _require(isinstance(body.get("ready"), bool), "ready must be a bool")
        or _require(isinstance(body.get("blockers"), list), "blockers must be a list")
        or _require(isinstance(body.get("month"), str), "missing month")
    )


def _validate_explain(body: Any) -> str:
    """Assert the net explanation carries metric/value/formula/components."""
    if not isinstance(body, Mapping):
        return "response is not a JSON object"
    return (
        _require(
            body.get("metric") == "net_revenue_usd",
            f"unexpected metric: {body.get('metric')!r}",
        )
        or _require(
            isinstance(body.get("value"), str),
            "value must be a string (money-as-string)",
        )
        or _require(isinstance(body.get("formula"), str), "missing formula")
        or _require(
            isinstance(body.get("components"), list) and len(body["components"]) >= 1,
            "components must be a non-empty list",
        )
    )


def _validate_smart_alerts(body: Any) -> str:
    """Assert the smart-alerts summary carries status + alerts list."""
    if not isinstance(body, Mapping):
        return "response is not a JSON object"
    return (
        _require(
            body.get("status") in {"ATTENTION_REQUIRED", "CLEAR"},
            f"unexpected smart-alert status: {body.get('status')!r}",
        )
        or _require(isinstance(body.get("alerts"), list), "alerts must be a list")
        or _require(isinstance(body.get("month"), str), "missing month")
    )


def _validate_paginated_items(body: Any) -> str:
    """Assert a list endpoint returns the {items, pagination} envelope."""
    if not isinstance(body, Mapping):
        return "response is not a JSON object"
    return (
        _require(isinstance(body.get("items"), list), "items must be a list")
        or _require(
            isinstance(body.get("pagination"), Mapping),
            "pagination envelope missing",
        )
    )


def _validate_adsense_payments(body: Any) -> str:
    """Assert the AdSense payment list returns items + pagination + audit event."""
    if not isinstance(body, Mapping):
        return "response is not a JSON object"
    return (
        _validate_paginated_items(body)
        or _require(
            isinstance(body.get("audit_event"), Mapping),
            "audit_event missing on payment read",
        )
    )


def _validate_export_created(body: Any) -> str:
    """Assert the 202 export creation response carries an id and export_type."""
    if not isinstance(body, Mapping):
        return "response is not a JSON object"
    return _require(isinstance(body.get("id"), str), "missing id on export job")


def _validate_session_me(body: Any) -> str:
    """Assert GET /session/me carries the principal identity + capabilities.

    This is the production hydration contract the SPA calls on bootstrap:
    identity (user_id/email) plus the camelCase capability booleans the AppShell
    gates the dashboard on. The demo principal is super_owner @ global scope, so
    every capability resolves true here — the assertion only proves the shape +
    that identity is present, not a specific permission grant.
    """
    if not isinstance(body, Mapping):
        return "response is not a JSON object"
    capabilities = body.get("capabilities")
    return (
        _require(isinstance(body.get("user_id"), str) and body.get("user_id"), "missing user_id")
        or _require(isinstance(body.get("email"), str) and body.get("email"), "missing email")
        or _require(isinstance(capabilities, Mapping), "capabilities object missing")
        or _require(
            isinstance(capabilities.get("canViewRevenue"), bool),
            "capabilities.canViewRevenue must be a camelCase bool",
        )
        or _require(
            isinstance(capabilities.get("canRunConnectorJobs"), bool),
            "capabilities.canRunConnectorJobs must be a camelCase bool",
        )
    )


def _build_checks(month: str) -> list[_Check]:
    """Build the per-screen endpoint checks for the seeded demo month."""
    return [
        # Production session hydration: the SPA bootstrap (useSessionBootstrap)
        # calls GET /session/me to hydrate the principal's capabilities and gate
        # the AppShell. It is a demo path the frontend now depends on, so the
        # smoke proves the identity + camelCase capability shape under the same
        # in-process trusted-gateway demo principal as every screen check.
        _Check(
            "Session",
            "session hydration",
            "GET",
            "/session/me",
            _validate_session_me,
        ),
        _Check(
            "Command Center",
            "net-revenue summary",
            "GET",
            f"/revenue/months/{month}/net-revenue",
            _validate_net_revenue,
        ),
        _Check(
            "Command Center",
            "smart-alerts panel",
            "GET",
            f"/revenue/months/{month}/smart-alerts",
            _validate_smart_alerts,
        ),
        _Check(
            "Month Close",
            "finance-close status",
            "GET",
            f"/finance-close/{month}",
            _validate_finance_close,
        ),
        _Check(
            "Month Close",
            "close readiness",
            "GET",
            f"/finance-close/{month}/readiness",
            _validate_readiness,
        ),
        _Check(
            "Trace / Explain",
            "net explanation",
            "POST",
            f"/revenue/channels/{_FIRST_DEMO_CHANNEL}/months/{month}"
            "/explain?metric=net_revenue_usd",
            _validate_explain,
        ),
        _Check(
            "Exports",
            "export jobs list",
            "GET",
            "/exports",
            _validate_paginated_items,
        ),
        _Check(
            "Connectors",
            "connector credentials",
            "GET",
            "/connectors/credentials",
            _validate_paginated_items,
        ),
        _Check(
            "Connectors",
            "adsense payments",
            "GET",
            f"/adsense/payments?month={month}",
            _validate_adsense_payments,
        ),
        # Write-path smoke: unlock → create export job → re-lock attempt (409 expected).
        # The demo month uses single-source channels so lock_month() always hits the
        # INSUFFICIENT_SOURCES readiness gate and returns 409 CONFLICT via the
        # production route; the --demo-lock-bypass flag is only available in the seed.
        _Check(
            "Month Close",
            "unlock month (write)",
            "POST",
            f"/finance-close/{month}/unlock",
            _validate_finance_close,
            json_body={"reason": "smoke-unlock"},
        ),
        _Check(
            "Exports",
            "create export job (write)",
            "POST",
            "/exports",
            _validate_export_created,
            expected_status=202,
            json_body={
                "export_type": "FINANCE_EXCEL",
                "scope_type": "global",
                "month": month,
                "reason": "smoke-export-test",
            },
        ),
        _Check(
            "Month Close",
            "re-lock month refused (write)",
            "POST",
            f"/finance-close/{month}/lock",
            _validate_lock_refused,
            expected_status=409,
            json_body={"reason": "smoke-re-lock"},
        ),
    ]


def _demo_headers() -> dict[str, str]:
    """Return the trusted-gateway headers an operator / Vite dev proxy injects."""
    return {
        "x-user-id": _DEMO_USER_ID,
        "x-user-email": _DEMO_USER_EMAIL,
        "x-role": _DEMO_ROLE,
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": _SMOKE_GATEWAY_TOKEN,
    }


@dataclass
class _SmokeContext:
    """Holds the throwaway DB path + URL + the in-process TestClient for one run."""

    database_path: str
    database_url: str
    client: Any
    headers: dict[str, str] = field(default_factory=_demo_headers)


def _seed_and_build_app(
    month: str, database_path: str, database_url: str
) -> _SmokeContext:
    """Seed the demo month then build the in-process app + TestClient.

    Sets the trusted-gateway token in THIS process env and clears the cached
    app settings so the real header-auth gate (which reads the cached settings)
    picks the smoke token up - the smoke run never patches or weakens auth.
    """
    os.environ["UMS_TRUSTED_GATEWAY_TOKEN"] = _SMOKE_GATEWAY_TOKEN

    # Imported here (not at module top) because _ensure_backend_path() puts the
    # backend package on sys.path only inside main(), which runs first.
    from ums_smart_revenue.config.settings import (
        AUTHZ_SOURCE_ENV,
        AUTHZ_SOURCE_HEADERS,
    )

    # FIX: pin header-based authz for THIS smoke process around app construction
    # so the run stays hermetic under an ambient UMS_AUTHZ_SOURCE=database. The
    # harness sends only trusted-gateway demo headers (no X-UMS-Tenant; the demo
    # user is tenant-scoped), so an inherited database authz mode would break
    # principal loading. The explicit create_app(authz_source=...) below is not
    # enough on its own: ums_smart_revenue.app builds a module-level default app
    # via create_app() AT IMPORT time, which reads UMS_AUTHZ_SOURCE from settings
    # and raises before the smoke's own pinned call runs. So the env var is set
    # (and the settings cache cleared) BEFORE that import, then restored after,
    # never weakening the real header-auth gate.
    previous_authz_source = os.environ.get(AUTHZ_SOURCE_ENV)
    os.environ[AUTHZ_SOURCE_ENV] = AUTHZ_SOURCE_HEADERS
    try:
        return _seed_and_build_app_pinned(month, database_path, database_url)
    finally:
        if previous_authz_source is None:
            os.environ.pop(AUTHZ_SOURCE_ENV, None)
        else:
            os.environ[AUTHZ_SOURCE_ENV] = previous_authz_source


def _seed_and_build_app_pinned(
    month: str, database_path: str, database_url: str
) -> _SmokeContext:
    """Seed + build the app with header-authz already pinned in the env.

    Split out so the caller owns the env set/restore lifetime; this body runs
    while UMS_AUTHZ_SOURCE is pinned to ``headers`` (see _seed_and_build_app).
    """
    from ums_smart_revenue.config import settings as settings_module
    from ums_smart_revenue.config.settings import AUTHZ_SOURCE_HEADERS

    # The settings loader is lru_cached; clear it so the gateway token AND the
    # pinned header-authz source set above are read on the next load.
    settings_module.load_app_settings.cache_clear()

    import scripts.seed_demo_month as seed

    # --demo-lock-bypass: the smoke exercises the production lock gate (which
    # refuses on the single-source demo month) and then forces the demo LOCKED
    # state so the LOCKED-month read paths are covered. Disposable SQLite only.
    seed_rc = seed.main(
        [
            "--database-url", database_url, "--create-schema",
            "--month", month, "--demo-lock-bypass",
        ]
    )
    if seed_rc != 0:
        raise RuntimeError(f"seed_demo_month exited non-zero ({seed_rc})")

    from fastapi.testclient import TestClient

    # Importing this module builds its module-level default app via create_app();
    # the env pin above keeps that import-time instantiation in header-auth mode.
    from ums_smart_revenue.app import create_app

    # The explicit authz_source also documents intent + defends the smoke's own
    # app against any future settings drift, independent of the env pin.
    app = create_app(
        database_url=database_url, authz_source=AUTHZ_SOURCE_HEADERS
    )
    return _SmokeContext(
        database_path=database_path,
        database_url=database_url,
        client=TestClient(app),
    )


def _run_check(context: _SmokeContext, check: _Check) -> _CheckResult:
    """Execute one endpoint check and capture its PASS/FAIL outcome.

    Network/transport failures are caught and reported as a FAIL row (never a
    bare except) so one broken endpoint cannot abort the whole table.
    """
    result = _CheckResult(
        screen=check.screen, name=check.name, method=check.method, path=check.path
    )
    try:
        if check.method == "GET":
            response = context.client.get(check.path, headers=context.headers)
        elif check.method == "POST":
            response = context.client.post(
                check.path, headers=context.headers, json=check.json_body
            )
        else:
            result.detail = f"unsupported method {check.method}"
            return result
    except (RuntimeError, ValueError, OSError) as exc:
        result.detail = f"request raised {type(exc).__name__}: {exc}"
        return result

    result.status_code = response.status_code
    if response.status_code != check.expected_status:
        body_preview = response.text[:160].replace("\n", " ")
        result.detail = (
            f"expected {check.expected_status}, got {response.status_code}"
            f" - {body_preview}"
        )
        return result

    try:
        body = response.json()
    except ValueError as exc:
        result.detail = f"response was not valid JSON: {exc}"
        return result

    failure = check.validate(body)
    if failure:
        result.detail = failure
        return result

    result.passed = True
    return result


def _print_table(results: list[_CheckResult]) -> None:
    """Print the per-endpoint PASS/FAIL table for the smoke run."""
    print("=" * 96)
    print("UMS Smart Revenue - MVP end-to-end smoke (in-process TestClient)")
    print("=" * 96)
    header = f"{'RESULT':<6} {'SCREEN':<16} {'ENDPOINT':<24} {'CODE':<5} DETAIL"
    print(header)
    print("-" * 96)
    for result in results:
        status_label = "PASS" if result.passed else "FAIL"
        code = "" if result.status_code is None else str(result.status_code)
        detail = "" if result.passed else result.detail
        print(
            f"{status_label:<6} {result.screen:<16} {result.name:<24} "
            f"{code:<5} {detail}"
        )
    print("-" * 96)
    passed = sum(1 for r in results if r.passed)
    print(f"{passed}/{len(results)} checks passed")
    print("=" * 96)


def _dispose_engine(database_url: str) -> None:
    """Dispose the cached SQLAlchemy engine so SQLite releases the file handle.

    build_session_factory caches one engine per database_url in a module-level
    cache; on Windows that open pool keeps the throwaway .db file locked. Dispose
    + evict it before deleting so cleanup does not WinError-32.
    """
    try:
        from ums_smart_revenue.db import session as session_module
    except ImportError:
        return
    # FIX: Use the public dispose_cached_engine helper instead of reaching into
    # the module-private _engine_cache, so cleanup no longer depends on backend
    # internals (the helper disposes AND evicts under the cache lock).
    session_module.dispose_cached_engine(database_url)


def _cleanup(database_path: str, database_url: str | None = None) -> None:
    """Dispose the engine then remove the throwaway SQLite file + side files."""
    if database_url is not None:
        _dispose_engine(database_url)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = f"{database_path}{suffix}"
        if os.path.exists(candidate):
            try:
                os.remove(candidate)
            except OSError as exc:
                print(f"warning: could not remove {candidate}: {exc}", file=sys.stderr)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse operator CLI arguments for the MVP smoke check."""
    parser = argparse.ArgumentParser(
        description="End-to-end smoke check for the June-14 MVP dashboard endpoints.",
    )
    parser.add_argument(
        "--month",
        default=_DEFAULT_MONTH,
        help=f"Finance month YYYY-MM to seed + probe (default {_DEFAULT_MONTH}).",
    )
    return parser.parse_args(argv)


# ============================================================================
# Purpose: Operator entrypoint. Seed one demo month into a throwaway SQLite db,
#   spin the app up in-process behind the real trusted-gateway header auth, run
#   every MVP screen's endpoint check, print the PASS/FAIL table, clean up the
#   db, and exit non-zero on ANY failure (so CI/operators get a hard signal).
# Database/ORM: Throwaway SQLite only (created by seed_demo_month --create-schema
#   and torn down here); all SQL owned by the seeded ORMs / app endpoints.
# Standards: thin entrypoint; typed; no bare except (transport errors become FAIL
#   rows); the throwaway db is always cleaned up via try/finally; no auth
#   weakening (the real header gate runs with the smoke token set in this env).
# Blast Radius: None on real data — disposable SQLite, read-mostly probes. No
#   finance math, no schema/migration change, no Neo4j.
# Connections:
#   - File: scripts/seed_demo_month.py -> seeds the demo month; the production
#     lock gate is exercised + reported, then --demo-lock-bypass forces LOCKED.
#   - File: backend/ums_smart_revenue/app.py -> create_app (in-process).
#   - File: frontend/src/lib/api/types.ts -> the contract fields asserted here.
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Seed, probe every MVP endpoint, print the table, and return the exit code."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    _ensure_backend_path()

    fd, database_path = tempfile.mkstemp(prefix="ums_smoke_", suffix=".db")
    os.close(fd)
    database_url = f"sqlite+pysqlite:///{Path(database_path).as_posix()}"

    try:
        try:
            context = _seed_and_build_app(args.month, database_path, database_url)
        except (RuntimeError, ValueError, ImportError) as exc:
            print(f"SMOKE SETUP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

        results = [_run_check(context, check) for check in _build_checks(args.month)]
        _print_table(results)
        return 0 if all(r.passed for r in results) else 1
    finally:
        _cleanup(database_path, database_url)


if __name__ == "__main__":
    raise SystemExit(main())
