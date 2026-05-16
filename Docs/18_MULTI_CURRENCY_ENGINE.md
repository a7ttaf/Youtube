# Multi-Currency Engine

> Status: **planned** — Phase S2 schema; wired into reconciliation in Phase 4.
> Replaces the previous USD-only storage model. **All math in `Decimal`, never `float`.**

---

## Why multi-currency

Channels operate across multiple economies: AdSense pays in USD, bank receipts arrive in AED for the UAE-based holding, an Egyptian sector reports in EGP, royalties may originate in EUR or GBP. Storing everything as a single denomination loses information and hides FX impact from finance. The system must:

1. **Store the native amount as the source of truth.** Conversion happens on read, not at storage.
2. **Freeze the FX rate used when a month is locked.** A locked month must produce the same numbers a year later.
3. **Cite the rate.** Every converted display value is explainable down to "rate X.YZZZ from provider P on date D".
4. **Aggregate sensibly across currencies.** Mixed-currency rollups show in the tenant's primary currency by default, with an explicit "native (per-currency)" view.

This page describes the data model, the conversion pipeline, the API surface, the display rules, and the test invariants.

---

## Storage rule

For every monetary value, two columns:

| Column | Type | Meaning |
|---|---|---|
| `amount_native` | `NUMERIC(20, 6)` | The amount as reported by the source. Never converted in place. |
| `currency_iso4217` | `CHAR(3)` | The ISO 4217 code in upper case (e.g. `'AED'`, `'USD'`). |

**Existing `*_usd` columns are deprecated.** A migration backfills `(amount_native, 'USD')` for every existing row and removes the suffix in API responses. The columns themselves stay for one release cycle to allow rollback.

**Why NUMERIC(20, 6):** 20 digits of precision is enough for trillions of any currency; 6 decimal places cover all current ISO 4217 minor unit scales (UYI has 0, most have 2-3, no real currency exceeds 6).

**Decimal in code:**

```python
from decimal import Decimal, getcontext

# All money math uses Decimal at this precision. Quantize at the display edge.
getcontext().prec = 28
```

`float` is treated as a bug in money modules. mypy strict + a lint rule (`ruff` `PLW1641`) flag it.

---

## Currencies

`currencies` table (platform-wide, seeded from ISO 4217):

```sql
CREATE TABLE currencies (
    code            CHAR(3) PRIMARY KEY,
    numeric_code    CHAR(3) NOT NULL,
    name            TEXT NOT NULL,
    minor_unit      SMALLINT NOT NULL,           -- 2 for USD, 0 for JPY/UYI, ...
    is_supported    BOOLEAN NOT NULL DEFAULT FALSE,
    activated_at    TIMESTAMPTZ,
    CONSTRAINT ck_currencies_code_upper CHECK (code = upper(code)),
    CONSTRAINT ck_currencies_minor_unit CHECK (minor_unit BETWEEN 0 AND 6)
);
```

**v1.0 supported set (`is_supported = TRUE`):** `AED`, `USD`, `EUR`, `GBP`, `SAR`, `EGP`. Any other ISO code can be flipped on by a platform admin without code changes — the seed inserts the full ISO 4217 list as unsupported rows.

---

## FX rate snapshots

`fx_rates` table:

```sql
CREATE TABLE fx_rates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    provider        TEXT NOT NULL,                -- 'ECB', 'EXCHANGERATE_HOST', 'MANUAL_CSV'
    base_code       CHAR(3) NOT NULL REFERENCES currencies(code),
    quote_code      CHAR(3) NOT NULL REFERENCES currencies(code),
    rate            NUMERIC(20, 10) NOT NULL,     -- amount in QUOTE per 1 of BASE
    as_of_date      DATE NOT NULL,                -- the day this rate applies
    source_ref      TEXT,                         -- raw provider reference
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingested_by     UUID REFERENCES users(id),    -- NULL for automated sync
    CONSTRAINT uq_fx_rates_unique
        UNIQUE (tenant_id, provider, base_code, quote_code, as_of_date),
    CONSTRAINT ck_fx_rates_positive_rate CHECK (rate > 0),
    CONSTRAINT ck_fx_rates_distinct CHECK (base_code <> quote_code)
);
CREATE INDEX ix_fx_rates_lookup ON fx_rates (tenant_id, base_code, quote_code, as_of_date DESC);
```

