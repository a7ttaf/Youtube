-- Initial UMS security seed data.
-- Safe to re-run after security_schema.sql.
--
-- RUNTIME CONTRACT (Track-E, post-FORCE migration 20260612_0002):
-- This seed writes to FORCEd RLS tenant-scoped tables (access_scopes,
-- user_role_assignments, user_permission_grants, role_permission_assignments,
-- permissions, roles). The tenant_id-scoped policies are now in force for the
-- table owner too, so a non-superuser non-BYPASSRLS table owner MUST either:
--   (a) run as a superuser / BYPASSRLS role, OR
--   (b) SET LOCAL app.current_tenant_id to a real tenant id before this
--       transaction (the table owner is subject to FORCE), OR
--   (c) the script-runner is the table owner who issues the
--       `SET LOCAL row_security = OFF;` shim below inside the same
--       transaction (the per-session table-owner bypass is exactly what
--       FORCE turns off, but `row_security` remains an explicit override
--       for the owner). Superusers and BYPASSRLS roles are unaffected.
-- The Python code path (backend/ums_smart_revenue/auth/seed.py + a migration
-- loader) is the in-repo source of truth; this .sql file is documentation
-- for an operator-run reseed and uses the SET LOCAL escape hatch so a
-- non-superuser owner can re-run it cleanly.

-- Owner-bypass escape hatch for the FORCEd tenant tables: the table owner
-- running this script would otherwise be subject to `tenant_id =
-- app_current_tenant_id()` and see NULL, which rejects every row. The shim
-- only affects the current transaction; it is a no-op for superusers and
-- BYPASSRLS roles.
SET LOCAL row_security = OFF;

INSERT INTO access_scopes (scope_type, scope_id, label)
VALUES ('global', NULL, 'Global')
ON CONFLICT DO NOTHING;

DELETE FROM user_role_assignments
WHERE scope_id IN (SELECT id FROM access_scopes WHERE scope_type = 'graph-read');

DELETE FROM user_permission_grants
WHERE scope_id IN (SELECT id FROM access_scopes WHERE scope_type = 'graph-read')
   OR permission_key IN ('graph.view', 'graph.view_finance');

DELETE FROM role_permission_assignments
WHERE permission_key IN ('graph.view', 'graph.view_finance');

DELETE FROM permissions
WHERE key IN ('graph.view', 'graph.view_finance');

DELETE FROM access_scopes
WHERE scope_type = 'graph-read';

INSERT INTO roles (key, label, description, service_only)
VALUES
    ('super_owner', 'Super Owner', 'Global break-glass owner with every platform and finance permission.', false),
    ('corporate_admin', 'Corporate Admin', 'Global platform administrator without default finance visibility.', false),
    ('revenue_operations_admin', 'Revenue Operations Admin', 'Global operational admin for ingestion, registry quality, and analytics.', false),
    ('finance_admin', 'Finance Admin', 'Finance owner for revenue, reconciliation, overrides, and month close.', false),
    ('finance_approver', 'Finance Approver', 'Second-control approver for finance overrides and month unlocks.', false),
    ('finance_viewer', 'Finance Viewer', 'Read-only finance role for granted organization scopes.', false),
    ('tv_sector_manager', 'TV Sector Manager', 'Sector-scoped management role for TV analytics and report operations.', false),
    ('news_sector_manager', 'News Sector Manager', 'Sector-scoped management role for News analytics and report operations.', false),
    ('company_manager', 'Company Manager', 'Company-scoped manager for assigned company analytics.', false),
    ('channel_manager', 'Channel Manager', 'Channel-scoped manager for assigned channel analytics.', false),
    ('assistant_analyst', 'Assistant Analyst', 'Assigned-scope analyst for analytics without default finance access.', false),
    ('export_operator', 'Export Operator', 'Scoped export operator for approved analytics or finance exports.', false),
    ('audit_viewer', 'Audit Viewer', 'Read-only audit and compliance reviewer.', false),
    ('system_integration_user', 'System Integration User', 'Non-human role for scheduled connector jobs and backend service flows.', true),
    ('connector_admin', 'Connector Admin', 'Technical owner for OAuth/API connector configuration.', false),
    ('data_steward', 'Data Steward', 'Scoped owner for channel registry, groups, and organization mapping.', false)
ON CONFLICT (key) DO UPDATE
SET label = EXCLUDED.label,
    description = EXCLUDED.description,
    service_only = EXCLUDED.service_only;

INSERT INTO permissions (key, label, sensitive, audit_on_use)
VALUES
    ('analytics.view', 'View performance analytics', false, false),
    ('analytics.view_confidence', 'View confidence labels and issue flags', false, false),
    ('finance.view_revenue', 'View revenue values', true, true),
    ('finance.view_finalized_payments', 'View finalized payments', true, true),
    ('finance.view_bank_reconciliation', 'View bank reconciliation', true, true),
    ('finance.manage_bank_reconciliation', 'Manage bank reconciliation', true, true),
    ('finance.create_manual_override', 'Create manual override', true, true),
    ('finance.approve_manual_override', 'Approve manual override', true, true),
    ('finance.lock_month', 'Lock finance month', true, true),
    ('finance.unlock_month', 'Unlock finance month', true, true),
    ('finance.change_allocation_rule', 'Change allocation rule', true, true),
    ('exports.analytics', 'Export analytics report', true, true),
    ('exports.revenue', 'Export revenue report', true, true),
    ('exports.manage_templates', 'Manage export templates', true, true),
    ('registry.manage_channels', 'Manage channel registry', true, true),
    ('registry.manage_org_mapping', 'Manage organization mapping', true, true),
    ('registry.manage_groups', 'Manage channel groups', true, true),
    ('connectors.view_health', 'View connector health', false, false),
    ('connectors.run_jobs', 'Run connector jobs', true, true),
    ('connectors.manage', 'Manage connectors', true, true),
    ('raw_files.view', 'View raw report files', true, true),
    ('audit.view', 'View audit log', true, true),
    ('audit.view_sensitive_payloads', 'View sensitive audit payloads', true, true),
    ('users.manage', 'Manage users', true, true),
    ('roles.assign', 'Assign roles', true, true),
    ('platform.manage_settings', 'Manage platform settings', true, true)
