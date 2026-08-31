# ============================================================================
# Purpose: Pin the P0.6 logging contract -- the UMS_LOG_LEVEL setting, the
#   one-time root handler install, the UTC/millisecond format carrying
#   timestamp + level + logger name, and the THREE documented ways to get
#   this wrong: silently killing uvicorn's access log, adding a dependency,
#   and handing the application verbosity knob to every third-party logger
#   in the process (which published the guarded CMS content-owner id through
#   httpx2's INFO request line on every Google API call).
# Database/ORM: None.
# Standards: Every test runs under an autouse fixture that snapshots and
#   restores the logging tree (root level plus every existing logger's level,
#   handlers, propagate and disabled flags), because the uvicorn regression
#   test installs a production-shaped uvicorn.access logger and
#   configure_logging mutates process-global state. Without it this module
#   would make the suite order-dependent, which is precisely the failure P0.6
#   must not introduce.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/config/logging_config.py -> the module
#     under test.
#   - File: backend/ums_smart_revenue/config/settings.py -> UMS_LOG_LEVEL.
#   - File: backend/ums_smart_revenue/app.py -> the ASGI lifespan wiring.
#   - File: backend/ums_smart_revenue/api/dependencies.py -> the load-bearing
#     unknown-principal WARNING that diagnoses a wrong X-User-ID under
#     UMS_AUTHZ_SOURCE=database.
#   - File: tests/test_version_baseline.py -> the exact-set dependency
#     assertion the stdlib-only rule protects.
#   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
#     httpx2 (imported as httpx) is the real client whose INFO request line
#     carries the CMS content-owner id in its query string.
# ============================================================================
"""Regression tests for the P0.6 process logging configuration."""

from __future__ import annotations

import ast
import io
import logging
import re
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ums_smart_revenue.api.dependencies import (
    TrustedGatewayIdentity,
    current_principal_from_database,
)
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.principals import PrincipalNotFoundError
from ums_smart_revenue.config import logging_config
from ums_smart_revenue.config.logging_config import (
    FIRST_PARTY_LOGGER_NAME,
    LOG_FORMAT,
    THIRD_PARTY_LOG_LEVEL,
    UMS_LOG_HANDLER_NAME,
    build_log_formatter,
    configure_logging,
    installed_log_handler,
    redact_exception_summary,
    redact_sensitive_text,
    release_logging_output,
    release_logging_safety,
    restore_logging,
)
from ums_smart_revenue.config.settings import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENV,
    LOG_LEVEL_NAMES,
    load_app_settings,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

_PROBE_LOGGER = "ums_smart_revenue.logging_probe"
_LOG_LINE_PATTERN = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\s+"
    r"(?P<level>[A-Z]+)\s+"
    r"(?P<name>[\w.]+)\s+"
    r"\[(?P<thread>[^\]]+)\]\s+"
    r"(?P<message>.*)$"
)

# The 11 module loggers that existed with no handler behind them.
MODULE_LOGGER_NAMES = (
    "ums_smart_revenue.tenancy.resolver",
    "ums_smart_revenue.auth.users",
    "ums_smart_revenue.finance.google_source_normalizer",
    "ums_smart_revenue.connectors.runs.executor",
    "ums_smart_revenue.connectors.runs.normalization",
    "ums_smart_revenue.connectors.runs.scheduler",
    "ums_smart_revenue.connectors.runs.orchestrator",
    "ums_smart_revenue.api.users",
    "ums_smart_revenue.api.dependencies",
    "ums_smart_revenue.api.connectors",
    "ums_smart_revenue.api.exports",
)


@dataclass(frozen=True)
class _LoggerState:
    """Everything about one logger that a test in this module can mutate."""

    level: int
    handlers: list[logging.Handler]
    propagate: bool
    disabled: bool
    parent: logging.Logger | None


@pytest.fixture(autouse=True)
def isolated_logging_state() -> Iterator[None]:
    """Snapshot the logging tree and restore it after each test."""
    manager = logging.Logger.manager
    # Materialise `ums_smart_revenue` BEFORE the snapshot. configure_logging
    # now writes that logger's level, and the module tree reaches this module
    # only through children (`ums_smart_revenue.api.dependencies`, ...), which
    # leave a PlaceHolder -- not a Logger -- at the package node. The
    # saved_states comprehension below skips PlaceHolders, and restoring
    # loggerDict cannot undo the `.parent` re-pointing that getLogger performs
    # on every child, so without this line a test that skips restore_logging
    # would leave every application logger inheriting INFO from an orphaned
    # parent: order-dependence, which is what this fixture exists to prevent.
    logging.getLogger(FIRST_PARTY_LOGGER_NAME)
    saved_logger_dict = dict(manager.loggerDict)
    saved_disable = manager.disable
    saved_call_handlers = logging.Logger.callHandlers
    root = logging.getLogger()
    saved_root_level = root.level
    saved_root_handlers = root.handlers[:]
    saved_handler_filters = {
        handler: handler.filters[:] for handler in logging_config._configured_handlers(root)
    }
    saved_states = {
        name: _LoggerState(
            level=existing.level,
            handlers=existing.handlers[:],
            propagate=existing.propagate,
            disabled=existing.disabled,
            parent=existing.parent,
        )
        for name, existing in list(manager.loggerDict.items())
        if isinstance(existing, logging.Logger)
    }
    try:
        yield
    finally:
        # FIX: The process-level redaction dispatcher intentionally survives
        # output release while a safety lease remains. Tests that exercise an
        # unclean shutdown can therefore omit restore_logging(); restore the
        # exact pre-test dispatcher here just like the handler/filter tree.
        logging.Logger.callHandlers = saved_call_handlers
        logging_config._logging_state = logging_config._LoggingLeaseState()
        # configure_logging installs safety at the front of every current
        # handler, including pytest capture and logging.lastResort. Restore the
        # exact pre-test filter lists so safety generations cannot accumulate
        # across tests that intentionally omit restore_logging().
        for handler, filters in saved_handler_filters.items():
            handler.filters[:] = filters
        root.setLevel(saved_root_level)
        # Only ever REMOVE handlers a test introduced. Re-adding snapshot
        # handlers would be wrong: pytest builds a fresh LogCaptureHandler per
        # test phase, so the setup-phase objects captured here are stale by
        # teardown and re-attaching them would leak one handler per test --
        # exactly the accumulating global state this fixture exists to
        # prevent. Handlers a test removes are restored in-body by
        # production_shaped_root.
        for handler in root.handlers[:]:
            if handler not in saved_root_handlers:
                root.removeHandler(handler)
        for name, state in saved_states.items():
            restored = manager.loggerDict.get(name)
            if not isinstance(restored, logging.Logger):
                continue
            restored.setLevel(state.level)
            restored.handlers[:] = state.handlers
            restored.propagate = state.propagate
            restored.disabled = state.disabled
            # FIX: `.parent` has to be restored with the rest, and it was not.
            # Restoring loggerDict cannot undo a re-parenting. When the
            # uvicorn-shape helper materialises an intermediate node -- turning
            # the `uvicorn` PlaceHolder into a real Logger at INFO -- getLogger
            # re-points every descendant at it. The dict restore then puts the
            # PlaceHolder back and ORPHANS that Logger, but
            # `uvicorn.error.parent` still references it, so `uvicorn.error`
            # keeps resolving to INFO for the rest of the session. Measured
            # before this line existed: after the uvicorn regression test's
            # teardown, uvicorn.error own=NOTSET,
            # parent=<Logger uvicorn (INFO)>, effective=20 -- exactly the
            # order-dependence this fixture exists to prevent, and what
            # test_no_third_party_logger_in_the_process_is_info_enabled
            # surfaced.
            restored.parent = state.parent
        manager.loggerDict.clear()
        manager.loggerDict.update(saved_logger_dict)
        manager.disable = saved_disable


@contextmanager
def production_shaped_root() -> Iterator[logging.Logger]:
    """Yield the root logger in the shape uvicorn boots with: no handlers, WARNING.

    This has to be a context manager used INSIDE the test body, not a
    fixture. pytest attaches its capture handlers per test phase, so a
    fixture that empties ``root.handlers`` during setup is undone before the
    test body runs -- and the production ``basicConfig`` path, gated on
    ``len(root.handlers) == 0``, would never be the path under test.
    """
    root = logging.getLogger()
    borrowed = root.handlers[:]
    previous_level = root.level
    root.handlers[:] = []
    root.setLevel(logging.WARNING)
    try:
        yield root
    finally:
        # Give pytest its own handlers back, keeping anything the test left.
        root.handlers[:] = borrowed + [h for h in root.handlers if h not in borrowed]
        root.setLevel(previous_level)


