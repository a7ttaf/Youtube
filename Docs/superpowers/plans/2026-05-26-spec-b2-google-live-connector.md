# Spec B2 - Live Google Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a live Google connector that fetches YouTube Reporting, YouTube Analytics, and AdSense Management reports for a given (tenant, account, month) and persists them into the PR #43 source-row substrate, with revenue-facts produced via C1 (PR #44) for YouTube paths and AdSense kept as ingestion/audit evidence.

**Architecture:** Six PRs (B2.1 → B2.6) ship sequentially. B2.1 adds secret resolver dispatch + Google OAuth refresh; B2.2 adds blob storage + raw_report_files lifecycle helpers; B2.3 adds `connector_runs` + `connector_run_raw_files` ORM/repo + Alembic; B2.4 adds google-auth + httpx base + YouTube Reporting client + `run_one()` orchestrator + CLI; B2.5 adds YouTube Analytics targeted channel ingestion; B2.6 adds AdSense Management client + audit wiring + mock end-to-end ingestion gate. All B2 PRs ship on the `spec/b2-google-connector` feature branch; each PR is one or more commits, ending with a validation-gate commit.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL 18-alpine (disposable, port 55432), pytest, ruff, httpx, google-auth, structlog. No `google-api-python-client`. No live Google calls in PR gates.

**Spec:** `Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md` is the contract. This plan implements it task-by-task. Open the spec alongside this plan; sections referenced as "spec 5.4" etc. point to the spec's section numbers.

**Branch:** `spec/b2-google-connector` (already checked out; spec commit `abedffd` is the first commit on this branch).

**Commit hygiene (every commit):**
- Co-Authored-By: `Claude Opus 4.7 <noreply@anthropic.com>` trailer.
- Stage specific files only (never `git add -A` / `git add .`).
- HEREDOC for multi-line messages.
- Never skip hooks (`--no-verify`, `--no-gpg-sign` forbidden unless explicitly requested).
- Use single-quoted heredocs (`<<'EOF'`) to prevent variable expansion.

