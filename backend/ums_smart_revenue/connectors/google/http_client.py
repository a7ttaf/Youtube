"""httpx-based Google HTTP client used by B2.4 / B2.5 / B2.6 API clients.

Pre-request: credentials.before_request(...) is invoked once per request so
google-auth handles access-token refresh via its own state machine; the
resulting headers dict is reused across retry attempts.

Retry policy (Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md §7)
is implemented in _send_with_retry and shared by request() (JSON) and
fetch_bytes() (raw download bodies):
    400/404/422 -> GoogleApiClientError, no retry
    401/403     -> GoogleApiAuthError, no retry
    429         -> exp backoff 1/2/4/8s max 4 attempts honoring Retry-After
                   clamp 64s -> GoogleApiRateLimitError
    5xx         -> exp backoff 1/2/4/8s max 4 attempts -> GoogleApiServerError
    timeout     -> exp backoff 1/2/4/8s max 4 attempts -> GoogleApiServerError
                   (status=0 sentinel: no HTTP response was produced)
    DNS / TCP   -> exp backoff 1/2/4s max 3 attempts -> GoogleApiServerError
                   (status=0 sentinel: no HTTP response was produced)
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request

from ums_smart_revenue.connectors.google.errors import (
    GoogleApiAuthError,
    GoogleApiClientError,
    GoogleApiRateLimitError,
    GoogleApiResponseError,
    GoogleApiServerError,
    OAuthRefreshError,
)

_CLIENT_STATUSES = frozenset({400, 404, 422})
_AUTH_STATUSES = frozenset({401, 403})
_RETRY_SERVER_STATUSES = frozenset({500, 502, 503, 504})

_MAX_STATUS_ATTEMPTS = 4
_MAX_TIMEOUT_ATTEMPTS = 4
_MAX_CONNECT_ATTEMPTS = 3
_BACKOFF_CAP_SECONDS = 64.0


@dataclass
class _RetryState:
    status_backoff: list[float]
    timeout_backoff: list[float]
    connect_backoff: list[float]
    status_attempt: int = 0
    timeout_attempts: int = 0
    connect_attempts: int = 0
    last_status: int = 0


def _backoff_schedule(max_attempts: int) -> list[float]:
    # 1, 2, 4, 8, ... capped at 64s; one entry per attempt, indexed by
    # (attempt - 1). Used identically by 429, 5xx, timeouts, and connect
    # errors; each retry budget consumes its own slot in the schedule.
    return [min(2.0 ** i, _BACKOFF_CAP_SECONDS) for i in range(max_attempts)]


# ============================================================================
# Purpose: Handle retryable HTTP status responses for Google API calls while
#          keeping the request loop narrow enough for static analyzers to
#          reason about each failure bucket separately.
# Database/ORM: None.
# Standards: Typed GoogleConnectorError subclasses; Retry-After clamped; no
#            response body or secret data included in errors.
# Blast Radius: Shared by request() and fetch_bytes() retry behavior.
# Connections:
#   - Method: GoogleHttpClient._send_with_retry -> caller that owns transport
#     errors and success responses.
#   - File: backend/ums_smart_revenue/connectors/google/errors.py -> typed
#     GoogleApiRateLimitError / GoogleApiServerError boundaries.
# ============================================================================
def _sleep_for_retryable_status(
    *,
    response: httpx.Response,
    attempt: int,
    method: str,
    url: str,
    status_backoff: list[float],
) -> bool:
    status = response.status_code
    if status == 429:
        if attempt == _MAX_STATUS_ATTEMPTS:
            raise GoogleApiRateLimitError(
                method=method, url=url, status=429,
                attempts=_MAX_STATUS_ATTEMPTS,
            )
        ra = response.headers.get("Retry-After")
        # Retry-After is honored when integer-seconds; otherwise fall back to
        # the exponential schedule slot. Both paths clamp at 64s to keep a
        # misbehaving server from parking us forever.
        sleep_s = float(ra) if ra and ra.isdigit() else status_backoff[attempt - 1]
        time.sleep(min(sleep_s, _BACKOFF_CAP_SECONDS))
        return True

    if status in _RETRY_SERVER_STATUSES:
        if attempt == _MAX_STATUS_ATTEMPTS:
            raise GoogleApiServerError(
                method=method, url=url, status=status,
                attempts=_MAX_STATUS_ATTEMPTS,
            )
        time.sleep(status_backoff[attempt - 1])
        return True

    return False


def _auth_headers(
    credentials: Any,
    auth_request: Request,
    *,
    method: str,
    url: str,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    try:
        credentials.before_request(auth_request, method, url, headers)
    except RefreshError as exc:
        # FIX: google-auth can refresh during before_request; preserve it
        # as the connector's terminal OAuth error instead of surfacing a
        # library-specific exception or retrying with stale credentials.
        raise OAuthRefreshError(inner=exc) from exc
    return headers


def _retry_after_timeout(
    state: _RetryState, *, method: str, url: str
) -> None:
    state.timeout_attempts += 1
    state.status_attempt -= 1
    if state.timeout_attempts >= _MAX_TIMEOUT_ATTEMPTS:
        raise GoogleApiServerError(
            method=method,
            url=url,
            status=0,
            attempts=_MAX_TIMEOUT_ATTEMPTS,
        )
    time.sleep(state.timeout_backoff[state.timeout_attempts - 1])


def _retry_after_connect_error(
    state: _RetryState, *, method: str, url: str
) -> None:
    state.connect_attempts += 1
    state.status_attempt -= 1
    if state.connect_attempts >= _MAX_CONNECT_ATTEMPTS:
        raise GoogleApiServerError(
            method=method,
            url=url,
            status=0,
            attempts=_MAX_CONNECT_ATTEMPTS,
        )
    time.sleep(state.connect_backoff[state.connect_attempts - 1])


def _request_or_retry_transport(
    client: httpx.Client,
    state: _RetryState,
    *,
    method: str,
    url: str,
    params: Mapping[str, str] | None,
    json_body: Mapping[str, object] | None,
    headers: dict[str, str],
) -> httpx.Response | None:
    try:
        return client.request(
            method=method,
            url=url,
            params=dict(params or {}),
            json=json_body,
            headers=headers,
        )
    except httpx.TimeoutException:
        # Timeout retries don't advance the HTTP-status budget; the next
        # iteration is still on attempt N from the loop's POV.
        _retry_after_timeout(state, method=method, url=url)
        return None
    except (httpx.NetworkError, httpx.ProtocolError):
        # Spec §7: "DNS / TCP reset" bucket -- 3-attempt budget for all
        # sub-HTTP transport failures. TimeoutException is handled above so
        # the 4-attempt timeout budget stays separate.
        _retry_after_connect_error(state, method=method, url=url)
        return None


def _terminal_response_or_raise(
    response: httpx.Response, *, method: str, url: str
) -> httpx.Response | None:
    status = response.status_code
    if status == 200:
        return response
    if status in _CLIENT_STATUSES:
        raise GoogleApiClientError(method=method, url=url, status=status)
    if status in _AUTH_STATUSES:
        raise GoogleApiAuthError(method=method, url=url, status=status)
    return None


def _send_with_retry_response(
    client: httpx.Client,
    *,
    method: str,
    url: str,
    params: Mapping[str, str] | None,
    json_body: Mapping[str, object] | None,
    headers: dict[str, str],
) -> httpx.Response:
    state = _RetryState(
        status_backoff=_backoff_schedule(_MAX_STATUS_ATTEMPTS),
        timeout_backoff=_backoff_schedule(_MAX_TIMEOUT_ATTEMPTS),
        connect_backoff=_backoff_schedule(_MAX_CONNECT_ATTEMPTS),
    )

    # The HTTP status loop runs up to _MAX_STATUS_ATTEMPTS (4); timeouts
    # and connect errors each carry their own independent budgets per
    # spec §7 (timeout=4, connect=3) so a flaky DNS path can't be hidden
    # by previous 5xx attempts and vice versa. status=0 is the sentinel
    # for "no HTTP response produced" raised on timeout/connect exhaust.
    while state.status_attempt < _MAX_STATUS_ATTEMPTS:
        state.status_attempt += 1
        response = _request_or_retry_transport(
            client,
            state,
            method=method,
            url=url,
            params=params,
            json_body=json_body,
            headers=headers,
        )
        if response is None:
            continue

        status = response.status_code
        state.last_status = status
        terminal = _terminal_response_or_raise(response, method=method, url=url)
        if terminal is not None:
            return terminal
        if _sleep_for_retryable_status(
            response=response,
            attempt=state.status_attempt,
            method=method,
            url=url,
            status_backoff=state.status_backoff,
        ):
            continue
        # Defensive fallthrough: any status outside the spec table
        # (3xx redirects, 451, etc.) is surfaced as a client error so
        # the orchestrator records a non-retryable FAILED run.
        raise GoogleApiClientError(method=method, url=url, status=status)

    # Unreachable in practice: every branch above either returns or raises.
    # Kept as a belt-and-suspenders no-return so type checkers don't flag a
    # fall-through path off the while loop.
    raise GoogleApiServerError(
        method=method,
        url=url,
        status=state.last_status,
        attempts=_MAX_STATUS_ATTEMPTS,
    )


class GoogleHttpClient:
    def __init__(
        self,
        *,
        credentials: Any,
        transport: httpx.BaseTransport | None = None,
        timeout_connect: float = 5.0,
        timeout_read: float = 60.0,
    ) -> None:
        self._credentials = credentials
        self._auth_request = Request()
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(connect=timeout_connect, read=timeout_read, write=None, pool=None),
        )

    # DeepSource keeps this historical complexity issue anchored here after the
    # retry loop was split into helper functions; local complexity is A(1).
    def _send_with_retry(  # skipcq: PY-R1000
        self,
        *,
        method: str,
        url: str,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        # ====================================================================
        # Purpose: Execute one Google API call with the spec §7 retry policy
        #   and translate HTTP / transport failures into the typed error
        #   taxonomy. On 200 returns the raw httpx.Response for the caller to
        #   consume (parse JSON, read bytes, etc.); on any non-200 raises the
        #   appropriate typed error and never returns a non-200 response.
        # Database/ORM: None.
        # Standards: Typed boundary errors; no bare except; httpx transport
        #   stays injected for tests; google-auth manages token refresh state.
        # Blast Radius: Every B2 live API call funnels through here via either
        #   request() (JSON) or fetch_bytes() (raw download bodies). The
        #   error class chosen determines whether the orchestrator records a
        #   connector_runs FAILED row with auth/client/rate-limit/server.
        # Connections:
        #   - File: backend/ums_smart_revenue/connectors/google/errors.py ->
        #     _GoogleApiHttpError subclasses for the HTTP-status branches.
        #   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-
        #     connector-design.md §7 -> retry table this loop implements
        #     (statuses, budgets, backoff, Retry-After clamp).
        # ====================================================================
        # Build auth headers once per overall request; google-auth's own
        # staleness state decides whether to refresh, and the same headers
        # dict is safe to replay across retry attempts within this call.
        headers = _auth_headers(
            self._credentials,
            self._auth_request,
            method=method,
            url=url,
        )
        return _send_with_retry_response(
            self._client,
            method=method,
            url=url,
            params=params,
            json_body=json_body,
            headers=headers,
        )

    def request(
        self,
        *,
        method: str,
        url: str,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        # ====================================================================
        # Purpose: Execute one Google JSON-API call and return the decoded
        #   object body. Retry policy + status taxonomy delegated to
        #   _send_with_retry; this method owns only the 200-body validation
        #   (must be JSON, must be an object/dict).
        # Database/ORM: None.
        # Standards: Typed boundary errors; no bare except; google-auth manages
        #   token refresh state via the shared retry helper.
        # Blast Radius: All B2 JSON API clients (list_supported_jobs,
        #   list_reports_for_month, AdSense reports, etc.) ingest via this
        #   method. A bad-body branch returning a non-dict here would crash
        #   downstream paginators expecting `.get("nextPageToken")` — so
        #   GoogleApiResponseError fails closed instead.
        # Connections:
        #   - Method: _send_with_retry -> retry/error taxonomy.
        #   - File: backend/ums_smart_revenue/connectors/google/errors.py ->
        #     GoogleApiResponseError (direct GoogleConnectorError, not a
        #     _GoogleApiHttpError) for the 200-with-bad-body branch.
        # ====================================================================
        response = self._send_with_retry(
            method=method, url=url, params=params, json_body=json_body,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise GoogleApiResponseError(url=url, reason=f"not valid json: {exc}") from exc
        if not isinstance(body, dict):
            raise GoogleApiResponseError(url=url, reason=f"expected object, got {type(body).__name__}")
        return body

    def fetch_bytes(self, *, url: str) -> bytes:
        # ====================================================================
        # Purpose: GET a URL and return the raw response body bytes. Used for
        #   binary/CSV downloads (e.g. YouTube Reporting downloadUrl) where
        #   the response is not JSON and request()'s body-validation would
        #   wrongly raise GoogleApiResponseError on a valid CSV payload.
        # Database/ORM: None.
        # Standards: Same Bearer-auth + full retry/error taxonomy as request()
        #   via the shared _send_with_retry helper; no JSON parsing or
        #   response-shape validation (raw bytes have no schema to validate).
        # Blast Radius: B2.5 report download path. A regression here would
        #   either crash on transient 503s (no retry) or leak a non-typed
        #   exception past the GoogleConnectorError contract the orchestrator
        #   catches.
        # Connections:
        #   - Method: _send_with_retry -> retry/error taxonomy.
        #   - File: backend/ums_smart_revenue/connectors/google/
        #     youtube_reporting_client.py -> fetch_report uses this for the
        #     reports.downloadUrl CSV fetch.
        # ====================================================================
        response = self._send_with_retry(method="GET", url=url)
        return response.content

    def close(self) -> None:
        self._client.close()