def _probe(name: str = _PROBE_LOGGER) -> logging.Logger:
    """Return a module-shaped logger with no level or handler of its own."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    return logger


# ============================================================================
# Purpose: Install the production uvicorn access/error logger shape without
#          calling logging.config.dictConfig (DeepSource PY-A6006).
# Database/ORM: None.
# Standards: Mirrors uvicorn's LOGGING_CONFIG outcomes that these regression
#            tests assert: uvicorn.access has its own handler, propagate=False,
#            and uvicorn.error exists as a sibling under the uvicorn parent.
# Blast Radius: Test-only.
# Connections:
# - File: backend/ums_smart_revenue/config/logging_config.py -> must leave
#   uvicorn.access handlers untouched.
# ============================================================================
def _install_uvicorn_logging_shape() -> None:
    """Give uvicorn loggers the handler layout production uvicorn installs."""
    root_like = logging.getLogger("uvicorn")
    root_like.handlers.clear()
    root_like.setLevel(logging.INFO)
    root_like.propagate = False
    root_like.disabled = False
    error_handler = logging.StreamHandler(sys.stderr)
    error_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root_like.addHandler(error_handler)

    error = logging.getLogger("uvicorn.error")
    error.handlers.clear()
    error.setLevel(logging.INFO)
    error.propagate = True
    error.disabled = False

    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.setLevel(logging.INFO)
    access.propagate = False
    access.disabled = False
    access_handler = logging.StreamHandler(sys.stderr)
    access_handler.setFormatter(logging.Formatter("%(message)s"))
    access.addHandler(access_handler)


# ---------------------------------------------------------------------------
# The corrected premise: lastResort is the whole current configuration.
# ---------------------------------------------------------------------------


def test_last_resort_emits_warning_and_drops_info_without_any_configuration(monkeypatch):
    """Pin the stdlib behaviour the P0.6 correction rests on.

    The first audit claimed every ``logger.*`` call was discarded. It is not:
    ``logging.lastResort`` emits WARNING and above with no configuration at
    all. What is actually lost is INFO/DEBUG, dropped at ``isEnabledFor``
    because the unconfigured root logger sits at WARNING -- and the lines
    that do survive carry no timestamp, level or logger name.
    """
    assert logging.lastResort.level == logging.WARNING

    captured = logging.StreamHandler(io.StringIO())
    captured.setLevel(logging.lastResort.level)
    monkeypatch.setattr(logging, "lastResort", captured)

    with production_shaped_root():
        logger = _probe()
        logger.warning("warning survives with no configuration")
        logger.info("info is dropped with no configuration")

    text = captured.stream.getvalue()
    assert "warning survives with no configuration" in text
    assert "info is dropped with no configuration" not in text
    assert _LOG_LINE_PATTERN.match(text.strip()) is None


def test_configure_logging_delivers_the_info_records_last_resort_dropped():
    """INFO from a real module logger reaches the configured handler."""
    stream = io.StringIO()
    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        logging.getLogger("ums_smart_revenue.connectors.runs.scheduler").info(
            "group-sync tick: %d submitted, %d in-flight-skipped across %d tenants", 1, 0, 1
        )

    assert "group-sync tick: 1 submitted" in stream.getvalue()


# ---------------------------------------------------------------------------
# Hazard (a): killing uvicorn's access log, which works today.
# ---------------------------------------------------------------------------


def test_configure_logging_leaves_the_uvicorn_access_logger_untouched():
    """uvicorn gates access logging on ``uvicorn.access``'s own handler list.

    See uvicorn 0.52.4 ``protocols/http/h11_impl.py:57`` and
    ``httptools_impl.py:61``: ``self.access_log = self.access_logger
    .hasHandlers()``. ``uvicorn.access`` carries ``propagate=False``, so that
    call sees only that logger's own handlers -- a config pass that
    disables existing loggers, or that names ``uvicorn`` at all, turns
    request logging off silently.
    """
    with production_shaped_root():
        _install_uvicorn_logging_shape()
        access_logger = logging.getLogger("uvicorn.access")
        handlers_before = access_logger.handlers[:]
        assert access_logger.hasHandlers()

        configuration = configure_logging(level="INFO", stream=io.StringIO())

        assert configuration.installed is True
        assert access_logger.hasHandlers(), "uvicorn would set access_log=False"
        assert access_logger.disabled is False, "disable_existing_loggers would silence emit()"
        assert access_logger.handlers == handlers_before
        assert access_logger.level == logging.INFO
        assert access_logger.propagate is False


def test_configure_logging_still_emits_a_real_uvicorn_access_record():
    """An access record still reaches uvicorn's handler exactly once.

    Every stream a record could reach is captured, because a record can be
    doubled up two different chains: ``uvicorn.access -> uvicorn`` (its
    parent, which also carries ``propagate=False``) and ``... -> root``.
    Asserting on only one of them would miss a duplicate on the other.
    """
    access_output = io.StringIO()
    error_output = io.StringIO()
    ums_output = io.StringIO()

    with production_shaped_root():
        _install_uvicorn_logging_shape()
        access_logger = logging.getLogger("uvicorn.access")
        for handler in access_logger.handlers:
            assert isinstance(handler, logging.StreamHandler)
            handler.setStream(access_output)
        for handler in logging.getLogger("uvicorn").handlers:
            assert isinstance(handler, logging.StreamHandler)
            handler.setStream(error_output)

        configure_logging(level="INFO", stream=ums_output)

        access_logger.info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:51234", "GET", "/health", "1.1", 200
        )

    assert access_output.getvalue().count('"GET /health HTTP/1.1" 200') == 1
    assert "/health" not in error_output.getvalue(), "duplicated up the uvicorn chain"
    assert "/health" not in ums_output.getvalue(), "duplicated onto the root handler"


def test_configure_logging_does_not_disable_the_application_module_loggers():
    """``disable_existing_loggers`` would also silence all 11 module loggers."""
    stream = io.StringIO()
    with production_shaped_root():
        for name in MODULE_LOGGER_NAMES:
            _probe(name)

        configure_logging(level="INFO", stream=stream)

        for name in MODULE_LOGGER_NAMES:
            module_logger = logging.getLogger(name)
            assert module_logger.disabled is False, name
            module_logger.info("probe %s", name)

    emitted = stream.getvalue()
    for name in MODULE_LOGGER_NAMES:
        assert f"probe {name}" in emitted, name


# ---------------------------------------------------------------------------
# Hazard (b): adding a dependency.
# ---------------------------------------------------------------------------


def test_logging_config_imports_only_stdlib_and_first_party():
    """No structlog, no python-json-logger -- the dependency set is frozen.

    ``tests/test_version_baseline.py`` asserts exact-set equality on the
    manifest, so a new package is a build break. This asserts the same rule
    at the source, where the temptation actually lives.
    """
    module_path = Path(logging_config.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_roots.add(node.module.split(".")[0])

    third_party = imported_roots - set(sys.stdlib_module_names) - {"ums_smart_revenue"}
    assert third_party == set(), f"logging_config gained a dependency: {sorted(third_party)}"


# ---------------------------------------------------------------------------
# Hazard (c): handing the application verbosity knob to every library.
#
# UMS_LOG_LEVEL buys connector-run progress, tenant resolution and the export
# lifecycle. Applied to the ROOT logger it also buys every library's INFO,
# because a third-party logger is born NOTSET and inherits from root. Measured
# on this dependency set with the app imported: 56 third-party loggers go
# INFO-enabled at root=INFO and only SQLAlchemy self-gates. Two of them print
# a full request URL: httpx2 (_client.py:1085) and urllib3.poolmanager (:500).
# The YouTube Analytics/Reporting/Groups clients put the CMS content-owner id
# in the QUERY STRING, so that is one leaked guarded identifier per API call.
#
# The tests below are deliberately class-shaped, not httpx2-shaped: the point
# is that a dependency added next year inherits the floor with no edit here.
# ---------------------------------------------------------------------------

# A stand-in for the operator's real CMS content owner. Never put the real one
# in a tracked file -- the shape is all these tests need.
_FAKE_CONTENT_OWNER_ID = "ZZFAKECMSOWNERID0000ZZ"


def _import_connector_http_stack() -> None:
    """Import the first-party modules that pull the real HTTP dependencies in."""
    import ums_smart_revenue.connectors.google.http_client  # noqa: F401
    import ums_smart_revenue.connectors.google.youtube_analytics_client  # noqa: F401
    import ums_smart_revenue.connectors.google.youtube_groups_client  # noqa: F401
    import ums_smart_revenue.connectors.google.youtube_reporting_client  # noqa: F401


def test_a_real_http_request_never_writes_its_query_string_to_the_log():
    """The instance: a real httpx2 client, a real URL, a real configured handler.

    ``youtube_analytics_client.py:109`` builds ``ids=contentOwner==<id>`` as a
    query PARAMETER and ``http_client.py`` forwards it to httpx2, which logs
    ``request.url`` -- query string included -- at INFO. With the level on the
    root logger that line reached stderr, and on this deployment stderr is
    ``docker compose logs app``. Only the transport is a double here; the
    client, the URL construction and the logging configuration are real.
    """
    stream = io.StringIO()

    def respond(request: httpx2.Request) -> httpx2.Response:
        """respond."""
        return httpx2.Response(200, json={"rows": []})

    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
            response = client.get(
                "https://youtubeanalytics.googleapis.com/v2/reports",
                params={
                    "ids": f"contentOwner=={_FAKE_CONTENT_OWNER_ID}",
                    "filters": "channel==UCzzzzzzzzzzzzzzzzzzzzzz",
                    "startDate": "2026-07-01",
                    "endDate": "2026-07-01",
                    "metrics": "estimatedRevenue",
                },
            )

    assert response.status_code == 200, "the request itself must still happen"
    emitted = stream.getvalue()
    assert _FAKE_CONTENT_OWNER_ID not in emitted, emitted
    assert "youtubeanalytics.googleapis.com" not in emitted, emitted


def test_first_party_info_lands_while_third_party_info_does_not():
    """The two halves of the policy, asserted against one another.

    Losing the library line is only correct if the application line survives:
    a fix that silences both would pass a leak test and destroy P0.6.
    """
    stream = io.StringIO()
    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        logging.getLogger("ums_smart_revenue.connectors.runs.executor").info(
            "first-party progress line"
        )
        logging.getLogger("httpx2").info("third-party request line")

    emitted = stream.getvalue()
    assert "first-party progress line" in emitted
    assert "third-party request line" not in emitted


@pytest.mark.parametrize("level", ["DEBUG", "INFO"])
def test_a_dependency_nobody_has_added_yet_is_gated_at_the_floor(level):
    """CLASS test: the floor is inherited, so it needs no list to maintain.

    This is the assertion that fails if a future dependency starts logging
    request URLs at INFO. It names no library: a logger created for a package
    that does not exist in this dependency set today is born at NOTSET and
    resolves through the root logger, which configure_logging holds at
    ``THIRD_PARTY_LOG_LEVEL``. A fix that pinned only ``httpx2`` -- the
    instance -- would pass every other test in this file and fail this one.
    """
    with production_shaped_root():
        configure_logging(level=level, stream=io.StringIO())

        newcomer = logging.getLogger("a_dependency_added_next_year.client")
        assert newcomer.level == logging.NOTSET, "the premise: no level of its own"
        assert newcomer.getEffectiveLevel() == THIRD_PARTY_LOG_LEVEL
        assert newcomer.isEnabledFor(logging.INFO) is False
        assert newcomer.isEnabledFor(logging.DEBUG) is False


def test_no_third_party_logger_in_the_process_is_info_enabled():
    """The residual hole the inherited floor cannot close, caught as a build break.

    A dependency that calls ``setLevel(INFO)`` on its OWN logger at import
    time overrides an inherited floor. No constant in ``logging_config`` can
    prevent that, so it is caught here instead: this walks the REAL logger
    tree of an imported app plus the connector HTTP stack and fails on any
    non-first-party logger that is INFO-enabled under our configuration.
    """
    _import_connector_http_stack()
    create_app()

    with production_shaped_root():
        configure_logging(level="INFO", stream=io.StringIO())

        offenders = sorted(
            name
            for name, existing in list(logging.Logger.manager.loggerDict.items())
            if isinstance(existing, logging.Logger)
            and name.split(".")[0] != FIRST_PARTY_LOGGER_NAME
            and existing.isEnabledFor(logging.INFO)
        )

    assert offenders == [], (
        "these third-party loggers are INFO-enabled, so anything they print -- "
        f"request URLs included -- reaches the log: {offenders}"
    )


def test_third_party_warnings_still_reach_the_configured_handler():
    """The floor must not become a gag: WARNING and above are why we log at all."""
    stream = io.StringIO()
    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        logging.getLogger("httpx2").warning("a library problem worth seeing")
        logging.getLogger("urllib3.poolmanager").error("a library error worth seeing")

    emitted = stream.getvalue()
    assert "a library problem worth seeing" in emitted
    assert "a library error worth seeing" in emitted


def test_turning_the_knob_below_the_floor_also_quiets_third_party():
    """``max(level, floor)``: UMS_LOG_LEVEL=ERROR must not keep printing library warnings.

    A hard WARNING floor would ignore the operator turning verbosity DOWN and
    read as a broken knob. The floor is a ceiling on verbosity, not a fixed
    level.
    """
    stream = io.StringIO()
    with production_shaped_root() as root:
        configure_logging(level="ERROR", stream=stream)

        assert root.level == logging.ERROR
        logging.getLogger("httpx2").warning("library warning below the knob")
        logging.getLogger("httpx2").error("library error at the knob")

    emitted = stream.getvalue()
    assert "library warning below the knob" not in emitted
    assert "library error at the knob" in emitted


def test_first_party_logger_name_is_the_application_package():
    """The derived constant must resolve to the package every module logs under."""
    assert FIRST_PARTY_LOGGER_NAME == "ums_smart_revenue"
    for name in MODULE_LOGGER_NAMES:
        assert name.split(".")[0] == FIRST_PARTY_LOGGER_NAME, name


# ---------------------------------------------------------------------------
# The format: timestamp, level, logger name.
# ---------------------------------------------------------------------------


def test_log_format_declares_timestamp_level_and_logger_name():
    """The three fields P0.6 requires are present in the format string."""
    assert "%(asctime)s" in LOG_FORMAT
    assert "%(levelname)" in LOG_FORMAT
    assert "%(name)s" in LOG_FORMAT


def test_log_lines_carry_a_utc_millisecond_timestamp_level_and_logger_name():
    """A real emitted line parses into the four documented fields."""
    stream = io.StringIO()
    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        _probe().warning("placed in time")

    line = stream.getvalue().strip()
    match = _LOG_LINE_PATTERN.match(line)
    assert match is not None, line
    assert match.group("level") == "WARNING"
    assert match.group("name") == _PROBE_LOGGER
    assert match.group("message") == "placed in time"

    stamp = datetime.strptime(match.group("stamp"), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 60, (
        "timestamps must be UTC; a local-time stamp lands hours away"
    )


def test_build_log_formatter_uses_utc_without_mutating_the_formatter_class():
    """``converter`` is shadowed on the instance, not assigned on the class."""
    formatter = build_log_formatter()

    assert formatter.converter is time.gmtime
    assert logging.Formatter.converter is time.localtime


# ============================================================================
# Purpose: Prove the installed formatter removes credential-shaped values from
#   ordinary messages and chained/cached tracebacks while retaining surrounding
#   diagnostics.
# Database/ORM: None.
# Standards: Exercise the real configured handler, stdlib exception rendering,
#   shared ``LogRecord.exc_text`` caching, case-insensitive matching, and an
#   already-redacted value. No production helper is used to build expectations.
# Blast Radius: Test-only; logging output contract.
# Connections:
#   - File: backend/ums_smart_revenue/config/logging_config.py ->
#     build_log_formatter and configure_logging own post-render redaction.
# ============================================================================
def test_configured_handler_redacts_credentials_and_preserves_safe_context():
    """Every supported credential shape is removed from a real emitted line."""
    stream = io.StringIO()
    message = (
        "db=postgresql://db-user:url-password@db.internal/revenue "
        "url=https://storage.test/object?x-AmZ-SiGnAtUrE=query-signature&report=august "
        "aUtHoRiZaTiOn: Bearer header-authorization, trace_id=trace-123, "
        "Cookie='session=cookie-value; theme=blue', "
        "X-API-Key=header-api-key, "
        "db_password=assignment-password "
        "clientSecret='assignment-token' "
        "password_policy=rotate token_count=7 safe_note=keep-me "
        "token=[REDACTED]"
    )

    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        _probe().warning("%s", message)

    output = stream.getvalue()
    for secret in (
        "db-user",
        "url-password",
        "query-signature",
        "header-authorization",
        "cookie-value",
        "header-api-key",
        "assignment-password",
        "assignment-token",
    ):
        assert secret not in output
    for safe_text in (
        "db.internal/revenue",
        "report=august",
        "password_policy=rotate",
        "token_count=7",
        "trace_id=trace-123",
        "safe_note=keep-me",
    ):
        assert safe_text in output
    assert output.count("[REDACTED]") == 8
    assert "[[REDACTED]]" not in output


def test_configured_handler_redacts_a_chained_traceback():
    """Message and every exception in a chain are sanitized after rendering."""
    stream = io.StringIO()

    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        try:
            try:
                raise ValueError("password=inner-password safe_inner=kept")
            except ValueError as cause:
                raise RuntimeError(
                    "Authorization='Bearer outer-authorization' safe_outer=kept"
                ) from cause
        except RuntimeError:
            _probe().exception("connector failed token=message-token safe_message=kept")

    output = stream.getvalue()
    assert "inner-password" not in output
    assert "outer-authorization" not in output
    assert "message-token" not in output
    assert "ValueError: password=[REDACTED] safe_inner=kept" in output
    assert "RuntimeError: Authorization='[REDACTED]' safe_outer=kept" in output
    assert "connector failed token=[REDACTED] safe_message=kept" in output
    assert "The above exception was the direct cause" in output


def test_configured_handler_redacts_google_signing_values_from_traceback():
    """Signed-URL credentials are removed from query and header reprs."""
    stream = io.StringIO()
    unsafe_error = (
        "request=https://trace-user:trace-password@storage.test/object?"
        "X-Goog-Credential=query-credential&"
        "X-Goog-Signature=query-signature&safe_query=kept "
        "headers={'X-Goog-Credential': 'dict-credential', "
        "'X-Goog-Signature': 'dict-signature', 'X-Request-ID': 'dict-safe'} "
        "header_pairs=[('X-Goog-Credential', 'tuple-credential'), "
        "('X-Goog-Signature', 'tuple-signature'), "
        "('X-Request-ID', 'tuple-safe')]"
    )

    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        try:
            raise RuntimeError(unsafe_error)
        except RuntimeError:
            _probe().exception("signed request failed safe_message=kept")

    output = stream.getvalue()
    for secret in (
        "trace-user",
        "trace-password",
        "query-credential",
        "query-signature",
        "dict-credential",
        "dict-signature",
        "tuple-credential",
        "tuple-signature",
    ):
        assert secret not in output
    for safe_text in (
        "storage.test/object",
        "safe_query=kept",
        "X-Goog-Credential",
        "X-Goog-Signature",
        "dict-safe",
        "tuple-safe",
        "signed request failed safe_message=kept",
    ):
        assert safe_text in output


@pytest.mark.parametrize(
    ("unsafe", "safe_fragment"),
    [
        (
            "https://youtube.test/v2/jobs?onBehalfOfContentOwner=GuardedOwner123&safe=kept",
            "onBehalfOfContentOwner=[REDACTED]&safe=kept",
        ),
        (
            "https://youtube.test/v2/reports?ids=contentOwner%3D%3DGuardedOwner123&safe=kept",
            "ids=contentOwner%3D%3D[REDACTED]&safe=kept",
        ),
        (
            "ids=contentOwner%253D%253DGuardedOwner123",
            "ids=contentOwner%253D%253D[REDACTED]",
        ),
        ("ids=contentOwner==GuardedOwner123", "ids=contentOwner==[REDACTED]"),
        (
            "{'ids': 'contentOwner==GuardedOwner123'}",
            "{'ids': 'contentOwner==[REDACTED]'}",
        ),
        (
            "{'onBehalfOfContentOwner': 'GuardedOwner123'}",
            "{'onBehalfOfContentOwner': '[REDACTED]'}",
        ),
        (
            "[('onBehalfOfContentOwner', 'GuardedOwner123')]",
            "[('onBehalfOfContentOwner', '[REDACTED]')]",
        ),
        ("contentOwner=GuardedOwner123", "contentOwner=[REDACTED]"),
        ("content_owner_id=GuardedOwner123", "content_owner_id=[REDACTED]"),
        ("contentOwnerId=GuardedOwner123", "contentOwnerId=[REDACTED]"),
        (
            "on_behalf_of_content_owner=GuardedOwner123",
            "on_behalf_of_content_owner=[REDACTED]",
        ),
    ],
)
def test_logging_redacts_guarded_content_owner_query_forms(
    unsafe: str,
    safe_fragment: str,
) -> None:
    """Every live Google owner alias is redacted idempotently."""
    sanitized = redact_sensitive_text(unsafe)
    assert "GuardedOwner123" not in sanitized
    assert safe_fragment in sanitized
    assert redact_sensitive_text(sanitized) == sanitized


def test_logging_preserves_non_owner_selectors_and_counts() -> None:
    """Owner redaction does not erase generic owners, channels, or counts."""
    safe = "owner=operator ids=channel==UC-safe content_owner_count=3"
    assert redact_sensitive_text(safe) == safe


def test_configured_handler_redacts_content_owner_from_exception_chain() -> None:
    """Raw and encoded CMS owner query forms never survive traceback rendering."""
    stream = io.StringIO()
    owner_id = "GuardedOwnerTraceback123"

    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        try:
            raise RuntimeError(
                "https://youtube.test/reports?"
                f"onBehalfOfContentOwner={owner_id}&"
                f"ids=contentOwner%3D%3D{owner_id}&safe=kept"
            )
        except RuntimeError:
            _probe().exception("unexpected Google request failure")

    output = stream.getvalue()
    assert owner_id not in output
    assert "onBehalfOfContentOwner=[REDACTED]" in output
    assert "ids=contentOwner%3D%3D[REDACTED]" in output
    assert "safe=kept" in output


def test_handlers_added_after_configuration_and_output_release_are_redacted() -> None:
    """The safety lease covers runtime handler additions before first dispatch."""
    first_stream = io.StringIO()
    second_stream = io.StringIO()
    first_handler = logging.StreamHandler(first_stream)
    second_handler = logging.StreamHandler(second_stream)
    formatter = logging.Formatter("%(message)s owner=%(onBehalfOfContentOwner)s nested=%(context)s")
    first_handler.setFormatter(formatter)
    second_handler.setFormatter(formatter)
    late_logger = logging.getLogger("ums_smart_revenue.late_handler_probe")
    late_logger.setLevel(logging.ERROR)
    late_logger.propagate = False
    original_dispatch = logging.Logger.callHandlers
    configuration = configure_logging(level="ERROR", stream=io.StringIO())
    try:
        late_logger.addHandler(first_handler)
        try:
            raise RuntimeError("https://youtube.test/x?ids=contentOwner%3D%3DLateOwnerOne")
        except RuntimeError:
            late_logger.exception(
                "late handler exception",
                extra={
                    "onBehalfOfContentOwner": "LateOwnerOne",
                    "context": {"contentOwnerId": "LateOwnerOne"},
                },
            )
        assert "LateOwnerOne" not in first_stream.getvalue()
        assert first_stream.getvalue().count("[REDACTED]") >= 3

        release_logging_output(configuration)
        late_logger.addHandler(second_handler)
        late_logger.error(
            "after output release content_owner_id=LateOwnerTwo",
            extra={
                "onBehalfOfContentOwner": "LateOwnerTwo",
                "context": {"ids": "contentOwner==LateOwnerTwo"},
            },
        )
        assert "LateOwnerTwo" not in second_stream.getvalue()
        assert "[REDACTED]" in second_stream.getvalue()
    finally:
        restore_logging(configuration)

    assert logging.Logger.callHandlers is original_dispatch


def test_formatter_replaces_a_precached_raw_exception_and_is_idempotent():
    """A formatter that ran first cannot leave raw credentials in exc_text."""
    try:
        raise RuntimeError("client_secret=cached-secret safe_cache=kept")
    except RuntimeError:
        record = logging.LogRecord(
            name=_PROBE_LOGGER,
            level=logging.ERROR,
            pathname=__file__,
            lineno=0,
            msg="cached exception",
            args=(),
            exc_info=sys.exc_info(),
        )

    logging.Formatter("%(message)s").format(record)
    assert record.exc_text is not None
    assert "cached-secret" in record.exc_text

    formatter = build_log_formatter()
    first = formatter.format(record)
    second = formatter.format(record)

    assert record.exc_text is not None
    assert "cached-secret" not in record.exc_text
    assert "client_secret=[REDACTED] safe_cache=kept" in record.exc_text
    assert "cached-secret" not in first
    assert first == second
    assert "[[REDACTED]]" not in second


def test_logging_redacts_encrypted_private_key_blocks() -> None:
    """PKCS#8 encrypted PEM material is covered alongside plain private keys."""
    stream = io.StringIO()
    private_key = (
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
        "literal-encrypted-private-material\n"
        "-----END ENCRYPTED PRIVATE KEY-----"
    )

    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        _probe().warning("key material: %s", private_key)

    output = stream.getvalue()
    assert "literal-encrypted-private-material" not in output
    assert "BEGIN ENCRYPTED PRIVATE KEY" not in output
    assert "[REDACTED-PRIVATE-KEY]" in output


