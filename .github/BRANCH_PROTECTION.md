# Required branch checks

After merging the `feat/required-ci` workflows, configure **`main`** branch
protection (GitHub → Settings → Branches → Branch protection rules) to require
these status checks before merge:

| Check name | Workflow |
| --- | --- |
| `Lint and unit tests` | `ci-fast` |
| `Postgres migrations and authz` | `ci-database` |
| `Build and Vitest` | `ci-frontend` |

Optional follow-ups (not blocking beta P0):

- `ci-compose.yml` — `docker compose config` smoke
- `ci-restore.yml` — nightly backup-restore rehearsal

Use **Require branches to be up to date before merging** so stale green checks
cannot merge.
