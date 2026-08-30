# ============================================================================
# Purpose: Load process-level runtime settings from UMS_* environment
#   variables into the frozen AppSettings dataclass (cached via lru_cache),
#   including the connector job-executor and group-sync scheduler flags the
#   app boot wiring enforces fail-fast, and the bootstrap tenant's declared
#   ISO-4217 primary currency.
# Database/ORM: None.
# Standards: every setting is read through an explicit UMS_* env constant;
#   strict truthy/falsy token parsing; no implicit defaults for identity or
#   URL values. Callers consume load_app_settings(), never os.environ
#   directly.
# Blast Radius: App boot wiring only -- the feature flags gate thread-
#   spawning workers at startup. No authorization, finance, audit, or export
#   behavior. The primary-currency setting is a DECLARED label only: it never
#   converts an amount and never reaches a fact row.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> create_app consumes
#     AppSettings and enforces the cross-flag fail-fast contract;
#     _bootstrap_tenant stamps tenant_primary_currency on the headers-mode
#     bootstrap tenant.
#   - File: backend/ums_smart_revenue/db/iso_4217_2026_05.py -> immutable
#     repository snapshot used as the currency-code membership authority.
# ============================================================================
"""Runtime configuration loaded from UMS_* environment variables."""

import re
from dataclasses import dataclass
from functools import lru_cache
from os import environ
from uuid import UUID

from ums_smart_revenue.db.iso_4217_2026_05 import ISO_4217_CURRENCIES_2026_05

DATABASE_URL_ENV = "UMS_DATABASE_URL"
TRUSTED_GATEWAY_TOKEN_ENV = "UMS_TRUSTED_GATEWAY_TOKEN"
AUTHZ_SOURCE_ENV = "UMS_AUTHZ_SOURCE"
GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV = "UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID"
CONNECTOR_JOB_EXECUTOR_ENABLED_ENV = "UMS_CONNECTOR_JOB_EXECUTOR_ENABLED"
CONNECTOR_JOB_MAX_WORKERS_ENV = "UMS_CONNECTOR_JOB_MAX_WORKERS"
CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV = "UMS_CONNECTOR_JOB_STALE_RUNNING_HOURS"
GROUP_SYNC_SCHEDULE_ENABLED_ENV = "UMS_GROUP_SYNC_SCHEDULE_ENABLED"
GROUP_SYNC_INTERVAL_HOURS_ENV = "UMS_GROUP_SYNC_INTERVAL_HOURS"
TENANT_PRIMARY_CURRENCY_ENV = "UMS_TENANT_PRIMARY_CURRENCY"

_TRUTHY_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSY_TOKENS = frozenset({"0", "false", "no", "off", ""})

# Shape guard for ISO 4217 alphabetic codes. Membership in the immutable
# repository snapshot below is authoritative; the regex alone must not accept
# arbitrary three-letter strings such as ``ZZZ``.
_ISO_4217_ALPHA_CODE = re.compile(r"[A-Z]{3}")
_ISO_4217_CODES = frozenset(
    str(entry["code"]) for entry in ISO_4217_CURRENCIES_2026_05
)

# Phase 1 of the EGP program builds the currency spine WITHOUT flipping it:
# this default stays "USD" so wiring the setting changes no behaviour. The
# operator-decided flip to EGP is a later, deliberate edit of this one value
# (or a UMS_TENANT_PRIMARY_CURRENCY entry in the deployment environment).
DEFAULT_TENANT_PRIMARY_CURRENCY = "USD"

AUTHZ_SOURCE_HEADERS = "headers"
AUTHZ_SOURCE_DATABASE = "database"
ALLOWED_AUTHZ_SOURCES = frozenset({AUTHZ_SOURCE_HEADERS, AUTHZ_SOURCE_DATABASE})


