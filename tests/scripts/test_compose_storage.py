from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "compose_storage.py"


def _load_script():
    """Load the standalone operator script without adding scripts/ as a package."""
    spec = importlib.util.spec_from_file_location("ums_compose_storage", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


storage = _load_script()


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """Create a repository and storage root pair for a test."""
    repository = tmp_path / "repo"
    repository.mkdir()
    safe_root = repository / "data"
    return repository, safe_root


@pytest.mark.parametrize("raw_path", ["", " ", ".", "./", ".\\", ".."])
def test_prepare_rejects_empty_dot_and_workspace_relative_paths(tmp_path, raw_path):
    """Reject ambiguous values before any directory is approved."""
    repository, safe_root = _layout(tmp_path)

    with pytest.raises(storage.StorageContractError):
        storage.prepare_storage(raw_path, safe_root=safe_root, repository_root=repository)


def test_prepare_rejects_filesystem_repo_safe_root_home_and_outside(tmp_path):
    """Require a strict child of the explicitly approved safe root."""
    repository, safe_root = _layout(tmp_path)
    root = Path(repository.anchor)
    rejected = [root, repository, safe_root, Path.home(), tmp_path / "outside"]

    for candidate in rejected:
        with pytest.raises(storage.StorageContractError):
            storage.prepare_storage(
                str(candidate),
                safe_root=safe_root,
                repository_root=repository,
            )


def test_prepare_requires_direct_child_of_approved_root(tmp_path):
    """A broad approved root cannot authorize an arbitrary deep typo path."""
    repository, safe_root = _layout(tmp_path)

    with pytest.raises(storage.StorageContractError, match="direct child"):
        storage.prepare_storage(
            str(safe_root / "misspelled" / "ums"),
            safe_root=safe_root,
            repository_root=repository,
        )


def test_check_rejects_docker_created_empty_typo_until_explicitly_prepared(tmp_path):
    """An empty auto-created directory is not trusted merely because it exists."""
    repository, safe_root = _layout(tmp_path)
    typo = safe_root / "typo"
    typo.mkdir(parents=True)

    with pytest.raises(storage.StorageContractError, match="contract is missing"):
        storage.check_host_storage(str(typo), repository_root=repository)

    prepared = storage.prepare_storage(
        str(typo),
        safe_root=safe_root,
        repository_root=repository,
    )
    assert prepared == typo.resolve()
    assert storage.check_host_storage(str(typo), repository_root=repository) == prepared


def test_prepare_refuses_nonempty_unmarked_directory(tmp_path):
    """Do not claim or mutate an existing directory whose ownership is unknown."""
    repository, safe_root = _layout(tmp_path)
    target = safe_root / "occupied"
    target.mkdir(parents=True)
    (target / "operator-data.txt").write_text("do not touch", encoding="utf-8")

    with pytest.raises(storage.StorageContractError, match="not empty"):
        storage.prepare_storage(str(target), safe_root=safe_root, repository_root=repository)

    assert (target / "operator-data.txt").read_text(encoding="utf-8") == "do not touch"


def test_marker_is_bound_to_exact_canonical_path(tmp_path):
    """Copying another target's marker cannot accidentally authorize a typo path."""
    repository, safe_root = _layout(tmp_path)
    first = storage.prepare_storage(
        str(safe_root / "first"),
        safe_root=safe_root,
        repository_root=repository,
    )
    second = safe_root / "second"
    second.mkdir()
    (second / storage.MARKER_FILENAME).write_bytes((first / storage.MARKER_FILENAME).read_bytes())

    with pytest.raises(storage.StorageContractError, match="different host path"):
        storage.check_host_storage(str(second), repository_root=repository)


def test_missing_marker_fails_before_root_mutation(tmp_path, monkeypatch):
    """The container guard runs before mkdir, chown, or chmod on the bind."""
    mount = tmp_path / "unmarked"
    mount.mkdir()
    mutations: list[tuple[str, object]] = []
    monkeypatch.setattr(
        os,
        "chown",
        lambda *args: mutations.append(("chown", args)),
        raising=False,
    )
    monkeypatch.setattr(os, "chmod", lambda *args: mutations.append(("chmod", args)))

    with pytest.raises(storage.StorageContractError, match="contract is missing"):
        storage.initialize_container_storage(mount, app_user="app")

    assert mutations == []
    assert list(mount.iterdir()) == []


def test_unsafe_configured_path_fails_before_root_mutation_even_with_marker(tmp_path, monkeypatch):
    """A copied or fabricated marker cannot authorize dot through direct Compose up."""
    mount = tmp_path / "forged"
    mount.mkdir()
    (mount / storage.MARKER_FILENAME).write_text(
        json.dumps(
            {
                "canonical_path": str(tmp_path / "somewhere-else"),
                "configured_path_key": ".",
                "contract": storage.CONTRACT_NAME,
                "safe_root": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    mutations: list[tuple[str, object]] = []
    monkeypatch.setattr(
        os,
        "chown",
        lambda *args: mutations.append(("chown", args)),
        raising=False,
    )
    monkeypatch.setattr(os, "chmod", lambda *args: mutations.append(("chmod", args)))

    with pytest.raises(storage.StorageContractError, match="root or repository/workspace"):
        storage.initialize_container_storage(
            mount,
            app_user="app",
            configured_host_path=".",
        )

    assert mutations == []
    assert [path.name for path in mount.iterdir()] == [storage.MARKER_FILENAME]


def test_mounted_marker_is_bound_to_configured_host_path(tmp_path):
    """Copying a valid marker to another existing bind fails inside init too."""
    repository, safe_root = _layout(tmp_path)
    source = storage.prepare_storage(
        str(safe_root / "source"),
        safe_root=safe_root,
        repository_root=repository,
    )
    copied = safe_root / "copied"
    copied.mkdir()
    (copied / storage.MARKER_FILENAME).write_bytes((source / storage.MARKER_FILENAME).read_bytes())

    with pytest.raises(storage.StorageContractError, match="does not match"):
        storage._validate_mounted_marker(
            copied,
            configured_host_path=str(copied),
        )


def test_direct_container_init_without_host_receipt_fails_before_mutation(tmp_path, monkeypatch):
    """A direct Compose init cannot skip the invocation-bound host preflight."""
    repository, safe_root = _layout(tmp_path)
    target = storage.prepare_storage(
        str(safe_root / "ums"),
        safe_root=safe_root,
        repository_root=repository,
    )
    mutations: list[tuple[str, object]] = []
    monkeypatch.delenv(storage.HOST_CANONICAL_ENV, raising=False)
    monkeypatch.setattr(
        os,
        "chown",
        lambda *args: mutations.append(("chown", args)),
        raising=False,
    )
    monkeypatch.setattr(os, "chmod", lambda *args: mutations.append(("chmod", args)))

    with pytest.raises(storage.StorageContractError, match="compose wrapper"):
        storage.initialize_container_storage(
            target,
            app_user="app",
            configured_host_path=str(target),
        )

    monkeypatch.setenv(storage.HOST_CANONICAL_ENV, str(safe_root / "other"))
    with pytest.raises(storage.StorageContractError, match="does not match the marker"):
        storage.initialize_container_storage(
            target,
            app_user="app",
            configured_host_path=str(target),
        )

    assert mutations == []
    assert {path.name for path in target.iterdir()} == {storage.MARKER_FILENAME}


def test_compose_wrapper_validates_host_before_spawning(tmp_path):
    """Only a successful host canonical check may start the Compose subprocess."""
    repository, safe_root = _layout(tmp_path)
    raw_path = str(Path("data") / "ums")
    target = storage.prepare_storage(
        raw_path,
        safe_root=safe_root,
        repository_root=repository,
    )
    invocations: list[tuple[list[str], dict[str, object]]] = []

    class Result:
        """Command outcome captured by the fake command runner."""

        returncode = 17

    def runner(command, **kwargs):
        """Return a command runner that records the argv it is given."""
        invocations.append((command, kwargs))
        return Result()

    assert (
        storage.run_compose_with_preflight(
            raw_path,
            ["-p", "isolated", "up", "app"],
            repository_root=repository,
            runner=runner,
        )
        == 17
    )
    command, kwargs = invocations.pop()
    assert command == ["docker", "compose", "-p", "isolated", "up", "app"]
    assert kwargs["cwd"] == repository.resolve()
    assert kwargs["env"]["UMS_APP_DATA_HOST"] == str(target.resolve())
    assert kwargs["env"]["UMS_APP_DATA_HOST_CANONICAL"] == str(target.resolve())
    assert kwargs["env"][storage.HOST_CONFIGURED_ENV] == raw_path

    copied = safe_root / "copied"
    copied.mkdir()
    (copied / storage.MARKER_FILENAME).write_bytes((target / storage.MARKER_FILENAME).read_bytes())
    with pytest.raises(storage.StorageContractError, match="different host path"):
        storage.run_compose_with_preflight(
            str(copied),
            ["up", "app"],
            repository_root=repository,
            runner=runner,
        )
    assert invocations == []


def test_application_storage_gate_blocks_no_deps_start_without_readiness(tmp_path, monkeypatch):
    """Skipping Compose dependencies cannot skip the non-root application gate."""
    mount = tmp_path / "mount"
    mount.mkdir()
    for name in storage.STORAGE_DIRECTORIES:
        (mount / name).mkdir()
    monkeypatch.setenv(storage.HOST_PATH_ENV, "./data/ums")
    monkeypatch.setenv(storage.HOST_CANONICAL_ENV, str(tmp_path / "host"))

    with pytest.raises(storage.StorageContractError, match="contract is missing"):
        storage._validate_ready_storage(mount)

    ready = {
        "canonical_path": str(tmp_path / "host"),
        "configured_path_key": storage._configured_path_key("./data/ums"),
        "contract": storage.CONTRACT_NAME,
        "state": "initialized",
    }
    (mount / storage.READY_FILENAME).write_text(json.dumps(ready), encoding="utf-8")
    storage._validate_ready_storage(mount)

    (mount / storage.RESTORE_PENDING_FILENAME).write_text("{}", encoding="utf-8")
    with pytest.raises(storage.StorageContractError, match="not fully initialized"):
        storage._validate_ready_storage(mount)
    (mount / storage.RESTORE_PENDING_FILENAME).unlink()

    monkeypatch.delenv(storage.HOST_CANONICAL_ENV)
    with pytest.raises(storage.StorageContractError, match="canonical receipt"):
        storage._validate_ready_storage(mount)


def test_restore_journal_without_pending_blocks_init_before_mutation(tmp_path, monkeypatch):
    """A power-loss-shaped missing pending link cannot bypass restore adoption."""
    repository, safe_root = _layout(tmp_path)
    target = storage.prepare_storage(
        str(safe_root / "ums"),
        safe_root=safe_root,
        repository_root=repository,
    )
    (target / storage.RESTORE_JOURNAL_FILENAME).write_text("{}", encoding="utf-8")
    monkeypatch.setenv(storage.HOST_CANONICAL_ENV, str(target.resolve()))
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(storage, "_runtime_identity", lambda _user: (10001, 10001))
    mutations: list[tuple[str, object]] = []
    monkeypatch.setattr(
        os,
        "chown",
        lambda *args: mutations.append(("chown", args)),
        raising=False,
    )
    monkeypatch.setattr(os, "chmod", lambda *args: mutations.append(("chmod", args)))

    with pytest.raises(storage.StorageContractError, match="disagree"):
        storage.initialize_container_storage(
            target,
            app_user="app",
            configured_host_path=str(target),
        )

    assert mutations == []
    assert not (target / "artifacts").exists()
    assert not (target / "blobs").exists()


def test_failed_reinitialization_invalidates_stale_readiness(tmp_path, monkeypatch):
    """A prior readiness marker cannot survive a later failed init attempt."""
    repository, safe_root = _layout(tmp_path)
    target = _seed_storage(repository, safe_root)
    marker = json.loads((target / storage.MARKER_FILENAME).read_text(encoding="utf-8"))
    ready = target / storage.READY_FILENAME
    ready.write_text(json.dumps(storage._ready_payload(marker)), encoding="utf-8")
    monkeypatch.setenv(storage.HOST_CANONICAL_ENV, str(target.resolve()))
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(storage, "_runtime_identity", lambda _user: (10001, 10001))
    monkeypatch.setattr(os, "chown", lambda *_args: None, raising=False)
    monkeypatch.setattr(
        os,
        "chmod",
        lambda *_args: (_ for _ in ()).throw(OSError("injected chmod failure")),
    )

    with pytest.raises(OSError, match="injected chmod failure"):
        storage.initialize_container_storage(
            target,
            app_user="app",
            configured_host_path=str(target),
        )

    assert not ready.exists()


def test_completed_restore_cleanup_retries_after_each_removal_boundary(tmp_path, monkeypatch):
    """Ready-first cleanup tolerates interruption after stage or journal removal."""

    def completed_state(root: Path) -> tuple[Path, Path, Path]:
        """Create a storage tree that already reached the ready state."""
        root.mkdir()
        for name in storage.STORAGE_DIRECTORIES:
            (root / name).mkdir()
        stage = root / f"{storage.RESTORE_STAGE_PREFIX}cleanup"
        stage.mkdir()
        digest = "a" * 64
        pending = root / storage.RESTORE_PENDING_FILENAME
        pending.write_text(
            json.dumps(
                storage._pending_restore_payload(
                    digest,
                    "b" * 64,
                    "file-store",
                    None,
                )
            ),
            encoding="utf-8",
        )
        journal = storage._restore_journal_payload(
            digest,
            "b" * 64,
            "file-store",
            None,
            stage.name,
        )
        journal["published"] = list(storage.STORAGE_DIRECTORIES)
        journal["state"] = "complete"
        journal_path = root / storage.RESTORE_JOURNAL_FILENAME
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        return stage, journal_path, pending

    first = tmp_path / "after-stage"
    stage, journal_path, pending = completed_state(first)
    real_unlink = storage._unlink_and_sync
    interrupted = False

    def interrupt_journal(path):
        """Abort initialization right after the journal lands."""
        nonlocal interrupted
        if Path(path) == journal_path and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return real_unlink(Path(path))

    monkeypatch.setattr(storage, "_unlink_and_sync", interrupt_journal)
    with pytest.raises(KeyboardInterrupt):
        storage._finish_pending_restore_initialization(first)
    assert not stage.exists()
    assert journal_path.exists()
    assert pending.exists()
    monkeypatch.setattr(storage, "_unlink_and_sync", real_unlink)
    storage._finish_pending_restore_initialization(first)
    assert not journal_path.exists()
    assert not pending.exists()

    second = tmp_path / "after-journal"
    stage, journal_path, pending = completed_state(second)
    interrupted = False

    def interrupt_pending(path):
        """Abort initialization right after the pending marker lands."""
        nonlocal interrupted
        if Path(path) == pending and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return real_unlink(Path(path))

    monkeypatch.setattr(storage, "_unlink_and_sync", interrupt_pending)
    with pytest.raises(KeyboardInterrupt):
        storage._finish_pending_restore_initialization(second)
    assert not stage.exists()
    assert not journal_path.exists()
    assert pending.exists()
    monkeypatch.setattr(storage, "_unlink_and_sync", real_unlink)
    storage._finish_pending_restore_initialization(second)
    assert not pending.exists()


def test_prepare_rejects_symlink_or_junction_target(tmp_path):
    """A redirected target cannot escape after lexical containment validation."""
    repository, safe_root = _layout(tmp_path)
    safe_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    redirect = safe_root / "redirect"
    try:
        redirect.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this Windows account may not create symlinks")

    with pytest.raises(storage.StorageContractError, match="link"):
        storage.prepare_storage(
            str(redirect / "ums"),
            safe_root=safe_root,
            repository_root=repository,
        )


def _seed_storage(repository: Path, safe_root: Path, name: str = "ums") -> Path:
    """Create an already-initialized storage tree."""
    target = storage.prepare_storage(
        str(safe_root / name),
        safe_root=safe_root,
        repository_root=repository,
    )
    for directory in storage.STORAGE_DIRECTORIES:
        (target / directory).mkdir()
    (target / "artifacts" / "finance.xlsx").write_bytes(b"finance")
    (target / "blobs" / "raw.json").write_bytes(b'{"source": true}')
    return target


def _recovery_members(bundle: Path, archive: Path) -> list[Path]:
    """List the bundle members needed to drive a recovery run."""
    database_run = bundle / "ums-database-backup-20260831T000000Z-1234abcd"
    database_run.mkdir(exist_ok=True)
    database_dump = database_run / "database.dump"
    roles_dump = database_run / "roles.sql"
    database_manifest = database_run / "database-manifest.json"
    git_revision = bundle / "git-revision.txt"
    service_record = bundle / "running-services.txt"
    database_dump.write_bytes(b"postgres-custom-format")
    roles_dump.write_text(
        "CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n",
        encoding="utf-8",
    )
    database_manifest.write_text('{"schema":"ums-database-backup/v2"}\n', encoding="utf-8")
    git_revision.write_text("26bf0256c64389a77d1b1053ea6aeb1e7c0bc994\n", encoding="utf-8")
    service_record.write_text("postgres\nredis\n", encoding="utf-8")
    return [
        archive,
        database_dump,
        roles_dump,
        database_manifest,
        git_revision,
        service_record,
    ]


def test_archive_requires_stopped_writers_and_external_destination(tmp_path):
    """Refuse a live or worktree-local sensitive archive before publication."""
    repository, safe_root = _layout(tmp_path)
    target = _seed_storage(repository, safe_root)

    with pytest.raises(storage.StorageContractError, match="writers-stopped"):
        storage.create_artifact_archive(
            str(target),
            output=tmp_path / "bundle" / "app.tgz",
            writers_stopped=False,
            repository_root=repository,
        )
    with pytest.raises(storage.StorageContractError, match="outside the repository"):
        storage.create_artifact_archive(
            str(target),
            output=repository / "app.tgz",
            writers_stopped=True,
            repository_root=repository,
        )


def test_archive_manifest_verify_and_empty_target_restore_round_trip(tmp_path):
    """Seal and restore exact bytes only through an integrity-verified bundle."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root)
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )

    verified = storage.verify_bundle_manifest(manifest)
    assert set(verified) == storage.REQUIRED_RECOVERY_MEMBERS | {
        "ums-database-backup-20260831T000000Z-1234abcd/database.dump",
        "ums-database-backup-20260831T000000Z-1234abcd/roles.sql",
        "ums-database-backup-20260831T000000Z-1234abcd/database-manifest.json",
    }
    storage.verify_artifact_archive(archive)

    restore_target = storage.prepare_storage(
        str(safe_root / "restored"),
        safe_root=safe_root,
        repository_root=repository,
    )
    storage.restore_artifact_archive(
        str(restore_target),
        archive=archive,
        manifest=manifest,
        repository_root=repository,
    )

    assert (restore_target / "artifacts" / "finance.xlsx").read_bytes() == b"finance"
    assert (restore_target / "blobs" / "raw.json").read_bytes() == b'{"source": true}'
    assert (restore_target / storage.RESTORE_PENDING_FILENAME).is_file()
    if os.name != "nt":
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


def test_manifest_detects_tampering(tmp_path):
    """Any post-manifest size or hash change blocks recovery."""
    repository, _ = _layout(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    dump = bundle / "ums-database.dump"
    dump.write_bytes(b"before")
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        [dump],
        profile=storage.GENERIC_BACKUP_PROFILE,
        repository_root=repository,
    )
    dump.write_bytes(b"after")

    with pytest.raises(storage.StorageContractError, match="checksum mismatch"):
        storage.verify_bundle_manifest(manifest)


def test_archive_verifier_rejects_path_traversal(tmp_path):
    """A checksum-valid but structurally malicious tar cannot reach extraction."""
    archive = tmp_path / "malicious.tgz"
    payload = tmp_path / "payload"
    payload.write_bytes(b"owned")
    with tarfile.open(archive, mode="w:gz") as handle:
        handle.add(payload, arcname="../outside")

    with pytest.raises(storage.StorageContractError, match="unsafe archive path"):
        storage.verify_artifact_archive(archive)


def test_archive_verifier_requires_directory_typed_storage_roots(tmp_path):
    """Regular files named artifacts/blobs cannot satisfy the archive contract."""
    archive = tmp_path / "fake-roots.tgz"
    with tarfile.open(archive, mode="w:gz") as handle:
        for name in storage.STORAGE_DIRECTORIES:
            member = tarfile.TarInfo(name)
            member.size = 0
            handle.addfile(member, io.BytesIO())

    with pytest.raises(storage.StorageContractError, match="root must be a directory"):
        storage.verify_artifact_archive(archive)

    implicit = tmp_path / "implicit-roots.tgz"
    with tarfile.open(implicit, mode="w:gz") as handle:
        for name in storage.STORAGE_DIRECTORIES:
            member = tarfile.TarInfo(f"{name}/file.txt")
            member.size = 1
            handle.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(storage.StorageContractError, match="explicit artifacts and blobs"):
        storage.verify_artifact_archive(implicit)


def test_compose_recovery_manifest_rejects_incomplete_bundle(tmp_path, monkeypatch):
    """Recovery cannot be complete without outer and structural DB records."""
    repository, _ = _layout(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    database_run = bundle / "ums-database-backup-20260831T000000Z-1234abcd"
    database_run.mkdir()
    for name in ("database.dump", "roles.sql"):
        (database_run / name).write_bytes(name.encode())
    (bundle / "ums-app-data.tgz").write_bytes(b"archive")

    with pytest.raises(storage.StorageContractError, match="structural database.*incomplete"):
        storage.create_bundle_manifest(
            bundle / "SHA256SUMS.json",
            [
                database_run / "database.dump",
                database_run / "roles.sql",
                bundle / "ums-app-data.tgz",
            ],
            repository_root=repository,
        )

    generic = storage.create_bundle_manifest(
        bundle / "generic.json",
        [database_run / "database.dump"],
        profile=storage.GENERIC_BACKUP_PROFILE,
        repository_root=repository,
    )
    payload = json.loads(generic.read_text(encoding="utf-8"))
    payload["profile"] = storage.COMPOSE_RECOVERY_PROFILE
    generic.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(storage.StorageContractError, match="incomplete"):
        storage.verify_bundle_manifest(
            generic,
            required_profile=storage.COMPOSE_RECOVERY_PROFILE,
        )

    recovery_members = _recovery_members(bundle, bundle / "ums-app-data.tgz")
    with pytest.raises(storage.StorageContractError, match="gcs-snapshot.json"):
        storage.create_bundle_manifest(
            bundle / "gcs-missing-snapshot.json",
            recovery_members,
            blob_backend="gcs",
            repository_root=repository,
        )
    snapshot = bundle / storage.GCS_SNAPSHOT_MEMBER
    snapshot.write_text(
        json.dumps(
            {
                "bucket": "ums-raw",
                "objects": [
                    {
                        "crc32c": "not-a-checksum",
                        "generation": "123456",
                        "name": "connector/raw.json",
                    }
                ],
                "schema": "ums-gcs-snapshot-v1",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(storage.StorageContractError, match="canonical base64"):
        storage.create_bundle_manifest(
            bundle / "gcs-malformed-checksum.json",
            [*recovery_members, snapshot],
            blob_backend="gcs",
            expected_gcs_bucket="ums-raw",
            repository_root=repository,
        )

    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["objects"][0]["crc32c"] = "AQ=="
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(storage.StorageContractError, match="exactly four bytes"):
        storage.create_bundle_manifest(
            bundle / "gcs-short-checksum.json",
            [*recovery_members, snapshot],
            blob_backend="gcs",
            expected_gcs_bucket="ums-raw",
            repository_root=repository,
        )

    snapshot.write_text(
        json.dumps(
            {
                "bucket": "ums-raw",
                "objects": [
                    {
                        "crc32c": "ImIEBA==",
                        "generation": "123456",
                        "name": "connector/raw.json",
                    }
                ],
                "schema": "ums-gcs-snapshot-v1",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(storage.StorageContractError, match="configured bucket"):
        storage.create_bundle_manifest(
            bundle / "gcs-wrong-bucket.json",
            [*recovery_members, snapshot],
            blob_backend="gcs",
            expected_gcs_bucket="different-bucket",
            repository_root=repository,
        )
    gcs_manifest = storage.create_bundle_manifest(
        bundle / "gcs.json",
        [*recovery_members, snapshot],
        blob_backend="gcs",
        expected_gcs_bucket="ums-raw",
        repository_root=repository,
    )
    with pytest.raises(storage.StorageContractError, match="configured bucket"):
        storage.verify_bundle_manifest(
            gcs_manifest,
            required_profile=storage.COMPOSE_RECOVERY_PROFILE,
            required_blob_backend="gcs",
            expected_gcs_bucket="different-bucket",
        )
    assert storage.GCS_SNAPSHOT_MEMBER in storage.verify_bundle_manifest(
        gcs_manifest,
        required_profile=storage.COMPOSE_RECOVERY_PROFILE,
        required_blob_backend="gcs",
        expected_gcs_bucket="ums-raw",
    )

    wrong_snapshot_payload = {
        "bucket": "wrong-bucket",
        "objects": [
            {
                "crc32c": "ImIEBA==",
                "generation": "111",
                "name": "trusted.json",
            }
        ],
        "schema": "ums-gcs-snapshot-v1",
    }
    snapshot.write_text(json.dumps(wrong_snapshot_payload), encoding="utf-8")
    wrong_snapshot_manifest = storage.create_bundle_manifest(
        bundle / "gcs-wrong-snapshot-contract.json",
        [*recovery_members, snapshot],
        blob_backend="gcs",
        expected_gcs_bucket="wrong-bucket",
        repository_root=repository,
    )
    manifested_snapshot_digest = storage._sha256(snapshot)
    replacement_payload = {
        "bucket": "ums-raw",
        "objects": [
            {
                "crc32c": "ImIEBA==",
                "generation": "999",
                "name": "unmanifested.json",
            }
        ],
        "schema": "ums-gcs-snapshot-v1",
    }
    real_validate = storage._validate_gcs_snapshot_payload

    def swap_snapshot_before_semantic_validation(payload, *, expected_bucket):
        """Swap the GCS snapshot before semantic validation runs."""
        snapshot.write_text(json.dumps(replacement_payload), encoding="utf-8")
        return real_validate(payload, expected_bucket=expected_bucket)

    monkeypatch.setattr(
        storage,
        "_validate_gcs_snapshot_payload",
        swap_snapshot_before_semantic_validation,
    )
    with pytest.raises(storage.StorageContractError, match="configured bucket"):
        storage.verify_bundle_manifest(
            wrong_snapshot_manifest,
            required_profile=storage.COMPOSE_RECOVERY_PROFILE,
            required_blob_backend="gcs",
            expected_gcs_bucket="ums-raw",
        )
    assert storage._sha256(snapshot) != manifested_snapshot_digest


def test_restore_cli_accepts_explicit_gcs_contract_before_target_mutation(tmp_path, monkeypatch):
    """Restore receives the same backend and bucket boundary as manifest verification."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    snapshot = bundle / storage.GCS_SNAPSHOT_MEMBER
    snapshot.write_text(
        json.dumps(
            {
                "bucket": "ums-raw",
                "objects": [
                    {
                        "crc32c": "ImIEBA==",
                        "generation": "123456",
                        "name": "connector/raw.json",
                    }
                ],
                "schema": "ums-gcs-snapshot-v1",
            }
        ),
        encoding="utf-8",
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        [*_recovery_members(bundle, archive), snapshot],
        blob_backend="gcs",
        expected_gcs_bucket="ums-raw",
        repository_root=repository,
    )
    wrong_target = storage.prepare_storage(
        str(safe_root / "wrong-target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    with pytest.raises(storage.StorageContractError, match="configured bucket"):
        storage.restore_artifact_archive(
            str(wrong_target),
            archive=archive,
            manifest=manifest,
            blob_backend="gcs",
            expected_gcs_bucket="wrong-bucket",
            repository_root=repository,
        )
    assert not (wrong_target / storage.RESTORE_JOURNAL_FILENAME).exists()
    assert not list(wrong_target.glob(f"{storage.RESTORE_STAGE_PREFIX}*"))

    wrong_backend_target = storage.prepare_storage(
        str(safe_root / "wrong-backend-target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    with pytest.raises(storage.StorageContractError, match="must be file-store, not gcs"):
        storage.restore_artifact_archive(
            str(wrong_backend_target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )
    assert not (wrong_backend_target / storage.RESTORE_JOURNAL_FILENAME).exists()
    assert not list(wrong_backend_target.glob(f"{storage.RESTORE_STAGE_PREFIX}*"))

    missing_bucket_target = storage.prepare_storage(
        str(safe_root / "missing-bucket-target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    monkeypatch.setenv("UMS_GCS_BUCKET", "ums-raw")
    with pytest.raises(storage.StorageContractError, match="explicit expected bucket"):
        storage.restore_artifact_archive(
            str(missing_bucket_target),
            archive=archive,
            manifest=manifest,
            blob_backend="gcs",
            repository_root=repository,
        )
    assert not (missing_bucket_target / storage.RESTORE_JOURNAL_FILENAME).exists()

    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    monkeypatch.setenv("UMS_BLOB_BACKEND", "file-store")
    monkeypatch.setenv("UMS_GCS_BUCKET", "environment-wrong-bucket")
    assert (
        storage.main(
            [
                "restore-artifacts",
                "--path",
                str(target),
                "--archive",
                str(archive),
                "--manifest",
                str(manifest),
                "--blob-backend",
                "gcs",
                "--gcs-bucket",
                "ums-raw",
            ]
        )
        == 0
    )
    assert (target / "artifacts" / "finance.xlsx").read_bytes() == b"finance"
    journal = json.loads((target / storage.RESTORE_JOURNAL_FILENAME).read_text(encoding="utf-8"))
    assert journal["blob_backend"] == "gcs"
    assert journal["gcs_bucket"] == "ums-raw"

    monkeypatch.setenv("UMS_BLOB_BACKEND", "gcs")
    monkeypatch.setenv("UMS_GCS_BUCKET", "ums-raw")
    parsed = storage._build_parser().parse_args(
        [
            "restore-artifacts",
            "--path",
            str(target),
            "--archive",
            str(archive),
            "--manifest",
            str(manifest),
        ]
    )
    assert parsed.blob_backend == "gcs"
    assert parsed.gcs_bucket == "ums-raw"


def test_restore_journal_precedes_stage_creation_and_missing_stage_retries(tmp_path, monkeypatch):
    """A crash after durable intent but before mkdir leaves a resumable target."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_stage = storage._stage_verified_restore

    def interrupt_before_stage(*_args, **_kwargs):
        """Abort the restore before the stage directory is created."""
        raise KeyboardInterrupt

    monkeypatch.setattr(storage, "_stage_verified_restore", interrupt_before_stage)
    with pytest.raises(KeyboardInterrupt):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )
    journal = json.loads((target / storage.RESTORE_JOURNAL_FILENAME).read_text(encoding="utf-8"))
    assert journal["state"] == "staging"
    assert not (target / journal["stage"]).exists()

    monkeypatch.setattr(storage, "_stage_verified_restore", real_stage)
    storage.restore_artifact_archive(
        str(target),
        archive=archive,
        manifest=manifest,
        repository_root=repository,
    )
    assert (target / "blobs" / "raw.json").is_file()


def test_restore_retry_discards_partial_journaled_extraction(tmp_path, monkeypatch):
    """A mid-file crash cannot strand or publish a partial staging tree."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_copy = storage.shutil.copyfileobj

    def interrupt_copy(source_handle, output, *, length):
        """Abort the restore partway through copying the archive."""
        output.write(source_handle.read(1))
        raise KeyboardInterrupt

    monkeypatch.setattr(storage.shutil, "copyfileobj", interrupt_copy)
    with pytest.raises(KeyboardInterrupt):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )
    journal = json.loads((target / storage.RESTORE_JOURNAL_FILENAME).read_text(encoding="utf-8"))
    stage = target / journal["stage"]
    assert journal["state"] == "staging"
    assert any(path.is_file() for path in stage.rglob("*"))

    monkeypatch.setattr(storage.shutil, "copyfileobj", real_copy)
    storage.restore_artifact_archive(
        str(target),
        archive=archive,
        manifest=manifest,
        repository_root=repository,
    )
    assert (target / "artifacts" / "finance.xlsx").read_bytes() == b"finance"
    assert (target / "blobs" / "raw.json").read_bytes() == b'{"source": true}'


def test_restore_retry_restarts_verified_stage_before_publishing_transition(tmp_path, monkeypatch):
    """A crash after extraction but before state transition remains resumable."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_atomic = storage._write_json_atomic

    def interrupt_transition(path, payload):
        """Abort the restore after the stage is written but before publication."""
        if (
            Path(path) == target / storage.RESTORE_JOURNAL_FILENAME
            and payload["state"] == "publishing"
        ):
            raise KeyboardInterrupt
        return real_atomic(Path(path), payload)

    monkeypatch.setattr(storage, "_write_json_atomic", interrupt_transition)
    with pytest.raises(KeyboardInterrupt):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )
    journal = json.loads((target / storage.RESTORE_JOURNAL_FILENAME).read_text(encoding="utf-8"))
    stage = target / journal["stage"]
    assert journal["state"] == "staging"
    assert stage.is_dir()
    assert not (target / "artifacts").exists()
    assert not (target / "blobs").exists()

    monkeypatch.setattr(storage, "_write_json_atomic", real_atomic)
    storage.restore_artifact_archive(
        str(target),
        archive=archive,
        manifest=manifest,
        repository_root=repository,
    )
    assert (target / "artifacts" / "finance.xlsx").is_file()


def test_restore_retry_rejects_different_complete_manifest_before_mutation(tmp_path, monkeypatch):
    """The same tar cannot resume under a different coordinated recovery set."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    members = _recovery_members(bundle, archive)
    first_manifest = storage.create_bundle_manifest(
        bundle / "first-manifest.json",
        members,
        repository_root=repository,
    )
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_stage = storage._stage_verified_restore
    monkeypatch.setattr(
        storage,
        "_stage_verified_restore",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=first_manifest,
            repository_root=repository,
        )
    journal_path = target / storage.RESTORE_JOURNAL_FILENAME
    journal_before = journal_path.read_bytes()

    (bundle / "running-services.txt").write_text("postgres\nredis\napp\n", encoding="utf-8")
    second_manifest = storage.create_bundle_manifest(
        bundle / "second-manifest.json",
        members,
        repository_root=repository,
    )
    monkeypatch.setattr(storage, "_stage_verified_restore", real_stage)
    with pytest.raises(storage.StorageContractError, match="different recovery contract"):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=second_manifest,
            repository_root=repository,
        )
    assert journal_path.read_bytes() == journal_before
    journal = json.loads(journal_before)
    assert not (target / journal["stage"]).exists()


def test_restore_uses_pinned_manifest_bytes_after_atomic_path_swap(tmp_path, monkeypatch):
    """Manifest path replacement cannot change the verified recovery identity."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    members = _recovery_members(bundle, archive)
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        members,
        repository_root=repository,
    )
    original_manifest_digest = storage._sha256(manifest)
    extra = bundle / "operator-note.txt"
    extra.write_text("different but valid manifest\n", encoding="utf-8")
    replacement_manifest = storage.create_bundle_manifest(
        bundle / "replacement-manifest.json",
        [*members, extra],
        repository_root=repository,
    )
    assert storage._sha256(replacement_manifest) != original_manifest_digest
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_verify = storage.verify_bundle_manifest

    def swap_paths_after_verification(*args, **kwargs):
        """Swap stage and target paths once verification has passed."""
        verified = real_verify(*args, **kwargs)
        os.replace(replacement_manifest, manifest)
        return verified

    monkeypatch.setattr(storage, "verify_bundle_manifest", swap_paths_after_verification)
    storage.restore_artifact_archive(
        str(target),
        archive=archive,
        manifest=manifest,
        repository_root=repository,
    )
    assert (target / "artifacts" / "finance.xlsx").read_bytes() == b"finance"
    assert (target / "blobs" / "raw.json").read_bytes() == b'{"source": true}'
    journal = json.loads((target / storage.RESTORE_JOURNAL_FILENAME).read_text(encoding="utf-8"))
    assert journal["manifest_sha256"] == original_manifest_digest
    assert journal["manifest_sha256"] != storage._sha256(manifest)


def test_restore_archive_path_swap_is_blocked_or_uses_pinned_handle(tmp_path, monkeypatch):
    """An atomic archive replacement cannot redirect extraction to unverified bytes."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    replacement_source = _seed_storage(repository, safe_root, "replacement-source")
    (replacement_source / "artifacts" / "finance.xlsx").write_bytes(b"replacement")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    replacement_archive = storage.create_artifact_archive(
        str(replacement_source),
        output=bundle / "replacement.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_verify = storage.verify_bundle_manifest

    def swap_archive_after_verification(*args, **kwargs):
        """Replace the archive after verification has passed."""
        verified = real_verify(*args, **kwargs)
        os.replace(replacement_archive, archive)
        return verified

    monkeypatch.setattr(storage, "verify_bundle_manifest", swap_archive_after_verification)
    if os.name == "nt":
        with pytest.raises(PermissionError):
            storage.restore_artifact_archive(
                str(target),
                archive=archive,
                manifest=manifest,
                repository_root=repository,
            )
        assert not (target / storage.RESTORE_JOURNAL_FILENAME).exists()
        return

    storage.restore_artifact_archive(
        str(target),
        archive=archive,
        manifest=manifest,
        repository_root=repository,
    )
    assert (target / "artifacts" / "finance.xlsx").read_bytes() == b"finance"


def test_restore_rejects_in_place_archive_change_after_verification(tmp_path, monkeypatch):
    """A held descriptor is rehashed before journal creation and target mutation."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    replacement_source = _seed_storage(repository, safe_root, "replacement-source")
    (replacement_source / "artifacts" / "finance.xlsx").write_bytes(b"replacement")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    replacement_archive = storage.create_artifact_archive(
        str(replacement_source),
        output=bundle / "replacement.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_verify = storage.verify_bundle_manifest

    def overwrite_archive_after_verification(*args, **kwargs):
        """Overwrite the published archive after verification has passed."""
        verified = real_verify(*args, **kwargs)
        archive.write_bytes(replacement_archive.read_bytes())
        return verified

    monkeypatch.setattr(storage, "verify_bundle_manifest", overwrite_archive_after_verification)
    with pytest.raises(storage.StorageContractError, match="changed after verification"):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )
    assert not (target / storage.RESTORE_JOURNAL_FILENAME).exists()
    assert not list(target.glob(f"{storage.RESTORE_STAGE_PREFIX}*"))


def test_restore_publication_rolls_back_and_retries_after_replace_error(tmp_path, monkeypatch):
    """An ordinary second-root replace error leaves no half-published target."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_replace = storage._durable_replace
    failed = False

    def fail_second_publication(source_path, destination_path):
        """Fail the publication of the second artifact."""
        nonlocal failed
        if (
            not failed
            and Path(source_path).name == "blobs"
            and Path(destination_path) == target / "blobs"
        ):
            failed = True
            raise OSError("injected publication failure")
        return real_replace(Path(source_path), Path(destination_path))

    monkeypatch.setattr(storage, "_durable_replace", fail_second_publication)
    with pytest.raises(storage.StorageContractError, match="rolled back"):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )

    assert not (target / "artifacts").exists()
    assert not (target / "blobs").exists()
    journal = json.loads((target / storage.RESTORE_JOURNAL_FILENAME).read_text(encoding="utf-8"))
    assert journal["state"] == "rolled-back"
    assert len(list(target.glob(f"{storage.RESTORE_STAGE_PREFIX}*"))) == 1

    monkeypatch.setattr(storage, "_durable_replace", real_replace)
    storage.restore_artifact_archive(
        str(target),
        archive=archive,
        manifest=manifest,
        repository_root=repository,
    )
    assert (target / "artifacts" / "finance.xlsx").read_bytes() == b"finance"
    assert (target / "blobs" / "raw.json").is_file()


def test_restore_publication_resumes_after_abrupt_second_replace(tmp_path, monkeypatch):
    """A crash-shaped interruption is inferred from journaled filesystem state."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_replace = storage._durable_replace
    interrupted = False

    def interrupt_second_publication(source_path, destination_path):
        """Abort the publication of the second artifact."""
        nonlocal interrupted
        if (
            not interrupted
            and Path(source_path).name == "blobs"
            and Path(destination_path) == target / "blobs"
        ):
            interrupted = True
            raise KeyboardInterrupt
        return real_replace(Path(source_path), Path(destination_path))

    monkeypatch.setattr(storage, "_durable_replace", interrupt_second_publication)
    with pytest.raises(KeyboardInterrupt):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )
    assert (target / "artifacts").is_dir()
    assert not (target / "blobs").exists()
    assert (target / storage.RESTORE_JOURNAL_FILENAME).is_file()

    monkeypatch.setattr(storage, "_durable_replace", real_replace)
    storage.restore_artifact_archive(
        str(target),
        archive=archive,
        manifest=manifest,
        repository_root=repository,
    )
    assert (target / "artifacts" / "finance.xlsx").is_file()
    assert (target / "blobs" / "raw.json").is_file()
    journal = json.loads((target / storage.RESTORE_JOURNAL_FILENAME).read_text(encoding="utf-8"))
    assert journal["state"] == "complete"
    assert (target / storage.RESTORE_PENDING_FILENAME).is_file()


def test_restore_retry_rejects_truncated_journaled_stage(tmp_path, monkeypatch):
    """A crash-surviving journal cannot publish damaged staged bytes."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_replace = storage._durable_replace

    def interrupt_first_publication(source_path, destination_path):
        """Abort the publication of the first artifact."""
        if Path(destination_path) == target / "artifacts":
            raise KeyboardInterrupt
        return real_replace(Path(source_path), Path(destination_path))

    monkeypatch.setattr(storage, "_durable_replace", interrupt_first_publication)
    with pytest.raises(KeyboardInterrupt):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )
    stage = next(target.glob(f"{storage.RESTORE_STAGE_PREFIX}*"))
    (stage / "artifacts" / "finance.xlsx").write_bytes(b"truncated")

    monkeypatch.setattr(storage, "_durable_replace", real_replace)
    with pytest.raises(storage.StorageContractError, match="file (size|content) mismatch"):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )
    assert not (target / "artifacts").exists()
    assert not (target / "blobs").exists()


def test_restore_retry_rejects_directory_replaced_by_regular_file(tmp_path, monkeypatch):
    """Retry validates archive entry types, including empty nested directories."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    (source / "artifacts" / "empty-dir").mkdir()
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )
    target = storage.prepare_storage(
        str(safe_root / "target"),
        safe_root=safe_root,
        repository_root=repository,
    )
    real_replace = storage._durable_replace

    def interrupt_first_publication(source_path, destination_path):
        """Abort the first publication attempt for this scenario."""
        if Path(destination_path) == target / "artifacts":
            raise KeyboardInterrupt
        return real_replace(Path(source_path), Path(destination_path))

    monkeypatch.setattr(storage, "_durable_replace", interrupt_first_publication)
    with pytest.raises(KeyboardInterrupt):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )
    stage = next(target.glob(f"{storage.RESTORE_STAGE_PREFIX}*"))
    empty_directory = stage / "artifacts" / "empty-dir"
    empty_directory.rmdir()
    empty_directory.write_bytes(b"not-a-directory")

    monkeypatch.setattr(storage, "_durable_replace", real_replace)
    with pytest.raises(storage.StorageContractError, match="directory type mismatch"):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute-bit contract")
def test_empty_nested_directory_without_search_permission_is_rejected():
    """Listing an empty directory is insufficient without execute/search access."""
    code = """
import importlib.util
import os
import pathlib
import sys
spec = importlib.util.spec_from_file_location('storage_probe', pathlib.Path(sys.argv[1]))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if os.geteuid() == 0:
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
try:
    module._require_search_access(pathlib.Path(sys.argv[2]))
except PermissionError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    with tempfile.TemporaryDirectory(prefix="ums-search-contract-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        nested = root / "no-search"
        nested.mkdir(mode=0o444)
        result = subprocess.run(
            [sys.executable, "-c", code, str(SCRIPT_PATH), str(nested)],
            check=False,
        )
        assert result.returncode == 0


def test_pending_restore_kept_when_runtime_cannot_read_descendants(tmp_path, monkeypatch):
    """A tolerated chown failure is safe only after every restored entry is readable."""
    repository, safe_root = _layout(tmp_path)
    target = _seed_storage(repository, safe_root)
    pending = target / storage.RESTORE_PENDING_FILENAME
    digest = "0" * 64
    pending.write_text(
        json.dumps(
            storage._pending_restore_payload(
                digest,
                "b" * 64,
                "file-store",
                None,
            )
        ),
        encoding="utf-8",
    )
    stage = target / f"{storage.RESTORE_STAGE_PREFIX}pending-test"
    stage.mkdir()
    journal = storage._restore_journal_payload(
        digest,
        "b" * 64,
        "file-store",
        None,
        stage.name,
    )
    journal["published"] = list(storage.STORAGE_DIRECTORIES)
    journal["state"] = "complete"
    (target / storage.RESTORE_JOURNAL_FILENAME).write_text(
        json.dumps(journal),
        encoding="utf-8",
    )
    monkeypatch.setenv(storage.HOST_CANONICAL_ENV, str(target.resolve()))
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(storage, "_runtime_identity", lambda _user: (10001, 10001))
    monkeypatch.setattr(
        os,
        "chown",
        lambda *_args: (_ for _ in ()).throw(OSError("host mapping rejected chown")),
        raising=False,
    )
    monkeypatch.setattr(os, "chmod", lambda *_args: None)
    monkeypatch.setattr(
        storage,
        "_verify_tree_readable_as_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("unreadable child")),
    )
    probes: list[bool] = []
    monkeypatch.setattr(
        storage,
        "_probe_as_identity",
        lambda *_args, **_kwargs: probes.append(True),
    )

    with pytest.raises(PermissionError, match="unreadable child"):
        storage.initialize_container_storage(
            target,
            app_user="app",
            configured_host_path=str(target),
        )

    assert pending.is_file()
    assert probes == []


def test_root_operator_can_publish_root_owned_backup(tmp_path, monkeypatch):
    """The documented 0:0 operator path is executable, not rejected by validation."""
    mount = tmp_path / "mount"
    mount.mkdir()
    output = tmp_path / "bundle" / "ums-app-data.tgz"
    ownership: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(storage, "_validate_mounted_marker", lambda _path: {})
    monkeypatch.setattr(storage, "_assert_sensitive_output", lambda path, **_kwargs: path)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        os,
        "chown",
        lambda path, uid, gid: ownership.append((path, uid, gid)),
        raising=False,
    )

    def fake_archive(_mount, archive):
        """Substitute a small archive for the real bundle payload."""
        archive.parent.mkdir()
        archive.write_bytes(b"archive")
        return archive

    monkeypatch.setattr(storage, "_archive_storage_tree", fake_archive)
    result = storage.create_mounted_artifact_archive(
        mount,
        output=output,
        writers_stopped=True,
        output_uid=0,
        output_gid=0,
    )

    assert result == output
    assert ownership == [(output, 0, 0)]


def test_restore_refuses_nonempty_target(tmp_path):
    """Recovery cannot overwrite live or partially recovered storage."""
    repository, safe_root = _layout(tmp_path)
    source = _seed_storage(repository, safe_root, "source")
    bundle = tmp_path / "bundle"
    archive = storage.create_artifact_archive(
        str(source),
        output=bundle / "ums-app-data.tgz",
        writers_stopped=True,
        repository_root=repository,
    )
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        _recovery_members(bundle, archive),
        repository_root=repository,
    )
    target = _seed_storage(repository, safe_root, "target")

    with pytest.raises(storage.StorageContractError, match="not empty"):
        storage.restore_artifact_archive(
            str(target),
            archive=archive,
            manifest=manifest,
            repository_root=repository,
        )


def test_compose_contract_requires_precreated_bind_and_actual_image_identity():
    """Pin Compose and Dockerfile wiring that unit tests cannot execute safely."""
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    init_block = compose.split("  app-data-init:", 1)[1].split("\n  app:", 1)[0]
    app_block = compose.split("\n  app:", 1)[1].split("\n  # Optional dev", 1)[0]
    app_dev_block = compose.split("\n  app-dev:", 1)[1]

    assert compose.count("create_host_path: false") == 3
    assert compose.count("source: ${UMS_APP_DATA_HOST:?") == 3
    assert "APP_UID: ${APP_UID:-10001}" in compose
    assert "UMS_APP_DATA_HOST_CONTRACT: ${UMS_APP_DATA_HOST_CONFIGURED-}" in init_block
    assert "UMS_APP_DATA_HOST_CANONICAL_CONTRACT: ${UMS_APP_DATA_HOST_CANONICAL-}" in init_block
    assert 'scripts/compose_storage.py", "container-init' in init_block
    assert 'entrypoint: ["/usr/bin/tini", "--"]' in init_block
    assert "chown 10001" not in init_block
    assert "setpriv" not in init_block
    assert "ARG APP_UID=10001" in dockerfile
    assert "APP_UID=${APP_UID}" in dockerfile
    assert "case \"${APP_UID}\" in ''|*[!0-9]*" in dockerfile
    assert '[ "${APP_UID}" -ge 1 ]' in dockerfile
    assert "scripts/compose_storage.py ${APP_HOME}/scripts/compose_storage.py" in dockerfile
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in dockerfile
    assert '"container-exec", "--path", "/var/lib/ums", "--"' not in dockerfile
    assert "x-app-storage-entrypoint: &app-storage-entrypoint" in compose
    assert '"container-exec", "--path", "/var/lib/ums", "--"' in compose
    assert "entrypoint: *app-storage-entrypoint" in app_block
    assert "entrypoint: *app-storage-entrypoint" in app_dev_block
    assert '"--proxy-headers"]' in app_block
    assert '      - "--reload"' in app_dev_block


def test_runbook_seals_roles_database_and_bind_as_one_recovery_set():
    """Pin the security and recovery claims that replaced unsafe header commands."""
    runbook = (PROJECT_ROOT / "Docs" / "20_COMPOSE_STORAGE_RUNBOOK.md").read_text(encoding="utf-8")
    roles_sql = (PROJECT_ROOT / "scripts" / "compose_restore_roles.sql").read_text(encoding="utf-8")

    assert "scripts/backup_database.py" in runbook
    assert "database-manifest.json" in runbook
    assert "pg_dumpall --roles-only --no-role-passwords" not in runbook
    assert "scripts/restore_database.py" in runbook
    assert "--confirm-clean-target" in runbook
    assert "pg_restore --exit-on-error --clean" not in runbook
    assert "umask 077" in runbook
    assert "SetAccessRuleProtection($true, $false)" in runbook
    assert "SHA256SUMS.json" in runbook
    assert "does **not**\n  delete or empty `UMS_APP_DATA_HOST`" in runbook
    assert "restore-artifacts" in runbook
    assert "compose_storage.py compose" in runbook
    assert '--path "$UMS_APP_DATA_HOST"' in runbook
    assert runbook.count("--blob-backend $env:UMS_BLOB_BACKEND") >= 2
    assert runbook.count("--gcs-bucket $env:UMS_GCS_BUCKET") >= 2
    assert runbook.count('--blob-backend "$UMS_BLOB_BACKEND"') >= 2
    assert runbook.count('--gcs-bucket "$UMS_GCS_BUCKET"') >= 2
    assert runbook.count("GCS-GENERATIONS-VERIFIED") >= 4
    assert "CREATE ROLE %I NOLOGIN NOSUPERUSER" in roles_sql
    assert "rolcanlogin OR rolsuper" in roles_sql
    assert "rolbypassrls" in roles_sql


def test_manifest_rejects_unsafe_relative_record(tmp_path):
    """Manifest verification never resolves a member outside its bundle."""
    manifest = tmp_path / "SHA256SUMS.json"
    manifest.write_text(
        json.dumps(
            {
                "blob_backend": "file-store",
                "files": [{"path": "../escape", "sha256": "0" * 64, "size": 0}],
                "profile": storage.GENERIC_BACKUP_PROFILE,
                "schema": storage.MANIFEST_NAME,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(storage.StorageContractError, match="unsafe backup manifest path"):
        storage.verify_bundle_manifest(manifest)