def test_database_exception_detection_covers_exact_psycopg_module_and_base() -> None:
    """Exact psycopg.Error and application subclasses retain class names only."""
    from psycopg import Error as PsycopgError

    exact_error = PsycopgError("CALL private.rotate_key('exact-module-secret')")
    assert type(exact_error).__module__ == "psycopg"
    exact_summary = redact_exception_summary(exact_error)
    assert exact_summary == "Error"
    assert "exact-module-secret" not in exact_summary

    class ApplicationDatabaseError(PsycopgError):
        """Application wrapper whose module no longer starts with psycopg."""

    wrapped_error = ApplicationDatabaseError(
        "SELECT private_value FROM accounts WHERE token='base-class-secret'"
    )
    assert type(wrapped_error).__module__ == __name__
    wrapped_summary = redact_exception_summary(wrapped_error)
    assert wrapped_summary == "ApplicationDatabaseError"
    assert "base-class-secret" not in wrapped_summary


def test_sql_redaction_covers_call_without_treating_prose_as_sql() -> None:
    """CALL statements redact, while literal SQL-like English stays intact."""
    unsafe = "CALL finance.rotate_key('literal-call-secret')"
    redacted = redact_sensitive_text(unsafe)
    assert redacted == "[REDACTED-SQL]"
    assert "literal-call-secret" not in redacted

    safe_literals = (
        "Please call support and select the retry path from the runbook.",
        "CALL support before retrying",
        "SELECT the report FROM the menu",
        "update the scheduler set after review",
        "delete from the queue after review",
        "statement cache healthy; query_count=4",
    )
    for literal in safe_literals:
        assert redact_sensitive_text(literal) == literal


