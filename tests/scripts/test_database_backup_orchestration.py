"""Orchestration tests for safe backup publication and clean restore order."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import scripts.backup_database as backup_cli
import scripts.restore_database as restore_cli

from ums_smart_revenue.ops.database_backup import backup, restore
from ums_smart_revenue.ops.database_backup.contracts import (
    DUMP_NAME,
    MANIFEST_NAME,
    ROLES_NAME,
    SEED_TABLES,
    ArtifactRecord,
    BackupManifest,
    BackupToolError,
    DatabaseIdentity,
    DatabaseLocale,
    SequenceRecord,
    SourceRecord,
    TableRecord,
    sha256_file,
)
from ums_smart_revenue.ops.database_backup.filesystem import DirectoryIdentity
from ums_smart_revenue.ops.database_backup.postgres import (
    ContainerConnection,
    TargetContract,
)
from ums_smart_revenue.ops.database_backup.semantic import (
    authorization_catalog_digest,
    canonical_authorization_payload,
)


@pytest.fixture(autouse=True)
def _unit_restore_packages_are_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit fixtures platform-neutral; integration tests own repository gates."""
    monkeypatch.setattr(
        restore,
        "capture_trusted_directory_identity",
        lambda *_: DirectoryIdentity(device=1, inode=1),
    )
    monkeypatch.setattr(restore, "require_trusted_directory_identity", lambda *_: None)
    monkeypatch.setattr(restore, "require_migration_security_floor", lambda *_: None)


def _source() -> SourceRecord:
    """Build a valid source description."""
    return SourceRecord(
        identity=DatabaseIdentity(system_identifier="7677783453675450413", database="ums"),
        server_version_num=180000,
        image_id="sha256:" + "a" * 64,
        image_reference="postgres:18-alpine@sha256:" + "b" * 64,
        user="postgres",
        locale=DatabaseLocale(
            encoding="UTF8",
            collate="C.UTF-8",
            ctype="C.UTF-8",
            provider="c",
            locale="",
            icu_rules="",
            collation_version="",
        ),
        migration_heads=("20260825_0002",),
    )


def _tables(*, application_rows: int = 3) -> tuple[TableRecord, ...]:
    """Build a sorted table tuple with the requested row counts."""
    # Deliberately synthetic values: migration totals are measured at runtime
    # and must never become expectations in this orchestration fixture.
    counts = {
        "alembic_version": 1,
        "currencies": 2,
        "org_units": application_rows,
        "permissions": 3,
        "role_permission_assignments": 4,
        "roles": 5,
        "tenants": 1,
    }
    return tuple(
        TableRecord(schema="public", name=name, rows=count)
        for name, count in sorted(counts.items())
    )


def _sequences() -> tuple[SequenceRecord, ...]:
    """Build a valid sequence tuple."""
    return (
        SequenceRecord(
            schema="public",
            name="org_units_id_seq",
            data_type="bigint",
            start_value=1,
            increment_by=1,
            min_value=1,
            max_value=9223372036854775807,
            cache_size=1,
            cycle=False,
            last_value=3,
            is_called=True,
        ),
    )


def _connection(container: str = "source-postgres") -> ContainerConnection:
    """Build a container connection contract."""
    return ContainerConnection(
        container=container,
        host="127.0.0.1",
        port=5432,
        database="ums",
        user="postgres",
        password="not-printed",
        image_id="sha256:" + "a" * 64,
        image_reference="postgres:18-alpine@sha256:" + "b" * 64,
    )


def _repository(tmp_path: Path) -> Path:
    """Create a temporary repository root."""
    repository = tmp_path / "repo"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    canonical = Path(__file__).resolve().parents[2] / "scripts/compose_restore_roles.sql"
    (scripts / "compose_restore_roles.sql").write_bytes(canonical.read_bytes())
    return repository


