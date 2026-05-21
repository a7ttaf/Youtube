# S2 Stack Integration & Local Recovery — Design Spec

**Date:** 2026-05-21
**Owner:** Director DevOps / QA / Software Engineer (Claude)
**Status:** Design — awaiting user approval before implementation plan

---

## 1. Problem statement

The local working tree is at PR #9 (2026-05-13) after an HD-loss recovery. Origin is far ahead, and the entire S2 multi-tenant foundation lives on `pr/s2-4a-tenant-id-on-operational-tables` — 71 commits that have never been integrated to `main`. Three PRs are open in flight, planning docs on local `main` are stale, and `frontend/` exists on `main` but not on the stack. We need to: recover local state safely, finish the in-flight PRs, integrate the S2 stack to `main` once, add a minimal tenant header to the frontend, and reconcile planning docs with the new reality.

## 2. Goals

- Local working tree aligned with the live remote state, without losing any unpushed local commits.
- Open PRs #31, #32, #34 closed (merged or revised) only after live re-verification.
- The S2 multi-tenant stack integrated to `main` via one auditable merge PR with proper Alembic head merge and full validation.
- Frontend sends `X-Tenant-Id: UMS` on every API call so it works against the multi-tenant backend.
- Planning docs (`01_IMPLEMENTATION_PLAN.md`, `15_DELIVERY_BACKLOG.md`, `16_OPEN_DECISIONS.md`) reconciled with what was actually built; stale Neo4j references removed; shipped work marked.

## 3. Non-goals (explicitly out of scope)

- No frontend tenant switcher UI.
- No real auth integration in the frontend.
- No new backend features beyond what already shipped on the stack.
- No incidental refactor of files we touch.
- No deletion of `Docs/_archived/`.
- No squash, rebase, or rewrite of merged history.
- No CI/CD config changes.

## 4. Approach

Six phases, sequential. Every phase has explicit checkpoints where execution pauses for user approval before any push, merge, or destructive local action. Work happens on dedicated branches rooted at `origin/main` — local `main` is only ever fast-forwarded from origin.

## 5. Phases

### Phase 0 — Live state audit

Re-confirm everything the planning audit assumed is still current. Do not trust stale `APPROVED` / `CHANGES_REQUESTED` states from earlier inspection.

**Commands (read-only):**
- `git status -s`
- `git branch -vv` (shows tracking + ahead/behind per local branch)
- For each of PR #31, #32, #34: `gh pr view N --json state,reviewDecision,mergeStateStatus,headRefOid,statusCheckRollup,latestReviews`
- `gh pr checks 31`, `gh pr checks 32`, `gh pr checks 34`
- `gh api graphql` for current `reviewThreads { isResolved, isOutdated }` on each PR
- `gh pr view 25 --json mergedAt,headRefOid` to confirm PR #25 is still merged into stack head

**Outputs:** a single "live state" report containing, per PR: state, review decision, merge state, head SHA, status check rollup, unresolved review thread count.

**No checkpoint** (read-only). The output of this phase becomes input to Phases 1 and 2.

### Phase 1 — Local recovery

Align local branches with origin without losing unpushed local commits.

**Pre-step 1 (working tree safety):** run `git status --porcelain=v1 -uall` and inspect output.
- If any tracked/staged/modified entry exists (M, A, D, R, U, or `??` for untracked-but-not-ignored that matters), `git reset --hard` will destroy that work.
- Required action:
  - **🛑 CHECKPOINT (extra, conditional):** surface the dirty entries, ask whether to (a) safety-commit on a temporary `safety/working-tree-2026-05-21` branch, (b) `git stash push -u -m "pre-recovery 2026-05-21"`, or (c) discard.
  - Do NOT proceed to reset until working tree is clean per user's chosen option.
- Note: `.gitignore`-d files (e.g., `frontend/node_modules/`, `__pycache__/`) are safe — `git reset --hard` ignores them.

**Pre-step 2 (unpushed-commit safety):** for every local branch that has a remote tracking counterpart, run `git log origin/<branch>..<branch> --oneline`. If output is non-empty for ANY branch:
- Create safety tag: `git tag backup/<branch>-2026-05-21 <branch>`
- **🛑 CHECKPOINT (extra, conditional):** surface findings, ask user how to handle the unpushed commits (cherry-pick, push, or discard)

