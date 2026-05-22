# PR #39 — Commit Soft-Dark Mockup Variant + OFL Fonts — Handoff

**Date:** 2026-05-22
**PR:** https://github.com/XGenerationy/Youtube/pull/39
**Branch:** `pr/mockups-soft-dark-and-fonts`
**Base:** `main` at `94e99d1` (PR #38 merge commit)
**Status at handoff:** Local validation gate green (808 passed in 91.63s
after a `.gitattributes` whitelist for OFL-1.1 license text); commit
pending; push pending operator authorization.

## Scope

Design-asset catch-up. Companion to PR #38 (infrastructure catch-up).
Commits the soft-dark mockup variant and its OFL-licensed font stack
that have been in the working tree but untracked.

Concrete catch-up:

- `mockups/ums-smart-revenue-command-center-soft-dark.html` (single-file mockup).
- `mockups/qa/ums-command-center-soft-dark-*.png` (9 QA screenshots — desktop, mobile, restricted, registry, close, graph, exports, connectors, audit).
- `mockups/qa/generate-screenshots-soft-dark.py` (Playwright runner; parallel to the canonical `generate-screenshots.py`).
- `mockups/FontsGH/` (3 woff2 fonts + 3 LICENSE-*.txt + README.md).
- `Docs/09_SMART_DASHBOARD_UI.md` — new `## Visual reference` section pointing at both mockups.
- `Docs/01_IMPLEMENTATION_PLAN.md` + `Docs/15_DELIVERY_BACKLOG.md` — per-PR inline marks per the `feedback-per-pr-plan-status` rule.

## Non-goals

- No source behavior change. No `backend/`, `frontend/src/`, `tests/`, or `alembic/` file is touched.
- No new feature. No tenant scoping change. No auth change. No finance change.
- No CI workflow change.
- Not bundled with Spec A (frontend `X-UMS-Tenant` header). That's the next PR after #39 merges.
- Not a redesign. The canonical `DESIGN.md` typography stack
  (`mockups/FontsPP/` — Anthropic Sans / Serif / Mono) remains
  authoritative. The soft-dark variant is a redistributable sibling.

## Files changed

25 changed files in the staged diff for this PR:

| Category | Files | Approx size |
|---|---|---|
| Mockup HTML | 1 | ~115 KB (2,973 lines) |
| QA screenshots (PNG) | 9 | ~2.3 MB total |
| QA generator script | 1 | ~1.6 KB (42 lines) |
| Fonts + licenses + README | 7 (3 woff2 + 3 license + 1 readme) | ~340 KB total |
| Planning docs | 3 (`Docs/09`, `Docs/01`, `Docs/15`) | +39 lines |
| Pulls/ triple | 3 (report + changelog + handoff) | ~410 lines |
| `.gitattributes` | 1 | +7 lines (OFL license whitelist) |

Total binary additions: ~2.65 MB. Total non-vendored author-written text: ~3,500 lines (HTML mockup dominates).

## Files explicitly NOT in this PR

- `frontend/package-lock.json` — working-tree modification preserved per standing operator instruction.
- `nul` — Windows shell artifact in working tree; will NOT be committed.
- All `backend/`, `frontend/src/`, `tests/`, `alembic/` files — none touched.

## Behavior changes

- **At runtime: none.** No production code path is changed.
- **Design system: unchanged.** `DESIGN.md` continues to reference only the canonical Anthropic font stack.
- **Pytest count: unchanged** from PR #38 baseline.

## Tests run

Final local validation:

- `python scripts/run_validation_gate.py` — passed. `808 passed in 91.63s`. Ruff clean, AST policy clean, working-tree and staged whitespace clean.
- Initial gate run failed at step 5 (`git diff --cached --check`) on trailing whitespace inside the 3 OFL-1.1 LICENSE-*.txt files. OFL-1.1 requires the license body to be preserved verbatim, so the fix is a `.gitattributes` whitelist (`mockups/FontsGH/LICENSE-*.txt -whitespace`), not editing the license text. Re-ran gate after the change: green.

## Failures / skipped gates

None remaining. The remote merge gate remains blocked until GitHub review threads and checks clear.

## Risks

- **Code risk: zero.** No runtime code path changes. The added files are static assets.
- **Test-flake risk: zero.** No tests added or modified.
- **Repo-size risk: low.** ~2.65 MB added; negligible against current repo footprint.
- **License-compliance risk: zero.** OFL-1.1 explicitly permits the bundling; full license body + Reserved Font Name notices preserved verbatim alongside the binaries.
- **Reviewer-flow risk: low.** Diff is ~50 lines of planning-doc additions plus ~400 lines of pulls/ markdown plus binary additions. Binaries are reviewable as filenames; the OFL license text is standard boilerplate that reviewers do not need to read line-by-line.
- **Backward compat risk: zero.** Purely additive; no existing file's behavior changes.

## Rollback / operational notes

- Revert is `git revert <merge-commit>` — restores prior repo-on-disk-but-untracked state with no data loss.
- No data, schema, runtime state, or downstream consumer migration needed.
- *No graph projection impact detected.*

## Next session / next PR recommendations

After PR #39 merges:

1. **Spec A** — Frontend `X-UMS-Tenant` header (closes S2 spec Phase 5).
   Brainstorm → write spec → write plan → implement → PR → merge.
2. **Optional** — Write per-role / per-section narrative subsections in
   `Docs/09_SMART_DASHBOARD_UI.md` citing the matching `*-soft-dark-*.png`
   (or canonical) image as the implementation target. Out of scope for
   this PR; would be a `chore(docs)` PR after Spec A is done.
3. **Deferred from PR #38** — `chore(repo): git worktree prune` to clean
   up the 15 prunable worktrees from the HD-loss event recorded in
   `Docs/superpowers/runlog/2026-05-21-phase-0.md`.

## Open questions / decisions deferred

- None. The two-PR split (#38 infra + #39 mockups) was decided in the
  2026-05-22 audit and recorded in PR #38's handoff.

## Validation a future maintainer can rerun

```bash
# From repository root on branch pr/mockups-soft-dark-and-fonts:
git checkout pr/mockups-soft-dark-and-fonts

# Full local gate via committed entry point (PR #38's validation gate):
python scripts/run_validation_gate.py

# Regenerate soft-dark QA screenshots (requires playwright + browser install):
python mockups/qa/generate-screenshots-soft-dark.py

# Or just open the mockup directly in a browser:
#   file:///<repo>/mockups/ums-smart-revenue-command-center-soft-dark.html
```

Rerun target: validation gate green, pytest count unchanged from PR #38 baseline.