def _backup_directory(tmp_path: Path, repository: Path) -> tuple[Path, BackupManifest]:
    """Publish a verified backup directory and return its manifest."""
    run = tmp_path / "ums-database-backup-20260831T000000Z-1234abcd"
    run.mkdir()
    dump = run / DUMP_NAME
    roles = run / ROLES_NAME
    dump.write_bytes(b"PGDMP-test-archive")
    roles.write_bytes((repository / "scripts/compose_restore_roles.sql").read_bytes())
    tables = _tables()
    counts = {record.qualified_name: record.rows for record in tables}
    manifest = BackupManifest(
        created_at="2026-08-31T00:00:00Z",
        source=_source(),
        tables=tables,
        sequences=_sequences(),
        authorization_catalog_sha256=authorization_catalog_digest(
            canonical_authorization_payload()
        ),
        seed_floor={name: counts[name] for name in sorted(SEED_TABLES)},
        artifacts=(
            ArtifactRecord(DUMP_NAME, dump.stat().st_size, sha256_file(dump)),
            ArtifactRecord(ROLES_NAME, roles.stat().st_size, sha256_file(roles)),
        ),
        dump_toc_entries=42,
    )
    (run / MANIFEST_NAME).write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    return run, manifest


@contextmanager
def _fake_snapshot(_source: ContainerConnection) -> Iterator[tuple[object, str]]:
    """Patch the snapshot step and report the roles digest."""
    yield object(), "00000003-0000001B-1"


def _patch_backup_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tables: tuple[TableRecord, ...] | None = None,
) -> None:
    """Patch every backup proof to its happy-path answer."""
    monkeypatch.setattr(backup, "require_migration_security_floor", lambda *_: None)
    monkeypatch.setattr(backup, "resolve_container_connection", lambda *_: _connection())
    monkeypatch.setattr(backup, "exported_snapshot", _fake_snapshot)
    monkeypatch.setattr(backup, "snapshot_source_record", lambda *_: _source())
    monkeypatch.setattr(backup, "snapshot_table_counts", lambda *_: tables or _tables())
    monkeypatch.setattr(backup, "snapshot_sequences", lambda *_: _sequences())
    monkeypatch.setattr(backup, "require_source_quiescent", lambda *_: None)
    monkeypatch.setattr(backup, "snapshot_authorization_catalog_digest", lambda *_, **__: "c" * 64)
    monkeypatch.setattr(
        backup,
        "dump_snapshot",
        lambda _runner, _source, _snapshot, path: path.write_bytes(b"PGDMP-test-archive"),
    )
    monkeypatch.setattr(backup, "dump_toc_entries", lambda *_: 42)
    monkeypatch.setattr(backup.secrets, "token_hex", lambda _size: "1234abcd")


def test_backup_publishes_strict_manifest_after_all_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup publishes a strict manifest only after every proof passes."""
    repository = _repository(tmp_path)
    output = tmp_path / "backups"
    output.mkdir()
    _patch_backup_happy_path(monkeypatch)

    result = backup.run_backup(
        repository_root=repository,
        output_directory=output,
        container="source-postgres",
        timeout_seconds=30,
        writers_quiesced=True,
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )

    assert result.path.name == "ums-database-backup-20260831T000000Z-1234abcd"
    assert result.path.is_dir()
    assert not list(output.glob("*.partial"))
    assert BackupManifest.load(result.path / MANIFEST_NAME) == result.manifest
    expected_seed_floor = {
        record.qualified_name: record.rows
        for record in _tables()
        if record.qualified_name in SEED_TABLES
    }
    assert result.manifest.seed_floor == expected_seed_floor
    assert sum(record.rows for record in result.manifest.tables) > sum(
        result.manifest.seed_floor.values()
    )
    assert (result.path / ROLES_NAME).read_bytes() == (
        repository / "scripts/compose_restore_roles.sql"
    ).read_bytes()


def test_backup_requires_explicit_writer_quiescence_before_output_mutation(
    tmp_path: Path,
) -> None:
    """Backup needs explicit writer quiescence before touching the output."""
    with pytest.raises(BackupToolError, match="writers are stopped"):
        backup.run_backup(
            repository_root=tmp_path,
            output_directory=tmp_path / "missing-output",
            container="source-postgres",
            timeout_seconds=30,
            writers_quiesced=False,
        )
    assert not (tmp_path / "missing-output").exists()


def test_backup_refuses_sequence_state_change_during_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup refuses a sequence state change during the dump."""
    repository = _repository(tmp_path)
    output = tmp_path / "backups"
    output.mkdir()
    _patch_backup_happy_path(monkeypatch)
    current = _sequences()[0]
    changed = replace(current, last_value=current.last_value + 1)
    observed = iter((_sequences(), (changed,)))
    monkeypatch.setattr(backup, "snapshot_sequences", lambda *_: next(observed))
    with pytest.raises(BackupToolError, match="sequence state changed"):
        backup.run_backup(
            repository_root=repository,
            output_directory=output,
            container="source-postgres",
            timeout_seconds=30,
            writers_quiesced=True,
        )
    assert not list(output.glob("ums-database-backup-*"))


