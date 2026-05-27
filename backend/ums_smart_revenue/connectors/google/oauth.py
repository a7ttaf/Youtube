"""google-auth refresh wrapper.

Parses the resolved secret payload into a google.oauth2.credentials.Credentials
and exposes refresh_credentials() that maps google.auth.exceptions.GoogleAuthError
to OAuthRefreshError.

Required payload fields: refresh_token, client_id, client_secret, token_uri.
Optional (passed through if present): scopes.
"""
from __future__ import annotations

import json

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretPayloadError,
    OAuthRefreshError,
)

_REQUIRED_FIELDS = ("refresh_token", "client_id", "client_secret", "token_uri")


# ============================================================================
# Purpose: Convert the resolved OAuth secret JSON into google-auth credentials.
# Database/ORM: None.
# Standards: Typed payload validation; no secret values in error messages.
# Blast Radius: Credential bootstrap only. Authorization, finance, audit,
#               Neo4j, and exports are unaffected until credentials are used.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/secret_resolver.py -> Supplies the raw JSON payload.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py -> Calls before live/dry-run dispatch.
# ============================================================================
def build_credentials_from_payload(payload: str) -> Credentials:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MalformedSecretPayloadError(detail=f"json: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise MalformedSecretPayloadError(detail="payload is not a JSON object")
    fields = {field: _validated_required_field(data, field) for field in _REQUIRED_FIELDS}
    scopes = _validated_scopes(data.get("scopes"))
    return Credentials(
        token=None,  # google-auth fetches on first refresh
        refresh_token=fields["refresh_token"],
        client_id=fields["client_id"],
        client_secret=fields["client_secret"],
        token_uri=fields["token_uri"],
        scopes=scopes,
    )


def _validated_required_field(data: dict[str, object], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MalformedSecretPayloadError(
            detail=f"{field} must be a non-empty string"
        )
    return value.strip()


def _validated_scopes(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise MalformedSecretPayloadError(
            detail="scopes must be a list of non-empty strings"
        )
    scopes: list[str] = []
    for scope in value:
        if not isinstance(scope, str) or not scope.strip():
            raise MalformedSecretPayloadError(
                detail="scopes must be a list of non-empty strings"
            )
        scopes.append(scope.strip())
    return scopes


# ============================================================================
# Purpose: Force an OAuth token refresh before connector API calls begin.
# Database/ORM: None.
# Standards: Wrap google-auth GoogleAuthError in OAuthRefreshError; no token leak.
# Blast Radius: Pre-start connector gate only. No run row or finance data is
#               written when refresh fails.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/errors.py -> OAuthRefreshError.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py -> Bucket A pre-start behavior.
# ============================================================================
def refresh_credentials(credentials: Credentials) -> None:
    try:
        credentials.refresh(Request())
    except GoogleAuthError as exc:
        raise OAuthRefreshError(inner=exc) from exc