ON CONFLICT (key) DO UPDATE
SET label = EXCLUDED.label,
    sensitive = EXCLUDED.sensitive,
    audit_on_use = EXCLUDED.audit_on_use;

INSERT INTO role_permission_assignments (role_key, permission_key)
SELECT 'super_owner' AS role_key, key AS permission_key FROM permissions
ON CONFLICT DO NOTHING;

INSERT INTO role_permission_assignments (role_key, permission_key)
VALUES
    ('corporate_admin', 'analytics.view'),
    ('corporate_admin', 'analytics.view_confidence'),
    ('corporate_admin', 'exports.analytics'),
    ('corporate_admin', 'exports.manage_templates'),
    ('corporate_admin', 'registry.manage_channels'),
    ('corporate_admin', 'registry.manage_org_mapping'),
    ('corporate_admin', 'registry.manage_groups'),
    ('corporate_admin', 'connectors.view_health'),
    ('corporate_admin', 'audit.view'),
    ('corporate_admin', 'users.manage'),
    ('corporate_admin', 'roles.assign'),
    ('corporate_admin', 'platform.manage_settings'),
    ('revenue_operations_admin', 'analytics.view'),
    ('revenue_operations_admin', 'analytics.view_confidence'),
    ('revenue_operations_admin', 'exports.analytics'),
    ('revenue_operations_admin', 'registry.manage_channels'),
    ('revenue_operations_admin', 'registry.manage_org_mapping'),
    ('revenue_operations_admin', 'registry.manage_groups'),
    ('revenue_operations_admin', 'connectors.view_health'),
    ('revenue_operations_admin', 'connectors.run_jobs'),
    ('finance_admin', 'analytics.view'),
    ('finance_admin', 'analytics.view_confidence'),
    ('finance_admin', 'finance.view_revenue'),
    ('finance_admin', 'finance.view_finalized_payments'),
    ('finance_admin', 'finance.view_bank_reconciliation'),
    ('finance_admin', 'finance.manage_bank_reconciliation'),
    ('finance_admin', 'finance.create_manual_override'),
    ('finance_admin', 'finance.approve_manual_override'),
    ('finance_admin', 'finance.lock_month'),
    ('finance_admin', 'finance.unlock_month'),
    ('finance_admin', 'finance.change_allocation_rule'),
    ('finance_admin', 'exports.analytics'),
    ('finance_admin', 'exports.revenue'),
    ('finance_admin', 'audit.view'),
    ('finance_admin', 'roles.assign'),
    ('finance_approver', 'analytics.view'),
    ('finance_approver', 'analytics.view_confidence'),
    ('finance_approver', 'finance.view_revenue'),
    ('finance_approver', 'finance.view_finalized_payments'),
    ('finance_approver', 'finance.view_bank_reconciliation'),
    ('finance_approver', 'finance.manage_bank_reconciliation'),
    ('finance_approver', 'finance.approve_manual_override'),
    ('finance_approver', 'finance.unlock_month'),
    ('finance_approver', 'finance.change_allocation_rule'),
    ('finance_approver', 'exports.revenue'),
    ('finance_approver', 'audit.view'),
    ('finance_viewer', 'analytics.view'),
    ('finance_viewer', 'analytics.view_confidence'),
    ('finance_viewer', 'finance.view_revenue'),
    ('finance_viewer', 'finance.view_finalized_payments'),
    ('finance_viewer', 'finance.view_bank_reconciliation'),
    ('tv_sector_manager', 'analytics.view'),
    ('tv_sector_manager', 'analytics.view_confidence'),
    ('tv_sector_manager', 'exports.analytics'),
    ('news_sector_manager', 'analytics.view'),
    ('news_sector_manager', 'analytics.view_confidence'),
    ('news_sector_manager', 'exports.analytics'),
    ('company_manager', 'analytics.view'),
    ('company_manager', 'analytics.view_confidence'),
    ('company_manager', 'exports.analytics'),
    ('channel_manager', 'analytics.view'),
    ('channel_manager', 'analytics.view_confidence'),
    ('channel_manager', 'exports.analytics'),
    ('assistant_analyst', 'analytics.view'),
    ('assistant_analyst', 'analytics.view_confidence'),
    ('export_operator', 'analytics.view'),
    ('export_operator', 'analytics.view_confidence'),
    ('export_operator', 'exports.analytics'),
    ('audit_viewer', 'audit.view'),
    ('system_integration_user', 'connectors.view_health'),
    ('system_integration_user', 'connectors.run_jobs'),
    ('connector_admin', 'connectors.view_health'),
    ('connector_admin', 'connectors.run_jobs'),
    ('connector_admin', 'connectors.manage'),
    ('connector_admin', 'raw_files.view'),
    ('data_steward', 'analytics.view'),
    ('data_steward', 'analytics.view_confidence'),
    ('data_steward', 'registry.manage_channels'),
    ('data_steward', 'registry.manage_org_mapping'),
    ('data_steward', 'registry.manage_groups')
ON CONFLICT DO NOTHING;

