-- Initial UMS security seed data.
-- Safe to re-run against a database migrated to Alembic head.
--
-- RUNTIME CONTRACT (Track-E, post-FORCE migration 20260612_0002):
-- After migration 20260612_0002 the tenant-scoped tables carry FORCE ROW LEVEL
-- SECURITY, so their owner is policy-subject too. The tenant-scoped writes in
-- this script (access_scopes, user_role_assignments, user_permission_grants)
-- are therefore governed by `tenant_id = app_current_tenant_id()`; the global
-- reference writes (roles, permissions, role_permission_assignments) are NOT
-- in TENANT_SCOPED_TABLES and are unaffected by FORCE.
--
-- Run this raw reseed only as a privileged catalog-maintenance role (the
-- application app_tenant/app_platform lanes deliberately cannot mutate the
-- roles/permissions catalogs) and only after establishing the target tenant on
-- the SAME database backend with the privileged, parameterized setter, e.g.
--     SELECT set_app_current_tenant_id('<validated-tenant-uuid>'::uuid);
-- The access_scopes INSERT below reads that trusted backend context instead of
-- the transitional UMS tenant server default. Missing context therefore fails
-- closed at the tenant_id NOT NULL boundary, including for a superuser whose
-- RLS bypass would otherwise hide the mistake.
--
-- NOTE: `SET row_security = OFF` is NOT a bypass -- PostgreSQL raises an error
-- instead of filtering when a policy would otherwise apply, and `SET LOCAL` is
-- a no-op outside an explicit transaction block, so neither helps an owner
-- reseed FORCEd tables. Run the setter + this file in one transaction with the
-- client configured to stop on the first error; otherwise a statement failure
-- can leave a partial catalog refresh. For tenant bootstrap, prefer
-- scripts/bootstrap_operator.py, which validates --tenant and creates the
-- global scope on demand through the tenant-aware role repository.

INSERT INTO access_scopes (tenant_id, scope_type, scope_id, label)
VALUES (app_current_tenant_id(), 'global', NULL, 'Global')
ON CONFLICT (tenant_id, scope_type)
WHERE scope_type = 'global' AND scope_id IS NULL
DO UPDATE SET label = excluded.label;

DELETE FROM user_role_assignments
WHERE scope_id IN
(SELECT id FROM access_scopes WHERE scope_type = 'graph-read');

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
('super_owner',
'Super Owner',
'Global break-glass owner with every platform and finance permission.',
FALSE),
('corporate_admin',
'Corporate Admin',
'Global platform administrator without default finance visibility.',
FALSE),
('revenue_operations_admin',
'Revenue Operations Admin',
'Global operational admin for ingestion, registry quality, and analytics.',
FALSE),
('finance_admin',
'Finance Admin',
'Finance owner for revenue, reconciliation, overrides, and month close.',
FALSE),
('beta_operator',
'Beta Operator',
'First-beta finance operator with manual revenue-upload access.',
FALSE),
('finance_approver',
'Finance Approver',
'Second-control approver for finance overrides and month unlocks.',
FALSE),
('finance_viewer',
'Finance Viewer',
'Read-only finance role for granted organization scopes.',
FALSE),
('tv_sector_manager',
'TV Sector Manager',
'Sector-scoped management role for TV analytics and report operations.',
FALSE),
('news_sector_manager',
'News Sector Manager',
'Sector-scoped management role for News analytics and report operations.',
FALSE),
('company_manager',
'Company Manager',
'Company-scoped manager for assigned company analytics.',
FALSE),
('channel_manager',
'Channel Manager',
'Channel-scoped manager for assigned channel analytics.',
FALSE),
('assistant_analyst',
'Assistant Analyst',
'Assigned-scope analyst for analytics without default finance access.',
FALSE),
('export_operator',
'Export Operator',
'Scoped export operator for approved analytics or finance exports.',
FALSE),
('audit_viewer',
'Audit Viewer',
'Read-only audit and compliance reviewer.',
FALSE),
('system_integration_user',
'System Integration User',
'Non-human role for scheduled connector jobs and backend service flows.',
TRUE),
('connector_admin',
'Connector Admin',
'Technical owner for Google/API connector credential configuration.',
FALSE),
('data_steward',
'Data Steward',
'Scoped owner for channel registry, groups, and organization mapping.',
FALSE)
ON CONFLICT (key) DO UPDATE
SET label = excluded.label,
description = excluded.description,
service_only = excluded.service_only;

