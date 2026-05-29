# AdSense Live Payment Sync — Design Spec

**Status:** Implemented in PR #51 on 2026-05-29 after operator-approved design
and implementation planning.

**Goal:** Pull *real* AdSense payments from the Google AdSense Management API
(`accounts.payments.list`) and land settled payments into the existing
`adsense_payments` PostgreSQL source-of-truth table, fail-closed, preserving the
Google-reported payment amount, currency, payment date, settlement month,
account identity, and source-report identity.

**Architecture:** A dedicated, CLI-triggered payment-sync path that is fully
separate from the `run_one` source-row connector framework. A new
`GoogleAdSensePaymentClient` fetches `payments.list`; a pure mapping module
converts each Google `Payment` into the repository input shape; a thin
`AdSensePaymentSyncService` orchestrates credential resolution, fetch, locked-
month prefilter, strict parsing, persistence via the existing
`SqlAlchemyAdSensePaymentRepository.sync_payments`, and audit. PostgreSQL stays
the source of truth.

**Tech stack:** Python 3.x, FastAPI (existing surfaces only), SQLAlchemy 2.x,
Alembic, PostgreSQL, `httpx` + `google-auth` (existing `GoogleHttpClient`).

---

## 1. Problem statement and current state

The delivery backlog line "AdSense payment sync — payment ORM + repo tests
(PR #26); real pull not built" understates what already exists. Verified state:

- **`adsense_payments` table + `SqlAlchemyAdSensePaymentRepository`** already
  exist (`db/finance_models.py:356`, `finance/adsense_payments.py`). Today they
  are fed only by a **manual operator upload**: `POST /adsense/sync-payments`
  (`api/adsense.py:80`).
- **No Google *payments* API client exists.** The PR #49
  `AdSenseManagementClient` calls `reports.generate` for *earnings*
  (ESTIMATED/TOTAL_EARNINGS), not payments.
- **`AuditEventType.ADSENSE_PAYMENT_SYNCED` already exists** (`auth/audit.py:14`)
  and is already emitted by the manual endpoint. No new enum value is required.
- **Reconciliation primitives already exist** (`finance/payment_matching.py`
  computes `payment_gap_usd`; `finance/bank_reconciliation.py`) and are **out of
  scope** for this PR.

"Real pull" therefore means: build the missing Google **payments** client +
connector service that lands real settled payment records into the existing
`adsense_payments` table — replacing the manual-only path with an automated
pull, **not** re-routing payments through `google_revenue_source_rows`.

---

## 2. Data source — `accounts.payments.list` (verified against Google docs)

`GET https://adsense.googleapis.com/v2/accounts/{account}/payments`

- Response wraps the list in a `"payments": [ {Payment}, ... ]` field.
- **No pagination** — no `pageSize` / `pageToken` request params and no
  `nextPageToken` response field. A single GET returns the full list.
- The `Payment` resource has exactly three output-only fields:
  - `name` — resource id whose suffix encodes type/status. Four documented
    forms:
    - `accounts/{account}/payments/unpaid` — running AdSense balance (no date)
    - `accounts/{account}/payments/youtube-unpaid` — running YouTube balance
      (no date)
    - `accounts/{account}/payments/yyyy-MM-dd` — a **paid** AdSense settlement
    - `accounts/{account}/payments/youtube-yyyy-MM-dd` — a **paid** YouTube
      settlement
  - `date` — credited date for paid earnings; **empty for unpaid**. Dates are in
    AdSense billing timezone, America/Los_Angeles.
  - `amount` — a **formatted string with the currency embedded**, e.g.
    `"$1,234.57"`, `"£87.65"`, `"¥1,235 JPY"` (inconsistent: sometimes a bare
    symbol, sometimes an explicit ISO code).

This thin, full-history, formatted-string shape drives the mapping, month-
derivation, currency-parsing, and locked-month decisions below.

---

## 3. Scope and non-goals

### In scope (this PR)
- `GoogleAdSensePaymentClient` for `payments.list`.
- Pure `Payment` → repository-input mapping with fail-closed parsing.
- `AdSensePaymentSyncService` (credential → fetch → locked-month prefilter →
  strict parse → `sync_payments` → audit), with `dry_run`.
- New CLI `scripts/run_adsense_payment_sync.py`.
- Schema change: add `source_account_id` to `adsense_payments` and extend the
  uniqueness contract; one Alembic migration.
- Update the manual `POST /adsense/sync-payments` request contract to require
  `source_account_id` (no silent default).
- Tests across client, mapping, service, repository, migration, manual API, CLI.
- Docs/backlog status updates (see §13).

### Explicit non-goals (this PR)
- **No `run_one` / `connector_runs` / `counts_json` involvement** for payments.
- **No writes to `google_revenue_source_rows`** or `raw_report_files`.
- **No reconciliation, allocation, or month-close logic changes** — the existing
  `payment_matching` / `bank_reconciliation` modules are untouched.
- **No new HTTP route** and **no new permission** — the live trigger is CLI-only.
- **No balance-snapshot model** — `unpaid`/`youtube-unpaid` balances are retained
  only as skip evidence, never as rows or reconciliation inputs.
- **No per-account expected-payment-currency config** (see §6 limitation).
- **No B3 FX / currency-conversion work.**
- **No Neo4j / graph projection work.**

---

## 4. Architecture and components

### New files
- `connectors/google/adsense_payments_client.py`
  - `GoogleAdSensePaymentClient(http: GoogleHttpClient)` mirroring
    `AdSenseManagementClient`.
  - `fetch_payments(*, account_id: str) -> dict[str, object]`: validates the
    account id via the shared `_validated_account_id` convention
    (strip → reject blank/whitespace-padded → strip `accounts/` prefix → reject
    reserved chars `/?#%`), issues the single GET via `GoogleHttpClient.request`,
    and stamps a deterministic source-report identity
    `source_report_id = sha256(f"{account_id}|accounts.payments.list").hexdigest()`.
  - Raises the existing typed errors (`MalformedAdsenseAccountIdError`,
    `OAuthRefreshError`, `GoogleApiResponseError`, and the `GoogleHttpClient`
    HTTP error family) — no new HTTP error classes.

- `connectors/google/adsense_payment_mapping.py` (pure, no I/O)
  - Classifies each `Payment` as a **balance** (no-date `unpaid`/`youtube-unpaid`)
    or a **paid settlement** (`[youtube-]yyyy-MM-dd`).
  - For paid settlements: derives the settlement month, enforces the
    suffix-date/`Payment.date` agreement, and (for open months only — see §7)
    parses the formatted amount string into `(Decimal amount, ISO currency)`.
  - Returns a structured result carrying eligible `AdSensePaymentInput`s, the
    skipped balances (with safe metadata), and skipped locked-month settlements.
  - All raw formatted amount strings are preserved into `raw_payload`.

- `connectors/google/adsense_payment_sync.py`
  - `AdSensePaymentSyncService` orchestrating the §5 pipeline. Builds the
    audit event, threads the connector service principal, and supports `dry_run`.

- `scripts/run_adsense_payment_sync.py` — CLI (see §10).

- A shared constant `ADSENSE_MANAGEMENT_CONNECTOR_KEY = "adsense-management"`
  placed in `connectors/google/registry.py`, referenced by the new code instead
  of raw string literals.

### Changed files
- `db/finance_models.py` — add `source_account_id` column; swap the unique
  constraint (§8).
- New Alembic migration under `db/alembic/versions/` (§8).
- `finance/adsense_payments.py` — `source_account_id` on `AdSensePaymentInput`
  and `AdSensePaymentEntry`; updated `audit_entity_id`, `to_api()`, within-batch
  duplicate key, and ON CONFLICT target (§8).
- `finance/month_close.py` — add a **read-only** `get_month_close_status(...)`
  accessor (§7) alongside the existing
  `get_or_create_month_close_row(..., for_update=True)`, which is unchanged.
- `connectors/runs/orchestrator.py` — promote `_credentials_for_run` to a public
  `resolve_connector_credentials(...)` (private name kept as a thin alias);
  **zero behavior change**, and **not** a `run_one` invocation (§9).
- `api/adsense.py` — require `source_account_id` on the manual request model and
  thread it into `AdSensePaymentInput` (§8, §11).
- `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` — status (§13).

---

## 5. Service pipeline (`AdSensePaymentSyncService`)

Operator-approved processing order, executed inside one DB transaction scope:

1. **Classify and shallow-validate.** Split `payments[]` into balances vs paid
   settlements. For each paid settlement, validate `name` + `Payment.date`
   enough to derive the settlement month. The resource-name date suffix **must
   equal** `Payment.date`; disagreement → typed error, abort, zero writes.
2. **Read-only locked-month prefilter.** For the distinct settlement months,
   look up existing finance month-close rows via the new read-only
   `get_month_close_status(...)`. A **missing** close row means **OPEN**. The
   prefilter **must not create close rows and must not take `FOR UPDATE` locks.**
3. **Skip LOCKED-month settlements with evidence.** They are excluded before any
   amount parsing. Record `skipped_locked_count` plus capped, safe per-entry
   metadata: resource name, month, payment_date, raw formatted amount,
   `reason="month_locked"`.
4. **Strict parse for OPEN-month settlements only.** Run the allowlist amount/
   currency parser (§6) on the remaining settlements. **Any** parse failure here
   raises a typed sync error and aborts the entire sync with **zero DB writes**.
   (Consequence: a locked-month `"$"` row is skipped in step 3 and never causes a
   parse abort.)
5. **Persist or no-op.** If nothing remains after filtering, **do not** call
   `sync_payments`; return success with `synced_count=0` and still audit the
   pull + skips. Otherwise call `sync_payments` with the open-month
   `AdSensePaymentInput`s.

`sync_payments` is invoked unchanged. If a month transitions to LOCKED between
the prefilter (step 2, unlocked read) and the write, `sync_payments`' own per-
month `get_or_create_month_close_row(..., for_update=True)` gate raises
`AdSensePaymentLockedMonthError`, which propagates and aborts the sync
(fail-closed). The prefilter is a best-effort skip; `sync_payments` is the
authoritative race guard.

`dry_run=True`: execute steps 1–4 (including fail-closed validation/parsing) but
**skip step 5 entirely** — no `sync_payments` call, no audit event, no DB rows.
Print the would-sync / skipped counts.

---

## 6. Amount and currency parsing (fail-closed)

Applied only to OPEN-month settlements (§5 step 4). The raw formatted string is
**always** preserved into `raw_payload` regardless of outcome.

Resolution order for currency:
1. If an explicit 3-letter ISO 4217 code is present in the string, use it
   (uppercased) and store it.
2. Else, accept only **unambiguous** symbols from an explicit allowlist:
   `£ → GBP`, `€ → EUR`.
3. Any bare ambiguous symbol (`$`, `¥`, `kr`, `د.إ`-style variants, or any
   symbol mapping to more than one currency) → **fail closed**. There is **no**
   global `$ → USD` assumption.

Numeric parsing is deterministic and `Decimal`-based; it rejects malformed
values, negatives, multiple decimal separators, and unsupported locale shapes.

Storage accepts **any** valid ISO-4217-shaped 3-letter uppercase code permitted
by the table CHECK constraint; it is **not** gated on the app's supported
display/conversion currency set, so a real but "unsupported" currency is stored,
never silently dropped.

Failure mode: any unknown/ambiguous currency or unparseable amount on an
**open-month** settlement raises a typed sync error and aborts the whole sync
before `sync_payments` writes anything. **Malformed paid settlements are never
silently skipped** — a real payment missing from the source of truth is worse
than a loud failed sync.

**Known, documented limitation (intentional this PR):** AdSense commonly returns
a bare `"$1,234.57"`. Such accounts will **fail loudly** here because there is no
per-account expected-payment-currency config yet. Adding that config (so a
configured/validated account currency can disambiguate `$`) is an explicit
follow-up PR — never a silent USD assumption.

---

## 7. Month derivation, eligibility, and locked months

- **Eligible for `sync_payments`:** only paid settlements with a real credited
  date — resource names `accounts/{account}/payments/yyyy-MM-dd` and
  `accounts/{account}/payments/youtube-yyyy-MM-dd`.
- `unpaid` and `youtube-unpaid` are **never** inserted (no date, no settlement
  month). They are retained only as skip evidence: `skipped_balance_count` plus
  safe per-entry metadata (resource name, raw formatted amount,
  `reason ∈ {"no_payment_date", "no_settlement_month"}`), kept out of all
  reconciliation/math until a separate balance-snapshot model exists.
- `month = Payment.date`'s `YYYY-MM` (the only defensible derivation; billing tz
  is America/Los_Angeles per the docs). The resource-name date suffix is
  cross-checked against `Payment.date`; disagreement fails closed.
