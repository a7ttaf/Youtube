# 25 — Program Dependency Graph

*Written 2026-08-30. Supersedes implicit ordering scattered across Docs/20–24.
P0 **implementation** is tracked by current successor PRs **#221–#225 (P0-a…P0-e)**.
PR #210 is historical: it merged on 2026-08-29 into the non-main
`docs/deployment-readiness-audit` branch, not into `main`.*

---

## DAG (execution order)

```text
[#220 docs amend]  ← you are here (draft pending final owner review after this cleanup)
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
[P0-e dev gateway/docs] ──► Vite proxy (/users, /org-units); .env.example; runbook
        │
        ├──► [ci-fast + ci-database + ci-frontend] proposed required gates (see status note)
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

> **CI status (2026-08-30):** `ci-fast`, `ci-database`, and `ci-frontend` are proposed
> by open PR #226; they are **not active required contexts** on current `main`.
> Branch protection currently requires only `DeepSource: Docker`, `DeepSource: JavaScript`,
> `DeepSource: Python`, `DeepSource: SQL`, `DeepSource: Secrets`, and `DeepSource: Shell`.
> Treat the three `ci-*` names as future gates until #226 lands and branch protection is
> updated.

## Hard gates (do not skip)

| Gate | Blocks |
| --- | --- |
| All 9 #220 review threads resolved | Satisfied; retain draft until final owner review and current checks |
| P0-a…P0-e merged to `main` | A1, beta runbook, living Docs/21 status |
| Proposed `ci-fast` + `ci-database` + `ci-frontend` gates in #226 | Future review-readiness gate; not active on current `main` |
| Normalization fence merged | U2 country ingest |
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

As of 2026-08-30, #221 is open/BLOCKED against `main` = `41b4953`; #222–#225 are
open/BEHIND from `d8418cea2`. Treat those live PR states, not this static graph, as
the source for merge ordering.

See also: [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md),
[`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md),
[`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md),
[`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md).