INSERT INTO permissions (key, label, sensitive, audit_on_use)
VALUES
('analytics.view', 'View performance analytics', FALSE, FALSE),
('analytics.view_confidence',
'View confidence labels and issue flags',
FALSE,
FALSE),
('finance.view_revenue', 'View revenue values', TRUE, TRUE),
('finance.view_finalized_payments', 'View finalized payments', TRUE, TRUE),
('finance.view_bank_reconciliation', 'View bank reconciliation', TRUE, TRUE),
('finance.manage_bank_reconciliation',
'Manage bank reconciliation',
TRUE,
TRUE),
('finance.create_manual_override', 'Create manual override', TRUE, TRUE),
('finance.approve_manual_override', 'Approve manual override', TRUE, TRUE),
('finance.lock_month', 'Lock finance month', TRUE, TRUE),
('finance.unlock_month', 'Unlock finance month', TRUE, TRUE),
('finance.change_allocation_rule', 'Change allocation rule', TRUE, TRUE),
('finance.import_manual_revenue', 'Import manual revenue facts', TRUE, TRUE),
('exports.analytics', 'Export analytics report', TRUE, TRUE),
('exports.revenue', 'Export revenue report', TRUE, TRUE),
('exports.manage_templates', 'Manage export templates', TRUE, TRUE),
('registry.manage_channels', 'Manage channel registry', TRUE, TRUE),
('registry.manage_org_mapping', 'Manage organization mapping', TRUE, TRUE),
('registry.manage_groups', 'Manage channel groups', TRUE, TRUE),
('connectors.view_health', 'View connector health', FALSE, FALSE),
('connectors.run_jobs', 'Run connector jobs', TRUE, TRUE),
('connectors.manage', 'Manage connectors', TRUE, TRUE),
('raw_files.view', 'View raw report files', TRUE, TRUE),
('audit.view', 'View audit log', TRUE, TRUE),
('audit.view_sensitive_payloads', 'View sensitive audit payloads', TRUE, TRUE),
('users.manage', 'Manage users', TRUE, TRUE),
('roles.assign', 'Assign roles', TRUE, TRUE),
('platform.manage_settings', 'Manage platform settings', TRUE, TRUE)
ON CONFLICT (key) DO UPDATE
SET label = excluded.label,
sensitive = excluded.sensitive,
audit_on_use = excluded.audit_on_use;

-- FIX: An earlier PR #223 draft mapped beta_operator to connectors.run_jobs,
-- which authorizes every connector rather than the bounded manual-revenue flow.
-- Remove that exact unsafe draft edge so a manually re-run seed converges.
DELETE FROM role_permission_assignments
WHERE role_key = 'beta_operator'
  AND permission_key = 'connectors.run_jobs';

INSERT INTO role_permission_assignments (role_key, permission_key)
SELECT
'super_owner' AS role_key,
key AS permission_key
FROM permissions
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
('beta_operator', 'analytics.view'),
('beta_operator', 'analytics.view_confidence'),
('beta_operator', 'finance.view_revenue'),
('beta_operator', 'finance.view_finalized_payments'),
('beta_operator', 'finance.view_bank_reconciliation'),
('beta_operator', 'finance.manage_bank_reconciliation'),
('beta_operator', 'finance.create_manual_override'),
('beta_operator', 'finance.approve_manual_override'),
('beta_operator', 'finance.lock_month'),
('beta_operator', 'finance.unlock_month'),
('beta_operator', 'finance.change_allocation_rule'),
('beta_operator', 'finance.import_manual_revenue'),
('beta_operator', 'exports.analytics'),
('beta_operator', 'exports.revenue'),
('beta_operator', 'audit.view'),
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
