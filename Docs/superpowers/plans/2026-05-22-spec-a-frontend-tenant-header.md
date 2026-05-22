# Spec A — Frontend X-UMS-Tenant Header Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the smallest end-to-end proof that the frontend can talk to the multi-tenant backend introduced in PR #36: `TenantContext`, a thin `fetch` wrapper that injects `X-UMS-Tenant` (set last, cannot be overridden), new `GET /tenants/me` endpoint backed by the existing resolver + principal dependency, dev-only AppShell proof element, Vitest test framework wired into the local validation gate.

**Architecture:** Backend route is a thin handler that depends on `current_principal_from_headers` (database mode overrides this to SQL principal loading) and reads `TENANT_CTX` populated by the existing `TenantResolverMiddleware`. Frontend uses a React context for the tenant selector seeded with the bootstrap slug `"ums"` and a `useApiClient()` hook that wraps native `fetch()` and injects the tenant header via `new Headers(init).set(...)` so the caller cannot override it. Vite dev proxy is the local trusted-gateway shim — the browser never sees or sends `X-UMS-Trusted-Gateway-Token`.

**Tech Stack:** FastAPI + SQLAlchemy + Pydantic (Python 3.14); React 19.2 + TypeScript 6 + Vite 8 + Tailwind 4 (frontend); Vitest + Testing Library + jsdom (new frontend test framework); the existing `scripts/run_validation_gate.py` + `backend/ums_smart_revenue/devtools/quality_gate.py` validation gate (extended).

---

## Source of truth

This plan implements the design at
`Docs/superpowers/specs/2026-05-22-spec-a-frontend-tenant-header-design.md`
(merged in PR #40, commit `c79ab3f`). When this plan and the spec
disagree, **the spec wins**. If you find a disagreement, stop and ask
before improvising.

## Pre-flight context for the implementer (read this once)

This is a multi-tenant FastAPI app. Every request that does **not** hit a
bypass path must carry an `X-UMS-Tenant` header whose value is a
tenant **slug** (e.g. `"ums"`, the seeded bootstrap row in
`backend/ums_smart_revenue/db/alembic/versions/20260516_0001_tenants_foundation.py`),
**not** a tenant UUID. The middleware at
`backend/ums_smart_revenue/tenancy/resolver.py:86` calls
`get_by_slug(normalised_slug)`.

The bootstrap tenant has:
- `id = UUID("00000000-0000-0000-0000-000000000001")`
  (`backend/ums_smart_revenue/tenancy/constants.py:16`)
- `slug = "ums"` (`backend/ums_smart_revenue/app.py:243-244`)
- `display_name = "UMS"` (same)
- `status = ACTIVE`

There are two app modes:
- `create_app(authz_source="database")` — wires
  `TrustedGatewayTenantResolverMiddleware` + overrides
  `current_principal_from_headers` to load from SQL. **This is the mode
  Spec A's correctness claims are scoped to.**
- `create_app(authz_source="headers")` — wires `DefaultTenantMiddleware`
  (when a `database_url` is provided), which binds the bootstrap tenant
  without validating the header. The `/tenants/me` route still requires
  the principal dependency, so unauthenticated callers in this mode
  still get 401, not 200.

**Never** ship `X-User-ID` or `X-UMS-Trusted-Gateway-Token` from the
browser. Those headers are the **gateway's** responsibility (deployed
edge or, in dev, the Vite proxy).

### Validation gate

Run the local validation gate after every material edit and before any
push:

```
python scripts/run_validation_gate.py
```

After this plan's Phase 6 lands, the gate also runs
`npm --prefix frontend run test` between pytest and the `git diff --check`
steps. Until then, run frontend tests manually with
`cd frontend && npm test`.

### Commit hygiene

- Every task ends with a commit. Real engineering happens one
  commit at a time.
- Use `git add <explicit-paths>` — never `git add -A` or `git add .`,
  per standing rule (the `frontend/package-lock.json` standing-exclusion
  no longer applies once Phase 2 modifies `package.json`; the
  regenerated lockfile is then in-scope and must be committed alongside
  `package.json`).
- Add the Co-Authored-By trailer per CLAUDE.md.
- Don't push until Phase 7 explicitly authorizes it; pause for
  CR/Codex review before merge.

### File-touch boundary

You will create or modify exactly the files listed below. **Do not
touch any other file** unless the spec or this plan asks for it.

---

## File structure

### Create

| Path | Purpose |
|---|---|
| `backend/ums_smart_revenue/api/tenants.py` | New router exposing `GET /tenants/me`. |
| `tests/api/test_tenants_api.py` | Cases 1–9 from the spec. |
| `frontend/src/contexts/TenantContext.tsx` | `TenantProvider`, `useTenant()`, `hydrate()`. |
| `frontend/src/contexts/__tests__/TenantContext.test.tsx` | Provider/hook unit tests. |
| `frontend/src/lib/api/client.ts` | `useApiClient()`, `ApiError`, URL + header helpers. |
| `frontend/src/lib/api/types.ts` | `TenantRead` TS type. |
| `frontend/src/lib/api/__tests__/client.test.ts` | Header-injection + ApiError + URL tests. |
| `frontend/src/components/srcc/__tests__/AppShell.test.tsx` | Happy-path / error-path / StrictMode-guard smoke tests. |
| `frontend/vitest.config.ts` | jsdom env, alias, setupFiles, globals. |
| `frontend/src/test-setup.ts` | `@testing-library/jest-dom/vitest` + `afterEach(cleanup)`. |

### Modify

| Path | Why |
|---|---|
| `backend/ums_smart_revenue/app.py` | Include the new `tenants_router`. |
| `backend/ums_smart_revenue/devtools/quality_gate.py` | Add `Frontend tests (Vitest)` `GateCommand`. |
| `tests/devtools/test_quality_gate.py` | Update step-order / label assertions. |
| `frontend/src/main.tsx` | Wrap `<AppShell />` in `<TenantProvider>`. |
| `frontend/src/components/srcc/AppShell.tsx` | Mount-time `/tenants/me` call + dev-only proof tag. |
| `frontend/vite.config.ts` | Dev proxy injecting gateway + token headers. |
| `frontend/package.json` | New devDeps + scripts (no `test:ui`). |
| `frontend/package-lock.json` | Regenerated by `npm install`. **Now in scope.** |
| `frontend/tsconfig.json` | Add `vitest/globals` + `@testing-library/jest-dom` to `types`. |
| `Docs/01_IMPLEMENTATION_PLAN.md` | `✅ PR #NN — Spec A …` inline mark. |
| `Docs/15_DELIVERY_BACKLOG.md` | `✅ PR #NN — Spec A …` inline mark. |

### Create (Phase 7 docs)

| Path | Purpose |
|---|---|
| `Docs/pulls/2026-MM-DD-prNN-spec-a-frontend-tenant-header-report.md` | What was requested + done. |
| `Docs/pulls/2026-MM-DD-prNN-spec-a-frontend-tenant-header-changelog.md` | Added/changed/removed. |
| `Docs/pulls/2026-MM-DD-prNN-spec-a-frontend-tenant-header-handoff.md` | Risks, rollback, next session. |

---

## Phase 0 — Pre-flight

### Task 0.1: Sync local main + branch off

**Files:** none (git state)

- [ ] **Step 1: Confirm clean working tree on `main`**

```bash
git status -s
git branch --show-current
```

Expected: branch is `main`. Working tree may show
` M frontend/package-lock.json` and `?? nul` — both are standing
exclusions and will not be committed.

- [ ] **Step 2: Sync main**

```bash
git fetch origin main
git checkout main
git pull --ff-only
git log --oneline -1
```

Expected last commit: `c79ab3f docs(spec): Spec A frontend X-UMS-Tenant header design (#40)`
or newer.

- [ ] **Step 3: Branch off**

```bash
git checkout -b pr/spec-a-frontend-tenant-header
git status -s
```

Expected: branch switched; same working-tree state as before.

### Task 0.2: Verify Node + npm + Python baseline

**Files:** none

- [ ] **Step 1: Verify Node + npm**

```bash
source ~/.bashrc && node --version && npm --version
```

Expected: `node ≥ 22.12.0` (matches `frontend/package.json:engines`),
`npm ≥ 10.x`. If Node not found, install it or activate the Nodist
override before continuing.

- [ ] **Step 2: Verify Python baseline**

```bash
python --version
python -c "import sys; print(sys.path)"
```

Expected: Python ≥ 3.14.

- [ ] **Step 3: Run the existing validation gate to confirm baseline green**

```bash
python scripts/run_validation_gate.py
```

Expected: `808 passed in ~90s`, ruff clean, AST policy clean, both
`git diff --check` clean.

Do not commit anything yet; this is just baseline verification.

---

## Phase 1 — Backend `GET /tenants/me`

