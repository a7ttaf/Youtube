"""Prepare, back up, verify, and restore the Compose application-data bind."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import posixpath
import secrets
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

LOGGER = logging.getLogger("ums.compose_storage")
CONTRACT_NAME = "ums-compose-storage-v1"
MANIFEST_NAME = "ums-compose-backup-v1"
MARKER_FILENAME = ".ums-storage-root.json"
RESTORE_PENDING_FILENAME = ".ums-restore-pending"
STORAGE_DIRECTORIES = ("artifacts", "blobs")
CHUNK_SIZE = 1024 * 1024
MAX_POSIX_ID = 2_147_483_647
HOST_PATH_ENV = "UMS_APP_DATA_HOST_CONTRACT"


class StorageContractError(RuntimeError):
    """Raised when a storage path or backup violates the safety contract."""


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _is_redirect(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _reject_existing_redirects(path: Path, *, stop_at: Path | None = None) -> None:
    candidate = path
    while True:
        if candidate.exists() and _is_redirect(candidate):
            raise StorageContractError(f"storage paths may not traverse a link: {candidate}")
        if candidate == candidate.parent or candidate == stop_at:
            return
        candidate = candidate.parent


def _raw_path(raw_path: str, *, base: Path) -> Path:
    if not raw_path or not raw_path.strip():
        raise StorageContractError("storage path must be explicitly set and non-empty")
    if raw_path.strip() in {".", "./", ".\\"}:
        raise StorageContractError("storage path may not be the repository/workspace directory")
    path = Path(raw_path.strip()).expanduser()
    return path if path.is_absolute() else base / path


def _configured_path_key(raw_path: str) -> str:
    if not raw_path or not raw_path.strip():
        raise StorageContractError("storage path must be explicitly set and non-empty")
    portable = raw_path.strip().replace("\\", "/")
    raw_parts = [part for part in portable.split("/") if part]
    if ".." in raw_parts:
        raise StorageContractError("storage path may not contain parent traversal")
    normalized = posixpath.normpath(portable)
    drive_root = (
        len(normalized) in {2, 3}
        and normalized[0].isalpha()
        and normalized[1] == ":"
        and normalized[2:] in {"", "/"}
    )
    unc_parts = [part for part in normalized.split("/") if part]
    unc_root = normalized.startswith("//") and len(unc_parts) <= 2
    if normalized in {".", "/"} or drive_root or unc_root:
        raise StorageContractError(
            "storage path may not be a root or repository/workspace dot path"
        )
    return normalized.casefold()


def _validate_host_candidate(
    raw_path: str,
    *,
    safe_root: Path,
    repository_root: Path,
) -> Path:
    unresolved = _raw_path(raw_path, base=repository_root)
    unresolved_safe_root = safe_root.expanduser()
    if not unresolved_safe_root.is_absolute():
        unresolved_safe_root = repository_root / unresolved_safe_root

    _reject_existing_redirects(unresolved)
    _reject_existing_redirects(unresolved_safe_root)

    target = unresolved.resolve(strict=False)
    approved_root = unresolved_safe_root.resolve(strict=False)
    repository = repository_root.resolve(strict=False)
    filesystem_root = Path(target.anchor).resolve(strict=False)
    home = Path.home().resolve(strict=False)

    if target in {filesystem_root, repository, approved_root, home}:
        raise StorageContractError(
            "storage path must be a dedicated child directory, not a filesystem, home, "
            "repository/workspace, or approved-root directory"
        )
    if approved_root in {Path(approved_root.anchor).resolve(strict=False), repository, home}:
        raise StorageContractError(
            "safe root must be a dedicated directory, not a filesystem, home, or repository"
        )
    if not target.is_relative_to(approved_root):
        raise StorageContractError(
            f"storage path {target} is outside the approved safe root {approved_root}"
        )
    if target.parent != approved_root:
        raise StorageContractError("storage path must be a direct child of the approved safe root")
    if target.exists() and not target.is_dir():
        raise StorageContractError(f"storage path is not a directory: {target}")
    return target


def _read_json(path: Path) -> dict[str, Any]:
    if _is_redirect(path):
        raise StorageContractError(f"contract file may not be a link: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StorageContractError(
            f"storage contract is missing: {path}; run the prepare command first"
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageContractError(f"storage contract is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise StorageContractError(f"storage contract is not a JSON object: {path}")
    return payload


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    temporary = path.with_name(f".{path.name}.partial-{os.getpid()}-{secrets.token_hex(4)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
    except BaseException:
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _validate_marker_payload(payload: dict[str, Any], marker_path: Path) -> None:
    if payload.get("contract") != CONTRACT_NAME:
        raise StorageContractError(f"unknown storage contract in {marker_path}")
    if set(payload) != {"canonical_path", "configured_path_key", "contract", "safe_root"}:
        raise StorageContractError(f"storage contract has unexpected fields: {marker_path}")
    if not all(isinstance(payload.get(key), str) for key in payload):
        raise StorageContractError(f"storage contract fields must be strings: {marker_path}")


# ============================================================================
# Purpose: Approve one dedicated host directory before Compose may mount it.
# Database/ORM: None.
# Standards: Fail closed on ambiguous paths, redirects, and unmarked content.
# Blast Radius: Artifact and connector-blob storage; no database state changed.
# Connections:
#   - File: docker-compose.yml -> app-data-init requires this marker.
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> operator preparation contract.
# ============================================================================
def prepare_storage(
    raw_path: str,
    *,
    safe_root: Path,
    repository_root: Path | None = None,
) -> Path:
    """Create or validate the host-side marker for a dedicated storage target."""
    repository = (repository_root or _repository_root()).resolve(strict=False)
    target = _validate_host_candidate(
        raw_path,
        safe_root=safe_root,
        repository_root=repository,
    )
    approved_root = safe_root.expanduser()
    if not approved_root.is_absolute():
        approved_root = repository / approved_root
    approved_root = approved_root.resolve(strict=False)
    marker_path = target / MARKER_FILENAME
    configured_path_key = _configured_path_key(raw_path)

    if marker_path.exists():
        payload = _read_json(marker_path)
        _validate_marker_payload(payload, marker_path)
        if _normalized_path(Path(payload["canonical_path"])) != _normalized_path(target):
            raise StorageContractError("storage marker belongs to a different host path")
        if _normalized_path(Path(payload["safe_root"])) != _normalized_path(approved_root):
            raise StorageContractError("storage marker belongs to a different approved safe root")
        if payload["configured_path_key"] != configured_path_key:
            raise StorageContractError("storage marker belongs to a different configured host path")
        return target

    if target.exists() and any(target.iterdir()):
        raise StorageContractError(
            "unmarked storage directory is not empty; refusing to claim existing data"
        )

    approved_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.mkdir(mode=0o700, parents=False, exist_ok=True)
    _write_json_exclusive(
        marker_path,
        {
            "canonical_path": str(target),
            "configured_path_key": configured_path_key,
            "contract": CONTRACT_NAME,
            "safe_root": str(approved_root),
        },
    )
    return target


def check_host_storage(raw_path: str, *, repository_root: Path | None = None) -> Path:
    """Validate an already-prepared host path without creating or mutating it."""
    repository = (repository_root or _repository_root()).resolve(strict=False)
    unresolved = _raw_path(raw_path, base=repository)
    target = unresolved.resolve(strict=False)
    marker_path = target / MARKER_FILENAME
    payload = _read_json(marker_path)
    _validate_marker_payload(payload, marker_path)
    safe_root = Path(payload["safe_root"])
    validated = _validate_host_candidate(
        raw_path,
        safe_root=safe_root,
        repository_root=repository,
    )
    if _normalized_path(Path(payload["canonical_path"])) != _normalized_path(validated):
        raise StorageContractError("storage marker belongs to a different host path")
    if payload["configured_path_key"] != _configured_path_key(raw_path):
        raise StorageContractError("storage marker belongs to a different configured host path")
    return validated


def _validate_mounted_marker(
    mount_root: Path,
    *,
    configured_host_path: str | None = None,
) -> dict[str, Any]:
    if not mount_root.is_dir() or _is_redirect(mount_root):
        raise StorageContractError(f"mounted storage root is not a real directory: {mount_root}")
    marker_path = mount_root / MARKER_FILENAME
    payload = _read_json(marker_path)
    _validate_marker_payload(payload, marker_path)
    configured = configured_host_path or os.environ.get(HOST_PATH_ENV)
    if configured is None:
        raise StorageContractError(f"{HOST_PATH_ENV} is required for mounted-path validation")
    if payload["configured_path_key"] != _configured_path_key(configured):
        raise StorageContractError("mounted marker does not match UMS_APP_DATA_HOST")
    return payload


def _walk_without_redirects(root: Path) -> list[Path]:
    paths = [root]
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        directory_names.sort()
        file_names.sort()
        for name in [*directory_names, *file_names]:
            child = current / name
            if _is_redirect(child):
                raise StorageContractError(f"storage tree contains a link: {child}")
            paths.append(child)
    return paths


def _probe_as_identity(roots: tuple[Path, ...], *, uid: int, gid: int) -> None:
    original_euid = os.geteuid()
    original_egid = os.getegid()
    original_groups = os.getgroups()
    try:
        os.setgroups([])
        os.setegid(gid)
        os.seteuid(uid)
        for root in roots:
            probe = root / f".rw-probe-{secrets.token_hex(8)}"
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            probe.unlink()
    finally:
        os.seteuid(original_euid)
        os.setegid(original_egid)
        os.setgroups(original_groups)


# ============================================================================
# Purpose: Provision the marked bind only after its host contract is proven.
# Database/ORM: None.
# Standards: Derive the runtime identity from the image and probe as that user.
# Blast Radius: Ownership and modes under the dedicated artifact/blob bind only.
# Connections:
#   - File: Dockerfile -> the app account is built with configurable APP_UID.
#   - File: docker-compose.yml -> root one-shot invokes this entry point.
# ============================================================================
def initialize_container_storage(
    mount_root: Path,
    *,
    app_user: str,
    configured_host_path: str | None = None,
) -> None:
    """Initialize and prove mounted storage as the image's actual app identity."""
    _validate_mounted_marker(mount_root, configured_host_path=configured_host_path)
    if os.geteuid() != 0:
        raise StorageContractError("container initialization must run as root")
    try:
        import pwd
    except ModuleNotFoundError as exc:  # pragma: no cover - container is Linux.
        raise StorageContractError("container initialization requires a POSIX image") from exc
    try:
        account = pwd.getpwnam(app_user)
    except KeyError as exc:
        raise StorageContractError(f"runtime account does not exist: {app_user}") from exc

    roots = tuple(mount_root / name for name in STORAGE_DIRECTORIES)
    for root in roots:
        root.mkdir(mode=0o750, exist_ok=True)

    pending_restore = mount_root / RESTORE_PENDING_FILENAME
    ownership_targets: list[Path] = [mount_root, *roots]
    if pending_restore.exists():
        if _is_redirect(pending_restore) or not pending_restore.is_file():
            raise StorageContractError("restore-pending marker is not a regular file")
        ownership_targets = [mount_root]
        for root in roots:
            ownership_targets.extend(_walk_without_redirects(root))

    chown_failures: list[str] = []
    for path in ownership_targets:
        try:
            os.chown(path, account.pw_uid, account.pw_gid)
        except OSError as exc:
            chown_failures.append(f"{path}: {exc}")

    for root in (mount_root, *roots):
        os.chmod(root, 0o750)

    _probe_as_identity(roots, uid=account.pw_uid, gid=account.pw_gid)
    if pending_restore.exists():
        pending_restore.unlink()
    if chown_failures:
        LOGGER.warning(
            "host bind did not accept chown; runtime-identity write probe passed: %s",
            "; ".join(chown_failures),
        )


