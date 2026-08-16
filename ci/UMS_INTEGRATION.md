# UMS Smart Revenue — Elite-CI integration notes

This repository **vendors [Elite-CI](https://github.com/XGenerationy/Elite-CI)** as a local-first quality gate. The standard Elite-CI surface is preserved (`make verify`, `make ci-full`, `make install-hooks`, git hooks under `.githooks/`); this document captures only the deltas that make it work for the UMS Python stack.

## Why local-first

The default GitHub-hosted runners on `XGenerationy/Youtube` are billing-blocked. Rather than build a workflow that can never run, we gate quality at the developer's machine via `make verify` and the git pre-push hook.

## What's enabled / disabled

Edited in [`ci/config/checks.yml`](config/checks.yml):

| Lane | State | Why |
|---|---|---|
| **lint-shell** | enabled | bash scripts in `ci/` and `.githooks/` |
| **lint-python** | enabled | `ruff` over `backend/` and `tests/` |
| **lint-yaml** | enabled | `.github/dependabot.yml`, `ci/config/*.yml` |
| **lint-actions** | enabled | Future workflows |
| **lint-markdown** | enabled (advisory) | The 19 design docs |
| **typecheck-python** | enabled | `mypy` over `backend/ums_smart_revenue` |
| **format-shell** | enabled | `shfmt` over `ci/` |
| **format-python** | enabled | `ruff format --check` |
| **tests-python** | enabled | `pytest` via the project's pyproject `[tool.pytest.ini_options]` |
| **sast** / **secrets** / **supply-chain** / **license** | enabled | bandit / gitleaks / pip-audit / license-checker |
| **commit-hygiene** / **branch-protection** | enabled | conventional commits, no direct main pushes |
| **typecheck-js** | enabled | `tsc --noEmit` in `frontend/`, via the `node` lane |
| **tests-js** | enabled | `vitest run` in `frontend/`, via the `node` lane |
| **lint-js / format-js** | **disabled** | `frontend/` has no eslint or prettier config, and no `lint` / `format:check` script |
| **lint-go / typecheck-go / format-go / tests-go** | **disabled** | No Go in the project |
| **lint-rust / typecheck-rust / format-rust / tests-rust** | **disabled** | No Rust in the project |
| **container** | **disabled** | Re-enable once Dockerfile lint (`hadolint`) is wanted in the gate |
| **iac** | **disabled** | Re-enable once Helm/Terraform land in Phase S2 |

Re-enable any of the disabled lanes by flipping `enabled: true` in `ci/config/checks.yml`.

These toggles are coarser than one row per check suggests. `ci/preflight.sh`
schedules the coarse `node` and `python` lanes, and skips a lane only when
*every* related check is disabled — so `tests-js: true` alone is enough to make
the whole `node` lane run, and `lint-js: false` does not stop it. Within the
lane, `ci/checks/node.sh` runs whichever of `format:check`, `lint`, `typecheck`,
`test`, `test:unit` and `build` the workspace's `package.json` defines, and
prints `Skipping missing script:` for the rest.

## How tools resolve

`make verify` invokes preflight via `uv run bash` whenever `uv` is on PATH. `uv run` activates the project's managed venv (`.venv/Scripts` on Windows or `.venv/bin` on Linux/macOS) and ensures every tool — `ruff`, `mypy`, `pytest`, `sqlfluff`, `alembic`, `bandit`, `pip-audit` — is on PATH for the duration of the gate. No manual venv activation required.

When `uv` is not installed, preflight falls back to bare `bash`. Elite-CI's Python lane then searches `.venv/bin/<tool>` first, then global PATH.

## Day-to-day workflow

```bash
# Once, after cloning:
uv sync --extra test --extra dev --extra lint   # provision the venv
make install-hooks                              # wire git hooks

# Before each push:
make verify                                     # full gate — runtime depends on what you changed
```

Pre-push hook runs `make ci-full` automatically. If anything fails, the push is refused before it leaves your machine.

### How long `make verify` takes

Under two minutes for an ordinary Python or frontend changeset, and that is what
it was measured at.

It is much longer when your changeset includes a shell script, because that
schedules `tests-shell` — the bats suites under `ci/tests/`, which drive
preflight, the node lane and the layout guard against synthetic trees and spawn
a package manager or init a repository in many of them. That is roughly 540
cases and around half an hour; `ci/config/checks.yml` gives the lane a
3600-second timeout for exactly that reason, because at the 20-minute gate
default it was being killed and reported as broken infrastructure. A run that
has been sitting in `tests-shell` for twenty minutes is not hung.

The gate's own scripts are all shell, so "I edited something under `ci/`" and
"this run will take half an hour" are the same statement. `make ci-quick` is the
fast pre-commit pass; the long lane belongs to the pre-push gate.

That lane needs **bats**, and it is the one prerequisite `uv sync` does not
install. It is a blocker on purpose — a lane that reports PASS without running
those suites leaves the layout, node and changeset gates unguarded — so on a
machine without bats every push touching `ci/` is refused with `FAIL_INFRA`
rather than passing quietly. Provision a pinned copy into the worktree:

```
make bats-install
```

It installs under `.ci-gate/bats/` (already git-ignored): no sudo, nothing
outside the repository, and `rm -rf .ci-gate/bats` undoes it. A `bats` already on
`PATH` — from a platform package, or `npm i -g bats` — is used in preference.
`ci/install-hooks.sh` says the same thing at the moment it makes the hook live.

## When the gate fails

| Mode | Use when |
|---|---|
| `make ci-quick` | Pre-commit smoke. Fastest pass. |
| `make ci-full` | Default. Full check suite incremental to your changeset. |
| `make ci-all` | Ignore changeset filtering; check the entire tree. |
| `make ci-fix` | Auto-format with ruff/shfmt where possible. |
| `make ci-profile` | Show timing per check. |
| `make ci-ship` | Tightest gate; same as the pre-push hook uses. |

## When billing for GitHub-hosted Actions is restored

If hosted runners become usable again, add a thin mirror workflow at `.github/workflows/elite-ci-mirror.yml` that runs `make verify` on `ubuntu-latest`. The local gate stays the source of truth; the workflow exists only to catch contributors who skipped hooks.

## Upgrading vendored Elite-CI

Re-fetch from upstream and merge:

```bash
git clone --depth=1 https://github.com/XGenerationy/Elite-CI.git /tmp/elite-ci
# Diff and merge selectively:
diff -ur ci /tmp/elite-ci/ci
# Hand-merge using your tool of choice. Preserve:
#   - ci/config/checks.yml (UMS lane toggles)
#   - ci/UMS_INTEGRATION.md (this file)
#   - ci/.gitignore (artifact rules)
```

The vendored copy is intentionally a checkpoint, not a live submodule, so UMS can pin a tested Elite-CI version and roll forward deliberately.

## Tested on

- Windows 11 + Git Bash + `uv` 0.11 (developer station).
- Linux containers — covered when the mirror workflow is later enabled.
