# Delivery Backlog

## Status (2026-05-22)

Reconciled through PR #36 (S2 multi-tenant integration). Marker conventions
match `01_IMPLEMENTATION_PLAN.md`:

- `✅ PR #N` — shipped end-to-end at the layer being marked.
- `⏳ PR #N — remaining: <note>` — partial; concrete remaining work is
  named.
- `🗑️ removed in PR #N — <reason>` — dropped from scope.

Honesty rule: scaffolding-only items (ORM + repo + tests but no real
ingestion / UI / user-facing path) are marked `⏳`, not `✅`.

## P0 — Must build first

- ⏳ Dynamic org hierarchy — remaining: ORG models (PR #25); hierarchy
  assignment workflow not built.
- ✅ Channel registry (tenant-scoped, PR #25).
- ⏳ CMS/outside-CMS status — remaining: schema column (PR #25);
  outside-CMS revenue sourcing unresolved (Hard Problem #1).
- ✅ Group builder (tenant-scoped channel group registry, PR #25 + tests
  in PR #30).
- ⏳ YouTube report ingestion — remaining: credentials repo (PRs #33,
  #34); real ingestion not built.
- ⏳ Monthly revenue normalization — remaining: revenue facts foundation
  (PR #2); ingestion source not wired.
- ⏳ AdSense payment sync — remaining: payment ORM + repo tests (PR #26);
  real pull not built.
- ⏳ Finance month-close screen — remaining: close-gate backend (PR #8);
  UI not built.
- ⏳ Net revenue calculation — remaining: revenue facts (PR #2);
  allocation pass not built.
- ⏳ Confidence labels — remaining: column scaffolding in revenue facts;
  UI surfacing not built.
- ⏳ Explain-number API — remaining: number explanation entry + repo +
  factory (PR #31); explain endpoint not built.
- ⏳ Smart issue panel — remaining: not started.
- ⏳ Excel export — remaining: export artifacts foundation (PR #9) +
  tenant-scoped export jobs (PR #36); format/template not finalized.

## P1 — Strong beta features

- ⏳ PDF export — remaining: not started.
- ⏳ Branded slide export — remaining: not started.
- ⏳ Outside-CMS monitor — remaining: not started.
- ⏳ Recalculation by allocation method dry-run foundation — remaining:
  not started.
- ⏳ Month lock/unlock — remaining: close-gate (PR #8); explicit
  lock/unlock workflow not built.
- ⏳ Manual override approval — remaining: number explanation substrate
  (PR #31); approval workflow not built.
- ⏳ Audit dashboard — remaining: tenant-scoped audit log backend
  (PR #22); dashboard UI not built.

## P2 — Advanced features

- ⏳ Automated currency rate integration foundation — remaining:
  `currency_exchange_rates` schema scaffolding + design doc (PR #16,
  `Docs/18`); engine, fetcher, fallback policy not built.
- ⏳ Anomaly detection foundation for source-backed month-over-month
  revenue movement — remaining: not started.
- ⏳ Detailed Shorts revenue handling foundation — remaining: not started.
- ⏳ Custom report builder — remaining: not started.
- ⏳ Saved dashboard views — remaining: not started.
- ⏳ User-level favorite groups — remaining: not started.
- ⏳ Scheduled report emails — remaining: not started.

## P3 — Later update

- ⏳ Title/show mapping — remaining: not started; out of scope for first
  release per Phase 0.
- ⏳ Ramadan/event title profit — remaining: not started.
- ⏳ Playlist/hashtag mapping — remaining: not started.
- ⏳ Video-level title classification — remaining: not started.
- ⏳ AI-assisted mapping review — remaining: not started.
- ⏳ Content ID/fingerprint/claims modules if needed later — remaining:
  not started; out of scope for first release per Phase 0.

## Cross-cutting shipped (not in original P0–P3)

Infrastructure delivered across S0/S1/S2 that does not map cleanly to any
single P-tier above.

- ✅ Backend foundation: FastAPI + SQLAlchemy + Alembic + pytest baseline
  (PR #1).
- ✅ Auth foundation: user accounts, roles, permission grants, DB-backed
  principal authorization, lifecycle APIs, access read APIs
  (PRs #2 – #7, #20, #22 – #24).
- ✅ Multi-tenant infrastructure: `tenants` + `platform_admins` tables;
  header resolver + contextvar; principal–tenant binding; `tenant_id` on
  18 operational tables; tenant-scoped repositories across auth, org,
  finance, connectors, reports; tenant-aware export jobs
  (PRs #18 – #34, #36).
- ✅ Trusted-gateway tenant middleware + bootstrap UMS tenant for
  SQL-backed trusted-header requests (PR #36).
- ✅ Governance + quickstart docs (PR #11).
- ✅ CI gate (Elite-CI vendored) + Dependabot (PRs #14, #17).
- ✅ Docker + docker-compose stack (PR #15).
- ✅ Architecture docs: multi-tenant (`Docs/17`), multi-currency
  (`Docs/18`) — PR #16.
- 🗑️ Neo4j graph component retired entirely (PR #12).
- ✅ Local validation gate: `scripts/run_validation_gate.py` invokes
  `backend/ums_smart_revenue/devtools/quality_gate.py` which runs ruff
  (backend + tests + scripts), the AST-based no-skip/xfail policy gate,
  pytest full suite (strict-config + strict-markers + isolated
  `.pytest-tmp`), and `git diff --check` (working tree + staged) — PR #38.
- ✅ Developer agent rules: `AGENTS.md` (Codex), `.agents/skills/`
  (vitest + postgresql-table-design), `skills-lock.json` — PR #38.
  The Claude-Code-local `CLAUDE.md` is gitignored as a per-machine copy.
- ✅ Per-PR documentation system at `Docs/pulls/` (one
  `report.md` + `changelog.md` + `handoff.md` per PR) — coexists with
  inline `✅ / ⏳ / 🗑️ PR #N` marks on this doc and
  `01_IMPLEMENTATION_PLAN.md`.
- ✅ Mockup catch-up: OFL-licensed soft-dark variant
  (`mockups/ums-smart-revenue-command-center-soft-dark.html` + 9 QA
  screenshots + `generate-screenshots-soft-dark.py` + `mockups/FontsGH/`
  with Mona Sans / Monaspace Neon / Newsreader) committed as a
  redistributable sibling to the canonical
  `mockups/ums-smart-revenue-command-center.html`, referenced from
  `Docs/09_SMART_DASHBOARD_UI.md` — PR #39.

## Hard problems to solve early

1. ⏳ Revenue source for 70 outside-CMS channels — still open; not solved
   by any shipped PR.
2. ⏳ System-managed report availability and retention — remaining: raw
   report file ORM + repo (PR #32); ingestion + retention policy not
   built.
3. ⏳ Payment gap explanation — remaining: bank reconciliation repo
   (PR #29) is the substrate; comparison + explanation pass not built.
4. ⏳ Bank/transfer/FX data source — remaining: bank recon repo (PR #29)
   and `currency_exchange_rates` scaffolding; real FX provider not wired.
5. ⏳ Confidence labels that finance trusts — remaining: column
   scaffolding in revenue facts; computation rules + UI surfacing not
   built.
6. ✅ Flexible grouping without hardcoded UMS structure — channel group
   registry (PR #25 + tests in PR #30).