def test_backup_counts_and_locks_tables_before_pg_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup counts and locks tables before running pg_dump."""
    repository = _repository(tmp_path)
    output = tmp_path / "backups"
    output.mkdir()
    _patch_backup_happy_path(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(
        backup,
        "snapshot_table_counts",
        lambda *_: events.append("count-and-lock") or _tables(),
    )
    monkeypatch.setattr(
        backup,
        "dump_snapshot",
        lambda _runner, _source, _snapshot, path: (
            events.append("dump") or path.write_bytes(b"PGDMP-test-archive")
        ),
    )
    backup.run_backup(
        repository_root=repository,
        output_directory=output,
        container="source-postgres",
        timeout_seconds=30,
        writers_quiesced=True,
    )
    assert events == ["count-and-lock", "dump"]


def test_backup_never_prunes_an_older_valid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup never prunes an older valid run."""
    repository = _repository(tmp_path)
    output = tmp_path / "backups"
    output.mkdir()
    old_run, _ = _backup_directory(output, repository)
    old_bytes = {path.name: path.read_bytes() for path in old_run.iterdir()}
    _patch_backup_happy_path(monkeypatch)
    backup.run_backup(
        repository_root=repository,
        output_directory=output,
        container="source-postgres",
        timeout_seconds=30,
        writers_quiesced=True,
        now=datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
    )
    assert old_run.is_dir()
    assert {path.name: path.read_bytes() for path in old_run.iterdir()} == old_bytes


def test_backup_refuses_seed_only_install_without_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup refuses a seed-only install without an explicit override."""
    repository = _repository(tmp_path)
    output = tmp_path / "backups"
    output.mkdir()
    _patch_backup_happy_path(monkeypatch, tables=_tables(application_rows=0))

    with pytest.raises(BackupToolError, match="no rows outside"):
        backup.run_backup(
            repository_root=repository,
            output_directory=output,
            container="source-postgres",
            timeout_seconds=30,
            writers_quiesced=True,
        )
    assert not list(output.glob("ums-database-backup-*"))
    assert list(output.glob(".*.partial"))


def test_backup_refuses_missing_seed_table_without_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup refuses a missing seed table without an explicit override."""
    repository = _repository(tmp_path)
    output = tmp_path / "backups"
    output.mkdir()
    tables = tuple(record for record in _tables() if record.name != "permissions")
    _patch_backup_happy_path(monkeypatch, tables=tables)
    with pytest.raises(BackupToolError, match="seed floor is incomplete"):
        backup.run_backup(
            repository_root=repository,
            output_directory=output,
            container="source-postgres",
            timeout_seconds=30,
            writers_quiesced=True,
        )


