# S2 Stack → main Integration Plan

**Date:** 2026-05-21
**Author:** Director DevOps / QA / Software Engineer
**Status:** Active — referenced by `Docs/superpowers/plans/2026-05-21-s2-integration-and-recovery.md`

## Purpose

Integrate `pr/s2-4a-tenant-id-on-operational-tables` (the S2 multi-tenant foundation, ~74 commits including the in-flight PRs #31, #32, #34 merged on 2026-05-21) into `main` via one auditable merge PR.

## Approach

Single merge PR. Merge type: **`gh pr merge --merge`** (NOT squash, NOT rebase). Rationale: preserve per-PR provenance for all S2 commits; single rollback anchor on main.

---

## Conflict surface

### Alembic migrations — 3-head merge required

`backend/ums_smart_revenue/db/alembic/versions/` divergence topology (discovered live on 2026-05-21):

```
20260510_0001..0008 (security_foundation, org_registry, finance_close, revenue_facts,
                     manual_overrides, raw_report_files, number_explanations, export_jobs)
        |
        v
20260511_0001 (permission_grant_revoke_reason)
        |
        v
20260512_0001 (users_service_account_status) → 20260512_0002 (adsense_payments)
        |
        v
20260513_0001 (bank_reconciliation)
        |
        ├──────────────────────────────────────────────────┐
        v                                                  v
20260513_0002 (retire_graph_permissions)        20260516_0001 (tenants_foundation)
        |                                                  |
        v                                                  v
20260513_0003 (export_artifact_metadata)        20260517_0001 (tenant_id_on_operational_tables)
        |                                                  |
        v                                                  v
20260513_0004 (currency_exchange_rates)         20260518_0001 (tenant_scoped_youtube_channel_identity) ★STACK HEAD
        |
        v
20260513_0005 (revenue_format_breakdown)
        |
        v
20260513_0006 (export_job_scope_channel_snapshot) ★MAIN HEAD #1
```

**Heads to merge:**
- `20260513_0006` — main's older branch (PRs #4–#10 era; was never integrated with the S2 line)
- `20260516_0001` — main's S2.1 branch tail (heads here, but `20260516_0001` itself skipped `20260513_0002..0006`)
- `20260518_0001` — stack's tail (S2.4b-org-1 + tenant-scoped channel identity)

Wait: `20260518_0001` is descended from `20260516_0001` (via `20260517_0001`), so the effective heads to merge are just **two**:
- `20260513_0006` (main's other branch)
- `20260518_0001` (stack's tail, which transitively includes `20260516_0001`)

**Resolution plan (live discovery confirmed):** during integration, after merging the stack:

```bash
PYTHONPATH=backend python -m alembic heads
# expect 2 heads: 20260513_0006 and 20260518_0001
PYTHONPATH=backend python -m alembic merge -m "merge S2 stack and main 0513 finance/export chain" 20260513_0006 20260518_0001
```

If `alembic heads` returns 3 heads (i.e., `20260516_0001` shows as separate from `20260518_0001`), then merge all three:

```bash
PYTHONPATH=backend python -m alembic merge -m "merge S2 stack and main heads" 20260513_0006 20260516_0001 20260518_0001
```

The generated merge migration MUST be a no-op (empty `upgrade()` and `downgrade()`). If alembic auto-generates schema operations, STOP and CHECKPOINT — that means the branches touched the same tables in incompatible ways.

### Frontend (`frontend/`)

- Status on stack: **missing entirely** (stack branched off at S2.1, before PR #10 added the frontend)
- Status on main: present (`index.html`, `package.json`, `package-lock.json`, `src/`, `tsconfig.json`, `vite.config.ts`)
- Resolution: **take main's version** (`git checkout --ours frontend/` on integration branch). Stack has no claim.

### Docs (`Docs/`)

Approximately 28 files changed across the divergence. Categories:

- **Stack-only updates (take `--theirs`)**: 
  - `Docs/01_IMPLEMENTATION_PLAN.md` (post-Neo4j retirement)
  - `Docs/03..04_*` (Neo4j docs archived in stack)
  - `Docs/07_REVENUE_RECONCILIATION_ENGINE.md`, `Docs/09_SMART_DASHBOARD_UI.md`, `Docs/12_BACKEND_API_SPEC.md`, `Docs/13_SQL_DATA_MODEL.md`, `Docs/15_DELIVERY_BACKLOG.md`, `Docs/16_OPEN_DECISIONS.md` (S2-aware content)
  - `Docs/_archived/*` (added on stack as part of Neo4j retirement)
  - `Docs/security/*` (post-S2 permission matrix)
- **Stack-added files (no conflict, just added)**:
  - `Docs/17_MULTI_TENANT_ARCHITECTURE.md`
  - `Docs/18_MULTI_CURRENCY_ENGINE.md`
  - `Docs/implementation/CODEX_STABILIZATION_PLAN.md`

Default rule: **take stack's version** (`git checkout --theirs <file>` then `git add <file>`).

Exception: if a `Docs/` file has only a trivial main-side update (e.g., a typo fix) that's NOT in the stack version, CHECKPOINT and hand-merge. Default is `--theirs`.

### Lockfiles

- `pyproject.toml`: take **main's** version (PR #17 bumped uvicorn 0.46.0 → 0.47.0; the stack predates this bump)
- `requirements*.txt`: same
- `frontend/package-lock.json`: take main's (stack has none)

### `docs/pulls/` (lowercase) handoff files

- Stack has `docs/pulls/2026-05-20-pr*-*.md` handoff artifacts (added during the S2.4b coverage PRs).
- Main does not. Take stack's version (`--theirs`).

### Other files

For any conflict not above: **STOP and CHECKPOINT** per ambiguous hunk. Surface the hunk to user, ask which side or hand-merge.

---

## Validation gate

Must run on the integration branch **before push**, in order:

1. `python -m ruff check backend tests` — must pass. (Origin/main has 55 pre-existing ruff errors per Phase 2.5 baseline; PR #27 cleanup is on the stack and will reach main via this integration. Post-merge expected: 0 errors.)
2. `python -m pytest -q` — must pass full suite. Origin/main baseline = 452 tests. Post-integration target = ~570 (452 + ~120 new from S2.4b coverage PRs).
3. **Alembic validation — prefer disposable PostgreSQL**:
   - If docker is available: `docker compose up -d postgres`, set `DATABASE_URL`, run `PYTHONPATH=backend python -m alembic upgrade head` (must reach single head), then exercise a round-trip using **one of** the two options below, then tear down the container. Record which option was used in the Phase 4 run log entry (`Docs/superpowers/runlog/2026-05-21-phase-4.md`).
     - **Option A — targeted (faster)**: downgrade to the recorded pre-merge head (`LAST_PRE_MERGE_HEAD`, captured from `alembic heads` against `origin/main` and `origin/pr/s2-4a-...` before the integration merge), then re-`upgrade head`. Exercises only the migrations introduced by the integration; failures point cleanly at the new chain.
       ```bash
       export LAST_PRE_MERGE_HEAD=<alembic_revision>   # e.g. 20260513_0006 or 20260518_0001 — the alembic revision identifier of the side you want to bounce against (NOT a git commit SHA)
       PYTHONPATH=backend python -m alembic downgrade "$LAST_PRE_MERGE_HEAD"
       PYTHONPATH=backend python -m alembic upgrade head
       ```
     - **Option B — full round-trip (more thorough, slower)**: downgrade to `base` then re-`upgrade head`. Exercises every `downgrade()` in the chain (including the older `20260510_*` / `20260513_*` migrations) at the cost of additional time. Use this when integrating a stack that touches multiple migration branches and you want maximum confidence.
       ```bash
       PYTHONPATH=backend python -m alembic downgrade base
       PYTHONPATH=backend python -m alembic upgrade head
       ```
   - SQLite fallback only if PG unavailable: same commands against `sqlite:///./.pytest-tmp/integration.sqlite`. If fallback used, document the contract gap in the run log (SQLite does not enforce all PG constraints used by this repo, especially partial indexes and `RESTRICT` FKs).
4. **Inspect the generated merge migration file** (the file produced by `alembic merge` in the "Alembic migrations" section above; lives at `backend/ums_smart_revenue/db/alembic/versions/<timestamp>_merge_*.py`). Confirm both `upgrade()` and `downgrade()` contain only `pass` (or are empty). If either function contains any schema operation (`op.create_table`, `op.add_column`, `op.alter_column`, `op.drop_*`, etc.), **STOP and CHECKPOINT** — incompatible schema changes between branches detected; the merge is unsafe without hand-resolution.
5. `cd frontend && npm install && npm run build` — must succeed. (`cd` required because `package.json` lives in `frontend/`.)
6. `git diff --check` — must show no whitespace issues.

If any gate fails, STOP. Surface the failure to user. Do not push until resolved or explicitly accepted with documented mitigation.

---

## Merge type

**`gh pr merge <N> --merge` — required.** NOT `--squash`, NOT `--rebase`. Rationale:

- Squash collapses ~74 commits' per-PR provenance into one opaque commit. The S2 stack's review trail (CodeRabbit/Codex feedback, fix commits, test additions) becomes unsearchable.
- Rebase rewrites 74 SHAs; any external references (PR comments, run logs, the prunable worktrees) to those SHAs become invalid.
- Merge preserves both: main gets one merge commit (the rollback anchor) AND every original stack commit is reachable from main.

---

## Rollback procedure

The integration merge commit on `main` (recorded as `INTEGRATION_MERGE_SHA` in the run log) is the **single rollback anchor**.

1. Branch off origin/main: `git checkout -b revert/s2-integration origin/main`
2. `git revert -m 1 <INTEGRATION_MERGE_SHA>` — `-m 1` because main is the first parent of the merge commit
3. Push: `git push -u origin revert/s2-integration`
4. Open revert PR: `gh pr create --base main --head revert/s2-integration --title "Revert S2 integration" --body "Reverts merge commit <SHA>. See run log Docs/superpowers/runlog/2026-05-21-phase-4.md."`
5. Merge revert PR with `gh pr merge --merge`
6. Alembic rollback if needed: against disposable PG, `PYTHONPATH=backend python -m alembic downgrade "$LAST_PRE_MERGE_HEAD"`. `LAST_PRE_MERGE_HEAD` is the pre-integration head captured during the validation gate (e.g., `20260513_0006` or `20260518_0001`) — see the Phase 4 run log entry. The merge migration is no-op and reversible.

Net effect: main returns to pre-integration state in one PR. No data loss (integration is code-only; no production data migration runs).

---

## Post-merge

- `git fetch origin --prune && git rev-parse origin/main` → record the new main SHA as `INTEGRATION_MERGE_SHA` (rollback anchor).
- Confirm single Alembic head: `PYTHONPATH=backend python -m alembic heads` returns exactly one head, and the returned revision **equals the `revision = '...'` value inside the generated merge migration file** (look in `backend/ums_smart_revenue/db/alembic/versions/` for the file produced by `alembic merge` — its filename will include `_merge_` and its `revision` is what `alembic heads` must report). This must **not** match any of the original pre-merge heads (`20260513_0006`, `20260516_0001`, `20260518_0001`). For determinism, the Phase 4 merge command may be run as `PYTHONPATH=backend python -m alembic merge --rev-id <chosen_id> -m "..." <head_a> <head_b>` and `<chosen_id>` recorded in the run log alongside `INTEGRATION_MERGE_SHA`.
- Verify pytest count on main matches integration-branch result.
- Phase 5 (frontend tenant header) starts from `INTEGRATION_MERGE_SHA`.
- Phase 6 (planning docs reconcile) starts from `INTEGRATION_MERGE_SHA`.
