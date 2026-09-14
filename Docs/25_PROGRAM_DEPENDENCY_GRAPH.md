# 25 — Program Dependency Graph

*Written 2026-08-28; live state reconciled through the 2026-09-07 update below.
This is an execution map, not proof that an open PR is delivered. PR #220 is
documentation only.*

PR #210 is GitHub-merged only into the closed, non-main PR #209 planning branch.
Neither #210's head nor merge commit is an ancestor of `main` at `41b4953`; none of
that implementation is delivered on `main`. PRs #221–#225 are the only current
main-targeted P0 successors.

---

## Live PR map (captured 2026-08-31)

| PR | Exact captured head | Live state | Honest scope |
| --- | --- | --- | --- |
| #220 | `8192dcbf` | OPEN, non-draft, BLOCKED | Seven docs: Docs/01, 15, 20, 21, 23, 24, 25; 16/25 review threads unresolved at capture |
| #221 / P0-a | `26bf0256` | OPEN, draft, BLOCKED | Compose + artifact storage; not on `main` |
| #222 / P0-b | `02298275` | OPEN, draft, BEHIND | Backup/restore; not on `main` and currently assumes #223 seed tables |
| #223 / P0-c | `edabb9a2` | OPEN, draft, BEHIND | Seed migration/bootstrap/authz; not on `main` |
| #224 / P0-d | `03e58c49` | OPEN, draft, BEHIND | Logging/ops; not on `main` |
| #225 / P0-e | `e8750885` | OPEN, draft, BLOCKED | Dev gateway docs + `/org-units` and `/users`; **not** `/security` |
| #226 | `29a542d2` | OPEN, draft, BEHIND | Proposed CI checks; not required on `main` yet |
| #227 | `e174c51f` | OPEN, draft, BEHIND | Requires redesign; current head fails the U2 typed-fence/source-system constraint and is not U2 ingest |
| #228 | `579c7f13` | OPEN, draft, BEHIND | Persistence/repository scaffold only; not A6, A7, or U3 completion |
| #229 | `4d6967ca` | OPEN, draft, DIRTY | Router/query/design-system scaffold with static fixtures; rewrite/restack required |

PR #211 is already **merged** to `main` as `41b4953` (rolling months). PR #212 at
`db576e01` is OPEN, non-draft, BLOCKED. PR #213 at `fed1e2ab` and #216 at `b1bd238b`
are OPEN drafts/BLOCKED; #214 at `f0d1c1e4` and #215 at `27dd1401` are OPEN
drafts/DIRTY. PR #217 at `80ef0fd` is a separate OPEN EGP Phase 1 draft/BLOCKED, not
part of the remaining P1 band. #211's month options are computed at module load, so a
long-running tab needs reload until a clock/provider follow-up lands.

---

## DAG (safe execution order)

```text
[#220 plans only: open/non-draft; current review gate still unsatisfied]

[P0-a #221 compose/storage: ./data/ums:/var/lib/ums on app/app-dev,
 absolute artifact/blob env targets, .gitignore + runtime persistence smokes]
          │
          ▼
[P0-c #223 bootstrap/authz seed]
          │
          ├──────────────► [P0-b #222 backup/restore, restacked or atomic with #223]
          │                         │
          │                         ▼
          │               [P0-d #224 logging/ops]
          │                         │
          │                         ▼
          │               [P0-e #225 gateway/docs: /org-units + /users]
          │
          ├──────────────► [A5 database-authz cutover runbook — still planned]
          │
          └──────────────► [#228 persistence scaffold on corrected #223 0002]
                                    │
                                    ├──► [A6 backend ceiling + isolation proof]
                                    ├──► [A7 identity enrollment + Google OIDC gateway]
                                    └──► [U3 effective-dated rate prerequisite]

[#211 merged] + [#212/#214/#215 integrated; remaining P1 drafts reconciled]
          │
          ▼
[#229 rewritten/restacked frontend foundation; current head is not integration-safe]
          │
          ├──► [A1 Admin UI] ──► [A2 matrix + /security proxy]
          │          │
          │          └────────► [A6 delegated-admin UI, after backend A6]
          ├──► [U3 display UI, after U2 + config + D-U1]
          └──► [graph projection + Cytoscape Trace]
                           │
                           ▼
                 [read-only Revenue Investigation Agent]

[#227 redesigned for typed fence + source contract, then merged and verified]
          │
          ▼
[U2 country ingest — separate implementation]
          │
          └──► [U3 backend estimate: U2 + restacked #228 config + D-U1]

[A5] + [A7] ──► external Google login for an HQ-managed existing user
[A5] + [A6] + [A7] ──► external login for any delegated/sub-company user
[A6] ──► A3 scoped-grants UI
```