def test_backup_uses_only_the_tracked_role_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup uses only the tracked role SQL."""
    repository = _repository(tmp_path)
    canonical = repository / "scripts/compose_restore_roles.sql"
    canonical.write_text("CREATE ROLE app_tenant PASSWORD 'leak'; app_platform", encoding="utf-8")
    output = tmp_path / "backups"
    output.mkdir()
    _patch_backup_happy_path(monkeypatch)
    with pytest.raises(BackupToolError, match="required role contract|password material"):
        backup.run_backup(
            repository_root=repository,
            output_directory=output,
            container="source-postgres",
            timeout_seconds=30,
            writers_quiesced=True,
        )


def _patch_restore_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    manifest: BackupManifest,
    events: list[str],
) -> None:
    """Patch every restore proof to its happy-path answer."""
    target = _connection("target-postgres")
    monkeypatch.setattr(restore, "require_migration_security_floor", lambda *_: None)
    monkeypatch.setattr(
        restore,
        "resolve_container_connection",
        lambda *_: events.append("resolve") or target,
    )
    monkeypatch.setattr(restore, "wait_for_postgres", lambda *_, **__: events.append("wait"))
    monkeypatch.setattr(restore, "require_clean_target", lambda *_: events.append("clean"))
    monkeypatch.setattr(restore, "require_dedicated_cluster", lambda *_: events.append("cluster"))
    monkeypatch.setattr(
        restore, "require_password_authentication", lambda *_: events.append("password")
    )
    monkeypatch.setattr(
        restore,
        "target_contract",
        lambda *_: (
            events.append("contract")
            or TargetContract(
                database="ums", server_version_num=180000, locale=manifest.source.locale
            )
        ),
    )
    monkeypatch.setattr(
        restore,
        "dump_toc_entries",
        lambda *_: events.append("toc") or manifest.dump_toc_entries,
    )
    monkeypatch.setattr(restore, "apply_sql_file", lambda *_, **__: events.append("roles"))
    monkeypatch.setattr(restore, "restore_dump", lambda *_, **__: events.append("dump"))
    monkeypatch.setattr(
        restore,
        "target_table_counts",
        lambda *_: events.append("counts") or manifest.tables,
    )
    monkeypatch.setattr(
        restore,
        "target_migration_heads",
        lambda *_: events.append("head") or manifest.source.migration_heads,
    )
    monkeypatch.setattr(
        restore,
        "target_sequences",
        lambda *_: events.append("sequences") or manifest.sequences,
    )
    monkeypatch.setattr(
        restore,
        "target_authorization_catalog_digest",
        lambda *_: events.append("authorization") or manifest.authorization_catalog_sha256,
    )


def test_direct_restore_checks_everything_before_roles_and_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct restore checks everything before it touches roles or data."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)
    result = restore.restore_clean_target(
        repository_root=repository,
        backup_directory=run,
        target_container="target-postgres",
        timeout_seconds=30,
        wait_seconds=3,
        clean_target_confirmed=True,
    )
    assert result.tables == manifest.tables
    assert events == [
        "resolve",
        "wait",
        "clean",
        "cluster",
        "password",
        "contract",
        "toc",
        "clean",
        "cluster",
        "roles",
        "dump",
        "counts",
        "head",
        "sequences",
        "authorization",
    ]


def test_direct_restore_requires_explicit_clean_target_acknowledgement(tmp_path: Path) -> None:
    """Direct restore requires an explicit clean-target acknowledgement."""
    repository = _repository(tmp_path)
    run, _manifest = _backup_directory(tmp_path, repository)
    with pytest.raises(BackupToolError, match="confirm-clean-target"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=False,
        )


def test_confirmed_direct_restore_still_refuses_a_nonempty_target_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed direct restore still refuses a non-empty target."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)

    def _not_clean(*_: object) -> None:
        """Report the target as not clean."""
        events.append("clean")
        raise BackupToolError("restore target is not clean", exit_code=2)

    monkeypatch.setattr(restore, "require_clean_target", _not_clean)
    with pytest.raises(BackupToolError, match="not clean"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=True,
        )
    assert "roles" not in events
    assert "dump" not in events


def test_restore_refuses_noncanonical_role_sql_before_target_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore refuses non-canonical role SQL before resolving the target."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    roles = run / ROLES_NAME
    roles.write_text("malicious role SQL", encoding="utf-8")
    body = manifest.to_json()
    assert isinstance(body["artifacts"], list)
    body["artifacts"][1] = ArtifactRecord(
        ROLES_NAME, roles.stat().st_size, sha256_file(roles)
    ).to_json()
    (run / MANIFEST_NAME).write_text(json.dumps(body), encoding="utf-8")
    called = False

    def _unexpected(*_: object) -> ContainerConnection:
        """Fail the test if a target is resolved."""
        nonlocal called
        called = True
        return _connection()

    monkeypatch.setattr(restore, "resolve_container_connection", _unexpected)
    with pytest.raises(BackupToolError, match="not byte-identical"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=True,
        )
    assert called is False


def test_restore_refuses_tampered_dump_before_target_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore refuses a tampered dump before resolving the target."""
    repository = _repository(tmp_path)
    run, _manifest = _backup_directory(tmp_path, repository)
    (run / DUMP_NAME).write_bytes(b"tampered")
    called = False

    def _unexpected(*_: object) -> ContainerConnection:
        """Fail the test if a target is resolved."""
        nonlocal called
        called = True
        return _connection()

    monkeypatch.setattr(restore, "resolve_container_connection", _unexpected)
    with pytest.raises(BackupToolError, match="integrity"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=True,
        )
    assert called is False


