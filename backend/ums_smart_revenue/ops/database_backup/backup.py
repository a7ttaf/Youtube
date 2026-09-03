"""Orchestrate one snapshot-consistent PostgreSQL backup."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ums_smart_revenue.ops.database_backup.contracts import (
    DUMP_NAME,
    ROLES_NAME,
    SEED_TABLES,
    ArtifactRecord,
    BackupManifest,
    BackupToolError,
    require_migration_security_floor,
    sha256_file,
)
from ums_smart_revenue.ops.database_backup.filesystem import (
    exclusive_output_lock,
    new_staging_directory,
    publish,
    require_matching_backup_history,
    write_manifest,
)
from ums_smart_revenue.ops.database_backup.postgres import (
    CommandRunner,
    dump_snapshot,
    dump_toc_entries,
    exported_snapshot,
    require_source_quiescent,
    resolve_container_connection,
    snapshot_authorization_catalog_digest,
    snapshot_sequences,
    snapshot_source_record,
    snapshot_table_counts,
)


@dataclass(frozen=True)
class BackupResult:
    """Published path and semantic manifest returned to the thin CLI."""

    path: Path
    manifest: BackupManifest


def _canonical_roles_bytes(repository_root: Path) -> bytes:
    """Read the tracked canonical role SQL, refusing a missing or redirected file."""
    path = repository_root / "scripts" / "compose_restore_roles.sql"
    if not path.is_file() or path.is_symlink():
        raise BackupToolError(
            "canonical scripts/compose_restore_roles.sql is missing or redirected",
            exit_code=2,
        )
    try:
        body = path.read_bytes()
        decoded = body.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BackupToolError("canonical role SQL is unreadable", exit_code=2) from exc
    required_tokens = ("app_tenant", "app_platform", "NOBYPASSRLS", "NOLOGIN")
    if not body or any(token not in decoded for token in required_tokens):
        raise BackupToolError(
            "canonical role SQL is missing its required role contract", exit_code=2
        )
    if "PASSWORD" in decoded.upper():
        raise BackupToolError("canonical role SQL must not contain password material", exit_code=2)
    return body


def _write_roles(staging: Path, body: bytes) -> Path:
    """Write the canonical role SQL into the staging directory as a new file."""
    path = staging / ROLES_NAME
    with path.open("xb") as stream:
        stream.write(body)
        stream.flush()
    if path.stat().st_size < 1:
        raise BackupToolError("canonical role SQL copy is empty", exit_code=6)
    if path.is_symlink():
        raise BackupToolError("canonical role SQL destination became a link", exit_code=6)
    if path.stat().st_size != len(body):
        raise BackupToolError("canonical role SQL copy was truncated", exit_code=6)
    try:
        path.chmod(0o600)
    except OSError as exc:
        raise BackupToolError("could not restrict the role SQL copy", exit_code=6) from exc
    return path


def _content_floor(table_counts: dict[str, int]) -> dict[str, int]:
    """Reject any seed table that is missing or empty in the snapshot."""
    missing = sorted(SEED_TABLES - set(table_counts))
    empty = sorted(name for name in SEED_TABLES if table_counts.get(name, 0) < 1)
    if missing or empty:
        raise BackupToolError(
            f"database seed floor is incomplete (missing={missing}, empty={empty})",
            exit_code=8,
        )
    application_rows = sum(count for name, count in table_counts.items() if name not in SEED_TABLES)
    if application_rows < 1:
        raise BackupToolError(
            "database has no rows outside the migration seed tables; refusing to publish an "
            "empty-install backup as recovery evidence",
            exit_code=8,
        )
    return {name: table_counts[name] for name in sorted(SEED_TABLES)}


# ============================================================================
# Purpose: Produce one immutable database-only backup from a held PostgreSQL
#   snapshot, with table/sequence state, authorization semantics, identity,
#   migration head, hashes, and TOC proof.
# Database/ORM: Non-mutating repeatable-read locks/snapshot over every local
#   relation and alembic_version; pg_dump imports the same snapshot.
# Standards: Explicit writer quiescence, canonical role SQL only, exclusive
#   output lock, owner-only staging, strict gates, fsync, atomic publication.
# Blast Radius: Finance/auth/audit rows are copied byte-for-byte into a host
#   archive. No retention or artifact/blob backup is claimed by this function.
# Connections:
#   - File: scripts/compose_restore_roles.sql -> only accepted cluster role SQL.
#   - File: scripts/compose.py -> owns the coordinated outer bundle.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> operator contract.
# ============================================================================
def run_backup(
    *,
    repository_root: Path,
    output_directory: Path,
    container: str,
    timeout_seconds: int,
    writers_quiesced: bool,
    now: datetime | None = None,
) -> BackupResult:
    """Create and atomically publish one verified database-only backup.

    Args:
        repository_root: Path. Checkout root used to locate tracked SQL and Alembic state.
        output_directory: Path. Dedicated host directory that holds the backup runs.
        container: str. Docker container name or id hosting PostgreSQL.
        timeout_seconds: int. Bounded lifetime for every native command invocation.
        writers_quiesced: bool. Operator confirmation that all source writers are stopped.
        now: datetime | None. Optional injected clock used for the run timestamp.

    Returns:
        The published run's ``BackupResult``.

    Raises:
        BackupToolError: any gate refuses the run (for example a missing writer-quiescence
            confirmation, foreign tables, or a non-quiescent source), the capture fails (empty
            pg_dump output, sequence drift mid-dump), or staging and publication cannot complete
            safely.
    """
    if not writers_quiesced:
        raise BackupToolError(
            "backup requires explicit confirmation that all source writers are stopped",
            exit_code=2,
        )
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = instant.strftime("%Y%m%dT%H%M%SZ")
    run_name = f"ums-database-backup-{stamp}-{secrets.token_hex(4)}"
    destination = output_directory / run_name
    roles_body = _canonical_roles_bytes(repository_root)
    runner = CommandRunner(timeout_seconds=timeout_seconds)

    with exclusive_output_lock(output_directory):
        source = resolve_container_connection(runner, container)
        staging = new_staging_directory(output_directory, run_name=run_name)
        dump_path = staging / DUMP_NAME
        roles_path = _write_roles(staging, roles_body)

        with exported_snapshot(source) as (connection, snapshot_id):
            source_record = snapshot_source_record(connection, source)
            require_migration_security_floor(
                repository_root,
                source_record.migration_heads,
            )
            require_matching_backup_history(output_directory, source_record.identity)
            authorization_digest = snapshot_authorization_catalog_digest(
                connection, require_canonical=True
            )
            # FIX: Count every table before pg_dump so the exporting transaction
            # retains ACCESS SHARE locks through the dump. PostgreSQL TRUNCATE is
            # not MVCC-safe if a snapshot first touches a table after the dump's
            # own locks have already been released.
            tables = snapshot_table_counts(connection)
            sequences = snapshot_sequences(connection)
            dump_snapshot(runner, source, snapshot_id, dump_path)
            require_source_quiescent(connection)
            if snapshot_sequences(connection) != sequences:
                raise BackupToolError(
                    "source sequence state changed during pg_dump; backup was not published",
                    exit_code=8,
                )

        if dump_path.stat().st_size < 1:
            raise BackupToolError("pg_dump produced an empty archive", exit_code=6)
        try:
            dump_path.chmod(0o600)
        except OSError as exc:
            raise BackupToolError("could not restrict the database dump", exit_code=6) from exc
        toc_entries = dump_toc_entries(runner, source.container, dump_path)
        counts = {record.qualified_name: record.rows for record in tables}
        seed_floor = _content_floor(counts)
        artifacts = (
            ArtifactRecord(
                name=DUMP_NAME,
                bytes=dump_path.stat().st_size,
                sha256=sha256_file(dump_path),
            ),
            ArtifactRecord(
                name=ROLES_NAME,
                bytes=roles_path.stat().st_size,
                sha256=sha256_file(roles_path),
            ),
        )
        manifest = BackupManifest(
            created_at=instant.isoformat().replace("+00:00", "Z"),
            source=source_record,
            tables=tables,
            sequences=sequences,
            authorization_catalog_sha256=authorization_digest,
            seed_floor=seed_floor,
            artifacts=artifacts,
            dump_toc_entries=toc_entries,
        )
        write_manifest(staging, manifest)
        publish(staging, destination, manifest=manifest)
    return BackupResult(path=destination, manifest=manifest)
