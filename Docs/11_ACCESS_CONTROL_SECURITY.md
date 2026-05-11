# Access Control and Security

## Purpose
Control who can view analytics, finance, exports, configuration, and graph views.

## Roles

```text
SUPER_OWNER
CORPORATE_ADMIN
FINANCE_ADMIN
FINANCE_APPROVER
FINANCE_VIEWER
TV_SECTOR_MANAGER
NEWS_SECTOR_MANAGER
COMPANY_MANAGER
CHANNEL_MANAGER
ASSISTANT_ANALYST
EXPORT_OPERATOR
AUDIT_VIEWER
SYSTEM_INTEGRATION_USER
CONNECTOR_ADMIN
DATA_STEWARD
```

## Access matrix

| Capability | Owner | Finance | Sector | Company | Assistant |
|---|---:|---:|---:|---:|---:|
| View all channels | Yes | Yes | Sector only | Company only | Assigned only |
| View gross revenue | Yes | Yes | Sector only | Company only | Optional |
| View net revenue | Yes | Yes | Optional | Optional | No by default |
| Edit hierarchy | Yes | No | No | No | No |
| Run month close | Yes | Yes | No | No | No |
| Lock month | Yes | Yes | No | No | No |
| Export finance report | Yes | Yes | No | No | No |
| Export analytics report | Yes | Yes | Yes | Yes | Yes if assigned |
| Manage users | Yes | No | No | No | No |

## Security rules

- OAuth tokens are encrypted.
- Finance values are role-restricted.
- Production authorization loads active roles, direct permission grants, and scopes from SQL using `UMS_AUTHZ_SOURCE=database`.
- Every export is logged.
- Every manual override is logged.
- Month unlock requires reason.
- Graph views respect same backend permissions.
- Neo4j direct access is restricted to admins and read-only graph users.

## Authorization modes

`UMS_AUTHZ_SOURCE` controls where route authorization loads the active principal:

- `database`: production mode. The trusted gateway supplies the authenticated user identity, and the API loads active role assignments, direct permission grants, and scopes from SQL. Header-provided role and scope claims are ignored.
- `headers`: bootstrap/development mode. This is the default when `UMS_AUTHZ_SOURCE` is unset. The API trusts header-provided roles and scopes for local setup, deterministic tests, and early bootstrap work only.

Enable `UMS_AUTHZ_SOURCE=database` after users, roles, permissions, and scopes are configured in SQL, the trusted gateway is sending stable user IDs, and `UMS_TRUSTED_GATEWAY_TOKEN` is provisioned for the API and gateway. Missing trusted-gateway token configuration is a hard pre-deployment failure: protected routes return 503 until the token is configured. Switching from bootstrap/header mode to database mode is a breaking authorization behavior change: previously accepted header role/scope claims no longer grant access, and disabled or unregistered SQL users fail closed before route handlers run.

Database-mode principal reads run inside a loader-owned `SERIALIZABLE` transaction and PostgreSQL deployments set a transaction-local statement timeout before reading user, role, permission, and scope rows. Calls made with a pre-existing active session transaction fail closed so weaker caller isolation cannot bypass the principal-read contract. Transient SQLAlchemy storage failures are rolled back and retried once; persistent storage failures, corrupt stored authorization data, and unexpected loader errors fail closed with 503 responses.

## Neo4j roles

```text
neo4j_sync_writer     # only sync job can write graph projection
neo4j_dashboard_reader # dashboard read-only access
neo4j_admin           # technical admin only
```

## Audit events

The catalog below mirrors the current emitted event constants in `backend/ums_smart_revenue/auth/audit.py`.

```text
LOGIN
LOGOUT
CHANNEL_CREATED
CHANNEL_UPDATED
GROUP_UPDATED
REPORT_IMPORTED
ADSENSE_PAYMENT_SYNCED
MONTH_CLOSE_UPDATED
MONTH_LOCKED
MONTH_UNLOCKED
MANUAL_OVERRIDE_CREATED
MANUAL_OVERRIDE_APPROVED
ALLOCATION_RULE_CHANGED
EXPORT_CREATED
USER_ROLE_CHANGED
USER_PERMISSION_CHANGED
CONNECTOR_JOB_RUN
CONNECTOR_SETTINGS_CHANGED
RAW_FILE_VIEWED
REVENUE_VIEWED
GRAPH_FINANCE_VIEWED
AUDIT_LOG_VIEWED
```

## Acceptance checks

- Company user cannot see another company by default.
- Assistant cannot see finance unless explicitly granted.
- Finance export is impossible without finance/owner role.
- Every override and export appears in audit log.
- Audit log reads require `audit.view`; sensitive audit details remain masked unless `audit.view_sensitive_payloads` is also granted.
- Direct permission grants require `roles.assign`, are family-restricted, and create `USER_PERMISSION_CHANGED` audit events.
- SQL-backed principals ignore header role/scope claims and reject disabled or unregistered users before any route handlers are invoked.
