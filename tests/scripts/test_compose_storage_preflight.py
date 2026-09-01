"""Mutation-resistant host-only tests for the Compose storage boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))
try:
    import compose as compose_launcher
    import validate_compose_storage_path as storage
finally:
    sys.path.pop(0)


@pytest.fixture
def no_host_acl_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep generic path tests independent of this workstation's inherited DACL."""

    monkeypatch.setattr(storage, "_require_windows_host_acl", lambda _path: None)


def _storage_model(source: Path) -> dict[str, object]:
    """Return the smallest complete rendered model satisfying the reviewed contract."""

    def app_service(*, dev: bool) -> dict[str, object]:
        volumes: list[dict[str, object]] = []
        if dev:
            volumes.append(
                {
                    "type": "bind",
                    "source": str(PROJECT_ROOT / "backend"),
                    "target": "/srv/app/backend",
                    "read_only": True,
                    "bind": {},
                }
            )
        for child in storage.STORAGE_CHILDREN:
            volumes.append(
                {
                    "type": "bind",
                    "source": str(source / child),
                    "target": compose_launcher.STORAGE_TARGETS[child],
                    "bind": {"create_host_path": False},
                }
            )
        service: dict[str, object] = {
            "build": {
                "context": str(PROJECT_ROOT),
                "dockerfile": "Dockerfile",
            },
            "image": compose_launcher.STORAGE_IMAGE,
            "pull_policy": "never",
            "user": "10001:10001",
            "entrypoint": ["/usr/bin/tini", "--"],
            "volumes": volumes,
        }
        if dev:
            service["profiles"] = ["dev"]
        return service

    return {
        "name": compose_launcher.PROJECT_NAME,
        "services": {
            "postgres": {"volumes": []},
            "redis": {"volumes": []},
            "migrate": {
                "build": {
                    "context": str(PROJECT_ROOT),
                    "dockerfile": "Dockerfile",
                },
                "image": compose_launcher.STORAGE_IMAGE,
                "pull_policy": "never",
                "user": "10001:10001",
                "entrypoint": ["/usr/bin/tini", "--"],
                "volumes": [],
            },
            "app": app_service(dev=False),
            "app-dev": app_service(dev=True),
        },
    }


def _acl_payload(
    source: Path,
    *,
    child: Path | None = None,
    broad_allow: bool = False,
    deny: bool = False,
    root_protected: bool = True,
    root_inheritable: bool = True,
    root_propagation: str = "None",
    root_rights_mask: int = storage._WINDOWS_MODIFY_RIGHTS,
    child_operator: bool = True,
) -> dict[str, object]:
    """Build internally reconciled Windows ACL evidence for counterexamples."""

    current_sid = "S-1-5-21-1000"
    paths = [source]
    if child is not None:
        paths.append(child)
    owners = [
        {
            "Path": str(path),
            "OwnerSid": current_sid,
            "AreAccessRulesProtected": root_protected if path == source else False,
        }
        for path in paths
    ]
    root_flags = "ContainerInherit, ObjectInherit" if root_inheritable else "None"
    rows: list[dict[str, object]] = [
        {
            "Path": str(source),
            "Sid": current_sid,
            "Type": "Allow",
            "Rights": "Modify",
            "RightsMask": root_rights_mask,
            "InheritanceFlags": root_flags,
            "PropagationFlags": root_propagation,
            "IsInherited": False,
        }
    ]
    if child is not None:
        rows.append(
            {
                "Path": str(child),
                "Sid": current_sid if child_operator else "S-1-5-18",
                "Type": "Allow",
                "Rights": "Modify" if child_operator else "FullControl",
                "RightsMask": storage._WINDOWS_MODIFY_RIGHTS,
                "InheritanceFlags": "None",
                "PropagationFlags": "None",
                "IsInherited": True,
            }
        )
    if broad_allow:
        rows.append(
            {
                "Path": str(source),
                "Sid": "S-1-1-0",
                "Type": "Allow",
                "Rights": "ReadAndExecute",
                "RightsMask": 131241,
                "InheritanceFlags": root_flags,
                "PropagationFlags": "None",
                "IsInherited": False,
            }
        )
    if deny:
        rows.append(
            {
                "Path": str(source),
                "Sid": current_sid,
                "Type": "Deny",
                "Rights": "Delete",
                "RightsMask": 65536,
                "InheritanceFlags": "None",
                "PropagationFlags": "None",
                "IsInherited": False,
            }
        )
    return {
        "CurrentSid": current_sid,
        "Paths": [str(path) for path in paths],
        "Owners": owners,
        "Access": rows,
    }


def _install_acl_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    """Make the ACL validator consume deterministic machine-readable evidence."""

    monkeypatch.setattr(storage.os, "name", "nt")
    monkeypatch.setattr(
        storage,
        "_windows_acl_process_spec",
        lambda: (["powershell.exe"], {}),
    )
    monkeypatch.setattr(
        storage.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )


def _make_directory_link(link: Path, target: Path) -> None:
    """Create a real directory symlink or an unprivileged Windows junction."""

    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
    else:
        link.symlink_to(target, target_is_directory=True)


def _remove_directory_link(link: Path) -> None:
    """Remove only the link itself so pytest never traverses its target."""

    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()


