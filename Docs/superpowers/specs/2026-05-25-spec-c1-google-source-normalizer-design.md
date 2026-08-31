# Spec C1 - Google Source Rows to Revenue Facts Normalizer - Design Spec

**Date:** 2026-05-25
**Owner:** Director Software Architect / Operator
**Status:** Design lock - bridges PR #43 (`google_revenue_source_rows`) to the
existing `MonthlyChannelRevenueFactORM` contract.
**Primary docs:**
`Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md`,
`Docs/12_BACKEND_API_SPEC.md`,
`Docs/18_MULTI_CURRENCY_ENGINE.md`,
`backend/ums_smart_revenue/finance/revenue_facts.py`

---

## 1. Problem statement

PR #43 (Spec B1) shipped the `google_revenue_source_rows` substrate: a
tenant-scoped, multi-currency, idempotent landing zone for Google / YouTube /
AdSense reported monetary source rows. PR #43 also shipped three deterministic
parsers (YouTube Reporting, YouTube Analytics, AdSense Management) and a
PostgreSQL-backed migration round-trip.

What PR #43 does **not** do: turn those source rows into entries in the
existing `MonthlyChannelRevenueFactORM` table. Downstream finance code
(`finance.month_close_readiness`, `finance.explanations`, future
`finance.net_revenue`, future export) reads `revenue_facts`, not source rows.
Until a normalization bridge exists, the substrate is dark - source rows can
land but no consumer of `revenue_facts` sees them.

Spec C1 is that bridge. It reads `google_revenue_source_rows` for a given
(tenant, month), selects one canonical row per
(`youtube_channel_id`, `source_system`) group, and writes one
`MonthlyChannelRevenueFactORM` entry per eligible group via the existing
`SqlAlchemyRevenueFactRepository.record_fact()` write path. The write path is
preserved unchanged because it already enforces locked-month, active-channel,
tenant scope, and payload validation; C1 must not bypass any of that.

C1 is intentionally scoped as a normalization bridge only. It does not fetch
data from Google (B2 owns the live OAuth/API connector). It does not convert
currencies or introduce FX policy (B3 owns FX). It does not redefine the
`revenue_facts` schema, the `RevenueFactSourceKind` enum, or any existing
finance contract.

The strategic reason to deliver C1 before B2: a live connector that
successfully fetches Google data but cannot be consumed by `revenue_facts`
gives no business signal. Proving the downstream normalization contract first
means B2 can land with confidence that its output is consumable end-to-end.

## 2. Goals

- Add a single normalization service that converts
  `google_revenue_source_rows` into `MonthlyChannelRevenueFactORM` entries
  for a given (tenant, month).
- Reuse the existing `SqlAlchemyRevenueFactRepository.record_fact()` write
  path; never write to `MonthlyChannelRevenueFactORM` directly.
- Apply a fixed canonical-metric rule per `source_system` to collapse a
  source-rows group to one fact (no metric summing - that would
  double-count).
- Enforce USD-only writes; preserve all non-USD source rows untouched in
  `google_revenue_source_rows` and report them as
  `NON_USD_CURRENCY` skips.
- Make replay deterministically idempotent: byte-identical re-runs produce
  zero writes; only payload changes cause `UPDATED`.
- Classify each eligible group as `CREATED`, `UPDATED`, or `UNCHANGED` via
  read-before-write payload comparison.
- Keep country-dimensional YouTube Analytics rows as persisted evidence, but
  exclude them from canonical fact projection and classify them with the
  explicit `NON_PROJECTING_EVIDENCE` skip reason.
- Fail closed on malformed YouTube Analytics `raw_payload` or `dimensions`
  containers with the explicit `MALFORMED_SOURCE_PAYLOAD` skip reason.
- Classify each rejected row with an explicit `SkipReason` code.
- Fail loud on closed books: if the month is `LOCKED` for the tenant, raise
  `RevenueFactLockedMonthError` upfront and write nothing.
- Stay database-schema-stable: no new tables, no new columns, no database enum
  values, no new Alembic migration, no new exception classes.
- Provide a deterministic reverse lookup so a future explain endpoint can map
  a written fact back to the canonical source row.

## 3. Non-goals

- No OAuth flow, no credential storage, no live Google API client. **B2.**
- No FX rates, no currency conversion, no display-currency normalization.
  **B3.**
- No changes to `google_revenue_source_rows` schema (PR #43 substrate is
  final for C1).
