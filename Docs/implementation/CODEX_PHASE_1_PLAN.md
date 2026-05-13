# UMS Phase 1 Authorization Foundation Plan

## Context
The current repository started as the UMS Smart Revenue Control Center specification pack and now contains a Python/FastAPI backend foundation with PostgreSQL as source of truth. Neo4j and graph projections are retired from the active architecture.

## Assumptions
- PostgreSQL or warehouse tables remain the financial source of truth.
- No Neo4j, graph database, graph-read scope, or graph-specific permission is part of the active foundation.
- This task creates a backend authorization foundation, not a complete app server.
- Frontend does not exist yet, so UI-facing permission metadata will be exported from backend constants.
- Alembic migration scaffolding is now present for the backend foundation; standalone SQL files remain as readable starter artifacts.
- Existing spec files are under `Docs/` in this workspace, even though the requested logical paths use `/docs/`.

## Implementation Scope
1. Create a role and permission design for an internal finance/revenue platform.
2. Create a detailed permission matrix with scopes, inheritance, and audit requirements.
3. Add a dependency-light Python backend package containing role enums, permission enums, scoped access checks, API guard helpers, audit event definitions, seed role definitions, and UI metadata.
4. Add PostgreSQL schema and seed SQL files for users, roles, permissions, scoped assignments, and audit logs.
5. Add pytest coverage for positive and negative authorization cases.

## Continuation Scope
- Add SQLAlchemy-backed repositories where the first implementation used bootstrap in-memory stores.
- Keep PostgreSQL/warehouse tables as source of truth and use repository-backed FastAPI dependency overrides for production-style runs.
- Preserve in-memory bootstrap dependencies for deterministic spec tests and local mockup/demo work.

## File Plan
- `Docs/security/ROLE_PERMISSION_MODEL.md`: role model, scopes, sensitive action policy, audit rules.
- `Docs/security/PERMISSION_MATRIX.md`: matrix of roles to permissions and scope defaults.
- `backend/ums_smart_revenue/auth/roles.py`: role identifiers and role metadata.
- `backend/ums_smart_revenue/auth/permissions.py`: permission identifiers and sensitivity metadata.
- `backend/ums_smart_revenue/auth/scopes.py`: scoped access model and scope matching.
- `backend/ums_smart_revenue/auth/models.py`: principal, assignment, and grant data models.
- `backend/ums_smart_revenue/auth/policy.py`: testable permission helper functions.
- `backend/ums_smart_revenue/auth/audit.py`: sensitive audit event definitions.
- `backend/ums_smart_revenue/auth/seed.py`: initial role and permission assignments.
- `backend/ums_smart_revenue/auth/api_guards.py`: framework-ready route guard helpers.
- `backend/ums_smart_revenue/auth/ui_metadata.py`: frontend-facing role and permission metadata.
- `backend/ums_smart_revenue/db/security_schema.sql`: starter PostgreSQL security schema.
- `backend/ums_smart_revenue/db/security_seed.sql`: starter seed rows for roles and permissions.
- `tests/auth/test_policy.py`: permission behavior tests.
- `Docs/implementation/CODEX_PHASE_1_SUMMARY.md`: end-of-task summary.

## Validation Plan
- Run `pytest`.
- Verify negative cases explicitly:
  - assistant cannot view finance by default;
  - company manager cannot view another company;
  - export operator cannot change allocation rules;
  - finance viewer cannot lock or unlock a month;
  - retired graph permissions are absent from the active role catalog.