def test_logging_escapes_c1_nel_and_unicode_line_separators() -> None:
    """C1/NEL/U+2028/U+2029 cannot forge additional log lines."""
    raw = "left\x80middle\x85right\x9f\u2028next\u2029last"
    sanitized = redact_sensitive_text(raw)

    for character in ("\x80", "\x85", "\x9f", "\u2028", "\u2029"):
        assert character not in sanitized
    for escaped in (r"\x80", r"\x85", r"\x9f", r"\u2028", r"\u2029"):
        assert escaped in sanitized


def test_logging_redacts_nested_structured_keys_and_ignores_spoofed_marker() -> None:
    """Camel/snake structured keys cannot bypass a foreign formatter."""
    root = logging.getLogger()
    output = io.StringIO()
    structured = logging.StreamHandler(output)
    structured.setFormatter(
        logging.Formatter(
            "%(message)s|%(payload)s|%(clientSecret)s|%(rawSQL)s|"
            "%(password_policy)s|%(query_count)s"
        )
    )
    root.addHandler(structured)
    try:
        configuration = configure_logging(level="INFO", stream=io.StringIO())
        _probe().warning(
            "structured token=message-secret",
            extra={
                "_ums_log_redacted": True,
                "clientSecret": "top-level-secret",
                "rawSQL": "CALL private.rotate_key('top-level-sql-secret')",
                "password_policy": "rotate",
                "query_count": 4,
                "payload": {
                    "apiKey": "nested-api-secret",
                    "databaseStatement": "SELECT private FROM accounts WHERE id=7",
                    "items": [{"refreshToken": "deep-refresh-secret"}],
                    "password_policy": "keep-safe-policy",
                    "query_count": 9,
                    "possession": "keep-possession",
                    "monkey": "keep-monkey",
                },
            },
        )
        restore_logging(configuration)
    finally:
        root.removeHandler(structured)
        structured.close()

    emitted = output.getvalue()
    for secret in (
        "message-secret",
        "top-level-secret",
        "top-level-sql-secret",
        "nested-api-secret",
        "deep-refresh-secret",
        "SELECT private",
    ):
        assert secret not in emitted
    assert "[REDACTED]" in emitted
    assert "[REDACTED-SQL]" in emitted
    assert "|rotate|4" in emitted
    assert "'query_count': '9'" in emitted
    assert "keep-safe-policy" in emitted
    assert "keep-possession" in emitted
    assert "keep-monkey" in emitted