- `payment_status = "PAID"` for every row created from `payments.list` this PR.
- **Locked historical settlements are skipped before amount parsing** (§5 steps
  3→4), preserving month-close immutability and keeping live re-pulls operational
  forever as historical months lock.

The new read-only month-close accessor used by the prefilter:

```
get_month_close_status(session, month, *, tenant_id) -> str | None
# Returns the close status ("OPEN"/"LOCKED") or None when no row exists.
# Pure SELECT: no row creation, no FOR UPDATE. Read-only.
```

---

## 8. Data model change — `adsense_payments`

### Column
Add `source_account_id` (`Text`, **NOT NULL**) with a non-empty CHECK
constraint. Persisted value uses the **same canonical account-id convention as
the Google source rows**: input `accounts/{account}` → stripped non-empty suffix;
blank/malformed account ids fail closed (`_validated_account_id`).

### Uniqueness contract
Change from:

```
uq_adsense_payments_month_name           (tenant_id, month, payment_name)
```

to:

```
uq_adsense_payments_account_month_name   (tenant_id, source_account_id, month, payment_name)
```

`payment_name` stays the raw Google resource-name suffix (e.g. `2026-04-15` or
`youtube-2026-04-15`). Account identity is **not** encoded into `payment_name`,
and is **not** duplicated across `source_account_id` and `payment_name`.