If working tree is clean AND all local branches are clean relative to origin, no extra checkpoint needed.

**Recovery:**
- `git fetch origin --prune`
- `git checkout main && git reset --hard origin/main`
- For each S2 branch listed in branches with `+` (active in some worktree) or that has an origin counterpart, `git checkout <branch> && git reset --hard origin/<branch>`
- Verify: `wc -l backend/ums_smart_revenue/org/sql_channel_registry.py` should be 175 on `main`, 214 on stack head.

**Remote impact:** none.
**Remote checkpoint:** not required. Stop if destructive reset would discard unpushed local commits (see pre-step).
**Rollback:** re-fetch and reset; safety tags preserve any discarded local work.

### Phase 2 — Finish in-flight PRs

Three sub-phases, each with a live re-read before any merge action. "Approved earlier" does not equal "approved now."

**Phase 2a — PR #31 (finance/explanations tests, was CHANGES_REQUESTED)**

Live re-read first: `gh pr view 31 --json state,reviewDecision,mergeStateStatus,headRefOid,statusCheckRollup,latestReviews` and check current unresolved review threads via GraphQL. If a different review state is now in effect, branch logic accordingly.

If still CHANGES_REQUESTED with the previously identified items:
- Checkout `pr/s2-4b-finance-explanations-tests`
- Fix #1: in `tests/finance/test_explanations.py::test_record_explanation_separates_rows_by_composite_key`, change one entry's `metric` to a second constant so the partition test actually exercises `metric` as part of the composite key
- Fix #2: in `docs/pulls/2026-05-20-pr31-*.md` (3 files), replace `github.com/XGenerationy/youtube` with `github.com/XGenerationy/Youtube`
- Fix #3: in the same handoff files, replace hardcoded `/home/mahmoud/...` paths with portable `PYTHONPATH=backend python -m pytest …` / `python -m ruff …` invocations
- Validation gate: `ruff check backend tests`, `pytest -q tests/finance/test_explanations.py`, `pytest -q` (full suite), `git diff --check`
- **🛑 CHECKPOINT** — show diff + validation output, ask before `git push`
- After push, re-read PR state once more (`gh pr view 31 --json reviewDecision,mergeStateStatus,statusCheckRollup`)
- **🛑 CHECKPOINT** — show updated PR state, ask before `gh pr merge 31 --squash --delete-branch`

**Phase 2b — PR #32 (raw_files tests, was APPROVED)**

- Live re-read: `gh pr view 32 --json state,reviewDecision,mergeStateStatus,headRefOid,statusCheckRollup` plus current unresolved review threads
- Required preconditions for merge: state=OPEN, reviewDecision=APPROVED, mergeStateStatus=CLEAN, all checks green, zero unresolved review threads
- If any precondition fails, stop and report
- **🛑 CHECKPOINT** — show live state, ask before `gh pr merge 32 --squash --delete-branch`

**Phase 2c — PR #34 (credentials review cleanup, was APPROVED)**

Same protocol as Phase 2b, against PR #34.

**After Phase 2:** zero open PRs on the S2 stack. `git fetch origin && git rev-parse origin/pr/s2-4a-tenant-id-on-operational-tables` to record the new stack-head SHA.

### Phase 2.5 — Post-recovery baseline gate

Before any new code work, confirm the recovered tree actually passes the CLAUDE.md baseline. This catches the case where `origin/main` has pre-existing lint or test debt that would otherwise be misattributed to our work.

- `python -m ruff check backend tests` against current `origin/main`-aligned tree
- `python -m pytest -q` (full suite) against same
- `git diff --check` (should be clean if Phase 1 was clean)

Outcome handling:
- **All green:** record the baseline, proceed to Phase 3
- **Ruff fails on origin/main:** stop. **🛑 CHECKPOINT** — surface the failing rules. Decide with user whether to (a) fix lint debt as a separate small PR before continuing, or (b) document the blocker, snapshot the failing rules, and proceed with the explicit understanding that subsequent phases must not add to the debt.
- **Pytest fails on origin/main:** stop. **🛑 CHECKPOINT** — this is more serious; do not proceed until root cause is understood. Possible causes: environment drift (missing PYTHONPATH, missing pip deps, missing PG/docker), or genuine regression on main. Diagnose before continuing.

### Phase 3 — Write the S2 integration plan doc

