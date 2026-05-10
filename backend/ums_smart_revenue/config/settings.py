from dataclasses import dataclass
from os import environ


DATABASE_URL_ENV = "UMS_DATABASE_URL"


@dataclass(frozen=True)
class AppSettings:
    database_url: str | None = None


def load_app_settings() -> AppSettings:
    raw_database_url = environ.get(DATABASE_URL_ENV)
    database_url = raw_database_url.strip() if raw_database_url else None
    return AppSettings(database_url=database_url or None)
