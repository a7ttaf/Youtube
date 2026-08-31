# 22 — Database Backup, Clean Restore, and Rehearsal

This document is the P0-b database contract. It does one narrow job: produce a
snapshot-consistent PostgreSQL archive, prove its semantic contents, and restore
it into a clean database or a throwaway rehearsal container.

It does **not** back up export artifacts or connector blobs. The coordinated
outer bundle remains owned by `scripts/compose_storage.py` and
`Docs/20_COMPOSE_STORAGE_RUNBOOK.md`. P0-a seals the outer bundle and checks
that the nested database package is structurally present. P0-b alone parses
and verifies the database manifest and artifacts semantically.

## Safety boundary

The two scripts deliberately provide no retention, no prune command, no target
drop, and no `allow-nonempty` escape hatch.

- Backup output must be a dedicated host directory outside the repository.
- A dedicated root is initialized with `.ums-database-backup-root`; a populated
  unmarked directory is refused before its permissions can be changed. The
  explicit `--coordinated-bundle` mode instead requires an already-created
  owner-only P0-a bundle and never adopts or rewrites a broad directory.
- On Windows the output DACL is replaced with one inheritable Full Control rule
  for the current user's SID. On POSIX the directory is mode `0700`.
- One exclusive lock prevents overlapping publishers. A stale lock is never
  guessed away; inspect the prior process and partial run before removing it.
- A prior `.partial` run also blocks the next CLI invocation. The tool never
  deletes or silently steps around failed recovery evidence.
- A run is written under a hidden `.partial` directory, flushed, and atomically
  renamed only after the dump, canonical role SQL, hashes, table counts,
  migration head, and table-of-contents proof are complete.
- Completed history binds an output directory to one PostgreSQL
  `(system_identifier, database)` pair. Mixing another cluster or database is a
  refusal.
- Restore accepts only a completed run whose two artifacts still match the
  manifest and whose `roles.sql` remains byte-identical to the tracked
  `scripts/compose_restore_roles.sql`.
- Restore verifies owner-only directory provenance, opens each artifact once,
  binds its filesystem identity and digest, and consumes those same pinned
  handles for the TOC, role, and dump operations. A later path replacement
  cannot change the bytes sent to PostgreSQL.
- Direct restore requires both `--confirm-clean-target` and an objective
  zero-user-object query. It is supported only for a freshly provisioned
  target whose lifecycle is exclusively owned by this recovery operation. It
  also requires the exact source image/bootstrap user and a dedicated cluster
  containing only the target plus the expected fresh-cluster databases and
  that one bootstrap role. A reused, non-empty, or shared target is refused.
- Backup requires an explicit application-writer quiescence window. The
  operator must establish it before capture starts and hold it until `pg_dump`
  exits successfully. The database checks complement that operational fence;
  they do not claim to discover every external writer.

The manifest and SHA-256 values detect corruption; they are not a remote
publisher signature. Their provenance boundary is the current-owner-only
backup root or the independently verified current-owner-only P0-a bundle. Do
not download an untrusted three-file package, grant it the owner ACL, and treat
its self-consistent manifest as authentic recovery data.

## What one database run contains

```text
ums-database-backup-YYYYMMDDTHHMMSSZ-<nonce>/
  database.dump
  roles.sql
  database-manifest.json
```

`database.dump` is `pg_dump --format=custom --no-owner`. `roles.sql` is not a
`pg_dumpall` capture: it is the repository's reviewed, password-free, idempotent
NOLOGIN contract for `app_tenant` and `app_platform`. That avoids replaying
unrelated cluster users, memberships, passwords, object grants, or privileged
attributes.

The manifest schema is `ums-database-backup/v2`. It records:

- UTC creation time;
- database and cluster identity;
- PostgreSQL server version and immutable source image ID;
- database encoding, collation/ctype, provider/locale, ICU rules, and recorded
  collation version;
- the exact single Alembic head;
- every local non-system ordinary/partitioned table or materialized view and
  its row count;
- every captured sequence's pre/post value and state;
- the exact authorization-catalog digest after comparison with the runtime
  role, permission, and role-permission registries. Restore repeats this gate
  before target resolution, so a historically self-consistent backup cannot
  reintroduce grants forbidden by the current code;
- the observed seed-table floor;
- SHA-256 and byte size for both artifacts; and
- the readable `pg_restore --list` entry count.

