# ============================================================================
# Purpose: Install one shared process-level logging configuration at ASGI
#   startup and hand each lifespan independent output and safety leases. Before this
#   module the backend had no basicConfig, no dictConfig, and no handler at
#   all against 11 module loggers, so `logging.lastResort` was the whole
#   configuration: WARNING+ reached stderr with no timestamp, no level, and
#   no logger name, and every INFO/DEBUG line -- connector-run progress,
#   tenant resolution, the export lifecycle -- was dropped at isEnabledFor.
# Database/ORM: None.
# Standards: Standard library only (no structlog, no python-json-logger);
#   tests/test_version_baseline.py asserts exact-set equality on the
#   dependency manifest, so a logging dependency is a build break. Stdlib
#   handler filters sanitize the shared LogRecord before any formatter, and a
#   formatter provides defense in depth. Shared handler ownership is idempotent;
#   overlapping leases still recompute the most verbose surviving app level.
#   UMS_LOG_LEVEL is an APPLICATION verbosity knob, so it is applied to the
#   first-party logger, never to the root logger -- see the
#   configure_logging contract block and THIRD_PARTY_LOG_LEVEL below.
# Blast Radius: Process-global logging state (root handler list, root level,
#   and the `ums_smart_revenue` logger's level). No authorization, finance,
#   audit, tenancy, or export behavior.
#   Uvicorn logger levels, handlers, and propagation flags are not changed;
#   their existing handlers receive/removal-match exact safety filter objects.
# Connections:
#   - File: backend/ums_smart_revenue/config/settings.py -> UMS_LOG_LEVEL is
#     parsed and validated there; this module only maps the name to an int.
#   - File: backend/ums_smart_revenue/app.py -> the ASGI lifespan calls
#     configure_logging on startup and restore_logging on shutdown.
#   - File: Docs/21_BETA_IMPLEMENTATION_PLAN.md -> plan item P0.6.
# ============================================================================
"""Process-level logging configuration for the UMS Smart Revenue backend."""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
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

# Public handler names are observability metadata, not ownership proof. These
# private markers and persisted lease attributes survive importlib.reload and
# prevent an embedding application's same-named handler from being adopted or
# closed as ours.
_HANDLER_ID_ATTR = "_ums_smart_revenue_handler_id"
_HANDLER_ID_VALUE = "ums-smart-revenue-v3"
_HANDLER_LEVELS_ATTR = "_ums_smart_revenue_output_levels"
_HANDLER_SAFETY_COUNT_ATTR = "_ums_smart_revenue_safety_count"
_HANDLER_SAFETY_LEASES_ATTR = "_ums_smart_revenue_safety_leases"
_HANDLER_REDACTION_BINDINGS_ATTR = "_ums_smart_revenue_redaction_bindings"
_HANDLER_FLOOR_BINDINGS_ATTR = "_ums_smart_revenue_floor_bindings"
_FILTER_ID_ATTR = "_ums_smart_revenue_filter_id"
_FLOOR_FILTER_ID = "third-party-floor-v2"
_REDACTION_FILTER_ID = "redaction-v2"
_CALL_HANDLERS_ID_ATTR = "_ums_smart_revenue_dispatch_id"
_CALL_HANDLERS_ORIGINAL_ATTR = "_ums_smart_revenue_original_dispatch"
_CALL_HANDLERS_ID_VALUE = "redacting-call-handlers-v1"
_LOG_FINGERPRINT_KEY = secrets.token_bytes(32)

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

