# ============================================================================
# Purpose: Install the process-level logging configuration exactly once, at
#   ASGI startup, and hand the caller a token that undoes it. Before this
#   module the backend had no basicConfig, no dictConfig, and no handler at
#   all against 11 module loggers, so `logging.lastResort` was the whole
#   configuration: WARNING+ reached stderr with no timestamp, no level, and
#   no logger name, and every INFO/DEBUG line -- connector-run progress,
#   tenant resolution, the export lifecycle -- was dropped at isEnabledFor.
# Database/ORM: None.
# Standards: Standard library only (no structlog, no python-json-logger);
#   tests/test_version_baseline.py asserts exact-set equality on the
#   dependency manifest, so a logging dependency is a build break, and a
#   stdlib logging.Formatter covers the required fields anyway. Idempotent:
#   a second call while the handler is installed is a no-op, so "configure
#   once" is a property of the function, not of its call sites.
#   UMS_LOG_LEVEL is an APPLICATION verbosity knob, so it is applied to the
#   first-party logger, never to the root logger -- see the
#   configure_logging contract block and THIRD_PARTY_LOG_LEVEL below.
# Blast Radius: Process-global logging state (root handler list, root level,
#   and the `ums_smart_revenue` logger's level). No authorization, finance,
#   audit, tenancy, or export behavior.
#   Deliberately NOT in the blast radius: the `uvicorn`, `uvicorn.error`, and
#   `uvicorn.access` loggers -- see the configure_logging contract block.
# Connections:
#   - File: backend/ums_smart_revenue/config/settings.py -> UMS_LOG_LEVEL is
#     parsed and validated there; this module only maps the name to an int.
#   - File: backend/ums_smart_revenue/app.py -> the ASGI lifespan calls
#     configure_logging on startup and restore_logging on shutdown.
#   - File: Docs/21_BETA_IMPLEMENTATION_PLAN.md -> plan item P0.6.
# ============================================================================
"""Process-level logging configuration for the UMS Smart Revenue backend."""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import TextIO

from ums_smart_revenue.config.settings import (
    ALLOWED_LOG_LEVELS,
    LOG_LEVEL_ENV,
    LOG_LEVEL_NAMES,
    load_app_settings,
)

# Handler identity. `logging.Handler.set_name` registers the handler in the
# module-level name table, which is what makes "is our configuration already
# installed?" answerable without a module-global flag that a forked worker or
# a reimport would get wrong.
UMS_LOG_HANDLER_NAME = "ums-smart-revenue"

# The three fields P0.6 exists to add -- timestamp, level, logger name -- plus
# the thread name. The thread name is not decoration here: the connector job
# executor is a ThreadPoolExecutor and the group-sync scheduler owns a tick
# thread, so "which worker logged this" is the difference between reading a
# half-completed run and guessing at it.
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(threadName)s] %(message)s"

# UTC, ISO-8601 shaped, millisecond precision. `datefmt` is deliberately NOT
# used: logging.Formatter.formatTime drops the millisecond suffix whenever a
# datefmt is supplied, and millisecond ordering is what lets two lines from
# two connector worker threads be placed relative to each other. Setting
# default_time_format/default_msec_format on the INSTANCE keeps both the
# milliseconds and an explicit zone marker.
LOG_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
LOG_MSEC_FORMAT = "%s.%03dZ"

# The one logger UMS_LOG_LEVEL is about. Derived from this module's own
# package rather than typed as a literal, so a package rename cannot leave a
# stale string here that silently sends every application logger back to the
# third-party floor. tests/config/test_logging_config.py pins the value.
FIRST_PARTY_LOGGER_NAME = __name__.partition(".")[0]

