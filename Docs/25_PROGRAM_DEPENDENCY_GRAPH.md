# 25 — Program Dependency Graph

*Written 2026-08-28; live state reconciled 2026-08-31. This is an execution map,
not proof that an open PR is delivered. PR #220 is documentation only.*

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
restack/land them atomically. #228 is now linear on corrected #223:
`20260825_0001 → 20260825_0002 → 20260828_0001`; changing that parent would
reintroduce multiple Alembic heads. `20260825_0002` is an irreversible security floor;
#228 rollback stops at that revision rather than attempting to cross it.

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
| A6 read-isolation matrix green | Any sub-company / competitor account |
| A5 + A6 + A7 all green | External Google login for delegated users |

---

## PR lineage

| Historical | Current main-targeted successor |
| --- | --- |
| #209, #218, #219 (closed drafts) | #220 consolidated documentation (open/non-draft) |
| #210 (merged only into closed #209 lineage; never delivered on `main`) | #221–#225 P0 split PRs (all still open at capture) |
| P1.2 estimate | #211 merged as `41b4953`; live-rollover provider remains follow-up |
| A6/A7/U3 persistence plan | #228 scaffold, after mandatory migration restack; feature work remains |
| Frontend foundation plan | #229 conflicting scaffold; reimplementation/restack remains |

See also: [`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md),
[`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md), and
[`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md).