The declared P0 letter order is not safe as authored: #222 requires non-empty seed
tables that #223 creates, but #222 does not contain #223. Merge #223 before #222 or
restack/land them atomically. #228 is now linear on `main`'s published head:
`… → 20260825_0002 → 20260911_0001 → 20260913_0001 → 20260828_0001`; the new
revision is never spliced between already-published revisions, which would
reintroduce multiple Alembic heads and silently skip the new tables on any
database that already recorded `20260913_0001`. `20260825_0002` remains an
irreversible security floor behind the `20260911_0001` rollback gate; #228
rollback stops above it rather than attempting to cross it.

#229 is not a de-mocked frontend. Its production views still import static values from
`src/fixtures/snapshotPanels`; its session query and design-system package are not
integrated. It conflicts with merged #211 and overlaps #212/#214/#215, so no downstream
Admin, U3, or graph work should treat the current #229 head as a completed prerequisite.

---

> **CI status at capture:** #226 proposes `ci-fast`, `ci-database`, and
> `ci-frontend`; none is an active required context on `main`. Branch protection
> currently requires only `DeepSource: Docker`, `DeepSource: JavaScript`,
> `DeepSource: Python`, `DeepSource: SQL`, `DeepSource: Secrets`, and
> `DeepSource: Shell`. The three proposed names become gates only after #226 merges
> and branch protection is updated.

---
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
[P0-c bootstrap/authz] ──► seed migration; bootstrap_operator prints UUID/email; `finance_admin@global` + direct `connectors.run_jobs@connector:manual-upload`; truthful org/roster setup attributed to that stored UUID; then database authz (no global beta bundle / placeholder ownership)
        │
        ▼
[P0-b backup/restore] ──► roles dump + data dump + artifact backup + rehearsal (Docs/22); restacked after P0-c because it requires #223's non-empty seed tables
        │
        ▼
[P0-d logging/ops] ──► structured logging; /readyz honesty; credential redaction
        │
        ▼
[P0-e dev gateway/docs] ──► Vite proxy (/users, /org-units); .env.example; runbook
        │
        ├──► [manual-import gate] — source-verified USD manifest; immutable PostgreSQL import-batch ledger inside the replacement transaction; audited open-month replacement for reduced manifests; resumable/idempotent loop; complete active roster + exact manual-fact set/report id/totals compared before complete
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
| Current #220 unresolved review threads = 0 and current required checks pass | Claiming #220 merge-ready; undraft is already complete |
| #223 merged before/restacked with #222 | Safe backup deployment and seed-floor validation |
| #221–#225 intentionally integrated to `main` | Claiming P0 delivered or running the current beta runbook |
| #226 workflows merged **and branch protection actually requires them** | Calling `ci-fast`, `ci-database`, or `ci-frontend` required checks |
| Docs/23 A2 adds `/security` | Admin access matrix in dev; #225 does not satisfy this |
| Redesigned #227 successor satisfies typed-fence/source constraint, then is merged and verified | U2 country ingest; current `e174c51f` does not clear this gate |
| #228 on corrected #223 `20260825_0002` with one Alembic head | Consuming its persistence scaffold |
| U2 ingest complete + effective-dated config service complete + D-U1 recorded | U3 estimate surfaces |
| #229 rewritten/restacked after P1 integration | Treating router/query/design-system work as a frontend prerequisite |
| All current #220 review threads resolved | Re-check after the final push; the DAG status above must be updated from the same repoll |
| P0-a…P0-e merged to `main` | A1, beta runbook, living Docs/21 status |
| USD manifest preflight + immutable PostgreSQL import-batch ledger + audited replacement + resumable import + complete-roster/exact-fact-set comparison green (implemented and suite-validated on `feat/p02a-manual-import-gate` cd9ef6555 — 3135 passed / 0 failed on fresh PostgreSQL at 7dd97dd0a plus the 7-test runner battery over the real app at cd9ef6555, which caught and fixed two runner API-shape defects; pending push/merge; merge before the first real import) | Any real manual revenue import / claim that a month is complete |
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

