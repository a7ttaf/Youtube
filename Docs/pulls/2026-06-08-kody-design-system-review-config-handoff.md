# Kody Design System Review Config Handoff

Date: 2026-06-08
Branch: `codex/kody-design-system-review-config`

## Scope

Add repository-scoped Kodus centralized-config files for the UMS GitHub
repository `Youtube`, using the Kody centralized-config layout documented by
Kodus:
`https://docs.kodus.io/how_to_use/en/code_review/configs/centralized_config#repository-layout`.

The PR converts the supplied UMS Revenue Design System pack into durable Kody
review guidance instead of committing duplicate generated bundles, fonts, and
prototype assets that already exist in this repository.

## Non-goals

- No backend, API, auth, finance, database, Alembic, or Neo4j behavior changes.
- No frontend production component changes.
- No generated design-system bundle or font duplication.
- No Kodus manual sync. Sync would overwrite current Kodus database-backed
  configuration and requires explicit operator control.

## Files Added

- `Docs/kody/ums-revenue-design-system.md`
- `Youtube/kodus-config.yml`
- `Youtube/.kody-rules/review/ums-revenue-design-system.yml`
- `Youtube/.kody-rules/review/ums-revenue-finance-ui-contract.yml`
- `Docs/pulls/2026-06-08-kody-design-system-review-config-handoff.md`

## Behavior

When Kodus centralized config is enabled with a source repository containing
this layout, the `Youtube/` folder scopes review settings and Kody rules to the
UMS repository named `Youtube`.

The rules focus Kody review on:

- Soft Dark UMS design-system consistency.
- Money, confidence, permission, lock, export, and explainability contracts.
- Frontend drift that could hide restricted values, unresolved blockers, or
  source-of-truth boundaries.

## Validation Plan

Required local validation for this docs/config-only change:

- `git diff --cached --check` before commit.
- YAML parse validation for added Kodus config and Kody rule files.
- Final diff review to confirm no unrelated user-owned edits are included.

Full backend pytest, Ruff, frontend Vitest, and TypeScript gates are not
required for this docs/config-only PR because runtime code is unchanged.

## Risks And Follow-up

- Kodus CLI team-key authentication is required to run
  `kodus config centralized status`, `kodus config remote list`, or centralized
  sync commands. Local user-token auth is valid, but team-key auth was not
  configured during authoring.
- The folder name `Youtube/` is based on the GitHub repository name
  `XGenerationy/Youtube`. If Kodus displays this repository under a different
  configured name, rename the top-level scope folder before syncing.
- After merge, an operator with team-key auth should enable or verify
  centralized config and let the normal Kodus merge-triggered sync apply the
  reviewed files.

## Rollback

Revert this PR to remove the centralized Kody config and rule files. No data,
runtime services, migrations, or production assets are changed.