Author the integration plan as a docs-only PR so the strategy lives in the repo before any merge work begins.

- Branch `pr/docs-s2-integration-plan` off current `origin/main`
- Author `Docs/implementation/S2_INTEGRATION_PLAN.md` containing:
  - Conflict surface map: list of every file with conflict resolution rule (Alembic versions, Docs/01–18, frontend, lockfiles)
  - Alembic merge plan: "discover heads, then merge heads" (no hardcoded SHAs — heads are discovered live during Phase 4)
  - Validation gate sequence including disposable PostgreSQL preference
  - Rollback procedure
- **Also include this spec file** (`Docs/superpowers/specs/2026-05-21-s2-integration-and-recovery-design.md`) in the same PR. It belongs alongside the integration plan it spawned.
- Validation: doc diff hygiene, `git diff --check`, link check (canonical `Docs/` casing — no lowercase `docs/` references except where the existing `docs/pulls/` directory is genuinely lowercase)
- **🛑 CHECKPOINT** — show diff, ask before push + PR open + merge to main
- After merge: `git fetch origin --prune && git rev-parse origin/main` → record new main SHA

### Phase 4 — Execute the S2 stack → main integration

Highest-risk phase. Every transition surfaces evidence before continuing.

**Step 4.1 — Pre-flight**
- Confirm `origin/pr/s2-4a-tenant-id-on-operational-tables` head is the SHA recorded at end of Phase 2
- Re-fetch `origin/main`
- `git merge-base origin/main origin/pr/s2-4a-tenant-id-on-operational-tables` — confirm divergence is what the plan expects

**Step 4.2 — Discover live Alembic heads**

`alembic.ini` lives at repo root; all alembic commands run from repo root with `PYTHONPATH=backend`. Never `cd backend`.

- Check out a temp branch from `origin/main`, run `PYTHONPATH=backend python -m alembic heads` (from repo root), note output
- Check out a temp branch from `origin/pr/s2-4a-...`, run same command, note output
- Document the actual head IDs (DO NOT hardcode in advance)

**Discovered topology (recorded 2026-05-21 — confirm live before acting):**

Pre-integration audit (see `Docs/implementation/S2_INTEGRATION_PLAN.md`) discovered a **3-head divergence** on the main side that was previously missed: migration `20260516_0001` (`tenants_foundation`) branched from `20260513_0001` (`bank_reconciliation`) and **skipped** the parallel `20260513_0002..0006` chain (`retire_graph_permissions` → `export_artifact_metadata` → `currency_exchange_rates` → `revenue_format_breakdown` → `export_job_scope_channel_snapshot`). The stack's head `20260518_0001` is descended from `20260516_0001` via `20260517_0001`, so it transitively absorbs `20260516_0001`.

Effective heads to merge depend on what `alembic heads` reports against the integration tree:

