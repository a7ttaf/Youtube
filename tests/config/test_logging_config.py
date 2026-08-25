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
    root = logging.getLogger()
    saved_root_level = root.level
    saved_root_handlers = root.handlers[:]
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
        logging_config._logging_state = logging_config._LoggingLeaseState()
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


def test_configure_logging_is_idempotent():
    """A second call is a no-op, so lines are never doubled."""
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