def test_restore_consumes_pinned_roles_after_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore consumes the pinned roles even after a path replacement."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)
    original = (run / ROLES_NAME).read_bytes()

    def _replace_then_resolve(*_: object) -> ContainerConnection:
        """Replace the roles path, then resolve the target."""
        replacement = tmp_path / "replacement-roles.sql"
        replacement.write_text("SELECT 'unverified';", encoding="utf-8")
        try:
            os.replace(replacement, run / ROLES_NAME)
        except PermissionError:
            # Windows denies replacement while the verified handle is open;
            # POSIX keeps the already-open verified inode pinned.
            pass
        events.append("resolve")
        return _connection("target-postgres")

    consumed: list[bytes] = []

    def _consume_roles(*_: object, **kwargs: object) -> None:
        """Consume the roles bytes handed to the restore."""
        source = kwargs["source"]
        source.seek(0)  # type: ignore[attr-defined]
        consumed.append(source.read())  # type: ignore[attr-defined]
        events.append("roles")

    monkeypatch.setattr(restore, "resolve_container_connection", _replace_then_resolve)
    monkeypatch.setattr(restore, "apply_sql_file", _consume_roles)
    restore.restore_clean_target(
        repository_root=repository,
        backup_directory=run,
        target_container="target-postgres",
        timeout_seconds=30,
        wait_seconds=3,
        clean_target_confirmed=True,
    )
    assert consumed == [original]


def test_restore_verification_rejects_any_row_count_difference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore verification rejects any row count difference."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)
    changed = tuple(
        replace_record(record, rows=record.rows + 1) if record.name == "org_units" else record
        for record in manifest.tables
    )
    monkeypatch.setattr(restore, "target_table_counts", lambda *_: changed)
    with pytest.raises(BackupToolError, match="row-count verification failed"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=True,
        )