- **2-head outcome (expected):** `20260513_0006` (main's other branch) and `20260518_0001` (stack tail, transitively including `20260516_0001`). Proceed with a 2-head `alembic merge` in Step 4.5.
- **3-head outcome (defensive):** `20260513_0006`, `20260516_0001`, and `20260518_0001` listed independently — meaning the descent from `20260516_0001` to `20260518_0001` was not resolved during merge. Proceed with a 3-head `alembic merge` in Step 4.5 (pass all three head IDs to one `alembic merge` invocation; do not chain two 2-way merges, since that produces an asymmetric history that is harder to revert).
- **1-head outcome (unexpected):** a single head means somehow the topology was already flattened. **🛑 CHECKPOINT** — investigate before proceeding; the integration plan assumes ≥2 heads.

**Step 4.3 — Create integration branch**
- `git checkout -b pr/s2-integration-merge origin/main`
- `git merge origin/pr/s2-4a-tenant-id-on-operational-tables --no-ff --no-commit`

**Step 4.4 — Resolve conflicts per documented rules**
- **Alembic versions/**: keep both sides' migration files unchanged
- **Docs/01–18 conflicts**: take the stack version (post-Neo4j retirement, post-S2 docs)
- **frontend/**: take main's version (stack has no frontend)
- **Other prose conflicts**: **🛑 CHECKPOINT** per ambiguous hunk — surface to user, ask which side
- Commit the merge once conflicts are resolved

**Step 4.5 — Generate Alembic merge migration**
- All alembic commands run from repo root with `PYTHONPATH=backend`. Never `cd backend`.
- With both branches now in the working tree: `PYTHONPATH=backend python -m alembic heads` — confirm head count and match against the Step 4.2 topology
- Pass **all live heads in a single `alembic merge` invocation** (do not chain 2-way merges, which produce asymmetric history that is harder to revert):
  - 2-head case:
    ```bash
    PYTHONPATH=backend python -m alembic merge -m "merge S2 stack and main 0513 finance/export chain" <head_a> <head_b>
    ```
  - 3-head case (defensive — see Step 4.2):
    ```bash
    PYTHONPATH=backend python -m alembic merge -m "merge S2 stack and main heads" <head_a> <head_b> <head_c>
    ```
- Inspect the generated merge migration file. **Confirm it is a no-op merge**: `upgrade()` and `downgrade()` bodies must contain only `pass` (no `op.create_table`, `op.add_column`, `op.alter_column`, etc.). If the generator produced any schema operations, **🛑 STOP and CHECKPOINT** — that means the branches touched the same tables in incompatible ways and the merge is unsafe without hand-resolution
- After the merge migration is committed: `PYTHONPATH=backend python -m alembic heads` must now return exactly **one** head
- Commit the merge migration (this is a separate commit from the merge-resolution commit in Step 4.4 — keeps the diff readable)

**Step 4.6 — Validation gate**
- `python -m ruff check backend tests`
- `python -m pytest -q` (full suite)
- **Prefer disposable PostgreSQL** for Alembic validation. All alembic invocations from repo root with `PYTHONPATH=backend`:
  - If docker is available: `docker compose up -d postgres` (or equivalent), set `DATABASE_URL` env var to the disposable PG, run `PYTHONPATH=backend python -m alembic upgrade head`, then `PYTHONPATH=backend python -m alembic downgrade -2`, then `PYTHONPATH=backend python -m alembic upgrade head`, tear down container
  - Fall back to SQLite scratch DB only if PG unavailable. If fallback used, document it in the validation report and call out the contract gap explicitly (SQLite does not enforce all PG constraints used by this repo)
- `cd frontend && npm install && npm run build` — confirm frontend still builds against merged code (the `cd` is required here because `package.json` is in `frontend/`)
- `git diff --check` (final)
- **🛑 CHECKPOINT** — show conflict summary + validation report + Alembic head merge output before push

**Step 4.7 — Push + PR + merge**
- Push integration branch: `git push -u origin pr/s2-integration-merge` (after checkpoint)
- **🛑 CHECKPOINT** — show PR body draft before `gh pr create --base main --head pr/s2-integration-merge`
- After CI runs on the PR, re-read PR state: `gh pr view <N> --json state,reviewDecision,mergeStateStatus,statusCheckRollup`
- **🛑 CHECKPOINT** — show CI results + mergeStateStatus, ask before merging
- **Merge type: `gh pr merge <N> --merge` (NOT `--squash`, NOT `--rebase`).** Rationale: this PR represents the integration of an entire stack; squashing destroys per-PR provenance that the audit + review trail relies on. The merge commit on main is the single rollback anchor.

**After Phase 4 merge:** `git fetch origin --prune && git rev-parse origin/main` → record the new main SHA (this is the **integration merge commit SHA** — keep it; it is the rollback anchor for Step 4.8).

**Step 4.8 — Rollback definition**
- Rollback target: the integration merge commit on `main` (the SHA recorded above).
- Procedure: branch off `origin/main`, run `git revert -m 1 <integration_merge_sha>` (mainline=1 because main is the first parent of the merge commit), push the revert branch, open a revert PR, merge with `gh pr merge --merge`.
- Alembic rollback if needed: against disposable PG, `PYTHONPATH=backend python -m alembic downgrade <pre_merge_head>` where `pre_merge_head` is the head ID recorded in Step 4.2 from main's side. The Alembic merge migration is additive and reversible.
- Net effect: main returns to its pre-integration state in one PR. No data loss because the integration was a code-only merge (no data migration ran in prod).

### Phase 5 — Frontend tenant header

Add `X-Tenant-Id: UMS` to all API calls from the frontend. Minimal scope: hardcoded slug, no UI changes, no auth integration.

- Confirm `origin/main` SHA is the one recorded at end of Phase 4
- Branch `pr/frontend-tenant-header` off updated `main`
- Inspect `frontend/src/` to locate the existing API client. Decision matrix:
  - If a central API client/wrapper already exists: add the header in that one place. No refactor.
  - If 1–2 raw `fetch` call sites exist: add the header inline at each. No refactor.
  - If 3+ raw `fetch` call sites exist: introduce one minimal client module, route all calls through it, add the header there. **🛑 CHECKPOINT** before consolidation since this expands scope beyond pure header injection.
- The injected header is `X-Tenant-Id: UMS` (hardcoded constant, not env-driven for this run)
- Manual verification:
  - Backend running locally (uvicorn) with multi-tenant code from Phase 4
  - `npm run dev`, open browser, exercise one read path and one write path
  - Confirm backend logs / DB row shows the request was tenant-resolved
- **🛑 CHECKPOINT** — show diff + manual verification evidence before push
- **🛑 CHECKPOINT** — show PR before opening
- **🛑 CHECKPOINT** — ask before `gh pr merge`

**After Phase 5 merge:** record new `origin/main` SHA.

### Phase 6 — Reconcile planning docs

Per the user's definition: update in place; mark complete what is built; remove obsolete Neo4j; reflect multi-tenant/S2 state; keep unresolved items in backlog; do not delete or archive active planning docs.

Branch `pr/docs-reconcile-planning` off current `origin/main`.

**`Docs/01_IMPLEMENTATION_PLAN.md`:**
- Phase 0–2 (Foundation, Channel registry, YouTube ingestion): mark complete with PR refs from the shipped work
- Phase 3 (AdSense payment matching): mark partial — payment sync done (PR #9), matching pending; reference what shipped
- Phase 4 (Reconciliation & allocation): mark partial — month_close, manual_overrides, net revenue, bank reconciliation foundation done; allocation rules pending
- Phase 5 (Smart dashboard): mark partial per current frontend state
- **Phase 6 (Neo4j graph read-model): delete entirely.** Retired in PR #12.
- Renumber subsequent phases (7→6, 8→7)
- Add new phase: "Multi-tenant foundation (S2)" with completed item list (tenants table, header resolver, tenant_id on operational tables, tenant-scoped repositories) — and outstanding items (frontend tenant switcher, real auth integration, second-tenant onboarding workflow)

**`Docs/15_DELIVERY_BACKLOG.md`:**
- P0: mark shipped items inline (e.g., `- Channel registry. ✅ shipped (PR #25)`)
- P1: remove "Neo4j graph sync" and "Graph explorer pages"
- Add new P1 item: "Multi-tenant onboarding workflow"
- P2/P3 unchanged
- "Hard problems to solve early": review each, mark progress where applicable

**`Docs/16_OPEN_DECISIONS.md`:**
- "Stack decisions" — mark resolved: Backend (FastAPI), Database (PostgreSQL)
- "Neo4j decisions" — delete the entire section (system retired)
- Finance / UI / Data decisions: keep as-is; they remain genuinely open
- "Suggested default decisions" code block: remove Neo4j line, keep rest

Validation: doc diff hygiene, `git diff --check`, link check (no broken cross-doc references).

- **🛑 CHECKPOINT** — show full doc diff before push
- **🛑 CHECKPOINT** — show PR before opening
- **🛑 CHECKPOINT** — ask before `gh pr merge`

**After Phase 6 merge:** record final `origin/main` SHA.

## 6. Cross-cutting requirements

### Validation gates (CLAUDE.md baseline)

Run after every code change:
- `python -m ruff check backend tests`
- `python -m pytest -q`
- `git diff --check`

Plus changed-scope additions:
- Phase 2a: `pytest tests/finance/test_explanations.py`
- Phase 4: full pytest, Alembic upgrade/downgrade against disposable PostgreSQL (preferred) or SQLite (fallback, documented), `npm run build` for frontend
- Phase 5: `npm run build`, manual browser verification
- Phase 6: doc-diff hygiene, link integrity

Per CLAUDE.md rule #8: never skip, xfail, or loosen tests to pass a gate. If a gate fails, report the exact command, blocker, and rerun command.

### Post-merge state recording

After every merge to `main` (Phases 3, 4, 5, 6):
- `git fetch origin --prune`
- `git rev-parse origin/main` → record SHA in run log
- Confirm SHA before starting next phase

### Live re-read protocol

Before any `gh pr merge` or `git push` to a remote-watched branch:
- `gh pr view <N> --json state,reviewDecision,mergeStateStatus,headRefOid,statusCheckRollup`
- Confirm: state=OPEN, decision=APPROVED (or follow-up CR addressed), mergeStateStatus=CLEAN, checks=SUCCESS, zero current unresolved review threads
- Stop and report on any precondition failure

### Destructive operation protocol

Per CLAUDE.md and director-level discipline:
- No `git reset --hard` without verifying no unpushed local commits exist (Phase 1 pre-step)
- No `git push --force` (we don't need it; rebases are not used)
- No `--no-verify` skip of hooks
- No `gh pr merge` without prior live re-read
- Safety tags created before any potentially destructive local reset

## 7. Error handling & rollback contract

| Risk | Mitigation | Rollback |
|---|---|---|
| Local working tree corrupted | All work in branches; main only fast-forwards from origin | re-fetch + reset to origin |
| Local branch has unpushed commits | Phase 1 pre-step creates safety tag, surfaces to user | `git reset --hard backup/<branch>-<date>` |
| Push wrong content | Pause-at-every-push gate with diff review | revert PR or amend before merge |
| Bad merge on main | Single merge commit, easy to revert | `git revert -m 1 <merge_sha>` + revert PR |
| Alembic merge migration regresses | Validated on disposable PG before push | `alembic downgrade -1` + revert PR |
| PR #31 fix breaks tests | Validation gate catches before push | revert local commit, re-fix |
| Frontend breaks | Manual verify + npm build before push | revert PR |
| Doc reconciliation loses info | Doc-only PR, instant revert | revert PR |
| Live re-read shows new CR not addressed | Stop, surface to user, do not merge | n/a — never merged |

## 8. Success criteria

- Local `main` SHA == `origin/main` SHA, verified after each phase
- PRs #31, #32, #34 closed (merged or otherwise resolved)
- One green merge commit on `main` representing the S2 integration
- `alembic heads` returns a single head on `main`
- Full pytest suite green on `main` post-integration
- Frontend `npm run build` succeeds on `main` post-Phase 5
- Browser-loaded frontend successfully calls the multi-tenant backend (verified manually)
- `Docs/01_IMPLEMENTATION_PLAN.md` contains no Neo4j references (except in `Docs/_archived/`)
- `Docs/15_DELIVERY_BACKLOG.md` reflects shipped status of P0 items
- `Docs/16_OPEN_DECISIONS.md` has no obsolete decisions; genuine open items preserved
- No tests skipped, xfailed, or weakened
- No `--force` push to any remote
- All work auditable through PRs visible in `gh pr list --state all`

## 9. Open questions resolved by user prior to spec

- Scope: all four buckets (recovery, in-flight PRs, integration, frontend, planning reconcile)
- Integration approach: merge PR (not rebase, not squash); merge type is `gh pr merge --merge`
- Checkpoints: pause at every push/merge to remote
- Plan clearing: update planning docs in place, mark complete, remove obsolete, preserve open items, do not archive active docs
- Frontend tenancy: hardcode `X-Tenant-Id: UMS` header
- Phase 0 added per user feedback
- Phase 1 wording corrected per user feedback ("no remote checkpoint needed; stop if destructive reset would discard unpushed local commits")
- Pre-merge live re-read added for Phases 2b and 2c per user feedback
- Alembic heads discovered live, not hardcoded, per user feedback
- Disposable PostgreSQL preferred for Alembic validation per user feedback
- Post-merge `origin/main` SHA recorded after every merge per user feedback
- Phase 1 working-tree dirty check added per user feedback (Pre-step 1)
- Alembic invocations corrected per user feedback (always `PYTHONPATH=backend python -m alembic …` from repo root; never `cd backend`)
- Phase 2.5 post-recovery baseline gate added per user feedback
- Spec file location: canonical `Docs/` casing (this file lives at `Docs/superpowers/specs/2026-05-21-s2-integration-and-recovery-design.md`); committed alongside `Docs/implementation/S2_INTEGRATION_PLAN.md` in Phase 3's docs PR
- Integration merge type made explicit (`--merge`) and rollback procedure tied to the integration merge commit SHA per user feedback