def _run_real_posix_script(script: str, *, wsl_as_root: bool = False) -> None:
    """Run a real POSIX counterexample, using Ubuntu WSL on Windows."""

    if os.name == "nt":
        converted = subprocess.run(
            ["wsl.exe", "--exec", "wslpath", "-a", str(PROJECT_ROOT)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert converted.returncode == 0, converted.stderr or converted.stdout
        posix_scripts = f"{converted.stdout.strip()}/scripts"
        command = ["wsl.exe"]
        if wsl_as_root:
            command.extend(["--user", "root"])
        command.extend(["--exec", "python3", "-c", script.replace("__SCRIPTS__", posix_scripts)])
    else:
        command = [sys.executable, "-c", script.replace("__SCRIPTS__", str(SCRIPT_DIRECTORY))]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_prepare_creates_only_the_path_attested_default_store(
    tmp_path: Path,
    no_host_acl_probe: None,
) -> None:
    """The reserved missing default gets exactly two children and one sentinel."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = checkout / "data" / "ums"
    result = storage.prepare_storage_path(source, project_root=checkout)

    assert result == source.resolve()
    assert {item.name for item in result.iterdir()} == {
        storage.STORAGE_SENTINEL_NAME,
        "artifacts",
        "blobs",
    }
    assert (result / storage.STORAGE_SENTINEL_NAME).read_text() == (
        storage.storage_sentinel_content(result)
    )
    assert len(storage.storage_tree_identity(result)) == 4


def test_custom_missing_path_is_never_created(
    tmp_path: Path,
    no_host_acl_probe: None,
) -> None:
    """Even --adopt-existing cannot turn a typo into an arbitrary host mkdir."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = tmp_path / "custom" / "store"
    with pytest.raises(storage.StoragePathError, match="does not exist"):
        storage.prepare_storage_path(
            source,
            project_root=checkout,
            adopt_existing=True,
        )
    assert not source.exists()
    assert not source.parent.exists()


def test_custom_existing_store_is_not_mutated_before_explicit_adoption(
    tmp_path: Path,
    no_host_acl_probe: None,
) -> None:
    """A custom empty directory receives no children or marker on implicit use."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = tmp_path / "custom-store"
    source.mkdir()
    with pytest.raises(storage.StoragePathError, match="adopt-existing"):
        storage.prepare_storage_path(source, project_root=checkout)
    assert tuple(source.iterdir()) == ()

    result = storage.prepare_storage_path(
        source,
        project_root=checkout,
        adopt_existing=True,
    )
    assert result == source.resolve()
    assert {item.name for item in source.iterdir()} == {
        storage.STORAGE_SENTINEL_NAME,
        "artifacts",
        "blobs",
    }


def test_attested_custom_store_is_not_implicitly_completed(
    tmp_path: Path,
    no_host_acl_probe: None,
) -> None:
    """The normal launcher never mkdirs inside a custom root, even if attested."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = tmp_path / "attested-custom-store"
    source.mkdir()
    storage._create_sentinel(source)
    with pytest.raises(storage.StoragePathError, match="will not mutate"):
        storage.prepare_storage_path(source, project_root=checkout)
    assert {item.name for item in source.iterdir()} == {storage.STORAGE_SENTINEL_NAME}


def test_general_purpose_directory_is_rejected_without_mutation(
    tmp_path: Path,
    no_host_acl_probe: None,
) -> None:
    """An unrelated file prevents a Downloads-like directory from adoption."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = tmp_path / "Downloads"
    source.mkdir()
    keep = source / "photo.jpg"
    keep.write_bytes(b"keep")
    with pytest.raises(storage.StoragePathError, match="dedicated"):
        storage.prepare_storage_path(
            source,
            project_root=checkout,
            adopt_existing=True,
        )
    assert tuple(source.iterdir()) == (keep,)
    assert keep.read_bytes() == b"keep"


@pytest.mark.parametrize("raw_value", [".", "..", "./data/ums/.."])
def test_dot_paths_are_rejected_before_mutation(
    tmp_path: Path,
    raw_value: str,
    no_host_acl_probe: None,
) -> None:
    """Dot traversal cannot turn the checkout into application storage."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    with pytest.raises(storage.StoragePathError):
        storage.prepare_storage_path(raw_value, project_root=checkout)
    assert tuple(checkout.iterdir()) == ()


def test_checkout_backend_and_system_path_are_rejected(
    tmp_path: Path,
    no_host_acl_probe: None,
) -> None:
    """Only the exact checkout default is eligible within the source tree."""

    checkout = tmp_path / "checkout"
    backend = checkout / "backend"
    backend.mkdir(parents=True)
    with pytest.raises(storage.StoragePathError, match="reserved"):
        storage.prepare_storage_path(backend, project_root=checkout)
    system_path = Path(os.environ.get("SystemRoot", "/etc"))
    with pytest.raises(storage.StoragePathError):
        storage.validate_storage_path(system_path, project_root=PROJECT_ROOT)


def test_real_source_parent_junction_is_rejected_without_a_skip(tmp_path: Path) -> None:
    """The actual Windows junction API, not a mocked seam, protects creation."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    data_link = checkout / "data"
    _make_directory_link(data_link, outside)
    try:
        with pytest.raises(storage.StoragePathError, match="symlink or junction"):
            storage.prepare_storage_path("./data/ums", project_root=checkout)
        assert tuple(outside.iterdir()) == ()
    finally:
        _remove_directory_link(data_link)


def test_real_direct_child_junction_is_rejected_without_a_skip(tmp_path: Path) -> None:
    """A real artifacts junction is rejected before ACL or Docker operations."""

    checkout = tmp_path / "checkout"
    source = checkout / "data" / "ums"
    source.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = source / "artifacts"
    _make_directory_link(link, outside)
    try:
        with pytest.raises(storage.StoragePathError, match="symlink or junction"):
            storage.validate_storage_path(
                source,
                project_root=checkout,
                require_attestation=False,
            )
    finally:
        _remove_directory_link(link)


def test_real_nested_child_junction_is_rejected_without_a_skip(tmp_path: Path) -> None:
    """A junction below artifacts cannot hide from the recursive path walk."""

    checkout = tmp_path / "checkout"
    source = checkout / "data" / "ums"
    nested_parent = source / "artifacts" / "year"
    nested_parent.mkdir(parents=True)
    (source / "blobs").mkdir()
    storage._create_sentinel(source)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = nested_parent / "month"
    _make_directory_link(link, outside)
    try:
        with pytest.raises(storage.StoragePathError, match="symlink or junction"):
            storage.validate_storage_path(source, project_root=checkout)
    finally:
        _remove_directory_link(link)


def test_real_storage_identity_guard_survives_until_mount_resolution(
    tmp_path: Path,
) -> None:
    """Windows blocks replacement; POSIX mounts through the retained root fd."""

    source = tmp_path / "store"
    (source / "artifacts").mkdir(parents=True)
    (source / "blobs").mkdir()
    storage._create_sentinel(source)
    original_identity = storage.storage_tree_identity(source)
    moved = tmp_path / "moved-store"

    if os.name == "nt":
        with storage.hold_storage_identity(source) as guard:
            assert guard.child_source("artifacts") == (source / "artifacts").resolve()
            with pytest.raises(OSError):
                source.rename(moved)
            with pytest.raises(OSError):
                (source / "artifacts").rename(source / "replaced-artifacts")
            guard.assert_current()
        source.rename(moved)
        moved.rename(source)
        return

    original_child_identity = original_identity[1][1:]
    with pytest.raises(storage.StoragePathError, match="identity changed"):
        with storage.hold_storage_identity(source) as guard:
            stable_source = guard.child_source("artifacts")
            artifacts = source / "artifacts"
            artifacts.rename(source / "artifacts-original")
            artifacts.mkdir()
            held = os.fstat(guard._descriptors[1])
            assert (held.st_dev, held.st_ino) == original_child_identity
            assert stable_source == artifacts.resolve()
            guard.assert_current()
    assert stable_source.exists()


def test_real_wsl_child_sources_are_durable_after_guard_exit() -> None:
    """Restart-time bind sources remain canonical and live after descriptors close."""

    script = textwrap.dedent(
        """
        import shutil
        import sys
        import tempfile
        from pathlib import Path

        sys.path.insert(0, "__SCRIPTS__")
        import validate_compose_storage_path as storage

        root = Path(tempfile.mkdtemp(prefix="ums-child-fd-")) / "store"
        try:
            (root / "artifacts").mkdir(parents=True)
            (root / "blobs").mkdir()
            storage._create_sentinel(root)
            with storage.hold_storage_identity(root) as guard:
                sources = {name: guard.child_source(name) for name in storage.STORAGE_CHILDREN}
                assert sources == {name: root / name for name in storage.STORAGE_CHILDREN}
                assert all(source.exists() for source in sources.values())
                assert all(not str(source).startswith("/proc/") for source in sources.values())
                guard.assert_current()
            assert all(source.exists() for source in sources.values())
            assert all(source.is_dir() for source in sources.values())
        finally:
            shutil.rmtree(root.parent)
        """
    )
    _run_real_posix_script(script)


def test_real_wsl_open_child_descriptor_detects_name_replacement() -> None:
    """A held child fd plus pathname recheck rejects replacement before creation."""

    script = textwrap.dedent(
        """
        import os
        import shutil
        import sys
        import tempfile
        from pathlib import Path

        sys.path.insert(0, "__SCRIPTS__")
        import validate_compose_storage_path as storage

        root = Path(tempfile.mkdtemp(prefix="ums-child-fd-")) / "store"
        try:
            (root / "artifacts").mkdir(parents=True)
            (root / "blobs").mkdir()
            storage._create_sentinel(root)
            original = (root / "artifacts").stat()
            try:
                with storage.hold_storage_identity(root) as guard:
                    source = guard.child_source("artifacts")
                    assert source == root / "artifacts"
                    held = os.fstat(guard._descriptors[1])
                    assert (held.st_dev, held.st_ino) == (original.st_dev, original.st_ino)
                    (root / "artifacts").rename(root / "artifacts-original")
                    (root / "artifacts").mkdir()
                    guard.assert_current()
            except storage.StoragePathError as exc:
                assert "identity changed" in str(exc)
            else:
                raise AssertionError("child replacement passed the held-fd identity guard")
        finally:
            shutil.rmtree(root.parent)
        """
    )
    _run_real_posix_script(script)


def test_real_wsl_recursive_posix_policy_rejects_world_and_foreign_ownership() -> None:
    """Real POSIX metadata rejects 0777 and non-operator/non-app uid/gid."""

    script = textwrap.dedent(
        """
        import os
        import secrets
        import shutil
        import sys
        from pathlib import Path

        sys.path.insert(0, "__SCRIPTS__")
        import validate_compose_storage_path as storage

        privileged = os.geteuid() == 0
        if not privileged:
            storage.APP_UID = os.geteuid()
            storage.APP_GID = os.getegid()
        base_parent = Path("/home") if privileged else Path.home()
        base = base_parent / ("ums-posix-policy-" + secrets.token_hex(12))
        assert base.is_absolute() and base.parent == base_parent
        base.mkdir(mode=0o700)
        root = base / "store"
        try:
            (root / "artifacts" / "nested").mkdir(parents=True)
            (root / "blobs").mkdir()
            payload = root / "artifacts" / "nested" / "payload.bin"
            payload.write_bytes(b"proof")
            storage._create_sentinel(root)
            for entry in [root, *root.rglob("*")]:
                os.chmod(entry, 0o700 if entry.is_dir() else 0o600)
            for child_name in storage.STORAGE_CHILDREN:
                child = root / child_name
                for entry in [child, *child.rglob("*")]:
                    if privileged:
                        os.chown(entry, storage.APP_UID, storage.APP_GID)
            storage.validate_storage_path(root, project_root=Path("__SCRIPTS__").parent)

            os.chmod(payload, 0o777)
            try:
                storage.validate_storage_path(root, project_root=Path("__SCRIPTS__").parent)
            except storage.StoragePathError as exc:
                assert "group/world write" in str(exc)
            else:
                raise AssertionError("0777 descendant passed the POSIX policy")

            if privileged:
                os.chmod(payload, 0o600)
                os.chown(payload, storage.APP_UID, 42424)
                try:
                    storage.validate_storage_path(root, project_root=Path("__SCRIPTS__").parent)
                except storage.StoragePathError as exc:
                    assert "group gid 42424" in str(exc)
                else:
                    raise AssertionError("foreign group passed the POSIX policy")

                os.chown(payload, 42424, storage.APP_GID)
                try:
                    storage.validate_storage_path(root, project_root=Path("__SCRIPTS__").parent)
                except storage.StoragePathError as exc:
                    assert "owner uid 42424" in str(exc)
                else:
                    raise AssertionError("foreign owner passed the POSIX policy")

                os.chown(payload, storage.APP_UID, storage.APP_GID)
                os.chown(root / "artifacts", os.geteuid(), os.getegid())
                try:
                    storage.validate_storage_path(root, project_root=Path("__SCRIPTS__").parent)
                except storage.StoragePathError as exc:
                    assert "storage artifacts must be owned" in str(exc)
                else:
                    raise AssertionError("operator-owned direct child passed the POSIX boundary")
                os.chown(root / "artifacts", storage.APP_UID, storage.APP_GID)

            os.chmod(base, 0o770)
            try:
                storage.validate_storage_path(root, project_root=Path("__SCRIPTS__").parent)
            except storage.StoragePathError as exc:
                assert "ancestor is group/world writable" in str(exc)
            else:
                raise AssertionError("writable ancestor passed the POSIX boundary")
            os.chmod(base, 0o700)
            if privileged:
                os.chown(base, 42424, os.getegid())
                try:
                    storage.validate_storage_path(
                        root,
                        project_root=Path("__SCRIPTS__").parent,
                    )
                except storage.StoragePathError as exc:
                    assert "ancestor owner uid 42424" in str(exc)
                else:
                    raise AssertionError("foreign-owned ancestor passed the POSIX boundary")
                os.chown(base, os.geteuid(), os.getegid())
        finally:
            shutil.rmtree(base)
        """
    )
    _run_real_posix_script(script, wsl_as_root=True)


@pytest.mark.parametrize(
    ("child_uid", "child_gid", "message"),
    [
        (42424, storage.APP_GID, "owner uid 42424"),
        (storage.APP_UID, 42424, "group gid 42424"),
    ],
)
def test_recursive_posix_policy_rejects_synthetic_foreign_principal(
    monkeypatch: pytest.MonkeyPatch,
    child_uid: int,
    child_gid: int,
    message: str,
) -> None:
    """Foreign uid/gid metadata fails on hosts where tests cannot call chown."""

    class MetadataEntry:
        def __init__(self, name: str, mode: int, uid: int, gid: int) -> None:
            self.name = name
            self._metadata = SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)

        def stat(self, *, follow_symlinks: bool) -> SimpleNamespace:
            assert follow_symlinks is False
            return self._metadata

        def __str__(self) -> str:
            return self.name

    root = MetadataEntry("root", storage.stat.S_IFDIR | 0o700, 1000, 1000)
    child = MetadataEntry("child", storage.stat.S_IFREG | 0o600, child_uid, child_gid)
    monkeypatch.setattr(storage.os, "name", "posix")
    monkeypatch.setattr(storage.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(storage.os, "getegid", lambda: 1000, raising=False)
    monkeypatch.setattr(storage.os, "getgroups", lambda: [1001], raising=False)
    monkeypatch.setattr(storage.os, "access", lambda *_args: True)
    monkeypatch.setattr(storage, "_require_posix_ancestor_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(storage, "_require_posix_boundary_owners", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        storage,
        "_directory_entries",
        lambda current: [child] if current is root else [],
    )
    with pytest.raises(storage.StoragePathError, match=message):
        storage._require_posix_storage_policy(root)  # type: ignore[arg-type]


def test_nested_mount_inventory_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_host_acl_probe: None,
) -> None:
    """A non-link bind mount below artifacts still fails the mountinfo proof."""

    checkout = tmp_path / "checkout"
    source = checkout / "data" / "ums"
    (source / "artifacts").mkdir(parents=True)
    (source / "blobs").mkdir()
    storage._create_sentinel(source)
    monkeypatch.setattr(
        storage,
        "_mountpoints_under",
        lambda _path: (source / "artifacts" / "nested",),
    )
    with pytest.raises(storage.StoragePathError, match="mounted"):
        storage.validate_storage_path(source, project_root=checkout)


def test_sentinel_is_bound_to_the_canonical_path(
    tmp_path: Path,
    no_host_acl_probe: None,
) -> None:
    """Copying a marker from another store cannot attest the new source."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    for source in (first, second):
        (source / "artifacts").mkdir(parents=True)
        (source / "blobs").mkdir()
    storage._create_sentinel(first)
    (second / storage.STORAGE_SENTINEL_NAME).write_text(
        (first / storage.STORAGE_SENTINEL_NAME).read_text(),
        encoding="utf-8",
    )
    with pytest.raises(storage.StoragePathError, match="bound"):
        storage.validate_storage_path(second, project_root=checkout)


@pytest.mark.parametrize(
    ("payload_changes", "message"),
    [
        ({"broad_allow": True}, "unapproved principals"),
        ({"deny": True}, "deny entry"),
        ({"root_protected": False}, "inherits parent ACL"),
        ({"root_inheritable": False}, "inheritable operator access"),
        (
            {"root_propagation": "NoPropagateInherit"},
            "inheritable operator access",
        ),
        ({"root_rights_mask": 256}, "operator read/write"),
    ],
)
def test_windows_acl_counterexamples_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload_changes: dict[str, Any],
    message: str,
) -> None:
    """Broad, denied, inherited, non-propagating, and name-only rights fail."""

    source = tmp_path / "store"
    source.mkdir()
    payload = _acl_payload(source, **payload_changes)
    _install_acl_payload(monkeypatch, payload)
    with pytest.raises(storage.StoragePathError, match=message):
        storage._require_windows_host_acl(source)


def test_windows_acl_requires_operator_modify_on_every_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SYSTEM access alone cannot make a child recoverable by the operator."""

    source = tmp_path / "store"
    child = source / "artifacts"
    child.mkdir(parents=True)
    payload = _acl_payload(source, child=child, child_operator=False)
    _install_acl_payload(monkeypatch, payload)
    with pytest.raises(storage.StoragePathError, match="operator read/write"):
        storage._require_windows_host_acl(source)


def test_windows_acl_accepts_exact_numeric_and_inheritance_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A protected root and inherited operator Modify on a child are sufficient."""

    source = tmp_path / "store"
    child = source / "artifacts"
    child.mkdir(parents=True)
    _install_acl_payload(monkeypatch, _acl_payload(source, child=child))
    storage._require_windows_host_acl(source)


def test_windows_acl_query_uses_real_powershell_module_on_windows(tmp_path: Path) -> None:
    """The shipped Get-Acl query executes on Windows without skip or host-DACL claims."""

    if os.name != "nt":
        assert "Import-Module Microsoft.PowerShell.Security" in storage._WINDOWS_ACL_QUERY
        return
    command, environment = storage._windows_acl_process_spec()
    environment["UMS_STORAGE_ACL_PATH"] = str(tmp_path)
    result = subprocess.run(
        [*command, "-Command", storage._WINDOWS_ACL_QUERY],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["CurrentSid"].startswith("S-")
    assert storage._windows_path_key(str(tmp_path)) in {
        storage._windows_path_key(path) for path in payload["Paths"]
    }
    assert Path(environment["PSModulePath"]).parent == Path(command[0]).resolve().parent


def test_compose_has_no_root_initializer_or_forgeable_marker() -> None:
    """Direct Compose exposes no privileged path-mutating service."""

    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    launcher = (SCRIPT_DIRECTORY / "compose.py").read_text(encoding="utf-8")
    assert compose.count("type: bind") == 4
    assert compose.count("create_host_path: false") == 4
    assert compose.count("image: ${UMS_APP_IMAGE:-ums-smart-revenue:dev}") == 3
    assert compose.count("pull_policy: never") == 3
    assert compose.count('user: "10001:10001"') == 3
    assert "app-data-init:" not in compose
    assert 'user: "0:0"' not in compose
    assert "UMS_APP_DATA_HOST_PREFLIGHT" not in compose + launcher
    assert "chown " not in launcher
    assert "chmod " not in launcher
    assert '"--user",\n            "0:0"' not in launcher
    assert ".ums-write-probe-1" not in launcher
    assert "config --quiet" in compose
    assert "container `stat` mode is not a Windows ACL proof" in compose


def test_rendered_model_accepts_only_the_exact_reviewed_projection(tmp_path: Path) -> None:
    """Both app services must share one safe non-root bind in the complete model."""

    source = (tmp_path / "store").resolve()
    assert (
        compose_launcher._validate_rendered_model(
            _storage_model(source),
            project_root=PROJECT_ROOT,
        )
        == source
    )


def test_rendered_model_requires_same_immutable_image_for_all_app_actions(
    tmp_path: Path,
) -> None:
    """Migrate cannot drift to a mutable tag while app uses the reviewed build ID."""

    source = (tmp_path / "store").resolve()
    image_id = "sha256:" + "b" * 64
    model = _storage_model(source)
    services = model["services"]
    assert isinstance(services, dict)
    for name in compose_launcher.APPLICATION_IMAGE_SERVICES:
        service = services[name]
        assert isinstance(service, dict)
        service["image"] = image_id
    assert (
        compose_launcher._validate_rendered_model(
            model,
            project_root=PROJECT_ROOT,
            expected_image=image_id,
        )
        == source
    )
    migrate = services["migrate"]
    assert isinstance(migrate, dict)
    migrate["image"] = compose_launcher.STORAGE_IMAGE
    with pytest.raises(compose_launcher.StoragePathError, match="migrate changes"):
        compose_launcher._validate_rendered_model(
            model,
            project_root=PROJECT_ROOT,
            expected_image=image_id,
        )


def test_guarded_model_rejects_source_rewrite_even_when_canonical_path_matches(
    tmp_path: Path,
) -> None:
    """Each POSIX child-fd spelling must survive Compose without path fallback."""

    source = (tmp_path / "store").resolve()
    rewritten = source / "transient" / ".." / "blobs"
    model = _storage_model(source)
    services = model["services"]
    assert isinstance(services, dict)
    for name in compose_launcher.APP_SERVICES:
        service = services[name]
        assert isinstance(service, dict)
        volumes = service["volumes"]
        assert isinstance(volumes, list)
        mount = volumes[-1]
        assert isinstance(mount, dict)
        mount["source"] = str(rewritten)
    assert rewritten.resolve(strict=False) == source / "blobs"
    with pytest.raises(compose_launcher.StoragePathError, match="rewrote"):
        compose_launcher._validate_rendered_model(
            model,
            project_root=PROJECT_ROOT,
            expected_storage_sources={child: source / child for child in storage.STORAGE_CHILDREN},
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-service",
        "missing-service",
        "project-name",
        "root-user",
        "missing-user",
        "different-image",
        "migrate-image",
        "migrate-root-user",
        "mutable-pull-policy",
        "different-build",
        "profile-bypass",
        "backend-rw",
        "privileged",
        "cap-add",
        "create-host-path",
        "different-source",
        "nested-target",
        "ambiguous-root-target",
        "alternate-source-access",
    ],
)
def test_rendered_model_mutations_fail_closed(tmp_path: Path, mutation: str) -> None:
    """Rendered submount, image/user, service, and alternate-access bypasses fail."""

    source = (tmp_path / "store").resolve()
    model = _storage_model(source)
    services = model["services"]
    assert isinstance(services, dict)
    app = services["app"]
    app_dev = services["app-dev"]
    assert isinstance(app, dict) and isinstance(app_dev, dict)
    app_volumes = app["volumes"]
    assert isinstance(app_volumes, list)
    app_mount = app_volumes[0]
    assert isinstance(app_mount, dict)

    if mutation == "extra-service":
        services["rogue"] = {"volumes": []}
    elif mutation == "missing-service":
        del services["migrate"]
    elif mutation == "project-name":
        model["name"] = "attacker-project"
    elif mutation == "root-user":
        app["user"] = "0:0"
    elif mutation == "missing-user":
        del app["user"]
    elif mutation == "different-image":
        app["image"] = "attacker:latest"
    elif mutation == "migrate-image":
        migrate = services["migrate"]
        assert isinstance(migrate, dict)
        migrate["image"] = "attacker:latest"
    elif mutation == "migrate-root-user":
        migrate = services["migrate"]
        assert isinstance(migrate, dict)
        migrate["user"] = "0:0"
    elif mutation == "mutable-pull-policy":
        app["pull_policy"] = "missing"
    elif mutation == "different-build":
        app["build"] = {"context": str(tmp_path), "dockerfile": "Dockerfile"}
    elif mutation == "profile-bypass":
        app_dev["profiles"] = None
    elif mutation == "backend-rw":
        app_dev_volumes = app_dev["volumes"]
        assert isinstance(app_dev_volumes, list)
        backend_mount = app_dev_volumes[0]
        assert isinstance(backend_mount, dict)
        backend_mount["read_only"] = False
    elif mutation == "privileged":
        app["privileged"] = True
    elif mutation == "cap-add":
        app["cap_add"] = ["SYS_ADMIN"]
    elif mutation == "create-host-path":
        app_mount["bind"] = {"create_host_path": True}
    elif mutation == "different-source":
        app_dev_volumes = app_dev["volumes"]
        assert isinstance(app_dev_volumes, list)
        app_dev_mount = app_dev_volumes[-1]
        assert isinstance(app_dev_mount, dict)
        app_dev_mount["source"] = str(tmp_path / "other")
    elif mutation == "nested-target":
        app_volumes.append(
            {
                "type": "bind",
                "source": str(tmp_path / "other"),
                "target": "/var/lib/ums/artifacts/nested",
                "bind": {"create_host_path": False},
            }
        )
    elif mutation == "ambiguous-root-target":
        app_mount["target"] = "/var/lib/ums/artifacts/."
    elif mutation == "alternate-source-access":
        migrate = services["migrate"]
        assert isinstance(migrate, dict)
        migrate["volumes"] = [
            {
                "type": "bind",
                "source": str(source),
                "target": "/tmp/store",
                "bind": {},
            }
        ]
    else:
        raise AssertionError(f"unhandled mutation {mutation}")

    with pytest.raises(compose_launcher.StoragePathError):
        compose_launcher._validate_rendered_model(model, project_root=PROJECT_ROOT)


def test_rendered_model_input_is_not_mutated(tmp_path: Path) -> None:
    """Validation is a pure proof and leaves Compose JSON unchanged."""

    model = _storage_model((tmp_path / "store").resolve())
    before = deepcopy(model)
    compose_launcher._validate_rendered_model(model, project_root=PROJECT_ROOT)
    assert model == before


@pytest.mark.parametrize(
    "arguments",
    [
        ["watch"],
        ["restart", "app"],
        ["--dry-run", "up", "app"],
        ["up", "--dry-run", "app"],
        ["up", "--scale", "postgres=1", "postgres"],
        ["up", "--wait-timeout", "1", "app"],
        ["up", "--build", "app"],
        ["up", "app", "--build"],
        ["run", "--user", "0", "app"],
        ["run", "--rm", "--volume", "C:\\x:/var/lib/ums", "app"],
        ["exec", "-u", "0", "app", "sh"],
        ["exec", "--privileged", "postgres", "sh"],
        ["-f", "override.yml", "up", "app"],
        ["down", "-v"],
        ["config"],
        ["--profile", "dev", "up"],
        ["--profile", "dev", "up", "app"],
        ["--profile", "dev", "run", "--rm", "migrate"],
        ["--profile", "dev", "logs", "app-dev"],
        ["--profile", "dev", "config", "--quiet"],
        ["--profile", "dev", "down"],
        ["--profile", "other", "up", "app-dev"],
    ],
)
def test_narrow_command_grammar_rejects_every_known_parser_bypass(
    arguments: list[str],
) -> None:
    """Unknown/value-taking/global-command option ambiguity never falls through."""

    with pytest.raises(compose_launcher.StoragePathError):
        compose_launcher._parse_request(arguments)


@pytest.mark.parametrize(
    ("arguments", "requires_storage", "normalized"),
    [
        (["up", "-d"], True, ("up", "--detach")),
        (["up", "postgres"], False, ("up", "postgres")),
        (
            ["--profile", "dev", "up", "app-dev"],
            True,
            ("--profile", "dev", "up", "app-dev"),
        ),
        (["run", "--rm", "migrate"], False, ("run", "--rm", "migrate")),
        (["logs", "-f", "app"], False, ("logs", "--follow", "app")),
        (["stop", "app", "app-dev"], False, ("stop", "app", "app-dev")),
        (["down"], False, ("down",)),
        (["config", "--quiet"], False, ("config", "--quiet")),
        (["ps"], False, ("ps",)),
    ],
)
def test_narrow_command_grammar_accepts_only_documented_workflows(
    arguments: list[str],
    requires_storage: bool,
    normalized: tuple[str, ...],
) -> None:
    """The accepted surface is explicit and produces canonical Compose argv."""

    request = compose_launcher._parse_request(arguments)
    assert request.requires_storage is requires_storage
    assert request.compose_args == normalized


@pytest.mark.parametrize(
    ("arguments", "requires_image"),
    [
        (["up"], True),
        (["up", "postgres"], False),
        (["up", "migrate"], True),
        (["up", "app"], True),
        (["--profile", "dev", "up", "app-dev"], True),
        (["run", "--rm", "migrate"], True),
        (["logs", "app"], False),
    ],
)
def test_image_build_is_required_exactly_when_an_app_image_can_start(
    arguments: list[str],
    requires_image: bool,
) -> None:
    """Migrate and app starts pin a build ID; lifecycle reads never rebuild."""

    assert compose_launcher._parse_request(arguments).requires_image is requires_image


def test_storage_up_identifies_exactly_one_container_for_post_create_inspection() -> None:
    """Every accepted storage start maps to one exact Compose service inventory."""

    assert compose_launcher._parse_request(["up"]).storage_services == ("app",)
    assert compose_launcher._parse_request(["up", "app"]).storage_services == ("app",)
    assert compose_launcher._parse_request(
        ["--profile", "dev", "up", "app-dev"]
    ).storage_services == ("app-dev",)
    assert compose_launcher._parse_request(["up", "postgres"]).storage_services == ()


def test_local_docker_transport_rejects_arbitrary_named_pipes() -> None:
    """Only the two Docker-owned Windows pipes and the exact Unix socket pass."""

    assert compose_launcher._local_endpoint("npipe:////./pipe/docker_engine")
    assert compose_launcher._local_endpoint("npipe:////./pipe/dockerDesktopLinuxEngine")
    assert compose_launcher._local_endpoint("unix:///var/run/docker.sock")
    assert not compose_launcher._local_endpoint("npipe:////./pipe/attacker")
    assert not compose_launcher._local_endpoint("npipe:////server/pipe/docker_engine")
    assert not compose_launcher._local_endpoint("")
    assert not compose_launcher._local_endpoint("tcp://127.0.0.1:2375")
    assert not compose_launcher._local_endpoint("ssh://localhost/run/docker.sock")


def test_local_context_proof_pins_endpoint_for_later_daemon_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A context-file swap after proof cannot redirect the final Docker command."""

    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="desktop-linux\n", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout='"npipe:////./pipe/dockerDesktopLinuxEngine"\n',
                stderr="",
            ),
        ]
    )
    monkeypatch.setattr(
        compose_launcher,
        "_run_checked",
        lambda *_args, **_kwargs: next(responses),
    )
    pinned = compose_launcher._require_local_docker_context(
        cwd=tmp_path,
        env={
            "PATH": "safe",
            "DOCKER_CONTEXT": "desktop-linux",
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_CERT_PATH": "attacker",
        },
    )
    assert pinned == {
        "PATH": "safe",
        "DOCKER_HOST": "npipe:////./pipe/dockerDesktopLinuxEngine",
    }


def test_compose_environment_pins_all_launcher_owned_controls() -> None:
    """Project, profile, file, default-env, and image behavior are exact."""

    environment = compose_launcher._compose_environment({"PATH": "safe"})
    assert environment == {
        "PATH": "safe",
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "COMPOSE_FILE": str(compose_launcher.COMPOSE_FILE),
        "COMPOSE_PROFILES": "",
        "COMPOSE_PROJECT_NAME": compose_launcher.PROJECT_NAME,
        "UMS_APP_IMAGE": compose_launcher.STORAGE_IMAGE,
    }
    for control in (
        "COMPOSE_FILE",
        "compose_profiles",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_ENV_FILES",
        "COMPOSE_PATH_SEPARATOR",
    ):
        with pytest.raises(compose_launcher.StoragePathError, match=r"COMPOSE_\*"):
            compose_launcher._compose_environment({control: "override"})
    with pytest.raises(compose_launcher.StoragePathError, match="UMS_APP_IMAGE"):
        compose_launcher._compose_environment({"ums_app_image": "attacker:latest"})


@pytest.mark.parametrize(
    "assignment",
    [
        "COMPOSE_FILE=override.yml",
        "export COMPOSE_PROFILES=rogue",
        "COMPOSE_PROJECT_NAME=rogue",
        "COMPOSE_ENV_FILES=second.env",
        "COMPOSE_PATH_SEPARATOR=,",
        "COMPOSE_REMOVE_ORPHANS: true",
        "  compose_remove_orphans : false",
        "export COMPOSE_PARALLEL_LIMIT : 99",
        "COMPOSE_EXPERIMENTAL",
        "UMS_APP_IMAGE=attacker:latest",
        "UMS_APP_ARTIFACTS_HOST: /tmp/attacker",
        "UMS_APP_BLOBS_HOST = /tmp/attacker",
    ],
)
def test_env_file_cannot_restore_reserved_compose_behavior(
    tmp_path: Path,
    assignment: str,
) -> None:
    """The exact bytes later read by Compose reject every behavioral override."""

    env_file = tmp_path / "operator.env"
    env_file.write_text(f"UMS_DB_USER=operator\n{assignment}\n", encoding="utf-8")
    request = compose_launcher._parse_request(["--env-file", str(env_file), "config", "--quiet"])
    with pytest.raises(compose_launcher.StoragePathError, match="reserved launcher variable"):
        with compose_launcher._isolated_env_request(request, cwd=tmp_path):
            pytest.fail("reserved env assignment must fail before Compose")


def test_default_env_is_snapshotted_once_and_only_private_copy_is_used(
    tmp_path: Path,
) -> None:
    """A post-validation replacement of .env cannot change Compose behavior."""

    source = tmp_path / ".env"
    payload = b"UMS_DB_USER=operator\nUMS_DB_PASSWORD=TOP-SECRET\n"
    source.write_bytes(payload)
    request = compose_launcher._parse_request(["config", "--quiet"])
    with compose_launcher._isolated_env_request(request, cwd=tmp_path) as isolated:
        snapshot = Path(isolated.global_args[1])
        assert snapshot != source
        assert snapshot.read_bytes() == payload
        source.write_text("COMPOSE_FILE=attacker.yml\n", encoding="utf-8")
        assert snapshot.read_bytes() == payload
    assert not snapshot.exists()


def test_config_executes_only_quiet_and_never_contacts_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secret-bearing rendered YAML has no supported stdout-producing route."""

    calls: list[tuple[list[str], bool]] = []

    def fake_checked(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture: bool = False,
    ) -> SimpleNamespace:
        del cwd, env
        calls.append((command, capture))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(compose_launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(compose_launcher, "COMPOSE_FILE", tmp_path / "docker-compose.yml")
    monkeypatch.setattr(compose_launcher, "_run_checked", fake_checked)
    monkeypatch.setattr(
        compose_launcher,
        "_require_local_docker_context",
        lambda **_kwargs: pytest.fail("config --quiet must not contact the daemon"),
    )
    assert compose_launcher.main(["config", "--quiet"]) == 0
    assert calls == [(["docker", "compose", "config", "--quiet"], True)]


def test_public_config_failure_captures_and_redacts_malformed_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed quoted secrets from Compose stderr never reach the terminal."""

    secret = "TOP-SECRET-UNTERMINATED-QUOTE"

    def fake_checked(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture: bool = False,
    ) -> SimpleNamespace:
        del cwd, env
        assert capture is True
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f'unexpected character in UMS_DB_PASSWORD="{secret}',
        )

    monkeypatch.setattr(compose_launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(compose_launcher, "COMPOSE_FILE", tmp_path / "docker-compose.yml")
    monkeypatch.setattr(compose_launcher, "_run_checked", fake_checked)
    assert compose_launcher.main(["config", "--quiet"]) == 2
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert output.out == ""
    assert output.err == (
        "storage preflight failed: the pinned Compose configuration is invalid; "
        "inspect the env file syntax\n"
    )


def test_internal_config_failure_never_replays_captured_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Captured config stdout/stderr is replaced by one generic operator error."""

    secret = "TOP-SECRET-RENDERED-VALUE"
    monkeypatch.setattr(
        compose_launcher,
        "_run_checked",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=f"UMS_DB_PASSWORD: {secret}",
            stderr=f"failed near {secret}",
        ),
    )
    request = compose_launcher._parse_request(["config", "--quiet"])
    with pytest.raises(compose_launcher.StoragePathError) as captured:
        compose_launcher._render_model(request, cwd=tmp_path, env={})
    assert secret not in str(captured.value)
    output = capsys.readouterr()
    assert secret not in output.out + output.err


def test_non_storage_action_does_not_render_or_touch_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supported stop remains available when the bind is missing or unsafe."""

    captured: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.append(command)
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(compose_launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        compose_launcher,
        "COMPOSE_FILE",
        tmp_path / "docker-compose.yml",
    )
    monkeypatch.setattr(compose_launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(
        compose_launcher,
        "_render_model",
        lambda *_args, **_kwargs: pytest.fail("stop must not render"),
    )
    proofs: list[dict[str, str]] = []

    def prove_local(*, cwd: Path, env: dict[str, str]) -> dict[str, str]:
        del cwd
        proofs.append(env)
        return {**env, "DOCKER_HOST": "npipe:////./pipe/docker_engine"}

    monkeypatch.setattr(compose_launcher, "_require_local_docker_context", prove_local)
    assert compose_launcher.main(["stop", "app"]) == 17
    assert len(proofs) == 1
    assert captured == [["docker", "compose", "stop", "app"]]


@pytest.mark.parametrize(
    "arguments",
    [
        ["up", "postgres"],
        ["run", "--rm", "migrate"],
        ["logs", "app"],
        ["stop", "app"],
        ["down"],
        ["ps"],
    ],
)
def test_every_daemon_action_rejects_remote_host_before_contact(
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote DOCKER_HOST cannot reach even non-storage lifecycle actions."""

    monkeypatch.setattr(compose_launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(compose_launcher, "COMPOSE_FILE", tmp_path / "docker-compose.yml")
    monkeypatch.setenv("DOCKER_HOST", "tcp://attacker.example:2375")
    monkeypatch.setattr(
        compose_launcher,
        "_run_checked",
        lambda *_args, **_kwargs: pytest.fail("remote action must stop before Docker"),
    )
    assert compose_launcher.main(arguments) == 2


def test_invalid_rendered_start_fails_before_host_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No host mkdir/sentinel write happens until the rendered model is proven."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(compose_launcher, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(
        compose_launcher,
        "COMPOSE_FILE",
        checkout / "docker-compose.yml",
    )
    monkeypatch.setattr(
        compose_launcher,
        "_render_model",
        lambda *_args, **_kwargs: {"services": {}},
    )
    monkeypatch.setattr(
        compose_launcher,
        "_require_local_docker_context",
        lambda *, cwd, env: {**env, "DOCKER_HOST": "npipe:////./pipe/docker_engine"},
    )
    monkeypatch.setattr(
        compose_launcher,
        "prepare_storage_path",
        lambda *_args, **_kwargs: pytest.fail("invalid model must not prepare storage"),
    )
    assert compose_launcher.main(["up", "app"]) == 2
    assert tuple(checkout.iterdir()) == ()


def test_image_build_returns_and_verifies_only_an_immutable_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing mutable tag is irrelevant to exact build provenance."""

    calls: list[tuple[list[str], bool]] = []
    image_id = "sha256:" + "a" * 64

    def fake_checked(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture: bool = False,
    ) -> SimpleNamespace:
        del cwd, env
        calls.append((command, capture))
        if command[1] == "build":
            return SimpleNamespace(returncode=0, stdout=f"{image_id}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(image_id), stderr="")

    monkeypatch.setattr(compose_launcher, "_run_checked", fake_checked)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    result = compose_launcher._build_reviewed_image(
        cwd=tmp_path,
        env={"DOCKER_HOST": "npipe:////./pipe/docker_engine"},
    )
    assert result == image_id
    assert calls[0][0] == [
        "docker",
        "build",
        "--quiet",
        "--pull=false",
        "--file",
        str(dockerfile),
        str(tmp_path),
    ]
    assert calls[1][0] == [
        "docker",
        "image",
        "inspect",
        image_id,
        "--format",
        "{{json .Id}}",
    ]
    assert all(capture for _command, capture in calls)
    assert all(compose_launcher.STORAGE_IMAGE not in command for command, _capture in calls)


def test_mutable_tag_build_output_is_rejected_as_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker returning a tag instead of a content ID cannot reach Compose."""

    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(
        compose_launcher,
        "_run_checked",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="ums-smart-revenue:dev\n",
            stderr="TOP-SECRET",
        ),
    )
    with pytest.raises(compose_launcher.StoragePathError, match="immutable image ID"):
        compose_launcher._build_reviewed_image(
            cwd=tmp_path,
            env={"DOCKER_HOST": "npipe:////./pipe/docker_engine"},
        )