def test_log_lines_carry_the_thread_name():
    """The connector executor and the scheduler both log from worker threads."""
    stream = io.StringIO()
    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        _probe().info("from a thread")

    match = _LOG_LINE_PATTERN.match(stream.getvalue().strip())
    assert match is not None
    assert match.group("thread") == "MainThread"


# ---------------------------------------------------------------------------
# Install / idempotence / release.
#
# A 30-mutation matrix over config/logging_config.py left three survivors
# against this file. One -- deleting ``configuration.handler.close()`` -- was a
# real gap and is now killed by the ``getHandlerByName`` assertion in
# ``test_restore_logging_puts_both_levels_back_where_it_found_them``. The other
# two are kept and argued rather than tested, because each names a state no
# caller can reach:
#
#   * ``if handler not in root.handlers:`` -> ``if True:``. EQUIVALENT. On the
#     production path basicConfig has already appended the handler and set the
#     root level to the same ``root_level``, and CPython's ``Logger.addHandler``
#     is ``if not (hdlr in self.handlers): self.handlers.append(hdlr)`` -- both
#     statements in the branch are idempotent, so running them unconditionally
#     changes nothing. Verified against the installed interpreter, not from
#     memory. The condition is kept because it states the intent: this branch is
#     the embedded fallback, not the normal path.
#   * ``if not configuration.installed or configuration.handler is None:`` ->
#     ``if configuration.handler is None:``. EQUIVALENT for every token this
#     module produces: ``configure_logging`` returns exactly two shapes,
#     ``(installed=False, handler=None)`` and ``(installed=True, handler=<h>)``,
#     so the two conjuncts co-vary. Only a hand-built
#     ``LoggingConfiguration(installed=False, handler=<h>)`` separates them, and
#     writing a test around that would pin an unreachable state rather than a
#     behaviour. The conjunct is kept as the honest reading of the field.
# ---------------------------------------------------------------------------


def test_configure_logging_installs_exactly_one_named_root_handler():
    """The production path: basicConfig owns an empty root logger.

    CORRECTED (see the third-party-floor section below): this test used to
    assert ``root.level == logging.INFO``. That premise -- that the
    application verbosity knob belongs on the ROOT logger -- is the defect,
    not the contract: it handed INFO to every library in the dependency set,
    and httpx2's INFO request line then published the CMS content-owner id.
    The assertion is not loosened, it is redirected and doubled: the level
    must land on the first-party logger AND the root logger must sit at the
    third-party floor.
    """
    with production_shaped_root() as root:
        configure_logging(level="INFO", stream=io.StringIO())

        named = [h for h in root.handlers if h.get_name() == UMS_LOG_HANDLER_NAME]
        assert len(named) == 1
        assert root.handlers == named
        assert root.level == THIRD_PARTY_LOG_LEVEL
        assert logging.getLogger(FIRST_PARTY_LOGGER_NAME).level == logging.INFO


def test_configure_logging_reuses_handler_without_duplicate_output():
    """A second level lease reuses the handler, so lines are never doubled."""
    first_stream = io.StringIO()
    second_stream = io.StringIO()

    with production_shaped_root() as root:
        first = configure_logging(level="INFO", stream=first_stream)
        second = configure_logging(level="DEBUG", stream=second_stream)

        assert first.installed is True
        assert second.installed is False
        named = [h for h in root.handlers if h.get_name() == UMS_LOG_HANDLER_NAME]
        assert len(named) == 1

        _probe().warning("emitted once")

    assert first_stream.getvalue().count("emitted once") == 1
    assert second_stream.getvalue() == ""


