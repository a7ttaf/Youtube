"""httpx-based Google HTTP client used by B2.4 / B2.5 / B2.6 API clients.

Pre-request: credentials.before_request(...) is invoked on every send so
google-auth handles access-token refresh via its own state machine.

Retry policy (spec §7) is added in task 21; this commit covers the
happy-path 200 OK -> parsed JSON contract only.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


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
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(connect=timeout_connect, read=timeout_read, write=None, pool=None),
        )

    def request(
        self,
        *,
        method: str,
        url: str,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        headers: dict[str, str] = {}
        self._credentials.before_request(None, method, url, headers)
        response = self._client.request(
            method=method, url=url, params=dict(params or {}),
            json=json_body, headers=headers,
        )
        # Retry / error mapping in task 21. Happy path only here:
        return response.json()

    def close(self) -> None:
        self._client.close()
