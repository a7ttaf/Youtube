"""Runtime configuration loaded from UMS_* environment variables."""

from dataclasses import dataclass
from functools import lru_cache
from os import environ
from uuid import UUID

DATABASE_URL_ENV = "UMS_DATABASE_URL"
TRUSTED_GATEWAY_TOKEN_ENV = "UMS_TRUSTED_GATEWAY_TOKEN"
AUTHZ_SOURCE_ENV = "UMS_AUTHZ_SOURCE"
GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV = "UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID"

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


@lru_cache(maxsize=1)
def load_app_settings() -> AppSettings:
    """Load and validate cached application settings from environment variables."""
    raw_database_url = environ.get(DATABASE_URL_ENV)
    database_url = raw_database_url.strip() if raw_database_url else None
    raw_trusted_gateway_token = environ.get(TRUSTED_GATEWAY_TOKEN_ENV)
    trusted_gateway_token = (
        raw_trusted_gateway_token.strip() if raw_trusted_gateway_token else None
    )
    raw_authz_source = environ.get(AUTHZ_SOURCE_ENV)
    authz_source = (
        raw_authz_source.strip().lower() if raw_authz_source else AUTHZ_SOURCE_HEADERS
    )
    if authz_source not in ALLOWED_AUTHZ_SOURCES:
        allowed = ", ".join(sorted(ALLOWED_AUTHZ_SOURCES))
        raise ValueError(f"{AUTHZ_SOURCE_ENV} must be one of: {allowed}")
    return AppSettings(
        database_url=database_url or None,
        trusted_gateway_token=trusted_gateway_token or None,
        authz_source=authz_source,
        google_connector_service_actor_id=_load_google_connector_service_actor_id(),
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
        raise ValueError(
            f"{GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV} must be a valid UUID"
        ) from exc
    return str(parsed)
