"""Adversarial tests for database-backup disk and manifest boundaries."""

from __future__ import annotations

import inspect
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from ums_smart_revenue.ops.database_backup import filesystem
from ums_smart_revenue.ops.database_backup.contracts import (
    BACKUP_SCHEMA,
    DUMP_NAME,
    MANIFEST_NAME,
    MINIMUM_SECURITY_REVISION,
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
    open_verified_artifacts,
    require_migration_security_floor,
    sha256_file,
    verify_artifacts,
)
from ums_smart_revenue.ops.database_backup.filesystem import (
    capture_trusted_directory_identity,
    exclusive_output_lock,
    new_staging_directory,
    publish,
    require_matching_backup_history,
    require_owner_only_directory,
    require_trusted_directory_identity,
    resolve_output_directory,
    write_manifest,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _source(identity: DatabaseIdentity | None = None) -> SourceRecord:
    """Build a valid source record for manifest tests."""
    return SourceRecord(
        identity=identity
        or DatabaseIdentity(system_identifier="7677783453675450413", database="ums"),
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
    # and must never become expectations in this unit-contract fixture.
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
        TableRecord(schema="public", name=name, rows=rows) for name, rows in sorted(counts.items())
    )


def _sequence(name: str = "org_units_id_seq") -> SequenceRecord:
    """Build a valid sequence record."""
    return SequenceRecord(
        schema="public",
        name=name,
        data_type="bigint",
        start_value=1,
        increment_by=1,
        min_value=1,
        max_value=9223372036854775807,
        cache_size=1,
        cycle=False,
        last_value=3,
        is_called=True,
    )


def _materialize(directory: Path, *, identity: DatabaseIdentity | None = None) -> BackupManifest:
    """Write a complete backup directory with a verified manifest."""
    directory.mkdir()
    dump = directory / DUMP_NAME
    roles = directory / ROLES_NAME
    dump.write_bytes(b"PGDMP-test")
    roles.write_text("role contract", encoding="utf-8")
    tables = _tables()
    observed = {record.qualified_name: record.rows for record in tables}
    manifest = BackupManifest(
        created_at="2026-08-31T00:00:00Z",
        source=_source(identity),
        tables=tables,
        sequences=(_sequence(),),
        authorization_catalog_sha256="c" * 64,
        seed_floor={key: observed[key] for key in sorted(SEED_TABLES)},
        artifacts=(
            ArtifactRecord(DUMP_NAME, dump.stat().st_size, sha256_file(dump)),
            ArtifactRecord(ROLES_NAME, roles.stat().st_size, sha256_file(roles)),
        ),
        dump_toc_entries=42,
    )
    (directory / MANIFEST_NAME).write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    return manifest


def _body() -> dict[str, object]:
    """Build a manifest payload that passes strict validation."""
    dump = ArtifactRecord(DUMP_NAME, 10, "a" * 64)
    roles = ArtifactRecord(ROLES_NAME, 11, "b" * 64)
    tables = _tables()
    observed = {record.qualified_name: record.rows for record in tables}
    return BackupManifest(
        created_at="2026-08-31T00:00:00Z",
        source=_source(),
        tables=tables,
        sequences=(_sequence(),),
        authorization_catalog_sha256="c" * 64,
        seed_floor={key: observed[key] for key in sorted(SEED_TABLES)},
        artifacts=(dump, roles),
        dump_toc_entries=42,
    ).to_json()


def test_manifest_round_trip_is_strict_and_lossless() -> None:
    """A manifest survives a JSON round trip without losing anything."""
    body = _body()
    parsed = BackupManifest.from_json(body)
    assert parsed.to_json() == body
    assert body["schema"] == BACKUP_SCHEMA


@pytest.mark.parametrize(
    "field",
    [
        "status",
        "source",
        "tables",
        "sequences",
        "authorization_catalog_sha256",
        "seed_floor",
        "artifacts",
    ],
)
def test_manifest_refuses_missing_top_level_fields(field: str) -> None:
    """The manifest refuses a payload missing any top-level field."""
    body = _body()
    del body[field]
    with pytest.raises(BackupToolError, match="wrong fields"):
        BackupManifest.from_json(body)


def test_manifest_refuses_extra_fields() -> None:
    """The manifest refuses a payload with extra fields."""
    body = _body()
    body["signature"] = "pretend"
    with pytest.raises(BackupToolError, match="wrong fields"):
        BackupManifest.from_json(body)


def test_manifest_load_refuses_duplicate_keys(tmp_path: Path) -> None:
    """Manifest loading refuses duplicate JSON keys."""
    manifest = tmp_path / MANIFEST_NAME
    manifest.write_text(
        '{"schema":"ums-database-backup/v2","schema":"spoofed"}',
        encoding="utf-8",
    )
    with pytest.raises(BackupToolError, match="duplicate 'schema' field"):
        BackupManifest.load(manifest)


def test_migration_security_floor_uses_the_real_revision_graph() -> None:
    """The security floor is tied to the real revision graph."""
    require_migration_security_floor(REPOSITORY_ROOT, (MINIMUM_SECURITY_REVISION,))
    with pytest.raises(BackupToolError, match="minimum security floor"):
        require_migration_security_floor(REPOSITORY_ROOT, ("20260825_0001",))


def test_manifest_requires_exactly_one_migration_head() -> None:
    """The manifest requires exactly one Alembic head."""
    body = _body()
    assert isinstance(body["source"], dict)
    body["source"]["migration_heads"] = ["20260825_0001", "20260825_0002"]
    with pytest.raises(BackupToolError, match="exactly one head"):
        BackupManifest.from_json(body)


@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_manifest_refuses_coercible_or_negative_row_counts(value: object) -> None:
    """Row counts must be real non-negative integers, not coercible strings."""
    body = _body()
    assert isinstance(body["tables"], list)
    body["tables"][0]["rows"] = value  # type: ignore[index]
    with pytest.raises(BackupToolError, match="must be an integer"):
        BackupManifest.from_json(body)


def test_manifest_refuses_unsorted_or_duplicate_tables() -> None:
    """The manifest refuses unsorted or duplicate table entries."""
    body = _body()
    assert isinstance(body["tables"], list)
    body["tables"] = list(reversed(body["tables"]))
    with pytest.raises(BackupToolError, match="sorted and unique"):
        BackupManifest.from_json(body)


def test_manifest_refuses_malformed_or_duplicate_sequence_state() -> None:
    """The manifest refuses malformed or duplicate sequence state."""
    body = _body()
    assert isinstance(body["sequences"], list)
    body["sequences"][0]["is_called"] = 1  # type: ignore[index]
    with pytest.raises(BackupToolError, match="boolean fields"):
        BackupManifest.from_json(body)

    body = _body()
    assert isinstance(body["sequences"], list)
    body["sequences"].append(dict(body["sequences"][0]))
    with pytest.raises(BackupToolError, match="sequences must be sorted and unique"):
        BackupManifest.from_json(body)

    body = _body()
    assert isinstance(body["tables"], list)
    body["tables"].append(dict(body["tables"][0]))
    with pytest.raises(BackupToolError, match="sorted and unique"):
        BackupManifest.from_json(body)


def test_manifest_refuses_seed_floor_drift() -> None:
    """The manifest refuses seed floor drift."""
    body = _body()
    assert isinstance(body["seed_floor"], dict)
    body["seed_floor"]["public.roles"] = 999
    with pytest.raises(BackupToolError, match="does not match"):
        BackupManifest.from_json(body)


def test_manifest_refuses_old_seed_generation_below_security_floor() -> None:
    """The manifest refuses a seed generation below the security floor."""
    body = _body()
    assert isinstance(body["seed_floor"], dict)
    body["seed_floor"].pop("public.roles")
    body["seed_floor"].pop("public.permissions")
    body["seed_floor"].pop("public.role_permission_assignments")
    with pytest.raises(BackupToolError, match="required security floor"):
        BackupManifest.from_json(body)


def test_manifest_refuses_nonseed_tables_in_seed_floor() -> None:
    """The seed floor refuses non-seed tables."""
    body = _body()
    assert isinstance(body["seed_floor"], dict)
    body["seed_floor"]["public.org_units"] = 3
    with pytest.raises(BackupToolError, match="extra=.*public.org_units"):
        BackupManifest.from_json(body)


def test_manifest_requires_exact_two_artifacts_in_order() -> None:
    """The manifest requires exactly two artifacts in publication order."""
    body = _body()
    assert isinstance(body["artifacts"], list)
    body["artifacts"].reverse()
    with pytest.raises(BackupToolError, match="artifacts must be exactly"):
        BackupManifest.from_json(body)


def test_artifact_verification_rejects_tampering(tmp_path: Path) -> None:
    """Artifact verification rejects any tampering with the bytes."""
    run = tmp_path / "ums-database-backup-20260831T000000Z-1234abcd"
    manifest = _materialize(run)
    verify_artifacts(run, manifest)
    (run / DUMP_NAME).write_bytes(b"tampered")
    with pytest.raises(BackupToolError, match="integrity"):
        verify_artifacts(run, manifest)


def test_artifact_verification_rejects_a_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Artifact verification rejects a redirected path."""
    run = tmp_path / "ums-database-backup-20260831T000000Z-1234abcd"
    manifest = _materialize(run)
    dump = run / DUMP_NAME
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == dump)
    with pytest.raises(BackupToolError, match="not a regular file"):
        verify_artifacts(run, manifest)


def test_pinned_artifact_stream_survives_a_path_replacement(tmp_path: Path) -> None:
    """A pinned artifact stream survives a path replacement mid-read."""
    run = tmp_path / "ums-database-backup-20260831T000000Z-1234abcd"
    manifest = _materialize(run)
    original = (run / DUMP_NAME).read_bytes()
    replacement = tmp_path / "replacement.dump"
    replacement.write_bytes(b"unverified replacement")
    with open_verified_artifacts(run, manifest) as streams:
        try:
            os.replace(replacement, run / DUMP_NAME)
        except PermissionError:
            # Windows denies replacement while the verified handle is open;
            # POSIX retains the verified inode even when the path is replaced.
            pass
        assert streams[DUMP_NAME].read() == original


def test_output_directory_must_be_outside_repository(tmp_path: Path) -> None:
    """Backup output must live outside the repository."""
    with pytest.raises(BackupToolError, match="outside the repository"):
        resolve_output_directory(
            REPOSITORY_ROOT / "unsafe-backups", repository_root=REPOSITORY_ROOT
        )
    output = resolve_output_directory(tmp_path / "safe-backups", repository_root=REPOSITORY_ROOT)
    assert output.is_dir()
    assert (output / ".ums-database-backup-root").read_text(encoding="ascii") == (
        "ums-database-backup-root/v1\n"
    )
    private_run = new_staging_directory(
        output, run_name="ums-database-backup-20260831T000000Z-abcdef12"
    )
    require_owner_only_directory(private_run)


def test_windows_acl_contract_requires_current_owner_and_explicit_protection() -> None:
    """The Windows ACL contract needs the current owner and explicit protection."""
    source = inspect.getsource(require_owner_only_directory)
    verifier = inspect.getsource(filesystem._verify_windows_owner_acl)
    enforcer = inspect.getsource(filesystem._restrict_windows_owner_acl)
    assert "_verify_windows_owner_acl" in source
    assert "$observed.GetOwner(" in verifier
    assert "$observedOwnerSid -ne $sid.Value" in verifier
    assert "-not $observed.AreAccessRulesProtected -or $rules[0].IsInherited" in verifier
    assert "$observed.GetOwner(" in enforcer
    assert "$observedOwnerSid -ne $sid.Value" in enforcer


def test_coordinated_bundle_mode_requires_preexisting_owner_only_directory(
    tmp_path: Path,
) -> None:
    """Coordinated bundle mode needs a pre-existing owner-only directory."""
    missing = tmp_path / "missing-bundle"
    with pytest.raises(BackupToolError, match="must already exist"):
        resolve_output_directory(
            missing,
            repository_root=REPOSITORY_ROOT,
            coordinated_bundle=True,
        )

    unprotected = tmp_path / "unprotected-bundle"
    unprotected.mkdir()
    with pytest.raises(BackupToolError, match="owner-only|mode 0700"):
        resolve_output_directory(
            unprotected,
            repository_root=REPOSITORY_ROOT,
            coordinated_bundle=True,
        )

    protected = resolve_output_directory(
        tmp_path / "protected-bundle", repository_root=REPOSITORY_ROOT
    )
    assert (
        resolve_output_directory(
            protected,
            repository_root=REPOSITORY_ROOT,
            coordinated_bundle=True,
        )
        == protected
    )
    (protected / "ums-database-backup-20260831T000000Z-1234abcd").mkdir()
    with pytest.raises(BackupToolError, match="already contains"):
        resolve_output_directory(
            protected,
            repository_root=REPOSITORY_ROOT,
            coordinated_bundle=True,
        )


def test_existing_output_directory_must_not_contain_foreign_entries(tmp_path: Path) -> None:
    """An existing output directory must not contain foreign entries."""
    output = tmp_path / "not-dedicated"
    output.mkdir()
    (output / "personal.txt").write_text("do not touch", encoding="utf-8")
    with pytest.raises(BackupToolError, match="root marker|not dedicated"):
        resolve_output_directory(output, repository_root=REPOSITORY_ROOT)
    assert (output / "personal.txt").read_text(encoding="utf-8") == "do not touch"


def test_existing_output_directory_rejects_non_ascii_root_marker(tmp_path: Path) -> None:
    """An existing output directory rejects a non-ASCII root marker."""
    output = tmp_path / "not-a-valid-backup-root"
    output.mkdir()
    (output / ".ums-database-backup-root").write_bytes(b"\xff")
    with pytest.raises(BackupToolError, match="root marker is unreadable") as raised:
        resolve_output_directory(output, repository_root=REPOSITORY_ROOT)
    assert raised.value.exit_code == 2


def test_trusted_directory_identity_rejects_path_replacement(tmp_path: Path) -> None:
    """Trusted directory identity rejects a path replaced after opening."""
    output = resolve_output_directory(
        tmp_path / "identity-backups", repository_root=REPOSITORY_ROOT
    )
    trusted = new_staging_directory(
        output, run_name="ums-database-backup-20260831T000000Z-01020304"
    )
    expected = capture_trusted_directory_identity(trusted)
    require_trusted_directory_identity(trusted, expected)

    displaced = output / "displaced-partial"
    trusted.rename(displaced)
    trusted.mkdir(mode=0o700)
    if os.name == "nt":
        filesystem._restrict_windows_owner_acl(trusted)
    with pytest.raises(BackupToolError, match="identity changed"):
        require_trusted_directory_identity(trusted, expected)


def test_output_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    """The output lock is exclusive and released on exit."""
    output = tmp_path / "backups"
    output.mkdir()
    with (
        exclusive_output_lock(output),
        pytest.raises(BackupToolError, match="already exists"),
        exclusive_output_lock(output),
    ):
        raise AssertionError("unreachable")
    assert not (output / ".ums-database-backup.lock").exists()


def test_output_lock_does_not_remove_replaced_ownership_token(tmp_path: Path) -> None:
    """The output lock never removes a replaced ownership token."""
    output = tmp_path / "backups"
    output.mkdir()
    with pytest.raises(BackupToolError, match="ownership changed"), exclusive_output_lock(output):
        (output / ".ums-database-backup.lock" / "owner-token").write_text(
            "foreign", encoding="ascii"
        )
    assert (output / ".ums-database-backup.lock").exists()


def test_output_lock_does_not_remove_a_replacement_lock_with_the_same_token(
    tmp_path: Path,
) -> None:
    """The output lock never removes a same-token replacement lock."""
    output = tmp_path / "backups"
    output.mkdir()
    lock = output / ".ums-database-backup.lock"
    displaced = output / "displaced-lock"
    with pytest.raises(BackupToolError, match="identity changed"), exclusive_output_lock(output):
        token = (lock / "owner-token").read_text(encoding="ascii")
        lock.rename(displaced)
        lock.mkdir()
        (lock / "owner-token").write_text(token, encoding="ascii")
    assert lock.is_dir()
    assert (lock / "owner-token").is_file()
    assert displaced.is_dir()


def test_atomic_publication_exposes_only_complete_directory(tmp_path: Path) -> None:
    """Publication exposes only a complete directory."""
    output = tmp_path / "backups"
    output.mkdir()
    run_name = "ums-database-backup-20260831T000000Z-1234abcd"
    staging = new_staging_directory(output, run_name=run_name)
    manifest = _materialize(tmp_path / "source")
    for name in (DUMP_NAME, ROLES_NAME):
        (staging / name).write_bytes((tmp_path / "source" / name).read_bytes())
    write_manifest(staging, manifest)
    destination = output / run_name
    publish(staging, destination, manifest=manifest)
    assert destination.is_dir()
    assert not staging.exists()
    assert BackupManifest.load(destination / MANIFEST_NAME) == manifest


def test_publication_refuses_missing_or_modified_members(tmp_path: Path) -> None:
    """Publication refuses missing or modified members."""
    output = tmp_path / "backups"
    output.mkdir()
    manifest = _materialize(tmp_path / "source")

    missing = new_staging_directory(
        output, run_name="ums-database-backup-20260831T000000Z-1111aaaa"
    )
    destination = output / "ums-database-backup-20260831T000000Z-1111aaaa"
    with pytest.raises(BackupToolError, match="exactly the required"):
        publish(missing, destination, manifest=manifest)
    assert not destination.exists()

    modified = new_staging_directory(
        output, run_name="ums-database-backup-20260831T000000Z-2222bbbb"
    )
    for name in (DUMP_NAME, ROLES_NAME, MANIFEST_NAME):
        (modified / name).write_bytes((tmp_path / "source" / name).read_bytes())
    (modified / ROLES_NAME).write_text("changed", encoding="utf-8")
    destination = output / "ums-database-backup-20260831T000000Z-2222bbbb"
    with pytest.raises(BackupToolError, match="integrity"):
        publish(modified, destination, manifest=manifest)
    assert not destination.exists()


def test_backup_history_binds_cluster_and_database_identity(tmp_path: Path) -> None:
    """Backup history binds the cluster and database identity."""
    output = tmp_path / "backups"
    output.mkdir()
    first = DatabaseIdentity(system_identifier="111", database="ums")
    _materialize(output / "ums-database-backup-20260831T000000Z-1234abcd", identity=first)
    require_matching_backup_history(output, first)
    with pytest.raises(BackupToolError, match="different database identity"):
        require_matching_backup_history(
            output, DatabaseIdentity(system_identifier="222", database="ums")
        )


def test_backup_history_fails_closed_on_completed_looking_corruption(tmp_path: Path) -> None:
    """Backup history fails closed on corruption that looks completed."""
    output = tmp_path / "backups"
    run = output / "ums-database-backup-20260831T000000Z-1234abcd"
    run.mkdir(parents=True)
    (run / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(BackupToolError):
        require_matching_backup_history(
            output, DatabaseIdentity(system_identifier="111", database="ums")
        )


def test_test_helpers_never_hide_seed_counts_behind_manifest_coercion() -> None:
    """Test helpers never hide seed counts behind manifest coercion."""
    manifest = BackupManifest.from_json(_body())
    altered = replace(manifest, seed_floor={**manifest.seed_floor, "public.roles": True})
    with pytest.raises(BackupToolError, match="must be an integer"):
        BackupManifest.from_json(altered.to_json())
