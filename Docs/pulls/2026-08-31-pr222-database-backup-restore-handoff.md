# PR 222 replacement — database backup, restore, and rehearsal handoff

## Status

Implementation is isolated on `codex/pr222-reauthor-20260831` at
`C:\Users\Mrmah\Desktop\ums-codex-pr222-reauthor`.

The branch was constructed from exact main `41b4953939b39b55345d3d7a168eeaf57c8e2b90`,
then the reconciled plan `068982a09ad3e5f32d403adf4b55a25bf6fcd2c1`,
P0-a storage `cbbdc0ee2c047016eff6a1a7d5c03bb42e3e80bd`, and the corrected
P0-c authorization head `46f3b819606c2cfeb40dc687871815ecc78a74d9` in
that order. Revision `20260825_0002` is the irreversible minimum recovery floor;
the tooling does not support validating, backing up, rehearsing, restoring, or
downgrading below it.

The old PR #222 head `02298275` was not cherry-picked. Its 17,000-line backup
and test monolith, retention deletion paths, non-empty target replacement,
frontend churn, stale seed totals, `or True` assertions, and unrelated P0-d test
were deliberately rejected.

## Scope delivered

- `backend/ums_smart_revenue/ops/database_backup/contracts.py`
  - strict `ums-database-backup/v2` manifest;
  - exact JSON shapes without bool/float/string coercion;
  - database identity, full PostgreSQL 18 collation contract, one Alembic head,
    dynamic seed floor, table counts, sequence state, authorization-catalog digest, artifact byte
    sizes/SHA-256, and dump TOC count.
- `backend/ums_smart_revenue/ops/database_backup/postgres.py`
  - explicit Docker container resolution;
  - password kept out of argv/output/artifacts;
  - non-mutating repeatable-read capture transaction with pre-snapshot catalog
    and local-relation locks, exported to `pg_dump` and local-table counts;
  - local ordinary/partitioned tables and materialized views, foreign-table
    refusal, and sequence pre/post stability capture;
  - clean-target, version, locale, migration-head, and table-count adapters.
- `backend/ums_smart_revenue/ops/database_backup/semantic.py`
  - canonical runtime authorization catalog normalization and digest.
- `backend/ums_smart_revenue/ops/database_backup/filesystem.py`
  - host output outside the repository;
  - dedicated-root marker or explicit pre-protected P0-a bundle mode;
  - current-SID-only Windows DACL or POSIX `0700`;
  - exclusive non-reclaimed lock;
  - hidden partial run, file/directory flush, and atomic publication;
  - completed-history binding to one cluster/database identity.
- `backend/ums_smart_revenue/ops/database_backup/backup.py`
  - database-only orchestration;
  - explicit writer-quiescence contract spanning pre-capture through successful
    `pg_dump` completion;
  - canonical tracked role SQL only;
  - dynamic seed floor and non-seed content refusal;
  - no retention or pruning.
- `backend/ums_smart_revenue/ops/database_backup/restore.py`
  - byte-identical canonical role check;
  - owner-only provenance and pinned verified artifact handles through replay;
  - archive readability before target mutation;
  - direct restore only to a freshly provisioned, objectively clean target
    under exclusive recovery lifecycle ownership;
  - exact image-ID-selected throwaway rehearsal;
  - wrong-password endpoint proof, single-transaction restore, and exact
    count/sequence/head verification.
- `scripts/backup_database.py` and `scripts/restore_database.py`
  - thin typed CLI boundaries and stable safe exit codes.
- `scripts/compose_storage.py`
  - coordinated recovery profile now requires exactly one structurally complete
    nested database package alongside artifact/blob, service-state, Git, and
    optional GCS receipts; P0-b retains semantic-manifest ownership.
- `Docs/20_COMPOSE_STORAGE_RUNBOOK.md` and
  `Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md`
  - database-package and coordinated-recovery operator procedures.

## Explicit non-goals

- No automated retention or deletion.
- No `allow-nonempty`, database drop, schema clear, or in-place replacement.
- No broad `pg_dumpall --roles-only` replay.
- No frontend, seed-demo, smoke-MVP, finance formula, authorization, or session
  changes.
- Restore rejects a manifest whose authorization digest differs from the
  current runtime registries before it resolves or writes a target.
- No artifact/blob ownership in the database CLI; P0-a owns the outer bundle.
- No upload, push, PR mutation, branch-protection mutation, or live-data action.

## Current validation status

Final readiness validation is pending on the final branch bytes. Do not infer a
pass from an earlier development checkpoint. The final handoff must replace the
pending fields below with literal observed values:

```text
toolchain sync: PENDING
Ruff format/check: PENDING
focused database-backup tests: PENDING
mypy database-backup scope: PENDING
Alembic single head and minimum-floor proof: PENDING
real PostgreSQL 18 backup run: PENDING
observed dynamic seed floor: PENDING
observed local-table and sequence manifest totals: PENDING
wrong-password endpoint refusal: PENDING
throwaway rehearsal and exact cleanup: PENDING
non-empty target prewrite refusal: PENDING
full disposable-PostgreSQL pytest: PENDING
git diff/staged diff hygiene: PENDING
```

Read-only adversarial reviews found and the branch corrected pre-final defects
in snapshot ordering under concurrent `TRUNCATE`, restore artifact TOCTOU,
passwordless rehearsal exposure, mixed host bindings, broad output-directory
ACL adoption, incomplete clean-catalog coverage, mutable container-name use,
rehearsal cleanup ownership, lock cleanup identity, and incomplete atomic
publication validation. Final focused and real-PostgreSQL regression evidence
is still pending and must be recorded literally.

Superseded-head exercises are intentionally excluded from readiness evidence;
their counts and seed totals must not be copied into code or the final record.

## Final validation required

1. Confirm dependency `46f3b819606c2cfeb40dc687871815ecc78a74d9` is
   present, the migration graph has exactly one head at `20260825_0002`, and no
   downgrade below that security floor is attempted.
2. Upgrade one uniquely named disposable PostgreSQL 18 container to Alembic
   head and prove there is exactly one head.
3. Insert representative application data without weakening RLS/auth/audit.
4. Establish explicit application-writer quiescence before capture and retain
   it through successful `pg_dump`; run the real backup CLI and record the
   dynamic seed floor, local-table counts, sequence pre/post stability, run
   directory, and secret-absence evidence.
5. Run the real throwaway rehearsal with the exact immutable image ID; prove a
   deliberately wrong password is rejected on the published endpoint, verify
   table counts, sequence state, and Alembic head, and prove the exact created
   container/volume is removed.
6. Prove a non-empty or reused target is refused before role or data replay.
7. Run the focused tests, full relevant storage/migration/auth tests, baseline
   Ruff, full disposable-PostgreSQL pytest, and `git diff --check` on final bytes.
8. Re-read the final diff and this handoff; replace this section with exact
   commands, counts, durations, container names, and cleanup evidence.

## Database and rollback statement

PostgreSQL remains the sole source of truth. Backup issues no SQL writes but
holds a locking snapshot and requires writer quiescence. Restore writes the
full archived schema and rows only into a freshly provisioned exclusive target
proven clean before the first write; this includes finance, audit, tenancy, and
authorization state.

No ORM model or schema change is introduced by PR #222. No new migration or
backfill is required. The tooling depends on the corrected P0-c migration chain
at the irreversible `20260825_0002` security floor and measures its post-head
seed counts at runtime rather than hardcoding them.

Rollback is code-only. Revert these files; do not delete already published
backup packages or coordinated bundles. They are operator data, not build
artifacts. Never pair a PR #222 code rollback with an Alembic downgrade below
`20260825_0002`.