### Repository (`finance/adsense_payments.py`)
- `AdSensePaymentInput` and `AdSensePaymentEntry` gain `source_account_id: str`.
- `AdSensePaymentEntry.audit_entity_id` →
  `f"{source_account_id}:{month}:{payment_name}"`; `to_api()` includes it.
- Within-batch duplicate guard key → `(source_account_id, month, payment_name)`.
- `sync_payments` ON CONFLICT `index_elements` →
  `[tenant_id, source_account_id, month, payment_name]`.
- `_require_month_open` lock gate and `imported_by`-preservation-on-rerun are
  unchanged.

### Migration (one Alembic revision)
- Add the column and the non-empty CHECK; drop the old unique constraint; add the
  new one.
- **Existing pre-alpha rows** (manual uploads, which have no account id): backfill
  with an explicit, documented one-time legacy sentinel
  (`source_account_id = "__legacy_manual__"`) clearly marked non-production, then
  hold the column NOT NULL. Reset/reseed is an acceptable alternative given
  disposable pre-alpha data. **New writes must never omit `source_account_id`.**
- Downgrade reverses to the original `(tenant_id, month, payment_name)` unique
  constraint and drops the column.

### Manual endpoint (`api/adsense.py`)
- `AdSensePaymentRequest` gains `source_account_id: str = Field(min_length=1)`
  (per-payment), whitespace-stripped, canonicalized via `_validated_account_id`.