**Semantics:** rate is `quote_per_base` — i.e. 1 unit of `base_code` is worth `rate` units of `quote_code`. To convert an amount: `target = amount_native * rate(native -> target, on_date)`.

**Missing rate:** the conversion service raises `FxRateUnavailable`, never silently returns 0 or null. The UI then shows the value in its native currency with a "rate unavailable" warning. Locked-month rollups require all FX to be present (smart-alert `RATE_MISSING_FOR_LOCKED_MONTH`).

---

## FX provider abstraction

```python
class FxRateProvider(Protocol):
    name: str
    async def fetch_rates(self, *, as_of_date: date,
                          base: str, quotes: Iterable[str]) -> Iterable[FxRateInput]:
        ...
```

Implementations:

| Provider | Module | Notes |
|---|---|---|
| **ECB** (European Central Bank) | `finance/fx/providers/ecb.py` | Free, public, daily reference rates; EUR-pivot — derives cross rates. Default. |
| **exchangerate.host** | `finance/fx/providers/exchangerate_host.py` | Free, multiple bases; fallback if ECB doesn't cover a pair. |
| **Manual CSV upload** | `finance/fx/providers/manual_csv.py` | Finance uploads a `.csv` for unusual currencies or audit-mandated official rates. |
| Future: **OANDA, XE, central bank feeds** | — | Pluggable; just implement the protocol. |

Provider choice is **per-tenant**, configured in `tenants.fx_provider_settings JSONB`. Multiple providers can be enabled with a priority order.

### Sync job

Celery beat task `workers/fx_sync_job.py` runs daily at 02:00 UTC per tenant; pulls yesterday's rates for all (base, quote) pairs used by that tenant's data; writes to `fx_rates`. Idempotent per tenant: the `(tenant_id, provider, base, quote, as_of_date)` unique constraint ensures retries do not duplicate rates while allowing tenant-specific provider choices and manual overrides.

---

## Conversion service

```python
# backend/ums_smart_revenue/finance/fx/service.py (sketch)
class FxConversionService:
    async def convert(
        self,
        amount: Decimal,
        from_code: str,
        to_code: str,
        *,
        on_date: date,
        prefer_provider: str | None = None,
    ) -> FxConversion:
        ...
    async def convert_many(...) -> list[FxConversion]: ...

@dataclass(frozen=True)
class FxConversion:
    amount_native: Decimal
    amount_converted: Decimal
    from_code: str
    to_code: str
    rate: Decimal
    rate_as_of: date
    provider: str
```

Conversion never mutates the source row. It returns an `FxConversion` envelope so the UI can render both the converted value and the source rate.

**Rate lookup order:**
1. Exact `(tenant_id, base, quote, as_of_date)` match.
2. Most recent `(tenant_id, base, quote, on_or_before=as_of_date)` from preferred provider.
3. Most recent across any allowed provider for the tenant.
4. Cross-rate via USD pivot (`(base, USD)` * `(USD, quote)`).
5. Failure → `FxRateUnavailable`.

Step 4 is only used when explicitly enabled per-tenant. Pivoted rates are marked in the `FxConversion` envelope so the explanation engine can label them `B_RECONCILED` instead of `A_OFFICIAL`.

---

## Locked-month FX freeze

When a finance month is locked:

1. The conversion service is asked to compute the rate used for every monetary value in that month.
2. The selected rates are persisted to `fx_locked_month_rates(tenant_id, month, base, quote, rate, provider, as_of_date)`.
3. Subsequent reads of locked-month values use the **locked rate**, not the live one.
4. Unlocking the month deletes the locked rates and recomputes from current `fx_rates`. Unlock requires the standard reason + audit event.

This guarantees that re-running last March's executive PDF a year later produces identical numbers.

---

## API surface

### Request a value in a specific currency

```
GET /revenue/months/2026-04/net-revenue?currency=AED
GET /revenue/months/2026-04/net-revenue
    Accept-Currency: AED
```

Resolution order:
1. `?currency=` query param (highest precedence).
2. `Accept-Currency` header.
3. User preference `users.preferred_currency`.
4. Tenant default `tenants.primary_currency`.

### Currency in responses

Every monetary node carries:

```json
{
  "amount": "1234567.890000",
  "currency": "AED",
  "native": {
    "amount": "336000.000000",
    "currency": "USD"
  },
  "fx": {
    "rate": "3.6725",
    "rate_as_of": "2026-04-30",
    "provider": "ECB",
    "is_pivoted": false
  }
}
```

