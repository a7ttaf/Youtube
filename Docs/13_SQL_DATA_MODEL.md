# SQL / Warehouse Data Model

## Core app tables

```sql
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

## Revenue tables

```sql
revenue_monthly_channel (
  month date,
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
  month date,
  channel_id uuid,
  tax_withheld_usd numeric,
  tax_rate numeric,
  source_type text,
  confidence text,
  primary key (month, channel_id)
);

adsense_payments (
  id uuid primary key,
  month date,
  payment_name text,
  payment_date date,
  payment_amount numeric,
  payment_currency text,
  raw_payload jsonb,
  created_at timestamp
);

finance_month_close (
  month date primary key,
  adsense_payment_amount_usd numeric,
  bank_received_amount numeric,
  bank_currency text,
  fx_rate numeric,
  transfer_fee_usd numeric,
  manual_adjustment_usd numeric,
  unresolved_gap_usd numeric,
  allocation_method text,
  status text,
  locked_by uuid null,
  locked_at timestamp null
);

-- Phase 1 control-plane implementation note:
-- The backend stores month as YYYY-MM text for API consistency and adds
-- allocation_rule_payload, unlocked_by, unlocked_at, and updated_at control
-- columns. Revenue fact tables remain separate and are not calculated by the
-- close-control API.

channel_net_revenue (
  month date,
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
  month date,
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

manual_overrides (
  id uuid primary key,
  month date,
  entity_type text,
  entity_id text,
  field_name text,
  old_value text,
  new_value text,
  reason text,
  created_by uuid,
  approved_by uuid null,
  created_at timestamp
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

## Raw report storage

```sql
raw_report_files (
  id uuid primary key,
  source text,
  report_type text,
  report_month date,
  file_url text,
  checksum text,
  downloaded_at timestamp,
  parse_status text
);
```