- The handler threads it into `AdSensePaymentInput`. **No silent default.**
- `connector_key` stays `"adsense"` — this is the **control-plane scope key** for
  the manual endpoint's permission/audit scope and is unrelated to the live
  credential-lookup key; the two are deliberately not conflated.

---

## 9. Credential resolution and connector keys

- Live payment sync resolves the **same** Google AdSense OAuth credential the
  earnings connector uses, under the canonical credential key
  `adsense-management` (hyphen/underscore aliasing already handled by
  `_credential_key_candidates`). No new credential row, no `adsense-payments`
  alias, no separate OAuth identity.
- Reuse the existing resolution chain by promoting
  `_credentials_for_run(*, session, tenant_id, connector_key, account_id)`
  (`connectors/runs/orchestrator.py:442`, all-keyword) to a public
  `resolve_connector_credentials(...)` — the private name kept as a thin alias.
  This is the only orchestrator edit, carries **zero behavior change**, and is
  **not** a `run_one` invocation. It raises the existing typed
  `CredentialNotFoundError` / `InactiveCredentialError`, then resolves the secret
  (`resolve_secret`), builds and refreshes credentials
  (`build_credentials_from_payload` → `refresh_credentials`).
- The control-plane `"adsense"` key (manual endpoint scope) is distinct from the
  `adsense-management` credential key and must not be confused.