def _assert_sensitive_output(path: Path, *, repository_root: Path) -> Path:
    output = path.expanduser().resolve(strict=False)
    repository = repository_root.resolve(strict=False)
    if output.is_relative_to(repository):
        raise StorageContractError("sensitive backup output must be outside the repository")
    if output.exists():
        raise StorageContractError(f"refusing to overwrite existing output: {output}")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return output


def _archive_storage_tree(storage: Path, archive: Path) -> Path:
    roots = tuple(storage / name for name in STORAGE_DIRECTORIES)
    if any(not root.is_dir() for root in roots):
        raise StorageContractError("artifact and blob directories must exist before backup")
    entries: list[Path] = []
    for root in roots:
        entries.extend(_walk_without_redirects(root))

    temporary = archive.with_name(f".{archive.name}.tmp-{os.getpid()}")
    try:
        with tarfile.open(temporary, mode="x:gz") as handle:
            for entry in entries:
                handle.add(entry, arcname=entry.relative_to(storage).as_posix(), recursive=False)
        os.chmod(temporary, 0o600)
        os.link(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


# ============================================================================
# Purpose: Archive quiesced artifact and connector-blob bytes cross-platform.
# Database/ORM: None; database and roles dumps are separate bundle members.
# Standards: Refuse live-writer ambiguity, links, worktree output, and overwrite.
# Blast Radius: Reads artifact/blob storage and writes one external archive.
# Connections:
#   - File: docker-compose.yml -> app and app-dev are the bind writers.
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> coordinated backup sequence.
# ============================================================================
def create_artifact_archive(
    raw_path: str,
    *,
    output: Path,
    writers_stopped: bool,
    repository_root: Path | None = None,
) -> Path:
    """Create a tar.gz archive after explicit confirmation that writers stopped."""
    if not writers_stopped:
        raise StorageContractError("stop app and app-dev, then pass --writers-stopped")
    repository = (repository_root or _repository_root()).resolve(strict=False)
    storage = check_host_storage(raw_path, repository_root=repository)
    archive = _assert_sensitive_output(output, repository_root=repository)
    return _archive_storage_tree(storage, archive)


# ============================================================================
# Purpose: Archive the same stopped bind from the root init image on POSIX.
# Database/ORM: None.
# Standards: Validate the marker, publish no-clobber, and return host ownership.
# Blast Radius: Reads artifact/blob bytes and writes one external archive.
# Connections:
#   - File: docker-compose.yml -> app-data-init supplies the mounted bind.
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> Linux backup command.
# ============================================================================
def create_mounted_artifact_archive(
    mount_root: Path,
    *,
    output: Path,
    writers_stopped: bool,
    output_uid: int | None = None,
    output_gid: int | None = None,
) -> Path:
    """Archive mounted storage as root, optionally returning ownership to the host user."""
    if not writers_stopped:
        raise StorageContractError("stop app and app-dev, then pass --writers-stopped")
    if (output_uid is None) != (output_gid is None):
        raise StorageContractError("output uid and gid must be provided together")
    if output_uid is not None and not 1 <= output_uid <= MAX_POSIX_ID:
        raise StorageContractError("output uid is outside the supported positive range")
    if output_gid is not None and not 1 <= output_gid <= MAX_POSIX_ID:
        raise StorageContractError("output gid is outside the supported positive range")
    if output_uid is not None and output_gid is not None and os.geteuid() != 0:
        raise StorageContractError("changing backup output ownership requires root")
    _validate_mounted_marker(mount_root)
    archive = _assert_sensitive_output(output, repository_root=_repository_root())
    result = _archive_storage_tree(mount_root, archive)
    if output_uid is not None and output_gid is not None:
        os.chown(result, output_uid, output_gid)
    return result


def _validated_archive_members(handle: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = handle.getmembers()
    seen: set[str] = set()
    top_levels: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise StorageContractError(f"unsafe archive path: {member.name}")
        if "\\" in member.name or path.parts[0] not in STORAGE_DIRECTORIES:
            raise StorageContractError(f"archive member is outside storage roots: {member.name}")
        normalized_name = path.as_posix()
        if member.name.rstrip("/") != normalized_name:
            raise StorageContractError(f"non-canonical archive member: {member.name}")
        if normalized_name in seen:
            raise StorageContractError(f"duplicate archive member: {member.name}")
        if not (member.isdir() or member.isreg()):
            raise StorageContractError(f"archive links/devices are forbidden: {member.name}")
        seen.add(normalized_name)
        top_levels.add(path.parts[0])
    if top_levels != set(STORAGE_DIRECTORIES):
        raise StorageContractError("archive must contain both artifacts and blobs roots")
    return members


def verify_artifact_archive(archive: Path) -> None:
    """Verify archive readability and member-path safety without extracting it."""
    try:
        with tarfile.open(archive, mode="r:gz") as handle:
            _validated_archive_members(handle)
    except (OSError, tarfile.TarError) as exc:
        raise StorageContractError(f"artifact archive is invalid: {archive}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


# ============================================================================
# Purpose: Seal database, role, and artifact backups into one checksum manifest.
# Database/ORM: PostgreSQL dumps are opaque files; no live database is accessed.
# Standards: Record relative names, sizes, SHA-256 hashes, and restrictive mode.
# Blast Radius: Backup integrity metadata only; runtime state is unchanged.
# Connections:
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> backup and recovery gates.
#   - File: docker-compose.yml -> documents the three coordinated data planes.
# ============================================================================
def create_bundle_manifest(
    output: Path,
    files: list[Path],
    *,
    repository_root: Path | None = None,
) -> Path:
    """Write a SHA-256 manifest for all members of one external backup bundle."""
    repository = (repository_root or _repository_root()).resolve(strict=False)
    manifest = _assert_sensitive_output(output, repository_root=repository)
    bundle_root = manifest.parent
    records: list[dict[str, Any]] = []
    for file_path in files:
        unresolved_candidate = file_path.expanduser()
        if _is_redirect(unresolved_candidate):
            raise StorageContractError(f"manifest member may not be a link: {file_path}")
        candidate = unresolved_candidate.resolve(strict=True)
        if not candidate.is_file():
            raise StorageContractError(f"manifest member is not a regular file: {candidate}")
        try:
            relative = candidate.relative_to(bundle_root)
        except ValueError as exc:
            raise StorageContractError(
                "manifest members must be inside the bundle directory"
            ) from exc
        if candidate == manifest:
            raise StorageContractError("manifest cannot include itself")
        records.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256(candidate),
                "size": candidate.stat().st_size,
            }
        )
    if not records:
        raise StorageContractError("backup manifest needs at least one file")
    _write_json_exclusive(
        manifest,
        {"files": sorted(records, key=lambda record: record["path"]), "schema": MANIFEST_NAME},
    )
    return manifest


def verify_bundle_manifest(manifest: Path) -> dict[str, Path]:
    """Verify every size and SHA-256 recorded in a backup bundle manifest."""
    payload = _read_json(manifest)
    if set(payload) != {"files", "schema"} or payload.get("schema") != MANIFEST_NAME:
        raise StorageContractError(f"unknown backup manifest schema: {manifest}")
    records = payload.get("files")
    if not isinstance(records, list) or not records:
        raise StorageContractError("backup manifest has no file records")
    verified: dict[str, Path] = {}
    bundle_root = manifest.resolve(strict=True).parent
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            raise StorageContractError("backup manifest record is malformed")
        name = record["path"]
        digest = record["sha256"]
        size = record["size"]
        if (
            not isinstance(name, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise StorageContractError("backup manifest record has invalid field values")
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise StorageContractError(f"unsafe backup manifest path: {name}")
        unresolved_candidate = bundle_root.joinpath(*relative.parts)
        _reject_existing_redirects(unresolved_candidate, stop_at=bundle_root)
        candidate = unresolved_candidate.resolve(strict=True)
        if not candidate.is_relative_to(bundle_root) or not candidate.is_file():
            raise StorageContractError(f"backup member escapes the bundle: {name}")
        if candidate.stat().st_size != size or _sha256(candidate) != digest:
            raise StorageContractError(f"backup checksum mismatch: {name}")
        if name in verified:
            raise StorageContractError(f"duplicate backup manifest path: {name}")
        verified[name] = candidate
    return verified


def _ensure_empty_restore_target(storage: Path) -> None:
    pending = storage / RESTORE_PENDING_FILENAME
    if pending.exists():
        raise StorageContractError("an earlier restore is still pending container initialization")
    for name in STORAGE_DIRECTORIES:
        root = storage / name
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise StorageContractError(f"restore target is not empty: {root}")


# ============================================================================
# Purpose: Restore verified artifact/blob bytes into an empty prepared target.
# Database/ORM: None; roles and database restore remain explicit prior steps.
# Standards: Verify manifest first, reject overwrite/links, and stage extraction.
# Blast Radius: Writes only the marked empty storage target selected by operator.
# Connections:
#   - File: docker-compose.yml -> app-data-init adopts restored ownership.
#   - File: Docs/20_COMPOSE_STORAGE_RUNBOOK.md -> coordinated recovery order.
# ============================================================================
def restore_artifact_archive(
    raw_path: str,
    *,
    archive: Path,
    manifest: Path,
    repository_root: Path | None = None,
) -> None:
    """Restore one verified archive without overwriting existing storage bytes."""
    repository = (repository_root or _repository_root()).resolve(strict=False)
    storage = check_host_storage(raw_path, repository_root=repository)
    verified = verify_bundle_manifest(manifest)
    resolved_archive = archive.expanduser().resolve(strict=True)
    if resolved_archive not in verified.values():
        raise StorageContractError("artifact archive is not covered by the verified manifest")
    _ensure_empty_restore_target(storage)

    stage = Path(tempfile.mkdtemp(prefix=".ums-restore-", dir=storage.parent))
    try:
        with tarfile.open(resolved_archive, mode="r:gz") as handle:
            members = _validated_archive_members(handle)
            for member in members:
                destination = stage.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = handle.extractfile(member)
                if source is None:
                    raise StorageContractError(f"archive file has no payload: {member.name}")
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with source, os.fdopen(descriptor, "wb") as output:
                    shutil.copyfileobj(source, output, length=CHUNK_SIZE)

        for name in STORAGE_DIRECTORIES:
            destination = storage / name
            if destination.exists():
                destination.rmdir()
            os.replace(stage / name, destination)
        _write_json_exclusive(
            storage / RESTORE_PENDING_FILENAME,
            {"contract": CONTRACT_NAME, "state": "ownership-pending"},
        )
    except (OSError, tarfile.TarError) as exc:
        raise StorageContractError("artifact restore failed before initialization") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="approve a dedicated host storage path")
    prepare.add_argument("--path", required=True)
    prepare.add_argument("--safe-root", type=Path, default=Path("data"))

    check = subparsers.add_parser("check", help="validate a prepared host storage path")
    check.add_argument("--path", required=True)

    container_init = subparsers.add_parser("container-init", help="provision a mounted path")
    container_init.add_argument("--path", required=True, type=Path)
    container_init.add_argument("--app-user", default="app")

    archive = subparsers.add_parser("archive", help="archive stopped-writer artifact storage")
    archive.add_argument("--path", required=True)
    archive.add_argument("--output", required=True, type=Path)
    archive.add_argument("--writers-stopped", action="store_true")

    mounted_archive = subparsers.add_parser(
        "archive-mounted", help="archive a mounted path from the root init image"
    )
    mounted_archive.add_argument("--path", required=True, type=Path)
    mounted_archive.add_argument("--output", required=True, type=Path)
    mounted_archive.add_argument("--writers-stopped", action="store_true")
    mounted_archive.add_argument("--output-uid", type=int)
    mounted_archive.add_argument("--output-gid", type=int)

    manifest = subparsers.add_parser("manifest", help="create a bundle checksum manifest")
    manifest.add_argument("--output", required=True, type=Path)
    manifest.add_argument("files", nargs="+", type=Path)

    verify = subparsers.add_parser("verify", help="verify a complete bundle manifest")
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--artifact-archive", type=Path)

    restore = subparsers.add_parser("restore-artifacts", help="restore into empty prepared storage")
    restore.add_argument("--path", required=True)
    restore.add_argument("--archive", required=True, type=Path)
    restore.add_argument("--manifest", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the requested storage safety operation."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            path = prepare_storage(args.path, safe_root=args.safe_root)
            print(path)
        elif args.command == "check":
            print(check_host_storage(args.path))
        elif args.command == "container-init":
            initialize_container_storage(args.path, app_user=args.app_user)
        elif args.command == "archive":
            print(
                create_artifact_archive(
                    args.path,
                    output=args.output,
                    writers_stopped=args.writers_stopped,
                )
            )
        elif args.command == "archive-mounted":
            print(
                create_mounted_artifact_archive(
                    args.path,
                    output=args.output,
                    writers_stopped=args.writers_stopped,
                    output_uid=args.output_uid,
                    output_gid=args.output_gid,
                )
            )
        elif args.command == "manifest":
            print(create_bundle_manifest(args.output, args.files))
        elif args.command == "verify":
            verified = verify_bundle_manifest(args.manifest)
            if args.artifact_archive is not None:
                archive = args.artifact_archive.expanduser().resolve(strict=True)
                if archive not in verified.values():
                    raise StorageContractError("artifact archive is not covered by the manifest")
                verify_artifact_archive(archive)
            print(f"verified {len(verified)} backup files")
        elif args.command == "restore-artifacts":
            restore_artifact_archive(
                args.path,
                archive=args.archive,
                manifest=args.manifest,
            )
        else:  # pragma: no cover - argparse enforces the command choices.
            raise AssertionError(f"unknown command: {args.command}")
    except (OSError, StorageContractError) as exc:
        LOGGER.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