@pytest.mark.parametrize("variant", ["missing", "extra", "redistributed"])
def test_restore_requires_the_exact_table_set_and_per_table_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """Restore requires the exact table set and per-table counts."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)
    actual = list(manifest.tables)
    if variant == "missing":
        actual.pop()
    elif variant == "extra":
        actual.append(TableRecord(schema="public", name="unexpected", rows=0))
    else:
        first = actual[0]
        second = actual[1]
        actual[0] = replace_record(first, rows=first.rows + 1)
        actual[1] = replace_record(second, rows=second.rows - 1)
    monkeypatch.setattr(restore, "target_table_counts", lambda *_: tuple(actual))
    with pytest.raises(BackupToolError, match="row-count verification failed"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=True,
        )


def test_restore_requires_the_exact_alembic_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore requires the exact Alembic head."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)
    monkeypatch.setattr(restore, "target_migration_heads", lambda *_: ("wrong-head",))
    with pytest.raises(BackupToolError, match="Alembic head"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=True,
        )


def test_restore_requires_exact_sequence_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore requires exact sequence state."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)
    monkeypatch.setattr(restore, "target_sequences", lambda *_: ())
    with pytest.raises(BackupToolError, match="sequence parameters/state"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=True,
        )


def test_restore_requires_exact_authorization_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore requires the exact authorization catalog."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)
    monkeypatch.setattr(restore, "target_authorization_catalog_digest", lambda *_: "d" * 64)
    with pytest.raises(BackupToolError, match="authorization catalog"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=True,
        )


def test_restore_refuses_historical_authorization_manifest_before_target_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore refuses a historical authorization manifest before target access."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    historical = replace(manifest, authorization_catalog_sha256="d" * 64)
    (run / MANIFEST_NAME).write_text(json.dumps(historical.to_json()), encoding="utf-8")
    monkeypatch.setattr(
        restore,
        "resolve_container_connection",
        lambda *_: pytest.fail("historical authorization reached target resolution"),
    )

    with pytest.raises(BackupToolError, match="current runtime registries"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=True,
        )


def replace_record(record: TableRecord, *, rows: int) -> TableRecord:
    """Return the table record with a different row count."""
    return TableRecord(schema=record.schema, name=record.name, rows=rows)


def test_restore_refuses_locale_or_major_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore refuses a locale or major-version mismatch."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)
    monkeypatch.setattr(
        restore,
        "target_contract",
        lambda *_: TargetContract(
            database="ums", server_version_num=170000, locale=manifest.source.locale
        ),
    )
    with pytest.raises(BackupToolError, match="major version"):
        restore.restore_clean_target(
            repository_root=repository,
            backup_directory=run,
            target_container="target-postgres",
            timeout_seconds=30,
            wait_seconds=3,
            clean_target_confirmed=True,
        )


def test_rehearsal_removes_its_exact_container_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rehearsal removes its exact container after success."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)
    image_arguments: list[tuple[str, str]] = []

    def _resolve_image(_runner: object, *, operator_reference: str, expected_image_id: str) -> str:
        """Resolve the operator reference to the expected image id."""
        image_arguments.append((operator_reference, expected_image_id))
        events.append("image")
        return manifest.source.image_id

    created_images: list[str] = []

    def _create(*_: object, **kwargs: object) -> None:
        """Record the container creation call."""
        created_images.append(str(kwargs["image_id"]))
        events.append("create")

    monkeypatch.setattr(restore, "resolve_rehearsal_image", _resolve_image)
    monkeypatch.setattr(restore, "create_rehearsal_container", _create)
    removed: list[str] = []
    monkeypatch.setattr(
        restore,
        "remove_rehearsal_container",
        lambda _runner, name, *, ownership_token: removed.append(name),
    )
    result = restore.rehearse_restore(
        repository_root=repository,
        backup_directory=run,
        rehearsal_image="postgres:18-alpine@sha256:operator",
        timeout_seconds=30,
        wait_seconds=3,
    )
    assert result.kept is False
    assert removed == [result.container]
    assert result.container.startswith("ums-db-restore-rehearsal-")
    assert image_arguments == [("postgres:18-alpine@sha256:operator", manifest.source.image_id)]
    assert created_images == [manifest.source.image_id]


def test_rehearsal_cleanup_runs_when_restore_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rehearsal cleanup still runs when the restore fails."""
    repository = _repository(tmp_path)
    run, manifest = _backup_directory(tmp_path, repository)
    events: list[str] = []
    _patch_restore_happy_path(monkeypatch, manifest, events)
    monkeypatch.setattr(
        restore, "resolve_rehearsal_image", lambda *_, **__: manifest.source.image_id
    )
    monkeypatch.setattr(restore, "create_rehearsal_container", lambda *_, **__: None)
    monkeypatch.setattr(
        restore,
        "restore_dump",
        lambda *_, **__: (_ for _ in ()).throw(BackupToolError("restore failed", exit_code=6)),
    )
    removed: list[str] = []
    monkeypatch.setattr(
        restore,
        "remove_rehearsal_container",
        lambda _runner, name, *, ownership_token: removed.append(name),
    )
    with pytest.raises(BackupToolError, match="restore failed"):
        restore.rehearse_restore(
            repository_root=repository,
            backup_directory=run,
            rehearsal_image="postgres:18-alpine@sha256:operator",
            timeout_seconds=30,
            wait_seconds=3,
        )
    assert len(removed) == 1


