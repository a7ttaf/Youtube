# PR #227 re-author: U2 country evidence fence

## Scope

This branch replaces the unsafe live PR #227 implementation. It adds the U2
country-sliced evidence lane while retaining the existing allowlisted
`source_system="youtube_analytics"` contract.

Country rows are persisted in `google_revenue_source_rows` with their original
source account, channel, country, amount, currency, and source-row key. The
parser records `report_type="reports.query.country_evidence"` and
`raw_payload.projection_disposition="NON_PROJECTING_EVIDENCE"`. The distinct
report type gives evidence its own stale-cleanup scope, so a later flag-off
worldwide run cannot delete it.

Before the finance normalizer creates any canonical bucket, it preflights every
row and separates valid country evidence from projecting rows. Valid country
evidence never reaches `monthly_channel_revenue_facts`. Malformed or duplicate
evidence is rejected with a typed reason and remains visible in skipped-row
telemetry. The direct analytics CSV reader independently requires the projecting
report type, no country key, and either a legacy-missing or explicit PROJECTING
token before a row may enter its aggregate.

## Activation

Country collection is off by default. Set:

```text
UMS_YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_ENABLED=true
```

only after choosing a clean side of the EGP currency/source-row-key cutover.
Missing or false keeps the existing monthly request behavior. An unrecognized
value fails settings loading instead of ambiguously enabling collection.

The enabled runner issues a second per-channel request using:

```text
dimensions=country
metrics=estimatedRevenue
```

The ordinary worldwide monthly request remains unchanged. A country request
failure is a report-scoped partial failure; it does not silently disappear. An
empty response must still carry the exact metric headers requested, preventing
a malformed empty success from authorizing stale evidence cleanup.

## Audit behavior

The normalization adapter emits a separate
`lifecycle="NON_PROJECTING_EVIDENCE"` connector audit summary with:

- accepted and rejected counts;
- accepted counts by country;
- rejected counts by typed reason.

The summary excludes row identifiers, source-account identifiers, amounts, and
raw payloads. Healthy accepted evidence is not copied into `ROWS_SKIPPED`, so it
cannot create the generic high-severity skipped-source-row alert. Rejected
evidence remains in `ROWS_SKIPPED` because it is a real provenance defect. Both
summaries are limited to the triggering Analytics connector, content-owner
account, and PARSED country raw files linked to that exact run; another account
or a flag-off rerun cannot claim the retained evidence snapshot.

## Explicit non-goals

- No withholding rate, withholding estimate, actual withholding, or tax advice.
- No finance total, close, allocation, reconciliation, export, report, or UI
  consumption.
- No new source-system value.
- No client-side finance calculation.
- No schema or Alembic change.
- No automatic activation.

## Database and blast radius

Affected existing table: `google_revenue_source_rows` only, through the existing
repository and transaction path. The canonical normalizer and analytics CSV
reader are materially changed to fence evidence; no other finance/export/report
consumer is added. PostgreSQL remains the source of truth.

No migration/backfill required.

Existing country evidence imported without the parser-owned disposition token
is rejected from projection and reported as invalid evidence. It is not
silently relabeled. Disabling the feature flag is safe and preserves the
separate evidence rows. A code rollback is **not** safe while evidence remains:
retain the projection/export fences, or explicitly purge/quarantine country
evidence before reverting. Reverting to the parent normalizer with retained
evidence could project a country row into an official fact.

## Integration order

This branch is based on exact main commit
`41b4953939b39b55345d3d7a168eeaf57c8e2b90` and must be restacked after the
final PR #224 hardening branch. It must also be reconciled with the consolidated
Docs/24 plan from the PR #220 documentation line before review readiness.

Do not replay live PR #227 commit `e174c51f`: it invents an unsupported source
system, silently drops evidence, and does not implement the country producer.

## Validation record

- `uv sync --extra dev --extra test --extra lint` — passed.
- Focused parser/client/runner/normalizer/audit/export command — 166 passed.
- Widened connector/source-row/normalizer/export command — 723 passed with
  the two explicit PostgreSQL connector files excluded.
- Complete orchestrator module — 57 passed; complete exports API module — 47
  passed; PostgreSQL JSONB fence compile/fail-closed tests — 2 passed.
- `uv run ruff check backend tests scripts` — passed.
- `uv run mypy backend` — passed, 215 source files.
- Pre-review PostgreSQL executor/RLS retry:
  `uv run pytest -q tests/connectors/runs/test_executor_rls_postgres.py tests/connectors/runs/test_run_one_rls_postgres.py`
  — the exact eight selected tests passed in 6.98s. Later review fixes changed
  reader/cleanup/audit scoping and were validated by the final non-PostgreSQL
  commands above; per operator direction no second Docker retry or broad
  PostgreSQL suite was run.
- Earlier full baseline `uv run pytest -q` — 2,792 passed. The remaining 21
  failures and 122 errors were PostgreSQL-only tests refusing the absent
  `UMS_TEST_DATABASE_URL`; no non-PostgreSQL test failed. Final review fixes are
  covered by the final focused/widened commands above, not this earlier count.
- `git diff --check` — passed.

An exclusive PostgreSQL retry used container
`ums-pr227-u2-pg-20260831-a6d1`, container id prefix `e5a1d25fe341`, image
`postgres:18-alpine` (`sha256:9a8afca5...`), and host port 55527. Initial
`pg_isready` passed, then the Docker Desktop Linux engine disappeared during
fixture setup. All eight requested gates ended in connection timeouts. The
engine later recovered, and exact inspection by the full container id and name
returned `No such container`; exact-name and `owner=pr227-u2` listings were
empty. No removal command was necessary and no other container was touched.
This verifies cleanup of the initial attempt only; it does not retrospectively
convert that attempt's eight timeouts into passes.

A single bounded retry used container
`ums-pr227-u2-pg-retry-20260831-19127f`, full id
`e65333027f1c01b99daff3b0baaab330c33cdf1158098f47a790d4502266a133`,
image `postgres:18-alpine`, and dynamically assigned host port 55666. Exactly
the one executor lane test and seven `run_one` RLS tests passed (`8 passed in
6.98s`). The full id, `owner=pr227-u2-retry`, and candidate label were verified
immediately before removal. Only that container was removed; subsequent full-id,
exact-name, and owner/candidate-label checks confirmed it was absent.
