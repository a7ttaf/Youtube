# PR #39 — Commit Soft-Dark Mockup Variant + OFL Fonts — Changelog

**Date:** 2026-05-22
**PR:** https://github.com/XGenerationy/Youtube/pull/39
**Branch:** `pr/mockups-soft-dark-and-fonts`
**Base:** `main` at `94e99d1`

## Added

### Mockup HTML

- `mockups/ums-smart-revenue-command-center-soft-dark.html` — single-file mockup, ~115 KB. Soft-dark sibling of `mockups/ums-smart-revenue-command-center.html`. Same product surfaces, same role-restricted views; uses Mona Sans / Monaspace Neon / Newsreader instead of Anthropic Sans / Mono / Serif.

### QA screenshots (PNG, captured by Playwright)

- `mockups/qa/ums-command-center-soft-dark-desktop.png` — 1440×980 desktop landing.
- `mockups/qa/ums-command-center-soft-dark-mobile.png` — 390×900 full-page mobile.
- `mockups/qa/ums-command-center-soft-dark-restricted.png` — desktop, `roleSelect=assistant` permission-restricted view.
- `mockups/qa/ums-command-center-soft-dark-registry.png` — channel registry section.
- `mockups/qa/ums-command-center-soft-dark-close.png` — monthly close section.
- `mockups/qa/ums-command-center-soft-dark-graph.png` — graph/read-model preview section.
- `mockups/qa/ums-command-center-soft-dark-exports.png` — export center section.
- `mockups/qa/ums-command-center-soft-dark-connectors.png` — connectors section.
- `mockups/qa/ums-command-center-soft-dark-audit.png` — audit section.

### Generator script

- `mockups/qa/generate-screenshots-soft-dark.py` — Playwright runner that mirrors `generate-screenshots.py`. Same `roleSelect` + page-hash drive contract. Loads `../ums-smart-revenue-command-center-soft-dark.html`, writes the 9 PNGs above.

### Fonts (OFL-1.1)

- `mockups/FontsGH/MonaSans-VF.woff2` — Mona Sans variable font (`github.com/github/mona-sans`).
- `mockups/FontsGH/MonaspaceNeon-VF.woff2` — Monaspace Neon variable font (`github.com/githubnext/monaspace`).
- `mockups/FontsGH/Newsreader-VF.woff2` — Newsreader variable font (`github.com/productiontype/Newsreader`).
- `mockups/FontsGH/LICENSE-MonaSans.txt` — Full SIL OFL 1.1 license body + RFN notice.
- `mockups/FontsGH/LICENSE-Monaspace.txt` — Full SIL OFL 1.1 license body + RFN notice.
- `mockups/FontsGH/LICENSE-Newsreader.txt` — Full SIL OFL 1.1 license body.
- `mockups/FontsGH/README.md` — Family/source/license table + OFL-1.1 plain-language summary + sibling-folder pointer to `mockups/FontsPP/`.

### Per-PR documentation

- `Docs/pulls/2026-05-22-pr39-mockups-soft-dark-and-fonts-report.md` (this PR's report).
- `Docs/pulls/2026-05-22-pr39-mockups-soft-dark-and-fonts-changelog.md` (this file).
- `Docs/pulls/2026-05-22-pr39-mockups-soft-dark-and-fonts-handoff.md`.

## Changed

### Planning docs

- `Docs/09_SMART_DASHBOARD_UI.md` — New `## Visual reference` section at end (after `## User experience rules`). Tabulates the two mockups (canonical vs soft-dark) with stack + license posture; points at QA screenshots and the two generator scripts; states the canonical mockup remains authoritative per `DESIGN.md` while the soft-dark variant is a redistributable sibling.
- `Docs/01_IMPLEMENTATION_PLAN.md` — One new bullet appended to the existing `### S0/S1 catch-up (2026-05-22)` subsection: `✅ PR #39: Soft-dark mockup variant + OFL fonts …`.
- `Docs/15_DELIVERY_BACKLOG.md` — One new bullet appended to `Cross-cutting shipped`: `✅ Mockup catch-up: OFL-licensed soft-dark variant …`.

### Source semantics — none

No `backend/ums_smart_revenue/**` file is touched. No route, service, repository, ORM, migration, or schema is changed. No `frontend/src/**` file is touched.

### Lint / format — none

No Python source file in the validation gate's ruff target set (`backend tests scripts`) is touched. The new `mockups/qa/generate-screenshots-soft-dark.py` is parallel to the existing `mockups/qa/generate-screenshots.py` (also outside the ruff target set; both are local Playwright runners, not import-path modules).

### Symbol renames — none

### Enum migration — none

### Alembic — none

### SQL — none

### Tests — none

No test files added, modified, or removed. Pytest count unchanged.

## Removed

- Nothing removed.

## Behavior changes

- **Source semantics: none.**
- **Runtime: none.**
- **Design system: unchanged.** `DESIGN.md` continues to reference only `mockups/FontsPP/` (Anthropic) as the canonical typography stack. The soft-dark variant documents itself as a redistributable sibling, not a design pivot.
- **Pytest count: unchanged** from PR #38 baseline.

## Test surface change

- No test count delta.
- No new test file.
- No new test directory.

## Documentation changes

- 3 new artifacts under `Docs/pulls/` for this PR.
- 1 new section in `Docs/09_SMART_DASHBOARD_UI.md` (`## Visual reference`).
- 2 planning-doc bullets added (`Docs/01`, `Docs/15`).

## Schema / data

- No Prisma/Alembic migration. No DB column, index, constraint, enum, status, or JSON-shape change.

## Configuration / runtime

- `pyproject.toml` — unchanged.
- `alembic.ini` — unchanged.
- `Docker` / `docker-compose.yml` — unchanged.
- CI workflows — unchanged.
- `.gitignore` — unchanged.
- `.gitattributes` — +7 lines. Added a `mockups/FontsGH/LICENSE-*.txt -whitespace` block so the validation gate's `git diff --cached --check` does not flag verbatim OFL-1.1 license text (upstream contains a trailing space on one line; OFL-1.1 requires the license body to be preserved verbatim, so the rule must change, not the text). Mirrors the existing `.agents/** -whitespace` pattern from PR #38.

## Pattern compatibility

- Mirrors the existing `mockups/qa/generate-screenshots.py` shape for `generate-screenshots-soft-dark.py` (parallel script per mockup variant).
- Mirrors the existing `mockups/FontsPP/` folder shape for `mockups/FontsGH/` (per-family LICENSE + README + woff2 binaries).
- Mirrors the existing `Docs/pulls/2026-05-22-pr38-*` template structure for the report / changelog / handoff triple.
- Mirrors the existing `Docs/01_IMPLEMENTATION_PLAN.md` inline-mark convention (`✅ PR #N`).

## Compatibility with origin/main

- Purely additive on top of `main` at `94e99d1`. No file conflict.
- No backport / cherry-pick concern.