---

## 10. CLI — `scripts/run_adsense_payment_sync.py`

Mirrors `scripts/run_google_connector.py` conventions.

- Flags: `--tenant <UUID>` (required), `--account <account-id or accounts/...>`
  (required), `--reason <non-empty audit reason>` (required), `--dry-run`
  (optional).
- Resolves credentials with canonical key `adsense-management`.
- Uses the configured connector service actor
  (`UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` → `build_connector_service_principal`,
  `Permission.RUN_CONNECTOR_JOBS`) for audit attribution and permission
  consistency; a missing/blank actor env fails closed before any DB write.
- Calls `AdSensePaymentSyncService`, which fetches `accounts.payments.list`,
  runs the §5 pipeline, and (live mode) emits `ADSENSE_PAYMENT_SYNCED`.
- **Exit codes:** `0` success (including a clean dry-run and a
  `synced_count=0` no-op); `2` typed pre-write/config/credential/parse/validation
  failure (the typed-error set: account/month/suffix validation,
  amount/currency parse errors, `AdSensePaymentValidationError`,
  `AdSensePaymentLockedMonthError`, credential/`GoogleConnectorError`,
  service-actor `ValueError`). Untyped errors propagate with a traceback
  (non-zero).
- No new HTTP route, no new permission, no operator-console trigger this PR.

---

## 11. Error handling and fail-closed invariants

- Typed domain errors only; no bare `except`, no silent swallowing.
- Account id blank/malformed → fail closed (`MalformedAdsenseAccountIdError`).
- Suffix-date ≠ `Payment.date` → fail closed.
- Open-month amount/currency unparseable/ambiguous → fail closed, zero writes.
- Locked-month settlements → skipped with evidence (not an error).
- `sync_payments` remains strict on locked months as the final race guard;
  manual endpoint locked-month rejection is unchanged.
- No raw secret/token leakage in errors or audit; only safe identifiers
  (resource name, month, raw formatted amount string) appear in skip metadata,
  capped to a bounded count.

---

## 12. Invariants and guarantees (explicit)

1. **`sync_payments` remains strict as the race guard.** It is not modified to
   permit locked-month writes; it remains the authoritative per-month
   `FOR UPDATE` locked-month gate.
2. **The manual `POST /adsense/sync-payments` locked-month behavior is
   unchanged** — it still rejects locked months exactly as today (only its
   request contract gains the required `source_account_id`).
3. **Locked historical settlements are skipped before amount parsing** — the
   read-only locked-month prefilter (§5 step 3) runs *before* the strict parser
   (§5 step 4), so locked rows never trigger a parse abort.
4. **PostgreSQL `adsense_payments` remains the source of truth** for payments;
   the live pull only writes through the existing repository.
5. **No graph projection impact detected.** No Neo4j projection reads or writes
   are involved; nothing in this PR mutates source-of-truth data via a graph
   path.
6. **No `run_one` / `connector_runs` / `google_revenue_source_rows`
   involvement this PR.** The payment-sync path is fully separate from the
   source-row connector framework; no connector-run rows, no `counts_json`, no
   source-row writes.

---

## 13. Blast radius

- **Tables written:** `adsense_payments` (schema change + row upserts) and
  `audit_logs` (audit event) **only**.
- **Not touched:** `google_revenue_source_rows`, `raw_report_files`,
  `connector_runs`, `connector_run_raw_files`, revenue facts, C1 normalizer,
  reconciliation tables, currency/FX tables.
- **Auth/audit:** no new `AuditEventType` value, no new `Permission`; only a new
  `details` discriminator (`trigger="live_pull"`, `synced_count`, `months`,
  `skipped_balance_count`, `skipped_locked_count`) on the existing
  `ADSENSE_PAYMENT_SYNCED` event.