# A single stable replacement makes every sanitizer idempotent. Credential
# recognizers are bounded by URL/assignment delimiters. SQL detection is
# deliberately conservative: explicit SQL/parameter labels always redact,
# while an unlabeled statement must begin a line with a strong SQL shape.
_REDACTED = "[REDACTED]"
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_GUARDED_CONTENT_OWNER_NAME = (
    r"(?:on[_-]?behalf[_-]?of[_-]?content[_-]?owner(?:[_-]?id)?|"
    r"content[_-]?owner(?:[_-]?id)?)"
)
# Google renders the guarded CMS owner through both decoded selector syntax
# and URL-encoded equality operators. This recognizer intentionally excludes
# generic ``owner`` and channel selectors to avoid erasing safe diagnostics.
_CONTENT_OWNER_SELECTOR_RE = re.compile(
    r"(?i)(?P<prefix>\bcontent[_-]?owner(?:==|%3d%3d|%253d%253d))"
    r"(?!\[REDACTED\])(?P<value>[^&#\s,;\"'}\]]+)"
)
_AUTHORIZATION_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>authorization\s*[:=]\s*"
    r"(?!(?:(?:bearer|basic|digest)\s+)?\[REDACTED\])"
    r"(?:(?:bearer|basic|digest)\s+)?)(?P<quote>[\"']?)"
    r"(?!\[REDACTED\])(?P<value>[^\s,;&\"'}]+)(?P=quote)"
)
_GUARDED_IDENTIFIER_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<prefix>{_GUARDED_CONTENT_OWNER_NAME}\s*[\"']?\s*"
    r"(?:[:]|=(?!=))\s*)(?P<quote>[\"']?)(?!\[REDACTED\])"
    r"(?P<value>[^\s,;&\"'}]+)(?P=quote)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>"
    r"(?:bearer\s+|"
    r"(?:access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|api[_-]?key|apikey|token|password|passwd|pwd|"
    r"secret|credential|signature|sig|private[_-]?key|cookie|session|"
    r"csrf|set[_-]?cookie)\s*[:=]\s*)"
    r")(?P<quote>[\"']?)(?!\[REDACTED\])"
    r"(?P<value>[^\s,;&\"'}]+)(?P=quote)"
)
_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>(?:authorization|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|auth[_-]?token|client[_-]?secret|api[_-]?key|apikey|"
    r"token|password|passwd|pwd|secret|credential|signature|sig|"
    r"private[_-]?key|cookie|session|csrf|set[_-]?cookie)"
    r"\s*[\"']?\s*[:=]\s*)(?P<quote>[\"'])(?!\[REDACTED\])"
    r"(?P<value>.*?)(?P=quote)"
)
_SECRET_TUPLE_RE = re.compile(
    r"(?i)(?P<prefix>[\"'](?:authorization|proxy-authorization|cookie|"
    r"set-cookie|x-api-key|api-key|x-auth-token|"
    r"x-(?:amz|goog|google)-(?:credential|signature|security-token)|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|password|"
    rf"secret|credential|signature|private[_-]?key|"
    rf"{_GUARDED_CONTENT_OWNER_NAME})[\"']\s*,\s*)"
    r"(?P<quote>[\"'])(?!\[REDACTED\])(?P<value>.*?)(?P=quote)"
)
_URL_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[?&;](?:access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|auth[_-]?token|client[_-]?secret|api[_-]?key|apikey|"
    r"token|password|passwd|secret|key|signature|sig|credential|code|"
    r"authorization|oauth[_-]?token|x[-_](?:google|goog|amz)[-_]"
    r"(?:signature|credential|security[-_]token)|session|csrf)=)"
    r"(?!\[REDACTED\])(?P<value>[^&#\s]+)"
)
_URL_USERINFO_RE = re.compile(
    r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]*://)"
    r"(?!\[REDACTED\]@)(?P<userinfo>[^/@\s]+@)"
)
_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b")
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"(?is)-----BEGIN (?P<label>(?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY)-----"
    r".*?-----END (?P=label)-----"
)
_SQL_LABEL_RE = re.compile(
    r"(?is)(?:\[\s*(?:SQL|parameters)\s*:|"
    r"(?<![\w-])(?:sql|statement|query|parameters|params)\s*[:=]\s*).*$"
)
_SQL_STATEMENT_RE = re.compile(
    r"(?is)(?:^|[\r\n])\s*(?:"
    r"SELECT\b(?=.{0,256}\bFROM\b)(?=.{0,512}\b(?:WHERE|JOIN|GROUP\s+BY|"
    r"ORDER\s+BY|LIMIT|OFFSET|UNION)\b)|"
    r"INSERT\s+INTO\s+[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?"
    r"(?:\.[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?)?\s*(?:\(|VALUES\b|DEFAULT\b)|"
    r"UPDATE\s+[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?"
    r"(?:\.[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?)?\s+SET\s+"
    r"[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?\s*=|"
    r"DELETE\s+FROM\s+[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?"
    r"(?:\.[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?)?\s+"
    r"(?:WHERE|USING|RETURNING)\b|"
    r"WITH\b(?=.{0,256}\bAS\s*\()|"
    r"CALL\s+(?:[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?\.)*"
    r"[\"`\[]?[A-Za-z_][\w$]*[\"`\]]?\s*\(|"
    r"(?:CREATE|ALTER|DROP|TRUNCATE)\s+(?:TABLE|INDEX|SCHEMA)\b|"
    r"(?:GRANT|REVOKE)\b(?=.{0,256}\bON\b)"
    r").*$"
)
_DATABASE_EXCEPTION_MODULES = frozenset({"sqlite3", "psycopg", "psycopg2"})
_DATABASE_EXCEPTION_MODULE_PREFIXES = ("sqlalchemy.", "psycopg.", "psycopg2.")
_DATABASE_EXCEPTION_BASES = frozenset(
    {
        ("sqlite3", "Error"),
        ("sqlalchemy.exc", "SQLAlchemyError"),
        ("psycopg", "Error"),
        ("psycopg2", "Error"),
    }
)
_SECRET_STRUCTURED_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "authtoken",
        "clientsecret",
        "apikey",
        "password",
        "passwd",
        "pwd",
        "secret",
        "credential",
        "signature",
        "privatekey",
        "cookie",
        "setcookie",
        "session",
        "csrf",
        "contentowner",
        "contentownerid",
        "onbehalfofcontentowner",
        "onbehalfofcontentownerid",
    }
)
_SQL_STRUCTURED_KEYS = frozenset({"sql", "statement", "query", "params", "parameters"})
_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


