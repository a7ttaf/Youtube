# SQL / Warehouse Data Model

## Core app tables

```sql
users (
  id uuid primary key,
  email text not null,
  display_name text not null,
  status text not null default 'active',
  is_service_account boolean not null default false,
  created_at timestamp not null default now(),
  updated_at timestamp not null default now()
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
- `ck_users_service_account_status` enforces the service-account/status invariant:
  service accounts may use `service` or `disabled`, and human accounts may use
  `active` or `disabled`.
- `updated_at` is DB-defaulted and refreshed by the ORM when account rows change.

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
  month text not null,
  payment_name text not null,
  payment_date date not null,
  payment_amount numeric(18, 6) not null,
  payment_currency text not null,
  payment_status text not null default 'PAID',
  raw_payload jsonb not null,
  source_report_id text null,
  imported_by uuid null,
  imported_at timestamp not null default now(),
  updated_at timestamp not null default now(),
  unique (month, payment_name)
);

bank_reconciliation_entries (
  id uuid primary key,
  month text not null,
  bank_reference text not null,
  bank_received_date date not null,
  bank_received_amount numeric(18, 6) not null,
  bank_received_currency text not null,
  bank_received_amount_usd numeric(18, 6) not null,
  transfer_fee_usd numeric(18, 6) not null default 0,
  fx_difference_usd numeric(18, 6) not null default 0,
  notes text null,
  source_report_id text null,
  recorded_by uuid not null,
  recorded_at timestamp not null default now(),
  updated_at timestamp not null default now(),
  unique (month, bank_reference)
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
-- AdSense payment sync is idempotent by `(month, payment_name)` and is blocked
-- when the matching finance month is locked. Payment rows are official payment
-- metadata only; financial source-of-truth calculations remain in SQL revenue
-- fact/reconciliation tables, not Neo4j.
-- Bank reconciliation entries are manually supplied finance receipt metadata.
-- They store finance-normalized USD receipt values and are blocked when the
-- matching finance month is locked. Month-level bank gaps are derived in SQL
-- services from paid USD AdSense payment rows versus normalized bank receipts;
-- transfer/FX gaps are not allocated to channels in this phase.

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

Implementation note:
The first net-revenue API is read-only and does not persist this
`channel_net_revenue` table yet. It derives month/channel summaries from
`monthly_channel_revenue_facts.net_revenue_usd` and approved
`revenue_manual_overrides` only. Channels whose primary source has no official
net value are reported as missing source data rather than backfilled from tax,
payment, bank, or allocation assumptions.

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
The backend foundation records export job requests as queued metadata. It can generate an on-demand `FINANCE_EXCEL` workbook response from guarded SQL-backed preview data, but it does not persist generated files, update `file_url`, generate PDF or slide files, or calculate revenue values during export request creation.
`EXECUTIVE_PDF` export jobs can also be rendered on demand from the same guarded
SQL-backed source summaries. Generated PDF bytes are response-only in this
phase; `file_url`, `completed_at`, and job status are not updated until a
persistent artifact store/job runner is introduced.
`BRANDED_SLIDE_PACK` export jobs can also be rendered on demand as a PPTX deck
from the same guarded SQL-backed source summaries. Generated slide-pack bytes
are response-only in this phase; `file_url`, `completed_at`, and job status are
not updated until a persistent artifact store/job runner is introduced.
