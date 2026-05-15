# Channel Registry and Groups

## Purpose
Maintain a single master registry for all UMS YouTube channels and flexible groups.

## Requirements

- Support 300+ channels.
- Support channels inside and outside CMS.
- Support dynamic company/sector mapping.
- Support custom groups without changing database design.
- Support revenue-required flag.
- Support confidence/source status.

## Persisted channel status fields

```text
youtube_channel_id
channel_name
primary_org_unit_id
cms_status
content_owner_id
revenue_required
revenue_source_status
active
```

`primary_company_id` and `primary_sector_id` are derived UI/access-index fields from `primary_org_unit_id`, not persisted channel columns.

## CMS status values

```text
INSIDE_CMS
OUTSIDE_CMS
UNKNOWN
```

## Revenue source status

```text
OFFICIAL_CMS_REVENUE
OFFICIAL_MANUAL_IMPORT
ALLOCATED_FROM_PAYMENT_POOL
PERFORMANCE_ONLY
MISSING_REVENUE_SOURCE
```

The channel registry API carries `content_owner_id` and
`revenue_source_status` through both in-memory and SQL-backed registry stores.
The outside-CMS monitor uses these fields to show revenue-required channels
that still need an official revenue source without exposing financial amounts.

## Group types

```text
HOLDING
SECTOR
COMPANY
TV_BRAND
NEWS_BRAND
CUSTOM_GROUP
FINANCE_GROUP
SEASONAL_GROUP
```

## Group behavior

- Channel can have one primary company.
- Channel can belong to many custom groups.
- Group can be used as dashboard filter.
- Group can be used as export scope.
- Group can be used as allocation scope.

## Smart checks

- Channel has no company.
- Channel has no sector.
- Channel outside CMS but revenue required.
- Channel has revenue but no group.
- Channel duplicated under conflicting primary companies.
- Channel inactive but still appears in monthly report.

Current foundation endpoint:

```text
GET /channels/issues
```

The implemented foundation covers registry/group metadata checks available from
SQL channel and group tables: missing company, missing sector,
revenue-required outside-CMS channels, and revenue-required channels with no
active group. Revenue-fact-backed checks such as "has revenue but no group" and
"inactive but appears in monthly report" must be added when the issue feed is
joined to monthly revenue facts.

## Acceptance checks

- User can create a group and add/remove channels.
- User can filter dashboard by group.
- User can export by group.
- Outside-CMS channels are visible in a dedicated monitor.