@dataclass(frozen=True)
class AppSettings:
    """Process-level runtime settings loaded from UMS_* environment variables."""

    database_url: str | None = None
    trusted_gateway_token: str | None = None
    authz_source: str = AUTHZ_SOURCE_HEADERS
    # Canonical string form of the UUID identifying the system actor that
    # records connector-emitted audit events (T36, B2.6). Stored as ``str``
    # so it slots straight into ``UserPrincipal.user_id`` without a UUID
    # round-trip; the loader validates UUID format before assigning.
    #
    # Note: missing env is permitted at load time (parallels ``database_url``)
    # so non-connector workloads can boot without this variable set. The
    # fail-closed boundary lives at call time in
    # ``connectors/google/audit.py::build_connector_service_principal``,
    # which raises ``ValueError`` when the value is needed but absent.
    google_connector_service_actor_id: str | None = None
    # In-process connector-job executor toggle + tuning. Fail-closed OFF:
    # when False, POST /connectors/jobs returns 503 (explicit refusal, never a
    # silent fallback to the old recorder). max_workers / stale_running_hours
    # are positive ints validated at load time so a typo fails closed at boot.
    connector_job_executor_enabled: bool = False
    connector_job_max_workers: int = 1
    connector_job_stale_running_hours: int = 6
    # In-process CMS group-sync scheduler toggle + tick interval. Fail-closed
    # OFF: when False, create_app spawns no scheduler thread and boots exactly
    # as it does today. When True, create_app FAILS FAST (ValueError) at boot
    # unless the connector-job executor is also enabled and a service actor id
    # is configured -- a scheduler with nothing to submit to, or no identity
    # to submit jobs as, is a misconfiguration caught at boot, not silently at
    # the first tick. interval_hours is a positive int validated at load time,
    # same contract as max_workers.
    group_sync_schedule_enabled: bool = False
    group_sync_interval_hours: int = 24
    # ISO-4217 alphabetic code DECLARED as the bootstrap tenant's primary
    # currency by ``app._bootstrap_tenant`` (headers/trusted-gateway mode,
    # which fabricates its tenant instead of reading the ``tenants`` row).
    # Database-authz mode ignores this entirely -- the resolver reads
    # ``tenants.primary_currency``, which stays the source of truth there.
    # This is a LABEL, never a rate: UMS performs no currency conversion, so
    # changing it re-labels the declared tenant currency and converts nothing.
    tenant_primary_currency: str = DEFAULT_TENANT_PRIMARY_CURRENCY


@lru_cache(maxsize=1)
def load_app_settings() -> AppSettings:
    """Load and validate cached application settings from environment variables."""
    raw_database_url = environ.get(DATABASE_URL_ENV)
    database_url = raw_database_url.strip() if raw_database_url else None
    raw_trusted_gateway_token = environ.get(TRUSTED_GATEWAY_TOKEN_ENV)
    trusted_gateway_token = raw_trusted_gateway_token.strip() if raw_trusted_gateway_token else None
    raw_authz_source = environ.get(AUTHZ_SOURCE_ENV)
    authz_source = raw_authz_source.strip().lower() if raw_authz_source else AUTHZ_SOURCE_HEADERS
    if authz_source not in ALLOWED_AUTHZ_SOURCES:
        allowed = ", ".join(sorted(ALLOWED_AUTHZ_SOURCES))
        raise ValueError(f"{AUTHZ_SOURCE_ENV} must be one of: {allowed}")
    return AppSettings(
        database_url=database_url or None,
        trusted_gateway_token=trusted_gateway_token or None,
        authz_source=authz_source,
        google_connector_service_actor_id=_load_google_connector_service_actor_id(),
        connector_job_executor_enabled=_load_bool(
            CONNECTOR_JOB_EXECUTOR_ENABLED_ENV, default=False
        ),
        connector_job_max_workers=_load_int(CONNECTOR_JOB_MAX_WORKERS_ENV, default=1),
        connector_job_stale_running_hours=_load_int(
            CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV, default=6
        ),
        group_sync_schedule_enabled=_load_bool(GROUP_SYNC_SCHEDULE_ENABLED_ENV, default=False),
        group_sync_interval_hours=_load_int(GROUP_SYNC_INTERVAL_HOURS_ENV, default=24),
        tenant_primary_currency=_load_currency_code(
            TENANT_PRIMARY_CURRENCY_ENV, default=DEFAULT_TENANT_PRIMARY_CURRENCY
        ),
    )


# ============================================================================
# Purpose: Parse UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID at settings-load time,
#          rejecting malformed UUIDs so misconfigured deployments fail fast
#          instead of producing garbled audit actor identifiers later.
# Database/ORM: None directly; the parsed string is consumed by
#               backend/ums_smart_revenue/connectors/google/audit.py
#               (T36) when building the connector service principal that
#               appears as the actor on connector-emitted audit rows.
# Standards: Two-tier validation contract -- "missing env -> None (lazy at
#            load time)" so non-connector workloads can boot without this
#            variable set, vs. "present-but-malformed -> ValueError
#            (fail-closed at load time)" so a typo cannot silently produce a
#            garbage actor id. The fail-closed-when-actually-needed boundary
#            for the None case lives in
#            connectors/google/audit.py::build_connector_service_principal,
#            which raises at first use. ValueError on malformed input carries
#            the env name for operator triage.
# Blast Radius: Audit actor identity for B2.6 connector audit emitters.
#               No direct finance, authorization, or graph projection
#               impact; the identity is informational on the audit row
#               (`user_id` falls back to a sentinel in
#               SqlAlchemyAuditSink when the UUID is not a real user).
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/audit.py ->
#     build_connector_service_principal reads this value via
#     load_app_settings() to populate UserPrincipal.user_id.
#   - File: backend/ums_smart_revenue/auth/sql_audit_sink.py ->
#     append() handles unknown actor UUIDs by stashing the raw value in
#     details["actor_user_id"], so a non-user UUID here is safe.
# ============================================================================
def _load_google_connector_service_actor_id() -> str | None:
    """Return the canonical UUID string for the connector service actor, or None.

    Missing/blank env -> ``None`` (lazy; deferred fail-closed lives in
    ``build_connector_service_principal``). Present-but-malformed env ->
    ``ValueError`` so misconfigured deployments fail fast at boot.

    Raises:
        ValueError: If ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`` is present
            but not a valid UUID.
    """
    raw = environ.get(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV)
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        parsed = UUID(candidate)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV} must be a valid UUID") from exc
    return str(parsed)


