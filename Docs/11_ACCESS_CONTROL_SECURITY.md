# Access Control and Security

## Purpose
Control who can view analytics, finance, exports, configuration, and graph views.

## Roles

```text
SUPER_OWNER
CORPORATE_ADMIN
FINANCE_ADMIN
FINANCE_VIEWER
TV_SECTOR_MANAGER
NEWS_SECTOR_MANAGER
COMPANY_MANAGER
ASSISTANT_ANALYST
EXPORT_OPERATOR
AUDIT_VIEWER
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
- Every export is logged.
- Every manual override is logged.
- Month unlock requires reason.
- Graph views respect same backend permissions.
- Neo4j direct access is restricted to admins and read-only graph users.

## Neo4j roles

```text
neo4j_sync_writer     # only sync job can write graph projection
neo4j_dashboard_reader # dashboard read-only access
neo4j_admin           # technical admin only
```

## Audit events

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
EXPORT_CREATED
USER_ROLE_CHANGED
```

## Acceptance checks

- Company user cannot see another company by default.
- Assistant cannot see finance unless explicitly granted.
- Finance export is impossible without finance/owner role.
- Every override and export appears in audit log.
