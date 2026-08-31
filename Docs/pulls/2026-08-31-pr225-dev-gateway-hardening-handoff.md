# PR #225 - Development Gateway Hardening - Delivery Handoff

## Scope

This isolated branch hardens the Vite development gateway and its operator
contracts. It does not add a production gateway, connector credential lifecycle,
durable artifact storage, Helm deployment, or External Secrets Operator support.

The load-bearing implementation is `frontend/devProxy.ts` plus
`frontend/vite.config.ts`. The tests retain all local defenses: development-serve
only activation, explicit preview exclusion, loopback binding, exact route
segments, iterative encoded-path rejection, same-origin/Fetch Metadata checks,
broad gateway-header family scrubbing, blank-token startup refusal, and HTTPS plus
exact trust for non-loopback backends. Vite's post-merge resolved host is checked
again so an inline or CLI host override cannot expose the trusted proxy on a
wildcard or non-loopback listener.

The exact implementation-and-test snapshot validated below is
`7a1c4ec7da7d576e359b15226b2ccd6a8b4b3261`. The subsequent documentation-only
evidence commit changes no runtime or test file.

## Integration dependency and re-author requirement

Do **not** combine this branch blindly with PR #221 or PR #224. Both PR heads can
move while review fixes are force-pushed, so this handoff deliberately does not
pin either integration dependency to a stale SHA. The README, Compose, security,
and operator runbook text on this branch began from the pre-storage PR #225 line
and can auto-merge into assertions that are false after those changes.

The final PR #225 must be re-authored after PR #221 and the final PR #224 land:

1. Start from their resulting mainline, not from a textual auto-merge.
2. Reapply the gateway implementation and adversarial tests by behavior.
3. Reconcile `README.md`, `SECURITY.md`, `docker-compose.yml`, `.env.example`,
   `frontend/README.md`, and `Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md` against
   the durable storage mounts, current settings, and actual Compose environment.
4. Preserve immutable numeric Secret Manager references and the audited order:
   credential registration first, service-actor provisioning afterward.
5. Re-run the full validation gate at the newly authored head before any push.

## Non-goals and rollback

- No database or Alembic changes.
- No production header injection.
- No permission widening.
- No finance, audit-row, export, or connector runtime changes.

Rollback is a code/docs revert. No migration, reset, reseed, or backfill is
required.

## Validation

Validation evidence must name the final committed head. At this isolated
completion snapshot (`7a1c4ec7da7d576e359b15226b2ccd6a8b4b3261`):

| Gate | Result |
|---|---|
| `bun run test --run tests/devProxyRoutes.test.ts tests/devProxySecurity.test.ts` | 112 passed |
| `bun run test --run` | 642 passed across 46 files; existing React/jsdom warnings only |
| `bun run typecheck` | Passed |
| `bun run build` | Passed |
| `uv run pytest -q tests/api/test_org_units_api.py -k "missing_gateway_token or invalid_gateway_token or unknown_gateway_role"` | 3 passed, 6 deselected |
| `uv run ruff check tests/api/test_org_units_api.py` | Passed |
| `uv run pytest -q -x` | Environment blocker after 122 passed: `RuntimeError: UMS_TEST_DATABASE_URL required for PostgreSQL migration round-trip tests`; rerun with a disposable PostgreSQL test URL |
| `git diff --check` and `git diff --cached --check` | Passed on the exact staged implementation bytes; `git diff --check HEAD^..HEAD` passed after commit |