The local-table row counts and the dump share one exported repeatable-read
snapshot held through `pg_dump`. Counting each local relation before the dump
also retains its normal read lock through the dump. PostgreSQL sequence state
is not MVCC-snapshot data and is not made safe by those relation locks, so the
tool records it before `pg_dump`, reads it again afterward, and refuses
publication unless the two observations match.
The stable sequence state is part of the manifest and is verified after
restore. Any non-system foreign table is refused instead of pretending that a
logical dump contains a self-contained copy of remote data.

This is a non-mutating repeatable-read transaction, not a PostgreSQL `READ ONLY`
transaction. It takes a catalog `SHARE` lock before the first snapshot query and
explicit `ACCESS SHARE` locks on local relations, closing the non-MVCC
`TRUNCATE` gap while remaining compatible with `pg_dump`. Those locks still do
not make sequence values transactional and do not replace the required writer
quiescence window.

## Seed/content gate

The gate names seed **tables**, never seed row-count literals:

```text
public.alembic_version
public.currencies
public.tenants
public.roles
public.permissions
public.role_permission_assignments
```

Their counts are measured after the current migration head and written into the
manifest. A missing or empty seed table refuses publication. A database with no
rows outside those seed tables is also refused: a freshly migrated but
application-empty database is not recovery evidence, and this refusal has no
override.

Whenever a migration begins seeding another table, update `SEED_TABLES` and its
migration-parity test in the same change. Never copy a measured numeric total
into production code.

## Create a database backup

Prerequisites:

- `uv sync --extra dev --extra test --extra lint` completed;
- the Compose PostgreSQL service is healthy and publishes its loopback port;
- the selected database is at exactly one Alembic head;
- the database has reached the irreversible minimum recovery revision
  `20260825_0002`; and
- the host output is new/empty or already carries this tool's root marker, and
  has enough space for a new immutable run.

Before running the command, stop or otherwise fence every application,
connector, scheduler, maintenance job, and operator session that can write to
the database. Establish that quiescence before invoking the CLI and do not
release it until the command reports success. A sequence change during the
capture is a hard refusal, not a warning or a retry signal.

From the repository root in PowerShell:

```powershell
$ErrorActionPreference = 'Stop'
$dbContainer = (docker compose ps -q postgres).Trim()
if (-not $dbContainer) { throw 'Compose postgres container is not running' }

uv run python scripts/backup_database.py `
  --container $dbContainer `
  --out-dir D:\UMS-Backups `
  --confirm-writers-quiesced