# ============================================================================
# Purpose: Parse a UMS_* boolean env var at settings-load time with a two-tier
#          contract: missing/blank -> default; recognised truthy/falsy token ->
#          the matching bool; unrecognised token -> ValueError carrying the env
#          name so a misconfigured deployment fails fast at boot.
# Database/ORM: None.
# Standards: Mirrors _load_google_connector_service_actor_id's "missing ->
#            default vs malformed -> fail-closed" idiom; case/whitespace
#            insensitive.
# Blast Radius: Connector-job executor enablement (route 503 vs submit). No
#               finance, audit, or graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> create_app gates the executor
#     construction on connector_job_executor_enabled.
# ============================================================================
def _load_bool(env_name: str, *, default: bool) -> bool:
    """Parse a UMS_* boolean env var, failing fast on an unrecognised token."""
    raw = environ.get(env_name)
    if raw is None:
        return default
    candidate = raw.strip().lower()
    if candidate in _TRUTHY_TOKENS:
        return True
    if candidate in _FALSY_TOKENS:
        return False
    allowed = ", ".join(sorted(_TRUTHY_TOKENS | (_FALSY_TOKENS - {""})))
    raise ValueError(f"{env_name} must be one of: {allowed}")


# ============================================================================
# Purpose: Parse a UMS_* positive-int env var at settings-load time with a
#          two-tier contract: missing/blank -> default; present-but-malformed
#          or non-positive -> ValueError carrying the env name (a 0 or negative
#          worker count would build a broken ThreadPoolExecutor / stale window).
# Database/ORM: None.
# Standards: Mirrors the UUID loader's fail-closed-at-boot idiom; rejects <= 0.
# Blast Radius: Executor pool size + orphan-supersede age threshold. No finance,
#               audit, or graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> consumes
#     max_workers; the route consumes stale_running_hours.
# ============================================================================
def _load_int(env_name: str, *, default: int) -> int:
    """Parse a UMS_* positive-int env var, failing fast on bad/non-positive."""
    raw = environ.get(env_name)
    if raw is None:
        return default
    candidate = raw.strip()
    if not candidate:
        return default
    try:
        parsed = int(candidate)
    except ValueError as exc:
        raise ValueError(f"{env_name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{env_name} must be a positive integer")
    return parsed


# ============================================================================
# Purpose: Parse a UMS_* ISO-4217 currency-code env var at settings-load time
#          with the same two-tier contract as the other loaders: missing/blank
#          -> default; malformed or absent-from-snapshot -> ValueError carrying
#          the env name, so a typo ("US", "usd", "US$", "ZZZ") fails the
#          process at boot rather than producing an invalid tenant label.
# Database/ORM: None. The parsed code is a declared label copied onto the
#               fabricated bootstrap Tenant dataclass; it is never written to
#               a fact table and never compared against a stored amount.
# Standards: Mirrors _load_int / _load_bool's "missing -> default vs malformed
#            -> fail-closed" idiom. Case is NOT coerced: lowercase input is
#            rejected rather than silently upper-cased, because a currency code
#            an operator typed wrong is a configuration error worth surfacing,
#            and the tenants CHECK is shape-only (it would accept ``ZZZ``).
# Blast Radius: The declared primary currency label on the headers-mode
#               bootstrap tenant. NO conversion, NO FX, no finance math, no
#               fact-pipeline currency gate -- UMS converts nothing, so this
#               value can only re-label, never re-value.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> _bootstrap_tenant stamps the
#     parsed code onto the fabricated Tenant.
#   - File: backend/ums_smart_revenue/db/tenant_models.py -> the
#     ck_tenants_primary_currency_iso4217 shape CHECK remains compatible.
#   - File: backend/ums_smart_revenue/db/iso_4217_2026_05.py -> the immutable
#     membership snapshot this loader enforces.
# ============================================================================
def _load_currency_code(env_name: str, *, default: str) -> str:
    """Parse a UMS_* ISO-4217 code, rejecting malformed or unknown values."""
    raw = environ.get(env_name)
    if raw is None:
        return default
    candidate = raw.strip()
    if not candidate:
        return default
    if (
        _ISO_4217_ALPHA_CODE.fullmatch(candidate) is None
        or candidate not in _ISO_4217_CODES
    ):
        raise ValueError(
            f"{env_name} must be a 3-letter uppercase ISO 4217 code "
            "from the repository snapshot"
        )
    return candidate
