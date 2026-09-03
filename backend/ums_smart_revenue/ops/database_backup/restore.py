"""Clean-target restore and disposable rehearsal orchestration."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ums_smart_revenue.ops.database_backup.contracts import (
    DUMP_NAME,
    MANIFEST_NAME,
    ROLES_NAME,
    BackupManifest,
    BackupToolError,
    TableRecord,
    open_verified_artifacts,
    require_migration_security_floor,
)
from ums_smart_revenue.ops.database_backup.filesystem import (
    RUN_NAME_RE,
    DirectoryIdentity,
    capture_trusted_directory_identity,
    require_no_redirect_components,
    require_trusted_directory_identity,
)
from ums_smart_revenue.ops.database_backup.postgres import (
    CommandRunner,
    ContainerConnection,
    apply_sql_file,
    create_rehearsal_container,
    dump_toc_entries,
    remove_rehearsal_container,
    require_clean_target,
    require_dedicated_cluster,
    require_password_authentication,
    resolve_container_connection,
    resolve_rehearsal_image,
    restore_dump,
    target_authorization_catalog_digest,
    target_contract,
    target_migration_heads,
    target_sequences,
    target_table_counts,
    wait_for_postgres,
)
from ums_smart_revenue.ops.database_backup.semantic import (
    authorization_catalog_digest,
    canonical_authorization_payload,
)


@dataclass(frozen=True)
class RestoreResult:
    """Objective restore verification returned to the thin CLI."""

    container: str
    tables: tuple[TableRecord, ...]
    kept: bool


@dataclass(frozen=True)
class OpenBackup:
    """One manifest and the exact verified artifact identities it describes."""

    directory: Path
    directory_identity: DirectoryIdentity
    manifest: BackupManifest
    dump: BinaryIO
    roles: BinaryIO


# ============================================================================
# Purpose: Hold verified dump and role artifact handles open for the complete
#   restore so path replacement cannot change bytes after integrity checks.
# Database/ORM: None; target access begins only after this local trust gate.
# Standards: Real completed-run path, exact manifest, pinned file identities,
#   artifact digests, and byte-identical tracked role SQL before yielding.
# Blast Radius: Restore authorization/schema/data input; replacement fails shut.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/contracts.py -> pins.
#   - File: backend/ums_smart_revenue/ops/database_backup/postgres.py -> reads.
# ============================================================================
@contextmanager
def open_backup(directory: Path, *, repository_root: Path) -> Iterator[OpenBackup]:
    """Yield one completed run without reopening its verified artifacts.

    Args:
        directory: Path. Host directory the operation validates or writes.
        repository_root: Path. Checkout root used to locate tracked SQL and Alembic state.

    Returns:
        The verified ``OpenBackup`` bundle.

    Raises:
        BackupToolError: the directory is not exactly one completed backup run, its
            authorization catalog is incompatible with the runtime registries, or the canonical role
            SQL is missing or redirected.
    """
    unresolved = directory.expanduser()
    require_no_redirect_components(unresolved, label="backup directory")
    resolved = unresolved.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink() or not RUN_NAME_RE.fullmatch(resolved.name):
        raise BackupToolError(
            "backup directory must be one completed ums-database-backup-* run",
            exit_code=2,
        )
    directory_identity = capture_trusted_directory_identity(resolved)
    manifest_path = resolved / MANIFEST_NAME
    require_no_redirect_components(manifest_path, label="database manifest")
    manifest = BackupManifest.load(manifest_path)
    require_migration_security_floor(repository_root, manifest.source.migration_heads)
    current_authorization_digest = authorization_catalog_digest(canonical_authorization_payload())
    if manifest.authorization_catalog_sha256 != current_authorization_digest:
        raise BackupToolError(
            "backup authorization catalog is incompatible with the current runtime registries",
            exit_code=8,
        )
    canonical = repository_root / "scripts" / "compose_restore_roles.sql"
    if not canonical.is_file() or canonical.is_symlink():
        raise BackupToolError("canonical role SQL is missing or redirected", exit_code=2)
    with open_verified_artifacts(resolved, manifest) as artifacts:
        require_trusted_directory_identity(resolved, directory_identity)
        try:
            canonical_bytes = canonical.read_bytes()
            roles = artifacts[ROLES_NAME]
            roles.seek(0)
            roles_bytes = roles.read(len(canonical_bytes) + 1)
            roles.seek(0)
        except OSError as exc:
            raise BackupToolError("could not compare the canonical role SQL", exit_code=8) from exc
        if roles_bytes != canonical_bytes:
            raise BackupToolError(
                "backup role SQL is not byte-identical to the tracked canonical role SQL",
                exit_code=8,
            )
        try:
            yield OpenBackup(
                directory=resolved,
                directory_identity=directory_identity,
                manifest=manifest,
                dump=artifacts[DUMP_NAME],
                roles=roles,
            )
        finally:
            require_trusted_directory_identity(resolved, directory_identity)


def _major_version(server_version_num: int) -> int:
    """Reduce a server version number to its major release."""
    return server_version_num // 10000


def _require_compatible_target(target: ContainerConnection, manifest: BackupManifest) -> None:
    """Refuse a target whose major version or locale differs from the manifest."""
    if target.image_id != manifest.source.image_id:
        raise BackupToolError("target image id does not match the backup source", exit_code=2)
    if target.user != manifest.source.user:
        raise BackupToolError("target bootstrap user does not match the backup source", exit_code=2)
    contract = target_contract(target)
    if contract.database != manifest.source.identity.database:
        raise BackupToolError(
            "target database name does not match the backup manifest", exit_code=2
        )
    if _major_version(contract.server_version_num) != _major_version(
        manifest.source.server_version_num
    ):
        raise BackupToolError(
            "target PostgreSQL major version does not match the backup source",
            exit_code=2,
        )
    if contract.locale != manifest.source.locale:
        raise BackupToolError(
            "target database locale/encoding does not match the backup source",
            exit_code=2,
        )


# ============================================================================
# Purpose: Compare restored relational content, generators, authorization
#   semantics, and migration head with the strict backup manifest.
# Database/ORM: Reads all local tables/materialized views, sequences, the three
#   authorization catalogs, and public.alembic_version; no writes.
# Standards: Exact set/value equality with typed, operator-safe failures.
# Blast Radius: Finance, audit, tenancy, authorization, and future inserts.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/contracts.py -> manifest.
#   - File: backend/ums_smart_revenue/ops/database_backup/postgres.py -> adapters.
# ============================================================================
def _verify_restore(
    target: ContainerConnection, manifest: BackupManifest
) -> tuple[TableRecord, ...]:
    """Compare a restored target against the manifest's expectations."""
    actual = target_table_counts(target)
    expected_by_name = {record.qualified_name: record.rows for record in manifest.tables}
    actual_by_name = {record.qualified_name: record.rows for record in actual}
    if actual_by_name != expected_by_name:
        missing = sorted(set(expected_by_name) - set(actual_by_name))
        extra = sorted(set(actual_by_name) - set(expected_by_name))
        changed = sorted(
            name
            for name in set(expected_by_name) & set(actual_by_name)
            if expected_by_name[name] != actual_by_name[name]
        )
        raise BackupToolError(
            "restore row-count verification failed "
            f"(missing={missing}, extra={extra}, changed={changed})",
            exit_code=7,
        )
    if target_migration_heads(target) != manifest.source.migration_heads:
        raise BackupToolError("restored Alembic head does not match the manifest", exit_code=7)
    if target_sequences(target) != manifest.sequences:
        raise BackupToolError(
            "restored sequence parameters/state do not match the manifest", exit_code=7
        )
    if target_authorization_catalog_digest(target) != manifest.authorization_catalog_sha256:
        raise BackupToolError(
            "restored authorization catalog does not match the manifest", exit_code=7
        )
    return actual


