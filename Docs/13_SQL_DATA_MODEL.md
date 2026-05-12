# SQL / Warehouse Data Model

## Core app tables

```sql
users (
  id uuid primary key,
  email text not null,
  display_name text not null,
  status text not null default 'active',
  is_service_account boolean not null default false,
  created_at timestamp,
  updated_at timestamp
);

org_units (
  id uuid primary key,
  parent_id uuid null,
  type text,
  name text,
  active boolean,
  created_at timestamp,
  updated_at timestamp
);

youtube_channels (
  id uuid primary key,
  youtube_channel_id text unique,
  channel_name text,
  primary_org_unit_id uuid,
  cms_status text,
  content_owner_id text null,
  revenue_required boolean,
  revenue_source_status text,
  active boolean,
  created_at timestamp,
  updated_at timestamp
);

channel_groups (
  id uuid primary key,
  name text,
  group_type text,
  active boolean,
  created_at timestamp
);

channel_group_members (
  group_id uuid,
  channel_id uuid,
  primary key (group_id, channel_id)
);
```

User table constraints:

- `status` is constrained to `active`, `disabled`, or `service`.
- `uq_users_email_lower` enforces case-insensitive uniqueness on `lower(email)`.
- Service users are represented by `is_service_account=true` and `status='service'`
  unless explicitly disabled.

## Revenue tables

Canonical persisted month values use `text` in `YYYY-MM` format across operational, revenue, explanation, raw-report, and export tables. API payloads use the same representation.

```sql
revenue_monthly_channel (
  month text,
  channel_id uuid,
  gross_revenue_usd numeric,
  adjustments_usd numeric,
  shorts_revenue_usd numeric,
  longform_revenue_usd numeric,
  subscription_revenue_usd numeric,
  source_type text,
  confidence text,
  source_report_id uuid,
  primary key (month, channel_id)
);

tax_monthly_channel (
  month text,
  channel_id uuid,
  tax_withheld_usd numeric,
  tax_rate numeric,
  source_type text,
  confidence text,
  primary key (month, channel_id)
);

adsense_payments (
  id uuid primary key,
  month text,
  payment_name text,
  payment_date date,
  payment_amount numeric,
  payment_currency text,
  raw_payload jsonb,
  created_at timestamp
);

finance_month_close (
  month text primary key,
  status text,
  allocation_method text,
  allocation_rule_payload jsonb,
  locked_by uuid null,
  locked_at timestamp null,
  unlocked_by uuid null,
  unlocked_at timestamp null,
  updated_at timestamp
);

-- Phase 1 control-plane implementation note:
-- The backend stores month as YYYY-MM text for API consistency and adds
-- allocation_rule_payload, unlocked_by, unlocked_at, and updated_at control
-- columns. Revenue fact tables remain separate and are not calculated by the
-- close-control API.

channel_net_revenue (
  month text,
  channel_id uuid,
  gross_revenue_usd numeric,
  tax_usd numeric,
  allocated_deductions_usd numeric,
  manual_adjustment_usd numeric,
  net_revenue_usd numeric,
  deduction_percentage numeric,
  confidence text,
  locked boolean,
  primary key (month, channel_id)
);
```

## Explanation and audit tables

```sql
number_explanations (
  id uuid primary key,
  month text,
  entity_type text,
  entity_id text,
  metric text,
  value numeric,
  currency text,
  formula text,
  confidence text,
  components jsonb,
  warnings jsonb,
  created_at timestamp
);

revenue_manual_overrides (
  id uuid primary key,
  month text,
  youtube_channel_id text,
  adjustment_revenue_usd numeric,
  reason text,
  status text,
  created_by uuid,
  approved_by uuid null,
  approved_at timestamp null,
  approval_reason text null,
  created_at timestamp,
  updated_at timestamp
);

audit_logs (
  id uuid primary key,
  user_id uuid,
  event_type text,
  entity_type text,
  entity_id text,
  details jsonb,
  created_at timestamp
);
```

Implementation note:
The backend foundation stores explanation months as `YYYY-MM` text, records deterministic explanation snapshots keyed by month/entity/metric, and derives values from stored revenue facts plus approved manual overrides. Explanation snapshots are not a substitute for source revenue facts.

## Raw report storage

```sql
raw_report_files (
  id uuid primary key,
  source text,
  report_type text,
  report_month text,
  file_url text,
  checksum text,
  downloaded_at timestamp,
  parse_status text
);
```

Implementation note:
The backend foundation stores `report_month` as `YYYY-MM` text for API consistency and stores object-storage metadata only. Raw file contents and Google credentials are not stored in this table.

## Export job metadata

```sql
export_jobs (
  id uuid primary key,
  export_type text,
  scope_type text,
  scope_id text null,
  month text,
  currency text,
  requested_by uuid,
  status text,
  file_url text null,
  month_lock_status text,
  include_confidence_notes boolean,
  include_manual_override_notes boolean,
  created_at timestamp,
  completed_at timestamp null,
  updated_at timestamp
);
```

Implementation note:
The backend foundation records export job requests as queued metadata. It does not generate workbook, PDF, or slide files yet, and it does not calculate revenue values during export request creation.
