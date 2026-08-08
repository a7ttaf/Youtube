-- UMS Smart Revenue Control Center security foundation.
-- PostgreSQL is the source of truth for users, roles, scoped permissions, and audit logs.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text NOT NULL,
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'service')),
    is_service_account boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower
    ON users(lower(email));

CREATE TABLE IF NOT EXISTS roles (
    "key" text PRIMARY KEY,
    label text NOT NULL,
    description text NOT NULL,
    service_only boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS permissions (
    "key" text PRIMARY KEY,
    label text NOT NULL,
    "sensitive" boolean NOT NULL DEFAULT false,
    audit_on_use boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS access_scopes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type text NOT NULL CHECK (
        scope_type IN (
            'global',
            'sector',
            'company',
            'channel',
            'finance-month',
            'export',
            'connector'
        )
    ),
    scope_id text NULL,
    label text NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (scope_type = 'global' AND scope_id IS NULL)
        OR (scope_type <> 'global' AND scope_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_access_scopes_scope_type_scope_id
    ON access_scopes(scope_type, scope_id)
    WHERE scope_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_access_scopes_global_singleton
    ON access_scopes(scope_type)
    WHERE scope_type = 'global' AND scope_id IS NULL;

CREATE TABLE IF NOT EXISTS role_permission_assignments (
    role_key text NOT NULL REFERENCES roles("key") ON DELETE CASCADE,
    permission_key text NOT NULL REFERENCES permissions("key") ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (role_key, permission_key)
);

CREATE TABLE IF NOT EXISTS user_role_assignments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_key text NOT NULL REFERENCES roles("key") ON DELETE RESTRICT,
    scope_id uuid NOT NULL REFERENCES access_scopes(id) ON DELETE RESTRICT,
    assigned_by uuid NULL REFERENCES users(id) ON DELETE SET NULL,
    assigned_at timestamptz NOT NULL DEFAULT now(),
    revoked_by uuid NULL REFERENCES users(id) ON DELETE SET NULL,
    revoked_at timestamptz NULL,
    reason text NULL,
    active boolean NOT NULL DEFAULT true,
    CHECK (
        (active = true AND revoked_at IS NULL)
        OR (active = false AND revoked_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_user_role_scope
    ON user_role_assignments(user_id, role_key, scope_id)
    WHERE active = true;

CREATE INDEX IF NOT EXISTS ix_user_role_assignments_user_id
    ON user_role_assignments(user_id);

CREATE TABLE IF NOT EXISTS user_permission_grants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission_key text NOT NULL REFERENCES permissions("key") ON DELETE RESTRICT,
    scope_id uuid NOT NULL REFERENCES access_scopes(id) ON DELETE RESTRICT,
    granted_by uuid NULL REFERENCES users(id) ON DELETE SET NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_by uuid NULL REFERENCES users(id) ON DELETE SET NULL,
    revoked_at timestamptz NULL,
    reason text NULL,
    active boolean NOT NULL DEFAULT true,
    CHECK (
        (active = true AND revoked_at IS NULL)
        OR (active = false AND revoked_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_user_permission_scope
    ON user_permission_grants(user_id, permission_key, scope_id)
    WHERE active = true;

CREATE INDEX IF NOT EXISTS ix_user_permission_grants_user_id
    ON user_permission_grants(user_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NULL REFERENCES users(id) ON DELETE SET NULL,
    event_type text NOT NULL,
    entity_type text NULL,
    entity_id text NULL,
    scope_type text NULL,
    scope_id text NULL,
    request_id text NULL,
    reason text NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    "sensitive" boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_audit_logs_user_created
    ON audit_logs(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_audit_logs_event_created
    ON audit_logs(event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_audit_logs_entity
    ON audit_logs(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS api_connector_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_key text NOT NULL,
    account_id text NOT NULL,
    encrypted_secret_ref text NOT NULL,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'rotating', 'failed_auth')),
    created_by uuid NULL REFERENCES users(id) ON DELETE SET NULL,
    updated_by uuid NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (connector_key, account_id)
);

CREATE INDEX IF NOT EXISTS ix_api_connector_credentials_connector_key
    ON api_connector_credentials(connector_key);