- **Database disposition:** `Disposable pre-alpha data reset accepted: backfill
  sentinel (or reseed) required for the NOT-NULL `source_account_id` column.`
  Migration is otherwise backward-compatible (additive column + unique-constraint
  swap with a reversible downgrade).
- **Graph:** `No graph projection impact detected.`
- **PR #50 tracker flip:** the stale `⏳ PR #50 — awaiting merge` line in
  `Docs/01`/`Docs/15` will be flipped to `✅ merged 2026-05-28 (9c884bd)` as part
  of **this feature's implementation PR** (not the spec commit, and not a
  standalone tracker-only PR).

---

## 14. Test matrix

**Client (`adsense_payments_client.py`)**
- Correct method + URL path `…/v2/accounts/{account}/payments`.
- Account-id validation: blank/whitespace-padded/reserved-char → fail closed;
  `accounts/` prefix stripped.
- Single-response handling (no pagination); reads the `payments` array.
- Malformed response shape → `GoogleApiResponseError`.
- Deterministic `source_report_id` stamp; raw payload preserved.

**Mapping (`adsense_payment_mapping.py`)**
- Eligibility: `unpaid` and `youtube-unpaid` skipped with metadata; paid
  `yyyy-MM-dd` and `youtube-yyyy-MM-dd` eligible.
- `month` = `Payment.date` `YYYY-MM`; suffix-date ≠ `Payment.date` → fail closed.
- Currency: explicit ISO accepted; `£→GBP`, `€→EUR`; `$` abort; `¥` abort.
- Amount: malformed / negative / multiple-separator abort; `Decimal` exactness.
- Proof of **zero DB rows on failure** (mapping raises before persistence).

**Service (`adsense_payment_sync.py`)**
- Successful sync of open-month settlements; idempotent rerun.
- Locked-month settlement **skipped** (not aborted): `skipped_locked_count` + safe
  metadata; OPEN siblings still upserted.
- Locked-month `"$"` row skipped (no parse abort).
- Nothing-remains after filtering → no `sync_payments` call, `synced_count=0`,
  success, pull/skips still audited.
- Credential failure surfaces before any DB write.
- Mid-pull lock race → `sync_payments` raises and the sync aborts.
- Audit payload: `ADSENSE_PAYMENT_SYNCED` with `trigger="live_pull"` + counts.
- `dry_run`: fetch + validate + parse, **no** `sync_payments`, **no** audit,
  **no** rows.

**Repository / model (new unique key)**
- Same tenant + month + payment_name, **different** `source_account_id` →
  separate rows allowed.
- Same tenant + same `source_account_id` + month + payment_name → idempotent
  upsert.
- Within-batch duplicate keyed on `(source_account_id, month, payment_name)`.
- Tenant scoping intact (existing tenant-scope tests updated).

**Migration**
- New unique constraint is `(tenant_id, source_account_id, month, payment_name)`.
- `source_account_id` NOT NULL; legacy backfill sentinel applied; downgrade
  reverses cleanly.

**Manual API (`POST /adsense/sync-payments`)**
- Missing `source_account_id`/account id → validation error **before** DB write.
- Malformed account id → fail closed.
- Existing permission / reason / connector-scope / safe-error tests updated for
  the new field. (No new endpoint added.)

**CLI**
- Exit `0` on success and clean dry-run; exit `2` on typed pre-write failure;
  required-flag/argument validation.

---

## 15. Validation gate (for the implementation PR)

- `python -m ruff check backend tests scripts`
- Targeted: client, mapping, service, repository, migration, manual-API, CLI
  tests.
- Full gate: `pytest -q` with
  `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/test_ums`
  (real PostgreSQL migration + upsert round-trip).
- `git diff --check`.

---

## 16. Follow-ups (explicitly deferred)

- Per-account expected-payment-currency config to disambiguate bare-symbol
  amounts (notably `$`).
- A balance-snapshot model for `unpaid`/`youtube-unpaid` running balances.
- An operator-facing HTTP trigger / operator-console run history for payment
  pulls (Option 2 from design selection), if a unified run history is needed.
- A `--month` CLI filter as an operator convenience (does not replace the
  locked-month rule).
- Wiring pulled payments into the existing reconciliation surfaces.
