# 25 — Program Dependency Graph

*Written 2026-08-30; recertified 2026-08-31. Supersedes implicit ordering scattered across Docs/20–24.
P0 **implementation** is tracked by current successor PRs **#221–#225 (P0-a…P0-e)**.
PR #210 is historical: it merged on 2026-08-29 into the non-main
`docs/deployment-readiness-audit` branch, not into `main`.*

---

## DAG (execution order)

```text
[#220 docs amend]  ← re-check review threads and analyzer statuses after every push
        │
        ▼
[P0-a compose/storage] ──► `./data/ums:/var/lib/ums` on app/app-dev; anchored `/data/` gitignore + Docker-context ignore before writes; absolute artifact/blob env targets; PG18; Redis; grace; log rotate; storage smokes
        │
        ▼
[P0-b backup/restore] ──► roles dump + data dump + artifact backup + rehearsal (Docs/22)
        │
        ▼
[P0-c bootstrap/authz] ──► seed migration; bootstrap_operator prints UUID/email; `finance_admin@global` + direct `connectors.run_jobs@connector:manual-upload`; truthful org/roster setup attributed to that stored UUID; then database authz (no global beta bundle / placeholder ownership)
        │
        ▼
[P0-d logging/ops] ──► structured logging; /readyz honesty; credential redaction
        │
        ▼
[P0-e dev gateway/docs] ──► Vite proxy (/users, /org-units); .env.example; runbook
        │
        ├──► [manual-import gate] — source-verified USD manifest; audited open-month replacement for reduced manifests; resumable/idempotent loop; complete active roster + exact manual-fact set/report id/totals compared before complete
        │
        ├──► [ci-fast + ci-database + ci-frontend] proposed required gates (see status note)
        │
        ▼
[#211 merged] ──► rolling month window on `main`
        │
        ├──► [open P1 cleanup: #212–#217] — de-mock, error boundary, confidence/alias/currency fixes; validate and push each PR independently
        │
        ├──► [A1 Admin UI + A2 matrix] (Docs/23) — after P0-e + session capabilities
        │
        ├──► [U1 probe] (Docs/24) — read-only; no UMS writes
        │
        ├──► [normalization fence] — BEFORE U2 ingest (country rows non-projecting; intentional evidence excluded from dropped-row WARNING/HIGH signals before reason redaction)
        │
        ├──► [U2 US country ingest] — after fence + EGP sequencing decision
        │
        ├──► [withholding config service] — PostgreSQL account/category effective intervals; no env/default fallback; D-U1 confirmed
        │
        ├──► [U3 estimate display] — backend-emitted only; after config service
        │
        ├──► [ExternalIdentity enrollment + home_org_unit_id] — audited one-time binding into active-only-unique revision history + global lifecycle fence before A6/A7 external access
        │
        ├──► [A6 delegated admin + read-isolation proof]
        │
        └──► [A7 Google SSO gateway adapter]
                 │
                 ▼
        [frontend foundation: de-mock, router, TanStack Query, design-system package]
                 │
                 ▼
        [graph projection + Cytoscape Trace] ──► [read-only Revenue Investigation Agent]
```

---

> **CI status (2026-08-31):** `ci-fast`, `ci-database`, and `ci-frontend` are proposed
> by open PR #226; they are **not active required contexts** on current `main`.
> Branch protection currently requires only `DeepSource: Docker`, `DeepSource: JavaScript`,
> `DeepSource: Python`, `DeepSource: SQL`, `DeepSource: Secrets`, and `DeepSource: Shell`.
> Treat the three `ci-*` names as future gates until #226 lands and branch protection is
> updated.

## Hard gates (do not skip)

| Gate | Blocks |
| --- | --- |
| All current #220 review threads resolved | Re-check after the final push; the DAG status above must be updated from the same repoll |
| P0-a…P0-e merged to `main` | A1, beta runbook, living Docs/21 status |
| USD manifest preflight + audited replacement + resumable import + complete-roster/exact-fact-set comparison green | Any real manual revenue import / claim that a month is complete |
| Proposed `ci-fast` + `ci-database` + `ci-frontend` gates in #226 | Future review-readiness gate; not active on current `main` |
| Final reviewed PR #227 SHA supplied, exact contract verified, then merged | U2 country ingest; intermediate/open heads are not shipped evidence |
| D-U1 AdSense rate confirmed + config row written | U3 estimate surfaces |
| A6 read-isolation matrix green | Any sub-company / competitor account |
| A5 + A6 + A7 all green | External Google login for delegated users |

---

## PR lineage

| Former | Successor |
| --- | --- |
| #209, #218, #219 (closed) | **#220** (consolidated docs) |
| #210 (historical; merged 2026-08-29 into non-main `docs/deployment-readiness-audit`) | **#221–#225** (current P0-a … P0-e successors on `main`) |
| Living schedule | Docs/21 status table on `main` after each P0 split merges |

As of the 2026-08-31 live poll, #221 and #225 are open/BLOCKED; #222–#224 are
open/BEHIND; none is merged. #226 is open/draft/BEHIND with `ci-fast` failing, and
#227 is open/draft/BLOCKED with no operator-supplied final SHA; its current candidate
also has an unresolved P1 and unpushed changes. Treat live PR states, not this static
graph, as the source for merge ordering.

See also: [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md),
[`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md),
[`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md),
[`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md).

## Recertification commands

Run these from the repository root on the exact PR head being reviewed:

```powershell
git diff --check origin/main...HEAD
git diff --check
$files = git diff --name-only origin/main...HEAD
foreach ($f in $files) { if ($f -like 'Docs/*.md') { Select-String -LiteralPath $f -Pattern '\[[^\]]+\]\(([^)]+)\)' -AllMatches } }
rg -n '2026-08-31|#221 and #225 are open/BLOCKED|#222.?#224 are open/BEHIND|No migration/backfill required|Final PR #227 SHA is supplied|no environment fallback and no default rate' Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md Docs/20_DEPLOYMENT_READINESS_AUDIT.md Docs/21_BETA_IMPLEMENTATION_PLAN.md Docs/23_ADMIN_ACCESS_AND_CONFIG_PLAN.md Docs/24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md Docs/25_PROGRAM_DEPENDENCY_GRAPH.md
```

The link check must resolve each non-URL Markdown link target relative to the source
file. The table check must count unescaped `|` separators for each contiguous Markdown
table and fail on mixed column counts.