@pytest.mark.parametrize(
    "embedding_root_level",
    [logging.NOTSET, logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR],
    ids=["notset", "debug", "info", "warning", "error"],
)
def test_configure_logging_applies_the_floor_when_root_already_owns_a_handler(
    embedding_root_level,
):
    """``basicConfig`` no-ops on a non-empty root; the FLOOR must still land.

    This is the embedded path (pytest, or an operator ``--log-config``). A
    foreign handler must never be displaced, and ``root.setLevel(root_level)``
    is the line that stops the embedding process's own root level standing in
    for the third-party floor.

    REWRITTEN, because the previous version could not see that line go. It set
    ``root.setLevel(logging.WARNING)`` before calling -- which is already the
    floor -- so the assignment under test was a no-op, and its only behavioural
    assertion was a FIRST-party INFO record, which lands whatever root's level
    is (logger levels gate the originating logger only; ``callHandlers`` then
    consults handler levels, never ancestor logger levels). Deleting
    ``root.setLevel(root_level)`` left 45 passed.
    ``embedding_root_level`` is therefore parametrized across the floor: below
    it the deletion hands every third-party logger the application level -- the
    CMS content-owner id back in httpx2's request line, which is the whole
    reason ``THIRD_PARTY_LOG_LEVEL`` exists -- and above it the deletion
    silently drops library WARNINGs.
    """
    root = logging.getLogger()
    foreign = logging.StreamHandler(io.StringIO())
    root.addHandler(foreign)
    root.setLevel(embedding_root_level)

    stream = io.StringIO()
    configuration = configure_logging(level="INFO", stream=stream)

    assert configuration.installed is True
    assert foreign in root.handlers, "a foreign root handler must never be displaced"
    assert root.level == THIRD_PARTY_LOG_LEVEL, (
        "the embedding process's root level must not stand in for the floor"
    )

    newcomer = logging.getLogger("a_dependency_added_next_year.client")
    assert newcomer.getEffectiveLevel() == THIRD_PARTY_LOG_LEVEL
    assert newcomer.isEnabledFor(logging.INFO) is False

    logging.getLogger("httpx2").info(
        "HTTP Request: GET https://youtubeanalytics.googleapis.com/v2/reports"
        "?ids=contentOwner%%3D%%3D%s",
        _FAKE_CONTENT_OWNER_ID,
    )
    logging.getLogger("httpx2").warning("a library problem worth seeing")
    _probe().info("info under a foreign handler")

    emitted = stream.getvalue()
    assert "info under a foreign handler" in emitted, "the application level must still land"
    assert _FAKE_CONTENT_OWNER_ID not in emitted, emitted
    assert "a library problem worth seeing" in emitted, "the floor is not a gag"


@pytest.mark.parametrize(
    ("previous_root_level", "previous_first_party_level", "configured"),
    [
        (logging.NOTSET, logging.NOTSET, "DEBUG"),
        (logging.DEBUG, logging.WARNING, "DEBUG"),
        (logging.INFO, logging.NOTSET, "INFO"),
        (logging.CRITICAL, logging.DEBUG, "INFO"),
    ],
    ids=["notset", "debug", "info", "critical"],
)
def test_restore_logging_puts_both_levels_back_where_it_found_them(
    previous_root_level, previous_first_party_level, configured
):
    """The undo token puts process-global logging state back exactly.

    REWRITTEN, and this one is a guard loss the previous fix's own "correction"
    introduced. That version ran inside ``production_shaped_root()``, which
    pins root at WARNING, and then asserted ``root.level == logging.WARNING``
    after the restore -- but ``configure_logging`` leaves root at
    ``max(level, THIRD_PARTY_LOG_LEVEL)``, which for every level it was called
    with IS WARNING. Before and after were the same number, so deleting
    ``root.setLevel(configuration.previous_root_level)`` left 45 passed.

    The fixtures below are chosen so the level configure_logging leaves is
    never the level it found: root is restored DOWNWARDS (NOTSET/DEBUG/INFO ->
    30 -> back) and UPWARDS (CRITICAL -> 30 -> back). ``create_app`` is a
    factory the suite calls hundreds of times, so a level that does not come
    back is process-global state one app construction leaks into the next.
    """
    expected_root = max(logging.getLevelNamesMapping()[configured], THIRD_PARTY_LOG_LEVEL)
    first_party = logging.getLogger(FIRST_PARTY_LOGGER_NAME)

    with production_shaped_root() as root:
        root.setLevel(previous_root_level)
        first_party.setLevel(previous_first_party_level)

        configuration = configure_logging(level=configured, stream=io.StringIO())
        assert installed_log_handler() is not None
        assert logging.getHandlerByName(UMS_LOG_HANDLER_NAME) is configuration.handler
        assert configuration.previous_root_level == previous_root_level
        assert root.level == expected_root != previous_root_level, (
            "the premise: the restore has somewhere to restore FROM"
        )
        assert first_party.level == logging.getLevelNamesMapping()[configured]

        restore_logging(configuration)

        assert installed_log_handler() is None
        assert root.handlers == []
        assert root.level == previous_root_level
        assert first_party.level == previous_first_party_level
        # ``handler.close()`` is the other half of the release, and the only
        # thing it does that anything can see: it deregisters the handler from
        # the process-wide name table. That NAME is this module's entire
        # identity mechanism (``installed_log_handler`` matches on it), so a
        # factory that let a handler go while the table still resolves the name
        # to it is holding exactly the global state restore_logging exists to
        # give back. ``getHandlerByName`` is public API since 3.12.
        assert logging.getHandlerByName(UMS_LOG_HANDLER_NAME) is None


def test_restore_logging_of_a_noop_configuration_keeps_the_install():
    """Only the final lease release may remove the shared handler."""
    with production_shaped_root():
        first = configure_logging(level="INFO", stream=io.StringIO())
        second = configure_logging(level="INFO", stream=io.StringIO())

        restore_logging(second)
        assert installed_log_handler() is not None

        restore_logging(first)
        assert installed_log_handler() is None


def test_overlapping_logging_leases_survive_first_shutdown():
    """First app shutdown must not silence a still-active second lifespan."""
    with production_shaped_root():
        first = configure_logging(level="INFO", stream=io.StringIO())
        second = configure_logging(level="INFO", stream=io.StringIO())

        restore_logging(first)
        assert installed_log_handler() is not None

        restore_logging(second)
        assert installed_log_handler() is None


def test_surviving_logging_lease_recomputes_its_effective_level() -> None:
    """Releasing one token cannot leave another token's verbosity behind."""
    with production_shaped_root() as root:
        first_party = logging.getLogger(FIRST_PARTY_LOGGER_NAME)
        info_lease = configure_logging(level="INFO", stream=io.StringIO())
        debug_lease = configure_logging(level="DEBUG", stream=io.StringIO())
        error_lease = configure_logging(level="ERROR", stream=io.StringIO())

        assert first_party.level == logging.DEBUG
        assert root.level == THIRD_PARTY_LOG_LEVEL

        restore_logging(debug_lease)
        assert first_party.level == logging.INFO
        assert root.level == THIRD_PARTY_LOG_LEVEL

        restore_logging(info_lease)
        assert first_party.level == logging.ERROR
        assert root.level == logging.ERROR

        restore_logging(error_lease)
        assert installed_log_handler() is None


def test_output_release_retains_redaction_safety_until_explicit_termination() -> None:
    """Foreign handlers stay sanitized after output ownership is released."""
    root = logging.getLogger()
    foreign_output = io.StringIO()
    foreign = logging.StreamHandler(foreign_output)
    foreign.setFormatter(logging.Formatter("%(message)s|%(payload)s"))
    root.addHandler(foreign)
    original_filters = foreign.filters[:]
    try:
        configuration = configure_logging(level="INFO", stream=io.StringIO())
        owned = configuration.handler
        assert owned is not None

        # Safety cannot be released out of order.
        release_logging_safety(configuration)
        assert configuration.safety_released is False

        release_logging_output(configuration)
        assert configuration.output_released is True
        assert configuration.safety_released is False
        assert owned not in root.handlers
        assert foreign.filters != original_filters

        logging.getLogger(FIRST_PARTY_LOGGER_NAME).warning(
            "late password=late-worker-secret",
            extra={"payload": {"clientSecret": "late-structured-secret"}},
        )
        emitted = foreign_output.getvalue()
        assert "late-worker-secret" not in emitted
        assert "late-structured-secret" not in emitted
        assert emitted.count("[REDACTED]") >= 2

        release_logging_safety(configuration)
        assert configuration.released is True
        assert foreign.filters == original_filters
        assert installed_log_handler() is None
    finally:
        root.removeHandler(foreign)
        foreign.close()


