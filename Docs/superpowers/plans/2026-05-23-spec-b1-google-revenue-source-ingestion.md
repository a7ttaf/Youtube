# Spec B1 — Google Revenue Source Ingestion Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Date:** 2026-05-23
**Status:** Planned — PR #43 implementation against the PR #42 locked contract
**Design:** `Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md`

**Goal:** Ship the storage and synthetic-fixture parser foundation for Google source-reported revenue: a platform-wide `currencies` reference table, a tenant-scoped `google_revenue_source_rows` table with idempotent source-row keys, a storage repository, and parsers for YouTube Reporting / YouTube Analytics / AdSense Management — without any live OAuth flow, live API client, live download, or credential handling beyond what `connectors/credentials.py` already provides.

**Architecture:** SQLAlchemy ORM models on `FinanceBase` for both new tables. One Alembic revision (`20260523_0001_google_revenue_source_foundation`) only adds — `currency_exchange_rates` and the legacy `/exchange-rates/*` endpoints are left in place per spec §6. Storage repository lives at `backend/ums_smart_revenue/connectors/google_source_rows/` and exposes `upsert_many(tenant_id, rows, *, raw_file_id, imported_by)` keyed on `(tenant_id, source_system, source_row_key)`. Parsers live at `backend/ums_smart_revenue/connectors/google_source_parsers/` and consume synthetic JSON fixtures under `tests/connectors/_fixtures/`, emitting `ParsedSourceRow` instances. Full-64-char SHA-256 hex `source_row_key` derivation is the only logic crossing the parser/repository boundary.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.x, Alembic, pytest 9, psycopg with disposable Docker PostgreSQL 18 for the migration round-trip test, Pydantic v2. SQLite is used for fast unit tests via the dialect-insert helper pattern (`sqlite_insert` / `postgresql_insert`) that already lives in `finance/exchange_rates.py:278-285` so `ON CONFLICT DO UPDATE` works on both backends.