If the requested currency equals the native currency, `fx` is `null`.

### Endpoints for the engine itself

```
GET  /platform/currencies              # supported codes
GET  /fx/rates?base=USD&quote=AED&from=2026-01-01&to=2026-04-30
POST /fx/rates/manual-upload           # CSV; requires MANAGE_FX_RATES permission
POST /fx/sync                          # trigger a sync; CONNECTOR_ADMIN+
```

A new `Permission.MANAGE_FX_RATES` is added in the same Phase S2 migration.

---

## Display rules (frontend)

- Numbers render `1,234,567 AED` (locale + ISO suffix).
- On hover: native amount + rate + provider + date.
- On the Explain drawer: a dedicated FX row when the displayed currency ≠ native.
- A global currency switcher in the top bar (`AED ▾`) updates every visible number; preference saved to `users.preferred_currency`.
- The Channel Revenue Table allows toggling to "native (mixed)" mode that suppresses the sum row and shows per-currency subtotals.

---

## Test invariants

Property-based tests via `hypothesis`:

1. **Round-trip consistency**: `convert(amount, A, B) → convert(result, B, A)` yields the original amount within rounding tolerance (`abs(diff) ≤ 10^-minor_unit`).
2. **Identity**: `convert(x, A, A)` returns `FxConversion(amount_native=x, amount_converted=x, from_code=A, to_code=A, rate=Decimal("1"), rate_as_of=on_date, provider="IDENTITY")`.
3. **Sum invariance**: `sum(convert(x_i, A, B))` differs from `convert(sum(x_i), A, B)` by at most the rounding tolerance × N.
4. **Locked-month immutability**: after a month is locked, any subsequent conversion of any value in that month uses the locked rate, regardless of `fx_rates` changes.
5. **Pivoted rate consistency**: a USD-pivoted rate (`A→USD→B`) is within 0.5% of the direct rate when both are available (sanity check on provider data).
6. **Mixed-currency total refusal**: aggregating mixed currencies without an explicit target raises `MixedCurrencyAggregation`.

---

## Migration sequence (Phase S2)

`20260520_0002_multi_currency_engine`:

1. Create `currencies`, seed full ISO 4217 list, flip the v1.0 set to `is_supported = TRUE`.
2. Create `fx_rates`, `fx_locked_month_rates`.
3. For each monetary column on a tenant-scoped table:
   1. Add `amount_native NUMERIC(20,6) NULL`.
   2. Add `currency_iso4217 CHAR(3) NULL`.
   3. Backfill `amount_native = <existing_usd_column>`, `currency_iso4217 = 'USD'`.
   4. Mark both NOT NULL.
   5. Add FK on `currency_iso4217 → currencies(code)`.
4. Validate the `tenants.primary_currency` column created by `20260520_0001_multi_tenant_foundation`; do not add it a second time.
5. Add `users.preferred_currency CHAR(3) NULL`.
6. Add FK constraints `tenants.primary_currency -> currencies(code)` and `users.preferred_currency -> currencies(code)` after `currencies` is seeded, using `NOT VALID` then `VALIDATE CONSTRAINT` for existing installs.
7. Add `Permission.MANAGE_FX_RATES` row in `permissions`.

The original `*_usd` columns are kept for one release as deprecated; flagged in `pyproject.toml` removal-target list.

---

## Acceptance gates

- Any monetary endpoint can serve any of `{AED, USD, EUR, GBP, SAR, EGP}` via `?currency=` or `Accept-Currency`.
- Locked-month re-export reproduces byte-for-byte (after deterministic timestamps are masked) one month later, demonstrating FX freeze.
- Manual CSV upload for unusual rates works for an offline test fixture.
- Hypothesis suite green: round-trip, identity, sum invariance, locked-month immutability.

---

## Open decisions (close before Phase S2)

- Tenant-level toggle for USD-pivoted cross rates: allow by default or opt-in. (Recommend opt-in; UAE banks prefer the direct AED↔USD rate.)
- Whether `MANUAL_CSV` provider always overrides automated rates when present, or only when no automated rate exists. (Recommend "manual wins for that as_of_date".)
- Display rule for cumulative-since-inception aggregates that span multiple locked-month FX regimes. (Recommend a banner explaining the rate transitions.)
