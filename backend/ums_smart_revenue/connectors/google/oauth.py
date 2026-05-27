"""google-auth refresh wrapper.

Parses the resolved secret payload into a google.oauth2.credentials.Credentials
and exposes refresh_credentials() that maps google.auth.exceptions.RefreshError
to OAuthRefreshError.

Required payload fields: refresh_token, client_id, client_secret, token_uri.
Optional (passed through if present): scopes.
"""
from __future__ import annotations

import json

from google.auth.exceptions import RefreshError
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
    missing = [f for f in _REQUIRED_FIELDS if not data.get(f)]
    if missing:
        raise MalformedSecretPayloadError(
            detail=f"missing fields: {', '.join(missing)}"
        )
    return Credentials(
        token=None,  # google-auth fetches on first refresh
        refresh_token=data["refresh_token"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        token_uri=data["token_uri"],
        scopes=data.get("scopes"),
    )


# ============================================================================
# Purpose: Force an OAuth token refresh before connector API calls begin.
# Database/ORM: None.
# Standards: Wrap google-auth RefreshError in OAuthRefreshError; no token leak.
# Blast Radius: Pre-start connector gate only. No run row or finance data is
#               written when refresh fails.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/errors.py -> OAuthRefreshError.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py -> Bucket A pre-start behavior.
# ============================================================================
def refresh_credentials(credentials: Credentials) -> None:
    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        raise OAuthRefreshError(inner=exc) from exc