**Validation gate (every PR's last task):**
- `python -m ruff check backend tests scripts`
- `python -m pytest -q <PR-specific test paths>`
- `python scripts/run_validation_gate.py`
- `git diff --check`

**Disposable PostgreSQL for migration tests:**
```bash
docker run -d --name ums-pg-test -p 55432:5432 \
    -e POSTGRES_USER=ums -e POSTGRES_PASSWORD=ums -e POSTGRES_DB=ums \
    postgres:18-alpine
# Then: TEST_DATABASE_URL=postgresql+psycopg://ums:ums@localhost:55432/ums pytest -q <postgres test>
```
Migrations run from repo root with `PYTHONPATH=backend alembic ...` — never `cd backend`.

---

## File structure

```text
backend/ums_smart_revenue/
  connectors/
    google/                                # B2.1, B2.4, B2.5, B2.6
      __init__.py                          # B2.1 - re-exports public API
      errors.py                            # B2.1 - GoogleConnectorError hierarchy
      secret_resolver.py                   # B2.1 - dispatching Protocol + registry
      gcp_secret_manager.py                # B2.1 - GCP backend
      local_secret_resolver.py             # B2.1 - test backend
      oauth.py                             # B2.1 - google-auth refresh wrapper
      http_client.py                       # B2.4 - httpx base with retry policy
      report_type_whitelist.py             # B2.4 - YT Reporting supported types
      registry.py                          # B2.4 - CLI --connector dispatch
      youtube_reporting_client.py          # B2.4
      youtube_analytics_client.py          # B2.5
      adsense_management_client.py         # B2.6
      audit.py                             # B2.6 - service principal + emitters
    runs/                                  # B2.2, B2.3, B2.4
      __init__.py
      blob_storage.py                      # B2.2 - Protocol + GCS + file-store backends
      raw_file_helpers.py                  # B2.2 - mark_parsed, mark_failed (+ guards)
      repository.py                        # B2.3 - start_run, finish_run, link_raw_file
      orchestrator.py                      # B2.4 - run_one() public surface
  db/
    connector_models.py                    # B2.3 - ConnectorRunORM, ConnectorRunRawFileORM
    alembic/versions/
      20260527_0001_connector_runs.py      # B2.3 - migration
scripts/
  run_google_connector.py                  # B2.4 - CLI entrypoint
tests/
  connectors/
    google/                                # per-client unit tests
      __init__.py
      conftest.py                          # B2.4 - shared httpx.MockTransport helpers
      test_errors.py                       # B2.1
      test_secret_resolver.py              # B2.1
      test_gcp_secret_manager.py           # B2.1
      test_local_secret_resolver.py        # B2.1
      test_oauth.py                        # B2.1
      test_blob_storage.py                 # B2.2
      test_raw_file_helpers.py             # B2.2
      test_http_client.py                  # B2.4
      test_youtube_reporting_client.py     # B2.4
      test_registry.py                     # B2.4
      test_orchestrator.py                 # B2.4
      test_run_one_cli.py                  # B2.4
      test_youtube_analytics_client.py     # B2.5
      test_adsense_management_client.py    # B2.6
      test_audit_wiring.py                 # B2.6
    runs/
      __init__.py
      test_repository.py                   # B2.3
      test_ingestion_gate.py               # B2.6 (mock end-to-end)
  db/
    test_connector_runs_migration_postgres.py  # B2.3
```

Files **NOT** touched by B2 (load-bearing):
- `backend/ums_smart_revenue/connectors/google_source_parsers/*` (PR #43)
- `backend/ums_smart_revenue/connectors/google_source_rows/*` (PR #43)
- `backend/ums_smart_revenue/finance/google_source_normalizer.py` (PR #44)
- `backend/ums_smart_revenue/db/source_models.py` (PR #43)
- `backend/ums_smart_revenue/auth/*` (existing audit/permissions/principal infra)
- `backend/ums_smart_revenue/db/report_models.py` (RawReportFileORM; only its migration gets one additive UNIQUE constraint)
- `backend/ums_smart_revenue/db/security_models.py` (AuditLogORM, users)

---

## Per-PR task map

| PR | Tasks | Final commit titles |
|---|---|---|
| **B2.1** | T1–T6 | `feat(b2.1): add Google connector error hierarchy`<br>`feat(b2.1): add secret resolver dispatch`<br>`feat(b2.1): add GCP Secret Manager resolver`<br>`feat(b2.1): add local-secret:// resolver`<br>`feat(b2.1): add Google OAuth refresh wrapper`<br>`docs(b2.1): update plan/backlog markers for credential foundation` |
| **B2.2** | T7–T12 | `feat(b2.2): add blob storage Protocol + file-store backend`<br>`feat(b2.2): add GCS blob storage backend`<br>`feat(b2.2): add deterministic blob path + checksum upload`<br>`feat(b2.2): add raw_file mark_parsed helper`<br>`feat(b2.2): add raw_file mark_failed helper`<br>`docs(b2.2): update plan/backlog markers for blob + raw_file lifecycle` |
| **B2.3** | T13–T19 | `feat(b2.3): add ConnectorRunORM + ConnectorRunRawFileORM models`<br>`feat(b2.3): add Alembic migration for connector_runs (+ raw_report_files UNIQUE)`<br>`feat(b2.3): add connector_runs repository - start_run`<br>`feat(b2.3): add connector_runs repository - finish_run`<br>`feat(b2.3): add connector_runs repository - link_raw_file`<br>`test(b2.3): PostgreSQL migration round-trip + index assertions`<br>`docs(b2.3): update plan/backlog markers for run tracking` |
| **B2.4** | T20–T31 | (12 commits — see PR section) |
| **B2.5** | T32–T34 | (3 commits — see PR section) |
| **B2.6** | T35–T40 | (6 commits — see PR section) |

---

## PR B2.1 — Credential foundation

Spec reference: §5.1 (public surface), §6.5 (error taxonomy rows 1–5 + 8), §9.3 B2.1 (test coverage).

### Task 1: GoogleConnectorError hierarchy + B2.1 error subclasses

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/__init__.py` (empty)
- Create: `backend/ums_smart_revenue/connectors/google/errors.py`
- Create: `tests/connectors/google/__init__.py` (empty)
- Create: `tests/connectors/google/test_errors.py`

- [ ] **Step 1: Write the failing test**

`tests/connectors/google/test_errors.py`:
```python
"""B2.1 error hierarchy tests.

Every B2 error subclasses GoogleConnectorError so the orchestrator can catch
the whole family in one except clause. Subclasses are distinguishable by
isinstance.
"""
from __future__ import annotations

import pytest

from ums_smart_revenue.connectors.google.errors import (
    GoogleConnectorError,
    MalformedSecretPayloadError,
    MalformedSecretUriError,
    OAuthRefreshError,
    SecretFetchError,
    SecretNotFoundError,
    UnsupportedSecretSchemeError,
)


def test_all_b21_errors_subclass_google_connector_error() -> None:
    for cls in (
        UnsupportedSecretSchemeError,
        MalformedSecretUriError,
        SecretNotFoundError,
        SecretFetchError,
        MalformedSecretPayloadError,
        OAuthRefreshError,
    ):
        assert issubclass(cls, GoogleConnectorError), cls.__name__


def test_unsupported_secret_scheme_carries_scheme() -> None:
    err = UnsupportedSecretSchemeError(scheme="aws-secretsmanager")
    assert err.scheme == "aws-secretsmanager"
    assert "aws-secretsmanager" in str(err)


def test_secret_not_found_carries_ref() -> None:
    err = SecretNotFoundError(ref="gcp-secret-manager://projects/x/secrets/y/versions/latest")
    assert "y" in str(err)


def test_oauth_refresh_carries_inner_class_name() -> None:
    inner = RuntimeError("token revoked")
    err = OAuthRefreshError(inner=inner)
    assert "RuntimeError" in str(err)
    assert err.inner is inner
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest -q tests/connectors/google/test_errors.py
```
Expected: `ModuleNotFoundError: No module named 'ums_smart_revenue.connectors.google.errors'`.

- [ ] **Step 3: Implement minimal code to pass**

`backend/ums_smart_revenue/connectors/google/errors.py`:
```python
"""Typed error hierarchy for the B2 live Google connector.

Every error raised inside connectors/google or connectors/runs subclasses
GoogleConnectorError so the orchestrator's outer handler can catch the whole
family in one except clause and translate it into a connector_runs FAILED
row plus an audit event with error_class=<class name>.
"""
from __future__ import annotations


class GoogleConnectorError(Exception):
    """Root of all B2 typed errors."""


class UnsupportedSecretSchemeError(GoogleConnectorError):
    def __init__(self, *, scheme: str) -> None:
        super().__init__(f"unsupported secret scheme: {scheme}")
        self.scheme = scheme


class MalformedSecretUriError(GoogleConnectorError):
    def __init__(self, *, ref: str) -> None:
        super().__init__(f"malformed secret URI: {ref}")
        self.ref = ref


class SecretNotFoundError(GoogleConnectorError):
    def __init__(self, *, ref: str) -> None:
        super().__init__(f"secret not found: {ref}")
        self.ref = ref


class SecretFetchError(GoogleConnectorError):
    def __init__(self, *, ref: str, inner: Exception) -> None:
        super().__init__(f"secret fetch failed for {ref}: {type(inner).__name__}")
        self.ref = ref
        self.inner = inner


class MalformedSecretPayloadError(GoogleConnectorError):
    def __init__(self, *, detail: str) -> None:
        super().__init__(f"malformed secret payload: {detail}")
        self.detail = detail


class OAuthRefreshError(GoogleConnectorError):
    def __init__(self, *, inner: Exception) -> None:
        super().__init__(f"oauth refresh failed: {type(inner).__name__}")
        self.inner = inner
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest -q tests/connectors/google/test_errors.py
```
Expected: `4 passed`.

- [ ] **Step 5: Lint**

```bash
python -m ruff check backend/ums_smart_revenue/connectors/google/errors.py tests/connectors/google/test_errors.py
```
Expected: `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/connectors/google/__init__.py \
        backend/ums_smart_revenue/connectors/google/errors.py \
        tests/connectors/google/__init__.py \
        tests/connectors/google/test_errors.py
git commit -m "$(cat <<'EOF'
feat(b2.1): add Google connector error hierarchy

Adds GoogleConnectorError root plus B2.1 subclasses:
UnsupportedSecretSchemeError, MalformedSecretUriError, SecretNotFoundError,
SecretFetchError, MalformedSecretPayloadError, OAuthRefreshError. Later
slices add B2.2/B2.4 subclasses (blob, HTTP, lifecycle) under the same root
so the orchestrator can catch the family in one except clause.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Secret resolver dispatch (Protocol + registry)

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/secret_resolver.py`
- Create: `tests/connectors/google/test_secret_resolver.py`

- [ ] **Step 1: Write the failing test**

`tests/connectors/google/test_secret_resolver.py`:
```python
"""Secret resolver dispatch tests.

A dispatcher maps a URI scheme (e.g., 'gcp-secret-manager') to a resolver
implementation. Unknown / unimplemented schemes raise
UnsupportedSecretSchemeError; ORM-accepted prefixes that aren't implemented
(aws-secretsmanager://, secret-manager://, vault://, kms://, azure-keyvault://)
are intentionally unknown until a future credential-lifecycle PR.
"""
from __future__ import annotations

import pytest

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    UnsupportedSecretSchemeError,
)
from ums_smart_revenue.connectors.google.secret_resolver import (
    SecretResolver,
    register_resolver,
    resolve_secret,
)


class _StubResolver:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def resolve(self, ref: str) -> str:
        self.calls.append(ref)
        return self.payload


def test_resolve_secret_dispatches_to_registered_scheme(monkeypatch) -> None:
    stub = _StubResolver(payload='{"refresh_token": "x"}')
    register_resolver(scheme="local-secret", resolver=stub)
    out = resolve_secret("local-secret://my-key")
    assert out == '{"refresh_token": "x"}'
    assert stub.calls == ["local-secret://my-key"]


def test_resolve_secret_raises_for_unknown_scheme() -> None:
    with pytest.raises(UnsupportedSecretSchemeError) as ctx:
        resolve_secret("aws-secretsmanager://my-arn")
    assert ctx.value.scheme == "aws-secretsmanager"


@pytest.mark.parametrize(
    "ref",
    [
        "",                       # empty
        "no-scheme",              # missing ://
        "gcp-secret-manager:/",   # malformed delimiter
        "://no-scheme-name",      # empty scheme
    ],
)
def test_resolve_secret_raises_for_malformed_uri(ref: str) -> None:
    with pytest.raises(MalformedSecretUriError):
        resolve_secret(ref)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest -q tests/connectors/google/test_secret_resolver.py
```
Expected: `ModuleNotFoundError: No module named 'ums_smart_revenue.connectors.google.secret_resolver'`.

- [ ] **Step 3: Implement**

`backend/ums_smart_revenue/connectors/google/secret_resolver.py`:
```python
"""Secret resolver dispatch.

resolve_secret(ref) parses the URI scheme and dispatches to a registered
SecretResolver. Implemented schemes (registered at app/test boot):
- gcp-secret-manager:// -> GcpSecretManagerResolver (B2.1)
- local-secret://       -> LocalSecretResolver (B2.1, test only)

Other ORM-accepted prefixes (aws-secretsmanager://, secret-manager://,
vault://, kms://, azure-keyvault://) are intentionally unregistered until a
future credential-lifecycle PR. They raise UnsupportedSecretSchemeError so
B2 fails closed instead of silently dropping the secret.
"""
from __future__ import annotations

from typing import Protocol

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    UnsupportedSecretSchemeError,
)


class SecretResolver(Protocol):
    def resolve(self, ref: str) -> str:
        """Return the secret payload as a string. Raise SecretNotFoundError /
        SecretFetchError on backend failure."""


_REGISTRY: dict[str, SecretResolver] = {}


def register_resolver(*, scheme: str, resolver: SecretResolver) -> None:
    _REGISTRY[scheme] = resolver


def _parse_scheme(ref: str) -> str:
    if not ref or "://" not in ref:
        raise MalformedSecretUriError(ref=ref)
    scheme, _, rest = ref.partition("://")
    if not scheme or not rest:
        raise MalformedSecretUriError(ref=ref)
    return scheme


def resolve_secret(ref: str) -> str:
    scheme = _parse_scheme(ref)
    resolver = _REGISTRY.get(scheme)
    if resolver is None:
        raise UnsupportedSecretSchemeError(scheme=scheme)
    return resolver.resolve(ref)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest -q tests/connectors/google/test_secret_resolver.py
```
Expected: `7 passed` (1 dispatch + 1 unknown + 4 parametrized + 1 fixture).

Note: `register_resolver` mutates module-level state. The test using `register_resolver` should clean up via a fixture. Update test file to add at the top of the file:
```python
@pytest.fixture(autouse=True)
def _reset_registry():
    from ums_smart_revenue.connectors.google import secret_resolver as sr
    snapshot = dict(sr._REGISTRY)
    yield
    sr._REGISTRY.clear()
    sr._REGISTRY.update(snapshot)
```
Re-run the test; expected `7 passed`.

- [ ] **Step 5: Lint + commit**

```bash
python -m ruff check backend/ums_smart_revenue/connectors/google/secret_resolver.py \
                     tests/connectors/google/test_secret_resolver.py
git add backend/ums_smart_revenue/connectors/google/secret_resolver.py \
        tests/connectors/google/test_secret_resolver.py
git commit -m "$(cat <<'EOF'
feat(b2.1): add secret resolver dispatch

resolve_secret(ref) parses the URI scheme and routes to a registered
SecretResolver. Implemented schemes (registered at boot time by B2.1's
two backends): gcp-secret-manager:// and local-secret://. Other
ORM-accepted prefixes (aws-secretsmanager://, vault://, kms://, etc.)
intentionally raise UnsupportedSecretSchemeError until a future
credential-lifecycle PR registers them.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: GCP Secret Manager resolver

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/gcp_secret_manager.py`
- Create: `tests/connectors/google/test_gcp_secret_manager.py`

Dependency: `google-cloud-secret-manager` (already in `pyproject.toml`? — if not, this task adds it to `[project.dependencies]`).

- [ ] **Step 1: Check dependency**

```bash
python -c "import google.cloud.secretmanager_v1; print('ok')"
```
If `ModuleNotFoundError`, add `google-cloud-secret-manager>=2.20.0` to `pyproject.toml` under `[project.dependencies]`, then `python -m pip install -e .` and retry. Commit the `pyproject.toml` change in its own commit if added: `chore(b2.1): add google-cloud-secret-manager dependency`.

- [ ] **Step 2: Write the failing test**

`tests/connectors/google/test_gcp_secret_manager.py`:
```python
"""GCP Secret Manager resolver tests.

The resolver parses gcp-secret-manager://projects/{p}/secrets/{n}/versions/{v}
into a fully-qualified name, calls SecretManagerServiceClient.access_secret_version,
and returns the decoded payload. NotFound -> SecretNotFoundError; any other
google-cloud error -> SecretFetchError.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from google.api_core import exceptions as gcp_exceptions

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    SecretFetchError,
    SecretNotFoundError,
)
from ums_smart_revenue.connectors.google.gcp_secret_manager import (
    GcpSecretManagerResolver,
)


def _make_client_returning(payload: bytes) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.payload.data = payload
    client.access_secret_version.return_value = response
    return client


def test_resolve_returns_decoded_payload() -> None:
    client = _make_client_returning(b'{"refresh_token": "rt"}')
    resolver = GcpSecretManagerResolver(client=client)
    out = resolver.resolve(
        "gcp-secret-manager://projects/my-proj/secrets/yt-creds/versions/latest"
    )
    assert out == '{"refresh_token": "rt"}'
    client.access_secret_version.assert_called_once_with(
        request={"name": "projects/my-proj/secrets/yt-creds/versions/latest"}
    )


def test_resolve_raises_not_found_on_gcp_404() -> None:
    client = MagicMock()
    client.access_secret_version.side_effect = gcp_exceptions.NotFound("missing")
    resolver = GcpSecretManagerResolver(client=client)
    with pytest.raises(SecretNotFoundError):
        resolver.resolve(
            "gcp-secret-manager://projects/x/secrets/y/versions/1"
        )


def test_resolve_wraps_other_gcp_errors_as_fetch_error() -> None:
    client = MagicMock()
    inner = gcp_exceptions.PermissionDenied("denied")
    client.access_secret_version.side_effect = inner
    resolver = GcpSecretManagerResolver(client=client)
    with pytest.raises(SecretFetchError) as ctx:
        resolver.resolve(
            "gcp-secret-manager://projects/x/secrets/y/versions/latest"
        )
    assert ctx.value.inner is inner


@pytest.mark.parametrize(
    "ref",
    [
        "gcp-secret-manager://x",  # not projects/.../secrets/.../versions/...
        "gcp-secret-manager://projects/p/secrets/n",  # missing /versions/
        "gcp-secret-manager://projects//secrets/n/versions/1",  # empty project
    ],
)
def test_resolve_raises_malformed_uri(ref: str) -> None:
    resolver = GcpSecretManagerResolver(client=MagicMock())
    with pytest.raises(MalformedSecretUriError):
        resolver.resolve(ref)
```

- [ ] **Step 3: Run test to verify it fails**

```bash
python -m pytest -q tests/connectors/google/test_gcp_secret_manager.py
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 4: Implement**

`backend/ums_smart_revenue/connectors/google/gcp_secret_manager.py`:
```python
"""GCP Secret Manager resolver.

URI shape:
    gcp-secret-manager://projects/{project}/secrets/{name}/versions/{version}
where {version} is either an integer or 'latest'. The path after :// is the
exact name expected by SecretManagerServiceClient.access_secret_version.
"""
from __future__ import annotations

import re
from typing import Protocol

from google.api_core import exceptions as gcp_exceptions

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    SecretFetchError,
    SecretNotFoundError,
)

_NAME_PATTERN = re.compile(
    r"^projects/[^/]+/secrets/[^/]+/versions/[^/]+$"
)


class _SecretManagerClient(Protocol):
    def access_secret_version(self, *, request: dict) -> object: ...


class GcpSecretManagerResolver:
    """Resolver for the gcp-secret-manager:// scheme.

    Inject the client at construction time (B2.1 wiring uses a real
    SecretManagerServiceClient; tests use a mock).
    """

    def __init__(self, *, client: _SecretManagerClient) -> None:
        self._client = client

    def resolve(self, ref: str) -> str:
        if not ref.startswith("gcp-secret-manager://"):
            raise MalformedSecretUriError(ref=ref)
        name = ref[len("gcp-secret-manager://") :]
        if not _NAME_PATTERN.match(name):
            raise MalformedSecretUriError(ref=ref)
        try:
            response = self._client.access_secret_version(request={"name": name})
        except gcp_exceptions.NotFound as exc:
            raise SecretNotFoundError(ref=ref) from exc
        except gcp_exceptions.GoogleAPICallError as exc:
            raise SecretFetchError(ref=ref, inner=exc) from exc
        payload: bytes = response.payload.data
        return payload.decode("utf-8")
```

- [ ] **Step 5: Run test to verify it passes**

```bash
python -m pytest -q tests/connectors/google/test_gcp_secret_manager.py
```
Expected: `6 passed`.

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/connectors/google/gcp_secret_manager.py \
        tests/connectors/google/test_gcp_secret_manager.py
git commit -m "$(cat <<'EOF'
feat(b2.1): add GCP Secret Manager resolver

Resolves gcp-secret-manager://projects/{p}/secrets/{n}/versions/{v} via the
google-cloud-secret-manager client. NotFound -> SecretNotFoundError; other
GoogleAPICallError subclasses -> SecretFetchError(inner=...). Malformed URIs
fail closed via MalformedSecretUriError before any backend call.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: local-secret:// resolver (test backend)

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/local_secret_resolver.py`
- Create: `tests/connectors/google/test_local_secret_resolver.py`

- [ ] **Step 1: Write the failing test**

`tests/connectors/google/test_local_secret_resolver.py`:
```python
"""local-secret:// resolver - test/dev backend backed by an injected mapping."""
from __future__ import annotations

import pytest

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    SecretNotFoundError,
)
from ums_smart_revenue.connectors.google.local_secret_resolver import (
    LocalSecretResolver,
)


def test_resolve_returns_payload_from_mapping() -> None:
    resolver = LocalSecretResolver(mapping={"yt-creds": '{"refresh_token": "rt"}'})
    out = resolver.resolve("local-secret://yt-creds")
    assert out == '{"refresh_token": "rt"}'


def test_resolve_raises_not_found_for_unknown_key() -> None:
    resolver = LocalSecretResolver(mapping={})
    with pytest.raises(SecretNotFoundError):
        resolver.resolve("local-secret://missing")


@pytest.mark.parametrize(
    "ref",
    [
        "local-secret://",          # empty key
        "local-secret:/yt-creds",   # missing one /
        "not-local://yt-creds",     # wrong scheme
    ],
)
def test_resolve_raises_malformed_uri(ref: str) -> None:
    resolver = LocalSecretResolver(mapping={"yt-creds": "x"})
    with pytest.raises(MalformedSecretUriError):
        resolver.resolve(ref)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest -q tests/connectors/google/test_local_secret_resolver.py
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`backend/ums_smart_revenue/connectors/google/local_secret_resolver.py`:
```python
"""Test/dev secret resolver backed by an injected mapping.

URI shape: local-secret://{name} where {name} is a key in the mapping.
Never registered in production; production registers only gcp-secret-manager://.
"""
from __future__ import annotations

from collections.abc import Mapping

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    SecretNotFoundError,
)


class LocalSecretResolver:
    def __init__(self, *, mapping: Mapping[str, str]) -> None:
        self._mapping = dict(mapping)

    def resolve(self, ref: str) -> str:
        if not ref.startswith("local-secret://"):
            raise MalformedSecretUriError(ref=ref)
        key = ref[len("local-secret://") :]
        if not key:
            raise MalformedSecretUriError(ref=ref)
        try:
            return self._mapping[key]
        except KeyError as exc:
            raise SecretNotFoundError(ref=ref) from exc
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest -q tests/connectors/google/test_local_secret_resolver.py
```
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/connectors/google/local_secret_resolver.py \
        tests/connectors/google/test_local_secret_resolver.py
git commit -m "$(cat <<'EOF'
feat(b2.1): add local-secret:// resolver

Test/dev backend backed by an injected dict mapping {name -> payload}.
Unknown name -> SecretNotFoundError; bad URI shape -> MalformedSecretUriError.
Never registered in production wiring.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Google OAuth refresh wrapper

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/oauth.py`
- Create: `tests/connectors/google/test_oauth.py`

- [ ] **Step 1: Write the failing test**

`tests/connectors/google/test_oauth.py`:
```python
"""google-auth refresh wrapper tests.

build_credentials_from_payload(payload_json) parses the resolved secret string
and constructs google.oauth2.credentials.Credentials; missing fields or bad
JSON -> MalformedSecretPayloadError. refresh_credentials(creds) calls
creds.refresh(Request()); RefreshError -> OAuthRefreshError.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretPayloadError,
    OAuthRefreshError,
)
from ums_smart_revenue.connectors.google.oauth import (
    build_credentials_from_payload,
    refresh_credentials,
)

_VALID_PAYLOAD = json.dumps(
    {
        "refresh_token": "rt-abc",
        "client_id": "cid-abc",
        "client_secret": "secret-abc",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
)


def test_build_credentials_returns_credentials_with_fields() -> None:
    creds = build_credentials_from_payload(_VALID_PAYLOAD)
    assert creds.refresh_token == "rt-abc"
    assert creds.client_id == "cid-abc"
    assert creds.client_secret == "secret-abc"
    assert creds.token_uri == "https://oauth2.googleapis.com/token"


@pytest.mark.parametrize("bad_json", ["", "not-json", "{", "[]"])
def test_build_credentials_rejects_non_object_json(bad_json: str) -> None:
    with pytest.raises(MalformedSecretPayloadError):
        build_credentials_from_payload(bad_json)


@pytest.mark.parametrize(
    "missing_field",
    ["refresh_token", "client_id", "client_secret", "token_uri"],
)
def test_build_credentials_rejects_missing_field(missing_field: str) -> None:
    payload = json.loads(_VALID_PAYLOAD)
    payload.pop(missing_field)
    with pytest.raises(MalformedSecretPayloadError) as ctx:
        build_credentials_from_payload(json.dumps(payload))
    assert missing_field in ctx.value.detail


def test_refresh_credentials_calls_refresh() -> None:
    creds = MagicMock()
    with patch("ums_smart_revenue.connectors.google.oauth.Request") as request_cls:
        refresh_credentials(creds)
    creds.refresh.assert_called_once()
    request_cls.assert_called_once()


def test_refresh_credentials_wraps_refresh_error() -> None:
    creds = MagicMock()
    inner = RefreshError("token revoked")
    creds.refresh.side_effect = inner
    with patch("ums_smart_revenue.connectors.google.oauth.Request"):
        with pytest.raises(OAuthRefreshError) as ctx:
            refresh_credentials(creds)
    assert ctx.value.inner is inner
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest -q tests/connectors/google/test_oauth.py
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`backend/ums_smart_revenue/connectors/google/oauth.py`:
```python
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


def refresh_credentials(credentials: Credentials) -> None:
    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        raise OAuthRefreshError(inner=exc) from exc
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest -q tests/connectors/google/test_oauth.py
```
Expected: `10 passed` (1 valid + 4 bad json + 4 missing field + 1 refresh + 1 refresh error).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/connectors/google/oauth.py \
        tests/connectors/google/test_oauth.py
git commit -m "$(cat <<'EOF'
feat(b2.1): add Google OAuth refresh wrapper

build_credentials_from_payload(json_str) parses a resolved secret payload
into google.oauth2.credentials.Credentials. Missing refresh_token /
client_id / client_secret / token_uri or bad JSON ->
MalformedSecretPayloadError with the offending field(s) in detail.

refresh_credentials(creds) wraps creds.refresh(Request()); RefreshError ->
OAuthRefreshError(inner=...). B2.1 path uses this at the initial
credential build (bucket A); B2.4's mid-run path reuses the same wrapper.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: B2.1 validation gate + plan/backlog markers

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md` (mark B2.1 ⏳)
- Modify: `Docs/15_DELIVERY_BACKLOG.md` (mark B2.1 ⏳)

- [ ] **Step 1: Run B2.1 validation gate**

```bash
python -m ruff check backend tests
python -m pytest -q tests/connectors/google/test_errors.py \
                    tests/connectors/google/test_secret_resolver.py \
                    tests/connectors/google/test_gcp_secret_manager.py \
                    tests/connectors/google/test_local_secret_resolver.py \
                    tests/connectors/google/test_oauth.py
python scripts/run_validation_gate.py
git diff --check
```
Expected: every command exits 0.

- [ ] **Step 2: Update planning docs**

Open `Docs/01_IMPLEMENTATION_PLAN.md`. Locate the "Sx — Specced but not yet started" section that lists B2. Add a per-PR breakdown line for B2.1 with ⏳, e.g.:
```markdown
- ⏳ PR #N (B2.1) — Google connector credential foundation (secret resolver dispatch +
  gcp-secret-manager:// + local-secret:// + Google OAuth refresh wrapper).
```
(Replace `#N` with the next PR number — check `gh pr list` if uncertain.)

Open `Docs/15_DELIVERY_BACKLOG.md`. Find the same B2 entry and append the same B2.1 ⏳ line.

- [ ] **Step 3: Commit the doc updates**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "$(cat <<'EOF'
docs(b2.1): mark credential foundation as in-progress

Adds ⏳ PR markers for B2.1 (secret resolver dispatch + GCP / local
backends + Google OAuth refresh wrapper) in Docs/01_IMPLEMENTATION_PLAN.md
and Docs/15_DELIVERY_BACKLOG.md.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## PR B2.2 — Blob storage + raw_file lifecycle helpers

Spec reference: §5.2 (public surface), §6.5 (BlobUploadError, BlobChecksumMismatchError, RawFileLifecycleError, RawFileAlreadyParsedError), §9.3 B2.2.

### Task 7: Blob storage Protocol + file-store backend

**Files:**
- Create: `backend/ums_smart_revenue/connectors/runs/__init__.py` (empty)
- Create: `backend/ums_smart_revenue/connectors/runs/blob_storage.py`
- Create: `tests/connectors/google/test_blob_storage.py`

The `runs/` package houses blob storage + raw_file helpers + the connector runs repository + the orchestrator (sliced across B2.2/B2.3/B2.4). Tests for blob storage live under `tests/connectors/google/` because that's where all per-module unit tests for B2 live (one tree, one conftest).

- [ ] **Step 1: Add error classes to errors.py**

Edit `backend/ums_smart_revenue/connectors/google/errors.py` and append:
```python
class BlobUploadError(GoogleConnectorError):
    def __init__(self, *, storage_uri: str, inner: Exception) -> None:
        super().__init__(
            f"blob upload failed for {storage_uri}: {type(inner).__name__}"
        )
        self.storage_uri = storage_uri
        self.inner = inner


class BlobChecksumMismatchError(GoogleConnectorError):
    def __init__(self, *, storage_uri: str, computed: str, read: str) -> None:
        super().__init__(
            f"checksum mismatch at {storage_uri}: computed={computed} read={read}"
        )
        self.storage_uri = storage_uri
        self.computed = computed
        self.read = read
```

Add the new classes to `tests/connectors/google/test_errors.py`'s `test_all_b21_errors_subclass_google_connector_error` test (rename it or add a new test for B2.2 family). Run `python -m pytest -q tests/connectors/google/test_errors.py` to confirm green.

- [ ] **Step 2: Write the failing blob_storage test**

`tests/connectors/google/test_blob_storage.py`:
```python
"""Blob storage backend tests.

Two backends implement BlobStorageBackend:
- LocalFileStoreBackend (file-store://) - test/dev, writes to a tmp dir.
- GcsBlobStorageBackend (gs://) - production, uses google-cloud-storage.

Both must round-trip bytes deterministically; tests assert that get_bytes
after upload returns the same payload.
"""
from __future__ import annotations

import pytest

from ums_smart_revenue.connectors.runs.blob_storage import (
    BlobStorageBackend,
    LocalFileStoreBackend,
)


def test_file_store_round_trips_bytes(tmp_path) -> None:
    backend: BlobStorageBackend = LocalFileStoreBackend(root=tmp_path)
    uri = "file-store://bucket/tenant/yt/2026-05/abc.csv"
    payload = b"a,b,c\n1,2,3\n"
    backend.upload(storage_uri=uri, content=payload)
    assert backend.get_bytes(storage_uri=uri) == payload


def test_file_store_rejects_non_file_store_scheme(tmp_path) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    with pytest.raises(ValueError, match="file-store://"):
        backend.upload(storage_uri="gs://bucket/key", content=b"x")


def test_file_store_creates_parent_dirs(tmp_path) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    uri = "file-store://bucket/deep/nested/path/key.csv"
    backend.upload(storage_uri=uri, content=b"x")
    assert backend.get_bytes(storage_uri=uri) == b"x"


def test_file_store_get_bytes_missing_raises_file_not_found(tmp_path) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    with pytest.raises(FileNotFoundError):
        backend.get_bytes(storage_uri="file-store://bucket/missing.csv")
```

- [ ] **Step 3: Run to verify failure**

```bash
python -m pytest -q tests/connectors/google/test_blob_storage.py
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 4: Implement**

`backend/ums_smart_revenue/connectors/runs/blob_storage.py`:
```python
"""Blob storage backends for B2.

Two backends implement BlobStorageBackend (Protocol):
- LocalFileStoreBackend: file-store://{rest} -> {root}/{rest} on disk.
- GcsBlobStorageBackend: gs://{bucket}/{key} -> google-cloud-storage upload/download.

The orchestrator selects the backend by URI scheme; mixed-scheme runs are
not supported in a single orchestrator invocation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class BlobStorageBackend(Protocol):
    def upload(self, *, storage_uri: str, content: bytes) -> None: ...
    def get_bytes(self, *, storage_uri: str) -> bytes: ...


_FILE_STORE_PREFIX = "file-store://"


class LocalFileStoreBackend:
    def __init__(self, *, root: Path) -> None:
        self._root = Path(root)

    def _path_for(self, storage_uri: str) -> Path:
        if not storage_uri.startswith(_FILE_STORE_PREFIX):
            raise ValueError(
                f"LocalFileStoreBackend only handles {_FILE_STORE_PREFIX} URIs, got {storage_uri!r}"
            )
        rel = storage_uri[len(_FILE_STORE_PREFIX) :]
        # Guard against path traversal: any '..' segment is rejected.
        if any(part == ".." for part in rel.split("/")):
            raise ValueError(f"path traversal blocked in {storage_uri!r}")
        return self._root.joinpath(*rel.split("/"))

    def upload(self, *, storage_uri: str, content: bytes) -> None:
        path = self._path_for(storage_uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get_bytes(self, *, storage_uri: str) -> bytes:
        return self._path_for(storage_uri).read_bytes()
```

- [ ] **Step 5: Run test, lint, commit**

```bash
python -m pytest -q tests/connectors/google/test_blob_storage.py
python -m ruff check backend/ums_smart_revenue/connectors/runs/blob_storage.py \
                     tests/connectors/google/test_blob_storage.py \
                     backend/ums_smart_revenue/connectors/google/errors.py
```
Expected: `4 passed`, ruff clean.

```bash
git add backend/ums_smart_revenue/connectors/runs/__init__.py \
        backend/ums_smart_revenue/connectors/runs/blob_storage.py \
        backend/ums_smart_revenue/connectors/google/errors.py \
        tests/connectors/google/test_blob_storage.py \
        tests/connectors/google/test_errors.py
git commit -m "$(cat <<'EOF'
feat(b2.2): add blob storage Protocol + file-store backend

BlobStorageBackend Protocol covers two implementations:
- LocalFileStoreBackend (file-store://) for tests/dev with path-traversal
  rejection.
- GcsBlobStorageBackend (gs://) in the next commit.

Also extends GoogleConnectorError hierarchy with BlobUploadError and
BlobChecksumMismatchError (used by the upload_and_verify helper).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: GCS blob storage backend

**Files:**
- Modify: `backend/ums_smart_revenue/connectors/runs/blob_storage.py` (append class)
- Modify: `tests/connectors/google/test_blob_storage.py` (append tests)

Dependency: `google-cloud-storage`. Check via `python -c "import google.cloud.storage"`; if missing, add to `pyproject.toml` `[project.dependencies]` and `pip install -e .`. Commit dep bump separately as `chore(b2.2): add google-cloud-storage dependency`.

- [ ] **Step 1: Write failing tests**

Append to `tests/connectors/google/test_blob_storage.py`:
```python
from unittest.mock import MagicMock
from google.api_core import exceptions as gcp_exceptions

from ums_smart_revenue.connectors.google.errors import BlobUploadError
from ums_smart_revenue.connectors.runs.blob_storage import GcsBlobStorageBackend


def test_gcs_upload_parses_uri_and_calls_blob_upload() -> None:
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_client.bucket.return_value = fake_bucket
    fake_bucket.blob.return_value = fake_blob

    backend = GcsBlobStorageBackend(client=fake_client)
    backend.upload(storage_uri="gs://my-bucket/tenant/yt/key.csv", content=b"x")

    fake_client.bucket.assert_called_once_with("my-bucket")
    fake_bucket.blob.assert_called_once_with("tenant/yt/key.csv")
    fake_blob.upload_from_string.assert_called_once_with(b"x")


def test_gcs_upload_wraps_api_error_as_blob_upload_error() -> None:
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_client.bucket.return_value = fake_bucket
    fake_bucket.blob.return_value = fake_blob
    fake_blob.upload_from_string.side_effect = gcp_exceptions.GoogleAPICallError("fail")

    backend = GcsBlobStorageBackend(client=fake_client)
    with pytest.raises(BlobUploadError) as ctx:
        backend.upload(storage_uri="gs://my-bucket/key", content=b"x")
    assert ctx.value.storage_uri == "gs://my-bucket/key"


def test_gcs_get_bytes_downloads_via_blob() -> None:
    fake_client = MagicMock()
    fake_bucket = MagicMock()
    fake_blob = MagicMock()
    fake_blob.download_as_bytes.return_value = b"downloaded"
    fake_client.bucket.return_value = fake_bucket
    fake_bucket.blob.return_value = fake_blob

    backend = GcsBlobStorageBackend(client=fake_client)
    assert backend.get_bytes(storage_uri="gs://b/k") == b"downloaded"


def test_gcs_rejects_non_gs_scheme() -> None:
    backend = GcsBlobStorageBackend(client=MagicMock())
    with pytest.raises(ValueError, match="gs://"):
        backend.upload(storage_uri="file-store://x", content=b"x")
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest -q tests/connectors/google/test_blob_storage.py
```
Expected: ImportError for `GcsBlobStorageBackend`.

- [ ] **Step 3: Implement**

Append to `backend/ums_smart_revenue/connectors/runs/blob_storage.py`:
```python
from google.api_core import exceptions as gcp_exceptions
from google.cloud.storage import Client as GcsClient  # type: ignore[import-untyped]

from ums_smart_revenue.connectors.google.errors import BlobUploadError

_GCS_PREFIX = "gs://"


class GcsBlobStorageBackend:
    def __init__(self, *, client: GcsClient) -> None:
        self._client = client

    def _parse_uri(self, storage_uri: str) -> tuple[str, str]:
        if not storage_uri.startswith(_GCS_PREFIX):
            raise ValueError(
                f"GcsBlobStorageBackend only handles {_GCS_PREFIX} URIs, got {storage_uri!r}"
            )
        rest = storage_uri[len(_GCS_PREFIX) :]
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            raise ValueError(f"malformed gs:// URI: {storage_uri!r}")
        return bucket, key

    def upload(self, *, storage_uri: str, content: bytes) -> None:
        bucket, key = self._parse_uri(storage_uri)
        try:
            blob = self._client.bucket(bucket).blob(key)
            blob.upload_from_string(content)
        except gcp_exceptions.GoogleAPICallError as exc:
            raise BlobUploadError(storage_uri=storage_uri, inner=exc) from exc

    def get_bytes(self, *, storage_uri: str) -> bytes:
        bucket, key = self._parse_uri(storage_uri)
        return self._client.bucket(bucket).blob(key).download_as_bytes()
```

- [ ] **Step 4: Run, lint, commit**

```bash
python -m pytest -q tests/connectors/google/test_blob_storage.py
python -m ruff check backend/ums_smart_revenue/connectors/runs/blob_storage.py
```
Expected: `8 passed`, ruff clean.

```bash
git add backend/ums_smart_revenue/connectors/runs/blob_storage.py \
        tests/connectors/google/test_blob_storage.py
git commit -m "$(cat <<'EOF'
feat(b2.2): add GCS blob storage backend

GcsBlobStorageBackend handles gs://{bucket}/{key} URIs via the
google-cloud-storage client. GoogleAPICallError on upload ->
BlobUploadError(storage_uri, inner). Malformed gs:// URIs -> ValueError.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Deterministic blob path + upload_and_verify

**Files:**
- Modify: `backend/ums_smart_revenue/connectors/runs/blob_storage.py` (append helpers)
- Modify: `tests/connectors/google/test_blob_storage.py` (append tests)

- [ ] **Step 1: Write failing tests**

Append to the test file:
```python
import hashlib
from uuid import UUID

from ums_smart_revenue.connectors.google.errors import BlobChecksumMismatchError
from ums_smart_revenue.connectors.runs.blob_storage import (
    compute_checksum,
    deterministic_blob_path,
    upload_and_verify,
)


def test_deterministic_blob_path_format() -> None:
    path = deterministic_blob_path(
        bucket="my-bucket",
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        connector_key="youtube-reporting",
        report_type="channel_basic_a2",
        month="2026-05",
        checksum="abc123",
        ext="csv",
    )
    assert path == (
        "gs://my-bucket/00000000-0000-0000-0000-000000000001/"
        "youtube-reporting/channel_basic_a2/2026-05/abc123.csv"
    )


def test_compute_checksum_returns_hex_sha256() -> None:
    expected = hashlib.sha256(b"hello").hexdigest()
    assert compute_checksum(b"hello") == expected


def test_upload_and_verify_round_trips(tmp_path) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    uri = "file-store://bucket/tenant/yt/m/abc.csv"
    checksum = upload_and_verify(
        backend=backend, storage_uri=uri, content=b"payload"
    )
    assert checksum == hashlib.sha256(b"payload").hexdigest()


def test_upload_and_verify_raises_on_checksum_mismatch(tmp_path, monkeypatch) -> None:
    backend = LocalFileStoreBackend(root=tmp_path)
    uri = "file-store://bucket/key"

    def fake_get_bytes(*, storage_uri: str) -> bytes:
        return b"different"

    monkeypatch.setattr(backend, "get_bytes", fake_get_bytes)
    with pytest.raises(BlobChecksumMismatchError):
        upload_and_verify(backend=backend, storage_uri=uri, content=b"original")
```

- [ ] **Step 2: Verify failure**

```bash
python -m pytest -q tests/connectors/google/test_blob_storage.py -k "deterministic or compute_checksum or upload_and_verify"
```
Expected: ImportError.

- [ ] **Step 3: Implement**

Append to `backend/ums_smart_revenue/connectors/runs/blob_storage.py`:
```python
import hashlib
from uuid import UUID

from ums_smart_revenue.connectors.google.errors import BlobChecksumMismatchError


def deterministic_blob_path(
    *,
    bucket: str,
    tenant_id: UUID,
    connector_key: str,
    report_type: str,
    month: str,
    checksum: str,
    ext: str,
) -> str:
    """Build the deterministic gs:// URI for a raw report blob.

    Path shape: gs://{bucket}/{tenant_id}/{connector_key}/{report_type}/{month}/{checksum}.{ext}
    Note: account_id is intentionally NOT in the path - run context lives on
    connector_runs. Same bytes always map to the same path, so idempotent
    re-uploads on retry overwrite or hit the existing object.
    """
    return (
        f"gs://{bucket}/{tenant_id}/{connector_key}/{report_type}/"
        f"{month}/{checksum}.{ext}"
    )


def compute_checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def upload_and_verify(
    *,
    backend: BlobStorageBackend,
    storage_uri: str,
    content: bytes,
) -> str:
    """Upload, re-read, verify SHA-256, return the computed checksum.

    Raises BlobUploadError if backend.upload fails (passed through).
    Raises BlobChecksumMismatchError if re-read bytes hash differently
    (e.g., backend silently truncated).
    """
    computed = compute_checksum(content)
    backend.upload(storage_uri=storage_uri, content=content)
    read_back = backend.get_bytes(storage_uri=storage_uri)
    read_back_hash = compute_checksum(read_back)
    if read_back_hash != computed:
        raise BlobChecksumMismatchError(
            storage_uri=storage_uri, computed=computed, read=read_back_hash
        )
    return computed
```

- [ ] **Step 4: Run, lint, commit**

```bash
python -m pytest -q tests/connectors/google/test_blob_storage.py
python -m ruff check backend/ums_smart_revenue/connectors/runs/blob_storage.py
git add backend/ums_smart_revenue/connectors/runs/blob_storage.py \
        tests/connectors/google/test_blob_storage.py
git commit -m "$(cat <<'EOF'
feat(b2.2): add deterministic blob path + checksum upload

- deterministic_blob_path(...) returns gs://{bucket}/{tenant}/{connector}/
  {report_type}/{month}/{checksum}.{ext}; account_id is intentionally NOT
  in the path (run context lives on connector_runs).
- compute_checksum(bytes) -> SHA-256 hex.
- upload_and_verify(backend, uri, content) uploads, re-reads, verifies,
  returns the computed checksum. Mismatch -> BlobChecksumMismatchError.
  Backend upload error bubbles as BlobUploadError.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: mark_parsed helper (DOWNLOADED|FAILED -> PARSED)

**Files:**
- Modify: `backend/ums_smart_revenue/connectors/google/errors.py` (append two error classes)
- Create: `backend/ums_smart_revenue/connectors/runs/raw_file_helpers.py`
- Create: `tests/connectors/google/test_raw_file_helpers.py`

Spec §5.2: `mark_parsed` accepts `DOWNLOADED -> PARSED` (success) and `FAILED -> PARSED` (retry recovery). Refuses QUARANTINED. `RawFileAlreadyParsedError` on `PARSED -> PARSED`. `RawFileLifecycleError` on other illegal transitions.

- [ ] **Step 1: Add error classes**

Append to `backend/ums_smart_revenue/connectors/google/errors.py`:
```python
class RawFileLifecycleError(GoogleConnectorError):
    def __init__(self, *, raw_file_id: str, current: str, target: str) -> None:
        super().__init__(
            f"raw_file {raw_file_id}: {current} -> {target} not permitted"
        )
        self.raw_file_id = raw_file_id
        self.current = current
        self.target = target


class RawFileAlreadyParsedError(GoogleConnectorError):
    def __init__(self, *, raw_file_id: str) -> None:
        super().__init__(f"raw_file {raw_file_id} already parsed")
        self.raw_file_id = raw_file_id
```

- [ ] **Step 2: Write failing test**

`tests/connectors/google/test_raw_file_helpers.py`:
```python
"""Raw file lifecycle helper tests.

mark_parsed: DOWNLOADED|FAILED -> PARSED. Refuses QUARANTINED.
RawFileAlreadyParsedError on PARSED -> PARSED. RawFileLifecycleError otherwise.

mark_failed: DOWNLOADED|FAILED -> FAILED (idempotent on FAILED, overwrites
error_class/error_summary). Refuses QUARANTINED and PARSED.
error_summary truncated to 500 chars.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import (
    RawFileAlreadyParsedError,
    RawFileLifecycleError,
)
from ums_smart_revenue.connectors.runs.raw_file_helpers import mark_parsed
from ums_smart_revenue.db.bases import ReportBase
from ums_smart_revenue.db.report_models import RawReportFileORM


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    ReportBase.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _insert_raw_file(
    session: Session, *, tenant_id: UUID, parse_status: str
) -> UUID:
    row = RawReportFileORM(
        tenant_id=tenant_id,
        source="youtube_reporting",
        report_type="channel_basic_a2",
        report_month="2026-05",
        file_url="file-store://x/y.csv",
        checksum="abc",
        parse_status=parse_status,
    )
    session.add(row)
    session.flush()
    return row.id


def test_mark_parsed_downloaded_to_parsed(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="DOWNLOADED")
    mark_parsed(session, raw_file_id=rid, tenant_id=tenant_id)
    session.flush()
    row = session.get(RawReportFileORM, rid)
    assert row.parse_status == "PARSED"


def test_mark_parsed_failed_to_parsed_retry_recovery(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="FAILED")
    mark_parsed(session, raw_file_id=rid, tenant_id=tenant_id)
    row = session.get(RawReportFileORM, rid)
    assert row.parse_status == "PARSED"


def test_mark_parsed_already_parsed_raises(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="PARSED")
    with pytest.raises(RawFileAlreadyParsedError):
        mark_parsed(session, raw_file_id=rid, tenant_id=tenant_id)


def test_mark_parsed_quarantined_refused(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="QUARANTINED")
    with pytest.raises(RawFileLifecycleError) as ctx:
        mark_parsed(session, raw_file_id=rid, tenant_id=tenant_id)
    assert ctx.value.current == "QUARANTINED"
    assert ctx.value.target == "PARSED"


def test_mark_parsed_unknown_row_raises_lifecycle(session) -> None:
    tenant_id = uuid4()
    with pytest.raises(RawFileLifecycleError):
        mark_parsed(session, raw_file_id=uuid4(), tenant_id=tenant_id)


def test_mark_parsed_cross_tenant_refused(session) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_a, parse_status="DOWNLOADED")
    with pytest.raises(RawFileLifecycleError):
        mark_parsed(session, raw_file_id=rid, tenant_id=tenant_b)
```

- [ ] **Step 3: Verify failure**

```bash
python -m pytest -q tests/connectors/google/test_raw_file_helpers.py
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 4: Implement**

`backend/ums_smart_revenue/connectors/runs/raw_file_helpers.py`:
```python
"""raw_report_files lifecycle helpers used by the B2.4 orchestrator.

Allowed transitions (spec §5.2):
- DOWNLOADED -> PARSED  via mark_parsed (success)
- FAILED     -> PARSED  via mark_parsed (retry recovery)
- DOWNLOADED -> FAILED  via mark_failed
- FAILED     -> FAILED  via mark_failed (idempotent: overwrites error fields)

Refused (raise):
- QUARANTINED -> anything (terminal; externally-set)
- PARSED -> PARSED via mark_parsed (RawFileAlreadyParsedError)
- PARSED -> FAILED, DOWNLOADED -> DOWNLOADED, any other (RawFileLifecycleError)

Tenant scope is enforced: a (raw_file_id, tenant_id) mismatch is a
RawFileLifecycleError, not a silent no-op.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import (
    RawFileAlreadyParsedError,
    RawFileLifecycleError,
)
from ums_smart_revenue.db.report_models import RawReportFileORM

_ERROR_SUMMARY_MAX = 500


def _load_or_raise(
    session: Session, *, raw_file_id: UUID, tenant_id: UUID, target: str
) -> RawReportFileORM:
    stmt = select(RawReportFileORM).where(
        RawReportFileORM.id == raw_file_id,
        RawReportFileORM.tenant_id == tenant_id,
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        raise RawFileLifecycleError(
            raw_file_id=str(raw_file_id), current="<missing>", target=target
        )
    return row


def mark_parsed(
    session: Session, *, raw_file_id: UUID, tenant_id: UUID
) -> None:
    row = _load_or_raise(
        session, raw_file_id=raw_file_id, tenant_id=tenant_id, target="PARSED"
    )
    if row.parse_status == "PARSED":
        raise RawFileAlreadyParsedError(raw_file_id=str(raw_file_id))
    if row.parse_status in ("DOWNLOADED", "FAILED"):
        row.parse_status = "PARSED"
        return
    raise RawFileLifecycleError(
        raw_file_id=str(raw_file_id),
        current=row.parse_status,
        target="PARSED",
    )
```

- [ ] **Step 5: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_raw_file_helpers.py
python -m ruff check backend/ums_smart_revenue/connectors/runs/raw_file_helpers.py \
                     tests/connectors/google/test_raw_file_helpers.py \
                     backend/ums_smart_revenue/connectors/google/errors.py
```
Expected: `6 passed`, ruff clean.

```bash
git add backend/ums_smart_revenue/connectors/runs/raw_file_helpers.py \
        backend/ums_smart_revenue/connectors/google/errors.py \
        tests/connectors/google/test_raw_file_helpers.py
git commit -m "$(cat <<'EOF'
feat(b2.2): add raw_file mark_parsed helper

mark_parsed(session, raw_file_id, tenant_id) handles:
- DOWNLOADED -> PARSED (success).
- FAILED -> PARSED (retry recovery: a previously-failed row was re-parsed).
- PARSED -> PARSED -> RawFileAlreadyParsedError (defensive; orchestrator
  should idempotency-skip first).
- QUARANTINED and unknown rows -> RawFileLifecycleError.

Tenant scope enforced: cross-tenant raw_file_id raises RawFileLifecycleError.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: mark_failed helper (DOWNLOADED|FAILED -> FAILED idempotent)

**Files:**
- Modify: `backend/ums_smart_revenue/connectors/runs/raw_file_helpers.py` (append `mark_failed`)
- Modify: `tests/connectors/google/test_raw_file_helpers.py` (append tests)

Note: `raw_report_files` table has **no** `error_class` or `error_summary` columns (per PR #32). Per spec §3 non-goal "No changes to RawReportFileORM... column shape" except the additive UNIQUE in B2.3 — so `error_class`/`error_summary` cannot be persisted on raw_report_files. They live on `connector_runs.error_summary` and in the per-raw-file audit event payload. `mark_failed` therefore **does not write error fields to raw_report_files**; it only transitions `parse_status`. The error details flow to:
- `connector_runs.error_summary` (truncated to 500) — set by `finish_run`.
- `REPORT_IMPORTED` audit event payload (`error_class` only — no error_summary text in audit).

- [ ] **Step 1: Append failing tests**

Append to `tests/connectors/google/test_raw_file_helpers.py`:
```python
from ums_smart_revenue.connectors.runs.raw_file_helpers import mark_failed


def test_mark_failed_downloaded_to_failed(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="DOWNLOADED")
    mark_failed(session, raw_file_id=rid, tenant_id=tenant_id)
    row = session.get(RawReportFileORM, rid)
    assert row.parse_status == "FAILED"


def test_mark_failed_failed_to_failed_idempotent(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="FAILED")
    mark_failed(session, raw_file_id=rid, tenant_id=tenant_id)
    row = session.get(RawReportFileORM, rid)
    assert row.parse_status == "FAILED"  # unchanged but no raise


def test_mark_failed_parsed_refused(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="PARSED")
    with pytest.raises(RawFileLifecycleError):
        mark_failed(session, raw_file_id=rid, tenant_id=tenant_id)


def test_mark_failed_quarantined_refused(session) -> None:
    tenant_id = uuid4()
    rid = _insert_raw_file(session, tenant_id=tenant_id, parse_status="QUARANTINED")
    with pytest.raises(RawFileLifecycleError):
        mark_failed(session, raw_file_id=rid, tenant_id=tenant_id)
```

- [ ] **Step 2: Implement**

Append to `backend/ums_smart_revenue/connectors/runs/raw_file_helpers.py`:
```python
def mark_failed(
    session: Session, *, raw_file_id: UUID, tenant_id: UUID
) -> None:
    """Transition DOWNLOADED|FAILED -> FAILED.

    raw_report_files does not store error_class/error_summary (per the
    existing PR #32 schema; spec §3 non-goal forbids adding them in B2).
    Error details flow to connector_runs.error_summary at finish_run time
    and to the REPORT_IMPORTED audit event payload (error_class only).
    """
    row = _load_or_raise(
        session, raw_file_id=raw_file_id, tenant_id=tenant_id, target="FAILED"
    )
    if row.parse_status in ("DOWNLOADED", "FAILED"):
        row.parse_status = "FAILED"
        return
    raise RawFileLifecycleError(
        raw_file_id=str(raw_file_id),
        current=row.parse_status,
        target="FAILED",
    )
```

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_raw_file_helpers.py
git add backend/ums_smart_revenue/connectors/runs/raw_file_helpers.py \
        tests/connectors/google/test_raw_file_helpers.py
git commit -m "$(cat <<'EOF'
feat(b2.2): add raw_file mark_failed helper

mark_failed handles DOWNLOADED -> FAILED and FAILED -> FAILED (idempotent;
the row stays FAILED without raising). Refuses PARSED and QUARANTINED.

Error class/summary are intentionally NOT stored on raw_report_files
(PR #32 schema has no such columns; spec §3 forbids adding them). Error
context lives on connector_runs.error_summary (set by finish_run) and in
the REPORT_IMPORTED audit event payload (error_class only).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: B2.2 validation gate + plan/backlog markers

- [ ] **Step 1: Run B2.2 validation gate**

```bash
python -m ruff check backend tests
python -m pytest -q tests/connectors/google/test_blob_storage.py \
                    tests/connectors/google/test_raw_file_helpers.py \
                    tests/reports/test_raw_files.py
python scripts/run_validation_gate.py
git diff --check
```
Expected: all green.

- [ ] **Step 2: Update planning docs**

Append to the B2 section of `Docs/01_IMPLEMENTATION_PLAN.md` and `Docs/15_DELIVERY_BACKLOG.md`:
```markdown
- ⏳ PR #N (B2.2) — Blob storage backends (GCS + file-store) + raw_file
  lifecycle helpers (mark_parsed accepts FAILED -> PARSED retry recovery;
  mark_failed FAILED -> FAILED idempotent).
```

- [ ] **Step 3: Commit**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "$(cat <<'EOF'
docs(b2.2): mark blob + raw_file lifecycle as in-progress

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## PR B2.3 — Connector runs ORM + repo + Alembic

Spec reference: §5.3 (ORM + repo public surface), §6.5 (no new error classes here — `RawFileLifecycleError` already covers misuse), §9.3 B2.3.

### Task 13: ConnectorRunORM + ConnectorRunRawFileORM models

**Files:**
- Create: `backend/ums_smart_revenue/db/connector_models.py`
- Modify: `backend/ums_smart_revenue/db/alembic/env.py` (add import so metadata is discovered)
- Create: `tests/connectors/runs/__init__.py` (empty)
- Create: `tests/connectors/runs/test_repository.py` (just the metadata smoke for now)

- [ ] **Step 1: Write failing test**

`tests/connectors/runs/test_repository.py`:
```python
"""ConnectorRunORM + ConnectorRunRawFileORM smoke and repo tests."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from ums_smart_revenue.db.bases import ReportBase
from ums_smart_revenue.db.connector_models import (
    ConnectorRunORM,
    ConnectorRunRawFileORM,
)


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    ReportBase.metadata.create_all(eng)
    return eng


def test_orm_tables_registered_on_report_base(engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "connector_runs" in tables
    assert "connector_run_raw_files" in tables


def test_connector_runs_has_unique_tenant_id_constraint(engine) -> None:
    insp = inspect(engine)
    uniques = insp.get_unique_constraints("connector_runs")
    cols = [tuple(u["column_names"]) for u in uniques]
    assert ("tenant_id", "id") in cols
```

- [ ] **Step 2: Verify failure**

```bash
python -m pytest -q tests/connectors/runs/test_repository.py
```
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement ORM**

`backend/ums_smart_revenue/db/connector_models.py`:
```python
"""SQLAlchemy models for B2.3 connector run tracking.

Both tables live on ReportBase.metadata (same metadata family as
raw_report_files) so the B2.3 Alembic migration can create them together.

Schema (spec §5.3):

connector_runs:
- id                    UUID PK
- tenant_id             UUID NOT NULL
- connector_key         text NOT NULL
- account_id            text NOT NULL
- report_month          text NOT NULL (YYYY-MM)
- triggered_by_user_id  UUID nullable; composite FK -> users (tenant_id, id)
- started_at            timestamptz NOT NULL default now()
- finished_at           timestamptz nullable
- status                text NOT NULL ('RUNNING'|'SUCCEEDED'|'PARTIAL'|'FAILED')
- counts_json           jsonb NOT NULL (fixed shape, see start_run)
- error_summary         text nullable (truncated to 500 chars at write)
- UNIQUE (tenant_id, id)        <- required so the join table can FK on it.

connector_run_raw_files:
- id                    UUID PK
- tenant_id             UUID NOT NULL
- connector_run_id      UUID NOT NULL; composite FK -> connector_runs (tenant_id, id)
- raw_report_file_id    UUID NOT NULL; composite FK -> raw_report_files (tenant_id, id)
- linked_at             timestamptz NOT NULL default now()
- ordering_index        int NOT NULL
- UNIQUE (tenant_id, connector_run_id, raw_report_file_id)
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from ums_smart_revenue.db.bases import ReportBase

_ALLOWED_STATUSES = ("RUNNING", "SUCCEEDED", "PARTIAL", "FAILED")


class ConnectorRunORM(ReportBase):
    __tablename__ = "connector_runs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    connector_key: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    report_month: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'RUNNING'")
    )
    counts_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_connector_runs_tenant_id"),
        CheckConstraint(
            f"status IN {_ALLOWED_STATUSES}",
            name="ck_connector_runs_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "triggered_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_connector_runs_triggered_by_user",
        ),
    )


class ConnectorRunRawFileORM(ReportBase):
    __tablename__ = "connector_run_raw_files"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    connector_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    raw_report_file_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ordering_index: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "connector_run_id",
            "raw_report_file_id",
            name="uq_connector_run_raw_files_run_file",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connector_run_id"],
            ["connector_runs.tenant_id", "connector_runs.id"],
            name="fk_connector_run_raw_files_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "raw_report_file_id"],
            ["raw_report_files.tenant_id", "raw_report_files.id"],
            name="fk_connector_run_raw_files_raw_file",
        ),
    )
```

- [ ] **Step 4: Register on env.py**

Edit `backend/ums_smart_revenue/db/alembic/env.py`. Find the existing block of `from ums_smart_revenue.db import ... # noqa: F401` imports. Add:
```python
from ums_smart_revenue.db import connector_models  # noqa: F401  # B2.3 - registers ConnectorRunORM + ConnectorRunRawFileORM on ReportBase.metadata
```

- [ ] **Step 5: Run test, commit**

```bash
python -m pytest -q tests/connectors/runs/test_repository.py
python -m ruff check backend/ums_smart_revenue/db/connector_models.py \
                     tests/connectors/runs/test_repository.py
```
Expected: `2 passed`.

Note: The `test_orm_tables_registered_on_report_base` test creates ALL of `ReportBase.metadata` tables via `create_all(engine)`. That requires SQLite to resolve the composite FKs to `users` and `raw_report_files`. Verify that creating ReportBase tables doesn't fail because `users` lives on `SecurityBase.metadata`. If the test fails with an FK resolution error, scope the fixture to only the two new tables via `ConnectorRunORM.__table__.create(engine)` + `ConnectorRunRawFileORM.__table__.create(engine)` (skip the FK constraints in SQLite via `engine.connect()` + `execute(text("PRAGMA foreign_keys=OFF"))`). The PR #32 / PR #34 fixtures should illustrate the pattern; mirror them.

```bash
git add backend/ums_smart_revenue/db/connector_models.py \
        backend/ums_smart_revenue/db/alembic/env.py \
        tests/connectors/runs/__init__.py \
        tests/connectors/runs/test_repository.py
git commit -m "$(cat <<'EOF'
feat(b2.3): add ConnectorRunORM + ConnectorRunRawFileORM models

Two new tables on ReportBase.metadata for B2 run tracking:
- connector_runs: per-run lifecycle row with status enum
  (RUNNING|SUCCEEDED|PARTIAL|FAILED), counts_json (fixed shape), and a
  500-char-truncated error_summary. UNIQUE (tenant_id, id) supports
  composite FKs from the join table.
- connector_run_raw_files: tenant-scoped join with composite FKs to
  connector_runs and raw_report_files on (tenant_id, id), and a UNIQUE
  on (tenant_id, connector_run_id, raw_report_file_id).

Composite tenant-aware FKs (S3/RLS-ready); migration in next commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: Alembic migration (new tables + raw_report_files UNIQUE + indexes)

**Files:**
- Create: `backend/ums_smart_revenue/db/alembic/versions/20260527_0001_connector_runs.py`

- [ ] **Step 1: Generate migration skeleton**

Use the previous Alembic revision (head before B2.3) as `down_revision`. Find it:
```bash
PYTHONPATH=backend python -m alembic --raiseerr current 2>&1 | head -1
# OR list versions:
ls backend/ums_smart_revenue/db/alembic/versions/ | sort | tail -3
```
Capture the most recent revision id; use it as `down_revision` below.

- [ ] **Step 2: Write the migration**

`backend/ums_smart_revenue/db/alembic/versions/20260527_0001_connector_runs.py`:
```python
"""b2.3: connector_runs + connector_run_raw_files + raw_report_files UNIQUE.

Adds two new tables for B2 run tracking and one additive UNIQUE
(tenant_id, id) on the existing raw_report_files table so the join
table can declare composite tenant-aware FKs. The constraint is safe
because id alone is already PK-unique; no data backfill needed.

Revision ID: 20260527_0001
Revises: <fill in from `alembic current` or `ls .../versions/`>
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260527_0001"
down_revision = "<PREVIOUS_REV_ID>"  # replace with the captured revision
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. raw_report_files: additive UNIQUE (tenant_id, id) for composite FK targets.
    op.create_unique_constraint(
        "uq_raw_report_files_tenant_id_id",
        "raw_report_files",
        ["tenant_id", "id"],
    )

    # 2. connector_runs.
    op.create_table(
        "connector_runs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("connector_key", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("report_month", sa.Text(), nullable=False),
        sa.Column("triggered_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'RUNNING'"),
            nullable=False,
        ),
        sa.Column("counts_json", sa.JSON(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','PARTIAL','FAILED')",
            name="ck_connector_runs_status",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_connector_runs_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "triggered_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_connector_runs_triggered_by_user",
        ),
    )
    op.create_index(
        "ix_connector_runs_tenant_connector_month",
        "connector_runs",
        ["tenant_id", "connector_key", "report_month"],
    )
    op.create_index(
        "ix_connector_runs_tenant_started",
        "connector_runs",
        ["tenant_id", sa.text("started_at DESC")],
    )

    # 3. connector_run_raw_files.
    op.create_table(
        "connector_run_raw_files",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("connector_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("raw_report_file_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ordering_index", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "connector_run_id",
            "raw_report_file_id",
            name="uq_connector_run_raw_files_run_file",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connector_run_id"],
            ["connector_runs.tenant_id", "connector_runs.id"],
            name="fk_connector_run_raw_files_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "raw_report_file_id"],
            ["raw_report_files.tenant_id", "raw_report_files.id"],
            name="fk_connector_run_raw_files_raw_file",
        ),
    )
    op.create_index(
        "ix_connector_run_raw_files_tenant_raw_file",
        "connector_run_raw_files",
        ["tenant_id", "raw_report_file_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connector_run_raw_files_tenant_raw_file",
        table_name="connector_run_raw_files",
    )
    op.drop_table("connector_run_raw_files")
    op.drop_index(
        "ix_connector_runs_tenant_started", table_name="connector_runs"
    )
    op.drop_index(
        "ix_connector_runs_tenant_connector_month", table_name="connector_runs"
    )
    op.drop_table("connector_runs")
    op.drop_constraint(
        "uq_raw_report_files_tenant_id_id",
        "raw_report_files",
        type_="unique",
    )
```

- [ ] **Step 3: Smoke-test migration locally (SQLite)**

```bash
PYTHONPATH=backend python -m alembic upgrade head 2>&1 | tail -20
PYTHONPATH=backend python -m alembic downgrade -1 2>&1 | tail -10
PYTHONPATH=backend python -m alembic upgrade head
```
Expected: clean apply, downgrade, re-apply. (Real test against postgres:18-alpine is T18.)

- [ ] **Step 4: Commit**

```bash
git add backend/ums_smart_revenue/db/alembic/versions/20260527_0001_connector_runs.py
git commit -m "$(cat <<'EOF'
feat(b2.3): add Alembic migration for connector_runs

Creates:
- connector_runs with CHECK status, UNIQUE (tenant_id, id), composite FK
  to users (tenant_id, id), and indexes ix_*_tenant_connector_month +
  ix_*_tenant_started.
- connector_run_raw_files with composite FKs to both parents on
  (tenant_id, id), UNIQUE (tenant_id, connector_run_id, raw_report_file_id),
  and ix_*_tenant_raw_file for reverse lookup.
- Additive UNIQUE (tenant_id, id) on raw_report_files (safe; id alone is
  already PK-unique, no backfill).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: ConnectorRunsRepository.start_run

**Files:**
- Create: `backend/ums_smart_revenue/connectors/runs/repository.py`
- Modify: `tests/connectors/runs/test_repository.py` (append start_run tests)

- [ ] **Step 1: Append failing tests**

Append to `tests/connectors/runs/test_repository.py`:
```python
from uuid import uuid4

from ums_smart_revenue.connectors.runs.repository import (
    ConnectorRunEntry,
    start_run,
)
from ums_smart_revenue.db.connector_models import ConnectorRunORM


def test_start_run_inserts_running_row(session) -> None:
    tenant_id = uuid4()
    entry = start_run(
        session,
        tenant_id=tenant_id,
        connector_key="youtube-reporting",
        account_id="account-1",
        report_month="2026-05",
        triggered_by_user_id=None,
    )
    assert isinstance(entry, ConnectorRunEntry)
    assert entry.status == "RUNNING"
    row = session.get(ConnectorRunORM, entry.id)
    assert row is not None
    assert row.tenant_id == tenant_id
    assert row.connector_key == "youtube-reporting"
    assert row.counts_json == _zero_counts()


def _zero_counts() -> dict:
    return {
        "reports_attempted": 0,
        "reports_succeeded": 0,
        "reports_failed": 0,
        "rows_upserted_total": 0,
        "rows_upserted_created": 0,
        "rows_upserted_updated": 0,
        "rows_upserted_unchanged": 0,
    }
```

(Also update the `session` fixture: it currently yields a Session bound to an in-memory SQLite. start_run needs to flush to issue the INSERT and pick up the server-defaulted UUID. Verify the fixture's engine is `create_engine("sqlite:///:memory:")`; if `flush()` produces no id, use `id=uuid4()` explicitly in the repo function below.)

- [ ] **Step 2: Verify failure**

```bash
python -m pytest -q tests/connectors/runs/test_repository.py
```
Expected: `ImportError`.

- [ ] **Step 3: Implement**

`backend/ums_smart_revenue/connectors/runs/repository.py`:
```python
"""B2.3 repository: connector_runs lifecycle.

start_run inserts a RUNNING row with a zeroed counts_json. The caller is
responsible for committing the transaction (B2.4 orchestrator commits this
alongside the CONNECTOR_JOB_RUN/STARTED audit event for forensic
durability).

finish_run and link_raw_file are added in tasks 16 and 17.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from ums_smart_revenue.db.connector_models import ConnectorRunORM


@dataclass(frozen=True)
class ConnectorRunEntry:
    id: UUID
    tenant_id: UUID
    connector_key: str
    account_id: str
    report_month: str
    triggered_by_user_id: UUID | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    counts: dict[str, int]
    error_summary: str | None


def _zero_counts() -> dict[str, int]:
    return {
        "reports_attempted": 0,
        "reports_succeeded": 0,
        "reports_failed": 0,
        "rows_upserted_total": 0,
        "rows_upserted_created": 0,
        "rows_upserted_updated": 0,
        "rows_upserted_unchanged": 0,
    }


def _to_entry(row: ConnectorRunORM) -> ConnectorRunEntry:
    return ConnectorRunEntry(
        id=row.id,
        tenant_id=row.tenant_id,
        connector_key=row.connector_key,
        account_id=row.account_id,
        report_month=row.report_month,
        triggered_by_user_id=row.triggered_by_user_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=row.status,
        counts=dict(row.counts_json),
        error_summary=row.error_summary,
    )


def start_run(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    report_month: str,
    triggered_by_user_id: UUID | None,
) -> ConnectorRunEntry:
    row = ConnectorRunORM(
        id=uuid4(),
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
        report_month=report_month,
        triggered_by_user_id=triggered_by_user_id,
        status="RUNNING",
        counts_json=_zero_counts(),
    )
    session.add(row)
    session.flush()
    return _to_entry(row)
```

- [ ] **Step 4: Run, commit**

```bash
python -m pytest -q tests/connectors/runs/test_repository.py
git add backend/ums_smart_revenue/connectors/runs/repository.py \
        tests/connectors/runs/test_repository.py
git commit -m "$(cat <<'EOF'
feat(b2.3): add connector_runs repository - start_run

start_run inserts a RUNNING row with zeroed counts_json (fixed shape - every
key present, all 0). Returns an immutable ConnectorRunEntry. Caller commits
the transaction (B2.4 orchestrator commits alongside the audit event).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 16: ConnectorRunsRepository.finish_run

- [ ] **Step 1: Append failing tests**

Append to `tests/connectors/runs/test_repository.py`:
```python
import pytest
from datetime import datetime

from ums_smart_revenue.connectors.runs.repository import finish_run


def test_finish_run_sets_terminal_state(session) -> None:
    tenant_id = uuid4()
    entry = start_run(
        session,
        tenant_id=tenant_id,
        connector_key="youtube-reporting",
        account_id="acct",
        report_month="2026-05",
        triggered_by_user_id=None,
    )
    counts = {
        "reports_attempted": 3,
        "reports_succeeded": 2,
        "reports_failed": 1,
        "rows_upserted_total": 100,
        "rows_upserted_created": 80,
        "rows_upserted_updated": 15,
        "rows_upserted_unchanged": 5,
    }
    finished = finish_run(
        session,
        tenant_id=tenant_id,
        connector_run_id=entry.id,
        status="PARTIAL",
        counts=counts,
        error_summary="one report failed",
    )
    assert finished.status == "PARTIAL"
    assert finished.counts == counts
    assert finished.finished_at is not None
    assert finished.error_summary == "one report failed"


def test_finish_run_truncates_error_summary(session) -> None:
    tenant_id = uuid4()
    entry = start_run(
        session, tenant_id=tenant_id, connector_key="x", account_id="a",
        report_month="2026-05", triggered_by_user_id=None,
    )
    huge = "x" * 1000
    finished = finish_run(
        session, tenant_id=tenant_id, connector_run_id=entry.id,
        status="FAILED", counts=_zero_counts(), error_summary=huge,
    )
    assert finished.error_summary is not None
    assert len(finished.error_summary) == 500


@pytest.mark.parametrize("status", ["RUNNING", "succeeded", "invalid"])
def test_finish_run_rejects_invalid_status(session, status) -> None:
    tenant_id = uuid4()
    entry = start_run(
        session, tenant_id=tenant_id, connector_key="x", account_id="a",
        report_month="2026-05", triggered_by_user_id=None,
    )
    with pytest.raises(ValueError):
        finish_run(
            session, tenant_id=tenant_id, connector_run_id=entry.id,
            status=status, counts=_zero_counts(), error_summary=None,
        )


def test_finish_run_cross_tenant_refused(session) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    entry = start_run(
        session, tenant_id=tenant_a, connector_key="x", account_id="a",
        report_month="2026-05", triggered_by_user_id=None,
    )
    with pytest.raises(LookupError):
        finish_run(
            session, tenant_id=tenant_b, connector_run_id=entry.id,
            status="SUCCEEDED", counts=_zero_counts(), error_summary=None,
        )
```

- [ ] **Step 2: Implement (append to repository.py)**

```python
from typing import Literal

from sqlalchemy import select

_TERMINAL_STATUSES: tuple[Literal["SUCCEEDED", "PARTIAL", "FAILED"], ...] = (
    "SUCCEEDED", "PARTIAL", "FAILED"
)
_ERROR_SUMMARY_MAX = 500


def finish_run(
    session: Session,
    *,
    tenant_id: UUID,
    connector_run_id: UUID,
    status: Literal["SUCCEEDED", "PARTIAL", "FAILED"],
    counts: dict[str, int],
    error_summary: str | None,
) -> ConnectorRunEntry:
    if status not in _TERMINAL_STATUSES:
        raise ValueError(
            f"finish_run status must be one of {_TERMINAL_STATUSES}, got {status!r}"
        )
    stmt = select(ConnectorRunORM).where(
        ConnectorRunORM.id == connector_run_id,
        ConnectorRunORM.tenant_id == tenant_id,
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        raise LookupError(
            f"connector_run {connector_run_id} not found for tenant {tenant_id}"
        )
    from datetime import datetime, timezone
    row.status = status
    row.finished_at = datetime.now(timezone.utc)
    row.counts_json = dict(counts)
    if error_summary is None:
        row.error_summary = None
    else:
        row.error_summary = error_summary[:_ERROR_SUMMARY_MAX]
    session.flush()
    return _to_entry(row)
```

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/runs/test_repository.py
git add backend/ums_smart_revenue/connectors/runs/repository.py \
        tests/connectors/runs/test_repository.py
git commit -m "$(cat <<'EOF'
feat(b2.3): add connector_runs repository - finish_run

finish_run accepts only SUCCEEDED|PARTIAL|FAILED (rejects RUNNING and
arbitrary strings). Sets finished_at to now(UTC), overwrites counts_json
with the provided dict, truncates error_summary to 500 chars (None stays
None). Cross-tenant connector_run_id raises LookupError.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 17: ConnectorRunsRepository.link_raw_file

- [ ] **Step 1: Append failing tests**

Append to `tests/connectors/runs/test_repository.py`:
```python
from ums_smart_revenue.connectors.runs.repository import link_raw_file
from ums_smart_revenue.db.connector_models import ConnectorRunRawFileORM
from ums_smart_revenue.db.report_models import RawReportFileORM


def _insert_raw_report_file(session, *, tenant_id) -> UUID:
    raw = RawReportFileORM(
        tenant_id=tenant_id,
        source="youtube_reporting",
        report_type="channel_basic_a2",
        report_month="2026-05",
        file_url="file-store://x/y.csv",
        checksum="abc",
        parse_status="DOWNLOADED",
    )
    session.add(raw)
    session.flush()
    return raw.id


def test_link_raw_file_inserts_join_row(session) -> None:
    tenant_id = uuid4()
    run = start_run(
        session, tenant_id=tenant_id, connector_key="yt", account_id="a",
        report_month="2026-05", triggered_by_user_id=None,
    )
    raw_id = _insert_raw_report_file(session, tenant_id=tenant_id)
    link_raw_file(
        session, tenant_id=tenant_id, connector_run_id=run.id,
        raw_report_file_id=raw_id, ordering_index=0,
    )
    session.flush()
    rows = session.query(ConnectorRunRawFileORM).all()
    assert len(rows) == 1
    assert rows[0].connector_run_id == run.id
    assert rows[0].raw_report_file_id == raw_id


def test_link_raw_file_cross_tenant_raw_file_refused(session) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    run = start_run(
        session, tenant_id=tenant_a, connector_key="yt", account_id="a",
        report_month="2026-05", triggered_by_user_id=None,
    )
    raw_id_b = _insert_raw_report_file(session, tenant_id=tenant_b)
    with pytest.raises(LookupError):
        link_raw_file(
            session, tenant_id=tenant_a, connector_run_id=run.id,
            raw_report_file_id=raw_id_b, ordering_index=0,
        )


def test_link_raw_file_duplicate_link_rejected(session) -> None:
    tenant_id = uuid4()
    run = start_run(
        session, tenant_id=tenant_id, connector_key="yt", account_id="a",
        report_month="2026-05", triggered_by_user_id=None,
    )
    raw_id = _insert_raw_report_file(session, tenant_id=tenant_id)
    link_raw_file(
        session, tenant_id=tenant_id, connector_run_id=run.id,
        raw_report_file_id=raw_id, ordering_index=0,
    )
    session.flush()
    with pytest.raises(Exception):  # IntegrityError on UNIQUE
        link_raw_file(
            session, tenant_id=tenant_id, connector_run_id=run.id,
            raw_report_file_id=raw_id, ordering_index=1,
        )
        session.flush()
```

- [ ] **Step 2: Implement (append to repository.py)**

```python
def link_raw_file(
    session: Session,
    *,
    tenant_id: UUID,
    connector_run_id: UUID,
    raw_report_file_id: UUID,
    ordering_index: int,
) -> None:
    """Insert a tenant-scoped join row.

    Tenant scope is enforced by loading the raw_report_file with the same
    tenant_id; cross-tenant raw_file_id raises LookupError. The DB-level
    composite FK (tenant_id, raw_report_file_id) would also reject the
    insert, but loading first gives a clearer error.

    report_type is intentionally NOT a parameter - the spec contract says
    it derives from the loaded RawReportFileORM. If the caller wants to
    surface report_type, they read it via session.get(RawReportFileORM, raw_report_file_id).
    """
    from ums_smart_revenue.db.report_models import RawReportFileORM

    stmt = select(RawReportFileORM).where(
        RawReportFileORM.id == raw_report_file_id,
        RawReportFileORM.tenant_id == tenant_id,
    )
    raw = session.execute(stmt).scalar_one_or_none()
    if raw is None:
        raise LookupError(
            f"raw_report_file {raw_report_file_id} not found for tenant {tenant_id}"
        )
    join = ConnectorRunRawFileORM(
        id=uuid4(),
        tenant_id=tenant_id,
        connector_run_id=connector_run_id,
        raw_report_file_id=raw_report_file_id,
        ordering_index=ordering_index,
    )
    session.add(join)
```

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/runs/test_repository.py
git add backend/ums_smart_revenue/connectors/runs/repository.py \
        tests/connectors/runs/test_repository.py
git commit -m "$(cat <<'EOF'
feat(b2.3): add connector_runs repository - link_raw_file

link_raw_file inserts a tenant-scoped join row. report_type is NOT a
caller parameter (spec contract: derives from the loaded raw_file).
Cross-tenant raw_file_id raises LookupError before the DB-level
composite FK fires. UNIQUE (tenant_id, connector_run_id,
raw_report_file_id) prevents duplicate links.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 18: PostgreSQL migration round-trip test

**Files:**
- Create: `tests/db/test_connector_runs_migration_postgres.py`

Mirror the existing `tests/db/test_google_revenue_source_migration_postgres.py` pattern: spin a disposable `postgres:18-alpine`, run `alembic upgrade head`, run `alembic downgrade -1`, assert post-state matches expectations.

- [ ] **Step 1: Write the test**

```python
"""PostgreSQL migration round-trip for the B2.3 connector_runs Alembic.

Requires TEST_DATABASE_URL pointing at a disposable postgres:18-alpine.
Pattern mirrored from tests/db/test_google_revenue_source_migration_postgres.py.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set; skip live-postgres migration test.",
)


def _run_alembic(args: list[str]) -> None:
    import subprocess

    env = os.environ.copy()
    env["PYTHONPATH"] = "backend"
    result = subprocess.run(
        ["python", "-m", "alembic", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr


def test_upgrade_creates_tables_constraints_indexes() -> None:
    db_url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(db_url)
    # Ensure clean state.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    _run_alembic(["upgrade", "head"])

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "connector_runs" in tables
    assert "connector_run_raw_files" in tables

    # connector_runs UNIQUE (tenant_id, id)
    runs_uniques = [tuple(u["column_names"]) for u in insp.get_unique_constraints("connector_runs")]
    assert ("tenant_id", "id") in runs_uniques

    # raw_report_files additive UNIQUE (tenant_id, id)
    raw_uniques = [tuple(u["column_names"]) for u in insp.get_unique_constraints("raw_report_files")]
    assert ("tenant_id", "id") in raw_uniques

    # Indexes
    indexes = {idx["name"] for idx in insp.get_indexes("connector_run_raw_files")}
    assert "ix_connector_run_raw_files_tenant_raw_file" in indexes

    indexes_runs = {idx["name"] for idx in insp.get_indexes("connector_runs")}
    assert "ix_connector_runs_tenant_connector_month" in indexes_runs
    assert "ix_connector_runs_tenant_started" in indexes_runs


def test_downgrade_removes_everything_cleanly() -> None:
    _run_alembic(["downgrade", "-1"])
    db_url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(db_url)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "connector_runs" not in tables
    assert "connector_run_raw_files" not in tables
    # raw_report_files's additive UNIQUE is also gone.
    raw_uniques = [tuple(u["column_names"]) for u in insp.get_unique_constraints("raw_report_files")]
    assert ("tenant_id", "id") not in raw_uniques
    # Restore HEAD for downstream tests.
    _run_alembic(["upgrade", "head"])


def test_cross_tenant_join_insert_rejected_by_composite_fk() -> None:
    """The composite FK (tenant_id, connector_run_id) -> connector_runs and
    (tenant_id, raw_report_file_id) -> raw_report_files reject any insert
    whose tenant_id does not match BOTH parents. Test by direct INSERT."""
    from uuid import uuid4

    import sqlalchemy as sa

    db_url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(db_url)
    tenant_a = uuid4()
    tenant_b = uuid4()
    run_id = uuid4()
    raw_id_b = uuid4()
    with engine.begin() as conn:
        # Need a users row for the FK on connector_runs.triggered_by_user_id;
        # skip that by inserting connector_runs with triggered_by_user_id=NULL.
        conn.execute(
            sa.text("""
                INSERT INTO connector_runs
                    (id, tenant_id, connector_key, account_id, report_month,
                     started_at, status, counts_json)
                VALUES (:id, :t, 'yt', 'a', '2026-05', NOW(), 'RUNNING', '{}')
            """),
            {"id": run_id, "t": tenant_a},
        )
        conn.execute(
            sa.text("""
                INSERT INTO raw_report_files
                    (id, tenant_id, source, report_type, report_month,
                     file_url, checksum, parse_status, downloaded_at, updated_at)
                VALUES (:id, :t, 'youtube_reporting', 'channel_basic_a2',
                        '2026-05', 'x', 'abc', 'DOWNLOADED', NOW(), NOW())
            """),
            {"id": raw_id_b, "t": tenant_b},
        )
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text("""
                    INSERT INTO connector_run_raw_files
                        (id, tenant_id, connector_run_id, raw_report_file_id,
                         linked_at, ordering_index)
                    VALUES (:id, :t, :run, :raw, NOW(), 0)
                """),
                {"id": uuid4(), "t": tenant_a, "run": run_id, "raw": raw_id_b},
            )
```

- [ ] **Step 2: Run against disposable postgres**

```bash
docker run -d --name ums-pg-test -p 55432:5432 \
    -e POSTGRES_USER=ums -e POSTGRES_PASSWORD=ums -e POSTGRES_DB=ums \
    postgres:18-alpine
# Wait for ready:
sleep 3
TEST_DATABASE_URL=postgresql+psycopg://ums:ums@localhost:55432/ums \
    python -m pytest -q tests/db/test_connector_runs_migration_postgres.py
docker rm -f ums-pg-test
```
Expected: tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/db/test_connector_runs_migration_postgres.py
git commit -m "$(cat <<'EOF'
test(b2.3): PostgreSQL migration round-trip + index assertions

Asserts the B2.3 migration upgrades cleanly on postgres:18-alpine:
- connector_runs + connector_run_raw_files tables created.
- UNIQUE (tenant_id, id) on connector_runs AND raw_report_files.
- All three indexes present (ix_*_tenant_raw_file,
  ix_*_tenant_connector_month, ix_*_tenant_started).
Downgrade removes everything cleanly.

Test skips when TEST_DATABASE_URL is unset.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 19: B2.3 validation gate + plan/backlog markers

- [ ] **Step 1: Run B2.3 validation gate**

```bash
python -m ruff check backend tests
python -m pytest -q tests/connectors/runs/test_repository.py \
                    tests/db/test_connector_runs_migration_postgres.py
python scripts/run_validation_gate.py
git diff --check
```

- [ ] **Step 2: Update planning docs**

Append to B2 section of `Docs/01_IMPLEMENTATION_PLAN.md` and `Docs/15_DELIVERY_BACKLOG.md`:
```markdown
- ⏳ PR #N (B2.3) — connector_runs + connector_run_raw_files ORM + repo
  (start_run / finish_run / link_raw_file); Alembic migration with
  composite tenant-aware FKs, additive UNIQUE (tenant_id, id) on
  raw_report_files, and operational indexes.
```

- [ ] **Step 3: Commit**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "$(cat <<'EOF'
docs(b2.3): mark connector_runs run tracking as in-progress

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## PR B2.4 — HTTP base + YT Reporting + orchestrator + CLI

Spec reference: §5.4 (public surface), §6 (3-bucket failure model), §7 (retry policy), §6.5 rows 6-14 (error taxonomy), §9.3 B2.4.

### Task 20: GoogleHttpClient base (init + happy-path request)

**Files:**
- Modify: `backend/ums_smart_revenue/connectors/google/errors.py` (append HTTP error classes + CredentialNotFoundError + InactiveCredentialError + UnsupportedReportTypeError)
- Create: `backend/ums_smart_revenue/connectors/google/http_client.py`
- Create: `tests/connectors/google/conftest.py` (shared `httpx.MockTransport` helpers)
- Create: `tests/connectors/google/test_http_client.py`

- [ ] **Step 1: Append error classes**

Append to `backend/ums_smart_revenue/connectors/google/errors.py`:
```python
class CredentialNotFoundError(GoogleConnectorError):
    def __init__(self, *, connector_key: str, account_id: str) -> None:
        super().__init__(f"no credential for {connector_key}/{account_id}")
        self.connector_key = connector_key
        self.account_id = account_id


class InactiveCredentialError(GoogleConnectorError):
    def __init__(self, *, credential_id: str, status: str) -> None:
        super().__init__(f"credential {credential_id} is {status}, not active")
        self.credential_id = credential_id
        self.status = status


class _GoogleApiHttpError(GoogleConnectorError):
    def __init__(self, *, method: str, url: str, status: int, attempts: int = 1) -> None:
        if attempts > 1:
            msg = f"{method} {url}: HTTP {status} after {attempts} retries"
        else:
            msg = f"{method} {url}: HTTP {status}"
        super().__init__(msg)
        self.method = method
        self.url = url
        self.status = status
        self.attempts = attempts


class GoogleApiAuthError(_GoogleApiHttpError):
    pass


class GoogleApiClientError(_GoogleApiHttpError):
    pass


class GoogleApiRateLimitError(_GoogleApiHttpError):
    pass


class GoogleApiServerError(_GoogleApiHttpError):
    pass


class GoogleApiResponseError(GoogleConnectorError):
    def __init__(self, *, url: str, reason: str) -> None:
        super().__init__(f"{url}: response schema invalid ({reason})")
        self.url = url
        self.reason = reason


class UnsupportedReportTypeError(GoogleConnectorError):
    def __init__(self, *, report_type_id: str) -> None:
        super().__init__(f"report_type_id {report_type_id} not in supported set")
        self.report_type_id = report_type_id
```

- [ ] **Step 2: Write conftest.py for httpx.MockTransport**

`tests/connectors/google/conftest.py`:
```python
"""Shared httpx.MockTransport helpers for B2.4+ client tests."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest


def make_mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture
def mock_credentials():
    """Build a stub google-auth Credentials that no-ops before_request."""
    class _StubCreds:
        token = "fake-bearer"

        def before_request(self, request: Any, method: str, url: str, headers: dict) -> None:
            headers["Authorization"] = f"Bearer {self.token}"

    return _StubCreds()
```

- [ ] **Step 3: Write the failing happy-path test**

`tests/connectors/google/test_http_client.py`:
```python
"""GoogleHttpClient happy-path tests.

The client invokes credentials.before_request(...) on every request so
google-auth handles refresh; it parses JSON responses and returns the
decoded dict.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient


def test_request_invokes_before_request_and_parses_json(mock_credentials) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200, content=json.dumps({"jobs": [{"id": "job-1"}]}).encode()
        )

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    body = client.request(method="GET", url="https://example.com/v1/jobs")
    assert body == {"jobs": [{"id": "job-1"}]}
    assert captured["auth"] == "Bearer fake-bearer"


def test_request_passes_query_params(mock_credentials) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client.request(
        method="GET", url="https://example.com/v1/x",
        params={"pageToken": "abc", "limit": "10"},
    )
    assert captured["query"] == {"pageToken": "abc", "limit": "10"}
```

- [ ] **Step 4: Implement minimal client**

`backend/ums_smart_revenue/connectors/google/http_client.py`:
```python
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
```

- [ ] **Step 5: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_http_client.py
git add backend/ums_smart_revenue/connectors/google/errors.py \
        backend/ums_smart_revenue/connectors/google/http_client.py \
        tests/connectors/google/conftest.py \
        tests/connectors/google/test_http_client.py
git commit -m "$(cat <<'EOF'
feat(b2.4): add GoogleHttpClient base + B2.4 error classes

GoogleHttpClient.request invokes credentials.before_request(...) on every
send so google-auth handles access-token refresh. Happy path only here:
200 -> parsed JSON. Retry policy and error taxonomy in subsequent commits.

Also extends GoogleConnectorError with CredentialNotFoundError,
InactiveCredentialError, GoogleApiAuthError, GoogleApiClientError,
GoogleApiRateLimitError, GoogleApiServerError, GoogleApiResponseError,
and UnsupportedReportTypeError.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 21: GoogleHttpClient retry policy + HTTP error taxonomy

Spec §7 retry table:
| Status | Action |
|---|---|
| 400/404/422 | `GoogleApiClientError`, no retry |
| 401/403 | `GoogleApiAuthError`, no retry |
| 429 | exp backoff 1s/2s/4s/8s max 4 attempts, honor `Retry-After` clamp 64s -> `GoogleApiRateLimitError` |
| 5xx / timeout | exp backoff 1s/2s/4s/8s max 4 attempts -> `GoogleApiServerError` |
| DNS / TCP reset | exp backoff 1s/2s/4s max 3 attempts -> `GoogleApiServerError` |

- [ ] **Step 1: Append failing tests**

Append to `tests/connectors/google/test_http_client.py`:
```python
from ums_smart_revenue.connectors.google.errors import (
    GoogleApiAuthError,
    GoogleApiClientError,
    GoogleApiRateLimitError,
    GoogleApiServerError,
)


@pytest.mark.parametrize("status", [400, 404, 422])
def test_4xx_client_errors_no_retry(mock_credentials, status) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiClientError) as ctx:
        client.request(method="GET", url="https://example.com/x")
    assert ctx.value.status == status
    assert len(calls) == 1  # no retry


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_no_retry(mock_credentials, status) -> None:
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiAuthError):
        client.request(method="GET", url="https://example.com/x")
    assert len(calls) == 1


def test_429_retries_then_raises(mock_credentials, monkeypatch) -> None:
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        lambda _: None,
    )
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiRateLimitError) as ctx:
        client.request(method="GET", url="https://example.com/x")
    assert ctx.value.attempts == 4
    assert len(calls) == 4


def test_429_honors_retry_after(mock_credentials, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        sleeps.append,
    )
    seq = iter([
        httpx.Response(429, headers={"Retry-After": "3"}, content=b"{}"),
        httpx.Response(200, content=b"{}"),
    ])
    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(lambda r: next(seq)),
    )
    client.request(method="GET", url="https://example.com/x")
    assert sleeps == [3.0]


def test_5xx_retries_then_raises(mock_credentials, monkeypatch) -> None:
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        lambda _: None,
    )
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(503, content=b"{}")
    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiServerError):
        client.request(method="GET", url="https://example.com/x")
    assert len(calls) == 4
```

- [ ] **Step 2: Implement retry**

Replace the body of `request()` and add a `_send_with_retry()` helper:
```python
import time

_CLIENT_STATUSES = frozenset({400, 404, 422})
_AUTH_STATUSES = frozenset({401, 403})
_RETRY_SERVER_STATUSES = frozenset({500, 502, 503, 504})


def _backoff_schedule(max_attempts: int) -> list[float]:
    # 1, 2, 4, 8 (capped at 64); used for both 429 (without Retry-After) and 5xx.
    return [min(2 ** i, 64.0) for i in range(max_attempts)]


def request(...) -> dict[str, object]:
    # ... build headers ...
    backoff = _backoff_schedule(4)
    last_status = 0
    for attempt in range(1, 5):
        response = self._client.request(...)
        status = response.status_code
        last_status = status
        if status == 200:
            return response.json()
        if status in _CLIENT_STATUSES:
            raise GoogleApiClientError(method=method, url=url, status=status)
        if status in _AUTH_STATUSES:
            raise GoogleApiAuthError(method=method, url=url, status=status)
        if status == 429:
            if attempt == 4:
                raise GoogleApiRateLimitError(method=method, url=url, status=429, attempts=4)
            ra = response.headers.get("Retry-After")
            sleep_s = float(ra) if ra and ra.isdigit() else backoff[attempt - 1]
            time.sleep(min(sleep_s, 64.0))
            continue
        if status in _RETRY_SERVER_STATUSES:
            if attempt == 4:
                raise GoogleApiServerError(method=method, url=url, status=status, attempts=4)
            time.sleep(backoff[attempt - 1])
            continue
        # Any other status: treat as client error (defensive).
        raise GoogleApiClientError(method=method, url=url, status=status)
    raise GoogleApiServerError(method=method, url=url, status=last_status, attempts=4)
```

(Full implementation — connect/read timeout retry via `try/except httpx.TimeoutException` and DNS/TCP reset via `httpx.ConnectError` — follows the same backoff schedule shape; max 4 for timeout, max 3 for DNS/TCP. Add these `except` branches inside the loop.)

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_http_client.py
git add backend/ums_smart_revenue/connectors/google/http_client.py \
        tests/connectors/google/test_http_client.py
git commit -m "$(cat <<'EOF'
feat(b2.4): add GoogleHttpClient retry policy + HTTP error taxonomy

Spec §7: 400/404/422 -> GoogleApiClientError no retry; 401/403 ->
GoogleApiAuthError no retry; 429 -> exp 1/2/4/8s max 4 attempts honoring
Retry-After clamp 64s -> GoogleApiRateLimitError; 5xx -> same backoff ->
GoogleApiServerError; timeouts/connect errors retry with the same shape.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 22: GoogleHttpClient response validation (non-JSON + schema)

- [ ] **Step 1: Append tests**

```python
from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError


def test_non_json_response_raises_response_error(mock_credentials) -> None:
    def handler(request):
        return httpx.Response(200, content=b"<html>not json</html>")
    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiResponseError) as ctx:
        client.request(method="GET", url="https://example.com/x")
    assert "json" in ctx.value.reason.lower()


def test_non_object_json_raises_response_error(mock_credentials) -> None:
    def handler(request):
        return httpx.Response(200, content=b"[1, 2, 3]")
    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiResponseError):
        client.request(method="GET", url="https://example.com/x")
```

- [ ] **Step 2: Update `request()` to validate**

Replace `return response.json()` with:
```python
try:
    body = response.json()
except (ValueError, json.JSONDecodeError) as exc:
    raise GoogleApiResponseError(url=url, reason=f"not valid json: {exc}") from exc
if not isinstance(body, dict):
    raise GoogleApiResponseError(url=url, reason=f"expected object, got {type(body).__name__}")
return body
```

Import `json` at the top of the file.

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_http_client.py
git add backend/ums_smart_revenue/connectors/google/http_client.py \
        tests/connectors/google/test_http_client.py
git commit -m "$(cat <<'EOF'
feat(b2.4): GoogleHttpClient response validation

Reject non-JSON bodies and non-object JSON (arrays, scalars) with
GoogleApiResponseError. Caller-visible request lifecycle: parse OK ->
return dict; otherwise raise typed error. No retry on response errors.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 23: YouTubeReportingClient.list_supported_jobs + report_type_whitelist

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/report_type_whitelist.py`
- Create: `backend/ums_smart_revenue/connectors/google/youtube_reporting_client.py`
- Create: `tests/connectors/google/test_youtube_reporting_client.py`

- [ ] **Step 1: Implement whitelist**

`backend/ums_smart_revenue/connectors/google/report_type_whitelist.py`:
```python
"""YouTube Reporting report_type_id whitelist (spec §5.4).

These are the monetary/revenue-relevant report types the existing
B1 parsers know how to consume. Outside this set raises
UnsupportedReportTypeError at orchestrator time.
"""
from __future__ import annotations

SUPPORTED_REPORT_TYPES: frozenset[str] = frozenset(
    {
        "channel_basic_a2",
        "channel_combined_a2",
        # Add additional locked-at-ship report_type_ids here as the
        # parser grows; each new addition needs a parser-side change too.
    }
)


def is_supported(report_type_id: str) -> bool:
    return report_type_id in SUPPORTED_REPORT_TYPES
```

- [ ] **Step 2: Write tests for client**

`tests/connectors/google/test_youtube_reporting_client.py`:
```python
"""YouTube Reporting client tests (spec §5.4)."""
from __future__ import annotations

import httpx
import pytest

from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.youtube_reporting_client import (
    YouTubeReportingClient,
)


def test_list_supported_jobs_filters_to_whitelist(mock_credentials) -> None:
    payload = {
        "jobs": [
            {"id": "job-a", "reportTypeId": "channel_basic_a2"},
            {"id": "job-b", "reportTypeId": "channel_combined_a2"},
            {"id": "job-c", "reportTypeId": "channel_demographics_a1"},  # not supported
        ]
    }
    def handler(request: httpx.Request) -> httpx.Response:
        import json
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    jobs = client.list_supported_jobs(account_id="content-owner-1")
    ids = {job["id"] for job in jobs}
    assert ids == {"job-a", "job-b"}


def test_list_supported_jobs_paginates(mock_credentials) -> None:
    pages = iter([
        {"jobs": [{"id": "j1", "reportTypeId": "channel_basic_a2"}], "nextPageToken": "tok-2"},
        {"jobs": [{"id": "j2", "reportTypeId": "channel_basic_a2"}]},
    ])
    def handler(request: httpx.Request) -> httpx.Response:
        import json
        return httpx.Response(200, content=json.dumps(next(pages)).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    jobs = client.list_supported_jobs(account_id="acct")
    assert [j["id"] for j in jobs] == ["j1", "j2"]
```

- [ ] **Step 3: Implement client**

`backend/ums_smart_revenue/connectors/google/youtube_reporting_client.py`:
```python
"""YouTube Reporting v1 API client.

Endpoints used (Bearer auth via GoogleHttpClient):
- GET /v1/jobs                 -> list reporting jobs
- GET /v1/jobs/{jobId}/reports -> list reports under a job (date-filtered)
- GET <downloadUrl>            -> raw CSV bytes

Caller filters by SUPPORTED_REPORT_TYPES on list_supported_jobs and
date-bounds on list_reports_for_month.
"""
from __future__ import annotations

from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.report_type_whitelist import (
    SUPPORTED_REPORT_TYPES,
)

_BASE = "https://youtubereporting.googleapis.com/v1"


class YouTubeReportingClient:
    def __init__(self, *, http: GoogleHttpClient) -> None:
        self._http = http

    def list_supported_jobs(self, *, account_id: str) -> list[dict]:
        url = f"{_BASE}/jobs"
        token: str | None = None
        out: list[dict] = []
        while True:
            params: dict[str, str] = {"onBehalfOfContentOwner": account_id}
            if token:
                params["pageToken"] = token
            body = self._http.request(method="GET", url=url, params=params)
            for job in body.get("jobs", []):
                if job.get("reportTypeId") in SUPPORTED_REPORT_TYPES:
                    out.append(job)
            token = body.get("nextPageToken")
            if not token:
                break
        return out
```

- [ ] **Step 4: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_youtube_reporting_client.py
git add backend/ums_smart_revenue/connectors/google/report_type_whitelist.py \
        backend/ums_smart_revenue/connectors/google/youtube_reporting_client.py \
        tests/connectors/google/test_youtube_reporting_client.py
git commit -m "$(cat <<'EOF'
feat(b2.4): add YouTubeReportingClient.list_supported_jobs

GET /v1/jobs paginated via nextPageToken; results filtered to
SUPPORTED_REPORT_TYPES (channel_basic_a2 + channel_combined_a2 initial
set; locked at ship and extended only with a corresponding parser
change). Out-of-whitelist report_type_ids never reach the orchestrator.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 24: YouTubeReportingClient.list_reports_for_month

- [ ] **Step 1: Append tests**

```python
def test_list_reports_for_month_passes_date_bounds(mock_credentials) -> None:
    captured = {}
    def handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("queries", []).append(dict(request.url.params))
        import json
        return httpx.Response(200, content=json.dumps({"reports": []}).encode())
    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    client.list_reports_for_month(
        account_id="acct", job_id="job-1", report_month="2026-05",
    )
    q = captured["queries"][0]
    assert q["startTimeAtOrAfter"] == "2026-05-01T00:00:00Z"
    assert q["startTimeBefore"] == "2026-06-01T00:00:00Z"


def test_list_reports_for_month_paginates(mock_credentials) -> None:
    pages = iter([
        {"reports": [{"id": "r1", "downloadUrl": "https://x/r1"}], "nextPageToken": "p2"},
        {"reports": [{"id": "r2", "downloadUrl": "https://x/r2"}]},
    ])
    def handler(request: httpx.Request) -> httpx.Response:
        import json
        return httpx.Response(200, content=json.dumps(next(pages)).encode())
    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    reports = client.list_reports_for_month(
        account_id="acct", job_id="job-1", report_month="2026-05",
    )
    assert [r["id"] for r in reports] == ["r1", "r2"]


def test_list_reports_handles_december_boundary(mock_credentials) -> None:
    captured = {}
    def handler(request):
        captured["q"] = dict(request.url.params)
        import json
        return httpx.Response(200, content=json.dumps({"reports": []}).encode())
    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    client.list_reports_for_month(
        account_id="acct", job_id="j", report_month="2026-12",
    )
    assert captured["q"]["startTimeBefore"] == "2027-01-01T00:00:00Z"
```

- [ ] **Step 2: Implement**

Append to `youtube_reporting_client.py`:
```python
from datetime import date


def _month_bounds_iso(report_month: str) -> tuple[str, str]:
    year, month = report_month.split("-")
    year_i, month_i = int(year), int(month)
    start = date(year_i, month_i, 1)
    if month_i == 12:
        end = date(year_i + 1, 1, 1)
    else:
        end = date(year_i, month_i + 1, 1)
    return (
        f"{start.isoformat()}T00:00:00Z",
        f"{end.isoformat()}T00:00:00Z",
    )


def list_reports_for_month(  # method body inside the class
    self, *, account_id: str, job_id: str, report_month: str,
) -> list[dict]:
    url = f"{_BASE}/jobs/{job_id}/reports"
    start_iso, end_iso = _month_bounds_iso(report_month)
    token: str | None = None
    out: list[dict] = []
    while True:
        params: dict[str, str] = {
            "onBehalfOfContentOwner": account_id,
            "startTimeAtOrAfter": start_iso,
            "startTimeBefore": end_iso,
        }
        if token:
            params["pageToken"] = token
        body = self._http.request(method="GET", url=url, params=params)
        out.extend(body.get("reports", []))
        token = body.get("nextPageToken")
        if not token:
            break
    return out
```

(Move `_month_bounds_iso` to module scope above the class definition.)

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_youtube_reporting_client.py
git add backend/ums_smart_revenue/connectors/google/youtube_reporting_client.py \
        tests/connectors/google/test_youtube_reporting_client.py
git commit -m "$(cat <<'EOF'
feat(b2.4): add YouTubeReportingClient.list_reports_for_month

GET /v1/jobs/{jobId}/reports with date filter:
  startTimeAtOrAfter=<month-first>T00:00:00Z
  startTimeBefore=<next-month-first>T00:00:00Z
Paginates via nextPageToken. December boundary handled explicitly
(2026-12 -> 2027-01-01 endpoint).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 25: YouTubeReportingClient.fetch_report

- [ ] **Step 1: Append tests**

```python
def test_fetch_report_downloads_csv_bytes(mock_credentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://yt-download/abc"
        assert request.headers.get("Authorization") == "Bearer fake-bearer"
        return httpx.Response(200, content=b"day,channel\n2026-05,xyz\n")
    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    out = client.fetch_report(download_url="https://yt-download/abc")
    assert out == b"day,channel\n2026-05,xyz\n"
```

- [ ] **Step 2: Implement**

Add `fetch_bytes` to `GoogleHttpClient` first (the existing `request()` parses JSON; CSV download needs raw bytes):
```python
def fetch_bytes(self, *, url: str) -> bytes:
    """GET a URL and return raw response bytes. Bearer auth applied via
    credentials.before_request. Retry policy from request() applied; on
    success returns the bytes."""
    headers: dict[str, str] = {}
    self._credentials.before_request(None, "GET", url, headers)
    response = self._client.get(url, headers=headers)
    # Reuse the status-mapping helper from request(); on 200 return bytes.
    if response.status_code == 200:
        return response.content
    # For B2.4 simplicity: same status-mapping inline here, or factor into
    # a shared _map_status_to_error() helper. Recommend factoring.
    if response.status_code in (401, 403):
        from ums_smart_revenue.connectors.google.errors import GoogleApiAuthError
        raise GoogleApiAuthError(method="GET", url=url, status=response.status_code)
    # ... mirror the request() error mapping ...
```

(Best practice: factor `_map_status_to_error()` and `_send_with_retry()` out of `request()` so `fetch_bytes()` reuses them. Refactor in this commit if it doesn't blow up the diff.)

Then implement `fetch_report` in the client:
```python
def fetch_report(self, *, download_url: str) -> bytes:
    return self._http.fetch_bytes(url=download_url)
```

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_youtube_reporting_client.py \
                    tests/connectors/google/test_http_client.py
git add backend/ums_smart_revenue/connectors/google/youtube_reporting_client.py \
        backend/ums_smart_revenue/connectors/google/http_client.py \
        tests/connectors/google/test_youtube_reporting_client.py
git commit -m "$(cat <<'EOF'
feat(b2.4): add YouTubeReportingClient.fetch_report (downloadUrl)

Adds GoogleHttpClient.fetch_bytes(url) so the client can download CSV
bytes with the same Bearer-auth path. YouTubeReportingClient.fetch_report
takes the downloadUrl returned by reports.list and returns the raw CSV
bytes ready for blob upload and parser ingestion.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 26: Connector registry

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/registry.py`
- Create: `tests/connectors/google/test_registry.py`

- [ ] **Step 1: Write tests**

`tests/connectors/google/test_registry.py`:
```python
"""Connector registry: maps --connector key to a runner callable."""
from __future__ import annotations

import pytest

from ums_smart_revenue.connectors.google.registry import (
    dispatch_connector,
    register_connector,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    from ums_smart_revenue.connectors.google import registry
    snap = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(snap)


def test_register_and_dispatch() -> None:
    def runner(**kwargs):
        return "ok"
    register_connector(key="youtube-reporting", runner=runner)
    assert dispatch_connector(key="youtube-reporting") is runner


def test_dispatch_unknown_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown connector key"):
        dispatch_connector(key="no-such")
```

- [ ] **Step 2: Implement**

`backend/ums_smart_revenue/connectors/google/registry.py`:
```python
"""CLI --connector dispatch registry.

B2.4 registers 'youtube-reporting'. B2.5 adds 'youtube-analytics'.
B2.6 adds 'adsense-management'. Unknown keys raise ValueError at
argparse time.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

_RunnerFn = Callable[..., Any]
_REGISTRY: dict[str, _RunnerFn] = {}


def register_connector(*, key: str, runner: _RunnerFn) -> None:
    _REGISTRY[key] = runner


def dispatch_connector(*, key: str) -> _RunnerFn:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise ValueError(f"unknown connector key: {key!r}") from exc


def known_keys() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
```

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_registry.py
git add backend/ums_smart_revenue/connectors/google/registry.py \
        tests/connectors/google/test_registry.py
git commit -m "$(cat <<'EOF'
feat(b2.4): add connector registry for --connector dispatch

Module-level registry mapping connector keys to runner callables. B2.4
registers youtube-reporting; later slices extend with youtube-analytics
(B2.5) and adsense-management (B2.6). Unknown keys raise ValueError so
the CLI argparse layer can reject them early.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 27: run_one orchestrator — happy path (single YT Reporting source)

**Files:**
- Create: `backend/ums_smart_revenue/connectors/runs/orchestrator.py`
- Create: `tests/connectors/google/test_orchestrator.py`

The orchestrator wires every prior B2 slice together: load credential → resolve secret → build OAuth → start_run → for each report (list reports → fetch → blob upload → register raw_file → parse → upsert source rows → mark_parsed → link_raw_file) → finish_run. This task implements the happy path only; failure handlers A/B/C land in T28.

- [ ] **Step 1: Define ConnectorRunOutcome + run_one signature**

`backend/ums_smart_revenue/connectors/runs/orchestrator.py`:
```python
"""B2.4 orchestrator: the public `run_one(...)` surface.

Happy path (this task):
  1. load_credential(session, tenant_id, connector_key, account_id) -> credential row
  2. resolve_secret(credential.encrypted_secret_ref) -> payload string
  3. build_credentials_from_payload(payload) -> google.oauth2.credentials.Credentials
  4. start_run(...) commits a RUNNING row.
  5. dispatch_connector(connector_key) -> runner callable. The runner
     produces per-report (parser_payload, bytes_for_blob, report_type) tuples.
  6. For each tuple:
        a. compute_checksum + deterministic_blob_path
        b. upload_and_verify
        c. RawReportFileORM insert (parse_status=DOWNLOADED), session.flush -> raw_file_id
        d. link_raw_file(connector_run_id, raw_file_id, ordering_index=i)
        e. parser.parse(payload) -> ParsedSourceRow iterable
        f. SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many(...) -> counts
        g. mark_parsed(raw_file_id) -> PARSED
     Append (created, updated, unchanged) to running totals.
  7. finish_run(status=SUCCEEDED|PARTIAL|FAILED based on per-report outcomes,
     counts=accumulated, error_summary=None | first failure summary)
  8. Return ConnectorRunOutcome(run, counts, per_report_failures).

Failure handlers A/B/C: task 28.
Dry-run: task 29.
Audit wiring: B2.6 (task 37).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ConnectorRunOutcome:
    run: "ConnectorRunEntry | None"  # None on dry-run
    counts: dict[str, int]
    per_report_failures: list[tuple[str, str]]  # (report_type_id, error_class)


def run_one(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    report_month: str,
    dry_run: bool = False,
    triggered_by_user_id: UUID | None = None,
) -> ConnectorRunOutcome:
    """See module docstring. Returns an immutable outcome."""
    # Implementation in next steps. For T27, only the YT Reporting happy
    # path is wired; failure handlers and dry-run come in T28/T29.
    raise NotImplementedError
```

- [ ] **Step 2: Write the happy-path test (large fixture)**

`tests/connectors/google/test_orchestrator.py`:
```python
"""run_one orchestrator tests (B2.4 happy path)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# (Set up all relevant metadata families so the in-memory DB has all the
# tables this end-to-end fixture exercises.)
from ums_smart_revenue.db.bases import (
    FinanceBase, OrgBase, ReportBase, SecurityBase, TenantBase,
)
# Import models so they register on the bases.
from ums_smart_revenue.db import connector_models, report_models, security_models, source_models  # noqa: F401

from ums_smart_revenue.connectors.runs.orchestrator import (
    ConnectorRunOutcome,
    run_one,
)


@pytest.fixture
def session(tmp_path):
    eng = create_engine("sqlite:///:memory:")
    for base in (SecurityBase, OrgBase, FinanceBase, ReportBase, TenantBase):
        base.metadata.create_all(eng)
    with Session(eng) as session:
        yield session


@pytest.fixture
def stub_secret_resolver():
    """Register a local-secret:// resolver for the orchestrator's secret_resolve call."""
    from ums_smart_revenue.connectors.google import (
        local_secret_resolver,
        secret_resolver,
    )
    mapping = {
        "yt-creds": json.dumps({
            "refresh_token": "rt", "client_id": "cid", "client_secret": "cs",
            "token_uri": "https://oauth2.googleapis.com/token",
        }),
    }
    secret_resolver.register_resolver(
        scheme="local-secret",
        resolver=local_secret_resolver.LocalSecretResolver(mapping=mapping),
    )
    yield
    secret_resolver._REGISTRY.clear()


def _make_credential_row(session, *, tenant_id, connector_key, account_id):
    """Insert an ApiConnectorCredentialORM row pointing at local-secret://yt-creds."""
    from ums_smart_revenue.connectors.credentials import ApiConnectorCredentialORM

    row = ApiConnectorCredentialORM(
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
        encrypted_secret_ref="local-secret://yt-creds",
        status="active",
    )
    session.add(row)
    session.flush()
    return row


def test_run_one_happy_path_writes_run_raw_file_and_source_rows(
    session, stub_secret_resolver, tmp_path
) -> None:
    tenant_id = uuid4()
    _make_credential_row(
        session, tenant_id=tenant_id,
        connector_key="youtube-reporting", account_id="acct-1",
    )
    # Stub the YT Reporting client to return one report with a payload the
    # YouTubeReportingParser can consume.
    fake_csv = b"day,channel_id,ad_revenue\n2026-05-01,UCxxx,1.23\n"

    with patch(
        "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
    ) as YTClientCls, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"
    ) as LocalCls:
        client = YTClientCls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [
            {"id": "r1", "downloadUrl": "https://yt/r1"}
        ]
        client.fetch_report.return_value = fake_csv

        backend = LocalCls.return_value
        # Simulate file-store round-trip
        store: dict[str, bytes] = {}
        def fake_upload(*, storage_uri, content):
            store[storage_uri] = content
        def fake_get(*, storage_uri):
            return store[storage_uri]
        backend.upload.side_effect = fake_upload
        backend.get_bytes.side_effect = fake_get

        outcome = run_one(
            session, tenant_id=tenant_id,
            connector_key="youtube-reporting", account_id="acct-1",
            report_month="2026-05",
        )

    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert outcome.counts["reports_attempted"] == 1
    assert outcome.counts["reports_succeeded"] == 1
    assert outcome.counts["rows_upserted_total"] >= 1
```

(The fixture above stubs the YT client and storage backend; real B2.4 wiring loads these from a small factory layer inside `orchestrator.py`. The factory is what task 27's `run_one` implementation actually calls.)

- [ ] **Step 3: Implement orchestrator (extensive)**

This is the largest single file in B2. The implementation glues together:
- `resolve_secret` (B2.1)
- `build_credentials_from_payload` (B2.1)
- `GoogleHttpClient` (B2.4)
- `YouTubeReportingClient` (B2.4) — wrapped in a small per-connector adapter
- `upload_and_verify` (B2.2)
- `RawReportFileORM` (existing)
- `mark_parsed` / `mark_failed` (B2.2)
- `start_run` / `finish_run` / `link_raw_file` (B2.3)
- `YouTubeReportingParser` (existing PR #43)
- `SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many` (existing PR #43)

A practical pattern: implement a `ConnectorRunner` Protocol with a single method `produce_reports(session, run, credentials) -> Iterable[_ReportProducer]` where `_ReportProducer` yields `(report_type, parser_payload, raw_bytes_for_blob)`. The orchestrator handles blob, raw_file, parser, source-row upsert, and lifecycle uniformly across YT Reporting / YT Analytics / AdSense. B2.4 registers `YouTubeReportingRunner`; B2.5/B2.6 add the other two.

Outline the orchestrator body (full implementation runs ~250-300 lines; the engineer should write it inside `run_one()` plus a `YouTubeReportingRunner` class in the same file):
```python
def run_one(...) -> ConnectorRunOutcome:
    # Bucket A: pre-start_run errors (no run row, no audit).
    credential = _load_credential(session, tenant_id, connector_key, account_id)
    if credential is None:
        raise CredentialNotFoundError(connector_key=connector_key, account_id=account_id)
    if credential.status != "active":
        raise InactiveCredentialError(
            credential_id=str(credential.id), status=credential.status,
        )
    payload = resolve_secret(credential.encrypted_secret_ref)
    credentials = build_credentials_from_payload(payload)
    refresh_credentials(credentials)  # initial token build; OAuthRefreshError -> bucket A

    if dry_run:
        # Task 29 handles this branch.
        raise NotImplementedError("dry_run handled in task 29")

    # Bucket B/C: post-start_run.
    run_entry = start_run(
        session, tenant_id=tenant_id,
        connector_key=connector_key, account_id=account_id,
        report_month=report_month, triggered_by_user_id=triggered_by_user_id,
    )
    session.commit()  # Forensic durability for the started_at marker.

    runner = dispatch_connector(key=connector_key)
    counts = _zero_counts()
    per_report_failures: list[tuple[str, str]] = []
    ordering_index = 0
    try:
        for report_type, parser_payload, raw_bytes in runner.produce_reports(
            session=session, run=run_entry, credentials=credentials,
            report_month=report_month, account_id=account_id,
        ):
            counts["reports_attempted"] += 1
            try:
                # Compute checksum, build deterministic path, upload + verify.
                # Then register RawReportFileORM (DOWNLOADED), link to run,
                # parse, upsert source rows, mark PARSED.
                ...
                counts["reports_succeeded"] += 1
            except GoogleConnectorError as exc:
                per_report_failures.append((report_type, type(exc).__name__))
                counts["reports_failed"] += 1
            ordering_index += 1
    except GoogleConnectorError as exc:
        # Bucket C: terminal.
        finish_run(
            session, tenant_id=tenant_id, connector_run_id=run_entry.id,
            status="FAILED", counts=counts,
            error_summary=f"{type(exc).__name__}: {exc!s}",
        )
        session.commit()
        raise

    # Bucket B aggregate finish.
    status = (
        "SUCCEEDED" if counts["reports_failed"] == 0 and counts["reports_succeeded"] > 0
        else "FAILED" if counts["reports_succeeded"] == 0
        else "PARTIAL"
    )
    finished = finish_run(
        session, tenant_id=tenant_id, connector_run_id=run_entry.id,
        status=status, counts=counts,
        error_summary=None if not per_report_failures else _summarize(per_report_failures),
    )
    session.commit()
    return ConnectorRunOutcome(run=finished, counts=counts, per_report_failures=per_report_failures)
```

Implementation tasks the engineer must complete in this commit:
1. Write a `YouTubeReportingRunner` class implementing `produce_reports`. It instantiates a `YouTubeReportingClient`, walks `list_supported_jobs` + `list_reports_for_month`, calls `fetch_report` for each, parses the CSV into the parser's expected payload shape (use the existing parser-friendly JSON conversion in `connectors/google_source_parsers/`), and yields `(report_type_id, parser_payload, raw_bytes_for_blob)`.
2. Register `YouTubeReportingRunner` via `register_connector(key="youtube-reporting", runner=YouTubeReportingRunner)` at module import time (or via a `register_all()` called from the CLI bootstrap).
3. Implement the inner blob → raw_file → parse → upsert → mark_parsed sequence inside the for-loop above. Use `LocalFileStoreBackend` or `GcsBlobStorageBackend` based on a config env var `UMS_BLOB_BACKEND` (defaults to `file-store`).

- [ ] **Step 4: Run test, lint, commit**

```bash
python -m pytest -q tests/connectors/google/test_orchestrator.py
python -m ruff check backend/ums_smart_revenue/connectors/runs/orchestrator.py \
                     tests/connectors/google/test_orchestrator.py
git add backend/ums_smart_revenue/connectors/runs/orchestrator.py \
        tests/connectors/google/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(b2.4): add run_one orchestrator - happy path

Wires the full B2 ingestion pipeline for YouTube Reporting:
load credential -> resolve secret -> build OAuth (bucket A errors)
-> start_run + commit (forensic) -> for each report (list reports,
fetch, blob upload+verify, register raw_file DOWNLOADED, link, parse,
upsert source rows, mark PARSED) -> finish_run (SUCCEEDED).

Uses a small ConnectorRunner Protocol so B2.5/B2.6 add their clients
as additional registered runners without changing run_one itself.
Failure handlers (B/C) in next commit; dry-run in T29.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 28: run_one orchestrator — failure handlers A/B/C

Spec §6. Three handlers:
- A (pre-`start_run`): bubble to caller; no DB writes; no audit.
- B (per-report, inside for-loop): mark raw_file FAILED if in scope; append to failures; continue. Final status = `SUCCEEDED|PARTIAL|FAILED` at `finish_run`.
- C (post-`start_run`, escaped the for-loop): mark in-flight raw_file FAILED (if known); mark run FAILED; re-raise.

- [ ] **Step 1: Write failing tests**

Append to `tests/connectors/google/test_orchestrator.py`:
```python
from ums_smart_revenue.connectors.google.errors import (
    CredentialNotFoundError, InactiveCredentialError, OAuthRefreshError,
    GoogleApiServerError,
)


def test_bucket_a_no_credential_raises_and_no_run_row(
    session, stub_secret_resolver
) -> None:
    tenant_id = uuid4()
    with pytest.raises(CredentialNotFoundError):
        run_one(
            session, tenant_id=tenant_id,
            connector_key="youtube-reporting", account_id="missing",
            report_month="2026-05",
        )
    # No connector_runs row should exist.
    from ums_smart_revenue.db.connector_models import ConnectorRunORM
    assert session.query(ConnectorRunORM).count() == 0


def test_bucket_a_inactive_credential_raises(session, stub_secret_resolver) -> None:
    tenant_id = uuid4()
    cred = _make_credential_row(
        session, tenant_id=tenant_id,
        connector_key="youtube-reporting", account_id="acct-1",
    )
    cred.status = "disabled"
    session.flush()
    with pytest.raises(InactiveCredentialError):
        run_one(
            session, tenant_id=tenant_id,
            connector_key="youtube-reporting", account_id="acct-1",
            report_month="2026-05",
        )


def test_bucket_b_one_report_fails_finishes_partial(
    session, stub_secret_resolver
) -> None:
    # Stub YT client: list returns 2 reports; fetch_report fails for #2.
    # Expected: counts.reports_succeeded=1, reports_failed=1, run.status=PARTIAL,
    # raw_file_2.parse_status=FAILED.
    tenant_id = uuid4()
    _make_credential_row(
        session, tenant_id=tenant_id,
        connector_key="youtube-reporting", account_id="acct-1",
    )
    # ... use patch() to stub YT client returning 2 reports, second fetch raises
    # GoogleApiServerError. Assert outcome.run.status == "PARTIAL" and
    # outcome.counts["reports_failed"] == 1.


def test_bucket_c_terminal_oauth_refresh_marks_run_failed(
    session, stub_secret_resolver
) -> None:
    # Mid-run OAuthRefreshError after one successful report should:
    # 1. Mark the in-flight raw_file FAILED (none in flight at this exact
    #    point if refresh happens between reports - test the case where
    #    refresh fires inside fetch_report by patching).
    # 2. Mark run FAILED and re-raise.
    ...
```

- [ ] **Step 2: Wire handlers in orchestrator**

Update `run_one()` per the outline in T27 step 3 to:
- Wrap the credential lookup + secret resolve + initial OAuth refresh in handler A (no try/except — just let the typed errors bubble).
- Wrap the per-report inner block in `try: ... except GoogleConnectorError as exc: per_report_failures.append((report_type, exc.__class__.__name__)); counts["reports_failed"] += 1; if raw_file_id is not None: mark_failed(...); continue`.
- Wrap the for-loop in an outer `try: ... except Exception as exc:` that calls `finish_run(status="FAILED", error_summary=str(exc))` and re-raises. Convert non-`GoogleConnectorError` exceptions before commit.

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_orchestrator.py
git add backend/ums_smart_revenue/connectors/runs/orchestrator.py \
        tests/connectors/google/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(b2.4): wire failure handlers A/B/C into run_one

- Bucket A: pre-start_run errors (no credential, inactive credential,
  malformed secret, OAuthRefreshError on initial build) bubble to caller
  with no connector_runs row written.
- Bucket B: per-report errors mark the raw_file FAILED (if registered)
  and continue. Aggregate status at finish_run: SUCCEEDED if all parsed,
  PARTIAL if some succeeded and some failed, FAILED if all failed.
- Bucket C: any GoogleConnectorError escaping the per-report try/except
  (e.g., mid-run OAuth refresh failure) marks the in-flight raw_file
  FAILED (if known), marks the run FAILED, and re-raises.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 29: run_one orchestrator — dry-run

Spec §5.4: dry-run writes **nothing** (no `connector_runs` row, no raw_file row, no blob upload, no source-row upsert, no audit). Returns `ConnectorRunOutcome(run=None, counts=...)` with the counts that *would* have been written.

- [ ] **Step 1: Test**

```python
def test_dry_run_writes_nothing_returns_outcome_with_run_none(
    session, stub_secret_resolver
) -> None:
    tenant_id = uuid4()
    _make_credential_row(
        session, tenant_id=tenant_id,
        connector_key="youtube-reporting", account_id="acct-1",
    )
    # Stub YT client to return 2 fetchable reports.
    with patch("...YouTubeReportingClient") as YTClientCls, \
         patch("...LocalFileStoreBackend") as LocalCls:
        client = YTClientCls.return_value
        client.list_supported_jobs.return_value = [{"id": "j", "reportTypeId": "channel_basic_a2"}]
        client.list_reports_for_month.return_value = [
            {"id": "r1", "downloadUrl": "u1"}, {"id": "r2", "downloadUrl": "u2"},
        ]
        client.fetch_report.return_value = b"day,c\n2026-05-01,UC\n"

        outcome = run_one(
            session, tenant_id=tenant_id,
            connector_key="youtube-reporting", account_id="acct-1",
            report_month="2026-05", dry_run=True,
        )
    assert outcome.run is None
    assert outcome.counts["reports_attempted"] == 2
    # No DB writes:
    from ums_smart_revenue.db.connector_models import ConnectorRunORM
    from ums_smart_revenue.db.report_models import RawReportFileORM
    assert session.query(ConnectorRunORM).count() == 0
    assert session.query(RawReportFileORM).count() == 0
```

- [ ] **Step 2: Implement dry-run branch**

Inside `run_one()`:
```python
if dry_run:
    counts = _zero_counts()
    runner = dispatch_connector(key=connector_key)
    # Use an in-memory transient "session" or short-circuit: call the
    # runner's produce_reports but DON'T flush any inserts, DON'T upload
    # blobs (or upload to a discard backend), DON'T parse beyond counting.
    for report_type, parser_payload, raw_bytes in runner.produce_reports(
        session=session, run=None, credentials=credentials,
        report_month=report_month, account_id=account_id,
    ):
        counts["reports_attempted"] += 1
        # Validate by parsing (no DB write):
        try:
            rows = list(_parse(parser_payload, tenant_id=tenant_id))
            counts["reports_succeeded"] += 1
            counts["rows_upserted_total"] += len(rows)
            # rows_upserted_{created,updated,unchanged} stay 0 in dry-run
        except Exception:
            counts["reports_failed"] += 1
    return ConnectorRunOutcome(run=None, counts=counts, per_report_failures=[])
```

The exact "DON'T flush" guarantee comes from using a SAVEPOINT or by simply not calling `session.commit()` and rolling back at the end. A clean implementation uses `with session.begin_nested(): ... session.rollback()` so any inserts inside `produce_reports` (which there shouldn't be in a well-designed runner) are reverted.

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_orchestrator.py
git add backend/ums_smart_revenue/connectors/runs/orchestrator.py \
        tests/connectors/google/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(b2.4): add dry-run mode to run_one

Dry-run writes nothing: no connector_runs row, no raw_file row, no blob
upload, no source-row upsert, no audit. Returns ConnectorRunOutcome
with run=None and counts that report what would have been written. Uses
session.begin_nested() + rollback to guarantee zero state changes if the
runner accidentally tries to insert.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 30: CLI entrypoint

**Files:**
- Create: `scripts/run_google_connector.py`
- Create: `tests/connectors/google/test_run_one_cli.py`

- [ ] **Step 1: Write the CLI test**

`tests/connectors/google/test_run_one_cli.py`:
```python
"""CLI argparse + dispatch tests."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/run_google_connector.py", *args],
        capture_output=True, text=True,
    )


def test_cli_rejects_unknown_connector() -> None:
    out = _run([
        "--tenant", str(uuid4()), "--connector", "not-a-thing",
        "--account", "x", "--month", "2026-05",
    ])
    assert out.returncode != 0
    assert "not-a-thing" in out.stderr or "invalid choice" in out.stderr


def test_cli_rejects_bad_month_format() -> None:
    out = _run([
        "--tenant", str(uuid4()), "--connector", "youtube-reporting",
        "--account", "x", "--month", "2026-5",  # wrong format
    ])
    assert out.returncode != 0


def test_cli_dry_run_exits_zero_when_credential_missing(monkeypatch, tmp_path) -> None:
    # End-to-end CLI smoke; dry-run with no credential should hit
    # CredentialNotFoundError and exit non-zero with the class name on stderr.
    out = _run([
        "--tenant", str(uuid4()), "--connector", "youtube-reporting",
        "--account", "missing", "--month", "2026-05", "--dry-run",
    ])
    assert out.returncode != 0
    assert "CredentialNotFoundError" in out.stderr
```

- [ ] **Step 2: Implement CLI**

`scripts/run_google_connector.py`:
```python
#!/usr/bin/env python
"""CLI entrypoint for the B2 live Google connector.

Usage:
    python scripts/run_google_connector.py \
        --tenant <UUID> \
        --connector {youtube-reporting | youtube-analytics | adsense-management} \
        --account <account-id> \
        --month <YYYY-MM> \
        [--dry-run]

Loads config from the standard UMS settings, registers all known connector
runners, opens a SQLAlchemy session, calls run_one(...), prints a one-line
summary to stdout. Non-zero exit code if the run finished FAILED or
PARTIAL, or if a bucket A error fired pre-start_run.
"""
from __future__ import annotations

import argparse
import re
import sys
from uuid import UUID

from ums_smart_revenue.config.settings import get_settings
from ums_smart_revenue.connectors.google import registry  # noqa: F401 - registers runners on import
from ums_smart_revenue.connectors.google.errors import GoogleConnectorError
from ums_smart_revenue.connectors.runs.orchestrator import run_one
from ums_smart_revenue.db.session import build_session_factory


_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the B2 live Google connector.")
    parser.add_argument("--tenant", required=True, type=UUID)
    parser.add_argument(
        "--connector", required=True,
        choices=sorted(registry.known_keys()),
    )
    parser.add_argument("--account", required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not _MONTH_PATTERN.match(args.month):
        parser.error(f"--month must be YYYY-MM, got {args.month!r}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    settings = get_settings()
    session_factory = build_session_factory(settings)
    with session_factory() as session:
        try:
            outcome = run_one(
                session,
                tenant_id=args.tenant,
                connector_key=args.connector,
                account_id=args.account,
                report_month=args.month,
                dry_run=args.dry_run,
            )
        except GoogleConnectorError as exc:
            print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2
    if outcome.run is None:
        print(f"DRY-RUN counts={outcome.counts}")
        return 0
    print(
        f"{outcome.run.status} counts={outcome.counts} "
        f"failures={outcome.per_report_failures}"
    )
    return 0 if outcome.run.status == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_run_one_cli.py
python -m ruff check scripts/run_google_connector.py \
                     tests/connectors/google/test_run_one_cli.py
git add scripts/run_google_connector.py \
        tests/connectors/google/test_run_one_cli.py
git commit -m "$(cat <<'EOF'
feat(b2.4): add scripts/run_google_connector.py CLI

Public operator surface:
    --tenant <UUID> --connector {youtube-reporting,...} --account <id>
    --month YYYY-MM [--dry-run]

Argparse rejects unknown connectors (registry.known_keys() drives --connector
choices), invalid UUIDs, and malformed --month. Bucket A errors print
"<ClassName>: <message>" on stderr with exit 2. Bucket B PARTIAL/FAILED
exits 1; SUCCEEDED and DRY-RUN exit 0.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 31: B2.4 validation gate + plan/backlog markers

- [ ] **Step 1: Run gate**

```bash
python -m ruff check backend tests scripts
python -m pytest -q tests/connectors/google/test_http_client.py \
                    tests/connectors/google/test_youtube_reporting_client.py \
                    tests/connectors/google/test_registry.py \
                    tests/connectors/google/test_orchestrator.py \
                    tests/connectors/google/test_run_one_cli.py
python scripts/run_validation_gate.py
git diff --check
```

- [ ] **Step 2: Update docs + commit**

Append to plan/backlog:
```markdown
- ⏳ PR #N (B2.4) — google-auth + httpx base client + retry policy +
  YouTube Reporting client + report_type whitelist + run_one() orchestrator
  + scripts/run_google_connector.py CLI with extensible --connector registry.
```

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "$(cat <<'EOF'
docs(b2.4): mark HTTP base + YT Reporting + orchestrator + CLI as in-progress

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## PR B2.5 — YouTube Analytics (targeted channel ingestion)

Spec reference: §5.5, §10.1 blast-radius row B2.5.

### Task 32: YouTubeAnalyticsClient + list_target_channels

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/youtube_analytics_client.py`
- Create: `tests/connectors/google/test_youtube_analytics_client.py`

- [ ] **Step 1: Write tests**

`tests/connectors/google/test_youtube_analytics_client.py`:
```python
"""YouTube Analytics targeted channel ingestion tests (spec §5.5)."""
from __future__ import annotations

import httpx
import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.youtube_analytics_client import (
    YouTubeAnalyticsClient,
    list_target_channels,
)
from ums_smart_revenue.db.bases import OrgBase
from ums_smart_revenue.db.org_models import YouTubeChannelORM


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:")
    OrgBase.metadata.create_all(eng)
    with Session(eng) as session:
        yield session


def _insert_channel(session, *, tenant_id, youtube_channel_id, content_owner_id, active=True, revenue_required=True):
    ch = YouTubeChannelORM(
        tenant_id=tenant_id,
        youtube_channel_id=youtube_channel_id,
        channel_name=youtube_channel_id,
        content_owner_id=content_owner_id,
        active=active,
        revenue_required=revenue_required,
    )
    session.add(ch)
    session.flush()


def test_list_target_channels_includes_cms_match_and_outside_cms(session) -> None:
    tenant_id = uuid4()
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-1", content_owner_id="owner-a")
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-2", content_owner_id=None)  # outside-CMS
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-3", content_owner_id="owner-b")  # other CMS
    channels = list_target_channels(session, tenant_id=tenant_id, account_id="owner-a")
    assert channels == ["UC-1", "UC-2"]  # deterministic ascending


def test_list_target_channels_excludes_inactive_and_no_revenue(session) -> None:
    tenant_id = uuid4()
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-1", content_owner_id="o", active=False)
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-2", content_owner_id="o", revenue_required=False)
    _insert_channel(session, tenant_id=tenant_id, youtube_channel_id="UC-3", content_owner_id="o")
    channels = list_target_channels(session, tenant_id=tenant_id, account_id="o")
    assert channels == ["UC-3"]


def test_fetch_channel_report(mock_credentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        assert params["ids"] == "channel==UC-xyz"
        assert params["startDate"] == "2026-05-01"
        assert params["endDate"] == "2026-05-31"
        return httpx.Response(200, content=json.dumps({"rows": [["2026-05", "USD", "1.23"]]}).encode())
    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeAnalyticsClient(http=http)
    body = client.fetch_channel_report(channel_id="UC-xyz", report_month="2026-05")
    assert body == {"rows": [["2026-05", "USD", "1.23"]]}
```

- [ ] **Step 2: Implement**

`backend/ums_smart_revenue/connectors/google/youtube_analytics_client.py`:
```python
"""YouTube Analytics v2 reports.query targeted channel ingestion (spec §5.5).

Endpoint: GET https://youtubeanalytics.googleapis.com/v2/reports
Query params:
  ids=channel==<youtube_channel_id>
  startDate=<YYYY-MM-01>
  endDate=<YYYY-MM-last>
  metrics=estimatedRevenue,...   <- locked per parser requirements
  dimensions=month,...

Channels are sourced from the youtube_channels registry (PR #25) filtered
by tenant + active + revenue_required + content_owner match-or-null.
"""
from __future__ import annotations

from calendar import monthrange
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.db.org_models import YouTubeChannelORM

_BASE = "https://youtubeanalytics.googleapis.com/v2/reports"
# Locked metric set; matches what YouTubeAnalyticsParser consumes.
_METRICS = "estimatedRevenue,estimatedAdRevenue,grossRevenue"
_DIMENSIONS = "month"


def list_target_channels(
    session: Session, *, tenant_id: UUID, account_id: str,
) -> list[str]:
    stmt = (
        select(YouTubeChannelORM.youtube_channel_id)
        .where(
            YouTubeChannelORM.tenant_id == tenant_id,
            YouTubeChannelORM.active.is_(True),
            YouTubeChannelORM.revenue_required.is_(True),
            (
                (YouTubeChannelORM.content_owner_id == account_id)
                | (YouTubeChannelORM.content_owner_id.is_(None))
            ),
        )
        .order_by(YouTubeChannelORM.youtube_channel_id.asc())
    )
    return [row[0] for row in session.execute(stmt).all()]


class YouTubeAnalyticsClient:
    def __init__(self, *, http: GoogleHttpClient) -> None:
        self._http = http

    def fetch_channel_report(
        self, *, channel_id: str, report_month: str,
    ) -> dict:
        year, month = report_month.split("-")
        year_i, month_i = int(year), int(month)
        last_day = monthrange(year_i, month_i)[1]
        params = {
            "ids": f"channel=={channel_id}",
            "startDate": f"{year}-{month}-01",
            "endDate": f"{year}-{month}-{last_day:02d}",
            "metrics": _METRICS,
            "dimensions": _DIMENSIONS,
        }
        return self._http.request(method="GET", url=_BASE, params=params)
```

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_youtube_analytics_client.py
git add backend/ums_smart_revenue/connectors/google/youtube_analytics_client.py \
        tests/connectors/google/test_youtube_analytics_client.py
git commit -m "$(cat <<'EOF'
feat(b2.5): add YouTubeAnalyticsClient targeted channel ingestion

list_target_channels(session, tenant_id, account_id) queries the
youtube_channels registry (PR #25) and returns active + revenue_required
channels whose content_owner_id matches the account OR is NULL
(outside-CMS channels are always included for the tenant). Deterministic
ascending order.

YouTubeAnalyticsClient.fetch_channel_report performs a single
reports.query GET per channel with monthly date bounds; the response is
the parser-ready payload for YouTubeAnalyticsParser.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 33: Register youtube-analytics + orchestrator extension

- [ ] **Step 1: Write the runner**

In `backend/ums_smart_revenue/connectors/runs/orchestrator.py`, add a new `YouTubeAnalyticsRunner` class implementing the same `ConnectorRunner` Protocol used by `YouTubeReportingRunner`. Its `produce_reports` calls `list_target_channels(...)`, iterates channels, calls `fetch_channel_report` per channel, wraps the response into the parser payload, yields `("youtube_analytics", parser_payload, raw_bytes)` per channel.

Register at module import:
```python
register_connector(key="youtube-analytics", runner=YouTubeAnalyticsRunner)
```

- [ ] **Step 2: Test the orchestrator with youtube-analytics**

Append to `tests/connectors/google/test_orchestrator.py`:
```python
def test_run_one_with_youtube_analytics_succeeds_per_channel(
    session, stub_secret_resolver
) -> None:
    tenant_id = uuid4()
    _make_credential_row(
        session, tenant_id=tenant_id,
        connector_key="youtube-analytics", account_id="owner-a",
    )
    # Insert 2 active+revenue-required channels: one CMS-owned, one outside-CMS.
    # Stub YouTubeAnalyticsClient.fetch_channel_report to return a parser-ready
    # payload for each. Assert outcome.run.status == "SUCCEEDED" and
    # outcome.counts["reports_succeeded"] == 2.
    ...
```

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_youtube_analytics_client.py \
                    tests/connectors/google/test_orchestrator.py
git add backend/ums_smart_revenue/connectors/runs/orchestrator.py \
        tests/connectors/google/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(b2.5): register youtube-analytics runner; extend orchestrator

YouTubeAnalyticsRunner.produce_reports iterates list_target_channels
(active + revenue_required + CMS-match-or-outside) and yields one
ParserReady payload per channel for YouTubeAnalyticsParser. Registered
in the connector registry as 'youtube-analytics'. Orchestrator's run_one
needs no signature change - dispatch is registry-driven.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 34: B2.5 validation gate + plan/backlog markers

- [ ] **Step 1: Run gate**

```bash
python -m ruff check backend tests
python -m pytest -q tests/connectors/google/test_youtube_analytics_client.py \
                    tests/connectors/google/test_orchestrator.py
python scripts/run_validation_gate.py
git diff --check
```

- [ ] **Step 2: Doc + commit**

Append to plan/backlog:
```markdown
- ⏳ PR #N (B2.5) — YouTube Analytics targeted channel ingestion
  (includes outside-CMS channels from youtube_channels registry); extends
  the B2.4 CLI's --connector registry with youtube-analytics.
```

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "$(cat <<'EOF'
docs(b2.5): mark YouTube Analytics ingestion as in-progress

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## PR B2.6 — AdSense Management + audit + mock ingestion gate

Spec reference: §5.6, §8 (audit wiring), §9.3 B2.6, §10.1 blast radius B2.6.

### Task 35: AdSenseManagementClient + response adapter

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/adsense_management_client.py`
- Create: `tests/connectors/google/test_adsense_management_client.py`

- [ ] **Step 1: Write tests**

```python
"""AdSense client + adapter tests (spec §5.6)."""
from __future__ import annotations

import httpx
import json

import pytest

from ums_smart_revenue.connectors.google.adsense_management_client import (
    AdSenseManagementClient,
    adsense_response_to_parser_payload,
)
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient


def test_fetch_monthly_report_pins_currency_usd_and_date_bounds(mock_credentials) -> None:
    captured = {}
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["q"] = dict(request.url.params)
        return httpx.Response(200, content=json.dumps({"request": {"accountId": "accounts/pub-1"}, "headers": [], "rows": []}).encode())
    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = AdSenseManagementClient(http=http)
    out = client.fetch_monthly_report(account_id="pub-1", report_month="2026-05")
    assert "accounts/pub-1/reports:generate" in captured["url"]
    q = captured["q"]
    assert q["dateRange"] == "CUSTOM"
    assert q["startDate.year"] == "2026"
    assert q["startDate.month"] == "5"
    assert q["startDate.day"] == "1"
    assert q["endDate.year"] == "2026"
    assert q["endDate.month"] == "5"
    assert q["endDate.day"] == "31"
    assert q["metrics"] == "ESTIMATED_EARNINGS,PAID_AMOUNT"
    assert q["dimensions"] == "MONTH"
    assert q["currencyCode"] == "USD"
    assert "report_id" in out


def test_adapter_wraps_response_with_deterministic_report_id() -> None:
    response = {
        "request": {"accountId": "accounts/pub-1", "currencyCode": "USD"},
        "headers": [{"type": "DIMENSION", "name": "MONTH"}],
        "rows": [],
    }
    payload = adsense_response_to_parser_payload(
        response_json=response, account_id="pub-1", report_month="2026-05",
    )
    assert payload["report_id"]
    # Determinism: same input -> same report_id.
    payload2 = adsense_response_to_parser_payload(
        response_json=response, account_id="pub-1", report_month="2026-05",
    )
    assert payload["report_id"] == payload2["report_id"]
```

- [ ] **Step 2: Implement**

```python
"""AdSense Management v2 reports.generate client + parser adapter.

Endpoint: GET https://adsense.googleapis.com/v2/accounts/{account}/reports:generate
Query params (locked):
    dateRange=CUSTOM
    startDate.{year,month,day}=...
    endDate.{year,month,day}=...
    dimensions=MONTH
    metrics=ESTIMATED_EARNINGS,PAID_AMOUNT
    currencyCode=USD   <- C1 is USD-only; non-USD handling lives in C1's
                          NON_USD_CURRENCY skip path, not here.

AdSenseManagementParser requires a 'report_id' on the payload (parser
contract from PR #43). AdSense reports.generate does not return a stable
report id, so the adapter computes a deterministic SHA-256 of
(account_id, report_month, locked-query-key) and stamps it on the wrapped
payload.

AdSense in B2 is ingestion/audit evidence only. C1 skips AdSense rows as
SkipReason.MISSING_CHANNEL_ID until a future allocation/mapping spec.
"""
from __future__ import annotations

import hashlib
from calendar import monthrange

from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient

SUPPORTED_ADSENSE_REPORTS: frozenset[str] = frozenset({"monthly_account_earnings"})


def adsense_response_to_parser_payload(
    *, response_json: dict, account_id: str, report_month: str,
) -> dict[str, object]:
    report_id = hashlib.sha256(
        f"{account_id}|{report_month}|monthly_account_earnings".encode()
    ).hexdigest()
    return {
        "request": response_json.get("request", {}),
        "headers": response_json.get("headers", []),
        "rows": response_json.get("rows"),  # parser tolerates None
        "report_id": report_id,
    }


class AdSenseManagementClient:
    def __init__(self, *, http: GoogleHttpClient) -> None:
        self._http = http

    def fetch_monthly_report(
        self, *, account_id: str, report_month: str,
    ) -> dict[str, object]:
        year, month = report_month.split("-")
        year_i, month_i = int(year), int(month)
        last_day = monthrange(year_i, month_i)[1]
        url = (
            f"https://adsense.googleapis.com/v2/accounts/{account_id}"
            "/reports:generate"
        )
        params = {
            "dateRange": "CUSTOM",
            "startDate.year": year,
            "startDate.month": str(month_i),
            "startDate.day": "1",
            "endDate.year": year,
            "endDate.month": str(month_i),
            "endDate.day": str(last_day),
            "dimensions": "MONTH",
            "metrics": "ESTIMATED_EARNINGS,PAID_AMOUNT",
            "currencyCode": "USD",
        }
        response = self._http.request(method="GET", url=url, params=params)
        return adsense_response_to_parser_payload(
            response_json=response, account_id=account_id, report_month=report_month,
        )
```

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_adsense_management_client.py
git add backend/ums_smart_revenue/connectors/google/adsense_management_client.py \
        tests/connectors/google/test_adsense_management_client.py
git commit -m "$(cat <<'EOF'
feat(b2.6): add AdSenseManagementClient + parser adapter

AdSenseManagementClient.fetch_monthly_report calls accounts.reports.generate
with locked query params (dateRange=CUSTOM, dimensions=MONTH,
metrics=ESTIMATED_EARNINGS,PAID_AMOUNT, currencyCode=USD).
adsense_response_to_parser_payload wraps the response and stamps a
deterministic SHA-256 report_id (required by AdSenseManagementParser
because the API doesn't return a stable report id).

AdSense remains ingestion/audit evidence only; C1 skips rows as
MISSING_CHANNEL_ID per the existing parser contract.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 36: Connector audit emitters

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/audit.py`
- Create: `tests/connectors/google/test_audit_wiring.py`

- [ ] **Step 1: Write tests**

```python
"""Connector audit wiring tests (spec §8)."""
from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from ums_smart_revenue.connectors.google.audit import (
    build_connector_service_principal,
    emit_raw_file_downloaded,
    emit_raw_file_failed,
    emit_raw_file_parsed,
    emit_run_finished,
    emit_run_started,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.permissions import Permission


def test_build_service_principal_carries_run_connector_jobs() -> None:
    tenant_id = uuid4()
    principal = build_connector_service_principal(tenant_id=tenant_id)
    assert principal.tenant_id == tenant_id
    assert Permission.RUN_CONNECTOR_JOBS in principal.permissions


def test_emit_run_started_uses_connector_job_run_with_started_lifecycle() -> None:
    session = MagicMock()
    principal = MagicMock()
    run = MagicMock(id=uuid4(), connector_key="youtube-reporting",
                    account_id="a", report_month="2026-05")

    with pytest.MonkeyPatch().context() as m:
        recorded = []
        m.setattr(
            "ums_smart_revenue.connectors.google.audit.record_audit_event",
            lambda **kw: recorded.append(kw),
        )
        emit_run_started(session=session, principal=principal, run=run, dry_run=False)
    assert len(recorded) == 1
    call = recorded[0]
    assert call["event_type"] == AuditEventType.CONNECTOR_JOB_RUN
    assert call["payload"]["lifecycle"] == "STARTED"
    assert call["payload"]["dry_run"] is False
```

(Tests for finish_run lifecycle, downloaded/parsed/failed raw_file lifecycle, and ensure no secret material leaks into the payload.)

- [ ] **Step 2: Implement**

`backend/ums_smart_revenue/connectors/google/audit.py`:
```python
"""B2.6 audit wiring (spec §8).

Reuses AuditEventType.CONNECTOR_JOB_RUN (for run lifecycle) and
AuditEventType.REPORT_IMPORTED (for raw-file lifecycle). The `lifecycle`
discriminator in the payload distinguishes STARTED/FINISHED for runs
and DOWNLOADED/PARSED/FAILED for raw files. No new enum or permission
values.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.config.settings import get_settings


def build_connector_service_principal(*, tenant_id: UUID) -> UserPrincipal:
    settings = get_settings()
    return UserPrincipal(
        user_id=settings.UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID,
        tenant_id=tenant_id,
        permissions=frozenset({Permission.RUN_CONNECTOR_JOBS}),
        # ... other UserPrincipal fields populated as required by the
        # existing dataclass; mirror auth_service's service-principal pattern.
    )


def emit_run_started(
    *, session: Session, principal: UserPrincipal, run: Any, dry_run: bool,
) -> None:
    if dry_run:
        return  # dry-run emits zero audit events
    record_audit_event(
        session=session,
        principal=principal,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        payload={
            "lifecycle": "STARTED",
            "run_id": str(run.id),
            "connector_key": run.connector_key,
            "account_id": run.account_id,
            "report_month": run.report_month,
            "dry_run": False,
        },
    )


def emit_run_finished(
    *, session: Session, principal: UserPrincipal, run: Any,
) -> None:
    record_audit_event(
        session=session,
        principal=principal,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        payload={
            "lifecycle": "FINISHED",
            "run_id": str(run.id),
            "status": run.status,
            "counts": dict(run.counts),
            "error_summary_present": run.error_summary is not None,
        },
    )


def emit_raw_file_downloaded(
    *, session: Session, principal: UserPrincipal, run: Any, raw_file: Any,
) -> None:
    record_audit_event(
        session=session,
        principal=principal,
        event_type=AuditEventType.REPORT_IMPORTED,
        payload={
            "lifecycle": "DOWNLOADED",
            "run_id": str(run.id),
            "raw_file_id": str(raw_file.id),
            "source": raw_file.source,
            "report_type": raw_file.report_type,
            "report_month": raw_file.report_month,
            "checksum": raw_file.checksum,
            "storage_uri": raw_file.file_url,
        },
    )


def emit_raw_file_parsed(
    *, session: Session, principal: UserPrincipal, run: Any,
    raw_file: Any, count_upserted: int,
) -> None:
    record_audit_event(
        session=session,
        principal=principal,
        event_type=AuditEventType.REPORT_IMPORTED,
        payload={
            "lifecycle": "PARSED",
            "run_id": str(run.id),
            "raw_file_id": str(raw_file.id),
            "count_upserted": count_upserted,
        },
    )


def emit_raw_file_failed(
    *, session: Session, principal: UserPrincipal, run: Any,
    raw_file: Any, error_class: str,
) -> None:
    record_audit_event(
        session=session,
        principal=principal,
        event_type=AuditEventType.REPORT_IMPORTED,
        payload={
            "lifecycle": "FAILED",
            "run_id": str(run.id),
            "raw_file_id": str(raw_file.id),
            "error_class": error_class,
        },
    )
```

If `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` is not yet on the settings dataclass, add it as a UUID-typed env-loaded field in `config/settings.py` with no default (raise on missing). Commit that addition first as `chore(b2.6): add UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID setting`.

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_audit_wiring.py
git add backend/ums_smart_revenue/connectors/google/audit.py \
        tests/connectors/google/test_audit_wiring.py
git commit -m "$(cat <<'EOF'
feat(b2.6): add connector audit emitters + service principal

Reuses AuditEventType.CONNECTOR_JOB_RUN (STARTED|FINISHED) and
REPORT_IMPORTED (DOWNLOADED|PARSED|FAILED) with a 'lifecycle'
discriminator in payload. No new enum or permission values.
build_connector_service_principal carries Permission.RUN_CONNECTOR_JOBS
on a UserPrincipal scoped to the run's tenant. Dry-run emits zero
audit events.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 37: Wire audit emitters into orchestrator

Spec §8.4 transaction semantics:
- `CONNECTOR_JOB_RUN/STARTED` committed with `start_run`.
- Per-raw-file events staged in main transaction.
- `CONNECTOR_JOB_RUN/FINISHED` committed with `finish_run`.

- [ ] **Step 1: Update `run_one()`** in `orchestrator.py`:
- After `start_run` + before `session.commit()`, call `emit_run_started`.
- Inside the per-report try/except: after `mark_parsed`, call `emit_raw_file_parsed`. After `mark_failed` (in the except), call `emit_raw_file_failed`. After RawReportFileORM insert + before parse, call `emit_raw_file_downloaded`.
- After `finish_run` + before final `session.commit()`, call `emit_run_finished`.

- [ ] **Step 2: Update orchestrator tests** to assert the event sequence — append a test that uses a mock `record_audit_event` and asserts the expected ordering for a 2-report run (1 success, 1 fail).

- [ ] **Step 3: Run, commit**

```bash
python -m pytest -q tests/connectors/google/test_orchestrator.py \
                    tests/connectors/google/test_audit_wiring.py
git add backend/ums_smart_revenue/connectors/runs/orchestrator.py \
        tests/connectors/google/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(b2.6): wire audit emitters into run_one orchestrator

CONNECTOR_JOB_RUN/STARTED committed with start_run; per-raw-file
DOWNLOADED/PARSED/FAILED staged in the main transaction;
CONNECTOR_JOB_RUN/FINISHED committed with finish_run. Dry-run emits
zero events (no run, no raw_file).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 38: Register adsense-management runner

- [ ] **Step 1: Add `AdSenseManagementRunner`** in `orchestrator.py` implementing `ConnectorRunner`. Its `produce_reports` calls `AdSenseManagementClient.fetch_monthly_report` once per (account, month) pair (AdSense is account-scoped — one report per run, not per channel) and yields `("adsense_management", parser_payload, raw_bytes)`.

- [ ] **Step 2: `register_connector(key="adsense-management", runner=AdSenseManagementRunner)`** at module import.

- [ ] **Step 3: Append orchestrator test for `connector_key="adsense-management"`** — assert SUCCEEDED run, exactly one DOWNLOADED + PARSED raw_file, source rows present in `google_revenue_source_rows`, and (importantly) AdSense rows would skip in C1. This is verified end-to-end in T39's ingestion gate.

- [ ] **Step 4: Commit**

```bash
git add backend/ums_smart_revenue/connectors/runs/orchestrator.py \
        tests/connectors/google/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(b2.6): register adsense-management runner

AdSenseManagementRunner.produce_reports calls fetch_monthly_report once
per (account, month) - AdSense is account-scoped, not channel-scoped -
and yields a single parser-ready payload per run. Registered as
'adsense-management' in the connector registry.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 39: Mock end-to-end ingestion gate test

**Files:**
- Create: `tests/connectors/runs/test_ingestion_gate.py`

This is the spec §9.3 B2.6 mock end-to-end ingestion gate. Three mock backends (YT Reporting + YT Analytics + AdSense) all run through the orchestrator + CLI on local mocks; the test asserts that:
- All three connectors produce source rows in `google_revenue_source_rows`.
- YT-Reporting + YT-Analytics rows flow through C1 (`GoogleSourceNormalizer.normalize_month`) and produce `MonthlyChannelRevenueFactORM` entries.
- AdSense rows skip in C1 as `SkipReason.MISSING_CHANNEL_ID`.
- Audit log carries the expected event sequence with the right principal.

- [ ] **Step 1: Write the gate test**

```python
"""Mock end-to-end ingestion gate (spec §9.3 B2.6).

Three mock backends (YT Reporting + YT Analytics + AdSense) all run through
run_one(...) on local mocks. Asserts:
- google_revenue_source_rows has rows from all three connectors.
- C1 produces facts for YT-Reporting + YT-Analytics rows.
- AdSense rows skip in C1 as SkipReason.MISSING_CHANNEL_ID.
- audit_log carries the expected event sequence with RUN_CONNECTOR_JOBS principal.
"""
from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.runs.orchestrator import run_one
from ums_smart_revenue.db.bases import (
    ExplanationBase, FinanceBase, OrgBase, ReportBase, SecurityBase, TenantBase,
)
# Register all ORMs.
from ums_smart_revenue.db import (
    connector_models, org_models, report_models, security_models, source_models,
)  # noqa: F401
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
    SkipReason,
)


@pytest.fixture
def session(tmp_path):
    eng = create_engine("sqlite:///:memory:")
    for base in (
        SecurityBase, OrgBase, FinanceBase, ReportBase, ExplanationBase, TenantBase,
    ):
        base.metadata.create_all(eng)
    with Session(eng) as session:
        yield session


def test_three_connectors_end_to_end_on_mocks(session, tmp_path, monkeypatch) -> None:
    tenant_id = uuid4()
    # Insert tenants/users/channels/credentials fixtures (synthetic).
    # Stub all three Google clients via patch() so no live HTTP fires.
    # Run each connector via run_one(...).
    # Run C1.normalize_month and assert:
    #   - YT rows produce revenue facts.
    #   - AdSense rows skip as MISSING_CHANNEL_ID.
    #   - audit_log row count == expected (STARTED + DOWNLOADED + PARSED + FINISHED) x 3 runs.
    ...
```

(This test is the heaviest single test in B2; it ties together every prior task. Expect ~150-200 lines. Carefully synthesize fixture data — synthetic only, per repo rules.)

- [ ] **Step 2: Run, commit**

```bash
python -m pytest -q tests/connectors/runs/test_ingestion_gate.py
git add tests/connectors/runs/test_ingestion_gate.py
git commit -m "$(cat <<'EOF'
test(b2.6): add mock end-to-end ingestion gate

Three mock backends (YT Reporting + YT Analytics + AdSense) run through
run_one on httpx.MockTransport + local-secret:// + file-store://. Asserts:
- google_revenue_source_rows populated from all three connectors.
- C1.normalize_month produces revenue facts from YT rows.
- AdSense rows skip as SkipReason.MISSING_CHANNEL_ID (B2 is ingestion/
  audit evidence only; allocation -> facts is a future spec).
- audit_log carries STARTED + DOWNLOADED/PARSED + FINISHED events per run
  with the connector service principal.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 40: B2.6 validation gate + plan/backlog markers

- [ ] **Step 1: Run the full B2.6 gate**

```bash
python -m ruff check backend tests scripts
python -m pytest -q tests/connectors/google/ tests/connectors/runs/ \
                    tests/finance/test_google_source_normalizer_postgres.py \
                    tests/finance/test_google_source_normalizer_locked_month.py \
                    tests/finance/test_google_source_normalizer_logging.py \
                    tests/finance/test_google_source_normalizer_selection.py \
                    tests/finance/test_google_source_normalizer_service.py \
                    tests/db/test_connector_runs_migration_postgres.py \
                    tests/auth/test_audit_service.py \
                    tests/auth/test_audit_tenant_scope.py
python scripts/run_validation_gate.py
git diff --check
```
Expected: every test passes; ruff clean; gate clean; no whitespace issues.

- [ ] **Step 2: Update planning docs and mark all B2 PRs ⏳ (or ✅ for the merged ones)**

Append to plan/backlog:
```markdown
- ⏳ PR #N (B2.6) — AdSense Management client (ingestion/audit evidence
  only); audit wiring (CONNECTOR_JOB_RUN + REPORT_IMPORTED reused with
  lifecycle discriminator); mock end-to-end ingestion gate proves the
  three-connector substrate works on local mocks.
```

- [ ] **Step 3: Commit**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "$(cat <<'EOF'
docs(b2.6): mark AdSense + audit + ingestion gate as in-progress

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review checklist

After implementing all 40 tasks, run through this list:

**1. Spec coverage:**
- [ ] §1–§3 problem/goals/non-goals — covered by overall plan structure.
- [ ] §4.1 slicing — six PRs delivered.
- [ ] §4.2 module layout — every file in the layout is created by a task.
- [ ] §5.1 B2.1 surface — T1–T5.
- [ ] §5.2 B2.2 surface — T7–T11.
- [ ] §5.3 B2.3 surface (ORM + repo + migration + indexes) — T13–T18.
- [ ] §5.4 B2.4 surface (HTTP base + retry + YT Reporting + run_one + CLI) — T20–T30.
- [ ] §5.5 B2.5 surface (YT Analytics targeted) — T32–T33.
- [ ] §5.6 B2.6 surface (AdSense + audit + ingestion gate) — T35–T39.
- [ ] §6 failure model (3-bucket A/B/C) — T28.
- [ ] §7 retry policy — T21.
- [ ] §8 audit wiring — T36–T37.
- [ ] §9 testing posture + per-PR gates — T6, T12, T19, T31, T34, T40.
- [ ] §10 blast radius + rollout — captured in commit messages + plan/backlog updates.

**2. Placeholder scan:**
- [ ] No `TBD` / `TODO` / `implement later` in any task step.
- [ ] No "add appropriate error handling" without showing the error path.
- [ ] No "similar to Task N" — every task has its own code.
- [ ] No references to undefined types / functions / methods.

**3. Type consistency:**
- [ ] `ConnectorRunEntry` field names match in T15, T16, T27.
- [ ] `ConnectorRunOutcome` field names match in T27, T29, T30, T39.
- [ ] `mark_parsed` / `mark_failed` signatures consistent across T10, T11, T27, T28.
- [ ] `run_one` signature consistent across T27, T28, T29, T30.
- [ ] Audit emitter signatures consistent between T36 and T37.

---

## Execution handoff

Plan complete and saved to `Docs/superpowers/plans/2026-05-26-spec-b2-google-live-connector.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, two-stage review between tasks (spec compliance + code quality), fast iteration.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch with checkpoints for review.

Pause before code work per the brainstorming gate. Pick an option when ready to start, or redirect for plan revisions.
