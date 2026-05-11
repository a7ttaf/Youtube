from dataclasses import dataclass
from functools import lru_cache
from os import environ


DATABASE_URL_ENV = "UMS_DATABASE_URL"
TRUSTED_GATEWAY_TOKEN_ENV = "UMS_TRUSTED_GATEWAY_TOKEN"
AUTHZ_SOURCE_ENV = "UMS_AUTHZ_SOURCE"

AUTHZ_SOURCE_HEADERS = "headers"
AUTHZ_SOURCE_DATABASE = "database"
ALLOWED_AUTHZ_SOURCES = frozenset({AUTHZ_SOURCE_HEADERS, AUTHZ_SOURCE_DATABASE})


@dataclass(frozen=True)
class AppSettings:
    database_url: str | None = None
    trusted_gateway_token: str | None = None
    authz_source: str = AUTHZ_SOURCE_HEADERS


@lru_cache(maxsize=1)
def load_app_settings() -> AppSettings:
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
    )
