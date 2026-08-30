# Required branch checks

Configure the `main` ruleset or branch-protection rule to require these stable
status checks before merge:

| Required check | Workflow | Contract |
| --- | --- | --- |
| `Lint and unit tests` | `ci-fast` | Locked Python toolchain, lint/type checks, no-skip policy, no-database pytest lane, frontend test-layout guard, and committed-range whitespace hygiene. |
| `Postgres migrations and authz` | `ci-database` | Alembic head plus the complete database and real-Postgres pytest lane. |
| `Build and Vitest` | `ci-frontend` | Locked Bun install, production build, and the complete Vitest suite. |

All three workflows intentionally run for every pull request targeting `main`
and every push to `main`; do not add workflow-level `paths` filters. A skipped
required workflow does not report its context and can strand a pull request.
Changed-scope optimization is safe only inside a reporting job and only when
the repository changeset contract fails closed.

Also enable:

- Require branches to be up to date before merging.
- Require pull-request review before merging.
- Do not allow required checks to be bypassed.

Workflow job names are the required-check API contract. Rename a job only in
the same change that updates the configured required context.

No repository setting is changed by these files; an administrator must apply
and verify the rule in GitHub after the workflows exist on the default branch.