@dataclass
class LoggingConfiguration:
    """Per-lifespan output ownership and redaction-safety lease token."""

    installed: bool
    handler: logging.Handler | None
    previous_root_level: int
    previous_first_party_level: int
    lease_id: str = field(default_factory=lambda: secrets.token_hex(16))
    numeric_level: int | None = None
    output_released: bool = False
    safety_released: bool = False
    released: bool = False


_logging_lock = threading.Lock()


@dataclass
class _LoggingLeaseState:
    """Process-wide output-level ownership and independent safety count."""

    refcount: int = 0
    lease_levels: dict[str, int] = field(default_factory=dict)
    safety_refcount: int = 0
    safety_leases: set[str] = field(default_factory=set)
    previous_root_level: int | None = None
    previous_first_party_level: int | None = None


_logging_state = _LoggingLeaseState()

# Handler attributes that survive importlib.reload when module globals reset.
_HANDLER_PREVIOUS_ROOT_ATTR = "_ums_previous_root_level"
_HANDLER_PREVIOUS_FIRST_PARTY_ATTR = "_ums_previous_first_party_level"

_FilterBinding = tuple[weakref.ReferenceType[logging.Handler], logging.Filter]


def _is_owned_handler(handler: logging.Handler) -> bool:
    """Return whether a handler carries the private UMS ownership marker."""
    return getattr(handler, _HANDLER_ID_ATTR, None) == _HANDLER_ID_VALUE


def _configured_handlers(root: logging.Logger) -> list[logging.Handler]:
    """Return every current handler once, including non-propagating loggers."""
    handlers: list[logging.Handler] = []
    seen: set[int] = set()
    for handler in root.handlers:
        if id(handler) not in seen:
            handlers.append(handler)
            seen.add(id(handler))
    for existing in logging.Logger.manager.loggerDict.values():
        if not isinstance(existing, logging.Logger):
            continue
        for handler in existing.handlers:
            if id(handler) not in seen:
                handlers.append(handler)
                seen.add(id(handler))
    if logging.lastResort is not None and id(logging.lastResort) not in seen:
        handlers.append(logging.lastResort)
    return handlers


def _remove_exact_filter_bindings(bindings: tuple[_FilterBinding, ...]) -> None:
    """Remove one filter generation by object identity, never by marker/name."""
    for handler_ref, owned_filter in bindings:
        handler = handler_ref()
        if handler is None:
            continue
        handler.filters[:] = [
            existing for existing in handler.filters if existing is not owned_filter
        ]


def _replace_redaction_filters(
    root: logging.Logger,
    owner_handler: logging.Handler,
) -> None:
    """Publish redaction on all handlers before retiring the old generation."""
    previous = getattr(owner_handler, _HANDLER_REDACTION_BINDINGS_ATTR, ())
    bindings: list[_FilterBinding] = []
    for handler in _configured_handlers(root):
        redaction_filter = _RedactionFilter()
        # Safety runs before operator filters so none can observe a raw record.
        handler.filters.insert(0, redaction_filter)
        bindings.append((weakref.ref(handler), redaction_filter))
    setattr(owner_handler, _HANDLER_REDACTION_BINDINGS_ATTR, tuple(bindings))
    _remove_exact_filter_bindings(previous)


def _replace_floor_filters(
    root: logging.Logger,
    owner_handler: logging.Handler,
    floor: int,
) -> None:
    """Replace output-floor filters for the current effective lease level."""
    previous = getattr(owner_handler, _HANDLER_FLOOR_BINDINGS_ATTR, ())
    bindings: list[_FilterBinding] = []
    for handler in _configured_handlers(root):
        floor_filter = _ThirdPartyFloorFilter(floor)
        handler.addFilter(floor_filter)
        bindings.append((weakref.ref(handler), floor_filter))
    setattr(owner_handler, _HANDLER_FLOOR_BINDINGS_ATTR, tuple(bindings))
    _remove_exact_filter_bindings(previous)


