# PR #39 — Commit Soft-Dark Mockup Variant + OFL Fonts — Report

**Date:** 2026-05-22
**PR:** https://github.com/XGenerationy/Youtube/pull/39
**Branch:** `pr/mockups-soft-dark-and-fonts`
**Base:** `main` at `94e99d106b626272f2f224a6a7d8d60e4bce900a` (PR #38 merge commit)
**Status:** Catch-up PR — committing design-asset working-tree state that was
deferred from PR #38 by operator instruction so the two catch-up PRs review
independently.

## What was requested

After the 2026-05-22 audit (recorded in PR #38), the operator approved a
two-PR split for the working-tree catch-up:

- **PR #38** — Infrastructure (validation gate, devtools, agent rules,
  runlogs). Merged 2026-05-22 at `94e99d1`.
- **PR #39 (this PR)** — Design assets: the soft-dark mockup variant
  (HTML + 9 QA screenshots + Playwright generator) and the OFL-licensed
  fonts (`mockups/FontsGH/`) it depends on, plus a `Docs/09` reference
  pointing at both mockups as Phase 5 visual targets.

The soft-dark mockup is a sibling variant to the canonical Anthropic-
font mockup that already lives on `main`. It exists so the product
design can be shared and reviewed externally (clients, contractors,
recruiting demos) without bundling Anthropic's proprietary fonts.

## What was actually done

Purely additive design-asset catch-up plus three planning-doc updates
plus one `.gitattributes` rule extension (discovered by the validation
gate — OFL-1.1 license text must be preserved verbatim, so the rule
must whitelist the path rather than the text be edited). No source
code, no schema, no test, no runtime behavior change.

### Mockup assets

| File | Bytes | Role |
|---|---|---|
| `mockups/ums-smart-revenue-command-center-soft-dark.html` | 115,633 | Single-file mockup. Same product surfaces and role-restricted views as the canonical mockup; uses Mona Sans / Monaspace Neon / Newsreader instead of Anthropic Sans / Mono / Serif. |
| `mockups/qa/ums-command-center-soft-dark-desktop.png` | 326,059 | Desktop landing (1440×980). |
| `mockups/qa/ums-command-center-soft-dark-mobile.png` | 306,040 | Mobile (390×900, full-page). |
| `mockups/qa/ums-command-center-soft-dark-restricted.png` | 366,431 | Desktop, `roleSelect=assistant` (permission-restricted view). |
| `mockups/qa/ums-command-center-soft-dark-registry.png` | 231,276 | Channel registry section. |
| `mockups/qa/ums-command-center-soft-dark-close.png` | 219,422 | Monthly close section. |
| `mockups/qa/ums-command-center-soft-dark-graph.png` | 205,877 | Graph/read-model preview section. |
| `mockups/qa/ums-command-center-soft-dark-exports.png` | 195,900 | Export center section. |
| `mockups/qa/ums-command-center-soft-dark-connectors.png` | 206,823 | Connectors section. |
| `mockups/qa/ums-command-center-soft-dark-audit.png` | 218,466 | Audit section. |
| `mockups/qa/generate-screenshots-soft-dark.py` | ~1.6 KB | Playwright script. Mirrors `generate-screenshots.py`; same `roleSelect` + page-hash drive contract; reads `ums-smart-revenue-command-center-soft-dark.html` from the parent directory and writes the 9 PNGs above. |

### Fonts (OFL-1.1)

| File | Family | Source |
|---|---|---|
| `mockups/FontsGH/MonaSans-VF.woff2` | Mona Sans (variable: wdth, wght, opsz, ital) | `github.com/github/mona-sans` |
| `mockups/FontsGH/MonaspaceNeon-VF.woff2` | Monaspace Neon (variable) | `github.com/githubnext/monaspace` |
| `mockups/FontsGH/Newsreader-VF.woff2` | Newsreader (variable: opsz, wght) | `github.com/productiontype/Newsreader` |
| `mockups/FontsGH/LICENSE-MonaSans.txt` | — | Full SIL OFL 1.1 license body + Reserved Font Name notice. |
| `mockups/FontsGH/LICENSE-Monaspace.txt` | — | Full SIL OFL 1.1 license body + Reserved Font Name notice. |
| `mockups/FontsGH/LICENSE-Newsreader.txt` | — | Full SIL OFL 1.1 license body. |
| `mockups/FontsGH/README.md` | — | Family/source/license table + OFL-1.1 plain-language summary + sibling-folder pointer to `mockups/FontsPP/`. |

### Planning doc updates

| File | Change |
|---|---|
| `Docs/09_SMART_DASHBOARD_UI.md` | New `## Visual reference` section at end. Tabulates the two mockups (canonical vs soft-dark), points at the QA screenshots and the two generator scripts, and notes that the canonical mockup stays authoritative per `DESIGN.md` — the soft-dark variant is a redistributable sibling, not a new design direction. |
| `Docs/01_IMPLEMENTATION_PLAN.md` | One new bullet in the existing `### S0/S1 catch-up (2026-05-22)` subsection: `✅ PR #39: Soft-dark mockup variant + OFL fonts …`. |
| `Docs/15_DELIVERY_BACKLOG.md` | One new bullet in `Cross-cutting shipped`: `✅ Mockup catch-up: OFL-licensed soft-dark variant …`. |

### Docs/pulls/ triple (this PR's documentation)

| File | Role |
|---|---|
| `Docs/pulls/2026-05-22-pr39-mockups-soft-dark-and-fonts-report.md` | This file. |
| `Docs/pulls/2026-05-22-pr39-mockups-soft-dark-and-fonts-changelog.md` | Added / changed / removed breakdown. |
| `Docs/pulls/2026-05-22-pr39-mockups-soft-dark-and-fonts-handoff.md` | Risk + rollback + next-session note. |

## Phased execution

| Phase | Action | Notes |
|---|---|---|
| Sync | After PR #38 merge, `git checkout main && git pull --ff-only` brought local main to `94e99d1` | Confirmed via `gh pr view 38 --json state,mergedAt,mergeCommit` returning `MERGED` |
| Branch | `git checkout -b pr/mockups-soft-dark-and-fonts` | Working tree carries only the 11 mockup-related untracked items + `frontend/package-lock.json` (intentional standing exclusion) + Windows `nul` artifact (will not be committed) |
| Inspect | Read `mockups/FontsGH/README.md`, `generate-screenshots-soft-dark.py`, `Docs/09`, `DESIGN.md`, `mockups/DESIGN.md` | Confirmed FontsGH README is self-contained; confirmed `DESIGN.md` references only `FontsPP/` (canonical) so soft-dark variant doesn't need to alter the canonical design system |
| Plan docs | Add `## Visual reference` section to `Docs/09`; append PR #39 bullet to `Docs/01` S0/S1 catch-up; append PR #39 bullet to `Docs/15` Cross-cutting shipped | Per `feedback-per-pr-plan-status` rule |
| Pulls/ | Write the three `Docs/pulls/2026-05-22-pr39-*` artifacts | Mirrors PR #38 template |
| Stage | `git add` only the 11 mockup files + the 4 doc files + the 3 pulls/ files (no `git add -A`, no `frontend/package-lock.json`, no `nul`) | Verified via `git status --short` after staging |
| Validate | `python scripts/run_validation_gate.py` | Must pass before push |
| Commit | Single commit. Push pending operator authorization. | Per standing constraint "Pause before every push/merge" |

## Quality checks performed

- `python scripts/run_validation_gate.py` — passed; 808 passed in 91.63s; ruff clean; AST policy clean; `git diff --check` and `git diff --cached --check` clean (after the `.gitattributes` whitelist for OFL license text).
- Initial gate run failed at step 5 on trailing whitespace inside the 3 vendored OFL-1.1 LICENSE-*.txt files. The OFL-1.1 license body is required to be preserved verbatim and contains a trailing-space line upstream; the correct fix is to extend `.gitattributes` (the same pattern PR #38 used for `.agents/**`), not to edit the license text. Re-ran gate after the `.gitattributes` change: green.
- Static asset sanity: HTML opens locally in browser; 9 PNGs render; 3 woff2 files are valid (binaries Git tracks as `Bin 0 -> NNNN bytes`).

## Architecture & quality posture

- **No production source semantics change.** Zero changes under `backend/`, `frontend/src/`, `tests/`, or `alembic/`.
- **No tenant scoping change.** No authorization, audit, finance, or
  graph (Neo4j retired) impact.
- **Design system unchanged.** `DESIGN.md` continues to reference
  `FontsPP/` (Anthropic) as the canonical stack. The soft-dark mockup
  documents itself as a redistributable sibling, not a design pivot.
- **Licensing posture explicit.** OFL-1.1 license text is committed
  alongside the woff2 binaries with Reserved Font Name notices preserved.

## Blast-radius statement

*No graph projection impact detected.* No SQLAlchemy ORM, no Alembic
migration, no route, no service, no repository, no schema change, no
authorization or finance-number behavior change. The PR adds:

- 1 mockup HTML file (`mockups/`) — visual asset only, not imported by runtime.
- 9 PNG screenshots (`mockups/qa/`) — visual assets only.
- 1 Playwright generator script (`mockups/qa/`) — local QA tooling only.
- 3 woff2 font files + 3 license notices + 1 README (`mockups/FontsGH/`) — referenced only by the soft-dark mockup HTML.
- 3 planning-doc updates (markdown only).
- 3 `Docs/pulls/` files (markdown only).
- 1 `.gitattributes` extension (+7 lines): whitelist the verbatim OFL-1.1 LICENSE-*.txt paths from `git diff --check` so the validation gate accepts upstream license text. Mirrors PR #38's `.agents/** -whitespace` pattern.

Total: 25 files in the staged diff.

## Pre-existing baseline

- Pytest result: 808 passed (unchanged-by-this-PR; PR #38 final landed at this count after its review-loop policy-test additions).
- Alembic single head unchanged: `20260521_0001`.
- Ruff: no Python source files added under `backend/`, `tests/`, or `scripts/`. `mockups/qa/generate-screenshots-soft-dark.py` is outside the gate's ruff target set and is a Playwright runner not on the import path.

## Validation that could NOT be run

None. All gates passed.

## Remaining risks

- **Code risk: zero.** No runtime code path changes. The added files are static assets.
- **Repo-size risk: low.** ~2.3 MB of PNG + ~330 KB of woff2 + ~2 KB of script + ~115 KB of HTML ≈ 2.75 MB. Negligible against the repo's current Docs/+backend/ footprint.
- **License-compliance risk: zero.** OFL-1.1 explicitly permits this usage; the full license body + RFN notice for each family is preserved verbatim alongside the binaries.
- **Reviewer-flow risk: low.** Diff is ~10 lines of HTML/script visible plus ~30 lines of planning docs plus the binary additions. The binary files are reviewable as filenames; the OFL license text is standard boilerplate that reviewers do not need to read line-by-line.

## Follow-up recommendations

After PR #39 merges:

1. **Spec A** — Frontend `X-UMS-Tenant` header (closes S2 spec Phase 5).
   Continue with the brainstorming → spec → plan → implementation cycle
   (tasks #20–#23 in the session task list).
2. Eventually: write a `Docs/09_SMART_DASHBOARD_UI.md` section per major
   role view, citing the matching `mockups/qa/*-soft-dark-*.png` (or
   canonical) as the implementation target. Out of scope for this PR
   (would be a `chore(docs)` PR after Spec A is done).
3. Consider the deferred `chore(repo): git worktree prune` from PR #38
   `phase-0.md`.

## Rollback notes

Pure additive design-asset commit. Revert is `git revert <merge-commit>`
— restores the prior repo-on-disk-but-untracked state with no data
loss, no runtime impact.

## Open questions / decisions deferred

- None. The two-PR split (#38 infra + #39 mockups) was decided in the
  2026-05-22 audit and recorded in PR #38's handoff.