# The level every OTHER logger in the process gets, by inheritance from the
# root logger.
#
# WHY THIS EXISTS: UMS_LOG_LEVEL is an application verbosity knob -- the
# settings docstring names connector-run progress, tenant resolution and the
# export lifecycle as what it buys. Setting the ROOT logger to that level
# instead hands it to every library in the dependency set as well, because a
# third-party logger is created at NOTSET and inherits from root. Measured on
# this dependency set with the app imported: 56 third-party loggers go
# INFO-enabled, only SQLAlchemy self-gates (it pins its own logger to
# WARNING at import). Two of those 56 log a full request URL, query string
# included, at INFO:
#   * `httpx2` (_client.py:1085) -- 'HTTP Request: %s %s "%s %d %s"' with
#     request.url, which for connectors/google/youtube_analytics_client.py:109
#     carries `ids=contentOwner==<cms id>`, and for the reporting/groups
#     clients `onBehalfOfContentOwner=<cms id>`. One line per API call.
#   * `urllib3.poolmanager` (:500) -- "Redirecting %s -> %s" with both URLs;
#     google-auth and google-cloud-storage both call through urllib3.
# The CMS content-owner id is a guarded infrastructure identifier in this
# repository: it leaked once via #169 and was deliberately re-redacted.
#
# The floor is applied to the ROOT logger, not to an enumerated list of
# library names, precisely so it covers dependencies nobody has added yet: a
# new package's logger is born NOTSET and inherits it with no code change
# here. It gates at Logger.isEnabledFor, i.e. before the LogRecord exists, so
# it holds regardless of how many handlers the process ends up with or who
# owns them.
#
# Residual hole, stated rather than hidden: a dependency that calls
# setLevel(INFO) on its OWN logger at import time overrides an inherited
# floor and is not covered by this constant. That case is caught by
# tests/config/test_logging_config.py::
# test_no_third_party_logger_in_the_process_is_info_enabled, which walks the
# real logger tree of an imported app and fails the build.
THIRD_PARTY_LOG_LEVEL = logging.WARNING


@dataclass
class LoggingConfiguration:
    """Undo token returned by :func:`configure_logging`.

    ``installed`` is True when this call created the shared UMS handler.
    Every successful configure call (including a lease on an already-installed
    handler) increments a process-wide reference count; :func:`restore_logging`
    decrements it and only removes the handler when the count reaches zero.

    ``released`` makes each token consume its lease AT MOST ONCE. The refcount
    is process-wide, so without it a second ``restore_logging`` on the same
    token decremented again and consumed a DIFFERENT lifespan's lease: with two
    apps configured, a double release dropped the count to zero and removed the
    shared handler while the second app was still running, silencing its logs.
    The refcount guard alone cannot catch that -- it only sees "no lease
    outstanding", never "this token already released".

    Not frozen, because the flag has to travel with the token. Keeping the
    state on the token rather than in a module-level set is deliberate: a
    module-level registry is reset by ``importlib.reload`` and by the test
    fixture, which would recycle lease identities and break the reload
    re-adoption path.
    """

    installed: bool
    handler: logging.Handler | None
    previous_root_level: int
    previous_first_party_level: int
    released: bool = False


_logging_lock = threading.Lock()


@dataclass
class _LoggingLeaseState:
    """Process-wide lease counter for the shared UMS root handler."""

    refcount: int = 0
    previous_root_level: int | None = None
    previous_first_party_level: int | None = None


_logging_state = _LoggingLeaseState()

# Handler attributes that survive importlib.reload when module globals reset.
_HANDLER_PREVIOUS_ROOT_ATTR = "_ums_previous_root_level"
_HANDLER_PREVIOUS_FIRST_PARTY_ATTR = "_ums_previous_first_party_level"


# ============================================================================
# Purpose: Build the single stdlib formatter used for every UMS log line, in
#   UTC with millisecond precision.
# Database/ORM: None.
# Standards: logging.Formatter only. The instance attributes
#   default_time_format / default_msec_format / converter are the documented
#   hooks for this; assigning `converter` on the instance shadows the class
#   attribute instead of mutating logging.Formatter globally, so an
#   unrelated formatter elsewhere in the process keeps local time.
# Blast Radius: Log line rendering only.
# Connections:
#   - File: backend/ums_smart_revenue/config/logging_config.py ->
#     configure_logging attaches the result to the root handler.
# ============================================================================
def build_log_formatter() -> logging.Formatter:
    """Return the UTC, millisecond-precision formatter for UMS log lines."""
    formatter = logging.Formatter(fmt=LOG_FORMAT)
    formatter.default_time_format = LOG_TIME_FORMAT
    formatter.default_msec_format = LOG_MSEC_FORMAT
    formatter.converter = time.gmtime
    return formatter