def _remove_owned_filters(owner_handler: logging.Handler, *, safety: bool) -> None:
    """Remove exact UMS floor filters and optionally the safety generation."""
    floor_bindings = getattr(owner_handler, _HANDLER_FLOOR_BINDINGS_ATTR, ())
    _remove_exact_filter_bindings(floor_bindings)
    setattr(owner_handler, _HANDLER_FLOOR_BINDINGS_ATTR, ())
    if safety:
        redaction_bindings = getattr(
            owner_handler,
            _HANDLER_REDACTION_BINDINGS_ATTR,
            (),
        )
        _remove_exact_filter_bindings(redaction_bindings)
        setattr(owner_handler, _HANDLER_REDACTION_BINDINGS_ATTR, ())


# ============================================================================
# Purpose: Sanitize the shared LogRecord before any configured handler can
#   observe or format it, and build the UTC formatter for the UMS output.
# Database/ORM: None.
# Standards: Standard-library only. Handler filters mutate the shared record
#   before operator/structured formatters run; exception chains, SQL/parameters,
#   structured extras, PEM blocks, URLs, and line-breaking controls share the
#   same fail-safe boundary. The instance attributes
#   default_time_format / default_msec_format / converter are the documented
#   hooks for this; assigning `converter` on the instance shadows the class
#   attribute instead of mutating logging.Formatter globally, so an
#   unrelated formatter elsewhere in the process keeps local time.
# Blast Radius: Log line rendering only; credential values are replaced with
#   ``[REDACTED]``. Authorization, finance, audit, and database state are not
#   modified.
# Connections:
#   - File: backend/ums_smart_revenue/config/logging_config.py ->
#     configure_logging attaches the result to the root handler.
# ============================================================================
def _redact_private_sql(text: str) -> str:
    """Remove explicit or strongly shaped SQL while preserving safe prefixes."""
    matches = [
        match
        for match in (_SQL_LABEL_RE.search(text), _SQL_STATEMENT_RE.search(text))
        if match is not None
    ]
    if not matches:
        return text
    first = min(matches, key=lambda match: match.start())
    prefix = text[: first.start()].rstrip()
    return f"{prefix} [REDACTED-SQL]" if prefix else "[REDACTED-SQL]"


def _redact_log_text(value: object) -> str:
    """Redact credentials/private SQL and neutralize every line-break control."""
    try:
        text = str(value)
    except Exception:
        text = "<unrenderable log value>"
    text = _CONTENT_OWNER_SELECTOR_RE.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        text,
    )
    text = _GUARDED_IDENTIFIER_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"
        ),
        text,
    )
    text = _AUTHORIZATION_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"
        ),
        text,
    )
    text = _QUOTED_SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"
        ),
        text,
    )
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"
        ),
        text,
    )
    text = _SECRET_TUPLE_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"
        ),
        text,
    )
    text = _URL_SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        text,
    )
    text = _URL_USERINFO_RE.sub(
        lambda match: f"{match.group('scheme')}[REDACTED]@",
        text,
    )
    text = _JWT_RE.sub(_REDACTED, text)
    text = _PRIVATE_KEY_BLOCK_RE.sub("[REDACTED-PRIVATE-KEY]", text)
    text = _redact_private_sql(text)

    def _escape_control(match: re.Match[str]) -> str:
        """Render one control character as a safe visible escape."""
        character = match.group(0)
        replacements = {"\r": r"\r", "\n": r"\n", "\t": r"\t"}
        if character in replacements:
            return replacements[character]
        codepoint = ord(character)
        return f"\\x{codepoint:02x}" if codepoint <= 0xFF else f"\\u{codepoint:04x}"

    return _CONTROL_CHARACTER_RE.sub(_escape_control, text)


def redact_sensitive_text(value: object) -> str:
    """Return safe text for logs and durable operator-facing diagnostics."""
    return _redact_log_text(value)