<!-- historical-poll -->
Update 2026-09-01: #221–#229 all carry pushed, suite-validated heads (none merged
yet) — #221 3044/0, #222 3181/0 plus real backup/rehearsal scenarios, #223 3037/0,
#224 3165/0, #226 174 bats, #227 2989/0, #228 3099/0, #225 719/719 and #229 588/588
frontend. The #227 interlock remains BLOCKED: a final candidate SHA **b92feb63d**
(2989 passed on fresh PostgreSQL) is supplied and recorded by the continuation on
2026-09-01, but none of #221-#229 is merged, and the hard gate requires the
reviewed SHA to be verified and merged before U2 ingest proceeds. When #227
merges, update this graph and the U2 acceptance state in
Docs/24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md together.
<!-- /historical-poll -->
The retained dated snapshots above sit inside `historical-poll` blocks;
recertification tooling and drift gates must exclude historical-poll blocks. Treat
live PR states, not this static graph, as the source for merge ordering.

<!-- historical-poll -->
As of the 2026-08-31 live poll, #221 and #225 are open/BLOCKED; #222–#224 are
open/BEHIND; none is merged. #226 is open/draft/BEHIND with `ci-fast` failing, and
#227 is open/draft/BLOCKED with no operator-supplied final SHA; its current candidate
also has an unresolved P1 and unpushed changes. Treat live PR states, not this static
graph, as the source for merge ordering.
<!-- /historical-poll -->

Update 2026-09-07: merged to `main` — #221 (P0-a, 2026-09-03), #230, #231, #232,
#217 (EGP Phase 1), and #233 (dependency refresh). Still open: #220 (this PR),
#222 (green, thread cleanup), #224, #225, and drafts #226–#229. The #227 interlock
remains BLOCKED: the candidate SHA b92feb63d is still an unmerged PR head.

See also: [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md),
[`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md),
[`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md),
[`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md).

## Recertification commands

Run these from the repository root on the exact PR head being reviewed:

```powershell
git diff --check origin/main...HEAD
git diff --check
$files = git diff --name-only origin/main...HEAD -- '*.md'
$files
$docs = @(
  'Docs/01_IMPLEMENTATION_PLAN.md',
  'Docs/15_DELIVERY_BACKLOG.md',
  'Docs/20_DEPLOYMENT_READINESS_AUDIT.md',
  'Docs/21_BETA_IMPLEMENTATION_PLAN.md',
  'Docs/23_ADMIN_ACCESS_AND_CONFIG_PLAN.md',
  'Docs/24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md',
  'Docs/25_PROGRAM_DEPENDENCY_GRAPH.md'
)
$markers = @(
  '2026-08-31',
  '#227 interlock remains BLOCKED',
  'merged to `main`',
  'No migration/backfill required',
  'Final PR #227 SHA is supplied',
  'final candidate SHA **b92feb63d**',
  'implemented and suite-validated on `feat/p02a-manual-import-gate`',
  'no environment fallback and no default rate',
  'SELF-grants by the just-created account',
  'typed `PAYMENT_NOT_FINALIZED` status',
  'non-partial btree index on the child columns'
)
foreach ($doc in $docs) {
  # Remove historical-poll block CONTENT before any marker search: retained
  # dated snapshots still contain their old phrasing by design, so a
  # current-state marker must never be satisfied inside them.
  $current = (Get-Content $doc -Raw) -replace
    '(?s)<!-- historical-poll -->.*?<!-- /historical-poll -->', ''
  Set-Content "$doc.current" -Value $current -NoNewline
}
foreach ($m in $markers) {
  $hits = rg -n -F $m --glob '*.current' $docs
  if ($LASTEXITCODE -ne 0) { Write-Error "recertification marker not found: $m"; exit 1 }
  $hits
}
Remove-Item "$docs*.current" -ErrorAction SilentlyContinue
$conflicts = rg -n '^(<<<<<<<|=======|>>>>>>>)' $docs
if ($LASTEXITCODE -eq 0) { $conflicts; exit 1 }
if ($LASTEXITCODE -gt 1) { exit $LASTEXITCODE }
$claude = rg -n 'Generated with \[Claude Code\]\(https://claude.com/claude-code\)' $docs
if ($LASTEXITCODE -eq 0) { $claude; exit 1 }
if ($LASTEXITCODE -gt 1) { exit $LASTEXITCODE }
```