# ============================================================================
# Purpose: Install one stderr handler on the ROOT logger, put the configured
#   level on the FIRST-PARTY logger, and hold the root logger at the
#   third-party floor. Returns the undo token.
#
# WHERE THE LEVEL GOES, AND WHY IT IS NOT THE ROOT LOGGER:
#   `ums_smart_revenue` gets `numeric_level`; the root logger gets
#   `max(numeric_level, THIRD_PARTY_LOG_LEVEL)`. See THIRD_PARTY_LOG_LEVEL
#   above for the leak this closes and the measurement behind it.
#   Two properties make this work rather than merely quieten things:
#     * A first-party INFO record still reaches the root handler. Logger
#       levels gate only the ORIGINATING logger (Logger.isEnabledFor);
#       Logger.callHandlers then walks ancestors and consults HANDLER levels,
#       never ancestor logger levels. Root at WARNING therefore silences
#       library INFO without touching application INFO.
#     * `max(...)` and not a hard WARNING: turning the knob DOWN has to quiet
#       libraries too. UMS_LOG_LEVEL=ERROR with a hard WARNING floor would
#       keep printing library warnings and look like a broken knob.
#
# WHY basicConfig AND NOT dictConfig (the deliberate decision P0.6 asks for):
#   uvicorn decides whether to emit access logs by asking
#   `logging.getLogger("uvicorn.access").hasHandlers()` -- see
#   uvicorn/protocols/http/h11_impl.py:57 and httptools_impl.py:61 in the
#   pinned 0.52.4. `uvicorn.access` carries propagate=False, so hasHandlers()
#   sees only that logger's OWN handler list. A dictConfig can empty that
#   list two ways, both silent: `disable_existing_loggers` (which defaults to
#   TRUE when omitted) and any `loggers:` entry naming uvicorn. Either one
#   turns off the only request logging this deployment has, while looking
#   like it added logging. basicConfig has no disable_existing_loggers, takes
#   no per-logger configuration, and cannot name uvicorn.access -- it is the
#   option for which that failure is structurally impossible, and root-level
#   handling is all the required timestamp/level/name output needs.
#   Consequence accepted: uvicorn's own lines keep uvicorn's format (no
#   timestamp). Restyling them means reaching into
#   uvicorn.logging.AccessFormatter internals, which is the same hazard by a
#   different door; application lines -- the ones that could not be placed in
#   time -- get the full format.
#
#   basicConfig is a documented no-op once the root logger owns a handler
#   (CPython Lib/logging/__init__.py gates the whole body on
#   `len(root.handlers) == 0`). Under uvicorn root is empty, so the
#   basicConfig branch IS the production path. The explicit branch below
#   covers embedded callers -- pytest installs its own root handlers -- so
#   the level is honoured rather than silently skipped.
# Database/ORM: None.
# Standards: Level validated against ALLOWED_LOG_LEVELS before use, so a
#   direct caller gets the same fail-fast the settings loader gives the
#   operator. sys.stderr is resolved at call time, never captured at import.
#   No foreign handler is ever removed: pytest's capture handlers and any
#   operator-supplied --log-config survive. No third-party logger's own level
#   is ever written to, so an operator --log-config that deliberately raises
#   one library to INFO keeps that setting.
# Blast Radius: Root logger handlers + level, and the `ums_smart_revenue`
#   logger's level. No authorization, finance, audit, or export behavior.
#   Log CONTENT is unchanged -- this installs a handler, it does not rewrite
#   any call site.
#
#   TWO FIRST-PARTY DISCLOSURES this function newly surfaces, and does NOT
#   change, because both are `ums_smart_revenue` loggers running at the
#   configured level by design:
#     1. connectors/runs/executor.py:755 logs the YouTube CMS content-owner
#        id at INFO ("Scheduled group sync converged with no changes
#        (owner=%s)"), once per scheduled sync. Container logs therefore
#        carry a real infrastructure identifier whenever the group-sync
#        scheduler is enabled. Redacting it is a change to that file, not to
#        this one.
#     2. tenancy/resolver.py attaches `extra=` payloads to six records
#        (lines 174-176, 189-192, 256-259, 268-270, 294-296, 305-308). LOG_FORMAT
#        is a fixed field list, so those keys are carried on the LogRecord and
#        rendered nowhere -- structured detail silently dropped, not leaked.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> called from the ASGI lifespan.
#   - File: backend/ums_smart_revenue/config/settings.py -> AppSettings
#     .log_level supplies the default level.
#   - File: backend/ums_smart_revenue/api/dependencies.py -> the load-bearing
#     "rejected unknown principal" WARNING (line 204) that diagnoses a wrong
#     X-User-ID under UMS_AUTHZ_SOURCE=database reaches stderr through this
#     handler, timestamped and named.
#   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
#     every Google API call runs through httpx2, whose INFO line is what
#     THIRD_PARTY_LOG_LEVEL keeps out of the log.
#   - File: docker-compose.yml -> `UMS_LOG_LEVEL` pass-through in x-app-env.
# ============================================================================
def configure_logging(
    *,
    level: str | None = None,
    stream: TextIO | None = None,
) -> LoggingConfiguration:
    """Install the UMS root log handler once and return its undo token.

    Args:
        level: Level name override. Defaults to ``AppSettings.log_level``.
            Applied to the ``ums_smart_revenue`` logger, not to the root
            logger -- see the contract block above.
        stream: Destination stream override. Defaults to ``sys.stderr``,
            resolved at call time.

    Returns:
        A :class:`LoggingConfiguration` whose ``installed`` flag is False when
        the handler was already present (caller still holds a restore lease).

    Raises:
        ValueError: If ``level`` is not one of ``LOG_LEVEL_NAMES``.
    """
    resolved_level = (level if level is not None else load_app_settings().log_level).strip().upper()
    if resolved_level not in ALLOWED_LOG_LEVELS:
        allowed = ", ".join(LOG_LEVEL_NAMES)
        raise ValueError(f"{LOG_LEVEL_ENV} must be one of: {allowed}")
    numeric_level = logging.getLevelNamesMapping()[resolved_level]
    # FIX: the configured level goes on the first-party logger; the root
    # logger -- which every third-party logger inherits from -- is held at the
    # third-party floor. Setting root to numeric_level published the guarded
    # CMS content-owner id through httpx2's INFO request line on every Google
    # API call. See THIRD_PARTY_LOG_LEVEL for the measurement.
    root_level = max(numeric_level, THIRD_PARTY_LOG_LEVEL)

    with _logging_lock:
        # FIX: Snapshot previous levels inside the same critical section that
        # mutates them. Reading outside the lock raced with a concurrent
        # restore_logging that put the originals back after this call had
        # already captured the still-configured values as "previous".
        root = logging.getLogger()
        first_party = logging.getLogger(FIRST_PARTY_LOGGER_NAME)
        previous_root_level = root.level
        previous_first_party_level = first_party.level

        existing_handler = installed_log_handler()
        if existing_handler is not None:
            # FIX: importlib.reload clears module globals (refcount=0) while the
            # named handler can still sit on the root logger. Re-adopt that
            # orphaned install with one synthetic pre-reload lease so a new
            # configure/restore pair cannot strip logging from under an older
            # lifespan that still owns the handler.
            if _logging_state.refcount <= 0:
                _logging_state.refcount = 1
                # FIX: Do not snapshot the already-configured levels as
                # "previous" — that would restore WARNING/INFO after every
                # lease ends. Recover the pre-install originals stored on the
                # surviving handler at first install.
                recovered_root = getattr(
                    existing_handler, _HANDLER_PREVIOUS_ROOT_ATTR, None
                )
                recovered_first_party = getattr(
                    existing_handler, _HANDLER_PREVIOUS_FIRST_PARTY_ATTR, None
                )
                if recovered_root is not None:
                    _logging_state.previous_root_level = recovered_root
                if recovered_first_party is not None:
                    _logging_state.previous_first_party_level = recovered_first_party
                root.setLevel(root_level)
                first_party.setLevel(numeric_level)
            # FIX: Overlapping create_app() lifespans share one handler. Take a
            # lease so the first shutdown cannot strip logging from a still-live
            # second app.
            _logging_state.refcount += 1
            return LoggingConfiguration(
                installed=False,
                handler=None,
                previous_root_level=(
                    _logging_state.previous_root_level
                    if _logging_state.previous_root_level is not None
                    else previous_root_level
                ),
                previous_first_party_level=(
                    _logging_state.previous_first_party_level
                    if _logging_state.previous_first_party_level is not None
                    else previous_first_party_level
                ),
            )

        handler = logging.StreamHandler(sys.stderr if stream is None else stream)
        handler.set_name(UMS_LOG_HANDLER_NAME)
        handler.setFormatter(build_log_formatter())
        # Survive importlib.reload: module globals reset, handler attributes do not.
        setattr(handler, _HANDLER_PREVIOUS_ROOT_ATTR, previous_root_level)
        setattr(handler, _HANDLER_PREVIOUS_FIRST_PARTY_ATTR, previous_first_party_level)

        logging.basicConfig(handlers=[handler], level=root_level)
        if handler not in root.handlers:
            # basicConfig no-opped because the root logger already owns a handler
            # (pytest, or an operator-supplied --log-config). Add ours alongside
            # rather than displacing theirs, and apply the level basicConfig
            # skipped -- without this the handler is installed but the root floor
            # is whatever the embedding process happened to leave behind.
            root.addHandler(handler)
            root.setLevel(root_level)
        # Unconditional, and after the basicConfig branch: basicConfig never
        # touches a non-root logger, so this is the only place the application
        # level is applied on either path.
        first_party.setLevel(numeric_level)
        _logging_state.refcount = 1
        _logging_state.previous_root_level = previous_root_level
        _logging_state.previous_first_party_level = previous_first_party_level
        return LoggingConfiguration(
            installed=True,
            handler=handler,
            previous_root_level=previous_root_level,
            previous_first_party_level=previous_first_party_level,
        )