### Task 1.1: Backend test scaffolding (fixtures, helpers, imports — no test bodies yet)

**Files:**
- Create: `tests/api/test_tenants_api.py`

This task lays down the SQLite-engine + bootstrap-seed + `_gateway_headers`
helper without any actual test cases. We commit it as scaffolding so the
test-case tasks below stay tight.

- [ ] **Step 1: Read the precedent**

Open `tests/api/test_database_principals.py:1-100`. Note the imports,
the SQLite engine construction pattern, and how `TestClient(app)` is
wired against `create_app(database_url=..., authz_source="database")`.

- [ ] **Step 2: Create the new test file with scaffolding only**

```python
# tests/api/test_tenants_api.py
"""Endpoint tests for GET /tenants/me (Spec A).

Cases 1-9 from Docs/superpowers/specs/2026-05-22-spec-a-frontend-tenant-header-design.md
section 9.1. Fixtures mirror tests/api/test_database_principals.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.app import (
    TrustedGatewayTenantResolverMiddleware,
    create_app,
)
from ums_smart_revenue.db.security_models import (
    RoleORM,
    SecurityBase,
    UserORM,
    UserRoleAssignmentORM,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.models import TenantStatus

TEST_USER_ID = UUID("00000000-0000-0000-0000-0000000000aa")
BOOTSTRAP_TENANT_ID = UUID(UMS_TENANT_ID)
BOOTSTRAP_DISPLAY = "UMS"


def _gateway_headers(user_id: UUID = TEST_USER_ID) -> dict[str, str]:
    return {
        "X-User-ID": str(user_id),
        "X-UMS-Trusted-Gateway-Token": os.environ["UMS_TRUSTED_GATEWAY_TOKEN"],
    }


@pytest.fixture
def db_engine_url(tmp_path):
    db_path = tmp_path / "spec_a.sqlite"
    return f"sqlite:///{db_path}"


@pytest.fixture
def seeded_engine(db_engine_url):
    engine = create_engine(db_engine_url, future=True)
    TenantBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as session:
        _seed_bootstrap_tenant(session)
        _seed_enabled_user(session)
        session.commit()
    yield engine
    engine.dispose()


def _seed_bootstrap_tenant(session: Session) -> None:
    now = datetime.now(UTC)
    session.add(
        TenantORM(
            id=BOOTSTRAP_TENANT_ID,
            slug="ums",
            display_name=BOOTSTRAP_DISPLAY,
            primary_currency="USD",
            status=TenantStatus.ACTIVE.value,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
    )


def _seed_enabled_user(session: Session) -> None:
    # Insert ONE enabled SQL principal with at least a placeholder role
    # so current_principal_from_database can hydrate the principal for
    # X-User-ID=TEST_USER_ID. Match the exact column set in
    # backend/ums_smart_revenue/db/security_models.py UserORM and RoleORM.
    raise NotImplementedError(
        "Fill in _seed_enabled_user in Task 1.2 against the real "
        "UserORM/RoleORM column set."
    )


@pytest.fixture
def app_db_mode(seeded_engine):
    app = create_app(database_url=str(seeded_engine.url), authz_source="database")
    return app


@pytest.fixture
def client_db_mode(app_db_mode):
    with TestClient(app_db_mode) as client:
        yield client
```

- [ ] **Step 3: Run pytest collection to confirm the file imports cleanly**

```bash
pytest tests/api/test_tenants_api.py --collect-only -q
```

Expected: pytest reports 0 tests collected with no import errors. (The
`raise NotImplementedError` lives in a fixture and only fires when a
test asks for it.)

- [ ] **Step 4: Commit the scaffold**