def _is_database_exception(value: BaseException) -> bool:
    """Recognize database exceptions by exact modules and inherited bases."""
    seen: set[int] = set()
    current: BaseException | None = value
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        exception_type = type(current)
        module_name = exception_type.__module__
        if module_name in _DATABASE_EXCEPTION_MODULES or module_name.startswith(
            _DATABASE_EXCEPTION_MODULE_PREFIXES
        ):
            return True
        if any(
            (base.__module__, base.__name__) in _DATABASE_EXCEPTION_BASES
            for base in exception_type.__mro__
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def redact_exception_summary(value: BaseException) -> str:
    """Return a useful non-DB summary or class-only database failure."""
    exception_name = type(value).__name__
    if _is_database_exception(value):
        return exception_name
    return f"{exception_name}: {_redact_log_text(value)}"


def _redact_exception_chain(value: BaseException) -> str:
    """Render a bounded, sanitized exception chain without raw tracebacks."""
    chain: list[tuple[BaseException, bool]] = []
    seen: set[int] = set()
    current: BaseException | None = value
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        cause = current.__cause__
        chain.append((current, cause is not None))
        current = cause if cause is not None else current.__context__
    rendered: list[str] = []
    for index, (exception, _) in enumerate(reversed(chain)):
        if index:
            relation = (
                "The above exception was the direct cause"
                if chain[len(chain) - index - 1][1]
                else "During handling of the above exception"
            )
            rendered.append(relation)
        rendered.append(redact_exception_summary(exception))
    return " | ".join(rendered)


def _structured_key_kind(key: str) -> str | None:
    """Classify canonical/camel/snake structured keys without prose matching."""
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    tokens = re.findall(r"[a-z0-9]+", separated.casefold())
    for start in range(len(tokens)):
        suffix = "".join(tokens[start:])
        if suffix in _SQL_STRUCTURED_KEYS:
            return "sql"
        if suffix in _SECRET_STRUCTURED_KEYS:
            return "secret"
    return None


def _redact_structured_value(value: object) -> object:
    """Recursively sanitize values carried by structured ``extra`` fields."""
    if isinstance(value, str):
        return _redact_log_text(value)
    if isinstance(value, BaseException):
        return redact_exception_summary(value)
    if isinstance(value, dict):
        sanitized: dict[object, object] = {}
        for key, nested in value.items():
            key_kind = _structured_key_kind(key) if isinstance(key, str) else None
            safe_key: object
            if isinstance(key, str):
                safe_key = _redact_log_text(key)
            elif isinstance(key, (int, float, bool, type(None))):
                safe_key = key
            else:
                safe_key = _redact_log_text(key)
            if key_kind == "secret":
                sanitized[safe_key] = _REDACTED
            elif key_kind == "sql":
                sanitized[safe_key] = "[REDACTED-SQL]"
            else:
                sanitized[safe_key] = _redact_structured_value(nested)
        return sanitized
    if isinstance(value, list):
        return [_redact_structured_value(nested) for nested in value]
    if isinstance(value, tuple):
        return tuple(_redact_structured_value(nested) for nested in value)
    if isinstance(value, set):
        return {_redact_structured_value(nested) for nested in value}
    if isinstance(value, frozenset):
        return frozenset(_redact_structured_value(nested) for nested in value)
    return _redact_log_text(value)


def fingerprint_log_identifier(value: str) -> str:
    """Return a process-local keyed label for a sensitive identifier."""
    return hmac.new(_LOG_FINGERPRINT_KEY, value.encode(), "sha256").hexdigest()[:12]


def _is_first_party_logger(name: str) -> bool:
    """Return whether a record belongs to the UMS package."""
    return name == FIRST_PARTY_LOGGER_NAME or name.startswith(FIRST_PARTY_LOGGER_NAME + ".")


class _RedactionFilter(logging.Filter):
    """Sanitize the shared record before every configured handler formats it."""

    _ums_smart_revenue_filter_id = _REDACTION_FILTER_ID

    def filter(self, record: logging.LogRecord) -> bool:
        """Mutate message, exceptions, controls, and structured extras safely."""
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = "<unrenderable log message>"
        if record.exc_info:
            _exc_type, exc_value, _traceback = record.exc_info
            # FIX: Logging's exc_info value is typed as optional. A malformed
            # tuple must still fail safe instead of bypassing the redaction
            # filter or raising from the logging pipeline.
            exception_summary = (
                _redact_exception_chain(exc_value)
                if isinstance(exc_value, BaseException)
                else "UnknownError"
            )
            rendered = f"{rendered} [exception={exception_summary}]"
            # FIX: Raw traceback exception strings can contain secrets and SQL.
            # Replace them before any formatter can cache or serialize them.
            record.exc_info = None
            record.exc_text = None
        elif record.exc_text:
            record.exc_text = _redact_log_text(record.exc_text)
        record.msg = _redact_log_text(rendered)
        record.args = ()
        if record.stack_info:
            record.stack_info = _redact_log_text(record.stack_info)
        for key, nested in tuple(record.__dict__.items()):
            if key in {
                "msg",
                "args",
                "exc_info",
                "exc_text",
                "stack_info",
                "_ums_log_redacted",
            }:
                continue
            key_kind = _structured_key_kind(key) if isinstance(key, str) else None
            if key_kind == "secret":
                setattr(record, key, _REDACTED)
            elif key_kind == "sql":
                setattr(record, key, "[REDACTED-SQL]")
            elif isinstance(nested, (str, dict, list, tuple, set, frozenset)):
                setattr(record, key, _redact_structured_value(nested))
            elif key not in _STANDARD_LOG_RECORD_FIELDS:
                setattr(record, key, _redact_structured_value(nested))
        # This marker is diagnostic only. We intentionally never trust an
        # incoming value: callers can spoof extra={"_ums_log_redacted": True}.
        setattr(record, "_ums_log_redacted", True)
        return True


def _install_process_redaction_dispatch() -> None:
    """Sanitize records before any current or future handler can observe them."""
    current_dispatch = logging.Logger.callHandlers
    if getattr(current_dispatch, _CALL_HANDLERS_ID_ATTR, None) == _CALL_HANDLERS_ID_VALUE:
        return

    def _redacting_call_handlers(
        target_logger: logging.Logger,
        record: logging.LogRecord,
    ) -> None:
        """Apply the safety boundary before stdlib dispatch walks handlers."""
        _RedactionFilter().filter(record)
        current_dispatch(target_logger, record)

    setattr(
        _redacting_call_handlers,
        _CALL_HANDLERS_ID_ATTR,
        _CALL_HANDLERS_ID_VALUE,
    )
    setattr(
        _redacting_call_handlers,
        _CALL_HANDLERS_ORIGINAL_ATTR,
        current_dispatch,
    )
    # FIX: Handler snapshots cannot cover a handler added after configuration
    # or after output release while a worker still owns a redaction-safety
    # lease. Dispatch-level sanitization covers every handler present at emit
    # time, including new non-propagating child handlers and lastResort.
    setattr(logging.Logger, "callHandlers", _redacting_call_handlers)


def _remove_process_redaction_dispatch() -> None:
    """Restore the exact prior dispatcher after the final safety lease ends."""
    current_dispatch = logging.Logger.callHandlers
    if getattr(current_dispatch, _CALL_HANDLERS_ID_ATTR, None) != _CALL_HANDLERS_ID_VALUE:
        return
    original_dispatch = getattr(
        current_dispatch,
        _CALL_HANDLERS_ORIGINAL_ATTR,
        None,
    )
    if callable(original_dispatch):
        setattr(logging.Logger, "callHandlers", original_dispatch)


class _ThirdPartyFloorFilter(logging.Filter):
    """Allow first-party/Uvicorn records and gate other loggers at a floor."""

    _ums_smart_revenue_filter_id = _FLOOR_FILTER_ID

    def __init__(self, floor: int) -> None:
        """Store the current effective third-party floor."""
        super().__init__()
        self.floor = floor

    def filter(self, record: logging.LogRecord) -> bool:
        """Apply the floor without disabling Uvicorn's access contract."""
        return (
            _is_first_party_logger(record.name)
            or record.name == "uvicorn"
            or record.name.startswith("uvicorn.")
            or record.levelno >= self.floor
        )


class _RedactingFormatter(logging.Formatter):
    """Defense-in-depth for callers that use the formatter without filters."""

    def format(self, record: logging.LogRecord) -> str:
        """Sanitize rendered output and any pre-cached exception text."""
        rendered = super().format(record)
        if record.exc_text is not None:
            record.exc_text = _redact_log_text(record.exc_text)
        return _redact_log_text(rendered)


def build_log_formatter() -> logging.Formatter:
    """Return the UTC, millisecond-precision UMS formatter."""
    formatter = _RedactingFormatter(fmt=LOG_FORMAT)
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
# Blast Radius: Root/child handler filters, safety-lease-scoped LogRecord
#   dispatch, the shared UMS root handler, root level, and the
#   `ums_smart_revenue` logger level. Log records are sanitized for credentials,
#   guarded CMS ids, private SQL, structured keys, and line-breaking controls.
#   No authorization, finance, audit persistence, or export behavior changes.
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
def _third_party_floor_filter(floor: int) -> logging.Filter:
    """Return a handler filter enforcing the third-party floor by name.

    First-party (``ums_smart_revenue.*``) records pass unconditionally --
    the application knob governs them through the first-party logger level.
    Every other record must clear ``floor``: propagation never re-checks an
    ancestor logger's level, so an explicitly leveled dependency would
    otherwise publish below-floor output through the shared handler. The
    filter cannot silence first-party or audit output by construction.
    """
    return _ThirdPartyFloorFilter(floor)


def _adopt_persisted_logging_state(handler: logging.Handler) -> None:
    """Recover active leases and safety ownership after module-state reset."""
    persisted_levels = getattr(handler, _HANDLER_LEVELS_ATTR, {})
    if not _logging_state.lease_levels and isinstance(persisted_levels, dict):
        _logging_state.lease_levels = dict(persisted_levels)
    _logging_state.refcount = len(_logging_state.lease_levels)
    persisted_safety_leases: object = getattr(
        handler,
        _HANDLER_SAFETY_LEASES_ATTR,
        set(),
    )
    if not _logging_state.safety_leases and isinstance(
        persisted_safety_leases,
        (set, frozenset),
    ):
        _logging_state.safety_leases = set(persisted_safety_leases)
    _logging_state.safety_refcount = len(_logging_state.safety_leases)
    if _logging_state.previous_root_level is None:
        recovered_root = getattr(handler, _HANDLER_PREVIOUS_ROOT_ATTR, None)
        if isinstance(recovered_root, int):
            _logging_state.previous_root_level = recovered_root
    if _logging_state.previous_first_party_level is None:
        recovered_first_party = getattr(
            handler,
            _HANDLER_PREVIOUS_FIRST_PARTY_ATTR,
            None,
        )
        if isinstance(recovered_first_party, int):
            _logging_state.previous_first_party_level = recovered_first_party


def _apply_effective_output_level(
    root: logging.Logger,
    owner_handler: logging.Handler,
) -> None:
    """Apply the most verbose active lease and refresh handler floor filters."""
    effective_level = min(_logging_state.lease_levels.values())
    root_level = max(effective_level, THIRD_PARTY_LOG_LEVEL)
    root.setLevel(root_level)
    logging.getLogger(FIRST_PARTY_LOGGER_NAME).setLevel(effective_level)
    _replace_floor_filters(root, owner_handler, root_level)


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
    lease_id = secrets.token_hex(16)

    with _logging_lock:
        root = logging.getLogger()
        first_party = logging.getLogger(FIRST_PARTY_LOGGER_NAME)
        previous_root_level = root.level
        previous_first_party_level = first_party.level

        handler = installed_log_handler()
        installed = handler is None
        if handler is None:
            handler = logging.StreamHandler(sys.stderr if stream is None else stream)
            handler_name = UMS_LOG_HANDLER_NAME
            if logging.getHandlerByName(handler_name) is not None:
                # FIX: Do not overwrite a foreign handler's process-global name
                # registration. The private marker, not this suffix, proves
                # ownership during re-adoption and teardown.
                handler_name = f"{handler_name}-{secrets.token_hex(8)}"
            handler.set_name(handler_name)
            setattr(handler, _HANDLER_ID_ATTR, _HANDLER_ID_VALUE)
            setattr(handler, _HANDLER_LEVELS_ATTR, {})
            setattr(handler, _HANDLER_SAFETY_COUNT_ATTR, 0)
            setattr(handler, _HANDLER_SAFETY_LEASES_ATTR, set())
            setattr(handler, _HANDLER_REDACTION_BINDINGS_ATTR, ())
            setattr(handler, _HANDLER_FLOOR_BINDINGS_ATTR, ())
            handler.setFormatter(build_log_formatter())
        else:
            _adopt_persisted_logging_state(handler)
            if handler not in root.handlers:
                root.addHandler(handler)

        # Snapshot a new output-ownership epoch only when no output lease
        # survives. A redaction-only safety lease may legitimately retain the
        # detached handler between two app lifespans.
        if not _logging_state.lease_levels:
            _logging_state.previous_root_level = previous_root_level
            _logging_state.previous_first_party_level = previous_first_party_level
            setattr(handler, _HANDLER_PREVIOUS_ROOT_ATTR, previous_root_level)
            setattr(
                handler,
                _HANDLER_PREVIOUS_FIRST_PARTY_ATTR,
                previous_first_party_level,
            )

        _logging_state.lease_levels[lease_id] = numeric_level
        _logging_state.refcount = len(_logging_state.lease_levels)
        _logging_state.safety_leases.add(lease_id)
        _logging_state.safety_refcount = len(_logging_state.safety_leases)
        setattr(handler, _HANDLER_LEVELS_ATTR, dict(_logging_state.lease_levels))
        setattr(handler, _HANDLER_SAFETY_COUNT_ATTR, _logging_state.safety_refcount)
        setattr(handler, _HANDLER_SAFETY_LEASES_ATTR, set(_logging_state.safety_leases))
        _install_process_redaction_dispatch()

        effective_level = min(_logging_state.lease_levels.values())
        root_level = max(effective_level, THIRD_PARTY_LOG_LEVEL)
        if installed:
            logging.basicConfig(handlers=[handler], level=root_level)
            if handler not in root.handlers:
                root.addHandler(handler)
        # FIX: Add the replacement redaction generation before removing the
        # prior one. Concurrent worker logging therefore never sees a filter
        # gap while another lifespan configures or changes its level.
        _replace_redaction_filters(root, handler)
        _apply_effective_output_level(root, handler)
        return LoggingConfiguration(
            installed=installed,
            handler=handler,
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
            lease_id=lease_id,
            numeric_level=numeric_level,
        )


# ============================================================================
# Purpose: Release one token's output ownership independently from its
#   redaction-safety ownership. Restore levels/detach output when the final
#   output lease ends; filters remain until the final safety lease ends.
# Database/ORM: None.
# Standards: create_app is a FACTORY. One process can build many apps (the
#   test suite builds hundreds), so acquiring process-global logging state on
#   startup without releasing it would leak process-global state. Output level
#   recomputation is per lease; duplicate releases are per-token no-ops. A
#   bounded-shutdown survivor releases output promptly but retains redaction on
#   every configured handler and logging.lastResort until positive termination.
# Blast Radius: Root/first-party levels, shared output handler, and exact UMS
#   filter objects. No authorization, finance, audit persistence, or exports.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> called in the lifespan's
#     outer finally, after scheduler.close() and executor.close().
# ============================================================================
def release_logging_output(configuration: LoggingConfiguration) -> None:
    """Release output/level ownership while retaining redaction safety."""
    with _logging_lock:
        if configuration.output_released:
            return
        handler = configuration.handler
        if handler is None or not _is_owned_handler(handler):
            handler = installed_log_handler()
        if handler is not None:
            _adopt_persisted_logging_state(handler)
        configuration.output_released = True
        _logging_state.lease_levels.pop(configuration.lease_id, None)
        _logging_state.refcount = len(_logging_state.lease_levels)
        if handler is not None:
            setattr(handler, _HANDLER_LEVELS_ATTR, dict(_logging_state.lease_levels))
        if _logging_state.lease_levels:
            if handler is not None:
                _apply_effective_output_level(logging.getLogger(), handler)
            return

        root = logging.getLogger()
        if handler is not None:
            _remove_owned_filters(handler, safety=False)
            if handler in root.handlers:
                root.removeHandler(handler)
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
        logging.getLogger(FIRST_PARTY_LOGGER_NAME).setLevel(previous_first_party)
        _logging_state.previous_root_level = None
        _logging_state.previous_first_party_level = None


def release_logging_safety(configuration: LoggingConfiguration) -> None:
    """Release redaction only after this token's output ownership has ended."""
    with _logging_lock:
        if configuration.safety_released:
            return
        # Fail closed: a caller cannot discard the safety boundary while its
        # own app output lease is still live. restore_logging releases in the
        # only safe order, and the deferred watcher calls this after workers end.
        if not configuration.output_released:
            return
        handler = configuration.handler
        if handler is None or not _is_owned_handler(handler):
            handler = installed_log_handler()
        if handler is not None:
            _adopt_persisted_logging_state(handler)
        configuration.safety_released = True
        _logging_state.safety_leases.discard(configuration.lease_id)
        _logging_state.safety_refcount = len(_logging_state.safety_leases)
        if handler is not None:
            setattr(
                handler,
                _HANDLER_SAFETY_COUNT_ATTR,
                _logging_state.safety_refcount,
            )
            setattr(
                handler,
                _HANDLER_SAFETY_LEASES_ATTR,
                set(_logging_state.safety_leases),
            )
        configuration.released = configuration.output_released and configuration.safety_released
        if _logging_state.safety_refcount > 0:
            return
        if handler is not None:
            _remove_owned_filters(handler, safety=True)
            if not _logging_state.lease_levels:
                root = logging.getLogger()
                if handler in root.handlers:
                    root.removeHandler(handler)
                handler.close()
        _remove_process_redaction_dispatch()


def restore_logging(configuration: LoggingConfiguration) -> None:
    """Release one token's output first and its redaction safety second."""
    release_logging_output(configuration)
    release_logging_safety(configuration)


def installed_log_handler() -> logging.Handler | None:
    """Return the registered handler carrying the private ownership marker."""
    for handler in logging.getLogger().handlers:
        if _is_owned_handler(handler):
            return handler
    for handler_name in logging.getHandlerNames():
        registered_handler = logging.getHandlerByName(handler_name)
        if registered_handler is not None and _is_owned_handler(registered_handler):
            return registered_handler
    return None
