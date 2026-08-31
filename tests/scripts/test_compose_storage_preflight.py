"""Mutation-resistant host-only tests for the Compose storage boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
        volumes.append(
            {
                "type": "bind",
                "source": str(source),
                "target": "/var/lib/ums",
                "bind": {"create_host_path": False},
            }
        )
        service: dict[str, object] = {
            "build": {
                "context": str(PROJECT_ROOT),
                "dockerfile": "Dockerfile",
            },
            "image": compose_launcher.STORAGE_IMAGE,
            "user": "10001:10001",
            "entrypoint": ["/usr/bin/tini", "--"],
            "volumes": volumes,
        }
        if dev:
            service["profiles"] = ["dev"]
        return service

    return {
        "services": {
            "postgres": {"volumes": []},
            "redis": {"volumes": []},
            "migrate": {"volumes": []},
            "app": app_service(dev=False),
            "app-dev": app_service(dev=True),
        }
    }


def _acl_payload(
    source: Path,
    *,
    child: Path | None = None,
    broad_allow: bool = False,
    deny: bool = False,
    root_protected: bool = True,
    root_inheritable: bool = True,
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
            "PropagationFlags": "None",
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
    assert compose.count("type: bind") == 2
    assert compose.count("create_host_path: false") == 2
    assert "app-data-init:" not in compose
    assert 'user: "0:0"' not in compose
    assert "UMS_APP_DATA_HOST_PREFLIGHT" not in compose + launcher
    assert "chown " not in launcher
    assert "chmod " not in launcher
    assert '"--user",\n            "0:0"' not in launcher
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


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-service",
        "missing-service",
        "root-user",
        "missing-user",
        "different-image",
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
    elif mutation == "root-user":
        app["user"] = "0:0"
    elif mutation == "missing-user":
        del app["user"]
    elif mutation == "different-image":
        app["image"] = "attacker:latest"
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
                "target": "/var/lib/ums/artifacts",
                "bind": {"create_host_path": False},
            }
        )
    elif mutation == "ambiguous-root-target":
        app_mount["target"] = "/var/lib/ums/."
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
        ["up", "app", "--build"],
        ["run", "--user", "0", "app"],
        ["run", "--rm", "--volume", "C:\\x:/var/lib/ums", "app"],
        ["exec", "-u", "0", "app", "sh"],
        ["exec", "--privileged", "postgres", "sh"],
        ["-f", "override.yml", "up", "app"],
        ["down", "-v"],
        ["--profile", "dev", "up"],
        ["--profile", "dev", "up", "app"],
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
        (["up", "--build", "app"], True, ("up", "--build", "app")),
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


def test_compose_environment_pins_file_and_removes_ambient_controls() -> None:
    """Ambient profile/project/path-separator controls cannot alter invocation."""

    environment = compose_launcher._compose_environment(
        {
            "PATH": "safe",
            "COMPOSE_PROFILES": "rogue",
            "COMPOSE_PROJECT_NAME": "rogue",
            "COMPOSE_PATH_SEPARATOR": ",",
        }
    )
    assert environment == {
        "PATH": "safe",
        "COMPOSE_FILE": str(compose_launcher.COMPOSE_FILE),
    }
    with pytest.raises(compose_launcher.StoragePathError, match="COMPOSE_FILE"):
        compose_launcher._compose_environment({"COMPOSE_FILE": "override.yml"})


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
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    assert compose_launcher.main(["stop", "app"]) == 17
    assert captured == [["docker", "compose", "stop", "app"]]


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
        "prepare_storage_path",
        lambda *_args, **_kwargs: pytest.fail("invalid model must not prepare storage"),
    )
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    assert compose_launcher.main(["up", "app"]) == 2
    assert tuple(checkout.iterdir()) == ()


def test_image_inspection_is_captured_and_never_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Image config/labels containing secrets never reach launcher stdout/stderr."""

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
        return SimpleNamespace(returncode=0, stdout="TOP-SECRET", stderr="")

    monkeypatch.setattr(compose_launcher, "_run_checked", fake_checked)
    request = compose_launcher._parse_request(["up", "app"])
    compose_launcher._ensure_image(request=request, cwd=tmp_path, env={})
    assert calls == [(["docker", "image", "inspect", compose_launcher.STORAGE_IMAGE], True)]


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
    compose_launcher._probe_storage_writable(request=request, cwd=tmp_path, env={})
    probe = calls[-1]
    assert "docker" == probe[0]
    assert probe[1:3] == ["compose", "run"]
    assert "--mount" not in probe
    assert "--volume" not in probe
    assert "--user" not in probe
    assert "0:0" not in probe
    assert "chown" not in compose_launcher.STORAGE_WRITE_PROBE
    assert "chmod" not in compose_launcher.STORAGE_WRITE_PROBE
    comma_source = (tmp_path / "store,readonly").resolve()
    assert (
        compose_launcher._validate_rendered_model(
            _storage_model(comma_source),
            project_root=PROJECT_ROOT,
        )
        == comma_source
    )
    assert str(comma_source) not in probe
