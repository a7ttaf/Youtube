# Local CI Gate (Mac-Local)

This CI system runs locally before commit/push and blocks unsafe updates.

## What It Does

- Runs a modular validation pipeline from `./ci/preflight.sh`.
- Supports run modes: `quick`, `full`, `ship`, `debt`.
- Generates advisory impact analysis from changed, staged, and untracked files.
- Generates an advisory smart test plan from impact rules and lane metadata.
- Writes run reports to:
  - `ci/reports/latest.md`
  - `ci/reports/latest.log`
  - `ci/reports/affected-areas.md`
  - `ci/reports/affected-areas.json`
  - `ci/reports/test-plan.md`
  - `ci/reports/test-plan.json`
- Blocks push through `.githooks/pre-push` when `--mode ship` fails.

## How To Run

```bash
make ci-quick
make ci-full
make ci-ship
make ci-debt
make impact
make test-plan
make smart
```

Direct command:

```bash
./ci/preflight.sh --mode full
```

Advisory impact and smart-plan commands:

```bash
./ci/impact.sh
./ci/test-plan.sh
./ci/impact.sh --base origin/main
./ci/test-plan.sh --base origin/main
```

`make smart` generates the advisory test plan and then runs the full preflight gate. It does not replace `make verify` or weaken the pre-push gate.

## Install Hooks

```bash
make install-hooks
```

This sets `core.hooksPath=.githooks` for this repository and enables pre-push validation.

## Ship After Tests Pass

```bash
make ship MESSAGE="your commit message"
```

or:

```bash
./ci/ship.sh "your commit message"
```

By default, `ship.sh` expects you to stage files intentionally first (`git add -p` or `git add <paths>`).

Dry-run preview:

```bash
./ci/ship.sh --dry-run "your commit message"
```

Auto-stage tracked changes (explicit opt-in):

```bash
./ci/ship.sh --auto-stage-tracked "your commit message"
```

Include untracked files intentionally (tracked + untracked):

```bash
./ci/ship.sh --include-untracked "your commit message"
```

## What Happens If Checks Fail

- The failing check returns a CI result state:
  - `FAIL_NEW_ISSUE`
  - `FAIL_INFRA`
- `preflight` exits non-zero.
- `ship` stops before commit/push.
- The reason is written to `ci/reports/latest.md` and `ci/reports/latest.log`.

## CI Result States

- `PASS`
- `PASS_WITH_KNOWN_DEBT`
- `FAIL_NEW_ISSUE`
- `FAIL_INFRA`

## Impact And Smart Test Plan

- Path rules live in `ci/config/path-rules.conf`.
- Lane metadata lives in `ci/config/lanes.conf`.
- Impact analysis writes affected areas, risk, selected lanes, and whether the full gate is required.
- The smart test plan explains required and skipped lanes.
- The current smart plan is advisory. It is intentionally not allowed to skip blocking checks during `verify`, `ship`, or pre-push.

## What Not To Commit

- `.env`, `.env.local`, `.env.production`
- private keys (`*.pem`, `*.key`, `*.p12`, `*.pfx`)
- `node_modules/`, `.venv/`
- build outputs (`dist/`, `build/`, coverage folders) unless explicitly intended

## Git Hook Safety

- Avoid using `git push --no-verify` (or other `--no-verify` flags).
- `--no-verify` bypasses local pre-push checks and should only be used with explicit CI-owner authorization.

## Artifact Handling

- CI artifacts and reports are kept in `ci/reports/`.
- If build artifacts are needed, place them in a clear folder and validate before commit.
- By default, the gate blocks staged build output paths.

## Known Debt Handling

- Registry file: `ci/debt/known-failures.yml`
- Debt check command: `make ci-debt`
- Debt can be tolerated only when unchanged and unexpired (`PASS_WITH_KNOWN_DEBT`).
- For strict ratchet comparison, each active entry should include:
  - `must_not_increase: true`
  - `expected_count: <baseline count>`

## Known Limitations

- Optional tools (`shellcheck`, `pip-audit`) are skipped when not installed.
- Checks are capability-based: missing project ecosystems are skipped instead of forced.
- Dedicated Go, Rust, Docker, and database runners are not implemented yet.
- Smart lane execution is not implemented yet; smart planning still hands off to the full gate.
