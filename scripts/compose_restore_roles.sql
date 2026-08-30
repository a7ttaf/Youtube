-- ============================================================================
-- Purpose: Pre-create the two NOLOGIN RLS roles required by a database restore.
-- Database/ORM: PostgreSQL pg_roles; app_tenant and app_platform cluster roles.
-- Standards: Idempotent creation and fail-closed privilege drift validation.
-- Blast Radius: Authorization; creates roles but grants no login or membership.
-- Connections:
--   - File: backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py -> Canonical role contract.
--   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> Recovery ordering and checks.
-- ============================================================================
DO $ums_roles$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['app_tenant', 'app_platform']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOBYPASSRLS NOREPLICATION',
                role_name
            );
        END IF;

        IF EXISTS (
            SELECT 1
            FROM pg_roles
            WHERE rolname = role_name
              AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                   OR rolbypassrls OR rolreplication)
        ) THEN
            RAISE EXCEPTION 'unsafe privilege drift on required role %', role_name;
        END IF;
    END LOOP;
END
$ums_roles$;
