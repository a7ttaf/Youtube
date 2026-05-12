# UMS Permission Matrix

## Permission Keys

| Permission | Description | Sensitive |
|---|---|---:|
| `analytics.view` | View non-financial performance analytics. | No |
| `analytics.view_confidence` | View confidence labels and issue flags. | No |
| `finance.view_revenue` | View gross, finalized, allocated, or net revenue. | Yes |
| `finance.view_finalized_payments` | View AdSense/payment status and finalized payment amounts. | Yes |
| `finance.view_bank_reconciliation` | View bank received amounts, FX, transfer fees, and gaps. | Yes |
| `finance.manage_bank_reconciliation` | Record or update finance-provided bank reconciliation receipt metadata. | Yes |
| `finance.create_manual_override` | Create manual finance overrides. | Yes |
| `finance.approve_manual_override` | Approve manual finance overrides. | Yes |
| `finance.lock_month` | Lock a finance month. | Yes |
| `finance.unlock_month` | Unlock a finance month. | Yes |
| `finance.change_allocation_rule` | Change allocation or deduction rules. | Yes |
| `exports.analytics` | Export non-financial analytics reports. | Yes |
| `exports.revenue` | Export revenue or finance reports. | Yes |
| `exports.manage_templates` | Manage export templates and branding. | Yes |
| `registry.manage_channels` | Create/update channel registry records. | Yes |
| `registry.manage_org_mapping` | Change channel/company/sector mapping. | Yes |
| `registry.manage_groups` | Manage dynamic channel groups. | Yes |
| `connectors.view_health` | View connector health and run status. | No |
| `connectors.run_jobs` | Run ingestion/sync jobs. | Yes |
| `connectors.manage` | Manage OAuth/API connector configuration. | Yes |
| `raw_files.view` | View raw API payloads and raw report files. | Yes |
| `graph.view` | View Neo4j graph views through backend proxy. | No |
| `graph.view_finance` | View graph nodes or edges containing money values. | Yes |
| `audit.view` | View audit log entries. | Yes |
| `audit.view_sensitive_payloads` | View unmasked sensitive audit payloads. | Yes |
| `users.manage` | Create, update, disable, or reactivate user accounts. | Yes |
| `roles.assign` | Assign or revoke user roles. | Yes |
| `platform.manage_settings` | Manage platform-level settings. | Yes |

## Role Matrix

| Role | Default Scope | Key Permissions |
|---|---|---|
| Super Owner | `global` | All permissions. |
| Corporate Admin | `global` | Analytics, confidence, graph, users, role assignment below Super Owner, platform settings, registry, groups, export templates, audit metadata. No finance by default. |
| Revenue Operations Admin | `global` | Analytics, confidence, connector health, run connector jobs, registry/groups, raw parser errors when granted. No payment/bank finance by default. |
| Finance Admin | `global` or assigned finance scope | Revenue, payments, bank reconciliation, manual overrides, allocation rules, month lock/unlock, finance exports, finance graph, audit finance events. |
| Finance Approver | `global` or `finance-month` | Approve overrides, approve allocation changes, unlock month, view finance needed for approval. |
| Finance Viewer | `global`, `sector`, `company`, or `finance-month` | Read-only revenue, finalized payments, bank reconciliation, finance graph, finance export history. Cannot lock/unlock or edit. |
| TV Sector Manager | `sector:TV` | TV analytics, confidence, graph, analytics exports. Finance only by explicit direct grant or paired finance role. |
| News Sector Manager | `sector:NEWS` | News analytics, confidence, graph, analytics exports. Finance only by explicit direct grant or paired finance role. |
| Company Manager | `company` | Company analytics, confidence, graph, analytics exports. No cross-company visibility. Finance only by explicit grant. |
| Channel Manager | `channel` | Channel analytics, confidence, graph, analytics export for assigned channels. No finance by default. |
| Assistant Analyst | Assigned `sector`, `company`, or `channel` | Analytics, confidence, graph for assigned scope. No raw files, revenue, payments, bank data, or finance exports by default. |
| Export Operator | Assigned export and data scope | Create analytics exports; create revenue exports only where `finance.view_revenue` is also granted. Cannot edit finance rules. |
| Audit Viewer | `global` or assigned scope | View audit logs and masked sensitive events. Unmasked payloads require separate permission. |
| System Integration User | `connector` or `global` service scope | Run connector jobs, write ingestion audit events through backend services, view connector health. No dashboard login. |
| Connector Admin | `connector` or `global` | Manage connector configuration, rotate tokens, run jobs, view health and raw connector diagnostics. No finance by default. |
| Data Steward | `global`, `sector`, or `company` | Manage channel registry, org mapping, groups, outside-CMS classifications. No finance by default. |