# ============================================================================
# Purpose: Restore a verified archive only into an objectively clean database,
#   applying the tracked NOLOGIN role contract before a single-transaction dump.
# Database/ORM: Writes every schema/data/audit/auth/finance row in the archive;
#   verifies table counts, sequence state, auth catalog, and head after commit.
# Standards: Readability, digest, canonical-role, target cleanliness, version,
#   locale, and database-name checks all happen before the first target write.
# Blast Radius: Full database replacement is intentionally unavailable. A
#   non-clean target always fails and there is no override.
# Connections:
#   - File: scripts/compose_restore_roles.sql -> cluster roles applied first.
#   - File: backend/ums_smart_revenue/ops/database_backup/contracts.py -> proof.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> recovery procedure.
# ============================================================================
def restore_clean_target(
    *,
    repository_root: Path,
    backup_directory: Path,
    target_container: str,
    timeout_seconds: int,
    wait_seconds: int,
    clean_target_confirmed: bool,
) -> RestoreResult:
    """Restore into one explicit clean target container and verify exactly.

    Args:
        repository_root: Path. Checkout root used to locate tracked SQL and Alembic state.
        backup_directory: Path.
        target_container: str.
        timeout_seconds: int. Bounded lifetime for every native command invocation.
        wait_seconds: int.
        clean_target_confirmed: bool.

    Returns:
        ``RestoreResult`` summarizing the restored tables.

    Raises:
        BackupToolError: the clean-target confirmation is missing, the archive TOC does not
            match the manifest, or any clean-target/version/locale/role/content gate refuses the
            restore.
    """
    if not clean_target_confirmed:
        raise BackupToolError(
            "direct restore requires --confirm-clean-target after provisioning a new target",
            exit_code=2,
        )
    with open_backup(backup_directory, repository_root=repository_root) as backup:
        manifest = backup.manifest
        runner = CommandRunner(timeout_seconds=timeout_seconds)
        target = resolve_container_connection(runner, target_container)
        wait_for_postgres(
            runner,
            source=target,
            wait_seconds=wait_seconds,
        )
        require_clean_target(target)
        require_dedicated_cluster(target)
        require_password_authentication(target)
        _require_compatible_target(target, manifest)
        toc_entries = dump_toc_entries(runner, target.container, backup.dump)
        if toc_entries != manifest.dump_toc_entries:
            raise BackupToolError("archive TOC does not match the manifest", exit_code=8)
        # FIX: Recheck both the exclusive target and trusted run identity at the
        # last possible point before the first database write.
        require_clean_target(target)
        require_dedicated_cluster(target)
        require_trusted_directory_identity(backup.directory, backup.directory_identity)
        apply_sql_file(
            runner,
            container=target.container,
            user=target.user,
            database=target.database,
            source=backup.roles,
        )
        restore_dump(
            runner,
            container=target.container,
            user=target.user,
            database=target.database,
            source=backup.dump,
        )
        tables = _verify_restore(target, manifest)
        return RestoreResult(container=target.container, tables=tables, kept=True)


