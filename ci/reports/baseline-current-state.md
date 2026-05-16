# CI Gate Baseline Report

Generated: 2026-05-04T20:31:53Z
Branch: ci-gate-loopaudit-pr
Commit: 54dbd48f5e4c1ea78c47d9acf1d0f8131c2b7795

## Syntax Validation

All core scripts pass `bash -n`:

- ci/lib/changeset.sh: OK
- ci/lib/cache.sh: OK
- ci/lib/affected.sh: OK
- ci/preflight.sh: OK
- ci/checks/node.sh: OK
- ci/checks/python.sh: OK
- ci/checks/security.sh: OK
- ci/checks/debt.sh: OK
- ci/ship.sh: OK
- ci/lib/runner.sh: OK
- ci/checks/tests.sh: OK

## Known Debt Status

- `ci/debt/known-failures.yml`: empty list (no known debt)

## Unwired Subsystems

The following subsystems are implemented but not yet wired into `preflight.sh`:

1. **Cache (`ci/lib/cache.sh`)**: `ci::cache::init`, `ci::cache::key`, `ci::cache::get`, `ci::cache::put` exist but `run_check()` does not use them.
2. **Gate config (`ci/config/gate.yml`)**: Contains parallelism, timeouts, budgets, cache_enabled, incremental flags — never parsed by preflight.
3. **Checks config (`ci/config/checks.yml`)**: Per-check enable/disable toggles — not respected by `_check_should_skip`.
4. **Affected tests (`ci/lib/affected.sh`)**: `map_file_to_tests` and `get_affected_tests` exist but `ci/checks/tests.sh` never calls them.
5. **Runner timeout (`ci/lib/runner.sh`)**: `timeout_sec` is ignored in `ci::runner::submit`.
6. **Node/Python install caching**: `node.sh` and `python.sh` reinstall on every invocation.

## Reported vs Actual State

The `CI_GATE_FIX_PLAN.md` describes critical syntax corruption (truncated functions, garbage text, broken XML). Upon inspection, these issues appear to already be resolved in the working tree. The remaining work is wiring dead subsystems and adding elite features per Phases 2–3 of the fix plan.