- No changes to `MonthlyChannelRevenueFactORM` schema. No new
  `RevenueFactSourceKind` enum values.
- No new Alembic migration. Zero schema delta.
- No new exception classes. Reuse `RevenueFactLockedMonthError` and
  `RevenueFactValidationError`.
- No confidence-score policy beyond `Decimal("1.0")`. The dedicated
  confidence-labels spec owns scoring rules and any historical backfill.
- No net revenue calculation. `finance/net_revenue.py` owns that.
- No Shorts / longform / subscription revenue breakdown. C1 leaves those
  columns `None`; a future detail-revenue spec owns format-level
  decomposition.
- No connector-storage-side trigger or hook. The normalizer is invoked
  explicitly by callers; it never auto-fires from
  `SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many()`.
- No scheduler, cron, or background job registration.
- No FastAPI route exposure. A future spec wraps `normalize_month()` in an
  endpoint.
- No retry framework, exponential backoff, or partial-batch retry. Exceptions
  propagate; the caller decides.
- No frontend surface.
- No Neo4j graph projection update. Neo4j is retired from the active
  architecture (PR #12).
- No backfill of historical pre-PR-#43 data. PR #43 is the source-row
  substrate; historical re-ingestion is a separate operations exercise.

## 4. Public surface

### Layer placement

```
backend/ums_smart_revenue/finance/google_source_normalizer.py   (new file)
```

The service lives under `finance/`, not `connectors/`. It writes finance facts
and depends on `finance.revenue_facts` and `finance.month_close`. PR #43's
connector parsers stay storage-only.

### Public symbols

```python
class SkipReason(StrEnum):
    NON_USD_CURRENCY       = "non_usd_currency"
    MISSING_CHANNEL_ID     = "missing_channel_id"
    UNSUPPORTED_VALUE_KIND = "unsupported_value_kind"
    NON_CANONICAL_METRIC   = "non_canonical_metric"
    NON_PROJECTING_EVIDENCE = "non_projecting_evidence"
    MALFORMED_SOURCE_PAYLOAD = "malformed_source_payload"
    UNKNOWN_CHANNEL        = "unknown_channel"
    NO_CANONICAL_ROW       = "no_canonical_row"


@dataclass(frozen=True)
class SkippedSourceRow:
    source_row_id: str
    reason: SkipReason


@dataclass(frozen=True)
class NormalizationResult:
    created:   list[RevenueFactEntry]
    updated:   list[RevenueFactEntry]
    unchanged: list[RevenueFactEntry]
    skipped:   list[SkippedSourceRow]


SOURCE_SYSTEM_TO_SOURCE_KIND: Mapping[str, RevenueFactSourceKind] = MappingProxyType({
    "youtube_reporting":  RevenueFactSourceKind.YOUTUBE_CMS,
    "youtube_analytics":  RevenueFactSourceKind.YOUTUBE_ANALYTICS,
    "adsense_management": RevenueFactSourceKind.ADSENSE,
})


CANONICAL_METRIC_RULE: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "youtube_reporting":  ("estimatedRevenue",),
    "youtube_analytics":  ("estimatedRevenue",),
    "adsense_management": ("PAID_AMOUNT", "ESTIMATED_EARNINGS"),  # first present wins
})


def select_canonical_row(
    rows: list[GoogleRevenueSourceRowEntry],
) -> tuple[GoogleRevenueSourceRowEntry | None, list[GoogleRevenueSourceRowEntry]]:
    """Apply the per-source_system rule to a homogeneous USD-only group.

    Currency-blind by design - caller must pre-filter to USD. The tie-break
    across multiple rows with the same `metric_key` is deterministic by
    `source_row_key` ascending. Multiple same-metric_key rows can occur for
    reasons beyond currency (dimension breakdowns, distinct
    `source_account_id`, parallel report shapes); repository ingested_at
    order is not a stable contract.

    Returns (canonical_or_None, non_canonical_rest).
    """


class GoogleSourceNormalizer:
    def __init__(
        self,
        session: Session,
        *,
        tenant_id: UUID | str | None = None,
    ) -> None: ...

    def normalize_month(
        self,
        *,
        month: str,
        channel_ids: list[str] | None = None,
        actor_user_id: str,
    ) -> NormalizationResult: ...
```

### Write path

Writes go exclusively through
`SqlAlchemyRevenueFactRepository.record_fact()` in
`backend/ums_smart_revenue/finance/revenue_facts.py`. This preserves
existing locked-month, active-channel, tenant, and value validation. The
relevant guards in that module are (cited by function name to stay stable
against line drift):

- `_require_month_open` - locked-month refusal (secondary guard behind C1's
  Step 1 upfront check).
- `_require_active_channel_for_import` - rejects writes for inactive /
  unknown channels (secondary guard behind C1's Step 4 pre-filter).
- `_validate_month` - YYYY-MM format check (raises
  `RevenueFactValidationError`); C1 also invokes this implicitly via Step 0.
- `_validate_metrics`, `_validate_revenue_amounts` - payload value validation
  (finite decimals, non-negative, confidence in `[0, 1]`, etc.).
- `_normalize_source_kind` - enum coercion / validation for `source_kind`.
- `_actor_identity_uuid` - actor identity normalization (raises
  `RevenueFactValidationError` for malformed actor identities).

C1 introduces no bypass path.

### Tenant resolution

The constructor's `tenant_id` follows the existing `_resolve_tenant_id`
pattern shared across `finance/revenue_facts.py`,
`finance/bank_reconciliation.py`, `finance/adsense_payments.py`,
`finance/manual_overrides.py`, `finance/month_close.py`,
`finance/month_close_readiness.py`, and `finance/explanations.py`:
explicit arg, then `TENANT_CTX`, then default UMS tenant. No special
`RuntimeError` on missing tenant; behavior matches every other finance
service.

### channel_ids contract

The public type is `list[str] | None` to remain ergonomic for JSON-ish
callers, but is normalized to `set[str]` internally for deduplication.

### Field defaults for `record_fact()` calls

| `record_fact` argument | Value sourced by C1 |
|---|---|
| `month` | `normalize_month(month=...)` argument |
| `youtube_channel_id` | canonical row's `youtube_channel_id` |
| `source_kind` | `SOURCE_SYSTEM_TO_SOURCE_KIND[canonical.source_system]` |
| `source_report_id` | canonical row's `source_report_id` |
| `gross_revenue_usd` | canonical row's `amount_native` (USD-only by Step 6d filter) |
| `net_revenue_usd` | `None` (owned by `finance/net_revenue.py`) |
| `shorts_revenue_usd` | `None` |
| `longform_revenue_usd` | `None` |
| `subscription_revenue_usd` | `None` |
| `views` | `0` |
| `watch_time_minutes` | `Decimal("0")` |
| `confidence_score` | `Decimal("1.0")` |
| `actor_user_id` | `normalize_month(actor_user_id=...)` argument |

## 5. Data flow

`GoogleSourceNormalizer.normalize_month(month, channel_ids=None, *, actor_user_id) -> NormalizationResult`

Single SQLAlchemy session, single transaction (caller-managed).

```
Step 0 - Input normalization
  | Validate `month` against MONTH_PATTERN (YYYY-MM).
  |   Invalid format raises RevenueFactValidationError, matching the
  |   existing finance month validator (not plain ValueError).
  | channel_ids -> set[str] | None (drop duplicates; None preserved).

Step 1 - Upfront locked-month gate
  | Use the existing month-close contract:
  |     close_row = get_or_create_month_close_row(
  |         session, month, tenant_id=self._tenant_id, for_update=True
  |     )
  | This acquires the finance-month advisory lock
  | (pg_advisory_xact_lock at backend/ums_smart_revenue/finance/month_close.py:166)
  | plus SELECT ... FOR UPDATE on the close row
  | (backend/ums_smart_revenue/finance/month_close.py:174). It may create a
  | new OPEN close row even when there are zero eligible source rows; that is
  | acceptable for an explicit normalization attempt and gives the cleanest
  | concurrency story.
  | If close_row.status == "LOCKED":
  |     raise RevenueFactLockedMonthError immediately (write nothing).
  | record_fact() remains the secondary guard if a future code path bypasses
  | this service-level check.

Step 2 - Fetch source rows for this tenant + month
  | source_repo.list(self._tenant_id, report_month=month) -> list[Entry]
  |   (via SqlAlchemyGoogleRevenueSourceRowRepository)

Step 3 - Apply channel_ids scope filter
  | If channel_ids is set[str]:
  |     Drop rows where row.youtube_channel_id is None OR not in channel_ids.
  |     Out-of-scope rows are NOT skipped/classified - silently ignored.
  |     The caller restricted scope; "not requested" is not "broken".
  | This filter runs before Analytics raw_payload/dimensions validation or
  | country-evidence classification, so out-of-scope malformed rows remain
  | outside this normalization result.
  | If channel_ids is None:
  |     Keep every row; missing channel ids handled in Step 6(a).

Step 4 - Resolve active channels in one batched query
  | in_scope_channel_ids =
  |     {row.youtube_channel_id for row in source_rows
  |      if row.youtube_channel_id is not None}
  | Single SELECT against youtube_channels:
  |     SELECT youtube_channel_id FROM youtube_channels
  |     WHERE tenant_id = :t AND active = true
  |       AND youtube_channel_id IN :in_scope
  | Build active_channel_ids: set[str].

Step 5 - Bucket by (channel_id, source_system)
  Before bucketing, rows with source_system == "youtube_analytics" are
  validated as follows:
      malformed/non-mapping raw_payload or dimensions:
          append SkippedSourceRow(..., MALFORMED_SOURCE_PAYLOAD)
          do not add the row to any bucket
      a casefolded dimensions key in {country, country_code}:
          append SkippedSourceRow(..., NON_PROJECTING_EVIDENCE)
          do not add the row to any bucket
      a valid dimensions mapping with no country alias:
          keep the row in the normal youtube_analytics bucket
  The source_system stays allowlisted and is not renamed to an unpersistable
  evidence-only discriminator.
  | {(row.youtube_channel_id, row.source_system): list[Entry]}
  |   channel_id may be None; handled in Step 6(a).

Step 6 - Per-bucket processing
  (a) If channel_id is None:
        skip all rows in bucket -> MISSING_CHANNEL_ID
        continue
  (b) If channel_id NOT in active_channel_ids:
        skip all rows -> UNKNOWN_CHANNEL
        continue
        (Covers both missing-from-registry and active=false cases.)
  (c) Skip rows where value_kind in {"tax","deduction","adjustment"}:
        -> UNSUPPORTED_VALUE_KIND
  (d) Skip rows where currency_code != "USD":
        -> NON_USD_CURRENCY
        (USD filter runs BEFORE canonical selection so that a non-USD row
         cannot "win" canonical and starve an eligible USD sibling.)
  (e) canonical, non_canonical_rest = select_canonical_row(remaining_USD_rows)
  (f) If canonical is None:
        skip remaining_USD_rows -> NO_CANONICAL_ROW
        continue
        (USD candidates existed but none matched the preferred metric_keys
         for this source_system, e.g. AdSense group with only UNPAID_AMOUNT.)
  (g) Mark non_canonical_rest -> NON_CANONICAL_METRIC
  (h) Build proposed RevenueFact payload from canonical row + Section 4
      defaults.
  (i) existing_facts = revenue_facts_repo.list_channel_month_facts(
          month=month,
          youtube_channel_id=channel_id,
      )
      existing = next(
          (
              fact
              for fact in existing_facts
              if fact.source_kind == mapped_source_kind.value
          ),
          None,
      )
      (list_channel_month_facts() returns list[RevenueFactEntry]; C1
       filters by source_kind in Python rather than re-issuing a query.)
  (j) Classify via payload-only comparison
        Compare these fields only:
          gross_revenue_usd, net_revenue_usd, shorts_revenue_usd,
          longform_revenue_usd, subscription_revenue_usd, views,
          watch_time_minutes, confidence_score, source_report_id
        Excluded from compare: imported_by, last_imported_at, created_at,
        updated_at.
        | existing is None             -> record_fact(...) -> CREATED
        | payload fields identical     -> no write          -> UNCHANGED
        | payload fields differ        -> record_fact(...) -> UPDATED
                                          (record_fact internally updates
                                           imported_by to actor_user_id)

Step 7 - Return NormalizationResult(created, updated, unchanged, skipped)
```

### `select_canonical_row()` algorithm

```python
def select_canonical_row(rows):
    if not rows:
        return None, []
    preference = CANONICAL_METRIC_RULE[rows[0].source_system]
    for metric_key in preference:
        candidates = sorted(
            (r for r in rows if r.metric_key == metric_key),
            key=lambda r: r.source_row_key,   # deterministic tie-break
        )
        if candidates:
            canonical = candidates[0]
            return canonical, [r for r in rows if r is not canonical]
    return None, list(rows)
```

### Skip-reason matrix

| Row condition (when caller scope includes it) | Reason |
|---|---|
| `channel_id is None` | `MISSING_CHANNEL_ID` |
| `channel_id` not in active `youtube_channels` for tenant | `UNKNOWN_CHANNEL` |
| `value_kind in {tax, deduction, adjustment}` | `UNSUPPORTED_VALUE_KIND` |
| `currency_code != "USD"` | `NON_USD_CURRENCY` |
| `youtube_analytics` payload/dimensions is malformed or non-mapping | `MALFORMED_SOURCE_PAYLOAD` |
| `youtube_analytics` dimensions contains casefolded `country` or `country_code` | `NON_PROJECTING_EVIDENCE` |
| USD-eligible bucket has no row matching preferred metric_keys | `NO_CANONICAL_ROW` |
| USD row in same group that lost canonical selection | `NON_CANONICAL_METRIC` |

Rows filtered out by `channel_ids` scope are silently dropped (not present in
`skipped`) before any payload or evidence classification. Country-dimensional
Analytics evidence and malformed Analytics payloads are present in `skipped`
and therefore reach the normalizer adapter's `ROWS_SKIPPED` audit summary.

### Idempotency / replay properties

| Run | Behavior |
|---|---|
| 1st on fresh tenant+month | eligible groups -> `CREATED` |
| 2nd identical run, same actor | eligible groups -> `UNCHANGED` (zero writes) |
| 3rd run by a different actor, no source changes | still `UNCHANGED` (zero writes, no actor-only churn) |
| Re-run after one source-row `amount_native` changes | that group -> `UPDATED` (record_fact updates `imported_by` to current actor), others -> `UNCHANGED` |
| Any run with month locked at Step 1 | `RevenueFactLockedMonthError` raised before any DB write |

## 6. Error handling and edge cases

### 6.1 Exception taxonomy

`normalize_month()` raises only typed exceptions; all propagate to caller for
transaction rollback. C1 introduces no new exception classes.

| Exception | Origin | When | Operator interpretation |
|---|---|---|---|
| `RevenueFactLockedMonthError` | Step 1 upfront gate; secondary guard inside `record_fact()` | `FinanceMonthCloseORM.status == "LOCKED"` for `(tenant, month)` | Expected operational refusal - closed books |
| `RevenueFactValidationError` | Step 0 (invalid month format); `record_fact()` (Step 6j) | Invalid `YYYY-MM`, OR channel deactivated between Step 4 query and Step 6(j) write, OR computed payload fails value validation, OR invalid `actor_user_id` / `imported_by` normalization | For Step 0: caller bug. For record_fact() origin: **true bug / race** - Section 4 defaults + Step 4 active-channel pre-filter should preempt; raised here means a registry race or normalizer bug |
| `IntegrityError` (UNIQUE on `tenant_id, month, channel, source_kind`) | Step 6j | Concurrent `normalize_month()` raced past Step 1 - should be impossible given advisory + row lock | **True bug** - Step 1's `pg_advisory_xact_lock` + `SELECT ... FOR UPDATE` should have serialized; investigate lock contract |

Skip vocabulary (`SkipReason` enum) is reserved for **data conditions
discoverable from source rows alone**. Write-time exceptions are never
converted to skips.

### 6.2 Transaction boundary

C1 does not manage the transaction. Mirrors the existing repository pattern:

- Caller opens the `Session` and owns `commit()` / `rollback()`.
- `normalize_month()` performs reads + writes within the caller's
  transaction.
- Any exception aborts the call mid-batch; earlier `record_fact()` writes in
  the same call roll back when the caller's transaction rolls back.
- Successful return = caller decides to commit; `unchanged` classifications
  issued zero writes.

A partial batch never lands. Either every eligible group classifies cleanly
and the caller commits, or an exception propagates and the caller rolls back
the whole call.

### 6.3 Concurrency model

Step 1's `get_or_create_month_close_row(session, month, tenant_id=self._tenant_id, for_update=True)`
is C1's concurrency primitive. It performs both `pg_advisory_xact_lock` and
`SELECT ... FOR UPDATE` on the close row; both release at transaction end.

| Scenario | Behavior |
|---|---|
| Two `normalize_month(month=X)` calls for same tenant, same month | Second blocks on advisory + row lock until first commits/rolls back. Second then reads first's writes via the `existing=` lookup and classifies `UNCHANGED` / `UPDATED` correctly. No double-`CREATED`. |
| Same tenant, different months | No contention (different advisory keys, different rows). |
| Different tenants | No contention (different advisory keys, different rows). |
| Month locked between Step 1 and a later `record_fact()` call | First `record_fact()` after the lock raises `RevenueFactLockedMonthError`; caller rolls back. |
| Channel deactivated between Step 4 query and Step 6(j) write | `record_fact()` raises `RevenueFactValidationError`; propagates as bug/race. |
| New source row inserted by ingestion mid-batch | Invisible to this call (Step 2 read already completed); picked up on next invocation. |

C1 explicitly does not add row-level locks on `revenue_facts`, additional
advisory locks, or retry loops.

### 6.4 Empty / degenerate inputs

| Input | Behavior |
|---|---|
| No source rows for `(tenant, month)` | Step 1 still acquires the lock (may create OPEN close row); return `NormalizationResult(created=[], updated=[], unchanged=[], skipped=[])`. Not an error. |
| `channel_ids = set()` (empty set) | Step 3 filter drops everything -> all-empty result. Defensible "scope to nothing" call. |
| `channel_ids = {"UC_x"}` where no source rows exist for `UC_x` | All-empty result; caller infers nothing happened in that scope. |
| `channel_ids = {"UC_x"}` where `UC_x` has source rows but is inactive/missing in registry | Source rows classified as `UNKNOWN_CHANNEL` skips; reflected in `result.skipped`. |
| `month` locked AND zero eligible source rows | Step 1 raises `RevenueFactLockedMonthError`. Locked-month outranks "nothing to do" - closed books are sacrosanct. |

### 6.5 Logging contract

Use stdlib `logging` at the service-module level. PII / finance-safe - counts
only, no values, no raw payloads. C1 emits exactly two INFO records per call
- always a `start` line, followed by either a `complete` line on success or
a `refused` line if the month is locked. No service-level exception logging;
the caller logs traces if needed.

**INFO - start of call:**

```
normalize_month start tenant_id=<uuid> month=YYYY-MM
  channel_scope=<all|n_channels=N> actor_user_id=<id>
```

**INFO - successful return:**

```
normalize_month complete tenant_id=<uuid> month=YYYY-MM
  created=N updated=N unchanged=N skipped=N
  skipped_by_reason={<observed_reason>: N, ...}
```

**INFO - expected refusal (locked month, before any write):**

```
normalize_month refused tenant_id=<uuid> month=YYYY-MM reason=month_locked
```

**Never logged:**

- Source-row `raw_payload`
- `amount_native`, `gross_revenue_usd`, or any monetary value
- Individual `youtube_channel_id` values
- Individual `source_row_id` values

**Always logged:**

- `tenant_id`, `month`, `actor_user_id`
- Aggregate counts and skip-reason distribution

### 6.6 What is intentionally NOT caught

C1 has zero `try/except` blocks around `record_fact()` or any repository
call. The skip vocabulary is for **data shape**, not for **write-time
exceptions**.

| Tempting catch | Why we don't |
|---|---|
| `except RevenueFactLockedMonthError -> skip` | Closed books are sacrosanct (Step 1). Silent skip masks the refusal. |
| `except RevenueFactValidationError -> skip` | Step 4 query already filtered active channels; Section 4 defaults guarantee payload validation passes. A raise here means a race or normalizer bug. Catching hides it. |
| `except IntegrityError -> skip or retry` | Step 1's advisory + row lock should prevent UNIQUE races. A raise here means the lock contract is broken. Catching hides it. |

## 7. Testing

The suites cover pure selection, parser-to-normalizer evidence handling,
SQLite service flows, audit wiring, logging, and a PostgreSQL companion
subset.

### 7.1 Pure-function unit tests

`tests/finance/test_google_source_normalizer_selection.py` (no DB, no
session):

- `test_select_canonical_row_youtube_reporting_picks_estimatedRevenue`
- `test_select_canonical_row_youtube_analytics_picks_estimatedRevenue`
- `test_select_canonical_row_adsense_prefers_PAID_AMOUNT_over_ESTIMATED_EARNINGS`
- `test_select_canonical_row_adsense_falls_back_to_ESTIMATED_EARNINGS_when_no_PAID_AMOUNT`
- `test_select_canonical_row_returns_none_when_no_preferred_metric_present`
- `test_select_canonical_row_tie_break_is_deterministic_by_source_row_key_asc`
- `test_select_canonical_row_non_canonical_rest_excludes_canonical`
- `test_source_system_to_source_kind_mapping_covers_three_supported_systems`
- `test_canonical_metric_rule_mapping_is_frozen`

The same suite also exercises parser-backed worldwide preservation, country
alias exclusion, malformed payload exclusion, skip-reason recording, and the
canonical-result mutation guard.

### 7.2 Service flow tests

`tests/finance/test_google_source_normalizer_service.py` (SQLite + synthetic
fixtures via PR #43 parsers):

- `test_normalize_month_creates_revenue_facts_for_eligible_USD_rows`
- `test_normalize_month_classifies_byte_identical_replay_as_unchanged`
- `test_normalize_month_classifies_amount_change_as_updated`
- `test_normalize_month_replay_by_different_actor_with_identical_payload_is_unchanged`
- `test_normalize_month_writes_confidence_score_one_point_zero`
- `test_normalize_month_uses_canonical_source_report_id`
- `test_normalize_month_skips_non_usd_canonical_with_NON_USD_CURRENCY`
- `test_normalize_month_skips_unsupported_value_kind_rows`
- `test_normalize_month_skips_missing_channel_id_rows`
- `test_normalize_month_skips_unknown_channel_rows` (covers both
    `active=False` and missing-from-registry in two sub-arranges)
- `test_normalize_month_channel_ids_filter_drops_out_of_scope_rows_silently`
- `test_normalize_month_reverse_lookup_returns_same_canonical_row`
- `test_normalize_month_skips_no_canonical_row_with_NO_CANONICAL_ROW`
- `test_normalize_month_marks_unselected_usd_rows_as_NON_CANONICAL_METRIC`
- `test_normalize_month_raises_validation_error_for_invalid_month_format`
    (expects `RevenueFactValidationError`, not plain `ValueError`)
- `test_normalize_month_excludes_country_analytics_and_preserves_worldwide_fact`

### 7.3 Locked-month test

`tests/finance/test_google_source_normalizer_locked_month.py`:

- `test_normalize_month_raises_locked_month_error_with_zero_source_rows`
    (locked-month outranks empty input; verifies Step 1 gate fires before
    Step 2)

### 7.4 Logging contract test

`tests/finance/test_google_source_normalizer_logging.py`:

- `test_normalize_month_logging_redacts_payload_amount_channel_id_source_row_id`
    (`caplog` test asserting that across a complete `normalize_month` call
    producing CREATED / UPDATED / UNCHANGED / SKIPPED outcomes:
    - INFO logs DO contain: `created=N`, `updated=N`, `unchanged=N`,
      `skipped=N`, and the observed-key `skipped_by_reason={...}` distribution.
    - INFO logs DO NOT contain: any `raw_payload` substring, any
      `amount_native` / `gross_revenue_usd` decimal value, any individual
      `youtube_channel_id` value, any individual `source_row_id` UUID.)

### 7.5 PostgreSQL companion

`tests/finance/test_google_source_normalizer_postgres.py` repeats
approximately five representative service scenarios (CREATED, UPDATED,
UNCHANGED, `NON_USD_CURRENCY` skip, locked-month-raises) to verify the real
PostgreSQL lock path (`pg_advisory_xact_lock` +
`SELECT ... FOR UPDATE`) executes against a live engine. The existing
month-close locking tests in `tests/finance/test_month_close_locking.py`
cover advisory-lock SQL shape, key derivation, acquisition order, and
no-op behavior - but not live competing-session blocking. **C1 does not
add a true competing-session blocking test;** if that coverage is ever
needed it belongs in a future cross-service concurrency suite, not in
C1.

### 7.6 Fixture strategy

Source rows are built by exercising PR #43's parsers and
`SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many()`. No raw SQL
inserts in C1 tests.

Reused PR #43 fixtures:

- `tests/connectors/_fixtures/youtube_reporting/sample_estimated_revenue_2026_04.json`
  and `..._rerun.json`
- `tests/connectors/_fixtures/youtube_analytics/sample_query_response_2026_04.json`
  and `..._rerun.json`
- `tests/connectors/_fixtures/adsense_management/sample_earnings_report_2026_04.json`,
  `sample_payment_report_2026_04.json`, and their `..._rerun.json`
  counterparts

New synthetic fixtures added alongside PR #43 fixtures (all `.json`):

- One YouTube Reporting fixture with rows in multiple currencies (USD + EGP)
  for non-USD exclusion coverage.
- One AdSense Management fixture with both `PAID_AMOUNT` and
  `ESTIMATED_EARNINGS` for the same (channel, month) for payment-precedence
  coverage.
- One AdSense Management fixture with only `UNPAID_AMOUNT` for missing-paid-
  amount coverage.
- One AdSense Management fixture with `tax` / `deduction` / `adjustment`
  value_kinds for unsupported-value-kind coverage.

Tenant + registry setup in each service-flow test:

- `TenantORM(id=uuid4(), slug=..., display_name=...)`
- `YouTubeChannelORM(tenant_id=..., youtube_channel_id="UC_test_*", active=True)`
  for in-scope channels
- One `active=False` channel for inactive-channel coverage

Synthetic-data discipline (PR #43 standard, preserved):

- No real Google account IDs - use `"acct-test-N"` patterns
- No real YouTube channel IDs - use `"UC_test_N"` patterns
- No real revenue figures - use round-number decimals
  (`Decimal("100.000000")`, `Decimal("250.500000")`)
- No real OAuth tokens, credentials, or `raw_payload` content
  (use `{"dimensions": {"country": "US"}}` minimal objects)

## 8. Blast radius

**No graph projection impact detected.** Neo4j is retired from the active
architecture; C1 writes only PostgreSQL-backed revenue facts and adds no
graph code.

**Authorization impact: none.** C1 adds no auth surface, no endpoint, no
permission grant logic. Caller's existing auth context is preserved
untouched.

**Finance integrity impact: zero new bypass paths.** Writes go exclusively
through `SqlAlchemyRevenueFactRepository.record_fact()`, which preserves all
existing locked-month, active-channel, tenant, and value validation. C1
adds:

- Upfront locked-month gate (additive defence; record_fact() remains the
  secondary guard).
- USD-only filter (stricter than `record_fact()` alone, which assumes USD
  input).
- Active-channel pre-filter (additive defence; record_fact() remains the
  secondary guard).

**Database schema impact: none.** No new tables, no new columns, no new
database enum values, no constraint changes, no index changes, no Alembic
migration. The existing PostgreSQL `raw_payload` object check remains the
storage boundary; malformed object contents are classified at normalization
read time.

**Migration / data-reset impact: none.** Disposable pre-alpha data is
unaffected. No rollback, reseed, or destructive-change note is required for
C1 itself; existing PR #43 data is C1's read input.

**Connector code impact: none.** C1 reads from
`SqlAlchemyGoogleRevenueSourceRowRepository` via its public `.list()` API;
no parser, upsert, or storage code is touched.

## 9. Rollout and out-of-scope follow-ups

**Rollout sequence** (after C1 ships):

1. B2 (live Google OAuth + API connector) calls
   `GoogleSourceNormalizer.normalize_month(...)` after each successful
   ingest-and-store batch. C1's service surface is the integration point.
2. A future explain-endpoint spec exposes a route that reads a
   `revenue_facts` row, calls `select_canonical_row()` over the matching
   source rows, and surfaces the canonical-row trace + raw payload in a UI.
3. A future net-revenue spec (`finance/net_revenue.py`) populates
   `net_revenue_usd` on facts written by C1 once the deduction / fee
   allocation model is defined.
4. A future confidence-labels spec replaces the hard-coded `Decimal("1.0")`
   with real scoring rules (and any historical backfill those rules require).
5. A future detail-revenue spec populates `shorts_revenue_usd`,
   `longform_revenue_usd`, and `subscription_revenue_usd` from source-row
   dimension breakdowns.

**Plan-doc updates required at PR time** (per the per-PR plan-status memory
rule):

- `Docs/01_IMPLEMENTATION_PLAN.md` and `Docs/15_DELIVERY_BACKLOG.md` both
  get an inline `⏳ PR #N - remaining: ...` mark for the C1 row. C1 is a
  scaffolding-shipped but-not-yet-end-to-end deliverable (no live data
  source yet), which under the honesty rule stays `⏳` until B2 lands.

**Per-PR report** at `Docs/pulls/2026-05-25-pr44-spec-c1-google-source-normalizer-report.md`
follows the established PR-doc layout.