```bash
git add tests/api/test_tenants_api.py
git commit -m "$(cat <<'EOF'
test(api): scaffolding for Spec A /tenants/me endpoint tests

Sets up fixtures and helpers per the spec at
Docs/superpowers/specs/2026-05-22-spec-a-frontend-tenant-header-design.md
section 9.1. No test cases yet; those land per case in the next tasks.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 1.2: Case 1 (happy path) — implement the route to make it pass

**Files:**
- Create: `backend/ums_smart_revenue/api/tenants.py`
- Modify: `backend/ums_smart_revenue/app.py`
- Modify: `tests/api/test_tenants_api.py` (replace the
  `_seed_enabled_user` stub + add case 1)

- [ ] **Step 1: Fill in the real `_seed_enabled_user`**

Open `backend/ums_smart_revenue/db/security_models.py` and locate the
`UserORM`, `RoleORM`, `UserRoleAssignmentORM` column sets. Replace the
`_seed_enabled_user` body in
`tests/api/test_tenants_api.py` with:

```python
def _seed_enabled_user(session: Session) -> None:
    now = datetime.now(UTC)
    role = RoleORM(
        id=UUID("00000000-0000-0000-0000-0000000000a1"),
        tenant_id=BOOTSTRAP_TENANT_ID,
        slug="spec-a-test-role",
        display_name="Spec A Test Role",
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    user = UserORM(
        id=TEST_USER_ID,
        tenant_id=BOOTSTRAP_TENANT_ID,
        email="spec-a@example.invalid",
        display_name="Spec A Test User",
        status="ACTIVE",
        service_account=False,
        created_at=now,
        updated_at=now,
    )
    assignment = UserRoleAssignmentORM(
        id=UUID("00000000-0000-0000-0000-0000000000a2"),
        tenant_id=BOOTSTRAP_TENANT_ID,
        user_id=TEST_USER_ID,
        role_id=role.id,
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    session.add_all([role, user, assignment])
```

> If the column names above don't match the real ORM (e.g. `status` is
> an enum and not a string, or `service_account` is not present),
> reconcile to the real column names by reading
> `backend/ums_smart_revenue/db/security_models.py`. Do **not** invent
> columns. If the principal loader requires a permission grant too,
> add it; check `auth/principals.py:SqlAlchemyPrincipalLoader`.

- [ ] **Step 2: Add the failing case-1 test**

Append to `tests/api/test_tenants_api.py`:

```python
def test_tenants_me_returns_bootstrap_tenant_for_resolved_slug(
    client_db_mode,
):
    response = client_db_mode.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "ums", **_gateway_headers()},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload == {
        "id": str(BOOTSTRAP_TENANT_ID),
        "slug": "ums",
        "display_name": BOOTSTRAP_DISPLAY,
    }
    assert response.headers.get("Vary") and "X-UMS-Tenant" in response.headers["Vary"]
```

- [ ] **Step 3: Run case 1 — confirm it fails because the route doesn't exist**

```bash
pytest tests/api/test_tenants_api.py::test_tenants_me_returns_bootstrap_tenant_for_resolved_slug -q
```

Expected: FAIL with `404 Not Found` (FastAPI returns 404 for unregistered
routes).

- [ ] **Step 4: Implement the route**

Create `backend/ums_smart_revenue/api/tenants.py`:

```python
"""GET /tenants/me — return the tenant resolved by middleware after auth."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.tenancy.context import require_current_tenant

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantRead(BaseModel):
    """Public shape of the tenant context returned to the browser."""

    id: UUID
    slug: str
    display_name: str


# ============================================================================
# Purpose: Return the tenant resolved by TenantResolverMiddleware after the
#          gateway/principal dependency has authenticated the caller.
# Database/ORM: No SQL from this handler; current_principal_from_headers is
#               overridden to SQL principal loading in database auth mode.
# Standards: Thin route; dependency-owned auth; explicit field construction
#            because Tenant is a domain dataclass, not a Pydantic model.
# Blast Radius: Authorization dependency required; no write path, no finance
#               impact. No graph projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> auth dependency.
#   - File: backend/ums_smart_revenue/tenancy/resolver.py -> sets TENANT_CTX.
#   - File: backend/ums_smart_revenue/tenancy/context.py -> require_current_tenant.
#   - File: backend/ums_smart_revenue/app.py -> include_router wiring.
# ============================================================================
@router.get("/me", response_model=TenantRead)
def get_current_tenant_endpoint(
    _principal: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
) -> TenantRead:
    tenant = require_current_tenant()
    return TenantRead(
        id=tenant.id,
        slug=tenant.slug,
        display_name=tenant.display_name,
    )
```

- [ ] **Step 5: Register the router in `app.py`**

Open `backend/ums_smart_revenue/app.py`. The existing twelve
`app.include_router(...)` calls live at lines 111-122. Add the import
and the registration. Place the import alphabetically next to the other
`from ums_smart_revenue.api.*` imports near line 47-48, and the
`include_router` call next to `users_router` (alphabetical):

Add this import:

```python
from ums_smart_revenue.api.tenants import router as tenants_router
```

Add this line in the include block:

```python
app.include_router(tenants_router)
```

- [ ] **Step 6: Run case 1 — confirm it passes**

```bash
pytest tests/api/test_tenants_api.py::test_tenants_me_returns_bootstrap_tenant_for_resolved_slug -q
```

Expected: PASS.

- [ ] **Step 7: Run the full backend gate to verify no regression**

```bash
python scripts/run_validation_gate.py
```

Expected: 809 passed (+1 from case 1). Ruff clean. AST policy clean.
Both diff checks clean.

- [ ] **Step 8: Commit**

```bash
git add backend/ums_smart_revenue/api/tenants.py backend/ums_smart_revenue/app.py tests/api/test_tenants_api.py
git commit -m "$(cat <<'EOF'
feat(api): add GET /tenants/me proof endpoint (Spec A)

Thin route depending on current_principal_from_headers + the existing
TenantResolverMiddleware. Returns TenantRead { id, slug, display_name }
from TENANT_CTX. Case 1 (happy path) lands; cases 2-9 in following
commits.

Closes S2 spec Phase 5 (backend half).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 1.3: Cases 2-5 (resolver-input validation: missing / blank / over-255 / duplicate / unknown)

**Files:**
- Modify: `tests/api/test_tenants_api.py`

All four cases test the existing resolver without changing the route.
Add them as a batch.

- [ ] **Step 1: Add case 2 (missing X-UMS-Tenant → 400)**

Append:

```python
def test_tenants_me_rejects_missing_tenant_header(client_db_mode):
    response = client_db_mode.get("/tenants/me", headers=_gateway_headers())
    assert response.status_code == 400
    assert response.json()["detail"] == "Tenant slug must not be blank"
    assert "X-UMS-Tenant" in (response.headers.get("Vary") or "")
```

- [ ] **Step 2: Add case 3 (whitespace and over-255 chars → 400)**

```python
def test_tenants_me_rejects_whitespace_tenant_header(client_db_mode):
    response = client_db_mode.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "   ", **_gateway_headers()},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Tenant slug must not be blank"


def test_tenants_me_rejects_overlong_tenant_header(client_db_mode):
    response = client_db_mode.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "a" * 256, **_gateway_headers()},
    )
    assert response.status_code == 400
    assert "at most 255 characters" in response.json()["detail"]
```

- [ ] **Step 3: Add case 4 (duplicate header → 400)**

Use `client.build_request` + `client.send` to send a list-style
duplicated header; the dict-style `headers=` keyword on
`TestClient.get` collapses duplicates.

```python
def test_tenants_me_rejects_duplicate_tenant_headers(client_db_mode):
    request = client_db_mode.build_request(
        "GET",
        "/tenants/me",
        headers=[
            ("X-UMS-Tenant", "ums"),
            ("X-UMS-Tenant", "ums"),
            *_gateway_headers().items(),
        ],
    )
    response = client_db_mode.send(request)
    assert response.status_code == 400
    assert response.json()["detail"] == "X-UMS-Tenant must be provided exactly once"
```

- [ ] **Step 4: Add case 5 (unknown slug → 404)**

```python
def test_tenants_me_returns_404_for_unknown_slug(client_db_mode):
    response = client_db_mode.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "not-a-tenant", **_gateway_headers()},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Tenant 'not-a-tenant' not found"
```

- [ ] **Step 5: Run the new cases**

```bash
pytest tests/api/test_tenants_api.py -q
```

Expected: 5 passing (case 1 + new 4). If case 4 fails because httpx
collapses headers via `build_request`, switch to constructing the
`httpx.Request` directly with the same header list, then
`client.send(request)`.

- [ ] **Step 6: Commit**

```bash
git add tests/api/test_tenants_api.py
git commit -m "$(cat <<'EOF'
test(api): cover resolver-input validation for /tenants/me

Cases 2-5 from Spec A section 9.1: missing, whitespace, over-255-char,
duplicate, and unknown tenant slug. All resolver-side validation;
endpoint code unchanged.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 1.4: Case 6 (403 — custom-wired app with deny authorizer)

**Files:**
- Modify: `tests/api/test_tenants_api.py`

`create_app(authz_source="database")` wires
`_allow_database_auth_tenant`, which **always** returns True. To prove
the 403 path, we construct an isolated FastAPI app that wires
`TrustedGatewayTenantResolverMiddleware` with a deny authorizer.

- [ ] **Step 1: Add the fixture and the test**

Append to `tests/api/test_tenants_api.py`:

```python
@pytest.fixture
def client_deny_authz(seeded_engine):
    """Isolated app whose tenant authorizer denies every resolution."""
    from fastapi import FastAPI
    from sqlalchemy.orm import sessionmaker

    from ums_smart_revenue.api.dependencies import (
        current_db_session,
        current_principal_from_headers,
    )
    from ums_smart_revenue.api.tenants import router as tenants_router
    from ums_smart_revenue.db.session import session_dependency

    app = FastAPI()
    session_factory = sessionmaker(bind=seeded_engine, future=True)
    app.include_router(tenants_router)
    app.dependency_overrides[current_db_session] = session_dependency(session_factory)
    app.add_middleware(
        TrustedGatewayTenantResolverMiddleware,
        session_factory=lambda: session_factory(),
        authorize_tenant=lambda *_: False,
    )
    with TestClient(app) as client:
        yield client


def test_tenants_me_returns_403_when_authorizer_denies(client_deny_authz):
    response = client_deny_authz.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "ums", **_gateway_headers()},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Tenant access denied"
```

> If the principal dependency is required in this minimal app (it will
> be, because the route depends on it), override
> `current_principal_from_headers` to a stub that returns a valid
> `UserPrincipal` for `TEST_USER_ID` so the test isolates the 403 path
> to the authorizer outcome rather than principal lookup.

- [ ] **Step 2: Run the new test**

```bash
pytest tests/api/test_tenants_api.py::test_tenants_me_returns_403_when_authorizer_denies -q
```

Expected: PASS with `403 Tenant access denied`.

- [ ] **Step 3: Commit**

```bash
git add tests/api/test_tenants_api.py
git commit -m "$(cat <<'EOF'
test(api): cover 403 authorizer denial for /tenants/me

Case 6 from Spec A section 9.1: standalone app instance wires
TrustedGatewayTenantResolverMiddleware with authorize_tenant returning
False so we can prove the 403 path that stock create_app's permissive
authorizer would mask.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 1.5: Cases 7-9 (gateway auth + headers-mode tenant fallback)

**Files:**
- Modify: `tests/api/test_tenants_api.py`

- [ ] **Step 1: Add case 7 (missing/invalid gateway auth → 401)**

Two variants — missing `X-User-ID` and invalid token:

```python
def test_tenants_me_returns_401_when_gateway_user_id_missing(client_db_mode):
    headers = _gateway_headers()
    headers.pop("X-User-ID")
    headers["X-UMS-Tenant"] = "ums"
    response = client_db_mode.get("/tenants/me", headers=headers)
    assert response.status_code == 401


def test_tenants_me_returns_401_when_gateway_token_invalid(client_db_mode):
    response = client_db_mode.get(
        "/tenants/me",
        headers={
            "X-UMS-Tenant": "ums",
            "X-User-ID": str(TEST_USER_ID),
            "X-UMS-Trusted-Gateway-Token": "definitely-not-the-token",
        },
    )
    assert response.status_code == 401
```

> Read `current_trusted_gateway_identity` in
> `backend/ums_smart_revenue/api/dependencies.py` to confirm whether
> missing identity returns 401 or 403. Adjust the assertion if the
> existing dependency returns 403 — but the spec calls case 7 as 401
> and the dependency is the authoritative source. If they disagree,
> stop and ask before changing the assertion.

- [ ] **Step 2: Add case 8 (headers-mode requires gateway auth → 401)**

```python
@pytest.fixture
def client_headers_mode(seeded_engine):
    app = create_app(database_url=str(seeded_engine.url), authz_source="headers")
    with TestClient(app) as client:
        yield client


def test_tenants_me_headers_mode_requires_gateway_auth(client_headers_mode):
    """Default headers mode must NOT leak the bootstrap tenant identity to
    unauthenticated callers. The principal dependency fails closed even when
    DefaultTenantMiddleware would have bound the bootstrap tenant.
    """
    response = client_headers_mode.get("/tenants/me")
    assert response.status_code == 401
```

- [ ] **Step 3: Add case 9 (headers-mode with auth + no X-UMS-Tenant → 200 bootstrap)**

```python
def test_tenants_me_headers_mode_returns_bootstrap_after_gateway_auth(
    client_headers_mode,
):
    """Headers mode is documented as a developer-shortcut for bootstrap-only
    deployments. With trusted-gateway auth in place, DefaultTenantMiddleware
    binds the bootstrap tenant even when X-UMS-Tenant is absent. This test
    documents the behavior — it is NOT security coverage for /tenants/me.
    """
    response = client_headers_mode.get("/tenants/me", headers=_gateway_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["slug"] == "ums"
    assert payload["id"] == str(BOOTSTRAP_TENANT_ID)
```

- [ ] **Step 4: Run all 9 cases**

```bash
pytest tests/api/test_tenants_api.py -q
```

Expected: 9 passing.

- [ ] **Step 5: Run the full backend gate**

```bash
python scripts/run_validation_gate.py
```

Expected: 817 passed (+9 from baseline 808). Ruff clean. AST policy
clean. Both diff checks clean.

- [ ] **Step 6: Commit**

```bash
git add tests/api/test_tenants_api.py
git commit -m "$(cat <<'EOF'
test(api): cover gateway auth + headers-mode fallback for /tenants/me

Cases 7-9 from Spec A section 9.1:
- Missing/invalid trusted-gateway identity returns 401.
- Default headers mode without auth returns 401 (no leak).
- Headers mode with auth and no tenant header returns the bootstrap
  tenant — documented as a developer shortcut, NOT security coverage.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Phase 2 — Frontend test framework

### Task 2.1: Add devDeps + scripts + tsconfig types

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/tsconfig.json`

- [ ] **Step 1: Update `frontend/package.json`**

The existing file is 26 lines. Add new devDeps + scripts. The
resulting file should look like:

```json
{
  "name": "ums-smart-revenue-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "engines": {
    "node": ">=22.12.0"
  },
  "scripts": {
    "dev": "vite --host 127.0.0.1",
    "build": "vite build",
    "preview": "vite preview --host 127.0.0.1",
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "dependencies": {
    "react": "19.2.6",
    "react-dom": "19.2.6",
    "tw-animate-css": "1.4.0"
  },
  "devDependencies": {
    "@tailwindcss/vite": "4.3.0",
    "@testing-library/jest-dom": "^6.5.0",
    "@testing-library/react": "^16.1.0",
    "@testing-library/user-event": "^14.5.2",
    "@types/react": "^19.0.0",
    "@types/react-dom": "^19.0.0",
    "@vitejs/plugin-react": "6.0.1",
    "jsdom": "^25.0.0",
    "tailwindcss": "4.3.0",
    "typescript": "6.0.3",
    "vite": "8.0.12",
    "vitest": "^3.0.0"
  }
}
```

> If `@types/react` and `@types/react-dom` are already provided by some
> other entry (peer-dep), leave them off. Verify by checking the
> current `node_modules/@types/react` after `npm install`.

- [ ] **Step 2: Run `npm install` to regenerate the lockfile**

```bash
source ~/.bashrc && cd frontend && npm install && cd ..
```

Expected: `node_modules/` populated; `package-lock.json` updated to
include the new tree.

- [ ] **Step 3: Update `frontend/tsconfig.json`**

Open `frontend/tsconfig.json`. Locate `compilerOptions.types`. Add
`"vitest/globals"` and `"@testing-library/jest-dom"` to that array. If
the array doesn't exist yet, add it:

```jsonc
{
  "compilerOptions": {
    // ...existing options...
    "types": ["vitest/globals", "@testing-library/jest-dom"]
  }
  // ...rest unchanged...
}
```

- [ ] **Step 4: Commit (no validation gate yet — Phase 6 wires Vitest in)**

```bash
git add frontend/package.json frontend/package-lock.json frontend/tsconfig.json
git commit -m "$(cat <<'EOF'
chore(frontend): add Vitest + Testing Library + jsdom devDeps

Adds the frontend test framework Spec A wires into the validation gate.
package-lock.json is intentionally in scope for this PR (standing
package-lock exclusion applied to PR #38/#39 only).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 2.2: Vitest config + test setup file

**Files:**
- Create: `frontend/vitest.config.ts`
- Create: `frontend/src/test-setup.ts`

- [ ] **Step 1: Create `frontend/vitest.config.ts`**

```ts
import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    setupFiles: ["src/test-setup.ts"],
    globals: true,
    css: false,
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
});
```

- [ ] **Step 2: Create `frontend/src/test-setup.ts`**

```ts
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
```

- [ ] **Step 3: Smoke-run Vitest with no tests**

```bash
source ~/.bashrc && cd frontend && npm test ; cd ..
```

Expected: Vitest reports "No test files found" with exit code 0 (or 1
depending on the Vitest version; either way no crash, no config error).
If it crashes complaining about jsdom or jest-dom, re-check Steps 1-2.

- [ ] **Step 4: Commit**

```bash
git add frontend/vitest.config.ts frontend/src/test-setup.ts
git commit -m "$(cat <<'EOF'
chore(frontend): wire Vitest config + test-setup

jsdom environment, Testing Library cleanup hook, jest-dom matcher
import. globals: true so describe/it/expect are ambient under the
"vitest/globals" tsconfig type added in the previous commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Phase 3 — TenantContext

### Task 3.1: TenantProvider + useTenant + hydrate (TDD cycle)

**Files:**
- Create: `frontend/src/contexts/TenantContext.tsx`
- Create: `frontend/src/contexts/__tests__/TenantContext.test.tsx`

- [ ] **Step 1: Write the failing test file**

```tsx
// frontend/src/contexts/__tests__/TenantContext.test.tsx
import { renderHook, act } from "@testing-library/react";
import type { ReactNode } from "react";

import { TenantProvider, useTenant } from "@/contexts/TenantContext";

function wrapper({ children }: { children: ReactNode }) {
  return <TenantProvider>{children}</TenantProvider>;
}

describe("TenantContext", () => {
  it("seeds with the bootstrap slug and null id/displayName", () => {
    const { result } = renderHook(() => useTenant(), { wrapper });
    expect(result.current.tenantSlug).toBe("ums");
    expect(result.current.id).toBeNull();
    expect(result.current.displayName).toBeNull();
  });

  it("merges id and displayName when hydrate is called", () => {
    const { result } = renderHook(() => useTenant(), { wrapper });
    act(() => {
      result.current.hydrate({
        id: "00000000-0000-0000-0000-000000000001",
        slug: "ums",
        display_name: "UMS",
      });
    });
    expect(result.current.id).toBe("00000000-0000-0000-0000-000000000001");
    expect(result.current.displayName).toBe("UMS");
    expect(result.current.tenantSlug).toBe("ums");
  });

  it("throws when useTenant is called outside <TenantProvider>", () => {
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => renderHook(() => useTenant())).toThrow(
      /useTenant must be used within <TenantProvider>/,
    );
    consoleSpy.mockRestore();
  });
});
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd frontend && npm test -- src/contexts ; cd ..
```

Expected: FAIL — cannot find module `@/contexts/TenantContext`.

- [ ] **Step 3: Implement the provider**

Create `frontend/src/contexts/TenantContext.tsx`:

```tsx
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

type TenantState = {
  tenantSlug: string;
  id: string | null;
  displayName: string | null;
};

type TenantHydrationPayload = {
  id: string;
  slug: string;
  display_name: string;
};

type TenantContextValue = TenantState & {
  hydrate: (payload: TenantHydrationPayload) => void;
};

const TenantContext = createContext<TenantContextValue | null>(null);

export function TenantProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<TenantState>({
    tenantSlug: "ums",
    id: null,
    displayName: null,
  });

  const hydrate = useCallback((payload: TenantHydrationPayload) => {
    setState((previous) => ({
      ...previous,
      id: payload.id,
      displayName: payload.display_name,
    }));
  }, []);

  const value = useMemo<TenantContextValue>(
    () => ({ ...state, hydrate }),
    [state, hydrate],
  );

  return (
    <TenantContext.Provider value={value}>{children}</TenantContext.Provider>
  );
}

export function useTenant(): TenantContextValue {
  const value = useContext(TenantContext);
  if (value === null) {
    throw new Error("useTenant must be used within <TenantProvider>");
  }
  return value;
}
```

- [ ] **Step 4: Run to confirm pass**

```bash
cd frontend && npm test -- src/contexts ; cd ..
```

Expected: 3 passing.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/contexts/TenantContext.tsx frontend/src/contexts/__tests__/TenantContext.test.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): add TenantContext seeded with bootstrap slug

React context exposes { tenantSlug, id, displayName, hydrate }. Seeded
with tenantSlug "ums"; id and displayName hydrate from /tenants/me.
useTenant() outside <TenantProvider> throws.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Phase 4 — useApiClient + ApiError + types

### Task 4.1: ApiError + URL resolution + header injection (failing tests)

**Files:**
- Create: `frontend/src/lib/api/types.ts`
- Create: `frontend/src/lib/api/__tests__/client.test.ts`

The tests are written first; the impl lands in Task 4.2.

- [ ] **Step 1: Create the type module**

```ts
// frontend/src/lib/api/types.ts
export type TenantRead = {
  id: string;
  slug: string;
  display_name: string;
};
```

- [ ] **Step 2: Create the test file with all client cases**

```ts
// frontend/src/lib/api/__tests__/client.test.ts
import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, useApiClient } from "@/lib/api/client";
import { TenantProvider } from "@/contexts/TenantContext";

function wrapper({ children }: { children: React.ReactNode }) {
  return <TenantProvider>{children}</TenantProvider>;
}

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
});

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function textResponse(body: string, init: ResponseInit = {}) {
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/plain" },
    ...init,
  });
}

function lastFetchArgs() {
  return (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.at(-1);
}

describe("useApiClient header injection", () => {
  it("injects X-UMS-Tenant: ums when caller passes no headers", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/x");
    const [, init] = lastFetchArgs()!;
    const headers = new Headers(init?.headers);
    expect(headers.get("X-UMS-Tenant")).toBe("ums");
  });

  it("overrides caller-supplied X-UMS-Tenant with the provider slug", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/x", { headers: { "X-UMS-Tenant": "evil" } });
    const headers = new Headers(lastFetchArgs()![1]?.headers);
    expect(headers.get("X-UMS-Tenant")).toBe("ums");
  });

  it.each([
    ["Headers instance", new Headers([["X-Other", "1"]])],
    ["array of tuples", [["X-Other", "1"]] as HeadersInit],
    ["plain object", { "X-Other": "1" } as HeadersInit],
  ])("normalises %s and still ships X-UMS-Tenant: ums", async (_, headersInit) => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/x", { headers: headersInit });
    const sent = new Headers(lastFetchArgs()![1]?.headers);
    expect(sent.get("X-UMS-Tenant")).toBe("ums");
    expect(sent.get("X-Other")).toBe("1");
  });
});

describe("useApiClient URL resolution", () => {
  it("resolves to a relative URL when VITE_API_BASE_URL is unset", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/tenants/me");
    expect(lastFetchArgs()![0]).toBe("/tenants/me");
  });

  it("strips trailing slash from VITE_API_BASE_URL", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.com/");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/tenants/me");
    expect(lastFetchArgs()![0]).toBe("https://api.example.com/tenants/me");
  });
});

describe("useApiClient Content-Type handling", () => {
  it("does not set Content-Type on GET", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/x");
    const headers = new Headers(lastFetchArgs()![1]?.headers);
    expect(headers.has("Content-Type")).toBe(false);
  });

  it("sets Content-Type: application/json on POST with a plain object body", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.post("/x", { foo: 1 });
    const headers = new Headers(lastFetchArgs()![1]?.headers);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(lastFetchArgs()![1]?.body).toBe(JSON.stringify({ foo: 1 }));
  });

  it("does not set Content-Type on POST with FormData (lets browser set multipart)", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    const fd = new FormData();
    fd.append("k", "v");
    await result.current.post("/x", fd);
    const headers = new Headers(lastFetchArgs()![1]?.headers);
    expect(headers.has("Content-Type")).toBe(false);
  });
});

describe("useApiClient response handling", () => {
  it("returns the parsed JSON body on 200", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ id: "abc", slug: "ums", display_name: "UMS" }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    const payload = await result.current.get<{ id: string }>("/tenants/me");
    expect(payload.id).toBe("abc");
  });

  it("resolves to undefined on 204", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    const payload = await result.current.delete("/x");
    expect(payload).toBeUndefined();
  });

  it("throws ApiError with parsed JSON body on 4xx", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ detail: "Tenant slug must not be blank" }, { status: 400 }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await expect(result.current.get("/tenants/me")).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      body: { detail: "Tenant slug must not be blank" },
    });
  });

  it("throws ApiError with raw text body on 5xx text response", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      textResponse("upstream timed out", { status: 503 }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await expect(result.current.get("/x")).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      body: "upstream timed out",
    });
  });

  it("propagates fetch rejection (TypeError) unwrapped", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockRejectedValue(
      new TypeError("Failed to fetch"),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await expect(result.current.get("/x")).rejects.toBeInstanceOf(TypeError);
    await expect(result.current.get("/x")).rejects.not.toBeInstanceOf(ApiError);
  });
});
```

- [ ] **Step 3: Run — confirm all fail**

```bash
cd frontend && npm test -- src/lib/api ; cd ..
```

Expected: every test fails with "cannot find module `@/lib/api/client`".

### Task 4.2: Implement `useApiClient` + `ApiError`

**Files:**
- Create: `frontend/src/lib/api/client.ts`

- [ ] **Step 1: Implement the client**

```ts
// frontend/src/lib/api/client.ts
import { useMemo } from "react";

import { useTenant } from "@/contexts/TenantContext";

export class ApiError extends Error {
  readonly name = "ApiError";
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: unknown,
    public readonly url: string,
  ) {
    super(message);
  }
}

function resolveUrl(path: string): string {
  const raw = import.meta.env.VITE_API_BASE_URL ?? "";
  const base = raw.replace(/\/+$/, "");
  const normalisedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${normalisedPath}`;
}

function buildHeaders(
  init: HeadersInit | undefined,
  tenantSlug: string,
  hasJsonBody: boolean,
): Headers {
  const headers = new Headers(init);
  if (hasJsonBody && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("X-UMS-Tenant", tenantSlug);
  return headers;
}

async function parseBody(res: Response): Promise<unknown> {
  if (res.status === 204) return undefined;
  const contentType = res.headers.get("Content-Type") ?? "";
  if (contentType.includes("application/json")) return res.json();
  return res.text();
}

type RequestOptions = RequestInit & { bodyIsJson?: boolean };

function withJsonBody(
  body: unknown,
  init: RequestInit = {},
): RequestOptions {
  if (body === undefined) return init;
  if (
    typeof body === "string" ||
    body instanceof FormData ||
    body instanceof URLSearchParams ||
    body instanceof Blob ||
    body instanceof ArrayBuffer ||
    ArrayBuffer.isView(body)
  ) {
    return { ...init, body: body as BodyInit, bodyIsJson: false };
  }
  return { ...init, body: JSON.stringify(body), bodyIsJson: true };
}

export function useApiClient() {
  const { tenantSlug } = useTenant();

  return useMemo(() => {
    async function request<T>(
      method: string,
      path: string,
      init: RequestOptions = {},
    ): Promise<T> {
      const url = resolveUrl(path);
      const { bodyIsJson = false, ...requestInit } = init;
      const headers = buildHeaders(requestInit.headers, tenantSlug, bodyIsJson);
      const res = await fetch(url, { ...requestInit, method, headers });
      if (!res.ok) {
        const body = await parseBody(res);
        throw new ApiError(`${res.status} ${res.statusText}`, res.status, body, url);
      }
      return (await parseBody(res)) as T;
    }

    return {
      get: <T>(path: string, init?: RequestInit) =>
        request<T>("GET", path, init),
      post: <T>(path: string, body?: unknown, init?: RequestInit) =>
        request<T>("POST", path, withJsonBody(body, init)),
      put: <T>(path: string, body?: unknown, init?: RequestInit) =>
        request<T>("PUT", path, withJsonBody(body, init)),
      patch: <T>(path: string, body?: unknown, init?: RequestInit) =>
        request<T>("PATCH", path, withJsonBody(body, init)),
      delete: <T>(path: string, init?: RequestInit) =>
        request<T>("DELETE", path, init),
    };
  }, [tenantSlug]);
}
```

- [ ] **Step 2: Run — confirm all pass**

```bash
cd frontend && npm test -- src/lib/api ; cd ..
```

Expected: every test in `client.test.ts` passes.

- [ ] **Step 3: Run the full frontend test suite**

```bash
cd frontend && npm test ; cd ..
```

Expected: TenantContext + client tests all pass.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api/client.ts frontend/src/lib/api/types.ts frontend/src/lib/api/__tests__/client.test.ts
git commit -m "$(cat <<'EOF'
feat(frontend): add useApiClient + ApiError + TenantRead type

Thin fetch wrapper that normalises HeadersInit via new Headers(init),
sets X-UMS-Tenant: <slug> last so caller headers cannot override it,
emits Content-Type: application/json only for JSON bodies (no header
for GET / FormData / Blob / URLSearchParams / ArrayBuffer), parses 204
as undefined, parses JSON or text bodies, throws typed ApiError on
non-2xx, propagates fetch rejection unwrapped. URL resolution
trims trailing slashes from VITE_API_BASE_URL so unset env yields a
relative URL.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Phase 5 — AppShell wire-up + Vite dev proxy

### Task 5.1: Wrap `main.tsx` in `<TenantProvider>`

**Files:**
- Modify: `frontend/src/main.tsx`

- [ ] **Step 1: Update `frontend/src/main.tsx`**

Current contents (5 lines after imports):

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import AppShell from "@/components/srcc/AppShell";
import "@/styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppShell />
  </StrictMode>,
);
```

Updated:

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import AppShell from "@/components/srcc/AppShell";
import { TenantProvider } from "@/contexts/TenantContext";
import "@/styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TenantProvider>
      <AppShell />
    </TenantProvider>
  </StrictMode>,
);
```

- [ ] **Step 2: Build the frontend to confirm no TypeScript regression**

```bash
source ~/.bashrc && cd frontend && npm run build ; cd ..
```

Expected: build succeeds.

- [ ] **Step 3: Commit (no tests yet — AppShell tests follow)**

```bash
git add frontend/src/main.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): wrap AppShell in <TenantProvider>

Provider must wrap the app so useApiClient() can read the tenant slug.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 5.2: AppShell mount-time `/tenants/me` call + dev-only proof tag (failing tests → impl → pass)

**Files:**
- Create: `frontend/src/components/srcc/__tests__/AppShell.test.tsx`
- Modify: `frontend/src/components/srcc/AppShell.tsx`

- [ ] **Step 1: Read the existing AppShell**

```bash
wc -l frontend/src/components/srcc/AppShell.tsx
```

Note the file size and the current imports + the JSX root element. The
edit appends one `useEffect` block and inserts the dev-only `<small>`
element. Do **not** restructure the existing layout.

- [ ] **Step 2: Write the failing test file**

```tsx
// frontend/src/components/srcc/__tests__/AppShell.test.tsx
import { render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AppShell from "@/components/srcc/AppShell";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("AppShell tenant proof tag", () => {
  it("hydrates the tenant and shows UMS (ums) on the dev-only tag", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({
        id: "00000000-0000-0000-0000-000000000001",
        slug: "ums",
        display_name: "UMS",
      }),
    );
    render(
      <TenantProvider>
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    expect(tag.textContent).toContain("UMS (ums)");
    expect(tag.textContent).toContain("00000000-0000-0000-0000-000000000001");
  });

  it("shows the typed ApiError message on 503", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ detail: "Tenant registry unavailable" }, 503),
    );
    render(
      <TenantProvider>
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    expect(tag.textContent).toMatch(/503/);
  });

  it("fires fetch exactly once under <StrictMode> (re-entry guard)", async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock.mockResolvedValue(
      jsonResponse({
        id: "00000000-0000-0000-0000-000000000001",
        slug: "ums",
        display_name: "UMS",
      }),
    );
    render(
      <StrictMode>
        <TenantProvider>
          <AppShell />
        </TenantProvider>
      </StrictMode>,
    );
    await screen.findByTestId("tenant-proof");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 3: Run — confirm failure**

```bash
cd frontend && npm test -- src/components/srcc ; cd ..
```

Expected: FAIL — no element with `data-testid="tenant-proof"`.

- [ ] **Step 4: Modify `AppShell.tsx`**

Open `frontend/src/components/srcc/AppShell.tsx`. Add imports near the
top:

```tsx
import { useEffect, useRef, useState } from "react";
import { useTenant } from "@/contexts/TenantContext";
import { useApiClient, ApiError } from "@/lib/api/client";
import type { TenantRead } from "@/lib/api/types";
```

Inside the AppShell component body (above the existing return),
introduce the effect + state:

```tsx
const tenant = useTenant();
const client = useApiClient();
const hasRequestedTenantRef = useRef(false);
const [tenantError, setTenantError] = useState<ApiError | Error | null>(null);

useEffect(() => {
  if (hasRequestedTenantRef.current || tenant.id) return;
  hasRequestedTenantRef.current = true;
  client
    .get<TenantRead>("/tenants/me")
    .then(tenant.hydrate)
    .catch(setTenantError);
}, [client, tenant.id, tenant.hydrate]);

const tenantProofLabel = tenantError
  ? `Tenant: ${tenant.tenantSlug}; /tenants/me failed: ${tenantError.message}`
  : tenant.id
    ? `Tenant: ${tenant.displayName} (${tenant.tenantSlug}) — id ${tenant.id}`
    : `Tenant: ${tenant.tenantSlug} (loading…)`;
```

In the JSX return, insert just below the existing root opening tag (do
**not** change any existing structure):

```tsx
{import.meta.env.DEV && (
  <small
    data-testid="tenant-proof"
    style={{
      position: "fixed",
      bottom: 8,
      right: 8,
      fontSize: 11,
      opacity: 0.6,
      padding: "2px 6px",
      borderRadius: 4,
      background: "rgba(0,0,0,0.4)",
      color: "#fff",
      zIndex: 9999,
      pointerEvents: "none",
    }}
  >
    {tenantProofLabel}
  </small>
)}
```

> If the existing AppShell already uses Tailwind classes everywhere,
> replace the inline `style` block with the equivalent Tailwind classes
> (`fixed bottom-2 right-2 text-[11px] opacity-60 px-1.5 py-0.5 rounded
> bg-black/40 text-white z-50 pointer-events-none`). Either is
> acceptable.

- [ ] **Step 5: Run — confirm pass**

```bash
cd frontend && npm test -- src/components/srcc ; cd ..
```

Expected: 3 passing.

- [ ] **Step 6: Run the full frontend test suite**

```bash
cd frontend && npm test ; cd ..
```

Expected: all tests across `TenantContext`, `client`, and `AppShell`
pass.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/srcc/AppShell.tsx frontend/src/components/srcc/__tests__/AppShell.test.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): AppShell calls /tenants/me on mount with dev-only proof tag

useEffect fires once per page load (StrictMode re-entry guarded via
hasRequestedTenantRef). Success hydrates TenantContext; failure renders
the typed ApiError message in the dev-only tag. Production builds
(import.meta.env.DEV === false) render no tag.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 5.3: Vite dev proxy for trusted-gateway header injection

**Files:**
- Modify: `frontend/vite.config.ts`

The browser must never see `X-UMS-Trusted-Gateway-Token`. In dev, the
Vite proxy is the local gateway shim — it forwards `/tenants/me` (and
any future tenant-scoped routes) to FastAPI and injects the gateway
identity headers from the Node process environment.

- [ ] **Step 1: Update `frontend/vite.config.ts`**

Current contents (14 lines):

```ts
import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
});
```

Updated:

```ts
import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const TENANT_SCOPED_ROUTES = ["/tenants"];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backendTarget = env.VITE_DEV_BACKEND_URL ?? "http://127.0.0.1:8000";
  const gatewayUserId =
    env.VITE_DEV_GATEWAY_USER_ID ?? "00000000-0000-0000-0000-0000000000aa";
  const gatewayToken =
    env.VITE_DEV_GATEWAY_TOKEN ?? env.UMS_TRUSTED_GATEWAY_TOKEN ?? "";

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": fileURLToPath(new URL("./src", import.meta.url)),
      },
    },
    server: {
      proxy: Object.fromEntries(
        TENANT_SCOPED_ROUTES.map((route) => [
          route,
          {
            target: backendTarget,
            changeOrigin: true,
            configure(proxy) {
              proxy.on("proxyReq", (proxyReq) => {
                if (gatewayUserId) proxyReq.setHeader("X-User-ID", gatewayUserId);
                if (gatewayToken)
                  proxyReq.setHeader("X-UMS-Trusted-Gateway-Token", gatewayToken);
              });
            },
          },
        ]),
      ),
    },
  };
});
```

- [ ] **Step 2: Build to verify no regression**

```bash
source ~/.bashrc && cd frontend && npm run build ; cd ..
```

Expected: build succeeds.

> The dev proxy only fires under `vite dev`. The `npm run build`
> step does not exercise it. Manual smoke (optional): start the
> backend on `127.0.0.1:8000`, run `npm run dev`, hit
> `http://localhost:5173/tenants/me` — the response is the bootstrap
> tenant payload. Skip if Phase 7 will do an end-to-end smoke instead.

- [ ] **Step 3: Commit**

```bash
git add frontend/vite.config.ts
git commit -m "$(cat <<'EOF'
feat(frontend): Vite dev proxy injects trusted-gateway headers

Local gateway shim: proxies /tenants/** to VITE_DEV_BACKEND_URL
(default http://127.0.0.1:8000) and injects X-User-ID +
X-UMS-Trusted-Gateway-Token from the Node process environment. The
browser never sees or sends the token; production must use the
deployed gateway for the same same-origin contract.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Phase 6 — Validation gate update

### Task 6.1: Vitest GateCommand + self-test (TDD)

**Files:**
- Modify: `backend/ums_smart_revenue/devtools/quality_gate.py`
- Modify: `tests/devtools/test_quality_gate.py`

- [ ] **Step 1: Update the failing self-test FIRST**

Open `tests/devtools/test_quality_gate.py`. The current
`test_gate_commands_cover_required_local_validation_contract` at line 63
asserts a 5-tuple. Update it to expect 6 tuples in the new order:

Add at the top of the file, next to other constants:

```python
import shutil

def _resolved_npm() -> str:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise RuntimeError("npm not found on PATH for test expectations")
    return npm
```

Replace the existing assertion body (lines 66-116) with the new 6-tuple
expectation that inserts a `Frontend tests (Vitest)` step between the
two pytest commands and the `git diff --check` commands:

```python
    assert commands == (
        GateCommand(
            label="Ruff backend, tests, and scripts",
            command=(
                sys.executable,
                "-B",
                "-P",
                "-c",
                RUFF_ENTRYPOINT,
                "check",
                "backend",
                "tests",
                "scripts",
            ),
        ),
        GateCommand(
            label="Pytest no skip or xfail policy",
            command=(
                sys.executable,
                "-B",
                "-P",
                "-c",
                PYTEST_POLICY_ENTRYPOINT,
            ),
        ),
        GateCommand(
            label="Pytest full suite",
            command=(
                sys.executable,
                "-B",
                "-P",
                "-c",
                PYTEST_ENTRYPOINT,
                "-q",
                "--strict-config",
                "--strict-markers",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                ".pytest-tmp",
            ),
        ),
        GateCommand(
            label="Frontend tests (Vitest)",
            command=(_resolved_npm(), "--prefix", "frontend", "run", "test"),
        ),
        GateCommand(
            label="Git diff whitespace check",
            command=("git", "diff", "--check"),
        ),
        GateCommand(
            label="Git staged diff whitespace check",
            command=("git", "diff", "--cached", "--check"),
        ),
    )
```

- [ ] **Step 2: Run the self-test — confirm failure (current build_gate_commands lacks the Vitest step)**

```bash
pytest tests/devtools/test_quality_gate.py::test_gate_commands_cover_required_local_validation_contract -q
```

Expected: FAIL, with the assertion showing the actual tuple is missing
the Vitest step.

- [ ] **Step 3: Update `quality_gate.py`**

Open `backend/ums_smart_revenue/devtools/quality_gate.py`. Add `import
shutil` near the top with the other stdlib imports. Add the resolver
helper above `build_gate_commands`:

```python
def _resolve_npm() -> str:
    """Resolve an npm executable. On Windows, npm is npm.cmd."""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise RuntimeError(
            "npm not found on PATH; install Node.js to run frontend tests."
        )
    return npm
```

Insert the new `GateCommand` into `build_gate_commands` after
`*build_test_gate_commands(python=python)` and before the two
`git diff --check` commands:

```python
def build_gate_commands(*, python: str = sys.executable) -> tuple[GateCommand, ...]:
    """Return the ordered validation commands for local quality gates."""
    return (
        GateCommand(
            label="Ruff backend, tests, and scripts",
            command=(
                python,
                "-B",
                "-P",
                "-c",
                RUFF_ENTRYPOINT,
                "check",
                "backend",
                "tests",
                "scripts",
            ),
        ),
        *build_test_gate_commands(python=python),
        GateCommand(
            label="Frontend tests (Vitest)",
            command=(_resolve_npm(), "--prefix", "frontend", "run", "test"),
        ),
        GateCommand(
            label="Git diff whitespace check",
            command=("git", "diff", "--check"),
        ),
        GateCommand(
            label="Git staged diff whitespace check",
            command=("git", "diff", "--cached", "--check"),
        ),
    )
```

- [ ] **Step 4: Run the self-test — confirm pass**

```bash
pytest tests/devtools/test_quality_gate.py -q
```

Expected: every test in `test_quality_gate.py` passes (including the
unchanged `test_test_gate_commands_are_strict_and_test_only` and
`test_run_gate_uses_repo_root_and_disables_bytecode`).

- [ ] **Step 5: Run the full gate end-to-end (this now includes the Vitest step)**

```bash
python scripts/run_validation_gate.py
```

Expected: every step green:
1. Ruff backend, tests, and scripts
2. Pytest no skip or xfail policy
3. Pytest full suite — 817 passed (808 baseline + 9 from `test_tenants_api.py`)
4. Frontend tests (Vitest) — TenantContext + client + AppShell suites all pass
5. Git diff whitespace check
6. Git staged diff whitespace check

If the Vitest step is slow (>30s) under a cold cache, accept it. If it
fails because the gate runner cannot find `npm`, re-check the
`_resolve_npm` shim and that `shutil.which` returns a non-None value
on this machine.

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/devtools/quality_gate.py tests/devtools/test_quality_gate.py
git commit -m "$(cat <<'EOF'
feat(devtools): add Frontend tests (Vitest) to local validation gate

Inserts a new GateCommand between the pytest steps and the diff checks
so the local gate proves frontend tests pass before push. Windows-safe
npm resolution via shutil.which("npm") or shutil.which("npm.cmd").
test_quality_gate.py self-test updated to pin the new order and label.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Phase 7 — Final integration, planning docs, PR

### Task 7.1: Update planning docs with the inline PR mark

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`
- Modify: `Docs/15_DELIVERY_BACKLOG.md`

Per the standing `feedback-per-pr-plan-status` rule, every PR updates
both docs inline.

- [ ] **Step 1: Open `Docs/01_IMPLEMENTATION_PLAN.md` and locate the S2 / 2026-05-22 catch-up subsection**

The PR # is unknown at edit time. Use a placeholder bullet — the actual
number is filled in after PR open in Task 7.5.

Add to the subsection (typically `### S0/S1 catch-up (2026-05-22)` or
the closest equivalent for Spec A):

```markdown
- ✅ PR #<NN> — Spec A frontend `X-UMS-Tenant` header foundation. Backend `GET /tenants/me` proof endpoint; React `TenantContext` seeded with bootstrap slug `"ums"`; `useApiClient()` thin fetch wrapper; AppShell dev-only proof tag; Vite dev proxy injects trusted-gateway headers; Vitest framework wired into the local validation gate. Closes S2 spec Phase 5.
```

- [ ] **Step 2: Open `Docs/15_DELIVERY_BACKLOG.md` and locate `Cross-cutting shipped`**

Add:

```markdown
- ✅ Frontend tenant-header foundation: `TenantContext`, `useApiClient`, `GET /tenants/me`, Vite dev gateway proxy, Vitest framework + validation-gate integration — PR #<NN>.
```

- [ ] **Step 3: Run the gate to confirm the doc edits are clean**

```bash
python scripts/run_validation_gate.py
```

Expected: green.

- [ ] **Step 4: Commit (placeholder PR number — replaced in Task 7.5)**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "$(cat <<'EOF'
docs(plan): mark Spec A frontend tenant-header PR inline

Per feedback-per-pr-plan-status, both 01_IMPLEMENTATION_PLAN.md and
15_DELIVERY_BACKLOG.md carry the inline mark. PR number placeholder
will be replaced once the PR is open.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 7.2: Write the `Docs/pulls/` triple

**Files:**
- Create: `Docs/pulls/2026-MM-DD-prNN-spec-a-frontend-tenant-header-report.md`
- Create: `Docs/pulls/2026-MM-DD-prNN-spec-a-frontend-tenant-header-changelog.md`
- Create: `Docs/pulls/2026-MM-DD-prNN-spec-a-frontend-tenant-header-handoff.md`

Use the PR #39 templates as the structural reference. The PR number
goes into the filename once the PR is open; for the commit-before-push
step, use today's `MM-DD` and `prNN`, then rename after push.

- [ ] **Step 1: Write `…-report.md`**

Contents must cover (mirror PR #39 report):
- Date / PR URL / branch / base SHA / status
- What was requested (per Spec A)
- What was actually done (per this plan)
- Phased execution table mirroring this plan's phases 0–7
- Quality checks performed (the gate, including the new Vitest step)
- Blast-radius statement (verbatim: `No graph projection impact detected.`)
- Pre-existing baseline (pytest count delta from 808 → 817)
- Validation that could NOT be run (none, ideally)
- Remaining risks (code, repo-size, license-compliance, reviewer-flow)
- Follow-up recommendations (Spec B/C/D from the conversation)
- Rollback notes (revert is `git revert <merge-commit>`)
- Open questions / decisions deferred

- [ ] **Step 2: Write `…-changelog.md`**

Mirror PR #39 changelog shape: Added / Changed / Removed sections.

- [ ] **Step 3: Write `…-handoff.md`**

Mirror PR #39 handoff: scope, non-goals, files changed table, files NOT
in this PR, behavior changes, tests run, failures / skipped gates,
risks, rollback / operational notes, next session / next PR
recommendations, open questions, validation a future maintainer can
rerun.

- [ ] **Step 4: Run the gate**

```bash
python scripts/run_validation_gate.py
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add Docs/pulls/2026-*spec-a-*.md
git commit -m "$(cat <<'EOF'
docs(pulls): triple for Spec A frontend tenant-header PR

Mirrors the PR #39 report/changelog/handoff template structure.
PR number tokens will be filled in after PR open.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Task 7.3: Final validation gate + diff hygiene re-check

**Files:** none

- [ ] **Step 1: Re-read the staged + unstaged diff**

```bash
git status -s
git log --oneline -20
```

Expected: clean working tree (modulo standing exclusions
`frontend/package-lock.json` was committed in Task 2.1, so it should
now be clean; `nul` may still be untracked). Branch log shows ~10
commits from Phase 1–7.

- [ ] **Step 2: Run the final validation gate**

```bash
python scripts/run_validation_gate.py
```

Expected: every step green, including the new Vitest step.

- [ ] **Step 3: If any step fails — STOP, fix, re-run. Do NOT push with a failing gate.**

### Task 7.4: Push the branch + open the PR

**Files:** none — remote action

- [ ] **Step 1: Pause for explicit operator authorization to push**

Surface a one-line summary: "Branch
`pr/spec-a-frontend-tenant-header` ready. ~10 commits across Phases
0–7. Validation gate green (including the new Vitest step). Push and
open PR?" Wait for explicit "yes".

- [ ] **Step 2: Push**

```bash
git push -u origin pr/spec-a-frontend-tenant-header
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create --base main \
  --title "feat: Spec A frontend X-UMS-Tenant header foundation" \
  --body "$(cat <<'EOF'
## Summary

- Closes S2 spec Phase 5. Implements the design at `Docs/superpowers/specs/2026-05-22-spec-a-frontend-tenant-header-design.md`.
- Backend: new `GET /tenants/me` thin route depending on the existing principal + resolver middleware stack; returns `TenantRead { id, slug, display_name }` from `TENANT_CTX`.
- Frontend: `TenantContext` seeded with bootstrap slug `"ums"`, `useApiClient()` thin `fetch` wrapper (X-UMS-Tenant set last, cannot be overridden), `ApiError` class, AppShell dev-only proof tag, Vite dev proxy that injects trusted-gateway headers (browser never sees the token).
- Tests: 9 backend cases from spec section 9.1, frontend Vitest suites for `TenantContext` + `useApiClient` + `AppShell` (including a StrictMode re-entry-guard assertion).
- Validation gate: new `Frontend tests (Vitest)` `GateCommand` inserted between the pytest steps and the `git diff --check` steps; self-test pins the new order and the `Frontend tests (Vitest)` label.

## Test plan

- [x] `python scripts/run_validation_gate.py` green end-to-end including the new Vitest step.
- [x] `cd frontend && npm test` green standalone.
- [x] Manual dev smoke: backend on `127.0.0.1:8000`, `npm run dev`, open `http://localhost:5173/`, dev-only proof tag renders `Tenant: UMS (ums) — id 00000000…0001` (optional but recommended).

## Blast radius

*No graph projection impact detected.* No SQLAlchemy ORM, no Alembic
migration, no existing route change. Frontend additions are net new
files plus minimal surgical edits to `main.tsx` and `AppShell.tsx`.
Lockfile change is a deliberate scope addition.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Capture the PR URL**

The `gh pr create` output is the PR URL — e.g.
`https://github.com/XGenerationy/Youtube/pull/NN`. Note `NN`.

### Task 7.5: Replace placeholder PR number tokens in committed docs

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`
- Modify: `Docs/15_DELIVERY_BACKLOG.md`
- Rename: `Docs/pulls/2026-MM-DD-prNN-…` → `Docs/pulls/2026-05-DD-prNN-…` (real values)

- [ ] **Step 1: Replace `<NN>` with the real PR number in `Docs/01_IMPLEMENTATION_PLAN.md` and `Docs/15_DELIVERY_BACKLOG.md`**

- [ ] **Step 2: Rename `Docs/pulls/2026-MM-DD-prNN-spec-a-…` files to use the real date and PR number**

```bash
git mv Docs/pulls/2026-MM-DD-prNN-spec-a-frontend-tenant-header-report.md \
       Docs/pulls/2026-05-DD-prNN-spec-a-frontend-tenant-header-report.md
# (etc. for changelog + handoff)
```

(Replace `DD` with the actual day; `NN` with the actual PR number from
Task 7.4.)

- [ ] **Step 3: Run the gate**

```bash
python scripts/run_validation_gate.py
```

- [ ] **Step 4: Commit + push**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md Docs/pulls/
git commit -m "$(cat <<'EOF'
docs: fill PR number placeholders for Spec A

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push
```

### Task 7.6: Pause for CR/Codex review, then merge

**Files:** none — remote action

- [ ] **Step 1: Pause until the operator surfaces CR/Codex review state**

Do not merge while review threads or failed checks are outstanding.
The standing rule is: re-check live PR state after the last push;
do not claim clean remote review state without current evidence.

- [ ] **Step 2: After explicit operator approval, merge from `main`**

```bash
git checkout main
git pull --ff-only
gh pr merge NN --merge   # NOT --squash, NOT --rebase
```

- [ ] **Step 3: Sync local main**

```bash
git pull --ff-only
git log --oneline -3
```

Expected: the spec-A merge commit is now at the tip of `main`.

- [ ] **Step 4: Delete the merged local branch**

```bash
git branch -D pr/spec-a-frontend-tenant-header
git fetch --prune origin
```

---

## Self-review checklist

Run this checklist after the plan is written. Fix issues inline.

**1. Spec coverage:** Does every section of the spec map to a task?

| Spec section | Covered by |
|---|---|
| §1 problem statement | Phase 1 + Phase 3 + Phase 4 + Phase 5 |
| §2 goals | every Phase end-state |
| §3 non-goals | Honored throughout — no tenant switcher, no auth, no retry, no observability, no `/api/v1`, no axios, no `test:ui`, no caching, no lockfile-out rule |
| §4 approach (Approach 1 single PR) | All phases land on one branch |
| §5 architecture | Phase 1 backend + Phase 5 frontend |
| §6.1 backend route | Task 1.2 |
| §6.2 app.py registration | Task 1.2 step 5 |
| §6.3 backend tests | Tasks 1.1–1.5 |
| §6.4 frontend new modules | Tasks 3.1, 4.1, 4.2 |
| §6.5 frontend modified | Tasks 5.1, 5.2, 5.3, 2.1 |
| §6.6 validation gate | Task 6.1 |
| §7 data flow | Verified by Task 5.2 happy-path test + Task 1.2 backend case 1 |
| §8.1 backend error tree | Tasks 1.3, 1.4, 1.5 |
| §8.2 frontend error tree | Task 4.1 (ApiError cases) + Task 5.2 (AppShell error rendering) |
| §8.3 degradation | Task 5.2 (proof tag continues to ship `tenantSlug` even on failure) |
| §9.1 case 1–9 | Tasks 1.2, 1.3, 1.4, 1.5 |
| §9.2 framework setup | Tasks 2.1, 2.2 |
| §9.3 frontend tests | Tasks 3.1, 4.1+4.2, 5.2 |
| §9.4 gate update | Task 6.1 |
| §10 blast radius | Re-asserted in Task 7.2 (report) |
| §11 validation commands | Task 7.3 |
| §12 rollback notes | Task 7.6 |
| §13 open questions | Out-of-scope; documented in spec only |
| §14 done definition | Phase 7 checklist |

No gaps detected.

**2. Placeholder scan:** Search for red flags.

- "TBD", "TODO": only in intentional `<NN>` / `MM-DD` / `DD` filename placeholders, all flagged for replacement in Task 7.5.
- "implement later": none.
- "add appropriate error handling" without code: none (every error path has a concrete test + code).
- "Similar to Task N": none (every task block contains its own complete code).

**3. Type consistency:** Method / property names match across tasks.

- `tenantSlug` (camelCase) used in TenantContext, useApiClient, AppShell. Consistent.
- `display_name` (snake_case) preserved across backend `TenantRead` Pydantic model and frontend `TenantRead` TS type and `hydrate({ display_name })` payload. Backend ↔ frontend wire is snake_case; React state stores camelCase `displayName`. Hydrate explicitly maps the two. Consistent across Tasks 1.2, 3.1, 4.2, 5.2.
- `hydrate` callback signature `(payload: { id, slug, display_name })` matches across `TenantContext.tsx` and AppShell call site. Consistent.
- `_gateway_headers()` helper signature `(user_id: UUID = TEST_USER_ID) -> dict[str, str]` matches across Tasks 1.1, 1.2, 1.5. Consistent.
- `GateCommand.label = "Frontend tests (Vitest)"` matches between `quality_gate.py` (Task 6.1 step 3) and `test_quality_gate.py` (Task 6.1 step 1). Consistent.

No drifts detected.

---

## Execution

Plan complete and saved to `Docs/superpowers/plans/2026-05-22-spec-a-frontend-tenant-header.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

Which approach?