**Pre-merge contract (PR #42 — docs only, ships first):** The nine doc edits + new design spec + this plan land in PR #42 (markdown only, no runtime risk). PR #43 (this plan's implementation) opens against `main` AFTER PR #42 merges, so the implementer is working against a stable contract.

**Hard non-goals (preserved verbatim from spec §3, plus implementation-side amplifications):**

- No `fx_rates` table.
- No `fx_locked_month_rates` table.
- No `tenants.fx_provider_settings` column.
- No `Permission.MANAGE_FX_RATES`.
- No `/fx/sync`, `/fx/rates/manual-upload`, ECB provider, exchangerate.host provider, manual CSV provider, or provider priority chain.
- No official finance calculation from public/provider FX rates.
- No locked-month FX freeze behavior.
- No frontend currency switcher.
- No paired-column migration of any existing `_usd` table in this PR.
- No live Google OAuth flow.
- No live YouTube Reporting / YouTube Analytics / AdSense Management API client.
- No live HTTP download path.
- No credential, secret, or OAuth-token handling beyond what `backend/ums_smart_revenue/connectors/credentials.py` already provides.
- No new API endpoints. No expansion of existing API behavior.
- No deletion of `currency_exchange_rates`, `CurrencyExchangeRateORM`, `finance/exchange_rates.py`, `api/exchange_rates.py`, the `EXCHANGE_RATE_SYNCED` audit event, or the four existing legacy test files. Spec §6 explicitly preserves them as inert scaffolding.
- **Fixture data is synthetic or sanitized only.** No real Google account IDs, no real YouTube channel private metadata, no real revenue figures, no real OAuth payloads, no credentials in any committed fixture.

**Slice-to-phase mapping (matches operator-locked subagent dispatch boundaries):**

| Slice | Phases |
|---|---|
| DB/migration | 1, 2, 8 |
| Repository | 3 |
| Parsers/fixtures | 4, 5 |
| Finance guardrails | 6 |
| Auth/docs/validation | 7, 9, 10 |

---

## Phase 0 — Pre-flight

### Task 0.1: Branch off main and verify clean working tree

**Files:**
- Reads only.

- [ ] **Step 1: Confirm PR #42 has merged**

  Run: `gh pr view 42 --json state,mergedAt`
  Expected: `"state":"MERGED"` with a non-null `mergedAt`. If the PR is still open, STOP and report — PR #43 must not start until PR #42's design + plan + doc edits are merged so the implementer is working against the stable contract.

- [ ] **Step 2: Sync local main and confirm clean tree**

  ```powershell
  git checkout main
  git pull --ff-only origin main
  git status --short
  ```
  Expected: empty output from `git status --short` (or only `frontend/package-lock.json` per the standing operator exclusion — do NOT stage that file).

- [ ] **Step 3: Branch**

  ```powershell
  git checkout -b pr/spec-b1-google-revenue-source-ingestion
  ```

- [ ] **Step 4: Re-read the locked design + this plan**

  Files to load fully into context before any code change:
  - `Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md`
  - `Docs/superpowers/plans/2026-05-23-spec-b1-google-revenue-source-ingestion.md` (this file)
  - `Docs/05_CONNECTORS_YOUTUBE_ADSENSE.md` (cross-references the source-row contract)
  - `Docs/13_SQL_DATA_MODEL.md` (the `google_revenue_source_rows` shape lives here too)

### Task 0.2: Verify toolchain

**Files:**
- Reads only.

- [ ] **Step 1: Verify Python + pytest + ruff**

  ```powershell
  python --version
  python -m pytest --version
  python -m ruff --version
  ```
  Expected: Python 3.14.x, pytest >= 9.0.0, ruff present.

- [ ] **Step 2: Verify Docker availability for Phase 8 PostgreSQL round-trip**

  ```powershell
  docker --version
  docker pull postgres:18-alpine
  ```
  Expected: docker command succeeds; image pulled or already present. If Docker is not available on the implementer's machine, STOP — Phase 8 is mandatory and PostgreSQL-backed per spec §9.1.

- [ ] **Step 3: Verify npm for Vitest gate step**

  ```powershell
  # PowerShell uses Nodist by default on this machine; the gate's _resolve_npm()
  # helper handles both npm and npm.cmd. Just verify the binary is reachable.
  npm --version
  ```
  Expected: any Node 20+ npm version. The validation gate (PR #38) runs `npm --prefix frontend run test` automatically.

- [ ] **Step 4: Baseline pytest count**

  ```powershell
  python -m pytest --collect-only -q | Select-String "test" | Measure-Object -Line
  ```
  Record the count. The final PR #43 `report.md` will report the new count and the delta. Current baseline (post-PR #41): ~819 tests.

---

## Phase 1 — ISO 4217 snapshot + `currencies` table

### Task 1.1: Test + create the immutable ISO 4217 snapshot module

**Files:**
- Create: `backend/ums_smart_revenue/db/iso_4217_2026_05.py`
- Test: `tests/db/test_iso_4217_snapshot.py`

- [ ] **Step 1: Write the failing test**

  ```python
  # tests/db/test_iso_4217_snapshot.py
  """Smoke tests for the immutable ISO 4217 snapshot module.

  The snapshot is intentionally frozen: future ISO updates land as a new
  dated module (e.g. iso_4217_2027_03.py) plus a new migration, not by
  mutating this file.
  """

  from ums_smart_revenue.db.iso_4217_2026_05 import ISO_4217_CURRENCIES_2026_05

  SUPPORTED_V1 = ("AED", "USD", "EUR", "GBP", "SAR", "EGP")


  def test_snapshot_contains_v1_supported_set() -> None:
      codes = {row["code"] for row in ISO_4217_CURRENCIES_2026_05}
      for expected in SUPPORTED_V1:
          assert expected in codes, f"missing v1 supported code: {expected}"


  def test_all_codes_are_three_uppercase_letters() -> None:
      for row in ISO_4217_CURRENCIES_2026_05:
          code = row["code"]
          assert isinstance(code, str)
          assert len(code) == 3
          assert code == code.upper()
          assert code.isalpha()


  def test_all_numeric_codes_are_three_digit_strings_and_unique() -> None:
      numeric_codes = [row["numeric_code"] for row in ISO_4217_CURRENCIES_2026_05]
      assert len(numeric_codes) == len(set(numeric_codes)), "numeric codes must be unique"
      for numeric_code in numeric_codes:
          assert isinstance(numeric_code, str)
          assert len(numeric_code) == 3
          assert numeric_code.isdigit()


  def test_codes_are_unique() -> None:
      codes = [row["code"] for row in ISO_4217_CURRENCIES_2026_05]
      assert len(codes) == len(set(codes)), "ISO 4217 codes must be unique"


  def test_minor_unit_is_in_range_or_none() -> None:
      for row in ISO_4217_CURRENCIES_2026_05:
          minor_unit = row["minor_unit"]
          assert minor_unit is None or (isinstance(minor_unit, int) and 0 <= minor_unit <= 6)


  def test_v1_supported_codes_have_known_minor_unit() -> None:
      by_code = {row["code"]: row for row in ISO_4217_CURRENCIES_2026_05}
      for code in SUPPORTED_V1:
          assert by_code[code]["minor_unit"] is not None, (
              f"v1 supported currency {code} must declare minor_unit so it can be flipped is_supported"
          )


  def test_row_count_smoke() -> None:
      # Sanity check that the snapshot is the full ISO list, not just the v1 set.
      assert len(ISO_4217_CURRENCIES_2026_05) >= 150
  ```

- [ ] **Step 2: Run test to verify it fails**

  Run: `python -m pytest tests/db/test_iso_4217_snapshot.py -v`
  Expected: ImportError or ModuleNotFoundError on `iso_4217_2026_05`.

- [ ] **Step 3: Write minimal snapshot module**

  ```python
  # backend/ums_smart_revenue/db/iso_4217_2026_05.py
  """Immutable snapshot of the ISO 4217 currency list as of 2026-05.

  DO NOT MUTATE. Future updates land as a new dated module (e.g.
  iso_4217_2027_03.py) referenced by a new migration. Each entry is a
  plain dict so historical migrations cannot break if downstream
  dataclasses (connectors/google_source_rows/dataclasses.py) ever change.
  """

  from typing import Final

  ISO_4217_CURRENCIES_2026_05: Final[tuple[dict[str, object], ...]] = (
      {"code": "AED", "numeric_code": "784", "name": "UAE Dirham", "minor_unit": 2},
      {"code": "AFN", "numeric_code": "971", "name": "Afghani", "minor_unit": 2},
      {"code": "ALL", "numeric_code": "008", "name": "Lek", "minor_unit": 2},
      {"code": "AMD", "numeric_code": "051", "name": "Armenian Dram", "minor_unit": 2},
      {"code": "ANG", "numeric_code": "532", "name": "Netherlands Antillean Guilder", "minor_unit": 2},
      {"code": "AOA", "numeric_code": "973", "name": "Kwanza", "minor_unit": 2},
      {"code": "ARS", "numeric_code": "032", "name": "Argentine Peso", "minor_unit": 2},
      {"code": "AUD", "numeric_code": "036", "name": "Australian Dollar", "minor_unit": 2},
      {"code": "AWG", "numeric_code": "533", "name": "Aruban Florin", "minor_unit": 2},
      {"code": "AZN", "numeric_code": "944", "name": "Azerbaijan Manat", "minor_unit": 2},
      {"code": "BAM", "numeric_code": "977", "name": "Convertible Mark", "minor_unit": 2},
      {"code": "BBD", "numeric_code": "052", "name": "Barbados Dollar", "minor_unit": 2},
      {"code": "BDT", "numeric_code": "050", "name": "Taka", "minor_unit": 2},
      {"code": "BGN", "numeric_code": "975", "name": "Bulgarian Lev", "minor_unit": 2},
      {"code": "BHD", "numeric_code": "048", "name": "Bahraini Dinar", "minor_unit": 3},
      {"code": "BIF", "numeric_code": "108", "name": "Burundi Franc", "minor_unit": 0},
      {"code": "BMD", "numeric_code": "060", "name": "Bermudian Dollar", "minor_unit": 2},
      {"code": "BND", "numeric_code": "096", "name": "Brunei Dollar", "minor_unit": 2},
      {"code": "BOB", "numeric_code": "068", "name": "Boliviano", "minor_unit": 2},
      {"code": "BOV", "numeric_code": "984", "name": "Mvdol", "minor_unit": 2},
      {"code": "BRL", "numeric_code": "986", "name": "Brazilian Real", "minor_unit": 2},
      {"code": "BSD", "numeric_code": "044", "name": "Bahamian Dollar", "minor_unit": 2},
      {"code": "BTN", "numeric_code": "064", "name": "Ngultrum", "minor_unit": 2},
      {"code": "BWP", "numeric_code": "072", "name": "Pula", "minor_unit": 2},
      {"code": "BYN", "numeric_code": "933", "name": "Belarusian Ruble", "minor_unit": 2},
      {"code": "BZD", "numeric_code": "084", "name": "Belize Dollar", "minor_unit": 2},
      {"code": "CAD", "numeric_code": "124", "name": "Canadian Dollar", "minor_unit": 2},
      {"code": "CDF", "numeric_code": "976", "name": "Congolese Franc", "minor_unit": 2},
      {"code": "CHE", "numeric_code": "947", "name": "WIR Euro", "minor_unit": 2},
      {"code": "CHF", "numeric_code": "756", "name": "Swiss Franc", "minor_unit": 2},
      {"code": "CHW", "numeric_code": "948", "name": "WIR Franc", "minor_unit": 2},
      {"code": "CLF", "numeric_code": "990", "name": "Unidad de Fomento", "minor_unit": 4},
      {"code": "CLP", "numeric_code": "152", "name": "Chilean Peso", "minor_unit": 0},
      {"code": "CNY", "numeric_code": "156", "name": "Yuan Renminbi", "minor_unit": 2},
      {"code": "COP", "numeric_code": "170", "name": "Colombian Peso", "minor_unit": 2},
      {"code": "COU", "numeric_code": "970", "name": "Unidad de Valor Real", "minor_unit": 2},
      {"code": "CRC", "numeric_code": "188", "name": "Costa Rican Colon", "minor_unit": 2},
      {"code": "CUP", "numeric_code": "192", "name": "Cuban Peso", "minor_unit": 2},
      {"code": "CVE", "numeric_code": "132", "name": "Cabo Verde Escudo", "minor_unit": 2},
      {"code": "CZK", "numeric_code": "203", "name": "Czech Koruna", "minor_unit": 2},
      {"code": "DJF", "numeric_code": "262", "name": "Djibouti Franc", "minor_unit": 0},
      {"code": "DKK", "numeric_code": "208", "name": "Danish Krone", "minor_unit": 2},
      {"code": "DOP", "numeric_code": "214", "name": "Dominican Peso", "minor_unit": 2},
      {"code": "DZD", "numeric_code": "012", "name": "Algerian Dinar", "minor_unit": 2},
      {"code": "EGP", "numeric_code": "818", "name": "Egyptian Pound", "minor_unit": 2},
      {"code": "ERN", "numeric_code": "232", "name": "Nakfa", "minor_unit": 2},
      {"code": "ETB", "numeric_code": "230", "name": "Ethiopian Birr", "minor_unit": 2},
      {"code": "EUR", "numeric_code": "978", "name": "Euro", "minor_unit": 2},
      {"code": "FJD", "numeric_code": "242", "name": "Fiji Dollar", "minor_unit": 2},
      {"code": "FKP", "numeric_code": "238", "name": "Falkland Islands Pound", "minor_unit": 2},
      {"code": "GBP", "numeric_code": "826", "name": "Pound Sterling", "minor_unit": 2},
      {"code": "GEL", "numeric_code": "981", "name": "Lari", "minor_unit": 2},
      {"code": "GHS", "numeric_code": "936", "name": "Ghana Cedi", "minor_unit": 2},
      {"code": "GIP", "numeric_code": "292", "name": "Gibraltar Pound", "minor_unit": 2},
      {"code": "GMD", "numeric_code": "270", "name": "Dalasi", "minor_unit": 2},
      {"code": "GNF", "numeric_code": "324", "name": "Guinean Franc", "minor_unit": 0},
      {"code": "GTQ", "numeric_code": "320", "name": "Quetzal", "minor_unit": 2},
      {"code": "GYD", "numeric_code": "328", "name": "Guyana Dollar", "minor_unit": 2},
      {"code": "HKD", "numeric_code": "344", "name": "Hong Kong Dollar", "minor_unit": 2},
      {"code": "HNL", "numeric_code": "340", "name": "Lempira", "minor_unit": 2},
      {"code": "HTG", "numeric_code": "332", "name": "Gourde", "minor_unit": 2},
      {"code": "HUF", "numeric_code": "348", "name": "Forint", "minor_unit": 2},
      {"code": "IDR", "numeric_code": "360", "name": "Rupiah", "minor_unit": 2},
      {"code": "ILS", "numeric_code": "376", "name": "New Israeli Sheqel", "minor_unit": 2},
      {"code": "INR", "numeric_code": "356", "name": "Indian Rupee", "minor_unit": 2},
      {"code": "IQD", "numeric_code": "368", "name": "Iraqi Dinar", "minor_unit": 3},
      {"code": "IRR", "numeric_code": "364", "name": "Iranian Rial", "minor_unit": 2},
      {"code": "ISK", "numeric_code": "352", "name": "Iceland Krona", "minor_unit": 0},
      {"code": "JMD", "numeric_code": "388", "name": "Jamaican Dollar", "minor_unit": 2},
      {"code": "JOD", "numeric_code": "400", "name": "Jordanian Dinar", "minor_unit": 3},
      {"code": "JPY", "numeric_code": "392", "name": "Yen", "minor_unit": 0},
      {"code": "KES", "numeric_code": "404", "name": "Kenyan Shilling", "minor_unit": 2},
      {"code": "KGS", "numeric_code": "417", "name": "Som", "minor_unit": 2},
      {"code": "KHR", "numeric_code": "116", "name": "Riel", "minor_unit": 2},
      {"code": "KMF", "numeric_code": "174", "name": "Comorian Franc", "minor_unit": 0},
      {"code": "KPW", "numeric_code": "408", "name": "North Korean Won", "minor_unit": 2},
      {"code": "KRW", "numeric_code": "410", "name": "Won", "minor_unit": 0},
      {"code": "KWD", "numeric_code": "414", "name": "Kuwaiti Dinar", "minor_unit": 3},
      {"code": "KYD", "numeric_code": "136", "name": "Cayman Islands Dollar", "minor_unit": 2},
      {"code": "KZT", "numeric_code": "398", "name": "Tenge", "minor_unit": 2},
      {"code": "LAK", "numeric_code": "418", "name": "Lao Kip", "minor_unit": 2},
      {"code": "LBP", "numeric_code": "422", "name": "Lebanese Pound", "minor_unit": 2},
      {"code": "LKR", "numeric_code": "144", "name": "Sri Lanka Rupee", "minor_unit": 2},
      {"code": "LRD", "numeric_code": "430", "name": "Liberian Dollar", "minor_unit": 2},
      {"code": "LSL", "numeric_code": "426", "name": "Loti", "minor_unit": 2},
      {"code": "LYD", "numeric_code": "434", "name": "Libyan Dinar", "minor_unit": 3},
      {"code": "MAD", "numeric_code": "504", "name": "Moroccan Dirham", "minor_unit": 2},
      {"code": "MDL", "numeric_code": "498", "name": "Moldovan Leu", "minor_unit": 2},
      {"code": "MGA", "numeric_code": "969", "name": "Malagasy Ariary", "minor_unit": 2},
      {"code": "MKD", "numeric_code": "807", "name": "Denar", "minor_unit": 2},
      {"code": "MMK", "numeric_code": "104", "name": "Kyat", "minor_unit": 2},
      {"code": "MNT", "numeric_code": "496", "name": "Tugrik", "minor_unit": 2},
      {"code": "MOP", "numeric_code": "446", "name": "Pataca", "minor_unit": 2},
      {"code": "MRU", "numeric_code": "929", "name": "Ouguiya", "minor_unit": 2},
      {"code": "MUR", "numeric_code": "480", "name": "Mauritius Rupee", "minor_unit": 2},
      {"code": "MVR", "numeric_code": "462", "name": "Rufiyaa", "minor_unit": 2},
      {"code": "MWK", "numeric_code": "454", "name": "Malawi Kwacha", "minor_unit": 2},
      {"code": "MXN", "numeric_code": "484", "name": "Mexican Peso", "minor_unit": 2},
      {"code": "MXV", "numeric_code": "979", "name": "Mexican Unidad de Inversion (UDI)", "minor_unit": 2},
      {"code": "MYR", "numeric_code": "458", "name": "Malaysian Ringgit", "minor_unit": 2},
      {"code": "MZN", "numeric_code": "943", "name": "Mozambique Metical", "minor_unit": 2},
      {"code": "NAD", "numeric_code": "516", "name": "Namibia Dollar", "minor_unit": 2},
      {"code": "NGN", "numeric_code": "566", "name": "Naira", "minor_unit": 2},
      {"code": "NIO", "numeric_code": "558", "name": "Cordoba Oro", "minor_unit": 2},
      {"code": "NOK", "numeric_code": "578", "name": "Norwegian Krone", "minor_unit": 2},
      {"code": "NPR", "numeric_code": "524", "name": "Nepalese Rupee", "minor_unit": 2},
      {"code": "NZD", "numeric_code": "554", "name": "New Zealand Dollar", "minor_unit": 2},
      {"code": "OMR", "numeric_code": "512", "name": "Rial Omani", "minor_unit": 3},
      {"code": "PAB", "numeric_code": "590", "name": "Balboa", "minor_unit": 2},
      {"code": "PEN", "numeric_code": "604", "name": "Sol", "minor_unit": 2},
      {"code": "PGK", "numeric_code": "598", "name": "Kina", "minor_unit": 2},
      {"code": "PHP", "numeric_code": "608", "name": "Philippine Peso", "minor_unit": 2},
      {"code": "PKR", "numeric_code": "586", "name": "Pakistan Rupee", "minor_unit": 2},
      {"code": "PLN", "numeric_code": "985", "name": "Zloty", "minor_unit": 2},
      {"code": "PYG", "numeric_code": "600", "name": "Guarani", "minor_unit": 0},
      {"code": "QAR", "numeric_code": "634", "name": "Qatari Rial", "minor_unit": 2},
      {"code": "RON", "numeric_code": "946", "name": "Romanian Leu", "minor_unit": 2},
      {"code": "RSD", "numeric_code": "941", "name": "Serbian Dinar", "minor_unit": 2},
      {"code": "RUB", "numeric_code": "643", "name": "Russian Ruble", "minor_unit": 2},
      {"code": "RWF", "numeric_code": "646", "name": "Rwanda Franc", "minor_unit": 0},
      {"code": "SAR", "numeric_code": "682", "name": "Saudi Riyal", "minor_unit": 2},
      {"code": "SBD", "numeric_code": "090", "name": "Solomon Islands Dollar", "minor_unit": 2},
      {"code": "SCR", "numeric_code": "690", "name": "Seychelles Rupee", "minor_unit": 2},
      {"code": "SDG", "numeric_code": "938", "name": "Sudanese Pound", "minor_unit": 2},
      {"code": "SEK", "numeric_code": "752", "name": "Swedish Krona", "minor_unit": 2},
      {"code": "SGD", "numeric_code": "702", "name": "Singapore Dollar", "minor_unit": 2},
      {"code": "SHP", "numeric_code": "654", "name": "Saint Helena Pound", "minor_unit": 2},
      {"code": "SLE", "numeric_code": "925", "name": "Leone", "minor_unit": 2},
      {"code": "SOS", "numeric_code": "706", "name": "Somali Shilling", "minor_unit": 2},
      {"code": "SRD", "numeric_code": "968", "name": "Surinam Dollar", "minor_unit": 2},
      {"code": "SSP", "numeric_code": "728", "name": "South Sudanese Pound", "minor_unit": 2},
      {"code": "STN", "numeric_code": "930", "name": "Dobra", "minor_unit": 2},
      {"code": "SVC", "numeric_code": "222", "name": "El Salvador Colon", "minor_unit": 2},
      {"code": "SYP", "numeric_code": "760", "name": "Syrian Pound", "minor_unit": 2},
      {"code": "SZL", "numeric_code": "748", "name": "Lilangeni", "minor_unit": 2},
      {"code": "THB", "numeric_code": "764", "name": "Baht", "minor_unit": 2},
      {"code": "TJS", "numeric_code": "972", "name": "Somoni", "minor_unit": 2},
      {"code": "TMT", "numeric_code": "934", "name": "Turkmenistan New Manat", "minor_unit": 2},
      {"code": "TND", "numeric_code": "788", "name": "Tunisian Dinar", "minor_unit": 3},
      {"code": "TOP", "numeric_code": "776", "name": "Pa'anga", "minor_unit": 2},
      {"code": "TRY", "numeric_code": "949", "name": "Turkish Lira", "minor_unit": 2},
      {"code": "TTD", "numeric_code": "780", "name": "Trinidad and Tobago Dollar", "minor_unit": 2},
      {"code": "TWD", "numeric_code": "901", "name": "New Taiwan Dollar", "minor_unit": 2},
      {"code": "TZS", "numeric_code": "834", "name": "Tanzanian Shilling", "minor_unit": 2},
      {"code": "UAH", "numeric_code": "980", "name": "Hryvnia", "minor_unit": 2},
      {"code": "UGX", "numeric_code": "800", "name": "Uganda Shilling", "minor_unit": 0},
      {"code": "USD", "numeric_code": "840", "name": "US Dollar", "minor_unit": 2},
      {"code": "USN", "numeric_code": "997", "name": "US Dollar (Next day)", "minor_unit": 2},
      {"code": "UYI", "numeric_code": "940", "name": "Uruguay Peso en Unidades Indexadas (UI)", "minor_unit": 0},
      {"code": "UYU", "numeric_code": "858", "name": "Peso Uruguayo", "minor_unit": 2},
      {"code": "UZS", "numeric_code": "860", "name": "Uzbekistan Sum", "minor_unit": 2},
      {"code": "VED", "numeric_code": "926", "name": "Bolivar Soberano", "minor_unit": 2},
      {"code": "VES", "numeric_code": "928", "name": "Bolivar Soberano", "minor_unit": 2},
      {"code": "VND", "numeric_code": "704", "name": "Dong", "minor_unit": 0},
      {"code": "VUV", "numeric_code": "548", "name": "Vatu", "minor_unit": 0},
      {"code": "WST", "numeric_code": "882", "name": "Tala", "minor_unit": 2},
      {"code": "XAF", "numeric_code": "950", "name": "CFA Franc BEAC", "minor_unit": 0},
      {"code": "XAG", "numeric_code": "961", "name": "Silver", "minor_unit": None},
      {"code": "XAU", "numeric_code": "959", "name": "Gold", "minor_unit": None},
      {"code": "XBA", "numeric_code": "955", "name": "Bond Markets Unit European Composite Unit (EURCO)", "minor_unit": None},
      {"code": "XBB", "numeric_code": "956", "name": "Bond Markets Unit European Monetary Unit (E.M.U.-6)", "minor_unit": None},
      {"code": "XBC", "numeric_code": "957", "name": "Bond Markets Unit European Unit of Account 9 (E.U.A.-9)", "minor_unit": None},
      {"code": "XBD", "numeric_code": "958", "name": "Bond Markets Unit European Unit of Account 17 (E.U.A.-17)", "minor_unit": None},
      {"code": "XCD", "numeric_code": "951", "name": "East Caribbean Dollar", "minor_unit": 2},
      {"code": "XDR", "numeric_code": "960", "name": "SDR (Special Drawing Right)", "minor_unit": None},
      {"code": "XOF", "numeric_code": "952", "name": "CFA Franc BCEAO", "minor_unit": 0},
      {"code": "XPD", "numeric_code": "964", "name": "Palladium", "minor_unit": None},
      {"code": "XPF", "numeric_code": "953", "name": "CFP Franc", "minor_unit": 0},
      {"code": "XPT", "numeric_code": "962", "name": "Platinum", "minor_unit": None},
      {"code": "XSU", "numeric_code": "994", "name": "Sucre", "minor_unit": None},
      {"code": "XTS", "numeric_code": "963", "name": "Codes specifically reserved for testing purposes", "minor_unit": None},
      {"code": "XUA", "numeric_code": "965", "name": "ADB Unit of Account", "minor_unit": None},
      {"code": "XXX", "numeric_code": "999", "name": "The codes assigned for transactions where no currency is involved", "minor_unit": None},
      {"code": "YER", "numeric_code": "886", "name": "Yemeni Rial", "minor_unit": 2},
      {"code": "ZAR", "numeric_code": "710", "name": "Rand", "minor_unit": 2},
      {"code": "ZMW", "numeric_code": "967", "name": "Zambian Kwacha", "minor_unit": 2},
      {"code": "ZWG", "numeric_code": "924", "name": "Zimbabwe Gold", "minor_unit": 2},
  )
  ```

- [ ] **Step 4: Run tests to verify they pass**

  Run: `python -m pytest tests/db/test_iso_4217_snapshot.py -v`
  Expected: 7 passed.

- [ ] **Step 5: Commit**

  ```powershell
  git add backend/ums_smart_revenue/db/iso_4217_2026_05.py tests/db/test_iso_4217_snapshot.py
  git commit -m "chore(db): add immutable ISO 4217 snapshot module (2026-05)"
  ```

### Task 1.2: Test the `CurrencyORM` shape (RED)

**Files:**
- Test: `tests/db/test_source_models.py`

- [ ] **Step 1: Write the failing test**

  ```python
  # tests/db/test_source_models.py
  """ORM shape tests for source_models.CurrencyORM and
  source_models.GoogleRevenueSourceRowORM.

  These are SQLite-friendly assertions via metadata.create_all(). The
  PostgreSQL-backed migration round-trip lives at
  tests/db/test_google_revenue_source_migration.py.
  """

  from sqlalchemy import (
      Boolean,
      CheckConstraint,
      Integer,
      Text,
      UniqueConstraint,
  )

  from ums_smart_revenue.db.source_models import CurrencyORM


  def test_currency_orm_table_name() -> None:
      assert CurrencyORM.__tablename__ == "currencies"


  def test_currency_orm_columns() -> None:
      columns = {column.name: column for column in CurrencyORM.__table__.columns}
      assert set(columns) == {
          "code",
          "numeric_code",
          "name",
          "minor_unit",
          "is_supported",
          "activated_at",
      }
      assert columns["code"].primary_key is True
      assert isinstance(columns["code"].type, Text)
      assert isinstance(columns["numeric_code"].type, Text)
      assert isinstance(columns["name"].type, Text)
      assert isinstance(columns["minor_unit"].type, Integer)
      assert columns["minor_unit"].nullable is True
      assert isinstance(columns["is_supported"].type, Boolean)
      assert columns["is_supported"].nullable is False
      assert columns["activated_at"].nullable is True


  def test_currency_orm_unique_numeric_code() -> None:
      uniques = [
          c for c in CurrencyORM.__table__.constraints if isinstance(c, UniqueConstraint)
      ]
      named = {c.name for c in uniques}
      assert "uq_currencies_numeric_code" in named


  def test_currency_orm_checks() -> None:
      checks = [
          c for c in CurrencyORM.__table__.constraints if isinstance(c, CheckConstraint)
      ]
      names = {c.name for c in checks}
      assert "ck_currencies_code_format" in names
      assert "ck_currencies_numeric_code_format" in names
      assert "ck_currencies_minor_unit_range" in names
      assert "ck_currencies_supported_minor" in names
      assert "ck_currencies_supported_activated" in names
  ```

- [ ] **Step 2: Run test to verify failure**

  Run: `python -m pytest tests/db/test_source_models.py::test_currency_orm_table_name -v`
  Expected: ImportError on `ums_smart_revenue.db.source_models`.

### Task 1.3: Implement `CurrencyORM` in `db/source_models.py`

**Files:**
- Create: `backend/ums_smart_revenue/db/source_models.py`

- [ ] **Step 1: Write the ORM module**

  ```python
  # backend/ums_smart_revenue/db/source_models.py
  """SQLAlchemy ORM models for source-reported revenue ingestion.

  Tables defined here register on FinanceBase.metadata so they share the
  Alembic target metadata that env.py already imports for finance models.
  CurrencyORM is platform-wide reference data with no tenant column.
  GoogleRevenueSourceRowORM is tenant-scoped per spec §4.
  """

  from datetime import datetime
  from decimal import Decimal
  from uuid import UUID

  from sqlalchemy import (
      Boolean,
      CheckConstraint,
      Date,
      DateTime,
      ForeignKeyConstraint,
      Index,
      Integer,
      Numeric,
      Text,
      UniqueConstraint,
      Uuid,
      func,
      text,
  )
  from sqlalchemy.dialects import postgresql
  from sqlalchemy.orm import Mapped, mapped_column
  from sqlalchemy.types import JSON

  from ums_smart_revenue.db.finance_models import FinanceBase
  from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

  # ============================================================================
  # Purpose: Platform-wide ISO 4217 currency reference table.
  # Database/ORM: currencies table; FinanceBase metadata.
  # Standards: Format checks enforce 3-uppercase-letter codes and 3-digit
  #            numeric codes. minor_unit may be NULL only for non-applicable
  #            ISO entries (funds, precious metals, test codes); supported
  #            rows must declare a known minor_unit. activated_at is set
  #            when is_supported flips to TRUE.
  # Blast Radius: Reference data only — read by validation and seeds. No
  #               graph projection impact detected.
  # Connections:
  #   - File: backend/ums_smart_revenue/db/iso_4217_2026_05.py -> Seed source.
  # ============================================================================
  class CurrencyORM(FinanceBase):
      __tablename__ = "currencies"

      code: Mapped[str] = mapped_column(Text, primary_key=True)
      numeric_code: Mapped[str] = mapped_column(Text, nullable=False)
      name: Mapped[str] = mapped_column(Text, nullable=False)
      minor_unit: Mapped[int | None] = mapped_column(Integer, nullable=True)
      is_supported: Mapped[bool] = mapped_column(
          Boolean, nullable=False, server_default=text("false")
      )
      activated_at: Mapped[datetime | None] = mapped_column(
          DateTime(timezone=True), nullable=True
      )

      __table_args__ = (
          CheckConstraint(
              "length(code) = 3 "
              "AND code = upper(code) "
              "AND substr(code, 1, 1) BETWEEN 'A' AND 'Z' "
              "AND substr(code, 2, 1) BETWEEN 'A' AND 'Z' "
              "AND substr(code, 3, 1) BETWEEN 'A' AND 'Z'",
              name="ck_currencies_code_format",
          ),
          CheckConstraint(
              "length(numeric_code) = 3 "
              "AND substr(numeric_code, 1, 1) BETWEEN '0' AND '9' "
              "AND substr(numeric_code, 2, 1) BETWEEN '0' AND '9' "
              "AND substr(numeric_code, 3, 1) BETWEEN '0' AND '9'",
              name="ck_currencies_numeric_code_format",
          ),
          UniqueConstraint("numeric_code", name="uq_currencies_numeric_code"),
          CheckConstraint(
              "minor_unit IS NULL OR (minor_unit BETWEEN 0 AND 6)",
              name="ck_currencies_minor_unit_range",
          ),
          CheckConstraint(
              "is_supported = false OR minor_unit IS NOT NULL",
              name="ck_currencies_supported_minor",
          ),
          CheckConstraint(
              "is_supported = false OR activated_at IS NOT NULL",
              name="ck_currencies_supported_activated",
          ),
      )


  # GoogleRevenueSourceRowORM ships in Task 2.2.
  ```

- [ ] **Step 2: Run tests to verify they pass**

  Run: `python -m pytest tests/db/test_source_models.py -v`
  Expected: 4 passed.

- [ ] **Step 3: Commit**

  ```powershell
  git add backend/ums_smart_revenue/db/source_models.py tests/db/test_source_models.py
  git commit -m "feat(db): CurrencyORM on FinanceBase with ISO 4217 format checks"
  ```

### Task 1.4: Register `source_models` in Alembic `env.py`

**Files:**
- Modify: `backend/ums_smart_revenue/db/alembic/env.py:12`

- [ ] **Step 1: Add the import**

  Edit `backend/ums_smart_revenue/db/alembic/env.py`. After the existing line:

  ```python
  from ums_smart_revenue.db.tenant_models import TenantBase
  ```

  add:

  ```python
  from ums_smart_revenue.db import source_models  # noqa: F401  # registers tables on FinanceBase
  ```

- [ ] **Step 2: Verify the import resolves**

  Run: `python -c "import ums_smart_revenue.db.alembic.env"`
  Expected: silent success (no traceback).

- [ ] **Step 3: Verify target_metadata still has 6 entries**

  The `target_metadata` list does not change — `source_models` contributes to `FinanceBase.metadata` which is already listed.

- [ ] **Step 4: Commit**

  ```powershell
  git add backend/ums_smart_revenue/db/alembic/env.py
  git commit -m "feat(db): register source_models import in Alembic env"
  ```

### Task 1.5: Integration test — `currencies` create_all + insert/select via SQLite

**Files:**
- Test: `tests/db/test_source_models.py` (extend)

- [ ] **Step 1: Append the integration test**

  Append to `tests/db/test_source_models.py`:

  ```python
  import pytest
  from sqlalchemy import create_engine, select
  from sqlalchemy.orm import Session

  from ums_smart_revenue.db.finance_models import FinanceBase


  @pytest.fixture
  def session() -> Session:
      engine = create_engine("sqlite:///:memory:")
      FinanceBase.metadata.create_all(engine)
      with Session(engine) as s:
          yield s


  def test_insert_and_select_currency_row(session: Session) -> None:
      row = CurrencyORM(
          code="USD",
          numeric_code="840",
          name="US Dollar",
          minor_unit=2,
          is_supported=False,
      )
      session.add(row)
      session.flush()
      reloaded = session.scalar(select(CurrencyORM).where(CurrencyORM.code == "USD"))
      assert reloaded is not None
      assert reloaded.numeric_code == "840"
      assert reloaded.minor_unit == 2
      assert reloaded.is_supported is False
      assert reloaded.activated_at is None
  ```

- [ ] **Step 2: Run tests**

  Run: `python -m pytest tests/db/test_source_models.py -v`
  Expected: 5 passed.

- [ ] **Step 3: Commit**

  ```powershell
  git add tests/db/test_source_models.py
  git commit -m "test(db): CurrencyORM insert/select round-trip via SQLite metadata"
  ```

---

## Phase 2 — `google_revenue_source_rows` ORM + migration

### Task 2.1: Test `GoogleRevenueSourceRowORM` column shape (RED)

**Files:**
- Test: `tests/db/test_source_models.py` (extend)

- [ ] **Step 1: Append column-shape assertions**

  ```python
  from sqlalchemy.types import JSON
  from sqlalchemy import Date, DateTime, ForeignKeyConstraint, Index, Numeric, Uuid

  from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM


  def test_google_revenue_source_row_table_name() -> None:
      assert GoogleRevenueSourceRowORM.__tablename__ == "google_revenue_source_rows"


  def test_google_revenue_source_row_columns() -> None:
      columns = {c.name: c for c in GoogleRevenueSourceRowORM.__table__.columns}
      expected = {
          "id",
          "tenant_id",
          "source_system",
          "source_row_key",
          "source_account_id",
          "content_owner_id",
          "youtube_channel_id",
          "report_type",
          "report_month",
          "period_start",
          "period_end",
          "metric_key",
          "value_kind",
          "amount_native",
          "currency_code",
          "source_report_id",
          "raw_file_id",
          "raw_payload",
          "imported_by",
          "ingested_at",
      }
      assert set(columns) == expected
      assert columns["id"].primary_key is True
      assert columns["tenant_id"].nullable is False
      assert columns["source_system"].nullable is False
      assert columns["source_row_key"].nullable is False
      assert columns["source_account_id"].nullable is False
      assert columns["content_owner_id"].nullable is True
      assert columns["youtube_channel_id"].nullable is True
      assert columns["report_type"].nullable is False
      assert columns["report_month"].nullable is False
      assert columns["period_start"].nullable is False
      assert columns["period_end"].nullable is False
      assert columns["metric_key"].nullable is False
      assert columns["value_kind"].nullable is False
      assert columns["amount_native"].nullable is False
      assert columns["currency_code"].nullable is False
      assert columns["source_report_id"].nullable is True
      assert columns["raw_file_id"].nullable is True
      assert columns["raw_payload"].nullable is False
      assert columns["imported_by"].nullable is True
      assert columns["ingested_at"].nullable is False


  def test_google_revenue_source_row_unique_source_row_key() -> None:
      uniques = [
          c for c in GoogleRevenueSourceRowORM.__table__.constraints
          if isinstance(c, UniqueConstraint)
      ]
      named = {c.name for c in uniques}
      assert "uq_google_revenue_source_rows_source_key" in named


  def test_google_revenue_source_row_tenant_fk_present() -> None:
      fks = [
          c for c in GoogleRevenueSourceRowORM.__table__.constraints
          if isinstance(c, ForeignKeyConstraint)
      ]
      target_tables = {fk.referred_table.name for fk in fks}
      assert "tenants" in target_tables
      assert "currencies" in target_tables


  def test_google_revenue_source_row_indexes() -> None:
      index_names = {ix.name for ix in GoogleRevenueSourceRowORM.__table__.indexes}
      assert "ix_google_revenue_source_rows_tenant_month_source" in index_names
      assert "ix_google_revenue_source_rows_tenant_channel_month" in index_names
  ```

- [ ] **Step 2: Run and confirm failure**

  Run: `python -m pytest tests/db/test_source_models.py -v`
  Expected: ImportError on `GoogleRevenueSourceRowORM`.

### Task 2.2: Implement `GoogleRevenueSourceRowORM`

**Files:**
- Modify: `backend/ums_smart_revenue/db/source_models.py`

- [ ] **Step 1: Append the ORM class**

  ```python
  # ============================================================================
  # Purpose: Tenant-scoped storage for Google/YouTube/AdSense source-reported
  #          monetary source rows. Idempotent on
  #          (tenant_id, source_system, source_row_key); source_row_key is a
  #          full 64-char SHA-256 hex digest derived from stable Google
  #          identifiers + dimensions + period + report identifiers.
  # Database/ORM: google_revenue_source_rows table; FinanceBase metadata.
  # Standards: All monetary values preserved exactly as the Google source
  #            reported (amount_native + currency_code). Native-precision
  #            NUMERIC(20, 6) avoids float loss. raw_payload is JSONB on
  #            PostgreSQL, JSON elsewhere via the with_variant pattern.
  # Blast Radius: Source-of-truth table for downstream finance ingestion. No
  #               graph projection impact detected.
  # Connections:
  #   - File: Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md -> §4 schema.
  #   - File: backend/ums_smart_revenue/connectors/google_source_rows/repository.py -> Storage repository.
  # ============================================================================
  class GoogleRevenueSourceRowORM(FinanceBase):
      __tablename__ = "google_revenue_source_rows"

      id: Mapped[UUID] = mapped_column(
          Uuid(as_uuid=True),
          primary_key=True,
          server_default=text("gen_random_uuid()"),
      )
      tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
      source_system: Mapped[str] = mapped_column(Text, nullable=False)
      source_row_key: Mapped[str] = mapped_column(Text, nullable=False)
      source_account_id: Mapped[str] = mapped_column(Text, nullable=False)
      content_owner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
      youtube_channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
      report_type: Mapped[str] = mapped_column(Text, nullable=False)
      report_month: Mapped[str] = mapped_column(Text, nullable=False)
      period_start: Mapped[Date] = mapped_column(Date, nullable=False)
      period_end: Mapped[Date] = mapped_column(Date, nullable=False)
      metric_key: Mapped[str] = mapped_column(Text, nullable=False)
      value_kind: Mapped[str] = mapped_column(Text, nullable=False)
      amount_native: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
      currency_code: Mapped[str] = mapped_column(Text, nullable=False)
      source_report_id: Mapped[str | None] = mapped_column(Text, nullable=True)
      raw_file_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
      raw_payload: Mapped[dict[str, object]] = mapped_column(
          JSON().with_variant(postgresql.JSONB(), "postgresql"),
          nullable=False,
          server_default=text("'{}'"),
      )
      imported_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
      ingested_at: Mapped[datetime] = mapped_column(
          DateTime(timezone=True), nullable=False, server_default=func.now()
      )

      __table_args__ = (
          ForeignKeyConstraint(
              ["tenant_id"], ["tenants.id"],
              name="fk_google_revenue_source_rows_tenant",
              ondelete="RESTRICT",
          ),
          ForeignKeyConstraint(
              ["currency_code"], ["currencies.code"],
              name="fk_google_revenue_source_rows_currency",
          ),
          UniqueConstraint(
              "tenant_id", "source_system", "source_row_key",
              name="uq_google_revenue_source_rows_source_key",
          ),
          CheckConstraint("amount_native >= 0", name="ck_google_revenue_source_rows_nonneg"),
          CheckConstraint(
              "source_system IN ('youtube_reporting', 'youtube_analytics', 'adsense_management')",
              name="ck_google_revenue_source_rows_source_system",
          ),
          CheckConstraint(
              "value_kind IN ('estimated', 'settled', 'adjustment', 'tax', 'deduction')",
              name="ck_google_revenue_source_rows_value_kind",
          ),
          CheckConstraint(
              "length(report_month) = 7 AND substr(report_month, 5, 1) = '-' "
              "AND substr(report_month, 1, 1) BETWEEN '0' AND '9' "
              "AND substr(report_month, 2, 1) BETWEEN '0' AND '9' "
              "AND substr(report_month, 3, 1) BETWEEN '0' AND '9' "
              "AND substr(report_month, 4, 1) BETWEEN '0' AND '9' "
              "AND substr(report_month, 6, 2) BETWEEN '01' AND '12'",
              name="ck_google_revenue_source_rows_report_month_format",
          ),
          CheckConstraint(
              "length(source_row_key) = 64",
              name="ck_google_revenue_source_rows_source_row_key_length",
          ),
          Index(
              "ix_google_revenue_source_rows_tenant_month_source",
              "tenant_id", "report_month", "source_system",
          ),
          Index(
              "ix_google_revenue_source_rows_tenant_channel_month",
              "tenant_id", "youtube_channel_id", "report_month",
              postgresql_where=text("youtube_channel_id IS NOT NULL"),
              sqlite_where=text("youtube_channel_id IS NOT NULL"),
          ),
      )
  ```

- [ ] **Step 2: Run tests**

  Run: `python -m pytest tests/db/test_source_models.py -v`
  Expected: all green (10+ tests).

- [ ] **Step 3: Commit**

  ```powershell
  git add backend/ums_smart_revenue/db/source_models.py tests/db/test_source_models.py
  git commit -m "feat(db): GoogleRevenueSourceRowORM with idempotent source key and partial channel index"
  ```

### Task 2.3: Write Alembic migration `20260523_0001_google_revenue_source_foundation` (upgrade)

**Files:**
- Create: `backend/ums_smart_revenue/db/alembic/versions/20260523_0001_google_revenue_source_foundation.py`

- [ ] **Step 1: Write the migration**

  ```python
  """Google revenue source ingestion foundation (currencies + google_revenue_source_rows).

  Revision ID: 20260523_0001
  Revises: 20260521_0001
  Create Date: 2026-05-23

  Spec: Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md
  """

  import sqlalchemy as sa
  from alembic import op
  from sqlalchemy.dialects import postgresql

  from ums_smart_revenue.db.iso_4217_2026_05 import ISO_4217_CURRENCIES_2026_05

  revision = "20260523_0001"
  down_revision = "20260521_0001"
  branch_labels = None
  depends_on = None

  _SUPPORTED_V1_CODES = ("AED", "USD", "EUR", "GBP", "SAR", "EGP")


  def upgrade() -> None:
      _create_currencies_table()
      _seed_currencies()
      _flip_v1_supported_set()
      _create_google_revenue_source_rows_table()


  def downgrade() -> None:
      op.drop_index(
          "ix_google_revenue_source_rows_tenant_channel_month",
          table_name="google_revenue_source_rows",
      )
      op.drop_index(
          "ix_google_revenue_source_rows_tenant_month_source",
          table_name="google_revenue_source_rows",
      )
      op.drop_table("google_revenue_source_rows")
      op.drop_table("currencies")


  def _create_currencies_table() -> None:
      op.create_table(
          "currencies",
          sa.Column("code", sa.Text(), primary_key=True),
          sa.Column("numeric_code", sa.Text(), nullable=False),
          sa.Column("name", sa.Text(), nullable=False),
          sa.Column("minor_unit", sa.Integer(), nullable=True),
          sa.Column(
              "is_supported", sa.Boolean(), nullable=False, server_default=sa.text("false")
          ),
          sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
          sa.CheckConstraint(
              "length(code) = 3 "
              "AND code = upper(code) "
              "AND substr(code, 1, 1) BETWEEN 'A' AND 'Z' "
              "AND substr(code, 2, 1) BETWEEN 'A' AND 'Z' "
              "AND substr(code, 3, 1) BETWEEN 'A' AND 'Z'",
              name="ck_currencies_code_format",
          ),
          sa.CheckConstraint(
              "length(numeric_code) = 3 "
              "AND substr(numeric_code, 1, 1) BETWEEN '0' AND '9' "
              "AND substr(numeric_code, 2, 1) BETWEEN '0' AND '9' "
              "AND substr(numeric_code, 3, 1) BETWEEN '0' AND '9'",
              name="ck_currencies_numeric_code_format",
          ),
          sa.UniqueConstraint("numeric_code", name="uq_currencies_numeric_code"),
          sa.CheckConstraint(
              "minor_unit IS NULL OR (minor_unit BETWEEN 0 AND 6)",
              name="ck_currencies_minor_unit_range",
          ),
          sa.CheckConstraint(
              "is_supported = false OR minor_unit IS NOT NULL",
              name="ck_currencies_supported_minor",
          ),
          sa.CheckConstraint(
              "is_supported = false OR activated_at IS NOT NULL",
              name="ck_currencies_supported_activated",
          ),
      )


  def _seed_currencies() -> None:
      currencies_table = sa.table(
          "currencies",
          sa.column("code", sa.Text()),
          sa.column("numeric_code", sa.Text()),
          sa.column("name", sa.Text()),
          sa.column("minor_unit", sa.Integer()),
      )
      op.bulk_insert(
          currencies_table,
          [
              {
                  "code": row["code"],
                  "numeric_code": row["numeric_code"],
                  "name": row["name"],
                  "minor_unit": row["minor_unit"],
              }
              for row in ISO_4217_CURRENCIES_2026_05
          ],
      )


  def _flip_v1_supported_set() -> None:
      placeholders = ",".join(f"'{c}'" for c in _SUPPORTED_V1_CODES)
      op.execute(
          f"UPDATE currencies SET is_supported = true, activated_at = now() "
          f"WHERE code IN ({placeholders})"
      )


  def _create_google_revenue_source_rows_table() -> None:
      op.create_table(
          "google_revenue_source_rows",
          sa.Column(
              "id",
              sa.Uuid(),
              primary_key=True,
              server_default=sa.text("gen_random_uuid()"),
          ),
          sa.Column("tenant_id", sa.Uuid(), nullable=False),
          sa.Column("source_system", sa.Text(), nullable=False),
          sa.Column("source_row_key", sa.Text(), nullable=False),
          sa.Column("source_account_id", sa.Text(), nullable=False),
          sa.Column("content_owner_id", sa.Text(), nullable=True),
          sa.Column("youtube_channel_id", sa.Text(), nullable=True),
          sa.Column("report_type", sa.Text(), nullable=False),
          sa.Column("report_month", sa.Text(), nullable=False),
          sa.Column("period_start", sa.Date(), nullable=False),
          sa.Column("period_end", sa.Date(), nullable=False),
          sa.Column("metric_key", sa.Text(), nullable=False),
          sa.Column("value_kind", sa.Text(), nullable=False),
          sa.Column("amount_native", sa.Numeric(20, 6), nullable=False),
          sa.Column("currency_code", sa.Text(), nullable=False),
          sa.Column("source_report_id", sa.Text(), nullable=True),
          sa.Column("raw_file_id", sa.Uuid(), nullable=True),
          sa.Column(
              "raw_payload",
              sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
              nullable=False,
              server_default=sa.text("'{}'::jsonb"),
          ),
          sa.Column("imported_by", sa.Uuid(), nullable=True),
          sa.Column(
              "ingested_at",
              sa.DateTime(timezone=True),
              nullable=False,
              server_default=sa.func.now(),
          ),
          sa.ForeignKeyConstraint(
              ["tenant_id"], ["tenants.id"],
              name="fk_google_revenue_source_rows_tenant",
              ondelete="RESTRICT",
          ),
          sa.ForeignKeyConstraint(
              ["currency_code"], ["currencies.code"],
              name="fk_google_revenue_source_rows_currency",
          ),
          sa.UniqueConstraint(
              "tenant_id", "source_system", "source_row_key",
              name="uq_google_revenue_source_rows_source_key",
          ),
          sa.CheckConstraint("amount_native >= 0", name="ck_google_revenue_source_rows_nonneg"),
          sa.CheckConstraint(
              "source_system IN ('youtube_reporting', 'youtube_analytics', 'adsense_management')",
              name="ck_google_revenue_source_rows_source_system",
          ),
          sa.CheckConstraint(
              "value_kind IN ('estimated', 'settled', 'adjustment', 'tax', 'deduction')",
              name="ck_google_revenue_source_rows_value_kind",
          ),
          sa.CheckConstraint(
              "length(report_month) = 7 AND substr(report_month, 5, 1) = '-' "
              "AND substr(report_month, 1, 1) BETWEEN '0' AND '9' "
              "AND substr(report_month, 2, 1) BETWEEN '0' AND '9' "
              "AND substr(report_month, 3, 1) BETWEEN '0' AND '9' "
              "AND substr(report_month, 4, 1) BETWEEN '0' AND '9' "
              "AND substr(report_month, 6, 2) BETWEEN '01' AND '12'",
              name="ck_google_revenue_source_rows_report_month_format",
          ),
          sa.CheckConstraint(
              "length(source_row_key) = 64",
              name="ck_google_revenue_source_rows_source_row_key_length",
          ),
      )
      op.create_index(
          "ix_google_revenue_source_rows_tenant_month_source",
          "google_revenue_source_rows",
          ["tenant_id", "report_month", "source_system"],
      )
      op.create_index(
          "ix_google_revenue_source_rows_tenant_channel_month",
          "google_revenue_source_rows",
          ["tenant_id", "youtube_channel_id", "report_month"],
          postgresql_where=sa.text("youtube_channel_id IS NOT NULL"),
      )
  ```

- [ ] **Step 2: Verify the alembic head sequencing**

  Run: `PYTHONPATH=backend python -m alembic heads`
  Expected: a single head `20260523_0001`.

- [ ] **Step 3: Commit**

  ```powershell
  git add backend/ums_smart_revenue/db/alembic/versions/20260523_0001_google_revenue_source_foundation.py
  git commit -m "feat(db): migration 20260523_0001 — currencies + google_revenue_source_rows"
  ```

### Task 2.4: Lightweight migration test (SQLite metadata-shape; full Postgres round-trip in Phase 8)

**Files:**
- Test: `tests/db/test_google_revenue_source_migration.py`

- [ ] **Step 1: Write the test**

  ```python
  """Lightweight SQLite-friendly assertions for migration revision metadata.

  The full PostgreSQL upgrade -> downgrade -> upgrade round-trip lives at
  tests/db/test_google_revenue_source_migration_postgres.py (Phase 8).
  """

  import importlib


  def test_revision_metadata() -> None:
      module = importlib.import_module(
          "ums_smart_revenue.db.alembic.versions."
          "20260523_0001_google_revenue_source_foundation"
      )
      assert module.revision == "20260523_0001"
      assert module.down_revision == "20260521_0001"
      assert module.branch_labels is None
      assert module.depends_on is None


  def test_supported_v1_set_is_complete_in_migration_constant() -> None:
      module = importlib.import_module(
          "ums_smart_revenue.db.alembic.versions."
          "20260523_0001_google_revenue_source_foundation"
      )
      assert module._SUPPORTED_V1_CODES == ("AED", "USD", "EUR", "GBP", "SAR", "EGP")
  ```

- [ ] **Step 2: Run**

  Run: `python -m pytest tests/db/test_google_revenue_source_migration.py -v`
  Expected: 2 passed.

- [ ] **Step 3: Commit**

  ```powershell
  git add tests/db/test_google_revenue_source_migration.py
  git commit -m "test(db): migration revision metadata assertions for 20260523_0001"
  ```

### Task 2.5: Verify ORM ↔ migration parity via existing pattern

**Files:**
- No new files; verification step.

- [ ] **Step 1: Sanity check the table list registered on FinanceBase**

  ```powershell
  python -c "from ums_smart_revenue.db import source_models; from ums_smart_revenue.db.finance_models import FinanceBase; print(sorted(FinanceBase.metadata.tables.keys()))"
  ```
  Expected: list includes both `currencies` and `google_revenue_source_rows`.

- [ ] **Step 2: Confirm no Alembic autogen drift would suggest additional tables**

  This is a manual sanity step. If Phase 8's round-trip test fails because of an unrelated drift, fix it here.

---

## Phase 3 — Repository + dataclasses + error contract

### Task 3.1: Create the `google_source_rows` package + dataclasses

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google_source_rows/__init__.py`
- Create: `backend/ums_smart_revenue/connectors/google_source_rows/dataclasses.py`

- [ ] **Step 1: Create the package marker**

  ```python
  # backend/ums_smart_revenue/connectors/google_source_rows/__init__.py
  """Google revenue source-row storage repository and shared dataclasses.

  Parsers (one level up at connectors/google_source_parsers/) emit
  ParsedSourceRow instances; this package writes them to
  google_revenue_source_rows.
  """

  from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
      GoogleRevenueSourceRowEntry,
      GoogleRevenueSourceRowError,
      GoogleRevenueSourceRowValidationError,
      ParsedSourceRow,
  )
  from ums_smart_revenue.connectors.google_source_rows.repository import (
      SqlAlchemyCurrenciesRepository,
      SqlAlchemyGoogleRevenueSourceRowRepository,
  )

  __all__ = [
      "GoogleRevenueSourceRowEntry",
      "GoogleRevenueSourceRowError",
      "GoogleRevenueSourceRowValidationError",
      "ParsedSourceRow",
      "SqlAlchemyCurrenciesRepository",
      "SqlAlchemyGoogleRevenueSourceRowRepository",
  ]
  ```

- [ ] **Step 2: Write the dataclasses module**

  ```python
  # backend/ums_smart_revenue/connectors/google_source_rows/dataclasses.py
  """Immutable IO dataclasses + error contract for the source-row repository.

  ParsedSourceRow is the parser/repository boundary type. The 64-char
  source_row_key is computed by parsers (see
  connectors/google_source_parsers/source_row_keys.py) and never
  recomputed inside the repository.
  """

  from dataclasses import dataclass
  from datetime import date, datetime
  from decimal import Decimal
  from typing import Final
  from uuid import UUID

  ALLOWED_SOURCE_SYSTEMS: Final[frozenset[str]] = frozenset(
      {"youtube_reporting", "youtube_analytics", "adsense_management"}
  )
  ALLOWED_VALUE_KINDS: Final[frozenset[str]] = frozenset(
      {"estimated", "settled", "adjustment", "tax", "deduction"}
  )
  SOURCE_ROW_KEY_LENGTH: Final[int] = 64  # SHA-256 hex digest length


  @dataclass(frozen=True)
  class IsoCurrency:
      code: str
      numeric_code: str
      name: str
      minor_unit: int | None
      is_supported: bool
      activated_at: datetime | None


  @dataclass(frozen=True)
  class ParsedSourceRow:
      source_system: str
      source_row_key: str
      source_account_id: str
      content_owner_id: str | None
      youtube_channel_id: str | None
      report_type: str
      report_month: str  # YYYY-MM
      period_start: date
      period_end: date
      metric_key: str
      value_kind: str
      amount_native: Decimal
      currency_code: str
      source_report_id: str | None
      raw_payload: dict[str, object]


  @dataclass(frozen=True)
  class GoogleRevenueSourceRowEntry:
      id: str
      tenant_id: str
      source_system: str
      source_row_key: str
      source_account_id: str
      content_owner_id: str | None
      youtube_channel_id: str | None
      report_type: str
      report_month: str
      period_start: date
      period_end: date
      metric_key: str
      value_kind: str
      amount_native: Decimal
      currency_code: str
      source_report_id: str | None
      raw_file_id: str | None
      raw_payload: dict[str, object]
      imported_by: str | None
      ingested_at: datetime


  class GoogleRevenueSourceRowError(ValueError):
      """Base class for source-row repository errors."""


  class GoogleRevenueSourceRowValidationError(GoogleRevenueSourceRowError):
      """Raised when a ParsedSourceRow fails validation before write."""


  class CurrencyValidationError(ValueError):
      """Raised by currency lookup helpers when a code is unknown or malformed."""
  ```

- [ ] **Step 3: Commit**

  ```powershell
  git add backend/ums_smart_revenue/connectors/google_source_rows/__init__.py backend/ums_smart_revenue/connectors/google_source_rows/dataclasses.py
  git commit -m "feat(connectors): google_source_rows package + ParsedSourceRow / entry / error dataclasses"
  ```

### Task 3.2: Test `SqlAlchemyCurrenciesRepository` read-only contract (RED)

**Files:**
- Create: `tests/connectors/google_source_rows/__init__.py`
- Test: `tests/connectors/google_source_rows/test_currencies_repository.py`

- [ ] **Step 1: Add test directory init**

  ```python
  # tests/connectors/google_source_rows/__init__.py
  ```

- [ ] **Step 2: Write the failing tests**

  ```python
  # tests/connectors/google_source_rows/test_currencies_repository.py
  import pytest
  from sqlalchemy import create_engine
  from sqlalchemy.orm import Session

  from ums_smart_revenue.connectors.google_source_rows import (
      SqlAlchemyCurrenciesRepository,
  )
  from ums_smart_revenue.db.finance_models import FinanceBase
  from ums_smart_revenue.db.source_models import CurrencyORM


  @pytest.fixture
  def session() -> Session:
      engine = create_engine("sqlite:///:memory:")
      FinanceBase.metadata.create_all(engine)
      with Session(engine) as s:
          # Seed a minimal supported + unsupported pair.
          from datetime import UTC, datetime
          s.add_all([
              CurrencyORM(
                  code="USD", numeric_code="840", name="US Dollar",
                  minor_unit=2, is_supported=True,
                  activated_at=datetime.now(UTC),
              ),
              CurrencyORM(
                  code="EUR", numeric_code="978", name="Euro",
                  minor_unit=2, is_supported=True,
                  activated_at=datetime.now(UTC),
              ),
              CurrencyORM(
                  code="XTS", numeric_code="963", name="Test", minor_unit=None,
                  is_supported=False, activated_at=None,
              ),
          ])
          s.flush()
          yield s


  def test_list_all_returns_every_row(session: Session) -> None:
      repo = SqlAlchemyCurrenciesRepository(session)
      rows = repo.list_all()
      assert {r.code for r in rows} == {"USD", "EUR", "XTS"}


  def test_list_supported_filters_to_supported_rows(session: Session) -> None:
      repo = SqlAlchemyCurrenciesRepository(session)
      rows = repo.list_supported()
      assert {r.code for r in rows} == {"USD", "EUR"}
      for row in rows:
          assert row.is_supported is True
          assert row.activated_at is not None


  def test_get_returns_entry_for_known_code(session: Session) -> None:
      repo = SqlAlchemyCurrenciesRepository(session)
      entry = repo.get("USD")
      assert entry is not None
      assert entry.numeric_code == "840"


  def test_get_returns_none_for_unknown_code(session: Session) -> None:
      repo = SqlAlchemyCurrenciesRepository(session)
      assert repo.get("ZZZ") is None


  def test_repository_has_no_write_method(session: Session) -> None:
      repo = SqlAlchemyCurrenciesRepository(session)
      assert not hasattr(repo, "set_supported")
      assert not hasattr(repo, "create")
      assert not hasattr(repo, "update")
      assert not hasattr(repo, "delete")
  ```

- [ ] **Step 3: Run and confirm RED**

  Run: `python -m pytest tests/connectors/google_source_rows/test_currencies_repository.py -v`
  Expected: ImportError on `SqlAlchemyCurrenciesRepository`.

### Task 3.3: Implement `SqlAlchemyCurrenciesRepository` (read-only)

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google_source_rows/repository.py`

- [ ] **Step 1: Write the repository (currencies portion only; GoogleRevenueSourceRow repo lands in Task 3.5)**

  ```python
  # backend/ums_smart_revenue/connectors/google_source_rows/repository.py
  """Storage repositories for Google revenue source rows + ISO currencies.

  SqlAlchemyCurrenciesRepository is intentionally read-only — flipping the
  is_supported flag belongs to a later admin API with its own audit story
  (spec §6). SqlAlchemyGoogleRevenueSourceRowRepository exposes storage
  primitives only: idempotent upsert, tenant-scoped list, channel/month
  list, exact source-key lookup. No conversion, no provider chain.
  """

  from collections.abc import Iterable
  from typing import Final

  from sqlalchemy import select
  from sqlalchemy.orm import Session

  from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
      IsoCurrency,
  )
  from ums_smart_revenue.db.source_models import CurrencyORM


  # ============================================================================
  # Purpose: Read-only access to platform-wide ISO 4217 currency reference data.
  # Database/ORM: currencies (CurrencyORM).
  # Standards: Pure read repository; mutation paths intentionally absent.
  # Blast Radius: Reference data only. No graph projection impact detected.
  # Connections:
  #   - File: backend/ums_smart_revenue/db/source_models.py -> CurrencyORM.
  # ============================================================================
  class SqlAlchemyCurrenciesRepository:
      def __init__(self, session: Session) -> None:
          self._session = session

      def list_all(self) -> list[IsoCurrency]:
          rows = self._session.scalars(select(CurrencyORM).order_by(CurrencyORM.code)).all()
          return [self._to_entry(row) for row in rows]

      def list_supported(self) -> list[IsoCurrency]:
          rows = self._session.scalars(
              select(CurrencyORM)
              .where(CurrencyORM.is_supported.is_(True))
              .order_by(CurrencyORM.code)
          ).all()
          return [self._to_entry(row) for row in rows]

      def get(self, code: str) -> IsoCurrency | None:
          row = self._session.get(CurrencyORM, code)
          return self._to_entry(row) if row is not None else None

      @staticmethod
      def _to_entry(row: CurrencyORM) -> IsoCurrency:
          return IsoCurrency(
              code=row.code,
              numeric_code=row.numeric_code,
              name=row.name,
              minor_unit=row.minor_unit,
              is_supported=row.is_supported,
              activated_at=row.activated_at,
          )


  # SqlAlchemyGoogleRevenueSourceRowRepository ships in Task 3.5.
  ```

- [ ] **Step 2: Run currencies tests**

  Run: `python -m pytest tests/connectors/google_source_rows/test_currencies_repository.py -v`
  Expected: 5 passed.

- [ ] **Step 3: Commit**

  ```powershell
  git add backend/ums_smart_revenue/connectors/google_source_rows/repository.py tests/connectors/google_source_rows/__init__.py tests/connectors/google_source_rows/test_currencies_repository.py
  git commit -m "feat(connectors): SqlAlchemyCurrenciesRepository (read-only)"
  ```

### Task 3.4: Test `SqlAlchemyGoogleRevenueSourceRowRepository` upsert idempotency + tenant isolation (RED)

**Files:**
- Test: `tests/connectors/google_source_rows/test_repository.py`

- [ ] **Step 1: Write the failing tests**

  ```python
  # tests/connectors/google_source_rows/test_repository.py
  from datetime import date, datetime
  from decimal import Decimal
  from uuid import uuid4

  import pytest
  from sqlalchemy import create_engine, select
  from sqlalchemy.orm import Session

  from ums_smart_revenue.connectors.google_source_rows import (
      GoogleRevenueSourceRowValidationError,
      ParsedSourceRow,
      SqlAlchemyGoogleRevenueSourceRowRepository,
  )
  from ums_smart_revenue.db.finance_models import FinanceBase
  from ums_smart_revenue.db.source_models import (
      CurrencyORM,
      GoogleRevenueSourceRowORM,
  )
  from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM

  TENANT_A = uuid4()
  TENANT_B = uuid4()
  RAW_FILE_ID = uuid4()


  @pytest.fixture
  def session() -> Session:
      engine = create_engine("sqlite:///:memory:")
      FinanceBase.metadata.create_all(engine)
      TenantBase.metadata.create_all(engine)
      with Session(engine) as s:
          s.add_all([
              TenantORM(id=TENANT_A, slug="tenant-a", display_name="Tenant A"),
              TenantORM(id=TENANT_B, slug="tenant-b", display_name="Tenant B"),
              CurrencyORM(
                  code="USD", numeric_code="840", name="US Dollar",
                  minor_unit=2, is_supported=True, activated_at=datetime.now(),
              ),
              CurrencyORM(
                  code="EGP", numeric_code="818", name="Egyptian Pound",
                  minor_unit=2, is_supported=True, activated_at=datetime.now(),
              ),
          ])
          s.flush()
          yield s


  def _row(*, source_row_key: str, amount: str = "1234.560000",
           currency: str = "USD", source_system: str = "youtube_reporting") -> ParsedSourceRow:
      return ParsedSourceRow(
          source_system=source_system,
          source_row_key=source_row_key,
          source_account_id="acct-001",
          content_owner_id=None,
          youtube_channel_id="UC_test_channel",
          report_type="channel_monthly_estimated_revenue",
          report_month="2026-04",
          period_start=date(2026, 4, 1),
          period_end=date(2026, 4, 30),
          metric_key="estimatedRevenue",
          value_kind="estimated",
          amount_native=Decimal(amount),
          currency_code=currency,
          source_report_id="report-001",
          raw_payload={"sample": "payload"},
      )


  def test_upsert_many_inserts_new_rows(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      rows = [_row(source_row_key="a" * 64), _row(source_row_key="b" * 64)]
      written = repo.upsert_many(TENANT_A, rows, raw_file_id=RAW_FILE_ID, imported_by=None)
      assert len(written) == 2

      reloaded = session.scalars(
          select(GoogleRevenueSourceRowORM).where(
              GoogleRevenueSourceRowORM.tenant_id == TENANT_A
          )
      ).all()
      assert len(reloaded) == 2


  def test_upsert_many_is_idempotent_on_rerun(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      rows = [_row(source_row_key="c" * 64)]
      repo.upsert_many(TENANT_A, rows, raw_file_id=RAW_FILE_ID, imported_by=None)
      repo.upsert_many(TENANT_A, rows, raw_file_id=RAW_FILE_ID, imported_by=None)
      count = session.scalar(
          select(func.count(GoogleRevenueSourceRowORM.id)).where(
              GoogleRevenueSourceRowORM.tenant_id == TENANT_A
          )
      ) if False else session.query(GoogleRevenueSourceRowORM).filter(
          GoogleRevenueSourceRowORM.tenant_id == TENANT_A
      ).count()
      assert count == 1


  def test_upsert_many_updates_mutable_fields_on_conflict(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      key = "d" * 64
      repo.upsert_many(TENANT_A, [_row(source_row_key=key, amount="100.000000")],
                       raw_file_id=RAW_FILE_ID, imported_by=None)
      repo.upsert_many(TENANT_A, [_row(source_row_key=key, amount="150.000000")],
                       raw_file_id=RAW_FILE_ID, imported_by=None)
      reloaded = session.scalars(
          select(GoogleRevenueSourceRowORM).where(
              GoogleRevenueSourceRowORM.source_row_key == key
          )
      ).one()
      assert reloaded.amount_native == Decimal("150.000000")


  def test_tenant_isolation(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      shared_key = "e" * 64
      repo.upsert_many(TENANT_A, [_row(source_row_key=shared_key)], raw_file_id=RAW_FILE_ID, imported_by=None)
      repo.upsert_many(TENANT_B, [_row(source_row_key=shared_key)], raw_file_id=RAW_FILE_ID, imported_by=None)
      a_rows = repo.list(TENANT_A, report_month="2026-04")
      b_rows = repo.list(TENANT_B, report_month="2026-04")
      assert len(a_rows) == 1
      assert len(b_rows) == 1
      assert a_rows[0].tenant_id != b_rows[0].tenant_id


  def test_rejects_invalid_source_system(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      bad = _row(source_row_key="f" * 64, source_system="not_a_real_source")
      with pytest.raises(GoogleRevenueSourceRowValidationError):
          repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


  def test_rejects_short_source_row_key(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      bad = _row(source_row_key="too-short")
      with pytest.raises(GoogleRevenueSourceRowValidationError):
          repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


  def test_rejects_negative_amount(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      bad = _row(source_row_key="g" * 64, amount="-1.000000")
      with pytest.raises(GoogleRevenueSourceRowValidationError):
          repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


  def test_rejects_unknown_currency(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      bad = _row(source_row_key="h" * 64, currency="ZZZ")
      with pytest.raises(GoogleRevenueSourceRowValidationError):
          repo.upsert_many(TENANT_A, [bad], raw_file_id=RAW_FILE_ID, imported_by=None)


  def test_list_by_tenant_and_month(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      repo.upsert_many(TENANT_A, [
          _row(source_row_key="i" * 64),
          _row(source_row_key="j" * 64),
      ], raw_file_id=RAW_FILE_ID, imported_by=None)
      rows = repo.list(TENANT_A, report_month="2026-04")
      assert len(rows) == 2


  def test_get_exact_returns_match(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      key = "k" * 64
      repo.upsert_many(TENANT_A, [_row(source_row_key=key)], raw_file_id=RAW_FILE_ID, imported_by=None)
      entry = repo.get_exact(
          TENANT_A, source_system="youtube_reporting", source_row_key=key,
      )
      assert entry is not None
      assert entry.source_row_key == key


  def test_get_exact_returns_none_for_missing(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      entry = repo.get_exact(
          TENANT_A, source_system="youtube_reporting", source_row_key="m" * 64,
      )
      assert entry is None
  ```

- [ ] **Step 2: Run and confirm RED**

  Run: `python -m pytest tests/connectors/google_source_rows/test_repository.py -v`
  Expected: ImportError on `SqlAlchemyGoogleRevenueSourceRowRepository`.

### Task 3.5: Implement `SqlAlchemyGoogleRevenueSourceRowRepository`

**Files:**
- Modify: `backend/ums_smart_revenue/connectors/google_source_rows/repository.py`

- [ ] **Step 1: Append the source-row repository**

  ```python
  # Append to repository.py:

  from collections.abc import Iterable
  from datetime import datetime
  from decimal import Decimal
  from uuid import UUID

  from sqlalchemy.dialects.postgresql import insert as postgresql_insert
  from sqlalchemy.dialects.sqlite import insert as sqlite_insert

  from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
      ALLOWED_SOURCE_SYSTEMS,
      ALLOWED_VALUE_KINDS,
      SOURCE_ROW_KEY_LENGTH,
      GoogleRevenueSourceRowEntry,
      GoogleRevenueSourceRowValidationError,
      ParsedSourceRow,
  )
  from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM


  # ============================================================================
  # Purpose: Storage primitives for Google source-reported revenue rows.
  # Database/ORM: google_revenue_source_rows (GoogleRevenueSourceRowORM),
  #               currencies (FK validation only).
  # Standards: Idempotent upsert via dialect-aware ON CONFLICT keyed on
  #            (tenant_id, source_system, source_row_key). Validates
  #            source_row_key length (64), source_system membership,
  #            value_kind membership, non-negative amount, currency
  #            existence — all before DB write.
  # Blast Radius: Source-of-truth writes. No graph projection impact detected.
  # Connections:
  #   - File: backend/ums_smart_revenue/connectors/google_source_parsers/ ->
  #     emits ParsedSourceRow instances consumed here.
  # ============================================================================
  class SqlAlchemyGoogleRevenueSourceRowRepository:
      def __init__(self, session: Session) -> None:
          self._session = session

      def upsert_many(
          self,
          tenant_id: UUID,
          rows: Iterable[ParsedSourceRow],
          *,
          raw_file_id: UUID | None,
          imported_by: UUID | None,
      ) -> list[GoogleRevenueSourceRowEntry]:
          materialised = list(rows)
          if not materialised:
              return []
          for row in materialised:
              self._validate(row)
          # Pre-check currency existence (FK enforcement at DB level is the
          # final guard, but a domain-level error message is more useful).
          self._require_currencies({r.currency_code for r in materialised})

          written: list[GoogleRevenueSourceRowEntry] = []
          dialect_insert = self._dialect_insert(self._session.get_bind().dialect.name)
          for row in materialised:
              statement = dialect_insert(GoogleRevenueSourceRowORM).values(
                  tenant_id=tenant_id,
                  source_system=row.source_system,
                  source_row_key=row.source_row_key,
                  source_account_id=row.source_account_id,
                  content_owner_id=row.content_owner_id,
                  youtube_channel_id=row.youtube_channel_id,
                  report_type=row.report_type,
                  report_month=row.report_month,
                  period_start=row.period_start,
                  period_end=row.period_end,
                  metric_key=row.metric_key,
                  value_kind=row.value_kind,
                  amount_native=row.amount_native,
                  currency_code=row.currency_code,
                  source_report_id=row.source_report_id,
                  raw_file_id=raw_file_id,
                  raw_payload=row.raw_payload,
                  imported_by=imported_by,
              )
              statement = statement.on_conflict_do_update(
                  index_elements=[
                      GoogleRevenueSourceRowORM.tenant_id,
                      GoogleRevenueSourceRowORM.source_system,
                      GoogleRevenueSourceRowORM.source_row_key,
                  ],
                  set_={
                      "source_account_id": row.source_account_id,
                      "content_owner_id": row.content_owner_id,
                      "youtube_channel_id": row.youtube_channel_id,
                      "report_type": row.report_type,
                      "report_month": row.report_month,
                      "period_start": row.period_start,
                      "period_end": row.period_end,
                      "metric_key": row.metric_key,
                      "value_kind": row.value_kind,
                      "amount_native": row.amount_native,
                      "currency_code": row.currency_code,
                      "source_report_id": row.source_report_id,
                      "raw_file_id": raw_file_id,
                      "raw_payload": row.raw_payload,
                      "imported_by": imported_by,
                  },
              ).returning(GoogleRevenueSourceRowORM)
              orm_row = self._session.execute(statement).scalar_one()
              written.append(self._to_entry(orm_row))
          self._session.flush()
          return written

      def list(
          self,
          tenant_id: UUID,
          *,
          report_month: str | None = None,
          source_system: str | None = None,
      ) -> list[GoogleRevenueSourceRowEntry]:
          stmt = select(GoogleRevenueSourceRowORM).where(
              GoogleRevenueSourceRowORM.tenant_id == tenant_id
          )
          if report_month is not None:
              stmt = stmt.where(GoogleRevenueSourceRowORM.report_month == report_month)
          if source_system is not None:
              stmt = stmt.where(GoogleRevenueSourceRowORM.source_system == source_system)
          rows = self._session.scalars(stmt.order_by(GoogleRevenueSourceRowORM.ingested_at)).all()
          return [self._to_entry(r) for r in rows]

      def list_for_channel(
          self,
          tenant_id: UUID,
          *,
          youtube_channel_id: str,
          report_month: str,
      ) -> list[GoogleRevenueSourceRowEntry]:
          rows = self._session.scalars(
              select(GoogleRevenueSourceRowORM)
              .where(
                  GoogleRevenueSourceRowORM.tenant_id == tenant_id,
                  GoogleRevenueSourceRowORM.youtube_channel_id == youtube_channel_id,
                  GoogleRevenueSourceRowORM.report_month == report_month,
              )
              .order_by(GoogleRevenueSourceRowORM.source_system)
          ).all()
          return [self._to_entry(r) for r in rows]

      def get_exact(
          self,
          tenant_id: UUID,
          *,
          source_system: str,
          source_row_key: str,
      ) -> GoogleRevenueSourceRowEntry | None:
          row = self._session.scalar(
              select(GoogleRevenueSourceRowORM).where(
                  GoogleRevenueSourceRowORM.tenant_id == tenant_id,
                  GoogleRevenueSourceRowORM.source_system == source_system,
                  GoogleRevenueSourceRowORM.source_row_key == source_row_key,
              )
          )
          return self._to_entry(row) if row is not None else None

      def _validate(self, row: ParsedSourceRow) -> None:
          if row.source_system not in ALLOWED_SOURCE_SYSTEMS:
              raise GoogleRevenueSourceRowValidationError(
                  f"unknown source_system: {row.source_system!r}"
              )
          if row.value_kind not in ALLOWED_VALUE_KINDS:
              raise GoogleRevenueSourceRowValidationError(
                  f"unknown value_kind: {row.value_kind!r}"
              )
          if len(row.source_row_key) != SOURCE_ROW_KEY_LENGTH:
              raise GoogleRevenueSourceRowValidationError(
                  f"source_row_key must be {SOURCE_ROW_KEY_LENGTH} chars (got {len(row.source_row_key)})"
              )
          if row.amount_native < 0:
              raise GoogleRevenueSourceRowValidationError("amount_native must be >= 0")
          if not isinstance(row.raw_payload, dict):
              raise GoogleRevenueSourceRowValidationError("raw_payload must be a dict")

      def _require_currencies(self, codes: set[str]) -> None:
          if not codes:
              return
          from ums_smart_revenue.db.source_models import CurrencyORM
          present = set(
              self._session.scalars(
                  select(CurrencyORM.code).where(CurrencyORM.code.in_(codes))
              ).all()
          )
          missing = codes - present
          if missing:
              raise GoogleRevenueSourceRowValidationError(
                  f"unknown currency code(s): {sorted(missing)}"
              )

      @staticmethod
      def _dialect_insert(dialect_name: str):
          if dialect_name == "sqlite":
              return sqlite_insert
          if dialect_name == "postgresql":
              return postgresql_insert
          raise GoogleRevenueSourceRowValidationError(
              f"unsupported dialect for source-row upsert: {dialect_name}"
          )

      @staticmethod
      def _to_entry(row: GoogleRevenueSourceRowORM) -> GoogleRevenueSourceRowEntry:
          return GoogleRevenueSourceRowEntry(
              id=str(row.id),
              tenant_id=str(row.tenant_id),
              source_system=row.source_system,
              source_row_key=row.source_row_key,
              source_account_id=row.source_account_id,
              content_owner_id=row.content_owner_id,
              youtube_channel_id=row.youtube_channel_id,
              report_type=row.report_type,
              report_month=row.report_month,
              period_start=row.period_start,
              period_end=row.period_end,
              metric_key=row.metric_key,
              value_kind=row.value_kind,
              amount_native=row.amount_native,
              currency_code=row.currency_code,
              source_report_id=row.source_report_id,
              raw_file_id=str(row.raw_file_id) if row.raw_file_id else None,
              raw_payload=dict(row.raw_payload or {}),
              imported_by=str(row.imported_by) if row.imported_by else None,
              ingested_at=row.ingested_at,
          )
  ```

- [ ] **Step 2: Run repository tests**

  Run: `python -m pytest tests/connectors/google_source_rows/test_repository.py -v`
  Expected: 11 passed.

- [ ] **Step 3: Commit**

  ```powershell
  git add backend/ums_smart_revenue/connectors/google_source_rows/repository.py tests/connectors/google_source_rows/test_repository.py
  git commit -m "feat(connectors): SqlAlchemyGoogleRevenueSourceRowRepository idempotent upsert + tenant-scoped queries"
  ```

### Task 3.6: Add list filters polish + commit

**Files:**
- Test: `tests/connectors/google_source_rows/test_repository.py` (extend)

- [ ] **Step 1: Add a filter-combo test**

  Append:

  ```python
  def test_list_filters_combine(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      repo.upsert_many(TENANT_A, [
          _row(source_row_key="n" * 64, source_system="youtube_reporting"),
          _row(source_row_key="o" * 64, source_system="adsense_management"),
      ], raw_file_id=RAW_FILE_ID, imported_by=None)
      filtered = repo.list(TENANT_A, report_month="2026-04", source_system="youtube_reporting")
      assert len(filtered) == 1
      assert filtered[0].source_system == "youtube_reporting"
  ```

- [ ] **Step 2: Run + commit**

  ```powershell
  python -m pytest tests/connectors/google_source_rows/test_repository.py::test_list_filters_combine -v
  git add tests/connectors/google_source_rows/test_repository.py
  git commit -m "test(connectors): list filter-combo coverage for source-row repository"
  ```

### Task 3.7: Channel/month list coverage

**Files:**
- Test: `tests/connectors/google_source_rows/test_repository.py` (extend)

- [ ] **Step 1: Add channel/month coverage**

  ```python
  def test_list_for_channel_returns_only_matches(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      def row(key, channel):
          base = _row(source_row_key=key)
          # Build a new ParsedSourceRow with the channel override.
          from dataclasses import replace
          return replace(base, youtube_channel_id=channel)
      repo.upsert_many(TENANT_A, [
          row("p" * 64, "UC_alpha"),
          row("q" * 64, "UC_alpha"),
          row("r" * 64, "UC_beta"),
      ], raw_file_id=RAW_FILE_ID, imported_by=None)
      alpha = repo.list_for_channel(TENANT_A, youtube_channel_id="UC_alpha", report_month="2026-04")
      assert len(alpha) == 2
      assert {r.youtube_channel_id for r in alpha} == {"UC_alpha"}
  ```

- [ ] **Step 2: Run + commit**

  ```powershell
  python -m pytest tests/connectors/google_source_rows/test_repository.py::test_list_for_channel_returns_only_matches -v
  git add tests/connectors/google_source_rows/test_repository.py
  git commit -m "test(connectors): list_for_channel coverage for partial channel/month index"
  ```

---

## Phase 4 — Parser protocol + `source_row_keys` + 3 parsers + synthetic fixtures

**Fixture data discipline (hard non-goal — preserved verbatim from operator):** Fixture payloads in `tests/connectors/_fixtures/**` MUST be synthetic or sanitized. No real Google account IDs, no real YouTube channel private metadata, no real revenue figures, no real OAuth payloads, no real credentials in any committed fixture. Account IDs, channel IDs, content owner IDs, and money values are invented. The fixtures' purpose is to exercise the parser contracts, not to mirror production data.

### Task 4.1: Define `SourceRowParser` protocol + parser package skeleton

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google_source_parsers/__init__.py`
- Create: `backend/ums_smart_revenue/connectors/google_source_parsers/base.py`

- [ ] **Step 1: Create the package marker**

  ```python
  # backend/ums_smart_revenue/connectors/google_source_parsers/__init__.py
  """Parsers that translate Google source-report payloads into ParsedSourceRow.

  No OAuth, no live API client, no live download. Each parser takes a
  pre-recorded payload (loaded by tests from
  tests/connectors/_fixtures/) and emits an iterable of ParsedSourceRow.
  """

  from ums_smart_revenue.connectors.google_source_parsers.adsense_management import (
      AdSenseManagementParser,
  )
  from ums_smart_revenue.connectors.google_source_parsers.base import (
      ParserError,
      SourceRowParser,
  )
  from ums_smart_revenue.connectors.google_source_parsers.source_row_keys import (
      build_source_row_key,
  )
  from ums_smart_revenue.connectors.google_source_parsers.youtube_analytics import (
      YouTubeAnalyticsParser,
  )
  from ums_smart_revenue.connectors.google_source_parsers.youtube_reporting import (
      YouTubeReportingParser,
  )

  __all__ = [
      "AdSenseManagementParser",
      "ParserError",
      "SourceRowParser",
      "YouTubeAnalyticsParser",
      "YouTubeReportingParser",
      "build_source_row_key",
  ]
  ```

- [ ] **Step 2: Write the parser protocol + base error**

  ```python
  # backend/ums_smart_revenue/connectors/google_source_parsers/base.py
  """Parser protocol shared by every Google source-system parser.

  Parsers receive a pre-recorded payload + a tenant_id and emit
  ParsedSourceRow instances. They are the only place where
  source_row_key is derived (via source_row_keys.build_source_row_key).
  Repositories never re-derive the key.
  """

  from collections.abc import Iterable
  from typing import Protocol
  from uuid import UUID

  from ums_smart_revenue.connectors.google_source_rows import ParsedSourceRow


  class ParserError(ValueError):
      """Raised when a payload is malformed or violates the parser's contract."""


  class SourceRowParser(Protocol):
      source_system: str

      def parse(
          self,
          payload: dict[str, object],
          *,
          tenant_id: UUID,
      ) -> Iterable[ParsedSourceRow]:
          """Translate a single pre-recorded report payload into ParsedSourceRow rows."""
          ...
  ```

- [ ] **Step 3: Commit (impl files compile even though concrete parsers come in later tasks; defer import-time validation by importing them lazily where needed)**

  ```powershell
  git add backend/ums_smart_revenue/connectors/google_source_parsers/__init__.py backend/ums_smart_revenue/connectors/google_source_parsers/base.py
  git commit -m "feat(connectors): SourceRowParser protocol + ParserError"
  ```

  Note: The `__init__.py` re-export of the 3 concrete parsers references modules created in later tasks. To keep this commit green, comment out the 4 imports/exports of `AdSenseManagementParser`, `YouTubeAnalyticsParser`, `YouTubeReportingParser`, and `build_source_row_key` for now; uncomment them after Task 4.6 / 4.9 / 4.12. **Do not commit broken imports.** Verify with `python -c "import ums_smart_revenue.connectors.google_source_parsers"` before committing.

### Task 4.2: Test `build_source_row_key` deterministic derivation (RED)

**Files:**
- Test: `tests/connectors/google_source_parsers/__init__.py`
- Test: `tests/connectors/google_source_parsers/test_source_row_keys.py`

- [ ] **Step 1: Create test package**

  ```python
  # tests/connectors/google_source_parsers/__init__.py
  ```

- [ ] **Step 2: Write the failing tests**

  ```python
  # tests/connectors/google_source_parsers/test_source_row_keys.py
  """source_row_key derivation must be:
   - deterministic across repeated calls with the same inputs;
   - distinct across different inputs;
   - exactly 64 chars (SHA-256 hex digest);
   - source-system-specific (different prefix => different key).
  """

  import pytest

  from ums_smart_revenue.connectors.google_source_parsers import build_source_row_key


  def test_youtube_reporting_key_is_deterministic() -> None:
      key1 = build_source_row_key(
          source_system="youtube_reporting",
          source_report_id="report-001",
          line_index=42,
          dimensions={"channel": "UC_x", "country": "US"},
      )
      key2 = build_source_row_key(
          source_system="youtube_reporting",
          source_report_id="report-001",
          line_index=42,
          dimensions={"country": "US", "channel": "UC_x"},  # dict order varies
      )
      assert key1 == key2


  def test_youtube_analytics_key_uses_query_signature_and_period() -> None:
      key1 = build_source_row_key(
          source_system="youtube_analytics",
          query_signature="estimatedRevenue|channel,country",
          period_start="2026-04-01",
          period_end="2026-04-30",
          dimensions={"channel": "UC_y", "country": "EG"},
      )
      key2 = build_source_row_key(
          source_system="youtube_analytics",
          query_signature="estimatedRevenue|channel,country",
          period_start="2026-04-01",
          period_end="2026-04-30",
          dimensions={"channel": "UC_y", "country": "EG"},
      )
      assert key1 == key2


  def test_adsense_management_key_uses_account_period_dimensions() -> None:
      key = build_source_row_key(
          source_system="adsense_management",
          source_report_id="adsense-report-2026-04",
          account_id="pub-test-001",
          period_start="2026-04-01",
          period_end="2026-04-30",
          dimensions={"product": "AFC", "country": "EG"},
      )
      assert len(key) == 64
      assert all(c in "0123456789abcdef" for c in key)


  def test_different_inputs_produce_distinct_keys() -> None:
      keys = {
          build_source_row_key(
              source_system="youtube_reporting",
              source_report_id="r-1",
              line_index=0,
              dimensions={"k": "v"},
          ),
          build_source_row_key(
              source_system="youtube_reporting",
              source_report_id="r-2",
              line_index=0,
              dimensions={"k": "v"},
          ),
          build_source_row_key(
              source_system="youtube_reporting",
              source_report_id="r-1",
              line_index=1,
              dimensions={"k": "v"},
          ),
      }
      assert len(keys) == 3


  def test_different_source_systems_produce_distinct_keys() -> None:
      yt = build_source_row_key(
          source_system="youtube_reporting",
          source_report_id="r-1",
          line_index=0,
          dimensions={},
      )
      ana = build_source_row_key(
          source_system="youtube_analytics",
          query_signature="",
          period_start="2026-04-01",
          period_end="2026-04-30",
          dimensions={},
      )
      ads = build_source_row_key(
          source_system="adsense_management",
          source_report_id="r-1",
          account_id="acct",
          period_start="2026-04-01",
          period_end="2026-04-30",
          dimensions={},
      )
      assert len({yt, ana, ads}) == 3


  def test_unknown_source_system_raises() -> None:
      with pytest.raises(ValueError):
          build_source_row_key(source_system="not_a_real_source")  # type: ignore[call-arg]


  def test_key_length_is_64_chars() -> None:
      key = build_source_row_key(
          source_system="youtube_reporting",
          source_report_id="x",
          line_index=0,
          dimensions={},
      )
      assert len(key) == 64
  ```

- [ ] **Step 3: Confirm RED**

  Run: `python -m pytest tests/connectors/google_source_parsers/test_source_row_keys.py -v`
  Expected: ImportError on `build_source_row_key`.

### Task 4.3: Implement `source_row_keys.build_source_row_key`

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google_source_parsers/source_row_keys.py`

- [ ] **Step 1: Write the derivation module**

  ```python
  # backend/ums_smart_revenue/connectors/google_source_parsers/source_row_keys.py
  """Deterministic source_row_key derivation per source_system.

  Returns the full 64-char SHA-256 hex digest of a canonical string built
  from the inputs. The canonical string is source-system-specific so two
  different source systems can never collide even on identical
  identifiers.
  """

  import hashlib
  from typing import Final

  _PREFIX: Final[dict[str, str]] = {
      "youtube_reporting": "yt-rep",
      "youtube_analytics": "yt-ana",
      "adsense_management": "adsense",
  }


  def build_source_row_key(*, source_system: str, **fields: object) -> str:
      if source_system not in _PREFIX:
          raise ValueError(f"unknown source_system: {source_system!r}")
      prefix = _PREFIX[source_system]

      if source_system == "youtube_reporting":
          canonical = (
              f"{prefix}|"
              f"{fields['source_report_id']}|"
              f"{fields['line_index']}|"
              f"{_canonical_dimensions(fields.get('dimensions') or {})}"
          )
      elif source_system == "youtube_analytics":
          canonical = (
              f"{prefix}|"
              f"{fields['query_signature']}|"
              f"{fields['period_start']}|"
              f"{fields['period_end']}|"
              f"{_canonical_dimensions(fields.get('dimensions') or {})}"
          )
      else:  # adsense_management
          canonical = (
              f"{prefix}|"
              f"{fields['source_report_id']}|"
              f"{fields['account_id']}|"
              f"{fields['period_start']}|"
              f"{fields['period_end']}|"
              f"{_canonical_dimensions(fields.get('dimensions') or {})}"
          )

      return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


  def _canonical_dimensions(dimensions: dict[str, object]) -> str:
      """Stable tuple representation of a dimensions dict.

      Dict iteration order is insertion-stable in CPython but this is a
      cross-process key; we sort by key to guarantee stability across runs.
      """
      sorted_items = sorted(dimensions.items(), key=lambda kv: kv[0])
      return "&".join(f"{k}={v}" for k, v in sorted_items)
  ```

- [ ] **Step 2: Uncomment `build_source_row_key` in `__init__.py` and verify import**

  ```powershell
  python -c "from ums_smart_revenue.connectors.google_source_parsers import build_source_row_key; print(build_source_row_key(source_system='youtube_reporting', source_report_id='x', line_index=0, dimensions={}))"
  ```
  Expected: a 64-char hex digest printed.

- [ ] **Step 3: Run tests**

  Run: `python -m pytest tests/connectors/google_source_parsers/test_source_row_keys.py -v`
  Expected: 7 passed.

- [ ] **Step 4: Commit**

  ```powershell
  git add backend/ums_smart_revenue/connectors/google_source_parsers/source_row_keys.py backend/ums_smart_revenue/connectors/google_source_parsers/__init__.py tests/connectors/google_source_parsers/__init__.py tests/connectors/google_source_parsers/test_source_row_keys.py
  git commit -m "feat(parsers): build_source_row_key (SHA-256 hex, source-system-specific canonicalization)"
  ```

### Task 4.4: Create synthetic YouTube Reporting fixture + provenance README

**Files:**
- Create: `tests/connectors/_fixtures/__init__.py`
- Create: `tests/connectors/_fixtures/README.md`
- Create: `tests/connectors/_fixtures/youtube_reporting/__init__.py`
- Create: `tests/connectors/_fixtures/youtube_reporting/sample_estimated_revenue_2026_04.json`
- Create: `tests/connectors/_fixtures/youtube_reporting/sample_estimated_revenue_2026_04_rerun.json`

- [ ] **Step 1: Create the fixtures package marker**

  ```python
  # tests/connectors/_fixtures/__init__.py
  ```

  ```python
  # tests/connectors/_fixtures/youtube_reporting/__init__.py
  ```

- [ ] **Step 2: Write the README declaring fixture provenance**

  ```markdown
  # Fixture provenance

  Every payload in this tree is **synthetic**. None of the account IDs, channel IDs, content owner IDs, OAuth tokens, or money figures correspond to real Google/YouTube/AdSense data.

  Naming convention: `sample_<report_type>_<YYYY_MM>.json` plus a `_rerun.json` sibling that is byte-identical to the first file (used to assert parser/repository idempotency).

  Each fixture mirrors the structural shape Google publicly documents for that report type, with field names matching the upstream API, but the values are invented for B1 testing. Channel IDs follow the `UC_test_<n>` convention. Account IDs follow the `pub-test-<n>` convention for AdSense and `cms-test-<n>` for content owners. Money values are small integers (e.g. `123.456789`) chosen to verify Decimal preservation, not to reflect production amounts.

  Do not replace any fixture with real data without operator approval and a separate audit-logged commit.
  ```

- [ ] **Step 3: Write the YouTube Reporting fixture**

  The YouTube Reporting API delivers reports as downloadable CSV-style payloads. For test ingestion we mirror the API's JSON metadata + a parsed-rows array (typical wrapping a connector would produce after CSV → JSON conversion).

  ```json
  {
    "report_metadata": {
      "report_id": "yt-rep-2026-04-channel-estimated-001",
      "report_type": "channel_basic_a2",
      "job_id": "synthetic-job-001",
      "start_time": "2026-04-01T00:00:00Z",
      "end_time": "2026-04-30T23:59:59Z",
      "create_time": "2026-05-01T03:00:00Z"
    },
    "rows": [
      {
        "line_index": 0,
        "date_range": {"start": "2026-04-01", "end": "2026-04-30"},
        "dimensions": {
          "channel": "UC_test_alpha",
          "country": "US",
          "content_owner": "cms-test-1"
        },
        "metrics": {
          "estimatedRevenue": "1234.560000",
          "currencyCode": "USD"
        }
      },
      {
        "line_index": 1,
        "date_range": {"start": "2026-04-01", "end": "2026-04-30"},
        "dimensions": {
          "channel": "UC_test_alpha",
          "country": "EG",
          "content_owner": "cms-test-1"
        },
        "metrics": {
          "estimatedRevenue": "78.910000",
          "currencyCode": "USD"
        }
      },
      {
        "line_index": 2,
        "date_range": {"start": "2026-04-01", "end": "2026-04-30"},
        "dimensions": {
          "channel": "UC_test_beta",
          "country": "GB",
          "content_owner": "cms-test-1"
        },
        "metrics": {
          "estimatedRevenue": "456.780000",
          "currencyCode": "GBP"
        }
      }
    ]
  }
  ```

- [ ] **Step 4: Create the byte-identical rerun fixture**

  ```powershell
  Copy-Item tests/connectors/_fixtures/youtube_reporting/sample_estimated_revenue_2026_04.json tests/connectors/_fixtures/youtube_reporting/sample_estimated_revenue_2026_04_rerun.json
  ```

- [ ] **Step 5: Commit**

  ```powershell
  git add tests/connectors/_fixtures/
  git commit -m "test(fixtures): synthetic YouTube Reporting estimated-revenue fixture (+ rerun pair) + provenance README"
  ```

### Task 4.5: Test `YouTubeReportingParser` (RED)

**Files:**
- Test: `tests/connectors/google_source_parsers/test_youtube_reporting_parser.py`

- [ ] **Step 1: Write the failing tests**

  ```python
  # tests/connectors/google_source_parsers/test_youtube_reporting_parser.py
  import json
  from datetime import date
  from decimal import Decimal
  from importlib import resources
  from uuid import uuid4

  from ums_smart_revenue.connectors.google_source_parsers import YouTubeReportingParser

  TENANT_ID = uuid4()


  def _load_fixture(name: str) -> dict[str, object]:
      ref = resources.files("tests.connectors._fixtures.youtube_reporting").joinpath(name)
      with ref.open("r", encoding="utf-8") as fh:
          return json.load(fh)


  def test_parse_emits_one_row_per_input_line() -> None:
      payload = _load_fixture("sample_estimated_revenue_2026_04.json")
      parser = YouTubeReportingParser()
      rows = list(parser.parse(payload, tenant_id=TENANT_ID))
      assert len(rows) == 3


  def test_parse_preserves_amount_and_currency_exactly() -> None:
      payload = _load_fixture("sample_estimated_revenue_2026_04.json")
      rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
      amounts_by_channel = {(r.youtube_channel_id, r.amount_native, r.currency_code) for r in rows}
      assert (
          ("UC_test_alpha", Decimal("1234.560000"), "USD") in amounts_by_channel
      )
      assert (
          ("UC_test_beta", Decimal("456.780000"), "GBP") in amounts_by_channel
      )


  def test_parse_sets_value_kind_estimated_and_source_system_youtube_reporting() -> None:
      payload = _load_fixture("sample_estimated_revenue_2026_04.json")
      rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
      assert all(r.source_system == "youtube_reporting" for r in rows)
      assert all(r.value_kind == "estimated" for r in rows)


  def test_parse_sets_period_and_report_month() -> None:
      payload = _load_fixture("sample_estimated_revenue_2026_04.json")
      rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
      for row in rows:
          assert row.period_start == date(2026, 4, 1)
          assert row.period_end == date(2026, 4, 30)
          assert row.report_month == "2026-04"


  def test_parse_carries_source_report_id_and_content_owner() -> None:
      payload = _load_fixture("sample_estimated_revenue_2026_04.json")
      rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
      for row in rows:
          assert row.source_report_id == "yt-rep-2026-04-channel-estimated-001"
          assert row.content_owner_id == "cms-test-1"


  def test_source_row_key_is_deterministic_across_reruns() -> None:
      first = list(YouTubeReportingParser().parse(
          _load_fixture("sample_estimated_revenue_2026_04.json"), tenant_id=TENANT_ID,
      ))
      second = list(YouTubeReportingParser().parse(
          _load_fixture("sample_estimated_revenue_2026_04_rerun.json"), tenant_id=TENANT_ID,
      ))
      first_keys = sorted(r.source_row_key for r in first)
      second_keys = sorted(r.source_row_key for r in second)
      assert first_keys == second_keys
      for key in first_keys:
          assert len(key) == 64


  def test_raw_payload_carries_the_input_row_dict() -> None:
      payload = _load_fixture("sample_estimated_revenue_2026_04.json")
      rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
      for row in rows:
          assert "dimensions" in row.raw_payload
          assert "metrics" in row.raw_payload
  ```

- [ ] **Step 2: Confirm RED**

  Run: `python -m pytest tests/connectors/google_source_parsers/test_youtube_reporting_parser.py -v`
  Expected: ImportError on `YouTubeReportingParser`.

### Task 4.6: Implement `YouTubeReportingParser`

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google_source_parsers/youtube_reporting.py`

- [ ] **Step 1: Write the parser**

  ```python
  # backend/ums_smart_revenue/connectors/google_source_parsers/youtube_reporting.py
  """Parser for YouTube Reporting API report payloads.

  Consumes a pre-recorded payload shaped like the parser-friendly JSON
  the connector would emit after converting the upstream CSV report into
  a dict. Emits ParsedSourceRow instances with value_kind='estimated'.
  """

  from collections.abc import Iterable
  from datetime import date
  from decimal import Decimal
  from uuid import UUID

  from ums_smart_revenue.connectors.google_source_parsers.base import ParserError
  from ums_smart_revenue.connectors.google_source_parsers.source_row_keys import (
      build_source_row_key,
  )
  from ums_smart_revenue.connectors.google_source_rows import ParsedSourceRow


  # ============================================================================
  # Purpose: Translate YouTube Reporting API estimated-revenue payloads into
  #          ParsedSourceRow rows (one per input line). No live download.
  # Database/ORM: None directly. ParsedSourceRow is the parser/repository
  #               boundary.
  # Standards: Source amount + currency preserved exactly. Deterministic
  #            source_row_key via build_source_row_key. value_kind is
  #            'estimated' because the Reporting API reports estimated
  #            revenue, not settled payments.
  # Blast Radius: No DB write. No graph projection impact detected.
  # Connections:
  #   - File: tests/connectors/_fixtures/youtube_reporting/ -> Synthetic
  #     payloads consumed by parser tests.
  # ============================================================================
  class YouTubeReportingParser:
      source_system = "youtube_reporting"

      def parse(
          self,
          payload: dict[str, object],
          *,
          tenant_id: UUID,
      ) -> Iterable[ParsedSourceRow]:
          metadata = self._require_dict(payload, "report_metadata")
          rows = payload.get("rows")
          if not isinstance(rows, list):
              raise ParserError("payload['rows'] must be a list")
          report_id = self._require_str(metadata, "report_id")
          report_type = self._require_str(metadata, "report_type")

          for row in rows:
              if not isinstance(row, dict):
                  raise ParserError("each rows[*] must be a dict")
              line_index = self._require_int(row, "line_index")
              date_range = self._require_dict(row, "date_range")
              period_start = date.fromisoformat(self._require_str(date_range, "start"))
              period_end = date.fromisoformat(self._require_str(date_range, "end"))
              dimensions = self._require_dict(row, "dimensions")
              metrics = self._require_dict(row, "metrics")

              channel = dimensions.get("channel")
              content_owner = dimensions.get("content_owner")
              if not isinstance(channel, str):
                  raise ParserError("dimensions.channel must be a string")

              amount_raw = metrics.get("estimatedRevenue")
              if not isinstance(amount_raw, str):
                  raise ParserError("metrics.estimatedRevenue must be a string for Decimal precision")
              currency = metrics.get("currencyCode")
              if not isinstance(currency, str):
                  raise ParserError("metrics.currencyCode must be a string")

              source_row_key = build_source_row_key(
                  source_system=self.source_system,
                  source_report_id=report_id,
                  line_index=line_index,
                  dimensions=dimensions,
              )

              yield ParsedSourceRow(
                  source_system=self.source_system,
                  source_row_key=source_row_key,
                  source_account_id=str(content_owner) if content_owner else "unknown",
                  content_owner_id=str(content_owner) if content_owner else None,
                  youtube_channel_id=channel,
                  report_type=report_type,
                  report_month=f"{period_start.year:04d}-{period_start.month:02d}",
                  period_start=period_start,
                  period_end=period_end,
                  metric_key="estimatedRevenue",
                  value_kind="estimated",
                  amount_native=Decimal(amount_raw),
                  currency_code=currency,
                  source_report_id=report_id,
                  raw_payload=dict(row),
              )

      @staticmethod
      def _require_dict(d: dict[str, object], key: str) -> dict[str, object]:
          value = d.get(key)
          if not isinstance(value, dict):
              raise ParserError(f"missing or non-dict field: {key!r}")
          return value

      @staticmethod
      def _require_str(d: dict[str, object], key: str) -> str:
          value = d.get(key)
          if not isinstance(value, str):
              raise ParserError(f"missing or non-str field: {key!r}")
          return value

      @staticmethod
      def _require_int(d: dict[str, object], key: str) -> int:
          value = d.get(key)
          if not isinstance(value, int):
              raise ParserError(f"missing or non-int field: {key!r}")
          return value
  ```

- [ ] **Step 2: Uncomment `YouTubeReportingParser` in `__init__.py` and run tests**

  ```powershell
  python -m pytest tests/connectors/google_source_parsers/test_youtube_reporting_parser.py -v
  ```
  Expected: 7 passed.

- [ ] **Step 3: Commit**

  ```powershell
  git add backend/ums_smart_revenue/connectors/google_source_parsers/youtube_reporting.py backend/ums_smart_revenue/connectors/google_source_parsers/__init__.py tests/connectors/google_source_parsers/test_youtube_reporting_parser.py
  git commit -m "feat(parsers): YouTubeReportingParser — estimated revenue preservation + deterministic source_row_key"
  ```

### Task 4.7: Create synthetic YouTube Analytics fixture

**Files:**
- Create: `tests/connectors/_fixtures/youtube_analytics/__init__.py`
- Create: `tests/connectors/_fixtures/youtube_analytics/sample_query_response_2026_04.json`
- Create: `tests/connectors/_fixtures/youtube_analytics/sample_query_response_2026_04_rerun.json`

- [ ] **Step 1: Create package marker**

  ```python
  # tests/connectors/_fixtures/youtube_analytics/__init__.py
  ```

- [ ] **Step 2: Write the synthetic fixture**

  The YouTube Analytics `reports.query` response is a tabular shape: `columnHeaders` + `rows`. We mirror that exactly using only synthetic identifiers.

  ```json
  {
    "query_request": {
      "ids": "contentOwner==cms-test-1",
      "startDate": "2026-04-01",
      "endDate": "2026-04-30",
      "metrics": "estimatedRevenue,grossRevenue",
      "dimensions": "channel,country",
      "currency": "USD"
    },
    "kind": "youtubeAnalytics#resultTable",
    "columnHeaders": [
      {"name": "channel", "columnType": "DIMENSION", "dataType": "STRING"},
      {"name": "country", "columnType": "DIMENSION", "dataType": "STRING"},
      {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
      {"name": "grossRevenue", "columnType": "METRIC", "dataType": "FLOAT"}
    ],
    "rows": [
      ["UC_test_alpha", "US", "1234.567890", "1500.000000"],
      ["UC_test_alpha", "EG", "78.910000",   "95.000000"],
      ["UC_test_beta",  "GB", "456.780000",  "550.000000"]
    ]
  }
  ```

- [ ] **Step 3: Copy to rerun fixture**

  ```powershell
  Copy-Item tests/connectors/_fixtures/youtube_analytics/sample_query_response_2026_04.json tests/connectors/_fixtures/youtube_analytics/sample_query_response_2026_04_rerun.json
  ```

- [ ] **Step 4: Commit**

  ```powershell
  git add tests/connectors/_fixtures/youtube_analytics/
  git commit -m "test(fixtures): synthetic YouTube Analytics query-response fixture (+ rerun pair)"
  ```

### Task 4.8: Test `YouTubeAnalyticsParser` (RED)

**Files:**
- Test: `tests/connectors/google_source_parsers/test_youtube_analytics_parser.py`

- [ ] **Step 1: Write the failing tests**

  ```python
  # tests/connectors/google_source_parsers/test_youtube_analytics_parser.py
  import json
  from datetime import date
  from decimal import Decimal
  from importlib import resources
  from uuid import uuid4

  from ums_smart_revenue.connectors.google_source_parsers import YouTubeAnalyticsParser

  TENANT_ID = uuid4()


  def _load_fixture(name: str) -> dict[str, object]:
      ref = resources.files("tests.connectors._fixtures.youtube_analytics").joinpath(name)
      with ref.open("r", encoding="utf-8") as fh:
          return json.load(fh)


  def test_parse_emits_one_row_per_metric_per_data_row() -> None:
      # Three rows, two monetary metrics each => 6 ParsedSourceRow.
      payload = _load_fixture("sample_query_response_2026_04.json")
      rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
      assert len(rows) == 6


  def test_parse_preserves_amounts_and_uses_query_currency() -> None:
      payload = _load_fixture("sample_query_response_2026_04.json")
      rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
      # All rows carry USD because the query currency was USD.
      assert all(r.currency_code == "USD" for r in rows)
      # estimatedRevenue values appear with full precision preserved.
      est_amounts = {r.amount_native for r in rows if r.metric_key == "estimatedRevenue"}
      assert Decimal("1234.567890") in est_amounts


  def test_parse_sets_value_kind_estimated_for_estimated_metric() -> None:
      payload = _load_fixture("sample_query_response_2026_04.json")
      rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
      for row in rows:
          if row.metric_key == "estimatedRevenue":
              assert row.value_kind == "estimated"
          if row.metric_key == "grossRevenue":
              assert row.value_kind == "estimated"  # Analytics is always estimated


  def test_period_uses_query_start_end() -> None:
      payload = _load_fixture("sample_query_response_2026_04.json")
      rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
      for row in rows:
          assert row.period_start == date(2026, 4, 1)
          assert row.period_end == date(2026, 4, 30)
          assert row.report_month == "2026-04"


  def test_source_row_key_stable_across_reruns() -> None:
      a = list(YouTubeAnalyticsParser().parse(
          _load_fixture("sample_query_response_2026_04.json"), tenant_id=TENANT_ID,
      ))
      b = list(YouTubeAnalyticsParser().parse(
          _load_fixture("sample_query_response_2026_04_rerun.json"), tenant_id=TENANT_ID,
      ))
      assert sorted(r.source_row_key for r in a) == sorted(r.source_row_key for r in b)


  def test_parser_uses_youtube_analytics_source_system() -> None:
      payload = _load_fixture("sample_query_response_2026_04.json")
      rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
      assert all(r.source_system == "youtube_analytics" for r in rows)
  ```

- [ ] **Step 2: Confirm RED**

  Run: `python -m pytest tests/connectors/google_source_parsers/test_youtube_analytics_parser.py -v`
  Expected: ImportError on `YouTubeAnalyticsParser`.

### Task 4.9: Implement `YouTubeAnalyticsParser`

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py`

- [ ] **Step 1: Write the parser**

  ```python
  # backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py
  """Parser for YouTube Analytics reports.query response payloads.

  Consumes a `youtubeAnalytics#resultTable` shape: columnHeaders + rows.
  Emits one ParsedSourceRow per monetary metric per data row. value_kind
  is always 'estimated' because Analytics never returns settled values.
  """

  from collections.abc import Iterable
  from datetime import date
  from decimal import Decimal
  from typing import Final
  from uuid import UUID

  from ums_smart_revenue.connectors.google_source_parsers.base import ParserError
  from ums_smart_revenue.connectors.google_source_parsers.source_row_keys import (
      build_source_row_key,
  )
  from ums_smart_revenue.connectors.google_source_rows import ParsedSourceRow

  _MONETARY_METRICS: Final[frozenset[str]] = frozenset(
      {"estimatedRevenue", "grossRevenue", "estimatedAdRevenue",
       "estimatedRedPartnerRevenue", "adRevenue"}
  )


  # ============================================================================
  # Purpose: Translate YouTube Analytics reports.query payloads into
  #          ParsedSourceRow rows, one per monetary metric per data row.
  # Database/ORM: None. ParsedSourceRow boundary.
  # Standards: Source amount + query currency preserved exactly.
  #            value_kind='estimated' (Analytics never returns settled).
  # Blast Radius: No DB write. No graph projection impact detected.
  # Connections:
  #   - File: tests/connectors/_fixtures/youtube_analytics/ -> Fixtures.
  # ============================================================================
  class YouTubeAnalyticsParser:
      source_system = "youtube_analytics"

      def parse(
          self,
          payload: dict[str, object],
          *,
          tenant_id: UUID,
      ) -> Iterable[ParsedSourceRow]:
          request = self._require_dict(payload, "query_request")
          column_headers = payload.get("columnHeaders")
          rows = payload.get("rows")
          if not isinstance(column_headers, list):
              raise ParserError("columnHeaders must be a list")
          if not isinstance(rows, list):
              raise ParserError("rows must be a list")

          period_start = date.fromisoformat(self._require_str(request, "startDate"))
          period_end = date.fromisoformat(self._require_str(request, "endDate"))
          currency = self._require_str(request, "currency")
          metrics_csv = self._require_str(request, "metrics")
          dimensions_csv = self._require_str(request, "dimensions")
          query_signature = f"{metrics_csv}|{dimensions_csv}"
          ids = self._require_str(request, "ids")

          dimension_names = [
              h["name"] for h in column_headers
              if isinstance(h, dict) and h.get("columnType") == "DIMENSION"
          ]
          metric_names = [
              h["name"] for h in column_headers
              if isinstance(h, dict) and h.get("columnType") == "METRIC"
          ]

          for data_row in rows:
              if not isinstance(data_row, list):
                  raise ParserError("each rows[*] must be a list (tabular)")
              if len(data_row) != len(column_headers):
                  raise ParserError(
                      f"row length {len(data_row)} != columnHeaders length {len(column_headers)}"
                  )

              dim_values = dict(zip(dimension_names, data_row[:len(dimension_names)], strict=True))
              metric_values = dict(zip(metric_names, data_row[len(dimension_names):], strict=True))

              channel = dim_values.get("channel")
              if not isinstance(channel, str):
                  raise ParserError("dimensions.channel must be a string")

              for metric_name in metric_names:
                  if metric_name not in _MONETARY_METRICS:
                      continue  # B1 only tracks monetary metrics.
                  raw_value = metric_values[metric_name]
                  if not isinstance(raw_value, str):
                      raise ParserError(
                          f"metric {metric_name} value must be a string for Decimal precision"
                      )

                  source_row_key = build_source_row_key(
                      source_system=self.source_system,
                      query_signature=f"{query_signature}|{metric_name}",
                      period_start=period_start.isoformat(),
                      period_end=period_end.isoformat(),
                      dimensions=dim_values,
                  )

                  yield ParsedSourceRow(
                      source_system=self.source_system,
                      source_row_key=source_row_key,
                      source_account_id=ids,
                      content_owner_id=ids if ids.startswith("contentOwner==") else None,
                      youtube_channel_id=channel,
                      report_type="reports.query",
                      report_month=f"{period_start.year:04d}-{period_start.month:02d}",
                      period_start=period_start,
                      period_end=period_end,
                      metric_key=metric_name,
                      value_kind="estimated",
                      amount_native=Decimal(raw_value),
                      currency_code=currency,
                      source_report_id=None,
                      raw_payload={"dimensions": dim_values, "metric": metric_name, "value": raw_value},
                  )

      @staticmethod
      def _require_dict(d: dict[str, object], key: str) -> dict[str, object]:
          value = d.get(key)
          if not isinstance(value, dict):
              raise ParserError(f"missing or non-dict field: {key!r}")
          return value

      @staticmethod
      def _require_str(d: dict[str, object], key: str) -> str:
          value = d.get(key)
          if not isinstance(value, str):
              raise ParserError(f"missing or non-str field: {key!r}")
          return value
  ```

- [ ] **Step 2: Uncomment `YouTubeAnalyticsParser` in `__init__.py` and run tests**

  Run: `python -m pytest tests/connectors/google_source_parsers/test_youtube_analytics_parser.py -v`
  Expected: 6 passed.

- [ ] **Step 3: Commit**

  ```powershell
  git add backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py backend/ums_smart_revenue/connectors/google_source_parsers/__init__.py tests/connectors/google_source_parsers/test_youtube_analytics_parser.py
  git commit -m "feat(parsers): YouTubeAnalyticsParser — monetary metrics × data rows fan-out"
  ```

### Task 4.10: Create synthetic AdSense fixtures (earnings + payment)

**Files:**
- Create: `tests/connectors/_fixtures/adsense_management/__init__.py`
- Create: `tests/connectors/_fixtures/adsense_management/sample_earnings_report_2026_04.json`
- Create: `tests/connectors/_fixtures/adsense_management/sample_earnings_report_2026_04_rerun.json`
- Create: `tests/connectors/_fixtures/adsense_management/sample_payment_report_2026_04.json`
- Create: `tests/connectors/_fixtures/adsense_management/sample_payment_report_2026_04_rerun.json`

- [ ] **Step 1: Package marker**

  ```python
  # tests/connectors/_fixtures/adsense_management/__init__.py
  ```

- [ ] **Step 2: Earnings report fixture (estimated, multi-row)**

  ```json
  {
    "request": {
      "accountId": "accounts/pub-test-001",
      "dateRange": {"startDate": {"year": 2026, "month": 4, "day": 1}, "endDate": {"year": 2026, "month": 4, "day": 30}},
      "metrics": ["ESTIMATED_EARNINGS"],
      "dimensions": ["PRODUCT_CODE", "COUNTRY_CODE"],
      "currencyCode": "USD",
      "reportingTimeZone": "ACCOUNT_TIME_ZONE"
    },
    "report_id": "adsense-earnings-2026-04-001",
    "headers": [
      {"name": "PRODUCT_CODE", "type": "DIMENSION"},
      {"name": "COUNTRY_CODE", "type": "DIMENSION"},
      {"name": "ESTIMATED_EARNINGS", "type": "METRIC_CURRENCY", "currencyCode": "USD"}
    ],
    "rows": [
      {"cells": [{"value": "AFC"}, {"value": "US"}, {"value": "789.120000"}]},
      {"cells": [{"value": "AFC"}, {"value": "EG"}, {"value": "12.340000"}]},
      {"cells": [{"value": "AFS"}, {"value": "GB"}, {"value": "45.670000"}]}
    ]
  }
  ```

- [ ] **Step 3: Payment report fixture (settled)**

  ```json
  {
    "request": {
      "accountId": "accounts/pub-test-001",
      "dateRange": {"startDate": {"year": 2026, "month": 4, "day": 1}, "endDate": {"year": 2026, "month": 4, "day": 30}},
      "metrics": ["PAID_AMOUNT"],
      "currencyCode": "USD"
    },
    "report_id": "adsense-payment-2026-04-001",
    "headers": [
      {"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"}
    ],
    "rows": [
      {"cells": [{"value": "847.130000"}]}
    ]
  }
  ```

- [ ] **Step 4: Copy reruns**

  ```powershell
  Copy-Item tests/connectors/_fixtures/adsense_management/sample_earnings_report_2026_04.json tests/connectors/_fixtures/adsense_management/sample_earnings_report_2026_04_rerun.json
  Copy-Item tests/connectors/_fixtures/adsense_management/sample_payment_report_2026_04.json tests/connectors/_fixtures/adsense_management/sample_payment_report_2026_04_rerun.json
  ```

- [ ] **Step 5: Commit**

  ```powershell
  git add tests/connectors/_fixtures/adsense_management/
  git commit -m "test(fixtures): synthetic AdSense earnings + payment fixtures (+ rerun pairs)"
  ```

### Task 4.11: Test `AdSenseManagementParser` (RED)

**Files:**
- Test: `tests/connectors/google_source_parsers/test_adsense_management_parser.py`

- [ ] **Step 1: Write the failing tests**

  ```python
  # tests/connectors/google_source_parsers/test_adsense_management_parser.py
  import json
  from datetime import date
  from decimal import Decimal
  from importlib import resources
  from uuid import uuid4

  from ums_smart_revenue.connectors.google_source_parsers import AdSenseManagementParser

  TENANT_ID = uuid4()


  def _load(name: str) -> dict[str, object]:
      ref = resources.files("tests.connectors._fixtures.adsense_management").joinpath(name)
      with ref.open("r", encoding="utf-8") as fh:
          return json.load(fh)


  def test_earnings_report_emits_estimated_rows() -> None:
      payload = _load("sample_earnings_report_2026_04.json")
      rows = list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))
      assert len(rows) == 3
      for row in rows:
          assert row.source_system == "adsense_management"
          assert row.value_kind == "estimated"
          assert row.report_type == "earnings_report"
          assert row.report_month == "2026-04"
          assert row.period_start == date(2026, 4, 1)
          assert row.period_end == date(2026, 4, 30)


  def test_payment_report_emits_settled_row() -> None:
      payload = _load("sample_payment_report_2026_04.json")
      rows = list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))
      assert len(rows) == 1
      row = rows[0]
      assert row.value_kind == "settled"
      assert row.report_type == "payment_report"
      assert row.amount_native == Decimal("847.130000")
      assert row.currency_code == "USD"


  def test_account_id_is_normalized_from_request() -> None:
      payload = _load("sample_earnings_report_2026_04.json")
      rows = list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))
      for row in rows:
          assert row.source_account_id == "pub-test-001"


  def test_source_row_key_stable_across_reruns_for_earnings() -> None:
      a = list(AdSenseManagementParser().parse(_load("sample_earnings_report_2026_04.json"), tenant_id=TENANT_ID))
      b = list(AdSenseManagementParser().parse(_load("sample_earnings_report_2026_04_rerun.json"), tenant_id=TENANT_ID))
      assert sorted(r.source_row_key for r in a) == sorted(r.source_row_key for r in b)


  def test_source_row_key_stable_across_reruns_for_payments() -> None:
      a = list(AdSenseManagementParser().parse(_load("sample_payment_report_2026_04.json"), tenant_id=TENANT_ID))
      b = list(AdSenseManagementParser().parse(_load("sample_payment_report_2026_04_rerun.json"), tenant_id=TENANT_ID))
      assert sorted(r.source_row_key for r in a) == sorted(r.source_row_key for r in b)


  def test_earnings_and_payment_keys_differ() -> None:
      e = list(AdSenseManagementParser().parse(_load("sample_earnings_report_2026_04.json"), tenant_id=TENANT_ID))
      p = list(AdSenseManagementParser().parse(_load("sample_payment_report_2026_04.json"), tenant_id=TENANT_ID))
      assert not (set(r.source_row_key for r in e) & set(r.source_row_key for r in p))
  ```

- [ ] **Step 2: Confirm RED**

  Run: `python -m pytest tests/connectors/google_source_parsers/test_adsense_management_parser.py -v`
  Expected: ImportError on `AdSenseManagementParser`.

### Task 4.12: Implement `AdSenseManagementParser`

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google_source_parsers/adsense_management.py`

- [ ] **Step 1: Write the parser**

  ```python
  # backend/ums_smart_revenue/connectors/google_source_parsers/adsense_management.py
  """Parser for AdSense Management API report payloads.

  Two report shapes share the parser: estimated earnings (value_kind =
  'estimated', report_type = 'earnings_report') and payment/settled
  (value_kind = 'settled', report_type = 'payment_report'). The shape is
  distinguished by the metric column type: PAID_AMOUNT => settled,
  everything else => estimated.
  """

  from collections.abc import Iterable
  from datetime import date
  from decimal import Decimal
  from typing import Final
  from uuid import UUID

  from ums_smart_revenue.connectors.google_source_parsers.base import ParserError
  from ums_smart_revenue.connectors.google_source_parsers.source_row_keys import (
      build_source_row_key,
  )
  from ums_smart_revenue.connectors.google_source_rows import ParsedSourceRow

  _SETTLED_METRICS: Final[frozenset[str]] = frozenset({"PAID_AMOUNT", "UNPAID_AMOUNT"})


  # ============================================================================
  # Purpose: Translate AdSense earnings + payment report payloads into
  #          ParsedSourceRow rows.
  # Database/ORM: None. ParsedSourceRow boundary.
  # Standards: Source amount + currency preserved exactly. value_kind
  #            distinguishes estimated earnings vs settled payments.
  # Blast Radius: No DB write. No graph projection impact detected.
  # Connections:
  #   - File: tests/connectors/_fixtures/adsense_management/ -> Fixtures.
  # ============================================================================
  class AdSenseManagementParser:
      source_system = "adsense_management"

      def parse(
          self,
          payload: dict[str, object],
          *,
          tenant_id: UUID,
      ) -> Iterable[ParsedSourceRow]:
          request = self._require_dict(payload, "request")
          headers = payload.get("headers")
          rows = payload.get("rows")
          report_id = self._require_str(payload, "report_id")
          if not isinstance(headers, list):
              raise ParserError("headers must be a list")
          if not isinstance(rows, list):
              raise ParserError("rows must be a list")

          account_raw = self._require_str(request, "accountId")
          account_id = account_raw.removeprefix("accounts/")
          period_start = self._parse_iso_date(self._require_dict(self._require_dict(request, "dateRange"), "startDate"))
          period_end = self._parse_iso_date(self._require_dict(self._require_dict(request, "dateRange"), "endDate"))
          currency = self._require_str(request, "currencyCode")

          dim_names = [
              h["name"] for h in headers
              if isinstance(h, dict) and h.get("type") == "DIMENSION"
          ]
          metric_names = [
              h["name"] for h in headers
              if isinstance(h, dict) and h.get("type") == "METRIC_CURRENCY"
          ]
          report_type = "payment_report" if any(m in _SETTLED_METRICS for m in metric_names) else "earnings_report"
          default_value_kind = "settled" if report_type == "payment_report" else "estimated"

          for raw_row in rows:
              if not isinstance(raw_row, dict):
                  raise ParserError("each rows[*] must be a dict with 'cells'")
              cells = raw_row.get("cells")
              if not isinstance(cells, list) or len(cells) != len(headers):
                  raise ParserError("row.cells length must match headers")
              values = [cell.get("value") if isinstance(cell, dict) else None for cell in cells]
              dim_values = dict(zip(dim_names, values[:len(dim_names)], strict=True))
              metric_values = dict(zip(metric_names, values[len(dim_names):], strict=True))

              for metric_name, raw_value in metric_values.items():
                  if not isinstance(raw_value, str):
                      raise ParserError(f"metric {metric_name} value must be a string")
                  source_row_key = build_source_row_key(
                      source_system=self.source_system,
                      source_report_id=f"{report_id}|{metric_name}",
                      account_id=account_id,
                      period_start=period_start.isoformat(),
                      period_end=period_end.isoformat(),
                      dimensions=dim_values,
                  )
                  yield ParsedSourceRow(
                      source_system=self.source_system,
                      source_row_key=source_row_key,
                      source_account_id=account_id,
                      content_owner_id=None,
                      youtube_channel_id=None,  # AdSense reports are account-scoped, not channel-scoped.
                      report_type=report_type,
                      report_month=f"{period_start.year:04d}-{period_start.month:02d}",
                      period_start=period_start,
                      period_end=period_end,
                      metric_key=metric_name,
                      value_kind=default_value_kind,
                      amount_native=Decimal(raw_value),
                      currency_code=currency,
                      source_report_id=report_id,
                      raw_payload={"dimensions": dim_values, "metric": metric_name, "value": raw_value},
                  )

      @staticmethod
      def _require_dict(d: dict[str, object], key: str) -> dict[str, object]:
          value = d.get(key)
          if not isinstance(value, dict):
              raise ParserError(f"missing or non-dict field: {key!r}")
          return value

      @staticmethod
      def _require_str(d: dict[str, object], key: str) -> str:
          value = d.get(key)
          if not isinstance(value, str):
              raise ParserError(f"missing or non-str field: {key!r}")
          return value

      @staticmethod
      def _parse_iso_date(d: dict[str, object]) -> date:
          year = d.get("year")
          month = d.get("month")
          day = d.get("day")
          if not all(isinstance(v, int) for v in (year, month, day)):
              raise ParserError("dateRange.{startDate,endDate} require int year/month/day")
          return date(year, month, day)  # type: ignore[arg-type]
  ```

- [ ] **Step 2: Uncomment `AdSenseManagementParser` in `__init__.py` and run tests**

  Run: `python -m pytest tests/connectors/google_source_parsers/test_adsense_management_parser.py -v`
  Expected: 6 passed.

- [ ] **Step 3: Run all parser tests together**

  Run: `python -m pytest tests/connectors/google_source_parsers/ -v`
  Expected: all green.

- [ ] **Step 4: Commit**

  ```powershell
  git add backend/ums_smart_revenue/connectors/google_source_parsers/adsense_management.py backend/ums_smart_revenue/connectors/google_source_parsers/__init__.py tests/connectors/google_source_parsers/test_adsense_management_parser.py
  git commit -m "feat(parsers): AdSenseManagementParser — earnings + payment shape with value_kind dispatch"
  ```

---

## Phase 5 — End-to-end ingestion flow test

**Reminder of the non-goal:** This phase tests parser → repository wiring against the synthetic fixtures only. No raw-file storage backend is exercised (raw_file_id is a UUID we generate in the test, not a real file). No connector job runner is in scope — that belongs to a later live-connector spec.

### Task 5.1: End-to-end test — parse fixture → upsert → assert idempotency

**Files:**
- Test: `tests/connectors/test_google_source_ingestion_flow.py`

- [ ] **Step 1: Write the integration test**

  ```python
  # tests/connectors/test_google_source_ingestion_flow.py
  """End-to-end flow: parser → repository upsert → idempotency.

  Covers all three parsers + the repository surface in a single
  fixture-driven flow. Uses SQLite metadata-create-all; the full Postgres
  round-trip is Phase 8.
  """

  import json
  from datetime import datetime
  from importlib import resources
  from uuid import uuid4

  import pytest
  from sqlalchemy import create_engine, select
  from sqlalchemy.orm import Session

  from ums_smart_revenue.connectors.google_source_parsers import (
      AdSenseManagementParser,
      YouTubeAnalyticsParser,
      YouTubeReportingParser,
  )
  from ums_smart_revenue.connectors.google_source_rows import (
      SqlAlchemyGoogleRevenueSourceRowRepository,
  )
  from ums_smart_revenue.db.finance_models import FinanceBase
  from ums_smart_revenue.db.source_models import (
      CurrencyORM,
      GoogleRevenueSourceRowORM,
  )
  from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM

  TENANT_ID = uuid4()
  RAW_FILE_ID = uuid4()


  @pytest.fixture
  def session() -> Session:
      engine = create_engine("sqlite:///:memory:")
      FinanceBase.metadata.create_all(engine)
      TenantBase.metadata.create_all(engine)
      with Session(engine) as s:
          now = datetime.now()
          s.add_all([
              TenantORM(id=TENANT_ID, slug="tenant-x", display_name="Tenant X"),
              CurrencyORM(code="USD", numeric_code="840", name="US Dollar", minor_unit=2, is_supported=True, activated_at=now),
              CurrencyORM(code="GBP", numeric_code="826", name="Pound Sterling", minor_unit=2, is_supported=True, activated_at=now),
              CurrencyORM(code="EGP", numeric_code="818", name="Egyptian Pound", minor_unit=2, is_supported=True, activated_at=now),
          ])
          s.flush()
          yield s


  def _load(package: str, name: str) -> dict:
      ref = resources.files(package).joinpath(name)
      with ref.open("r", encoding="utf-8") as fh:
          return json.load(fh)


  def test_end_to_end_three_parsers_upsert_into_repository(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)

      yt_rep = list(YouTubeReportingParser().parse(
          _load("tests.connectors._fixtures.youtube_reporting", "sample_estimated_revenue_2026_04.json"),
          tenant_id=TENANT_ID,
      ))
      yt_ana = list(YouTubeAnalyticsParser().parse(
          _load("tests.connectors._fixtures.youtube_analytics", "sample_query_response_2026_04.json"),
          tenant_id=TENANT_ID,
      ))
      ads_earn = list(AdSenseManagementParser().parse(
          _load("tests.connectors._fixtures.adsense_management", "sample_earnings_report_2026_04.json"),
          tenant_id=TENANT_ID,
      ))
      ads_pay = list(AdSenseManagementParser().parse(
          _load("tests.connectors._fixtures.adsense_management", "sample_payment_report_2026_04.json"),
          tenant_id=TENANT_ID,
      ))

      repo.upsert_many(TENANT_ID, yt_rep, raw_file_id=RAW_FILE_ID, imported_by=None)
      repo.upsert_many(TENANT_ID, yt_ana, raw_file_id=RAW_FILE_ID, imported_by=None)
      repo.upsert_many(TENANT_ID, ads_earn, raw_file_id=RAW_FILE_ID, imported_by=None)
      repo.upsert_many(TENANT_ID, ads_pay, raw_file_id=RAW_FILE_ID, imported_by=None)

      written = session.scalars(
          select(GoogleRevenueSourceRowORM).where(
              GoogleRevenueSourceRowORM.tenant_id == TENANT_ID
          )
      ).all()
      expected = len(yt_rep) + len(yt_ana) + len(ads_earn) + len(ads_pay)
      assert len(written) == expected


  def test_rerun_with_identical_fixtures_produces_zero_new_rows(session: Session) -> None:
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)

      def parse_all(suffix: str) -> list:
          out = []
          out.extend(YouTubeReportingParser().parse(
              _load("tests.connectors._fixtures.youtube_reporting", f"sample_estimated_revenue_2026_04{suffix}.json"),
              tenant_id=TENANT_ID,
          ))
          out.extend(YouTubeAnalyticsParser().parse(
              _load("tests.connectors._fixtures.youtube_analytics", f"sample_query_response_2026_04{suffix}.json"),
              tenant_id=TENANT_ID,
          ))
          out.extend(AdSenseManagementParser().parse(
              _load("tests.connectors._fixtures.adsense_management", f"sample_earnings_report_2026_04{suffix}.json"),
              tenant_id=TENANT_ID,
          ))
          out.extend(AdSenseManagementParser().parse(
              _load("tests.connectors._fixtures.adsense_management", f"sample_payment_report_2026_04{suffix}.json"),
              tenant_id=TENANT_ID,
          ))
          return out

      first = parse_all("")
      repo.upsert_many(TENANT_ID, first, raw_file_id=RAW_FILE_ID, imported_by=None)
      first_count = session.query(GoogleRevenueSourceRowORM).filter_by(tenant_id=TENANT_ID).count()

      second = parse_all("_rerun")
      repo.upsert_many(TENANT_ID, second, raw_file_id=RAW_FILE_ID, imported_by=None)
      second_count = session.query(GoogleRevenueSourceRowORM).filter_by(tenant_id=TENANT_ID).count()

      assert second_count == first_count
  ```

- [ ] **Step 2: Run**

  Run: `python -m pytest tests/connectors/test_google_source_ingestion_flow.py -v`
  Expected: 2 passed.

- [ ] **Step 3: Commit**

  ```powershell
  git add tests/connectors/test_google_source_ingestion_flow.py
  git commit -m "test(connectors): end-to-end parser → repository ingestion flow + rerun idempotency"
  ```

### Task 5.2: Failure-mode test for parser orchestration

**Files:**
- Test: `tests/connectors/test_google_source_ingestion_flow.py` (extend)

- [ ] **Step 1: Add malformed-payload test**

  Append:

  ```python
  def test_malformed_payload_raises_parser_error_without_partial_writes(session: Session) -> None:
      from ums_smart_revenue.connectors.google_source_parsers import ParserError
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      bad_payload = {"report_metadata": {"report_id": "x", "report_type": "y"}, "rows": "not a list"}
      with pytest.raises(ParserError):
          list(YouTubeReportingParser().parse(bad_payload, tenant_id=TENANT_ID))
      # No rows were yielded, so no upsert call — partial writes are impossible
      # because the parser fails before producing any ParsedSourceRow.
      assert session.query(GoogleRevenueSourceRowORM).filter_by(tenant_id=TENANT_ID).count() == 0
  ```

- [ ] **Step 2: Run + commit**

  ```powershell
  python -m pytest tests/connectors/test_google_source_ingestion_flow.py::test_malformed_payload_raises_parser_error_without_partial_writes -v
  git add tests/connectors/test_google_source_ingestion_flow.py
  git commit -m "test(connectors): malformed payload raises ParserError; no partial writes possible"
  ```

---

## Phase 6 — Finance integration guardrails

**Phrasing discipline (operator-locked):** Non-USD source rows are surfaced at the **repository/service boundary** in B1. No existing finance API endpoint changes its response shape. If a future PR expands an endpoint to expose source coverage, that expansion is out of B1 scope.

### Task 6.1: Guard test — finance services do not consume `CurrencyExchangeRateORM` for monetary results

**Files:**
- Test: `tests/finance/test_finance_no_fx_dependency.py`

- [ ] **Step 1: Write the guard test**

  ```python
  # tests/finance/test_finance_no_fx_dependency.py
  """Guard tests proving B1's finance modules do not depend on
  CurrencyExchangeRateORM, market FX rates, or any provider FX feed for
  official revenue, payment, tax, deduction, or reconciliation values.

  Spec §6: currency_exchange_rates is legacy scaffolding. Any new
  finance/* module added in B1 must not import or query it.
  """

  import ast
  from pathlib import Path

  FINANCE_DIR = Path(__file__).resolve().parents[2] / "backend" / "ums_smart_revenue" / "finance"

  # exchange_rates.py is the legacy module itself; it's allowed to reference
  # CurrencyExchangeRateORM. Everything else must not.
  ALLOWED_LEGACY_FILES = {"exchange_rates.py"}


  def _module_imports_currency_exchange_rate_orm(path: Path) -> bool:
      source = path.read_text(encoding="utf-8")
      tree = ast.parse(source, filename=str(path))
      for node in ast.walk(tree):
          if isinstance(node, ast.ImportFrom):
              for alias in node.names:
                  if alias.name == "CurrencyExchangeRateORM":
                      return True
          if isinstance(node, ast.Attribute) and node.attr == "CurrencyExchangeRateORM":
              return True
      return False


  def test_no_finance_module_outside_legacy_imports_currency_exchange_rate_orm() -> None:
      offenders = []
      for path in FINANCE_DIR.glob("**/*.py"):
          if path.name in ALLOWED_LEGACY_FILES:
              continue
          if _module_imports_currency_exchange_rate_orm(path):
              offenders.append(str(path.relative_to(FINANCE_DIR)))
      assert not offenders, (
          "B1 forbids new finance modules from depending on CurrencyExchangeRateORM "
          f"for official money. Offenders: {offenders}"
      )
  ```

- [ ] **Step 2: Run + commit**

  ```powershell
  python -m pytest tests/finance/test_finance_no_fx_dependency.py -v
  git add tests/finance/test_finance_no_fx_dependency.py
  git commit -m "test(finance): guard — finance modules outside legacy do not consume CurrencyExchangeRateORM"
  ```

### Task 6.2: Repository-boundary test — non-USD source rows are visible at the repository layer (no API expansion in B1)

**Files:**
- Test: `tests/connectors/google_source_rows/test_repository.py` (extend)

- [ ] **Step 1: Add the non-USD coverage test**

  Append (uses the fixtures from Task 3.4):

  ```python
  def test_non_usd_source_rows_visible_at_repository_layer(session: Session) -> None:
      """B1 makes non-USD source rows queryable at the repository layer.

      Any future surfacing through an API endpoint is out of scope.
      This test pins the visibility contract: a caller that asks for all
      source rows for a tenant/month MUST see non-USD rows alongside USD
      rows — no silent filter, no automatic conversion.
      """
      repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
      from dataclasses import replace
      base = _row(source_row_key="s" * 64)
      repo.upsert_many(TENANT_A, [
          base,
          replace(base, source_row_key="t" * 64, currency_code="EGP"),
      ], raw_file_id=RAW_FILE_ID, imported_by=None)
      rows = repo.list(TENANT_A, report_month="2026-04")
      currencies = {r.currency_code for r in rows}
      assert currencies == {"USD", "EGP"}
  ```

- [ ] **Step 2: Run + commit**

  ```powershell
  python -m pytest tests/connectors/google_source_rows/test_repository.py::test_non_usd_source_rows_visible_at_repository_layer -v
  git add tests/connectors/google_source_rows/test_repository.py
  git commit -m "test(connectors): repository surfaces non-USD source rows (no API expansion in B1)"
  ```

### Task 6.3: Verify existing USD finance endpoints unchanged

**Files:**
- No new files; verification step.

- [ ] **Step 1: Run all existing finance API tests**

  ```powershell
  python -m pytest tests/api/ tests/finance/ -q
  ```
  Expected: every test that existed before this PR still passes. If any test fails, the change has violated the "no behavior change to existing endpoints" rule — investigate and either revert the offending change or move the failing test to a follow-up spec.

- [ ] **Step 2: Compare pytest count**

  Compare against the baseline captured in Task 0.2 Step 4. Net delta should equal exactly the count of NEW tests added in Phases 1-6 (no existing tests should disappear).

- [ ] **Step 3: (No commit; this is a verification gate.)**

---

## Phase 7 — Authz/audit guardrails

**Scope reminder (operator-locked):** "Connector failures record typed job failure state" stays scoped to the parser/ingestion-skeleton (`ParserError`), NOT a live connector job runner. B1 does not introduce a job runner.

### Task 7.1: Guard test — `Permission.MANAGE_FX_RATES` is NOT added

**Files:**
- Test: `tests/auth/test_no_fx_permission.py`

- [ ] **Step 1: Write the guard**

  ```python
  # tests/auth/test_no_fx_permission.py
  """Guard test proving B1 did not add Permission.MANAGE_FX_RATES.

  Spec §3 explicitly excludes this permission. If a future spec
  introduces FX rate management, this test will fail and the FX-spec
  author must remove it as part of that introduction.
  """

  from ums_smart_revenue.auth.permissions import Permission


  def test_manage_fx_rates_permission_does_not_exist() -> None:
      forbidden_names = {p.name for p in Permission}
      assert "MANAGE_FX_RATES" not in forbidden_names, (
          "B1 spec §3 prohibits Permission.MANAGE_FX_RATES. If a later spec "
          "introduces this permission, remove this guard as part of that spec."
      )


  def test_no_finance_manage_fx_rates_value_in_permissions() -> None:
      values = {p.value for p in Permission}
      assert "finance.manage_fx_rates" not in values
  ```

- [ ] **Step 2: Run + commit**

  ```powershell
  python -m pytest tests/auth/test_no_fx_permission.py -v
  git add tests/auth/test_no_fx_permission.py
  git commit -m "test(auth): guard — Permission.MANAGE_FX_RATES not added by B1"
  ```

### Task 7.2: Parser-skeleton failure-state test (typed errors, no connector job runner)

**Files:**
- Test: `tests/connectors/google_source_parsers/test_parser_failure_states.py`

- [ ] **Step 1: Write the failure-state tests**

  ```python
  # tests/connectors/google_source_parsers/test_parser_failure_states.py
  """Each parser must raise ParserError (typed) on malformed payloads.

  This pins the failure-state contract for the parser/ingestion
  orchestration skeleton. Live connector job runner failure-state
  recording is out of B1 scope.
  """

  import pytest
  from uuid import uuid4

  from ums_smart_revenue.connectors.google_source_parsers import (
      AdSenseManagementParser,
      ParserError,
      YouTubeAnalyticsParser,
      YouTubeReportingParser,
  )

  TENANT_ID = uuid4()


  def test_youtube_reporting_rejects_missing_metadata() -> None:
      with pytest.raises(ParserError):
          list(YouTubeReportingParser().parse({}, tenant_id=TENANT_ID))


  def test_youtube_reporting_rejects_non_string_amount() -> None:
      payload = {
          "report_metadata": {"report_id": "r", "report_type": "t"},
          "rows": [{
              "line_index": 0,
              "date_range": {"start": "2026-04-01", "end": "2026-04-30"},
              "dimensions": {"channel": "UC_x"},
              "metrics": {"estimatedRevenue": 123.45, "currencyCode": "USD"},  # float, not str
          }],
      }
      with pytest.raises(ParserError):
          list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))


  def test_youtube_analytics_rejects_mismatched_row_length() -> None:
      payload = {
          "query_request": {
              "ids": "contentOwner==cms-1",
              "startDate": "2026-04-01",
              "endDate": "2026-04-30",
              "metrics": "estimatedRevenue",
              "dimensions": "channel",
              "currency": "USD",
          },
          "columnHeaders": [
              {"name": "channel", "columnType": "DIMENSION", "dataType": "STRING"},
              {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
          ],
          "rows": [["UC_x", "100.00", "EXTRA_VALUE"]],  # 3 cells, 2 headers
      }
      with pytest.raises(ParserError):
          list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))


  def test_adsense_rejects_missing_date_range() -> None:
      payload = {
          "request": {"accountId": "accounts/pub-1", "currencyCode": "USD"},
          "report_id": "r",
          "headers": [{"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"}],
          "rows": [],
      }
      with pytest.raises(ParserError):
          list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))
  ```

- [ ] **Step 2: Run + commit**

  ```powershell
  python -m pytest tests/connectors/google_source_parsers/test_parser_failure_states.py -v
  git add tests/connectors/google_source_parsers/test_parser_failure_states.py
  git commit -m "test(parsers): typed ParserError on malformed payloads (parser-skeleton failure contract)"
  ```

### Task 7.3: Confirm `connectors.run_jobs` is the only permission relevant to ingestion

**Files:**
- Test: `tests/auth/test_no_new_ingestion_permission.py`

- [ ] **Step 1: Write the assertion**

  ```python
  # tests/auth/test_no_new_ingestion_permission.py
  """B1 does not introduce a new permission for Google source-ingestion.

  Existing connectors.run_jobs covers ingestion job authorization. This
  test pins the contract: no new connectors.* or ingestion.* permission
  appeared in this PR.
  """

  from ums_smart_revenue.auth.permissions import Permission

  # Snapshot of the permission set as of PR #41 (post-merge baseline).
  EXPECTED_PERMISSION_VALUES = frozenset({
      "analytics.view",
      "analytics.view_confidence",
      "finance.view_revenue",
      "finance.view_finalized_payments",
      "finance.view_bank_reconciliation",
      "finance.manage_bank_reconciliation",
      "finance.create_manual_override",
      "finance.approve_manual_override",
      "finance.lock_month",
      "finance.unlock_month",
      "finance.change_allocation_rule",
      "exports.analytics",
      "exports.revenue",
      "exports.manage_templates",
      "registry.manage_channels",
      "registry.manage_org_mapping",
      "registry.manage_groups",
      "connectors.view_health",
      "connectors.run_jobs",
      "connectors.manage",
      "raw_files.view",
      "audit.view",
      "audit.view_sensitive_payloads",
      "users.manage",
      "roles.assign",
      "platform.manage_settings",
  })


  def test_no_new_permission_added_in_b1() -> None:
      actual = {p.value for p in Permission}
      added = actual - EXPECTED_PERMISSION_VALUES
      removed = EXPECTED_PERMISSION_VALUES - actual
      assert not added, f"B1 added unexpected permissions: {sorted(added)}"
      assert not removed, f"B1 removed permissions (out of scope): {sorted(removed)}"
  ```

- [ ] **Step 2: Run + commit**

  ```powershell
  python -m pytest tests/auth/test_no_new_ingestion_permission.py -v
  git add tests/auth/test_no_new_ingestion_permission.py
  git commit -m "test(auth): permission-set snapshot — B1 does not add or remove permissions"
  ```

---

## Phase 8 — Migration PostgreSQL round-trip

**Fail-fast not skip:** The repo's AST policy gate (PR #38) forbids `pytest.skip` and `pytest.mark.skip`/`xfail`. If `UMS_TEST_DATABASE_URL` is missing, the migration test module MUST raise at import time so pytest collection fails loudly. No silent degradation to SQLite.

### Task 8.1: Add disposable Postgres helper + fail-fast environment guard

**Files:**
- Create: `tests/db/_postgres_helpers.py`

- [ ] **Step 1: Write the helper**

  ```python
  # tests/db/_postgres_helpers.py
  """Helper for tests that require disposable PostgreSQL.

  Tests that import this module MUST be runnable only when
  UMS_TEST_DATABASE_URL is set. The module raises at import time if the
  variable is missing — matching the AST policy gate's no-skip rule.
  """

  import os
  from typing import Final


  def require_postgres_url() -> str:
      url = os.environ.get("UMS_TEST_DATABASE_URL")
      if not url:
          raise RuntimeError(
              "UMS_TEST_DATABASE_URL required for PostgreSQL migration round-trip tests. "
              "Spin up disposable Postgres: "
              "`docker run --rm -d --name ums-mig-pg -p 55432:5432 "
              "-e POSTGRES_PASSWORD=ums postgres:18-alpine`, then "
              "`$env:UMS_TEST_DATABASE_URL = "
              "'postgresql+psycopg://postgres:ums@localhost:55432/postgres'`. "
              "SQLite is not a valid substitute for this test."
          )
      return url


  POSTGRES_URL: Final[str] = require_postgres_url()
  ```

- [ ] **Step 2: Verify import-time behavior**

  ```powershell
  # Without the env var set:
  Remove-Item Env:UMS_TEST_DATABASE_URL -ErrorAction SilentlyContinue
  python -c "import tests.db._postgres_helpers"
  ```
  Expected: `RuntimeError` with the actionable message above.

  ```powershell
  # With the env var set:
  $env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/postgres"
  python -c "import tests.db._postgres_helpers"
  ```
  Expected: silent success.

- [ ] **Step 3: Commit**

  ```powershell
  git add tests/db/_postgres_helpers.py
  git commit -m "test(db): _postgres_helpers — fail-fast UMS_TEST_DATABASE_URL guard (no skip per AST policy)"
  ```

### Task 8.2: PostgreSQL round-trip test for migration `20260523_0001`

**Files:**
- Test: `tests/db/test_google_revenue_source_migration_postgres.py`

- [ ] **Step 1: Write the round-trip test**

  ```python
  # tests/db/test_google_revenue_source_migration_postgres.py
  """PostgreSQL-backed round-trip test for 20260523_0001.

  upgrade head (20260521_0001) -> upgrade 20260523_0001 -> downgrade -1
  -> upgrade head again. Verifies idempotency and that downgrade truly
  reverses the upgrade.
  """

  from pathlib import Path

  import pytest
  from alembic import command
  from alembic.config import Config
  from sqlalchemy import create_engine, inspect, text

  from tests.db._postgres_helpers import POSTGRES_URL

  REPO_ROOT = Path(__file__).resolve().parents[2]
  ALEMBIC_INI = REPO_ROOT / "alembic.ini"


  @pytest.fixture
  def alembic_config() -> Config:
      cfg = Config(str(ALEMBIC_INI))
      cfg.set_main_option("sqlalchemy.url", POSTGRES_URL)
      cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"))
      return cfg


  @pytest.fixture
  def fresh_engine() -> "object":
      engine = create_engine(POSTGRES_URL)
      with engine.begin() as conn:
          conn.execute(text("DROP SCHEMA public CASCADE"))
          conn.execute(text("CREATE SCHEMA public"))
      yield engine
      engine.dispose()


  def test_pre_state_at_prior_head(alembic_config: Config, fresh_engine: object) -> None:
      command.upgrade(alembic_config, "20260521_0001")
      inspector = inspect(fresh_engine)
      tables = set(inspector.get_table_names())
      assert "currency_exchange_rates" in tables, "legacy table must be present from earlier revision"
      assert "currencies" not in tables
      assert "google_revenue_source_rows" not in tables


  def test_upgrade_creates_currencies_and_source_rows(alembic_config: Config, fresh_engine: object) -> None:
      command.upgrade(alembic_config, "20260523_0001")
      inspector = inspect(fresh_engine)
      tables = set(inspector.get_table_names())
      assert "currencies" in tables
      assert "google_revenue_source_rows" in tables
      assert "currency_exchange_rates" in tables  # legacy preserved per spec §6

      # Currencies seeded with v1 supported flip.
      with fresh_engine.begin() as conn:
          count = conn.execute(text("SELECT count(*) FROM currencies")).scalar_one()
          assert count >= 150
          supported = conn.execute(
              text("SELECT code FROM currencies WHERE is_supported = true ORDER BY code")
          ).scalars().all()
          assert supported == ["AED", "EGP", "EUR", "GBP", "SAR", "USD"]


  def test_indexes_present_on_google_revenue_source_rows(alembic_config: Config, fresh_engine: object) -> None:
      command.upgrade(alembic_config, "20260523_0001")
      inspector = inspect(fresh_engine)
      indexes = {i["name"] for i in inspector.get_indexes("google_revenue_source_rows")}
      assert "ix_google_revenue_source_rows_tenant_month_source" in indexes
      assert "ix_google_revenue_source_rows_tenant_channel_month" in indexes


  def test_partial_channel_month_index_has_where_clause(alembic_config: Config, fresh_engine: object) -> None:
      command.upgrade(alembic_config, "20260523_0001")
      with fresh_engine.begin() as conn:
          row = conn.execute(
              text(
                  "SELECT indexdef FROM pg_indexes "
                  "WHERE indexname = 'ix_google_revenue_source_rows_tenant_channel_month'"
              )
          ).scalar_one()
          assert "WHERE" in row.upper()
          assert "youtube_channel_id" in row.lower()


  def test_downgrade_drops_only_b1_tables(alembic_config: Config, fresh_engine: object) -> None:
      command.upgrade(alembic_config, "20260523_0001")
      command.downgrade(alembic_config, "-1")
      inspector = inspect(fresh_engine)
      tables = set(inspector.get_table_names())
      assert "currencies" not in tables
      assert "google_revenue_source_rows" not in tables
      assert "currency_exchange_rates" in tables  # untouched


  def test_round_trip_idempotency(alembic_config: Config, fresh_engine: object) -> None:
      command.upgrade(alembic_config, "20260523_0001")
      command.downgrade(alembic_config, "-1")
      command.upgrade(alembic_config, "20260523_0001")
      # Should not raise; re-upgrade must succeed cleanly.
      inspector = inspect(fresh_engine)
      assert "currencies" in inspector.get_table_names()
  ```

- [ ] **Step 2: Run with PostgreSQL up**

  ```powershell
  docker run --rm -d --name ums-mig-pg -p 55432:5432 -e POSTGRES_PASSWORD=ums postgres:18-alpine
  $env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/postgres"
  python -m pytest tests/db/test_google_revenue_source_migration_postgres.py -v
  docker stop ums-mig-pg
  ```
  Expected: 6 passed.

- [ ] **Step 3: Commit**

  ```powershell
  git add tests/db/test_google_revenue_source_migration_postgres.py
  git commit -m "test(db): PostgreSQL round-trip for migration 20260523_0001 (idempotency + downgrade safety)"
  ```

---

## Phase 9 — PR #43 docs/pulls triple + `Docs/01` + `Docs/15` shipped marks

**Scope reminder:** Phase 9 covers ONLY the implementation-PR documentation. The 9 doc edits + design spec + this plan landed in PR #42 (already merged at the start of Phase 0). Do not re-edit PR #42 docs here.

### Task 9.1: Write `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-report.md`

**Files:**
- Create: `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-report.md`

- [ ] **Step 1: Write the report**

  Template structure (fill in real numbers/SHAs after Phase 10's validation pass — the report can be drafted with placeholders, then finalized in the same commit):

  ```markdown
  # PR #43 — Spec B1 Google Revenue Source Ingestion Foundation — Report

  **Date:** 2026-05-23
  **PR:** https://github.com/XGenerationy/Youtube/pull/43
  **Branch:** `pr/spec-b1-google-revenue-source-ingestion`
  **Base:** `main` at <sha after PR #42 merge>
  **Status:** Implementation against the PR #42 locked spec/plan.

  ## What was requested

  PR #42 landed the pivot: B1 is Google source-reported revenue ingestion
  foundation, not FX storage. PR #43 implements that foundation:
  `currencies` reference table, tenant-scoped `google_revenue_source_rows`
  with idempotent source-row keys, storage repository, synthetic-fixture
  parsers for YouTube Reporting / YouTube Analytics / AdSense Management,
  plus finance + auth guardrails proving no FX dependency was introduced.

  ## What was actually done

  ### Schema + reference data

  - `iso_4217_2026_05.py` immutable snapshot module (~180 ISO codes).
  - `CurrencyORM` + `GoogleRevenueSourceRowORM` on `FinanceBase`.
  - Alembic migration `20260523_0001_google_revenue_source_foundation`
    that creates both tables, seeds the ISO 4217 list, flips the v1
    supported set, and adds the full + partial channel/month indexes.
  - Migration adds nothing else and removes nothing — legacy
    `currency_exchange_rates` and its endpoints are preserved per spec §6.

  ### Repository

  - `SqlAlchemyCurrenciesRepository` (read-only).
  - `SqlAlchemyGoogleRevenueSourceRowRepository` with `upsert_many`,
    `list`, `list_for_channel`, `get_exact`. Dialect-insert helper
    supports both SQLite (unit tests) and PostgreSQL.

  ### Parsers + fixtures

  - `SourceRowParser` protocol + `ParserError`.
  - `build_source_row_key` deterministic SHA-256 hex per source_system.
  - `YouTubeReportingParser`, `YouTubeAnalyticsParser`,
    `AdSenseManagementParser`.
  - Synthetic fixtures under `tests/connectors/_fixtures/` with
    `_rerun.json` pairs for idempotency proof.

  ### Guardrails

  - Finance modules outside `finance/exchange_rates.py` do not import
    `CurrencyExchangeRateORM`.
  - `Permission.MANAGE_FX_RATES` does not exist; permission set
    snapshot pinned.
  - Parsers raise typed `ParserError` on malformed payloads — no
    partial repository writes possible.

  ## Validation

  - `python scripts/run_validation_gate.py` — green at 6 steps.
  - Pytest total: <N> passed (delta <+M> from PR #41 baseline of 819).
  - Vitest: 21 passed (unchanged, no frontend changes).
  - PostgreSQL migration round-trip: 6 passed on disposable Postgres 18-alpine.
  - `git diff --check` (worktree + staged): clean.

  ## Blast radius

  *No graph projection impact detected.* No existing API endpoint
  shape changes. No existing test removed. No `_usd` column touched.
  `currency_exchange_rates`, `CurrencyExchangeRateORM`,
  `finance/exchange_rates.py`, `api/exchange_rates.py`, and the
  `EXCHANGE_RATE_SYNCED` audit event are all preserved per spec §6.

  ## Remaining risks

  - Migration test depends on disposable Postgres. CI must spin one up
    or the test fails fast (no silent skip per AST policy).
  - Fixture payloads mirror Google API shapes from public docs; if
    Google changes the shape upstream, parsers may need follow-up.

  ## Follow-up recommendations

  1. B2: live Google connector (OAuth + API client + download path) on
     top of B1's parsers.
  2. B3: optional display-only currency conversion when the source row
     is non-USD and the requester wants a display value.
  3. Paired `(amount_native, currency_iso4217)` migration on existing
     `_usd` finance tables (separate spec).

  ## Rollback notes

  `git revert <merge>` followed by `alembic downgrade -1` restores the
  exact pre-merge schema. Legacy `currency_exchange_rates` was never
  touched, so no data is lost.
  ```

- [ ] **Step 2: Commit**

  ```powershell
  git add Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-report.md
  git commit -m "docs(pulls): PR #43 report.md (placeholders for numbers/SHAs, finalized in Phase 10)"
  ```

### Task 9.2: Write `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-changelog.md`

**Files:**
- Create: `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-changelog.md`

- [ ] **Step 1: Write the changelog**

  ```markdown
  # PR #43 — Changelog

  ## Added

  - `backend/ums_smart_revenue/db/iso_4217_2026_05.py` — immutable ISO 4217 snapshot.
  - `backend/ums_smart_revenue/db/source_models.py` — `CurrencyORM`, `GoogleRevenueSourceRowORM`.
  - `backend/ums_smart_revenue/db/alembic/versions/20260523_0001_google_revenue_source_foundation.py` — migration.
  - `backend/ums_smart_revenue/connectors/google_source_rows/` — `__init__.py`, `dataclasses.py`, `repository.py`.
  - `backend/ums_smart_revenue/connectors/google_source_parsers/` — `__init__.py`, `base.py`, `source_row_keys.py`, `youtube_reporting.py`, `youtube_analytics.py`, `adsense_management.py`.
  - `tests/connectors/_fixtures/` — synthetic fixtures + provenance README.
  - `tests/connectors/google_source_rows/` — repository tests.
  - `tests/connectors/google_source_parsers/` — parser tests + source_row_keys tests + parser failure-state tests.
  - `tests/connectors/test_google_source_ingestion_flow.py` — end-to-end flow + rerun idempotency.
  - `tests/db/test_source_models.py` — ORM shape tests.
  - `tests/db/test_google_revenue_source_migration.py` — migration metadata assertions.
  - `tests/db/test_google_revenue_source_migration_postgres.py` — Postgres round-trip.
  - `tests/db/_postgres_helpers.py` — fail-fast env guard.
  - `tests/db/test_iso_4217_snapshot.py` — snapshot integrity.
  - `tests/finance/test_finance_no_fx_dependency.py` — finance guard.
  - `tests/auth/test_no_fx_permission.py` — auth guard.
  - `tests/auth/test_no_new_ingestion_permission.py` — permission-set snapshot.

  ## Changed

  - `backend/ums_smart_revenue/db/alembic/env.py` — added `from ums_smart_revenue.db import source_models  # noqa: F401`.

  ## Inline plan-status marks

  - `Docs/01_IMPLEMENTATION_PLAN.md` — added ✅ PR #43 bullet under Source-reported currency foundation section.
  - `Docs/15_DELIVERY_BACKLOG.md` — added ✅ PR #43 bullet under Cross-cutting shipped.

  ## Removed

  Nothing. Legacy `currency_exchange_rates` table, ORM, API, repository, and tests are preserved per spec §6.
  ```

- [ ] **Step 2: Commit**

  ```powershell
  git add Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-changelog.md
  git commit -m "docs(pulls): PR #43 changelog.md"
  ```

### Task 9.3: Write `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-handoff.md`

**Files:**
- Create: `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-handoff.md`

- [ ] **Step 1: Write the handoff**

  ```markdown
  # PR #43 — Handoff

  ## Scope

  Spec B1 implementation. Storage + synthetic-fixture parser foundation
  for Google source-reported revenue. No live OAuth, no live API client,
  no live download. Spec §3 non-goals strictly preserved.

  ## Non-goals (preserved)

  No `fx_rates`. No `fx_locked_month_rates`. No `tenants.fx_provider_settings`.
  No `Permission.MANAGE_FX_RATES`. No live Google API client. No paired
  `_usd` migration. No frontend currency switcher.

  ## Behavior changes at runtime

  None for existing endpoints. New repository + parser modules are not
  yet wired to any API route. They are consumed only by tests in B1; B2
  wires them to a live connector job.

  ## Tests run locally

  - `python scripts/run_validation_gate.py` — green at 6 steps.
  - PostgreSQL round-trip with disposable `postgres:18-alpine` on host
    port 55432.

  ## Failures / skipped gates

  None. Migration test fails fast if `UMS_TEST_DATABASE_URL` is missing
  (no silent skip per AST policy gate).

  ## Risks

  - Fixture payload shapes mirror public Google API documentation; an
    upstream API change might require parser tweaks. B2's live-connector
    work will surface any drift.
  - `connectors/credentials.py` is the only existing connector primitive;
    parsers do not require any credential handling, but the live
    connector in B2 will.

  ## Rollback / operational notes

  `git revert <merge> && alembic downgrade -1`. Legacy
  `currency_exchange_rates` is untouched, so revert is safe.

  ## Next-PR recommendations

  1. **B2: Live Google connector** — OAuth flow, YouTube Reporting jobs
     listing/download, YouTube Analytics query client, AdSense report
     generation polling, raw-file storage backend, scheduled run.
  2. **B3: Display-only currency conversion** — only after operators
     decide which display currency they want for non-USD source rows.
     Convert at the response edge, never in storage.
  3. **Paired-column migration on `_usd` tables** — separate spec,
     migrates `revenue_facts*` and `bank_reconciliation_entries` to
     paired `(amount_native, currency_iso4217)`.
  ```

- [ ] **Step 2: Commit**

  ```powershell
  git add Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-handoff.md
  git commit -m "docs(pulls): PR #43 handoff.md (risks + rollback + next-PR recommendations)"
  ```

### Task 9.4: Add inline `✅ PR #43` marks to `Docs/01` and `Docs/15`

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`
- Modify: `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1: Locate the Source-reported currency foundation bullet in `Docs/01`**

  After the existing line `- **Source-reported currency foundation.** ...` (added by PR #42), append a sub-bullet:

  ```markdown
    - ✅ PR #43: storage + synthetic-fixture parsers for Google revenue source
      ingestion. `currencies` + `google_revenue_source_rows` tables, repository
      with idempotent upsert, parsers for YouTube Reporting / YouTube Analytics
      / AdSense Management. No live OAuth, no live API client. Legacy
      `currency_exchange_rates` preserved as inert scaffolding.
  ```

- [ ] **Step 2: Add a Cross-cutting shipped bullet to `Docs/15`**

  After the existing `- ✅ Frontend tenant-header foundation: ... — PR #41.` line, append:

  ```markdown
  - ✅ Google source-reported revenue ingestion foundation: `currencies`
    reference table, tenant-scoped `google_revenue_source_rows` with idempotent
    source-row keys, storage repository, synthetic-fixture parsers for YouTube
    Reporting / YouTube Analytics / AdSense Management. No live OAuth or API
    client (B2). No FX/conversion behavior (deferred). PostgreSQL-backed
    migration round-trip — PR #43.
  ```

- [ ] **Step 3: Commit**

  ```powershell
  git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
  git commit -m "docs(plan): mark Spec B1 storage + parsers shipped — PR #43"
  ```

---

## Phase 10 — Final validation gate + push prep

### Task 10.1: Run the full 6-step validation gate; finalize report metrics

**Files:**
- Modify: `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-report.md` (fill placeholders)

- [ ] **Step 1: Spin up disposable Postgres for the migration test**

  ```powershell
  docker run --rm -d --name ums-mig-pg -p 55432:5432 -e POSTGRES_PASSWORD=ums postgres:18-alpine
  $env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/postgres"
  ```

- [ ] **Step 2: Run the validation gate**

  ```powershell
  python scripts/run_validation_gate.py
  ```
  Expected: green at all 6 steps (ruff → AST policy → pytest → Vitest → working-tree diff-check → staged diff-check).

- [ ] **Step 3: Record real numbers in report.md**

  Open `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-report.md` and replace:
  - `<sha after PR #42 merge>` with the actual merge SHA of PR #42 from `git log origin/main -1 --format=%h`.
  - `<N>` (pytest total) with the actual count from the gate output.
  - `<+M>` (delta) with `<N> - 819`.

  These are the only fields that depend on runtime numbers; everything else is static.

- [ ] **Step 4: Tear down disposable Postgres**

  ```powershell
  docker stop ums-mig-pg
  ```

- [ ] **Step 5: Commit the finalized report**

  ```powershell
  git add Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-report.md
  git commit -m "docs(pulls): finalize PR #43 report.md with measured pytest + vitest counts"
  ```

### Task 10.2: Pause for push approval — pre-flight checklist

**Files:**
- No file changes; gating step.

- [ ] **Step 1: Re-read the final diff against `main`**

  ```powershell
  git fetch origin main --quiet
  git diff origin/main --stat
  git diff origin/main -- backend/ tests/ Docs/ | Out-Host -Paging
  ```
  Inspect for unintended file changes. Confirm:
  - No `_usd` column was touched.
  - No existing test was deleted.
  - No new API route.
  - No new permission.
  - Legacy `currency_exchange_rates` artefacts are exactly as on `main`.
  - `frontend/package-lock.json` is NOT staged (operator standing exclusion).

- [ ] **Step 2: Confirm validation evidence is current**

  Re-run the gate one more time if more than ~30 minutes have passed since Task 10.1:

  ```powershell
  docker run --rm -d --name ums-mig-pg -p 55432:5432 -e POSTGRES_PASSWORD=ums postgres:18-alpine
  $env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/postgres"
  python scripts/run_validation_gate.py
  docker stop ums-mig-pg
  ```
  Expected: green.

- [ ] **Step 3: PAUSE — surface the diff summary + validation evidence to the operator**

  Report:
  - Branch name.
  - Commit count.
  - Files changed (count + categories).
  - Validation gate result (6/6 green, pytest delta, vitest count).
  - PostgreSQL round-trip result (6/6 passed).

  Then ask the operator to authorize the push. Do NOT run `git push` or `gh pr create` without explicit operator approval — this matches the standing constraint "Pause before every push/merge to remote — surface CR/Codex results before any merge command."

- [ ] **Step 4: After approval — push and open PR**

  ```powershell
  git push -u origin pr/spec-b1-google-revenue-source-ingestion
  gh pr create --title "feat: Spec B1 Google revenue source ingestion foundation" --body "@'
  ## Summary
  - Storage + synthetic-fixture parsers for Google source-reported revenue
  - `currencies` + `google_revenue_source_rows` tables on FinanceBase
  - SqlAlchemyGoogleRevenueSourceRowRepository (idempotent upsert by tenant + source_system + source_row_key)
  - YouTubeReportingParser / YouTubeAnalyticsParser / AdSenseManagementParser
  - Finance + auth guardrails proving no FX dependency introduced
  - PostgreSQL-backed migration round-trip

  ## Test plan
  - [x] `python scripts/run_validation_gate.py` green at 6 steps
  - [x] PostgreSQL migration round-trip 6/6
  - [x] No existing test removed; no existing API endpoint changed
  - [x] `Permission.MANAGE_FX_RATES` does not exist

  Refs: design `Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md`, plan `Docs/superpowers/plans/2026-05-23-spec-b1-google-revenue-source-ingestion.md` (both merged in PR #42).

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  '@"
  ```

- [ ] **Step 5: After PR opens — recheck remote state**

  ```powershell
  gh pr view <number> --json state,reviews,statusCheckRollup
  ```
  Confirm CI starts. Per the PR-merge-gating standing rule, wait for code-owner approval + CodeRabbit's APPROVED before any merge attempt. Operator handles the final merge.

---

## Self-review checklist (run by the plan author after writing)

1. **Spec coverage** — every spec §3-§9 requirement maps to a task above:
   - §4 `currencies` schema → Tasks 1.1, 1.2, 1.3, 2.3 (migration).
   - §4 `google_revenue_source_rows` schema → Tasks 2.1, 2.2, 2.3.
   - §5 connector flow → Tasks 4.1-4.12, 5.1-5.2.
   - §6 legacy scaffolding preservation → Task 4 explicit non-goals + Task 6.1 guard.
   - §7 authorization (no new FX permission) → Tasks 7.1, 7.3.
   - §8 blast radius (no graph impact) → covered by spec contract; mentioned in report.md.
   - §9 testing inventory → mapped across Phases 1-8.
2. **No placeholders** — no `TBD`, no `similar to Task N`, no missing code blocks. Every code change shown completely.
3. **Type consistency** — `ParsedSourceRow`, `GoogleRevenueSourceRowEntry`, `IsoCurrency` defined once in Task 3.1; consumed by repository in Task 3.5 and parsers in Tasks 4.6 / 4.9 / 4.12 with the same fields.
4. **Method-name consistency** — `upsert_many`, `list`, `list_for_channel`, `get_exact` consistent between Task 3.4 tests and Task 3.5 implementation.
5. **Migration filename consistency** — `20260523_0001_google_revenue_source_foundation` referenced identically in Tasks 2.3, 2.4, 8.2, 9.1, 9.2.
6. **Permission set** — Task 7.3 snapshot mirrors the actual `Permission` enum members as of PR #41 baseline.

If any of the above drifts during execution, the subagent must surface it BEFORE writing code so the controller can amend this plan instead of working from a stale contract.