def test_output_release_keeps_production_last_resort_redacted(monkeypatch) -> None:
    """A late warning stays safe when the UMS handler was the only root output."""
    late_output = io.StringIO()
    last_resort = logging.StreamHandler(late_output)
    last_resort.setLevel(logging.WARNING)
    last_resort.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(logging, "lastResort", last_resort)

    with production_shaped_root() as root:
        configuration = configure_logging(level="INFO", stream=io.StringIO())
        release_logging_output(configuration)
        assert root.handlers == []

        logging.getLogger(FIRST_PARTY_LOGGER_NAME).warning("late audit password=last-resort-secret")
        emitted = late_output.getvalue()
        assert "last-resort-secret" not in emitted
        assert "password=[REDACTED]" in emitted

        release_logging_safety(configuration)


def test_public_handler_name_collision_preserves_foreign_registration() -> None:
    """A foreign same-named handler is neither adopted nor closed."""
    root = logging.getLogger()
    foreign = logging.StreamHandler(io.StringIO())
    foreign.set_name(UMS_LOG_HANDLER_NAME)
    root.addHandler(foreign)
    try:
        configuration = configure_logging(level="INFO", stream=io.StringIO())
        owned = installed_log_handler()
        assert owned is not None
        assert owned is not foreign
        assert owned.get_name().startswith(UMS_LOG_HANDLER_NAME + "-")
        assert logging.getHandlerByName(UMS_LOG_HANDLER_NAME) is foreign

        restore_logging(configuration)

        assert foreign in root.handlers
        assert logging.getHandlerByName(UMS_LOG_HANDLER_NAME) is foreign
    finally:
        root.removeHandler(foreign)
        foreign.close()


def test_foreign_filter_marker_collision_is_removed_only_by_identity() -> None:
    """A lookalike operator filter survives UMS safety-filter teardown."""

    class _ForeignFilter(logging.Filter):
        _ums_smart_revenue_filter_id = "redaction-v2"

        def filter(self, _record: logging.LogRecord) -> bool:
            return True

    root = logging.getLogger()
    handler = logging.StreamHandler(io.StringIO())
    foreign_filter = _ForeignFilter()
    handler.addFilter(foreign_filter)
    root.addHandler(handler)
    try:
        configuration = configure_logging(level="INFO", stream=io.StringIO())
        assert foreign_filter in handler.filters
        restore_logging(configuration)
        assert handler.filters == [foreign_filter]
    finally:
        root.removeHandler(handler)
        handler.close()


def test_double_release_of_one_token_does_not_silence_a_live_lease():
    """A token releases at most once, so a repeat cannot spend another lease.

    The refcount is process-wide, so a second ``restore_logging`` on the SAME
    token used to decrement again and consume the second lifespan's lease --
    dropping the count to zero, removing the shared handler and restoring the
    previous levels while that second app was still running. The existing
    ``refcount <= 0`` guard cannot see this: at the second call there IS still
    a lease outstanding, just not this token's.
    """
    with production_shaped_root() as root:
        shared_stream = io.StringIO()
        first = configure_logging(level="INFO", stream=shared_stream)
        second = configure_logging(level="INFO", stream=io.StringIO())
        configured_root_level = root.level

        restore_logging(first)
        # A ``finally`` that runs twice, or a belt-and-braces teardown.
        restore_logging(first)

        # The second lifespan is still live: the handler must survive...
        assert installed_log_handler() is not None
        # ...the levels must not have been rolled back under it...
        assert root.level == configured_root_level
        # ...and it must still actually DELIVER, which is what the caller loses.
        logging.getLogger(FIRST_PARTY_LOGGER_NAME).info("second app still logging")
        assert "second app still logging" in shared_stream.getvalue()

        # The surviving lease still owns the only remaining release.
        restore_logging(second)
        assert installed_log_handler() is None


def test_concurrent_duplicate_release_preserves_and_finally_closes_live_lease(
    monkeypatch,
):
    """Concurrent restores of one token cannot consume a second token's lease."""
    with production_shaped_root() as root:
        shared_stream = io.StringIO()
        first = configure_logging(level="INFO", stream=shared_stream)
        second = configure_logging(level="INFO", stream=io.StringIO())
        handler = installed_log_handler()
        assert handler is first.handler
        assert handler is not None

        close_calls = 0
        count_lock = threading.Lock()
        original_close = handler.close

        def counted_close() -> None:
            """Count closure of the shared handler without changing its behavior."""
            nonlocal close_calls
            with count_lock:
                close_calls += 1
            original_close()

        monkeypatch.setattr(handler, "close", counted_close)

        # Both callers reach restore_logging together. Its process lock must
        # serialize the released-flag check with the refcount decrement; an
        # unlocked check/set lets both callers spend a lease from one token.
        restore_barrier = threading.Barrier(2)
        failures: list[BaseException] = []

        def restore_first_token() -> None:
            """Rendezvous, then race a duplicate restore of the first token."""
            try:
                restore_barrier.wait(timeout=5)
                restore_logging(first)
            except BaseException as exc:  # pragma: no cover - assertion reports it
                failures.append(exc)

        restore_threads = [
            threading.Thread(target=restore_first_token),
            threading.Thread(target=restore_first_token),
        ]
        for thread in restore_threads:
            thread.start()
        for thread in restore_threads:
            thread.join(timeout=5)

        assert not failures
        assert all(not thread.is_alive() for thread in restore_threads)
        assert installed_log_handler() is handler
        assert handler in root.handlers
        assert close_calls == 0

        logging.getLogger(FIRST_PARTY_LOGGER_NAME).info("surviving concurrent lease still logging")
        assert "surviving concurrent lease still logging" in shared_stream.getvalue()

        restore_logging(second)
        assert installed_log_handler() is None
        assert handler not in root.handlers
        assert close_calls == 1

        # The surviving token is idempotent too: final cleanup stays exact.
        restore_logging(second)
        assert close_calls == 1


def test_configure_snapshots_levels_under_lock_against_concurrent_restore(
    monkeypatch,
):
    """Previous levels must be read inside the lock, not before waiting on it.

    Pause the final restore after it has acquired ``_logging_lock`` but before
    it restores levels. A concurrent configure that snapshots outside the lock
    would capture still-configured levels as "previous"; after restore puts the
    originals back, that stale snapshot would permanently lose ERROR/CRITICAL.
    """
    with production_shaped_root() as root:
        root.setLevel(logging.ERROR)
        first_party = logging.getLogger(FIRST_PARTY_LOGGER_NAME)
        first_party.setLevel(logging.CRITICAL)

        configuration = configure_logging(level="INFO", stream=io.StringIO())
        assert root.level == THIRD_PARTY_LOG_LEVEL

        restore_entered = threading.Event()
        release_restore = threading.Event()
        result: dict[str, object] = {}

        real_remove_handler = logging.Logger.removeHandler

        def gated_remove_handler(self: logging.Logger, handler: logging.Handler) -> None:
            """gated remove handler."""
            if self is logging.getLogger() and not restore_entered.is_set():
                restore_entered.set()
                assert release_restore.wait(timeout=5)
            real_remove_handler(self, handler)

        monkeypatch.setattr(logging.Logger, "removeHandler", gated_remove_handler)

        def run_restore() -> None:
            """run restore."""
            restore_logging(configuration)

        def run_configure() -> None:
            """run configure."""
            assert restore_entered.wait(timeout=5)
            # Give the buggy path a chance to snapshot outside the lock while
            # restore still holds it and levels are still the configured ones.
            time.sleep(0.05)
            result["cfg"] = configure_logging(level="DEBUG", stream=io.StringIO())

        restore_thread = threading.Thread(target=run_restore)
        configure_thread = threading.Thread(target=run_configure)
        restore_thread.start()
        assert restore_entered.wait(timeout=5)
        configure_thread.start()
        time.sleep(0.1)
        release_restore.set()
        restore_thread.join(timeout=5)
        configure_thread.join(timeout=5)

        cfg = result["cfg"]
        assert isinstance(cfg, logging_config.LoggingConfiguration)
        assert cfg.previous_root_level == logging.ERROR
        assert cfg.previous_first_party_level == logging.CRITICAL
        restore_logging(cfg)
        assert installed_log_handler() is None
        assert root.level == logging.ERROR
        assert first_party.level == logging.CRITICAL