@pytest.mark.parametrize(
    ("parser", "base", "extra"),
    [
        (
            backup_cli._parse_args,
            ["--container", "db", "--out-dir", "backup"],
            ["--allow-nonempty"],
        ),
        (
            backup_cli._parse_args,
            ["--container", "db", "--out-dir", "backup"],
            ["--retention-days", "7"],
        ),
        (
            backup_cli._parse_args,
            ["--container", "db", "--out-dir", "backup"],
            ["--prune"],
        ),
        (
            restore_cli._parse_args,
            ["--backup-dir", "run", "--rehearse", "--rehearse-image", "image"],
            ["--allow-nonempty"],
        ),
        (
            restore_cli._parse_args,
            ["--backup-dir", "run", "--rehearse", "--rehearse-image", "image"],
            ["--keep-rehearsal"],
        ),
    ],
)
def test_clis_do_not_offer_nonempty_retention_or_prune_options(
    parser: object, base: list[str], extra: list[str]
) -> None:
    """Neither CLI offers non-empty retention or prune options."""
    with pytest.raises(SystemExit) as captured:
        parser(base + extra)  # type: ignore[operator]
    assert captured.value.code == 2


def test_backup_cli_maps_untyped_filesystem_errors_to_safe_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The backup CLI maps untyped filesystem errors to a safe exit code."""
    monkeypatch.setattr(
        backup_cli,
        "resolve_output_directory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("private-path")),
    )
    assert (
        backup_cli.main(
            [
                "--container",
                "db",
                "--out-dir",
                str(tmp_path / "backup"),
                "--confirm-writers-quiesced",
            ]
        )
        == 7
    )
    error = capsys.readouterr().err
    assert "PermissionError" in error
    assert "private-path" not in error


def test_backup_cli_requires_quiescence_ack_before_output_resolution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The backup CLI needs the quiescence acknowledgement before resolving output."""
    resolved = False

    def _unexpected(*_args: object, **_kwargs: object) -> Path:
        """Fail the test if the output path is resolved."""
        nonlocal resolved
        resolved = True
        raise AssertionError("output must not be resolved")

    monkeypatch.setattr(backup_cli, "resolve_output_directory", _unexpected)
    assert backup_cli.main(["--container", "db", "--out-dir", "backup"]) == 2
    assert resolved is False
    assert "confirm-writers-quiesced" in capsys.readouterr().err


def test_restore_cli_maps_a_missing_selection_to_operator_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The restore CLI maps a missing selection to an operator refusal."""
    monkeypatch.setattr(
        restore_cli,
        "rehearse_restore",
        lambda **_kwargs: (_ for _ in ()).throw(FileNotFoundError("private-path")),
    )
    assert (
        restore_cli.main(
            [
                "--backup-dir",
                "missing",
                "--rehearse",
                "--rehearse-image",
                "image",
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "does not exist" in error
    assert "private-path" not in error
