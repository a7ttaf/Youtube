from dataclasses import dataclass
from functools import lru_cache
from os import environ


DATABASE_URL_ENV = "UMS_DATABASE_URL"
TRUSTED_GATEWAY_TOKEN_ENV = "UMS_TRUSTED_GATEWAY_TOKEN"


@dataclass(frozen=True)
class AppSettings:
    database_url: str | None = None
    trusted_gateway_token: str | None = None


@lru_cache(maxsize=1)
def load_app_settings() -> AppSettings:
    raw_database_url = environ.get(DATABASE_URL_ENV)
    database_url = raw_database_url.strip() if raw_database_url else None
    raw_trusted_gateway_token = environ.get(TRUSTED_GATEWAY_TOKEN_ENV)
    trusted_gateway_token = raw_trusted_gateway_token.strip() if raw_trusted_gateway_token else None
    return AppSettings(
        database_url=database_url or None,
        trusted_gateway_token=trusted_gateway_token or None,
    )