def test_reload_readopts_orphaned_handler_without_silencing_prior_lease():
    """After module globals reset, a new configure/restore must not strip a prior install.

    ``importlib.reload`` clears ``_logging_state`` while the named UMS handler can
    still sit on the root logger. Re-adopt that orphan with a synthetic lease so
    a post-reload configure/restore pair cannot silence the pre-reload owner.
    Re-adoption must also recover the pre-install levels stored on the handler —
    snapshotting the already-configured WARNING/INFO as "previous" would leave
    those floors after every lease ends.
    """
    with production_shaped_root() as root:
        first_party = logging.getLogger(FIRST_PARTY_LOGGER_NAME)
        root.setLevel(logging.ERROR)
        first_party.setLevel(logging.CRITICAL)

        first = configure_logging(level="INFO", stream=io.StringIO())
        handler_before = installed_log_handler()
        assert handler_before is not None
        assert logging_config._logging_state.refcount == 1
        assert root.level == THIRD_PARTY_LOG_LEVEL
        assert first_party.level == logging.INFO

        # Simulate importlib.reload clearing module globals only.
        logging_config._logging_state = logging_config._LoggingLeaseState()
        assert logging_config._logging_state.refcount == 0
        assert installed_log_handler() is handler_before

        second = configure_logging(level="DEBUG", stream=io.StringIO())
        assert second.installed is False
        restore_logging(second)

        # Synthetic pre-reload lease keeps the orphaned handler alive.
        assert installed_log_handler() is handler_before
        assert logging_config._logging_state.refcount == 1

        # Drain the synthetic lease the way a pre-reload lifespan finally would.
        restore_logging(first)
        assert installed_log_handler() is None
        assert root.level == logging.ERROR
        assert first_party.level == logging.CRITICAL


# ---------------------------------------------------------------------------
# The app wiring, and the order-independence it buys.
# ---------------------------------------------------------------------------


def test_app_lifespan_configures_logging_and_releases_it_on_shutdown():
    """Startup installs; shutdown restores. Importing the app changes nothing.

    ``app = create_app()`` runs at module import, so configuring inside
    ``create_app`` would reconfigure the logging of any process that imports
    the module -- the pytest suite included. Configuring in the lifespan and
    releasing on shutdown keeps the factory free of permanent global state.
    """
    with production_shaped_root() as root:
        application = create_app()
        assert installed_log_handler() is None, "constructing an app must not configure logging"

        with TestClient(application) as client:
            assert installed_log_handler() is not None
            # CORRECTED: was ``root.level == logging.INFO``. The lifespan
            # still applies UMS_LOG_LEVEL -- to the first-party logger.
            assert root.level == THIRD_PARTY_LOG_LEVEL
            assert logging.getLogger(FIRST_PARTY_LOGGER_NAME).level == logging.INFO
            assert client.get("/health").status_code == 200

        assert installed_log_handler() is None
        assert root.handlers == []
        assert root.level == logging.WARNING


# ---------------------------------------------------------------------------
# The load-bearing UMS_AUTHZ_SOURCE=database warning (plan P2 gate).
# ---------------------------------------------------------------------------


def test_unknown_principal_warning_reaches_the_configured_handler(monkeypatch):
    """A wrong ``X-User-ID`` under database authz must stop being an empty log.

    The plan gates ``UMS_AUTHZ_SOURCE=database`` behind P0.6 because the
    failure is a blank "Access denied" screen with nothing in the log. This
    drives the real ``current_principal_from_database`` rejection branch
    (``api/dependencies.py:203-208``) and asserts the warning lands on the
    configured handler, timestamped and attributed.
    """

    class RejectingLoader:
        """Principal loader double that fails the way an unknown id fails."""

        def __init__(self, _session: object) -> None:
            """Accept and ignore the request session."""

        @staticmethod
        def load(*, user_id: str, tenant_id: str) -> None:
            """Raise the error the SQL loader raises for an unknown user."""
            raise PrincipalNotFoundError(f"no stored user {user_id} in tenant {tenant_id}")

    monkeypatch.setattr(
        "ums_smart_revenue.api.dependencies.SqlAlchemyPrincipalLoader", RejectingLoader
    )

    now = datetime.now(UTC)
    tenant = Tenant(
        id=UUID(UMS_TENANT_ID),
        slug="ums",
        display_name="UMS",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )
    stream = io.StringIO()

    with production_shaped_root():
        configure_logging(level="INFO", stream=stream)
        token = TENANT_CTX.set(tenant)
        try:
            identity = TrustedGatewayIdentity(user_id=str(uuid4()))
            with pytest.raises(HTTPException) as raised:
                current_principal_from_database(identity, object())
        finally:
            TENANT_CTX.reset(token)

    assert raised.value.status_code == 403
    assert raised.value.detail == "Forbidden"

    line = stream.getvalue().strip()
    match = _LOG_LINE_PATTERN.match(line)
    assert match is not None, line
    assert match.group("level") == "WARNING"
    assert match.group("name") == "ums_smart_revenue.api.dependencies"
    assert match.group("message") == "Database principal lookup rejected unknown principal"


# ---------------------------------------------------------------------------
# UMS_LOG_LEVEL parsing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [("debug", "DEBUG"), ("  warning  ", "WARNING"), ("ERROR", "ERROR"), ("Critical", "CRITICAL")],
)
def test_load_app_settings_parses_the_log_level(monkeypatch, token, expected):
    """Level names are case- and whitespace-insensitive, stored upper-case."""
    monkeypatch.setenv(LOG_LEVEL_ENV, token)
    load_app_settings.cache_clear()

    assert load_app_settings().log_level == expected


@pytest.mark.parametrize("token", [None, "", "   "])
def test_load_app_settings_defaults_the_log_level(monkeypatch, token):
    """Missing or blank falls back to INFO, matching the _load_int contract."""
    if token is None:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    else:
        monkeypatch.setenv(LOG_LEVEL_ENV, token)
    load_app_settings.cache_clear()

    assert load_app_settings().log_level == DEFAULT_LOG_LEVEL == "INFO"


@pytest.mark.parametrize("token", ["verbose", "WARN", "FATAL", "20", "trace", "notset"])
def test_load_app_settings_rejects_an_unknown_log_level(monkeypatch, token):
    """A typo fails fast at boot instead of silently costing the INFO lines."""
    monkeypatch.setenv(LOG_LEVEL_ENV, token)
    load_app_settings.cache_clear()

    with pytest.raises(ValueError, match=LOG_LEVEL_ENV):
        load_app_settings()


def test_configure_logging_rejects_an_unknown_level_override():
    """A direct caller gets the same fail-fast the operator gets."""
    with production_shaped_root():
        with pytest.raises(ValueError, match=LOG_LEVEL_ENV):
            configure_logging(level="verbose", stream=io.StringIO())

        assert installed_log_handler() is None


@pytest.mark.parametrize("name", LOG_LEVEL_NAMES)
def test_every_allowed_level_name_maps_to_a_stdlib_level(name):
    """Each accepted name resolves to a real logging level.

    CORRECTED: was ``root.level == mapping[name]``. The name resolves on the
    FIRST-PARTY logger; root gets ``max(level, THIRD_PARTY_LOG_LEVEL)``.
    """
    expected = logging.getLevelNamesMapping()[name]

    with production_shaped_root() as root:
        configuration = configure_logging(level=name, stream=io.StringIO())

        assert logging.getLogger(FIRST_PARTY_LOGGER_NAME).level == expected
        assert root.level == max(expected, THIRD_PARTY_LOG_LEVEL)
        restore_logging(configuration)


def test_configure_logging_reads_the_level_from_settings(monkeypatch):
    """With no override, the installed level comes from UMS_LOG_LEVEL.

    CORRECTED: was ``root.level == logging.DEBUG``; the setting lands on the
    first-party logger.
    """
    monkeypatch.setenv(LOG_LEVEL_ENV, "debug")
    load_app_settings.cache_clear()

    with production_shaped_root() as root:
        configure_logging(stream=io.StringIO())

        assert logging.getLogger(FIRST_PARTY_LOGGER_NAME).level == logging.DEBUG
        assert root.level == THIRD_PARTY_LOG_LEVEL


def test_leveled_third_party_logger_is_filtered_at_the_handler():
    """A dependency that sets its own WARNING level must still clear the floor.

    Propagation never re-checks an ancestor logger's level, so an explicitly
    leveled library (SQLAlchemy sets WARNING) published through the NOTSET
    shared handler even at UMS_LOG_LEVEL=ERROR (codex round-27 P2). The
    handler filter gates third-party records at the floor while first-party
    records keep passing at the configured application level.
    """
    capture = io.StringIO()
    with production_shaped_root():
        configure_logging(level="ERROR", stream=capture)
        third_party = logging.getLogger("sqlalchemy.engine")
        original_level = third_party.level
        try:
            third_party.setLevel(logging.WARNING)
            third_party.warning("below-floor library warning must not print")
            logging.getLogger(FIRST_PARTY_LOGGER_NAME).error(
                "first-party error passes at the application level"
            )
        finally:
            third_party.setLevel(original_level)
        out = capture.getvalue()
        assert "below-floor library warning" not in out
        assert "first-party error passes" in out