if ($LASTEXITCODE -ne 0) { throw "database backup failed: $LASTEXITCODE" }
```

The script reads the selected container's database/user/password and published
loopback port from Docker metadata. The password is used only for the local
snapshot connection; it is never placed in command arguments, output, the
manifest, or an artifact.

For a coordinated database + artifact/blob backup, use the writer-stop window
and exact command in `Docs/20_COMPOSE_STORAGE_RUNBOOK.md`. It passes the
pre-created owner-only outer bundle as `--out-dir` together with
`--coordinated-bundle`, so the semantic run becomes one direct child without a
manual move or broad-directory ACL adoption. The outer bundle owns service
state, Git revision, artifact archive, cloud-object receipts, and the final
cross-artifact checksum manifest. Its database-package check is structural;
the database CLI owns strict manifest parsing, artifact hashing, TOC checks,
and PostgreSQL semantic verification. This database run owns only PostgreSQL
semantics.

## Rehearse the restore

The operator must select the local PostgreSQL image explicitly. Its immutable
local image ID must equal the ID recorded in the manifest; there is no mutable
tag or manifest fallback.

```powershell
$run = 'D:\UMS-Backups\ums-database-backup-20260831T000000Z-1234abcd'
$image = 'postgres:18-alpine' # Explicit operator-selected local reference.
$recordedImageId = (Get-Content -Raw `
  (Join-Path $run 'database-manifest.json') | ConvertFrom-Json).source.image_id
$localImageId = (docker image inspect $image --format '{{.Id}}').Trim()
if ($localImageId -ne $recordedImageId) {
  throw 'selected local image does not match the recorded immutable image id'
}

uv run python scripts/restore_database.py `
  --backup-dir $run `
  --rehearse `
  --rehearse-image $image
if ($LASTEXITCODE -ne 0) { throw "restore rehearsal failed: $LASTEXITCODE" }
```

The rehearsal creates a uniquely named container with a new anonymous volume
and an ephemeral loopback port. Before the first write it proves:

1. strict manifest shape and both artifact hashes;
2. byte equality with the current canonical role SQL;
3. exact operator-selected image identity;
4. zero user objects in the target;
5. a dedicated fresh cluster with no unrelated database or non-system role;
6. exact image ID/bootstrap user plus matching database name, PostgreSQL major
   version, encoding, and locale; and
7. a readable archive with the recorded table-of-contents size.

The rehearsal also proves that a deliberately wrong password is rejected on
the exact published loopback endpoint before replay. That is a narrow runtime
credential check for this container and endpoint; it is not a general audit of
PostgreSQL HBA rules or every possible connection path.

It then applies the canonical roles, restores the dump in one
`pg_restore --single-transaction`, and compares every restored local-table
count, sequence state, authorization-catalog digest, and the Alembic head to the
manifest. The container and volume are always removed on success or failure;
rehearsal has no retention option. The published host port is loopback-only and
uses a fresh high-entropy per-run password.

Any `MISSING`, `EXTRA`, or changed table count is a failed rehearsal. Do not
enter real data until the exact backup intended for operations passes.

## Clean-target disaster recovery

Provision a new PostgreSQL container/database with the same image, database
name, encoding, and locale as the source. The target must be freshly created
for this recovery, must not have served another workload, and must remain under
the recovery operator's exclusive lifecycle control through verification. Do
not run migrations; the dump owns the schema. Prove the intended credential
works and a deliberately wrong password is rejected on the exact endpoint;
this checks that endpoint only and is not a claim about untested HBA paths.
Then:

```powershell
uv run python scripts/restore_database.py `
  --backup-dir $run `
  --target-container ums-clean-recovery-postgres `
  --confirm-clean-target
if ($LASTEXITCODE -ne 0) { throw "clean restore failed: $LASTEXITCODE" }
```

The acknowledgement does not override the clean check or turn a reused target
into a supported target. Restore refuses before mutation for tables, sequences,
views, materialized views, foreign tables, user types/functions/schemas,
extensions, event triggers, foreign-data objects,
default privileges, large objects, publications/subscriptions, transforms,
database/role/parameter settings and ACL drift, public-schema ownership or ACL
drift, security labels, casts/languages/access methods,
statistics/text-search/operator objects, or detected user-created catalog
shadows. It also refuses a cluster containing an extra or modified database
(including disabled/template-marked databases), an extra or modified role,
non-default membership, or custom tablespace.

There is intentionally no in-place replacement command. If the target matters,
back it up separately, provision another clean target, restore there, verify,
then make the deployment switch outside this tool.

If a direct restore fails after role replay begins, discard that freshly
provisioned target and start again. The database archive is transactional, but
the two cluster-level NOLOGIN role statements necessarily commit separately.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Backup/restore and all required verification completed. |
| `2` | Operator contract refused: path, manifest selection, image, or non-clean target. |
| `3` | Docker/container/image discovery failed. |
| `4` | PostgreSQL connection, readiness, identity, or snapshot failed. |
| `5` | Native dump command failed. |
| `6` | Archive creation/readability/restore failed. |
| `7` | Lock, durable publication, cleanup, or post-restore verification failed. |
| `8` | Integrity, identity history, seed/content, or semantic manifest refused. |

Every refusal is fail-closed. None of these codes means “use a broader flag.”

## Database blast radius

Backup issues no SQL writes, but it holds a locking snapshot and requires an
operator-enforced writer quiescence window. Restore writes every schema and row
represented by the archive, including finance, audit, tenancy, and
authorization state, but only into a freshly provisioned exclusive target
proven clean before the first write. PostgreSQL remains the sole source of
truth.

No ORM model or schema changes are introduced by this tooling. No migration or
backfill is required. The implementation depends on the post-P0-c migration
chain having exactly `20260825_0002` as its single Alembic head and measures its
seed rows at runtime. That revision is an irreversible authorization-security
repair and is the minimum supported recovery floor: never back up, validate,
restore, rehearse, or downgrade below `20260825_0002`.

Rollback is code-only: revert the tooling and this document. Published backup
runs remain immutable operator data and must not be deleted by a code rollback.
Code rollback does not authorize an Alembic downgrade through the `0002`
security floor.

## Validation record

The implementation handoff records the exact final commands, disposable
container name/image, migrated head, observed dynamic seed floor, backup run,
rehearsal result, full-suite result, and cleanup evidence. A passing unit test
without one real migrated PostgreSQL backup + throwaway restore does not clear
P0-b.