## Special Restrictions
- Super Owner role assignment requires an existing Super Owner.
- Corporate Admin can assign operational roles but cannot grant finance permissions unless also Finance Admin or Super Owner.
- Finance Admin can assign finance roles but cannot assign Super Owner.
- Direct permission grants require `roles.assign`, a compatible scope type, and family-specific grant authority.
- User account create/update requires `users.manage`, but service account lifecycle changes require Super Owner.
- User account list and access-profile reads require `users.manage`; list responses use bounded cursor pagination and profile responses show active role assignments and direct grants only.
- Finance direct grants require Finance Admin or Super Owner; connector/raw-file direct grants require Connector Admin or Super Owner; administrative direct grants require Super Owner.
- Finalized-payment and bank-reconciliation permissions may be granted on organization scopes or a specific `finance-month`; month-scoped grants do not imply another month.
- Export Operator requires underlying view permission for the export scope.
- Graph `revenue-flow` views require both `graph.view` and `graph.view_finance`.
- Raw report files require `raw_files.view` even when the user can view normalized analytics.
- Service users cannot be used for browser/dashboard sessions.
- Locked month values are immutable until a permitted unlock with a reason is audited.

## Default Role Permission Summary

| Role | Analytics | Finance | Payments | Bank Rec | Overrides | Month Lock | Allocation | Registry | Exports | Connectors | Raw Files | Graph | Audit | Users/Roles |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Super Owner | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Corporate Admin | Yes | No | No | No | No | No | No | Yes | Analytics/templates | Health only | No | Non-finance | Metadata | Yes |
| Revenue Operations Admin | Yes | No | No | No | No | No | No | Yes | Analytics | Run jobs | Optional | Non-finance | Operational | No |
| Finance Admin | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Read | Finance | Health | Optional | Finance | Finance | Finance roles |
| Finance Approver | Yes | Yes | Yes | Yes | Approve | Unlock | Approve/change | Read | Finance | No | No | Finance | Finance | No |
| Finance Viewer | Yes | Read | Read | Read | No | No | No | Read | View/export if granted | No | No | Finance | No | No |
| TV Sector Manager | Sector | Optional | Optional | Optional | No | No | No | Read | Analytics | No | No | Sector | No | No |
| News Sector Manager | Sector | Optional | Optional | Optional | No | No | No | Read | Analytics | No | No | Sector | No | No |
| Company Manager | Company | Optional | Optional | Optional | No | No | No | Read | Analytics | No | No | Company | No | No |
| Channel Manager | Channel | No | No | No | No | No | No | Read | Analytics | No | No | Channel | No | No |
| Assistant Analyst | Assigned | No | No | No | No | No | No | Read | Analytics | No | No | Assigned | No | No |
| Export Operator | Assigned | If granted | If granted | If granted | No | No | No | Read | Yes | No | No | Assigned | Export events | No |
| Audit Viewer | No | Masked if not granted | Masked if not granted | Masked if not granted | No | No | No | No | History | No | No | No | Yes | No |
| System Integration User | Service | No dashboard | No dashboard | No dashboard | No | No | No | Service writes | No | Run jobs | Service only | No | Service events | No |
| Connector Admin | No | No | No | No | No | No | No | No | No | Yes | Diagnostics | No | Connector events | No |
| Data Steward | Scoped | No | No | No | No | No | No | Yes | No | No | No | Scoped hierarchy | Mapping events | No |