def test_non_root_probe_has_no_host_mount_string_or_privilege_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Comma-bearing sources cannot inject a second mount into an internal command."""

    calls: list[list[str]] = []

    def fake_checked(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture: bool = False,
    ) -> SimpleNamespace:
        del cwd, env, capture
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(compose_launcher, "_run_checked", fake_checked)
    request = compose_launcher._parse_request(["up", "app"])
    compose_launcher._probe_storage_writable(
        request=request,
        cwd=tmp_path,
        env={"DOCKER_HOST": "npipe:////./pipe/docker_engine"},
    )
    probe = calls[-1]
    assert "docker" == probe[0]
    assert probe[1:3] == ["compose", "run"]
    assert "--mount" not in probe
    assert "--volume" not in probe
    assert "--user" not in probe
    assert "0:0" not in probe
    assert probe[probe.index("--entrypoint") + 1] == "python"
    assert "chown" not in compose_launcher.STORAGE_WRITE_PROBE
    assert "chmod" not in compose_launcher.STORAGE_WRITE_PROBE
    assert ".ums-write-probe-1" not in compose_launcher.STORAGE_WRITE_PROBE
    assert "secrets.token_hex(32)" in compose_launcher.STORAGE_WRITE_PROBE
    assert "os.O_EXCL" in compose_launcher.STORAGE_WRITE_PROBE
    assert "os.O_NOFOLLOW" in compose_launcher.STORAGE_WRITE_PROBE
    assert "dir_fd=" in compose_launcher.STORAGE_WRITE_PROBE
    assert "refusing to delete a replaced probe path" in compose_launcher.STORAGE_WRITE_PROBE
    comma_source = (tmp_path / "store,readonly").resolve()
    assert (
        compose_launcher._validate_rendered_model(
            _storage_model(comma_source),
            project_root=PROJECT_ROOT,
        )
        == comma_source
    )
    assert str(comma_source) not in probe


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("wrong-source", "wrong artifacts source"),
        ("process-fd", "bind source|wrong artifacts source"),
        ("read-only", "artifacts bind mode"),
        ("missing-child", "missing a durable child bind"),
    ],
)
def test_post_create_mount_mismatch_removes_only_the_scoped_app_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    """Docker's persisted mount projection must match or the app is removed."""

    source = (tmp_path / "store").resolve()
    expected_sources = {child: source / child for child in storage.STORAGE_CHILDREN}
    request = compose_launcher._parse_request(["up", "--detach", "app"])
    container_id = "d" * 64
    calls: list[tuple[list[str], bool]] = []

    class Guard:
        def assert_current(self) -> None:
            return

    mounts = [
        {
            "Type": "bind",
            "Source": str(expected_sources[child]),
            "Destination": target,
            "RW": True,
        }
        for child, target in compose_launcher.STORAGE_TARGETS.items()
    ]
    if mutation == "wrong-source":
        mounts[0]["Source"] = str(tmp_path / "attacker")
    elif mutation == "process-fd":
        mounts[0]["Source"] = "/proc/4242/fd/7"
    elif mutation == "read-only":
        mounts[0]["RW"] = False
    elif mutation == "missing-child":
        mounts.pop()
    else:
        raise AssertionError(f"unhandled mutation: {mutation}")

    def fake_daemon(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture: bool = False,
    ) -> SimpleNamespace:
        del cwd, env
        calls.append((command, capture))
        if command == ["docker", "compose", "up", "--detach", "app"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["docker", "compose", "ps", "--all", "--quiet", "app"]:
            return SimpleNamespace(returncode=0, stdout=f"{container_id}\n", stderr="")
        if command[:3] == ["docker", "container", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(mounts), stderr="")
        if command == ["docker", "container", "rm", "--force", container_id]:
            return SimpleNamespace(returncode=0, stdout=container_id, stderr="")
        raise AssertionError(f"unexpected daemon command: {command!r}")

    monkeypatch.setattr(compose_launcher, "_run_daemon_checked", fake_daemon)
    with pytest.raises(compose_launcher.StoragePathError, match=message):
        compose_launcher._run_guarded_storage_up(
            request,
            cwd=tmp_path,
            env={"DOCKER_HOST": "npipe:////./pipe/docker_engine"},
            identity_guard=Guard(),  # type: ignore[arg-type]
            expected_sources=expected_sources,
        )
    removal = [command for command, _capture in calls if command[1:3] == ["container", "rm"]]
    assert removal == [["docker", "container", "rm", "--force", container_id]]
    assert all("--volumes" not in command and "-v" not in command for command, _ in calls)


def test_nonzero_partial_create_audits_all_ids_and_removes_only_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed detached up can leave an app container that must be audited."""

    source = (tmp_path / "store").resolve()
    expected_sources = {child: source / child for child in storage.STORAGE_CHILDREN}
    request = compose_launcher._parse_request(["up", "app"])
    good_id = "e" * 64
    bad_id = "f" * 64
    calls: list[tuple[list[str], bool]] = []
    assertions = 0

    class Guard:
        def assert_current(self) -> None:
            nonlocal assertions
            assertions += 1

    def mounts_for(identifier: str) -> list[dict[str, object]]:
        mounts = [
            {
                "Type": "bind",
                "Source": str(expected_sources[child]),
                "Destination": target,
                "RW": True,
            }
            for child, target in compose_launcher.STORAGE_TARGETS.items()
        ]
        if identifier == bad_id:
            mounts[0]["Source"] = str(tmp_path / "attacker")
        return mounts

    def fake_daemon(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture: bool = False,
    ) -> SimpleNamespace:
        del cwd, env
        calls.append((command, capture))
        if command == ["docker", "compose", "up", "--detach", "app"]:
            assert capture is False
            return SimpleNamespace(returncode=37, stdout="", stderr="partial failure")
        if command == ["docker", "compose", "ps", "--all", "--quiet", "app"]:
            assert capture is True
            return SimpleNamespace(
                returncode=0,
                stdout=f"{good_id}\n{bad_id}\n",
                stderr="",
            )
        if tuple(command) in {
            (
                "docker",
                "container",
                "inspect",
                good_id,
                "--format",
                "{{json .Mounts}}",
            ),
            (
                "docker",
                "container",
                "inspect",
                bad_id,
                "--format",
                "{{json .Mounts}}",
            ),
        }:
            assert capture is True
            identifier = command[3]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(mounts_for(identifier)),
                stderr="",
            )
        if command == ["docker", "container", "rm", "--force", bad_id]:
            assert capture is True
            return SimpleNamespace(returncode=0, stdout=bad_id, stderr="")
        raise AssertionError(f"unexpected daemon command: {command!r}")

    monkeypatch.setattr(compose_launcher, "_run_daemon_checked", fake_daemon)
    completed = compose_launcher._run_guarded_storage_up(
        request,
        cwd=tmp_path,
        env={"DOCKER_HOST": "npipe:////./pipe/docker_engine"},
        identity_guard=Guard(),  # type: ignore[arg-type]
        expected_sources=expected_sources,
    )

    assert completed.returncode == 37
    assert assertions == 2
    inspections = [
        command[3] for command, _ in calls if command[:3] == ["docker", "container", "inspect"]
    ]
    assert inspections == [good_id, bad_id]
    removals = [command for command, _ in calls if command[1:3] == ["container", "rm"]]
    assert removals == [["docker", "container", "rm", "--force", bad_id]]
    assert all("--volumes" not in command and "-v" not in command for command, _ in calls)


@pytest.mark.parametrize(
    ("ps_returncode", "malformed_line"),
    [
        (0, "not-an-id"),
        (17, None),
    ],
    ids=["mixed-malformed-output", "nonzero-with-valid-output"],
)
def test_failed_create_remediates_valid_ids_before_untrusted_ps_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ps_returncode: int,
    malformed_line: str | None,
) -> None:
    """Partial ps output must not hide a valid unsafe app container ID."""

    source = (tmp_path / "store").resolve()
    expected_sources = {child: source / child for child in storage.STORAGE_CHILDREN}
    request = compose_launcher._parse_request(["up", "app"])
    container_id = "b" * 64
    ps_lines = [container_id]
    if malformed_line is not None:
        ps_lines.append(malformed_line)
    calls: list[tuple[list[str], bool]] = []
    mounts = [
        {
            "Type": "bind",
            "Source": str(expected_sources[child]),
            "Destination": target,
            "RW": True,
        }
        for child, target in compose_launcher.STORAGE_TARGETS.items()
    ]
    mounts[0]["Source"] = str(tmp_path / "attacker")

    class Guard:
        def assert_current(self) -> None:
            return

    def fake_daemon(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture: bool = False,
    ) -> SimpleNamespace:
        del cwd, env
        calls.append((command, capture))
        if command == ["docker", "compose", "up", "--detach", "app"]:
            assert capture is False
            return SimpleNamespace(returncode=41, stdout="", stderr="partial failure")
        if command == ["docker", "compose", "ps", "--all", "--quiet", "app"]:
            assert capture is True
            return SimpleNamespace(
                returncode=ps_returncode,
                stdout="\n".join(ps_lines) + "\n",
                stderr="enumeration failed" if ps_returncode else "",
            )
        if command == [
            "docker",
            "container",
            "inspect",
            container_id,
            "--format",
            "{{json .Mounts}}",
        ]:
            assert capture is True
            return SimpleNamespace(returncode=0, stdout=json.dumps(mounts), stderr="")
        if command == ["docker", "container", "rm", "--force", container_id]:
            assert capture is True
            return SimpleNamespace(returncode=0, stdout=container_id, stderr="")
        raise AssertionError(f"unexpected daemon command: {command!r}")

    monkeypatch.setattr(compose_launcher, "_run_daemon_checked", fake_daemon)
    with pytest.raises(
        compose_launcher.StoragePathError,
        match="cannot prove complete enumeration",
    ):
        compose_launcher._run_guarded_storage_up(
            request,
            cwd=tmp_path,
            env={"DOCKER_HOST": "npipe:////./pipe/docker_engine"},
            identity_guard=Guard(),  # type: ignore[arg-type]
            expected_sources=expected_sources,
        )

    inspections = [command for command, _ in calls if command[1:3] == ["container", "inspect"]]
    assert inspections == [
        [
            "docker",
            "container",
            "inspect",
            container_id,
            "--format",
            "{{json .Mounts}}",
        ]
    ]
    removals = [command for command, _ in calls if command[1:3] == ["container", "rm"]]
    assert removals == [["docker", "container", "rm", "--force", container_id]]
    assert all("not-an-id" not in command for command, _ in calls)
    assert all("--volumes" not in command and "-v" not in command for command, _ in calls)


def test_main_keeps_identity_and_immutable_image_pinned_through_final_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creation and restart-stable mount inspection stay inside the same guard."""

    source = (tmp_path / "store").resolve()
    image_id = "sha256:" + "c" * 64
    state = {"guard_active": False, "assertions": 0, "final_calls": 0, "inspections": 0}

    def rendered_model(
        _request: compose_launcher.LaunchRequest,
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> dict[str, object]:
        assert cwd == tmp_path
        model = _storage_model(source)
        services = model["services"]
        assert isinstance(services, dict)
        for name in compose_launcher.APPLICATION_IMAGE_SERVICES:
            service = services[name]
            assert isinstance(service, dict)
            service["image"] = env["UMS_APP_IMAGE"]
            build = service["build"]
            assert isinstance(build, dict)
            build["context"] = str(tmp_path)
        app_dev = services["app-dev"]
        assert isinstance(app_dev, dict)
        volumes = app_dev["volumes"]
        assert isinstance(volumes, list)
        backend_mount = volumes[0]
        assert isinstance(backend_mount, dict)
        backend_mount["source"] = str(tmp_path / "backend")
        for service_name in compose_launcher.APP_SERVICES:
            service = services[service_name]
            assert isinstance(service, dict)
            service_volumes = service["volumes"]
            assert isinstance(service_volumes, list)
            for mount in service_volumes:
                assert isinstance(mount, dict)
                for child, target in compose_launcher.STORAGE_TARGETS.items():
                    if mount.get("target") == target:
                        mount["source"] = env.get(
                            compose_launcher.STORAGE_SOURCE_ENV[child],
                            str(source / child),
                        )
        return model

    class Guard:
        def child_source(self, name: str) -> Path:
            return source / name

        def __enter__(self) -> "Guard":
            state["guard_active"] = True
            return self

        def assert_current(self) -> None:
            assert state["guard_active"]
            state["assertions"] += 1

        def __exit__(self, *_args: object) -> None:
            state["guard_active"] = False

    def final_daemon(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture: bool = False,
    ) -> SimpleNamespace:
        assert state["guard_active"]
        assert cwd == tmp_path
        assert env["UMS_APP_IMAGE"] == image_id
        assert env["UMS_APP_ARTIFACTS_HOST"] == str(source / "artifacts")
        assert env["UMS_APP_BLOBS_HOST"] == str(source / "blobs")
        if command == ["docker", "compose", "up", "--detach", "app"]:
            assert capture is False
            state["final_calls"] += 1
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["docker", "compose", "up", "app"]:
            assert capture is False
            state["final_calls"] += 1
            return SimpleNamespace(returncode=23, stdout="", stderr="")
        if command == ["docker", "compose", "ps", "--all", "--quiet", "app"]:
            assert capture is True
            return SimpleNamespace(returncode=0, stdout=f"{'a' * 64}\n", stderr="")
        if command == [
            "docker",
            "container",
            "inspect",
            "a" * 64,
            "--format",
            "{{json .Mounts}}",
        ]:
            assert capture is True
            state["inspections"] += 1
            mounts = [
                {
                    "Type": "bind",
                    "Source": str(source / child),
                    "Destination": target,
                    "RW": True,
                }
                for child, target in compose_launcher.STORAGE_TARGETS.items()
            ]
            return SimpleNamespace(returncode=0, stdout=json.dumps(mounts), stderr="")
        raise AssertionError(f"unexpected daemon command: {command!r}")

    monkeypatch.setattr(compose_launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(compose_launcher, "COMPOSE_FILE", tmp_path / "docker-compose.yml")
    monkeypatch.setattr(compose_launcher, "_render_model", rendered_model)
    monkeypatch.setattr(
        compose_launcher,
        "_require_local_docker_context",
        lambda *, cwd, env: {**env, "DOCKER_HOST": "npipe:////./pipe/docker_engine"},
    )
    monkeypatch.setattr(
        compose_launcher,
        "_build_reviewed_image",
        lambda *, cwd, env: image_id,
    )
    monkeypatch.setattr(compose_launcher, "prepare_storage_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(compose_launcher, "hold_storage_identity", lambda _path: Guard())
    monkeypatch.setattr(
        compose_launcher,
        "_probe_storage_writable",
        lambda **_kwargs: state["guard_active"] or pytest.fail("probe escaped the identity guard"),
    )
    monkeypatch.setattr(compose_launcher, "validate_storage_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(compose_launcher, "_run_daemon_checked", final_daemon)

    assert compose_launcher.main(["up", "app"]) == 23
    assert state == {
        "guard_active": False,
        "assertions": 6,
        "final_calls": 2,
        "inspections": 2,
    }


@pytest.mark.parametrize("race_phase", ["final", "exit"])
@pytest.mark.parametrize("cleanup_returncode", [0, 29], ids=["removed", "cleanup-failed"])
def test_main_late_identity_race_removes_scoped_app_and_returns_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    race_phase: str,
    cleanup_returncode: int,
) -> None:
    """Final and context-exit races must tear down the app or fail closed."""

    source = (tmp_path / "store").resolve()
    image_id = "sha256:" + "c" * 64
    container_id = "a" * 64
    state = {"guard_active": False, "assertions": 0, "guarded_up": 0}
    calls: list[tuple[list[str], bool]] = []

    class Guard:
        def child_source(self, name: str) -> Path:
            return source / name

        def __enter__(self) -> "Guard":
            state["guard_active"] = True
            return self

        def assert_current(self) -> None:
            assert state["guard_active"]
            state["assertions"] += 1
            failure_assertion = 2 if race_phase == "final" else 3
            if state["assertions"] == failure_assertion:
                raise compose_launcher.StoragePathError(f"{race_phase} identity race")

        def __exit__(self, exc_type: object, *_args: object) -> None:
            try:
                if race_phase == "exit" and exc_type is None:
                    self.assert_current()
            finally:
                state["guard_active"] = False

    def guarded_up(
        request: compose_launcher.LaunchRequest,
        *,
        cwd: Path,
        env: dict[str, str],
        identity_guard: object,
        expected_sources: dict[str, Path],
    ) -> SimpleNamespace:
        assert request.storage_services == ("app",)
        assert cwd == tmp_path
        assert env["DOCKER_HOST"] == "npipe:////./pipe/docker_engine"
        assert identity_guard is not None
        assert expected_sources == {child: source / child for child in storage.STORAGE_CHILDREN}
        assert state["guard_active"]
        state["guarded_up"] += 1
        return SimpleNamespace(returncode=0)

    def cleanup_daemon(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture: bool = False,
    ) -> SimpleNamespace:
        assert state["guard_active"] is (race_phase == "final")
        assert cwd == tmp_path
        assert env["DOCKER_HOST"] == "npipe:////./pipe/docker_engine"
        calls.append((command, capture))
        if command == ["docker", "compose", "ps", "--all", "--quiet", "app"]:
            assert capture is True
            return SimpleNamespace(returncode=0, stdout=f"{container_id}\n", stderr="")
        if command == ["docker", "container", "rm", "--force", container_id]:
            assert capture is True
            return SimpleNamespace(
                returncode=cleanup_returncode,
                stdout=container_id if cleanup_returncode == 0 else "",
                stderr="cleanup failed" if cleanup_returncode else "",
            )
        raise AssertionError(f"unexpected daemon command: {command!r}")

    monkeypatch.setattr(compose_launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(compose_launcher, "COMPOSE_FILE", tmp_path / "docker-compose.yml")
    monkeypatch.setattr(compose_launcher, "_render_model", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        compose_launcher,
        "_validate_rendered_model",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(
        compose_launcher,
        "_require_local_docker_context",
        lambda *, cwd, env: {**env, "DOCKER_HOST": "npipe:////./pipe/docker_engine"},
    )
    monkeypatch.setattr(compose_launcher, "_build_reviewed_image", lambda **_kwargs: image_id)
    monkeypatch.setattr(compose_launcher, "prepare_storage_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(compose_launcher, "hold_storage_identity", lambda _path: Guard())
    monkeypatch.setattr(compose_launcher, "_probe_storage_writable", lambda **_kwargs: None)
    monkeypatch.setattr(compose_launcher, "validate_storage_path", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(compose_launcher, "_run_guarded_storage_up", guarded_up)
    monkeypatch.setattr(compose_launcher, "_run_daemon_checked", cleanup_daemon)

    assert compose_launcher.main(["up", "--detach", "app"]) == 2
    expected_assertions = 2 if race_phase == "final" else 3
    assert state == {
        "guard_active": False,
        "assertions": expected_assertions,
        "guarded_up": 1,
    }
    assert calls == [
        (["docker", "compose", "ps", "--all", "--quiet", "app"], True),
        (["docker", "container", "rm", "--force", container_id], True),
    ]
    assert all("--volumes" not in command and "-v" not in command for command, _ in calls)
    stderr = capsys.readouterr().err
    if cleanup_returncode == 0:
        assert f"{race_phase} identity race" in stderr
    elif race_phase == "final":
        assert "final storage identity verification failed" in stderr
    else:
        assert "storage identity context exit failed" in stderr