# ============================================================================
# Purpose: Undo a configure_logging install -- remove the handler and put BOTH
#   levels back: the root floor and the first-party application level.
# Database/ORM: None.
# Standards: create_app is a FACTORY. One process can build many apps (the
#   test suite builds hundreds), so acquiring process-global logging state on
#   startup without releasing it on shutdown would make the factory leave a
#   permanent global side effect behind and make test behaviour depend on
#   which app was constructed first. Release is symmetric with the scheduler
#   and executor teardown already in the lifespan. Overlapping lifespans share
#   one handler via reference counting so the first shutdown cannot silence a
#   still-active second app. In production, shutdown is immediately followed by
#   process exit, and it runs AFTER the workers are closed, so no real shutdown
#   line loses its handler. Because the count is process-wide, the release is
#   also idempotent PER TOKEN (`LoggingConfiguration.released`): a token that
#   released once can never decrement again and consume a different lifespan's
#   lease. The refcount guard cannot do that on its own -- it only detects "no
#   lease outstanding at all".
# Blast Radius: Root logger handlers + level, and the `ums_smart_revenue`
#   logger's level. No authorization, finance, audit, or export behavior.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> called in the lifespan's
#     outer finally, after scheduler.close() and executor.close().
# ============================================================================
def restore_logging(configuration: LoggingConfiguration) -> None:
    """Release one logging lease; remove the handler when the last lease ends.

    Reference-counted: each :func:`configure_logging` call takes one lease, and
    only the release that drops the count to zero touches global logging state.
    Overlapping app lifespans therefore share a single handler and the first
    shutdown cannot silence a still-active second app.

    Each token releases AT MOST ONCE. The count is process-wide, so without a
    per-token flag a second release of the same ``configuration`` decremented
    again and consumed another lifespan's lease -- with two apps configured,
    that dropped the count to zero, removed the shared handler and restored the
    previous levels while the second app was still running, silencing its logs.
    The refcount guard alone cannot catch that: it only sees "no lease
    outstanding", never "this token already released". ``configuration.released``
    closes it, so a repeated release (a ``finally`` that runs twice, a
    belt-and-braces teardown) is now a genuine no-op.

    Args:
        configuration: The undo token returned by :func:`configure_logging`.
            Its ``handler`` is used when ``installed`` is True; otherwise the
            currently installed handler is resolved instead, so a caller
            holding a non-installing lease still restores the right one. Its
            recorded previous levels are the fallback when the module-level
            state has none.

    Returns:
        None. The call is a no-op when this token already released, when no
        lease is outstanding (``refcount`` already zero), when leases remain
        after this release, or when no handler is installed.
    """
    with _logging_lock:
        if configuration.released:
            return
        if _logging_state.refcount <= 0:
            return
        configuration.released = True
        _logging_state.refcount -= 1
        if _logging_state.refcount > 0:
            return

        handler = configuration.handler if configuration.installed else installed_log_handler()
        if handler is None:
            return
        root = logging.getLogger()
        root.removeHandler(handler)
        handler.close()
        previous_root = (
            _logging_state.previous_root_level
            if _logging_state.previous_root_level is not None
            else configuration.previous_root_level
        )
        previous_first_party = (
            _logging_state.previous_first_party_level
            if _logging_state.previous_first_party_level is not None
            else configuration.previous_first_party_level
        )
        root.setLevel(previous_root)
        # FIX: configure_logging now also writes the first-party logger's level,
        # so releasing only the root level would leave the factory holding
        # process-global state -- the exact leak restore_logging exists to avoid.
        logging.getLogger(FIRST_PARTY_LOGGER_NAME).setLevel(previous_first_party)
        _logging_state.previous_root_level = None
        _logging_state.previous_first_party_level = None


def installed_log_handler() -> logging.Handler | None:
    """Return the UMS root handler if this process already installed one."""
    for handler in logging.getLogger().handlers:
        if handler.get_name() == UMS_LOG_HANDLER_NAME:
            return handler
    return None
