from __future__ import annotations

import importlib.util
import json
import os
import stat
import tarfile
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
    database_dump = bundle / "ums-database.dump"
    roles_dump = bundle / "ums-roles.sql"
    database_dump.write_bytes(b"postgres-custom-format")
    roles_dump.write_text("CREATE ROLE app_tenant;\nCREATE ROLE app_platform;\n", encoding="utf-8")
    manifest = storage.create_bundle_manifest(
        bundle / "SHA256SUMS.json",
        [archive, database_dump, roles_dump],
        repository_root=repository,
    )

    verified = storage.verify_bundle_manifest(manifest)
    assert set(verified) == {"ums-app-data.tgz", "ums-database.dump", "ums-roles.sql"}
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
        [archive],
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

    assert compose.count("create_host_path: false") == 3
    assert compose.count("source: ${UMS_APP_DATA_HOST:?") == 3
    assert "APP_UID: ${APP_UID:-10001}" in compose
    assert "UMS_APP_DATA_HOST_CONTRACT: ${UMS_APP_DATA_HOST:?" in init_block
    assert 'scripts/compose_storage.py", "container-init' in init_block
    assert "chown 10001" not in init_block
    assert "setpriv" not in init_block
    assert "ARG APP_UID=10001" in dockerfile
    assert "APP_UID=${APP_UID}" in dockerfile
    assert "case \"${APP_UID}\" in ''|*[!0-9]*" in dockerfile
    assert '[ "${APP_UID}" -ge 1 ]' in dockerfile
    assert "scripts/compose_storage.py ${APP_HOME}/scripts/compose_storage.py" in dockerfile


def test_runbook_seals_roles_database_and_bind_as_one_recovery_set():
    """Pin the security and recovery claims that replaced unsafe header commands."""
    runbook = (PROJECT_ROOT / "Docs" / "20_COMPOSE_STORAGE_RUNBOOK.md").read_text(encoding="utf-8")
    roles_sql = (PROJECT_ROOT / "scripts" / "compose_restore_roles.sql").read_text(encoding="utf-8")

    assert "pg_dumpall --roles-only --no-role-passwords" in runbook
    assert "umask 077" in runbook
    assert "SetAccessRuleProtection($true, $false)" in runbook
    assert "SHA256SUMS.json" in runbook
    assert "does **not**\n  delete or empty `UMS_APP_DATA_HOST`" in runbook
    assert "restore-artifacts" in runbook
    assert "CREATE ROLE %I NOLOGIN NOSUPERUSER" in roles_sql
    assert "rolcanlogin OR rolsuper" in roles_sql
    assert "rolbypassrls" in roles_sql


def test_manifest_rejects_unsafe_relative_record(tmp_path):
    """Manifest verification never resolves a member outside its bundle."""
    manifest = tmp_path / "SHA256SUMS.json"
    manifest.write_text(
        json.dumps(
            {
                "files": [{"path": "../escape", "sha256": "0" * 64, "size": 0}],
                "schema": storage.MANIFEST_NAME,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(storage.StorageContractError, match="unsafe backup manifest path"):
        storage.verify_bundle_manifest(manifest)