# ============================================================================
# Purpose: Exercise the entire restore contract in a uniquely named throwaway
#   PostgreSQL container selected explicitly by the operator and matched by id.
# Database/ORM: Restores only into a new Docker volume, verifies, then removes
#   that exact container/volume on both success and failure.
# Standards: No image fallback, no target reuse, no non-empty override, bounded
#   readiness and cleanup with typed failure propagation.
# Blast Radius: Disposable Docker container and anonymous volume only.
# Connections:
#   - File: docker-compose.yml -> source PostgreSQL image pin.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> required rehearsal gate.
# ============================================================================
def rehearse_restore(
    *,
    repository_root: Path,
    backup_directory: Path,
    rehearsal_image: str,
    timeout_seconds: int,
    wait_seconds: int,
) -> RestoreResult:
    """Restore and verify inside one new throwaway PostgreSQL container.

    Args:
        repository_root: Path. Checkout root used to locate tracked SQL and Alembic state.
        backup_directory: Path.
        rehearsal_image: str.
        timeout_seconds: int. Bounded lifetime for every native command invocation.
        wait_seconds: int.

    Returns:
        ``RestoreResult`` summarizing the rehearsal target.

    Raises:
        BackupToolError: the archive TOC does not match the manifest, the rehearsal fails, or
            owned-container cleanup cannot be verified.
    """
    with open_backup(backup_directory, repository_root=repository_root) as backup:
        manifest = backup.manifest
        runner = CommandRunner(timeout_seconds=timeout_seconds)
        image_id = resolve_rehearsal_image(
            runner,
            operator_reference=rehearsal_image,
            expected_image_id=manifest.source.image_id,
        )
        ownership_token = secrets.token_hex(16)
        name = f"ums-db-restore-rehearsal-{ownership_token}"
        creation_attempted = False
        tables: tuple[TableRecord, ...] = ()
        try:
            creation_attempted = True
            create_rehearsal_container(
                runner,
                image_id=image_id,
                database=manifest.source.identity.database,
                user=manifest.source.user,
                name=name,
                ownership_token=ownership_token,
            )
            target = resolve_container_connection(runner, name)
            wait_for_postgres(
                runner,
                source=target,
                wait_seconds=wait_seconds,
            )
            require_clean_target(target)
            require_dedicated_cluster(target)
            require_password_authentication(target)
            _require_compatible_target(target, manifest)
            toc_entries = dump_toc_entries(runner, target.container, backup.dump)
            if toc_entries != manifest.dump_toc_entries:
                raise BackupToolError("archive TOC does not match the manifest", exit_code=8)
            require_clean_target(target)
            require_dedicated_cluster(target)
            require_trusted_directory_identity(backup.directory, backup.directory_identity)
            apply_sql_file(
                runner,
                container=target.container,
                user=target.user,
                database=target.database,
                source=backup.roles,
            )
            restore_dump(
                runner,
                container=target.container,
                user=target.user,
                database=target.database,
                source=backup.dump,
            )
            tables = _verify_restore(target, manifest)
        except BaseException as primary:
            if creation_attempted:
                try:
                    remove_rehearsal_container(runner, name, ownership_token=ownership_token)
                except BackupToolError:
                    raise BackupToolError(
                        "rehearsal failed and owned-container cleanup could not be verified",
                        exit_code=7,
                    ) from primary
            raise
        remove_rehearsal_container(runner, name, ownership_token=ownership_token)
        return RestoreResult(container=name, tables=tables, kept=False)
