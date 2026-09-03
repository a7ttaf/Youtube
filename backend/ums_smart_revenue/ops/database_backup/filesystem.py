"""Filesystem safety and atomic publication for database backups."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ums_smart_revenue.ops.database_backup.contracts import (
    DUMP_NAME,
    MANIFEST_NAME,
    ROLES_NAME,
    BackupManifest,
    BackupToolError,
    DatabaseIdentity,
    verify_artifacts,
)

LOCK_NAME = ".ums-database-backup.lock"
LOCK_OWNER_NAME = "owner-token"
ROOT_MARKER_NAME = ".ums-database-backup-root"
ROOT_MARKER_BODY = "ums-database-backup-root/v1\n"
RUN_NAME_RE = re.compile(r"^ums-database-backup-\d{8}T\d{6}Z-[0-9a-f]{8}$")
PARTIAL_RUN_NAME_RE = re.compile(r"^\.ums-database-backup-\d{8}T\d{6}Z-[0-9a-f]{8}\.partial$")


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    """Opaque local-filesystem identity for a trusted backup directory."""

    device: int
    inode: int


def _is_redirect(path: Path) -> bool:
    """Report whether the path is a symlink or Windows reparse point."""
    try:
        return path.is_symlink() or bool(
            path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
    except (AttributeError, FileNotFoundError):
        return path.is_symlink()


def require_no_redirect_components(path: Path, *, label: str) -> None:
    """Reject any existing symlink/reparse component without resolving through it.

    Args:
        path: Path. Filesystem path the operation reads or writes.
        label: str. Manifest field label used in validation errors.

    Returns:
        ``None``.

    Raises:
        BackupToolError: the path contains a symlink or other redirected component, or its
            identity cannot be read.
    """
    absolute = path if path.is_absolute() else Path.cwd() / path
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        try:
            if _is_redirect(cursor):
                raise BackupToolError(f"{label} may not contain a redirected path", exit_code=2)
        except OSError as exc:
            raise BackupToolError(f"{label} path identity is unreadable", exit_code=2) from exc


def _restrict_windows_owner_acl(path: Path) -> None:
    """Reduce the Windows DACL to the current owner with explicit protection."""
    if os.name != "nt":
        return
    script = r"""
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($env:UMS_DATABASE_BACKUP_ACL_TARGET)
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$rule = [Security.AccessControl.FileSystemAccessRule]::new(
  $sid,
  [Security.AccessControl.FileSystemRights]::FullControl,
  ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
   [Security.AccessControl.InheritanceFlags]::ObjectInherit),
  [Security.AccessControl.PropagationFlags]::None,
  [Security.AccessControl.AccessControlType]::Allow
)
$acl.AddAccessRule($rule)
[IO.FileSystemAclExtensions]::SetAccessControl(
  [IO.DirectoryInfo]::new($target),
  $acl
)
$observed = Get-Acl -LiteralPath $target
if (-not $observed.AreAccessRulesProtected) { throw 'DACL still inherits' }
$observedOwnerSid = $observed.GetOwner(
  [Security.Principal.SecurityIdentifier]
).Value
if ($observedOwnerSid -ne $sid.Value) { throw 'directory owner is not current user' }
$rules = @($observed.Access)
if ($rules.Count -ne 1) { throw 'DACL does not contain exactly one rule' }
$ruleSid = $rules[0].IdentityReference.Translate(
  [Security.Principal.SecurityIdentifier]
).Value
if ($ruleSid -ne $sid.Value -or
    $rules[0].AccessControlType -ne
      [Security.AccessControl.AccessControlType]::Allow) {
  throw 'DACL is not owner-only'
}
$requiredRights = [Security.AccessControl.FileSystemRights]::FullControl
$requiredInheritance = (
  [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
  [Security.AccessControl.InheritanceFlags]::ObjectInherit
)
if (($rules[0].FileSystemRights -band $requiredRights) -ne $requiredRights -or
    ($rules[0].InheritanceFlags -band $requiredInheritance) -ne
      $requiredInheritance) {
  throw 'DACL owner rule is not inheritable FullControl'
}
"""
    environment = os.environ.copy()
    environment["UMS_DATABASE_BACKUP_ACL_TARGET"] = str(path)
    shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if shell is None:
        raise BackupToolError(
            "PowerShell is required to enforce the owner-only Windows backup ACL",
            exit_code=2,
        )
    if Path(shell).name.casefold() == "powershell.exe":
        # A PowerShell 7 PSModulePath inherited by Windows PowerShell 5.1 can
        # make Microsoft.PowerShell.Security unloadable. Let 5.1 rebuild its
        # native module path rather than inheriting an incompatible one.
        environment.pop("PSModulePath", None)
    try:
        completed = subprocess.run(
            [
                shell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupToolError(
            "could not enforce the owner-only Windows backup ACL", exit_code=2
        ) from exc
    if completed.returncode != 0:
        detail = " ".join(line.strip() for line in completed.stderr.splitlines() if line.strip())[
            :400
        ]
        raise BackupToolError(
            "could not enforce the owner-only Windows backup ACL: "
            f"{detail or 'no diagnostic text'}",
            exit_code=2,
        )


def _verify_windows_owner_acl(path: Path) -> None:
    """Verify the directory DACL still grants only the current owner."""
    script = r"""
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($env:UMS_DATABASE_BACKUP_ACL_TARGET)
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$observed = Get-Acl -LiteralPath $target
$rules = @($observed.Access)
if ($rules.Count -ne 1) { throw 'DACL does not contain exactly one rule' }
if (-not $observed.AreAccessRulesProtected -or $rules[0].IsInherited) {
  throw 'DACL trust boundary is not protected and explicit'
}
$observedOwnerSid = $observed.GetOwner(
  [Security.Principal.SecurityIdentifier]
).Value
if ($observedOwnerSid -ne $sid.Value) { throw 'directory owner is not current user' }
$ruleSid = $rules[0].IdentityReference.Translate(
  [Security.Principal.SecurityIdentifier]
).Value
$requiredRights = [Security.AccessControl.FileSystemRights]::FullControl
if ($ruleSid -ne $sid.Value -or
    $rules[0].AccessControlType -ne
      [Security.AccessControl.AccessControlType]::Allow -or
    ($rules[0].FileSystemRights -band $requiredRights) -ne $requiredRights) {
  throw 'DACL is not current-owner-only FullControl'
}
"""
    environment = os.environ.copy()
    environment["UMS_DATABASE_BACKUP_ACL_TARGET"] = str(path)
    shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if shell is None:
        raise BackupToolError(
            "PowerShell is required to verify the owner-only Windows backup ACL",
            exit_code=2,
        )
    if Path(shell).name.casefold() == "powershell.exe":
        environment.pop("PSModulePath", None)
    try:
        completed = subprocess.run(
            [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupToolError(
            "could not verify the owner-only Windows backup ACL", exit_code=2
        ) from exc
    if completed.returncode != 0:
        raise BackupToolError(
            "backup directory is not protected by the current-owner-only ACL",
            exit_code=2,
        )


def require_owner_only_directory(path: Path) -> None:
    """Require the restore package to remain inside its private trust boundary.

    Args:
        path: Path. Filesystem path the operation reads or writes.

    Returns:
        ``None``.

    Raises:
        BackupToolError: the directory is not a real directory, is not owned by the current
            user, or is not mode 0700.
    """
    if not path.is_dir() or _is_redirect(path):
        raise BackupToolError("backup directory is not a real directory", exit_code=2)
    if os.name == "nt":
        _verify_windows_owner_acl(path)
        return
    status = path.stat()
    get_euid = getattr(os, "geteuid", None)
    if (get_euid is not None and status.st_uid != get_euid()) or stat.S_IMODE(
        status.st_mode
    ) != 0o700:
        raise BackupToolError(
            "backup directory must be owned by the current user with mode 0700",
            exit_code=2,
        )


def _real_directory_identity(path: Path) -> DirectoryIdentity:
    """Return the device and inode of the resolved directory."""
    try:
        status = path.lstat()
    except OSError as exc:
        raise BackupToolError("backup directory identity is unreadable", exit_code=2) from exc
    if not stat.S_ISDIR(status.st_mode) or _is_redirect(path):
        raise BackupToolError("backup directory is not a real directory", exit_code=2)
    return DirectoryIdentity(device=status.st_dev, inode=status.st_ino)


# ============================================================================
# Purpose: Pin and later recheck a completed backup directory's filesystem
#   identity while revalidating its current-owner-only trust boundary.
# Database/ORM: None.
# Standards: Redirects and replacement races fail closed with typed errors.
# Blast Radius: Backup artifact provenance and restore input integrity.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/contracts.py -> artifact pins.
#   - File: backend/ums_smart_revenue/ops/database_backup/restore.py -> restore boundary.
# ============================================================================
def capture_trusted_directory_identity(path: Path) -> DirectoryIdentity:
    """Capture one owner-only directory identity without accepting a validation swap.

    Args:
        path: Path. Filesystem path the operation reads or writes.

    Returns:
        ``DirectoryIdentity``.

    Raises:
        BackupToolError: the directory identity changed while the trust boundary was being
            validated.
    """
    require_no_redirect_components(path, label="backup directory")
    before = _real_directory_identity(path)
    require_owner_only_directory(path)
    after = _real_directory_identity(path)
    if after != before:
        raise BackupToolError(
            "backup directory identity changed while its trust boundary was validated",
            exit_code=2,
        )
    return after


def require_trusted_directory_identity(path: Path, expected: DirectoryIdentity) -> None:
    """Refuse a replacement directory or a weakened owner-only trust boundary.

    Args:
        path: Path. Filesystem path the operation reads or writes.
        expected: DirectoryIdentity.

    Returns:
        ``None``.

    Raises:
        BackupToolError: the directory identity no longer matches the captured trusted identity.
    """
    observed = capture_trusted_directory_identity(path)
    if observed != expected:
        raise BackupToolError("backup directory identity changed", exit_code=2)



def _require_dedicated_location(resolved: Path, repository: Path) -> None:
    """Refuse filesystem roots, the repository, the home directory, and repo children."""
    filesystem_root = Path(resolved.anchor).resolve(strict=False)
    home = Path.home().resolve(strict=False)
    if resolved in {filesystem_root, repository, home} or resolved.is_relative_to(repository):
        raise BackupToolError(
            "backup output must be a dedicated host directory outside the repository",
            exit_code=2,
        )


def _require_empty_bundle_directory(resolved: Path) -> Path:
    """Validate one coordinated-bundle directory: owner-only and run-free."""
    require_owner_only_directory(resolved)
    existing_runs = sorted(
        child.name
        for child in resolved.iterdir()
        if RUN_NAME_RE.fullmatch(child.name) or PARTIAL_RUN_NAME_RE.fullmatch(child.name)
    )
    if existing_runs:
        raise BackupToolError(
            "coordinated bundle already contains a database run or partial run",
            exit_code=2,
        )
    return resolved


def _require_existing_dedicated_root(resolved: Path) -> None:
    """Validate an existing dedicated root: marker, no foreign entries, no partials."""
    children = list(resolved.iterdir())
    marker = resolved / ROOT_MARKER_NAME
    marker_body: str | None = None
    if children:
        try:
            if marker.is_file() and not _is_redirect(marker):
                marker_body = marker.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise BackupToolError(
                "existing backup output root marker is unreadable",
                exit_code=2,
            ) from exc
    if children and marker_body != ROOT_MARKER_BODY:
        raise BackupToolError(
            "existing backup output lacks the database-backup root marker",
            exit_code=2,
        )
    foreign = sorted(
        child.name
        for child in children
        if child.name != LOCK_NAME
        and child.name != ROOT_MARKER_NAME
        and not RUN_NAME_RE.fullmatch(child.name)
        and not PARTIAL_RUN_NAME_RE.fullmatch(child.name)
    )
    if foreign:
        raise BackupToolError(
            "backup output is not dedicated to database runs; foreign entries: "
            + ", ".join(foreign[:8]),
            exit_code=2,
        )
    partials = sorted(
        child.name for child in children if PARTIAL_RUN_NAME_RE.fullmatch(child.name)
    )
    if partials:
        raise BackupToolError(
            "backup output contains a prior partial run; inspect it before retrying",
            exit_code=2,
        )
    if children:
        require_owner_only_directory(resolved)


def resolve_output_directory(
    raw: Path, *, repository_root: Path, coordinated_bundle: bool = False
) -> Path:
    """Resolve a dedicated host backup directory outside the working tree.

    Args:
        raw: Path.
        repository_root: Path. Checkout root used to locate tracked SQL and Alembic state.
        coordinated_bundle: bool.

    Returns:
        ``Path``.

    Raises:
        BackupToolError: the output path is a link, is not a dedicated host directory outside
            the repository, or (bundle mode) does not already exist with owner-only protection.
    """
    unresolved = raw.expanduser()
    if not unresolved.is_absolute():
        unresolved = Path.cwd() / unresolved
    require_no_redirect_components(unresolved, label="backup output")
    if unresolved.exists() and _is_redirect(unresolved):
        raise BackupToolError("backup output directory may not be a link", exit_code=2)
    resolved = unresolved.resolve(strict=False)
    repository = repository_root.resolve(strict=True)
    _require_dedicated_location(resolved, repository)
    existed = resolved.exists()
    if coordinated_bundle and not existed:
        raise BackupToolError(
            "coordinated bundle output must already exist and be owner-only",
            exit_code=2,
        )
    resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir() or _is_redirect(resolved):
        raise BackupToolError("backup output is not a real directory", exit_code=2)
    if coordinated_bundle:
        return _require_empty_bundle_directory(resolved)
    if existed:
        _require_existing_dedicated_root(resolved)
    if os.name == "nt":
        _restrict_windows_owner_acl(resolved)
    else:
        resolved.chmod(0o700)
    marker = resolved / ROOT_MARKER_NAME
    if not marker.exists():
        with marker.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(ROOT_MARKER_BODY)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            marker.chmod(0o600)
        _sync_directory(resolved)
    return resolved


def _sync_file(path: Path) -> None:
    """Flush a file's contents and metadata to stable storage."""
    flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    """Flush a directory entry to stable storage."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_move(source: Path, destination: Path) -> None:
    """Move a directory onto its destination and sync both parents."""
    if destination.exists():
        raise BackupToolError(f"backup destination already exists: {destination.name}", exit_code=2)
    if os.name == "nt":
        import ctypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file.restype = ctypes.c_int
        if not move_file(str(source), str(destination), 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.rename(source, destination)
    _sync_directory(destination.parent)


@contextmanager
def exclusive_output_lock(output: Path) -> Iterator[None]:
    """Refuse overlapping writers; stale locks require explicit operator review.

    Args:
        output: Path.

    Returns:
        ``Iterator[None]``.

    Raises:
        BackupToolError: a prior run's lock already exists, or lock ownership cannot be proven
            before any lock removal.
    """
    lock = output / LOCK_NAME
    owner = lock / LOCK_OWNER_NAME
    token = secrets.token_hex(32)
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise BackupToolError(
            f"backup lock already exists at {lock}; inspect the prior run before removing it",
            exit_code=7,
        ) from exc
    if os.name == "nt":
        _restrict_windows_owner_acl(lock)
    lock_status = lock.lstat()
    owner_status: os.stat_result | None = None
    try:
        with owner.open("x", encoding="ascii") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        owner_status = owner.lstat()
        if os.name != "nt":
            owner.chmod(0o600)
        _sync_directory(lock)
        yield
    finally:
        if owner_status is None:
            raise BackupToolError(
                "backup lock owner identity was never established; leaving the lock in place",
                exit_code=7,
            )
        try:
            observed = owner.read_text(encoding="ascii")
        except OSError as exc:
            raise BackupToolError(
                "backup lock ownership became unreadable; refusing to remove it",
                exit_code=7,
            ) from exc
        if observed != token:
            raise BackupToolError(
                "backup lock ownership changed; refusing to remove another process's lock",
                exit_code=7,
            )
        current_lock = lock.lstat()
        current_owner = owner.lstat()
        if (current_lock.st_dev, current_lock.st_ino) != (
            lock_status.st_dev,
            lock_status.st_ino,
        ) or (current_owner.st_dev, current_owner.st_ino) != (
            owner_status.st_dev,
            owner_status.st_ino,
        ):
            raise BackupToolError(
                "backup lock identity changed; refusing to remove another process's lock",
                exit_code=7,
            )
        cleanup = output / f"{LOCK_NAME}.cleanup-{token}"
        os.rename(lock, cleanup)
        moved_lock = cleanup.lstat()
        moved_owner = (cleanup / LOCK_OWNER_NAME).lstat()
        if (moved_lock.st_dev, moved_lock.st_ino) != (
            lock_status.st_dev,
            lock_status.st_ino,
        ) or (moved_owner.st_dev, moved_owner.st_ino) != (
            owner_status.st_dev,
            owner_status.st_ino,
        ):
            if not lock.exists():
                os.rename(cleanup, lock)
            raise BackupToolError(
                "backup lock changed during cleanup; refusing destructive cleanup",
                exit_code=7,
            )
        (cleanup / LOCK_OWNER_NAME).unlink()
        cleanup.rmdir()
        _sync_directory(output)


def new_staging_directory(output: Path, *, run_name: str) -> Path:
    """Create an owner-only partial directory that restore will never accept.

    Args:
        output: Path.
        run_name: str. Deterministic run directory name for this backup.

    Returns:
        ``Path``.

    Raises:
        BackupToolError: the staging directory already exists.
    """
    staging = output / f".{run_name}.partial"
    if staging.exists():
        raise BackupToolError(f"staging directory already exists: {staging.name}", exit_code=2)
    staging.mkdir(mode=0o700)
    if os.name == "nt":
        _restrict_windows_owner_acl(staging)
    _sync_directory(output)
    return staging


def write_manifest(staging: Path, manifest: BackupManifest) -> None:
    """Write and flush the final manifest only after both artifacts are complete.

    Args:
        staging: Path.
        manifest: BackupManifest. Parsed backup manifest validated or written by this call.

    Returns:
        ``None``.
    """
    destination = staging / MANIFEST_NAME
    temporary = staging / f".{MANIFEST_NAME}.{secrets.token_hex(8)}.tmp"
    body = json.dumps(manifest.to_json(), indent=2, sort_keys=True) + "\n"
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, destination)
    _sync_file(destination)
    _sync_directory(staging)


def publish(staging: Path, destination: Path, *, manifest: BackupManifest) -> None:
    """Reverify and atomically expose exactly one complete backup directory.

    Args:
        staging: Path.
        destination: Path. Staging file path the command writes to.
        manifest: BackupManifest. Parsed backup manifest validated or written by this call.

    Returns:
        ``None``.

    Raises:
        BackupToolError: staging does not hold exactly the required members, the staging
            manifest changed before publication, or a non-regular entry is present.
    """
    expected = {DUMP_NAME, ROLES_NAME, MANIFEST_NAME}
    observed = {child.name for child in staging.iterdir()}
    if observed != expected:
        raise BackupToolError(
            "staging does not contain exactly the required backup members",
            exit_code=7,
        )
    reloaded = BackupManifest.load(staging / MANIFEST_NAME)
    if reloaded != manifest:
        raise BackupToolError("staging manifest changed before publication", exit_code=7)
    verify_artifacts(staging, reloaded)
    for child in staging.iterdir():
        if not child.is_file() or _is_redirect(child):
            raise BackupToolError("staging contains a non-regular entry", exit_code=7)
        _sync_file(child)
    _sync_directory(staging)
    _durable_move(staging, destination)


# ============================================================================
# Purpose: Bind one output directory to one (cluster,database) identity by
#   validating every previously published run before a new run is accepted.
# Database/ORM: None; reads strict manifests and verifies their artifact hashes.
# Standards: Completed-looking malformed runs fail closed; foreign directories
#   and partial runs are left untouched and never treated as identity evidence.
# Blast Radius: Prevents cross-database backup history mixing and wrong restores.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/backup.py -> pre-publish gate.
#   - File: backend/ums_smart_revenue/ops/database_backup/contracts.py -> identity.
# ============================================================================
def require_matching_backup_history(output: Path, identity: DatabaseIdentity) -> None:
    """Refuse an output directory holding history for another database.

    Args:
        output: Path.
        identity: DatabaseIdentity. Previously captured directory identity to re-verify.

    Returns:
        ``None``.

    Raises:
        BackupToolError: the backup directory is already bound to a different database identity,
            contains multiple identities, or holds a corrupt completed run.
    """
    observed: set[DatabaseIdentity] = set()
    for child in sorted(output.iterdir(), key=lambda path: path.name):
        if not RUN_NAME_RE.fullmatch(child.name):
            continue
        if not child.is_dir() or _is_redirect(child):
            raise BackupToolError(
                f"completed-looking backup is not a real directory: {child.name}", exit_code=8
            )
        manifest = BackupManifest.load(child / MANIFEST_NAME)
        verify_artifacts(child, manifest)
        observed.add(manifest.source.identity)
    if len(observed) > 1:
        raise BackupToolError(
            "backup directory already contains multiple database identities", exit_code=8
        )
    if observed and identity not in observed:
        previous = next(iter(observed))
        raise BackupToolError(
            "backup directory is bound to a different database identity "
            f"({previous.system_identifier}/{previous.database})",
            exit_code=8,
        )
