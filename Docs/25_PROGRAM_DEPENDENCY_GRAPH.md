# 25 — Program Dependency Graph

*Written 2026-08-28. Supersedes implicit ordering scattered across Docs/20–24.
P0 **implementation** is tracked on restacked split PRs from former #210 — **not**
on blocked PR #210 itself until those land on `main`.*

---

## DAG (execution order)

```text
[#220 docs amend]  ← you are here (stay draft until 9 review threads resolved)
        │
        ▼
[P0-a compose/storage] ──► host bind mount for artifacts; PG18; Redis; grace; log rotate
        │
        ▼
[P0-b backup/restore] ──► roles dump + data dump + artifact backup + rehearsal (Docs/22)
        │
        ▼
[P0-c bootstrap/authz] ──► seed migration; bootstrap_operator; beta_operator role; org skeleton
        │
        ▼
[P0-d logging/ops] ──► structured logging; /readyz honesty; credential redaction
        │
        ▼
[P0-e dev gateway/docs] ──► Vite proxy (/users, /org-units, /security); .env.example; runbook
        │
        ├──► [ci-fast + ci-database + ci-frontend] required on main
        │
        ▼
[P1 band restack: #211–#217] ──► rolling months, de-mock, error boundary, alias cleanup
        │
        ├──► [A1 Admin UI + A2 matrix] (Docs/23) — after P0-e + session capabilities
        │
        ├──► [U1 probe] (Docs/24) — read-only; no UMS writes
        │
        ├──► [normalization fence] — BEFORE U2 ingest (country rows non-projecting)
        │
        ├──► [U2 US country ingest] — after fence + EGP sequencing decision
        │
        ├──► [withholding config service] — effective-dated; no default rate; D-U1 confirmed
        │
        ├──► [U3 estimate display] — backend-emitted only; after config service
        │
        ├──► [ExternalIdentity + home_org_unit_id] — before A6/A7 external access
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

## Hard gates (do not skip)

| Gate | Blocks |
| --- | --- |
| All 9 #220 review threads resolved | Undraft #220 |
| P0-a…P0-e merged to `main` | A1, beta runbook, living Docs/21 status |
| `ci-fast` + `ci-database` + `ci-frontend` required | Claiming review-ready on any code PR |
| Normalization fence merged | U2 country ingest |
| D-U1 AdSense rate confirmed + config row written | U3 estimate surfaces |
| A6 read-isolation matrix green | Any sub-company / competitor account |
| A5 + A6 + A7 all green | External Google login for delegated users |

---

## PR lineage

| Former | Successor |
| --- | --- |
| #209, #218, #219 (closed) | **#220** (consolidated docs) |
| #210 (blocked, wrong base) | **P0-a … P0-e** (restacked onto `main`) |
| Living schedule | Docs/21 status table on `main` after each P0 split merges |

See also: [`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md),
[`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md),
[`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md).
