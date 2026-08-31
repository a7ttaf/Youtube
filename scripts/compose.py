"""Run the supported Docker Compose workflow behind a narrow safety boundary.

This launcher is intentionally not a transparent Docker Compose proxy. It
accepts only the operator workflows documented by ``docker-compose.yml`` and
validates the authoritative rendered model before an application container can
write the durable host bind.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from validate_compose_storage_path import (
    StoragePathError,
    hold_storage_identity,
    prepare_storage_path,
    validate_storage_path,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
PROJECT_NAME = "ums-smart-revenue"
STORAGE_IMAGE = "ums-smart-revenue:dev"
STORAGE_TARGET = "/var/lib/ums"
EXPECTED_SERVICES = {"postgres", "redis", "migrate", "app", "app-dev"}
APP_SERVICES = {"app", "app-dev"}
APPLICATION_IMAGE_SERVICES = {"migrate", "app", "app-dev"}
DAEMON_ACTIONS = {"up", "run", "logs", "stop", "down", "ps"}
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?:=|$)")

# FIX: PID-derived probe names could be pre-created and truncated. The probe
# now runs as the exact uid/gid declared by the rendered app service and
# opens unpredictable names with O_EXCL + O_NOFOLLOW relative to already-open
# directory descriptors. Cleanup checks the still-open file identity first and
# unlinks only a file this invocation proved it created.
STORAGE_WRITE_PROBE = r"""
import os
import secrets
import stat

root = os.environ.get("UMS_STORAGE_PROBE_ROOT", "/var/lib/ums")
required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
if any(not hasattr(os, name) for name in required) or os.open not in os.supports_dir_fd:
    raise SystemExit("storage probe: no secure no-follow directory API")

directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
root_fd = os.open(root, directory_flags)
directory_fds = []
created = []
failure = None
try:
    for child in ("artifacts", "blobs"):
        directory_fd = os.open(child, directory_flags, dir_fd=root_fd)
        directory_fds.append(directory_fd)
        name = ".ums-write-probe-" + secrets.token_hex(32)
        file_fd = os.open(name, file_flags, 0o600, dir_fd=directory_fd)
        metadata = os.fstat(file_fd)
        created.append((directory_fd, name, file_fd, metadata.st_dev, metadata.st_ino))
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("storage probe: exclusive create was not a regular file")
        os.write(file_fd, b"ums-storage-probe\n")
        os.fsync(file_fd)
finally:
    for directory_fd, name, file_fd, device, inode in reversed(created):
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
                device,
                inode,
            ):
                failure = "storage probe: refusing to delete a replaced probe path"
            else:
                os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            failure = "storage probe: created probe disappeared before cleanup"
        except OSError:
            failure = "storage probe: cannot safely remove its created probe"
        finally:
            os.close(file_fd)
    for directory_fd in reversed(directory_fds):
        os.close(directory_fd)
    os.close(root_fd)
if failure is not None:
    raise SystemExit(failure)
""".strip()


@dataclass(frozen=True)
class LaunchRequest:
    """One parsed and normalized launcher invocation."""

    compose_args: tuple[str, ...]
    global_args: tuple[str, ...]
    action: str
    requires_storage: bool
    requires_image: bool
    internal_storage_path: bool = False


def _option_value(
    arguments: list[str],
    index: int,
    option: str,
) -> tuple[str, int]:
    """Read one ``--option value`` or ``--option=value`` global option."""

    argument = arguments[index]
    if argument == option:
        if index + 1 >= len(arguments) or not arguments[index + 1]:
            raise StoragePathError(f"{option} requires a non-empty value")
        return arguments[index + 1], index + 2
    value = argument.partition("=")[2]
    if not value:
        raise StoragePathError(f"{option} requires a non-empty value")
    return value, index + 1


def _service_tail(
    tail: list[str],
    *,
    action: str,
    allow_empty: bool,
) -> tuple[str, ...]:
    """Validate a tail containing service names and no command options."""

    if not tail and not allow_empty:
        raise StoragePathError(f"Compose {action} requires at least one service")
    if any(argument.startswith("-") for argument in tail):
        raise StoragePathError(f"Compose {action} options are not supported by this launcher")
    unknown = [service for service in tail if service not in EXPECTED_SERVICES]
    if unknown:
        raise StoragePathError(
            f"Compose {action} names unsupported service(s): {', '.join(unknown)}"
        )
    if len(set(tail)) != len(tail):
        raise StoragePathError(f"Compose {action} repeats a service name")
    return tuple(tail)


# ============================================================================
# Purpose: Parse a deliberately small Compose grammar with exact option/value
#   ownership so a new or reordered Compose flag cannot become a safety bypass.
# Database/ORM: None.
# Standards: Unknown actions/options fail closed; only the reviewed Compose file
#   and the documented dev profile are addressable.
# Blast Radius: Container startup and durable storage access.
# Connections:
#   - File: docker-compose.yml -> Documents every accepted operator workflow.
#   - File: tests/scripts/test_compose_storage_preflight.py -> Mutation matrix.
# ============================================================================
def _parse_request(arguments: list[str]) -> LaunchRequest:
    """Return a normalized allowlisted launcher request."""

    if not arguments:
        raise StoragePathError(
            "usage: python scripts/compose.py [--env-file PATH] [--profile dev] <supported action>"
        )

    global_args: list[str] = []
    env_file_seen = False
    profile: str | None = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--env-file" or argument.startswith("--env-file="):
            if env_file_seen:
                raise StoragePathError("--env-file may be supplied only once")
            value, index = _option_value(arguments, index, "--env-file")
            global_args.extend(("--env-file", value))
            env_file_seen = True
            continue
        if argument == "--profile" or argument.startswith("--profile="):
            if profile is not None:
                raise StoragePathError("--profile may be supplied only once")
            profile, index = _option_value(arguments, index, "--profile")
            if profile != "dev":
                raise StoragePathError("only the reviewed Compose profile 'dev' is supported")
            global_args.extend(("--profile", profile))
            continue
        break

    if index >= len(arguments):
        raise StoragePathError("a supported Compose action is required")
    action = arguments[index]
    tail = arguments[index + 1 :]

    if action == "storage-path":
        if profile is not None or tail:
            raise StoragePathError("storage-path accepts only an optional --env-file")
        return LaunchRequest(
            compose_args=(),
            global_args=tuple(global_args),
            action=action,
            requires_storage=False,
            requires_image=False,
            internal_storage_path=True,
        )
    if profile is not None and action != "up":
        raise StoragePathError("the dev profile is supported only for '--profile dev up app-dev'")

    normalized_tail: list[str]
    requires_storage = False
    requires_image = False
    if action == "up":
        normalized_tail = []
        tail_index = 0
        while tail_index < len(tail) and tail[tail_index] in {
            "-d",
            "--detach",
        }:
            option = tail[tail_index]
            canonical = "--detach" if option == "-d" else option
            if canonical in normalized_tail:
                raise StoragePathError(f"Compose up repeats {option}")
            normalized_tail.append(canonical)
            tail_index += 1
        services = _service_tail(
            tail[tail_index:],
            action=action,
            allow_empty=True,
        )
        if profile == "dev" and services != ("app-dev",):
            raise StoragePathError(
                "the dev profile must be targeted exactly as '--profile dev up app-dev' "
                "to avoid starting two schedulers"
            )
        if "app-dev" in services and profile != "dev":
            raise StoragePathError("app-dev requires the explicit reviewed --profile dev")
        normalized_tail.extend(services)
        requires_storage = not services or bool(APP_SERVICES.intersection(services))
        requires_image = not services or bool(APPLICATION_IMAGE_SERVICES.intersection(services))
    elif action == "run":
        if tail != ["--rm", "migrate"]:
            raise StoragePathError("the only supported one-shot run is 'run --rm migrate'")
        normalized_tail = list(tail)
        requires_image = True
    elif action == "logs":
        normalized_tail = []
        if tail and tail[0] in {"-f", "--follow"}:
            normalized_tail.append("--follow")
            tail = tail[1:]
        normalized_tail.extend(_service_tail(tail, action=action, allow_empty=True))
    elif action == "stop":
        normalized_tail = list(_service_tail(tail, action=action, allow_empty=False))
    elif action == "down":
        if tail:
            raise StoragePathError(
                "down options are intentionally unsupported; '-v' destroys named data"
            )
        normalized_tail = []
    elif action == "config":
        if tail != ["--quiet"]:
            raise StoragePathError("config requires the output-suppressing --quiet flag")
        normalized_tail = list(tail)
    elif action == "ps":
        if tail:
            raise StoragePathError("ps options are not supported by this launcher")
        normalized_tail = []
    else:
        raise StoragePathError(
            f"Compose action {action!r} is unsupported; this launcher is not a "
            "general Docker Compose proxy"
        )

    compose_args = (*global_args, action, *normalized_tail)
    return LaunchRequest(
        compose_args=tuple(compose_args),
        global_args=tuple(global_args),
        action=action,
        requires_storage=requires_storage,
        requires_image=requires_image,
    )


def _compose_environment(source: dict[str, str]) -> dict[str, str]:
    """Reject ambient Compose controls and pin every launcher-owned boundary."""

    compose_controls = sorted(key for key in source if key.upper().startswith("COMPOSE_"))
    if compose_controls:
        raise StoragePathError(
            "ambient COMPOSE_* controls are unsupported: " + ", ".join(compose_controls)
        )
    reserved_image = next(
        (key for key in source if key.upper() == "UMS_APP_IMAGE"),
        None,
    )
    if reserved_image is not None:
        raise StoragePathError("UMS_APP_IMAGE is reserved for immutable launcher provenance")
    environment = dict(source)
    environment.update(
        {
            "COMPOSE_DISABLE_ENV_FILE": "1",
            "COMPOSE_FILE": str(COMPOSE_FILE),
            "COMPOSE_PROFILES": "",
            "COMPOSE_PROJECT_NAME": PROJECT_NAME,
            "UMS_APP_IMAGE": STORAGE_IMAGE,
        }
    )
    return environment


def _request_env_file(request: LaunchRequest, *, cwd: Path) -> Path | None:
    """Resolve the explicit env file, or the conventional project .env, once."""

    global_args = list(request.global_args)
    for index in range(0, len(global_args), 2):
        if global_args[index] == "--env-file":
            candidate = Path(global_args[index + 1])
            if not candidate.is_absolute():
                candidate = cwd / candidate
            return candidate.resolve(strict=False)
    default = cwd / ".env"
    return default.resolve(strict=False) if default.exists() else None


def _replace_request_env_file(request: LaunchRequest, env_file: Path) -> LaunchRequest:
    """Return the same parsed action with one launcher-owned env-file copy."""

    original_globals = list(request.global_args)
    profile_args: list[str] = []
    for index in range(0, len(original_globals), 2):
        if original_globals[index] == "--profile":
            profile_args.extend(original_globals[index : index + 2])
    global_args = ("--env-file", str(env_file), *profile_args)
    action_tail = request.compose_args[len(request.global_args) :]
    return replace(
        request,
        global_args=tuple(global_args),
        compose_args=(*global_args, *action_tail),
    )


def _validated_env_bytes(path: Path) -> bytes:
    """Read one env file and reject every launcher/Compose behavioral assignment."""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise StoragePathError("cannot read the selected Compose env file") from exc
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StoragePathError("the selected Compose env file is not UTF-8") from exc
    if "\x00" in text:
        raise StoragePathError("the selected Compose env file contains a NUL byte")
    for line in text.splitlines():
        match = _ENV_ASSIGNMENT.match(line)
        if match is None:
            continue
        name = match.group("name").upper()
        if name.startswith("COMPOSE_") or name == "UMS_APP_IMAGE":
            raise StoragePathError(
                f"the selected env file assigns reserved launcher variable {name}"
            )
    return payload


# ============================================================================
# Purpose: Snapshot the selected env file into an exclusive private copy so a
#   post-validation replacement cannot inject Compose behavior or secrets output.
# Database/ORM: None.
# Standards: All COMPOSE_* and immutable-image assignments fail closed; Compose
#   automatic .env loading is disabled and only the validated bytes are used.
# Blast Radius: Project name, active profiles, file selection, and interpolation.
# Connections:
#   - File: docker-compose.yml -> Consumes only non-reserved application values.
#   - File: tests/scripts/test_compose_storage_preflight.py -> Env counterexamples.
# ============================================================================
@contextmanager
def _isolated_env_request(
    request: LaunchRequest,
    *,
    cwd: Path,
) -> Iterator[LaunchRequest]:
    """Yield a request referencing only an immutable launcher-owned env snapshot."""

    source = _request_env_file(request, cwd=cwd)
    if source is None:
        yield request
        return
    payload = _validated_env_bytes(source)
    descriptor, raw_temp_path = tempfile.mkstemp(prefix="ums-compose-", suffix=".env")
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield _replace_request_env_file(request, temp_path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StoragePathError("cannot remove the launcher-owned env snapshot") from exc


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run Docker without shell interpolation and optionally retain output."""

    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=capture,
            text=capture,
        )
    except OSError as exc:
        raise StoragePathError(
            f"could not execute the required Docker command ({command[0]!r})"
        ) from exc


def _local_endpoint(endpoint: object) -> bool:
    """Return whether an endpoint is one exact local Docker transport."""

    if not isinstance(endpoint, str):
        return False
    return endpoint.casefold() in {
        "unix:///var/run/docker.sock",
        "npipe:////./pipe/docker_engine",
        "npipe:////./pipe/dockerdesktoplinuxengine",
    }


def _require_local_docker_context(*, cwd: Path, env: dict[str, str]) -> dict[str, str]:
    """Prove one local endpoint and return an environment pinned to that endpoint."""

    explicit_host = env.get("DOCKER_HOST")
    if explicit_host and not _local_endpoint(explicit_host):
        raise StoragePathError("DOCKER_HOST selects a remote or unapproved daemon")
    context = _run_checked(["docker", "context", "show"], cwd=cwd, env=env, capture=True)
    context_name = (context.stdout or "").strip()
    if context.returncode != 0 or not context_name:
        raise StoragePathError("cannot identify the active Docker context")
    endpoint = _run_checked(
        [
            "docker",
            "context",
            "inspect",
            context_name,
            "--format",
            "{{json .Endpoints.docker.Host}}",
        ],
        cwd=cwd,
        env=env,
        capture=True,
    )
    if endpoint.returncode != 0:
        raise StoragePathError("cannot prove the active Docker endpoint is local")
    try:
        endpoint_value = json.loads((endpoint.stdout or "").strip())
    except (TypeError, ValueError) as exc:
        raise StoragePathError("Docker context endpoint was not machine-readable") from exc
    if not _local_endpoint(endpoint_value):
        raise StoragePathError("Docker context endpoint is remote or unapproved")
    pinned = {
        key: value
        for key, value in env.items()
        if key.upper()
        not in {"DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
    }
    pinned["DOCKER_HOST"] = endpoint_value
    return pinned


def _run_daemon_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a daemon action only with the exact endpoint returned by local proof."""

    if not _local_endpoint(env.get("DOCKER_HOST")):
        raise StoragePathError("Docker daemon action lacks a pinned local endpoint")
    return _run_checked(command, cwd=cwd, env=env, capture=capture)


def _render_model(
    request: LaunchRequest,
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, object]:
    """Render all profiles from the pinned Compose file without host mutation."""

    render_globals: list[str] = []
    global_args = list(request.global_args)
    index = 0
    while index < len(global_args):
        if global_args[index] == "--env-file":
            render_globals.extend(global_args[index : index + 2])
        index += 2
    result = _run_checked(
        [
            "docker",
            "compose",
            *render_globals,
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        cwd=cwd,
        env=env,
        capture=True,
    )
    if result.returncode != 0:
        raise StoragePathError(
            "the pinned Compose model failed to render; run the supported config "
            "command to inspect missing variables"
        )
    try:
        model = json.loads(result.stdout or "")
    except (TypeError, ValueError) as exc:
        raise StoragePathError("the rendered Compose model was not valid JSON") from exc
    if not isinstance(model, dict):
        raise StoragePathError("the rendered Compose model was not an object")
    return model


def _canonical_model_source(raw_source: object) -> Path:
    """Return one absolute canonical bind source from rendered JSON."""

    if not isinstance(raw_source, str) or not raw_source:
        raise StoragePathError("rendered storage bind has no source")
    if any(character in raw_source for character in ("\x00", "\r", "\n")):
        raise StoragePathError("rendered storage bind source contains control characters")
    source = Path(raw_source)
    if not source.is_absolute():
        raise StoragePathError("rendered storage bind source is not absolute")
    try:
        return source.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise StoragePathError("cannot canonicalize the rendered storage source") from exc


def _path_overlap(first: Path, second: Path) -> bool:
    """Return whether two canonical paths contain one another."""

    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _normalized_target(raw_target: object) -> str:
    """Normalize a rendered Linux container target and reject ambiguity."""

    if not isinstance(raw_target, str) or not raw_target.startswith("/"):
        raise StoragePathError("rendered volume target is not an absolute POSIX path")
    if "\\" in raw_target or any(character in raw_target for character in ("\x00", "\r", "\n")):
        raise StoragePathError("rendered volume target contains forbidden characters")
    return posixpath.normpath(raw_target)


# ============================================================================
# Purpose: Prove the complete rendered service/storage projection, including
#   exact service names, non-root app identity, mount source/target, and the
#   absence of alternate or nested access to the durable tree.
# Database/ORM: None.
# Standards: Rendered JSON is authoritative; ambiguous targets, extra services,
#   privilege additions, source overlap, and create-host-path all fail closed.
# Blast Radius: Durable artifacts/blob confidentiality and host bind exposure.
# Connections:
#   - File: docker-compose.yml -> Source model under validation.
#   - File: Dockerfile -> Image identity and uid 10001 runtime contract.
# ============================================================================
def _validate_rendered_model(
    model: dict[str, object],
    *,
    project_root: Path,
    expected_image: str = STORAGE_IMAGE,
    expected_storage_source: Path | None = None,
) -> Path:
    """Validate the exact storage projection and return its canonical source."""

    if model.get("name") != PROJECT_NAME:
        raise StoragePathError("rendered Compose project name differs from the pinned name")
    services = model.get("services")
    if not isinstance(services, dict):
        raise StoragePathError("rendered Compose model has no services object")
    if set(services) != EXPECTED_SERVICES:
        raise StoragePathError("rendered Compose service set differs from the reviewed model")

    root_mounts: dict[str, dict[str, object]] = {}
    for service_name, service in services.items():
        if not isinstance(service, dict):
            raise StoragePathError(f"rendered service {service_name} is not an object")
        volumes = service.get("volumes", [])
        if not isinstance(volumes, list):
            raise StoragePathError(f"rendered service {service_name} has invalid volumes")
        for volume in volumes:
            if not isinstance(volume, dict):
                raise StoragePathError(f"rendered service {service_name} has a non-object volume")
            target = _normalized_target(volume.get("target"))
            if target == STORAGE_TARGET:
                if volume.get("target") != STORAGE_TARGET:
                    raise StoragePathError("rendered storage target uses an ambiguous spelling")
                if service_name not in APP_SERVICES:
                    raise StoragePathError(
                        f"rendered service {service_name} touches application storage"
                    )
                if service_name in root_mounts:
                    raise StoragePathError(
                        f"rendered service {service_name} repeats the storage mount"
                    )
                root_mounts[service_name] = volume
            elif target.startswith(f"{STORAGE_TARGET}/"):
                raise StoragePathError(
                    f"rendered service {service_name} adds a nested storage mount"
                )

    if set(root_mounts) != APP_SERVICES:
        raise StoragePathError("both app services must retain one storage bind")
    sources = {_canonical_model_source(volume.get("source")) for volume in root_mounts.values()}
    if len(sources) != 1:
        raise StoragePathError("app and app-dev render different storage sources")
    storage_path = sources.pop()
    if expected_storage_source is not None:
        expected_spelling = os.path.normcase(str(expected_storage_source))
        rendered_spellings = {
            os.path.normcase(str(volume.get("source"))) for volume in root_mounts.values()
        }
        if rendered_spellings != {expected_spelling}:
            raise StoragePathError(
                "Compose rewrote the OS-pinned storage source before daemon handoff"
            )

    for service_name, service in services.items():
        if not isinstance(service, dict):
            raise StoragePathError(f"rendered service {service_name} is not an object")
        volumes = service.get("volumes", [])
        if not isinstance(volumes, list):
            raise StoragePathError(f"rendered service {service_name} has invalid volumes")
        for volume in volumes:
            if not isinstance(volume, dict):
                raise StoragePathError(f"rendered service {service_name} has a non-object volume")
            source_value = volume.get("source")
            source = _canonical_model_source(source_value) if volume.get("type") == "bind" else None
            target = _normalized_target(volume.get("target"))
            is_expected_root = service_name in APP_SERVICES and target == STORAGE_TARGET
            if source is not None and _path_overlap(source, storage_path) and not is_expected_root:
                raise StoragePathError(
                    f"rendered service {service_name} has alternate access to storage"
                )

        if service_name not in APPLICATION_IMAGE_SERVICES:
            continue
        if service.get("image") != expected_image:
            raise StoragePathError(f"rendered service {service_name} changes the application image")
        if service.get("user") != "10001:10001":
            raise StoragePathError(
                f"rendered service {service_name} must run exactly as uid/gid 10001"
            )
        if service.get("entrypoint") != ["/usr/bin/tini", "--"]:
            raise StoragePathError(
                f"rendered service {service_name} changes the application entrypoint"
            )
        build = service.get("build")
        if not isinstance(build, dict) or set(build) != {"context", "dockerfile"}:
            raise StoragePathError(
                f"rendered service {service_name} changes the reviewed image build"
            )
        if (
            _canonical_model_source(build.get("context")) != project_root
            or build.get("dockerfile") != "Dockerfile"
        ):
            raise StoragePathError(f"rendered service {service_name} redirects the image build")
        if service.get("pull_policy") != "never":
            raise StoragePathError(
                f"rendered service {service_name} permits mutable image resolution"
            )
        expected_profiles = ["dev"] if service_name == "app-dev" else None
        if service.get("profiles") != expected_profiles:
            raise StoragePathError(f"rendered service {service_name} changes its profile boundary")
        for privileged_key in (
            "privileged",
            "cap_add",
            "devices",
            "device_cgroup_rules",
            "volumes_from",
            "group_add",
            "ipc",
            "pid",
            "uts",
            "userns_mode",
            "security_opt",
            "sysctls",
            "cgroup",
            "cgroup_parent",
            "runtime",
        ):
            if service.get(privileged_key):
                raise StoragePathError(
                    f"rendered service {service_name} adds {privileged_key} privileges"
                )
        if service.get("network_mode") == "host" or service.get("pid") == "host":
            raise StoragePathError(f"rendered service {service_name} joins a host namespace")

        if service_name == "migrate":
            if volumes:
                raise StoragePathError("rendered migrate service adds an unexpected volume")
            continue

        mount = root_mounts[service_name]
        if mount.get("type") != "bind":
            raise StoragePathError(
                f"rendered service {service_name} changes storage to a non-bind volume"
            )
        if mount.get("bind") != {"create_host_path": False}:
            raise StoragePathError(f"rendered service {service_name} changes safe bind options")
        if mount.get("read_only") not in (None, False):
            raise StoragePathError(
                f"rendered service {service_name} makes application storage read-only"
            )

        volumes = service.get("volumes")
        if not isinstance(volumes, list):
            raise StoragePathError(f"rendered service {service_name} has invalid volumes")
        expected_volume_count = 2 if service_name == "app-dev" else 1
        if len(volumes) != expected_volume_count:
            raise StoragePathError(
                f"rendered service {service_name} changes its exact volume layout"
            )
        if service_name == "app-dev":
            backend_mount = volumes[0]
            if not isinstance(backend_mount, dict):
                raise StoragePathError("rendered app-dev backend bind is invalid")
            if (
                backend_mount.get("type") != "bind"
                or _canonical_model_source(backend_mount.get("source")) != project_root / "backend"
                or backend_mount.get("target") != "/srv/app/backend"
                or backend_mount.get("read_only") is not True
                or backend_mount.get("bind") != {}
            ):
                raise StoragePathError(
                    "rendered app-dev changes the reviewed read-only source bind"
                )

    if not project_root.is_absolute():
        raise StoragePathError("project root must be canonical before model validation")
    return storage_path


# ============================================================================
# Purpose: Build the exact checkout without a tag and prove the returned local
#   content ID still names the image before any Compose container is created.
# Database/ORM: None.
# Standards: Captures all build/inspect output; local endpoint already pinned;
#   mutable tags and multi-line/non-machine-readable identities fail closed.
# Blast Radius: Application and migration container provenance.
# Connections:
#   - File: Dockerfile -> Exact build recipe selected by absolute path.
#   - File: docker-compose.yml -> Receives the ID through reserved UMS_APP_IMAGE.
# ============================================================================
def _build_reviewed_image(*, cwd: Path, env: dict[str, str]) -> str:
    """Build the exact reviewed context and return its immutable local image ID."""

    project_root = cwd.resolve(strict=True)
    dockerfile = (project_root / "Dockerfile").resolve(strict=True)
    built = _run_daemon_checked(
        [
            "docker",
            "build",
            "--quiet",
            "--pull=false",
            "--file",
            str(dockerfile),
            str(project_root),
        ],
        cwd=project_root,
        env=env,
        capture=True,
    )
    if built.returncode != 0:
        raise StoragePathError("cannot build the reviewed application image")
    lines = [line.strip() for line in (built.stdout or "").splitlines() if line.strip()]
    if len(lines) != 1 or _IMAGE_ID.fullmatch(lines[0]) is None:
        raise StoragePathError("Docker build did not return one immutable image ID")
    image_id = lines[0]
    inspected = _run_daemon_checked(
        ["docker", "image", "inspect", image_id, "--format", "{{json .Id}}"],
        cwd=project_root,
        env=env,
        capture=True,
    )
    if inspected.returncode != 0:
        raise StoragePathError("cannot verify the built application image ID")
    try:
        inspected_id = json.loads((inspected.stdout or "").strip())
    except (TypeError, ValueError) as exc:
        raise StoragePathError("built image identity was not machine-readable") from exc
    if inspected_id != image_id:
        raise StoragePathError("built image identity changed before Compose pinning")
    return image_id


# ============================================================================
# Purpose: Prove uid 10001 can write both durable stores through the exact
#   rendered app service without granting a root container host-path access.
# Database/ORM: None.
# Standards: No shell interpolation on the host; exclusive random no-follow
#   files only; image identity and local endpoint are already pinned.
# Blast Radius: Two temporary probe files under artifacts and blobs.
# Connections:
#   - File: docker-compose.yml -> Supplies the validated app bind and user.
#   - File: Dockerfile -> Supplies the non-root runtime image.
# ============================================================================
def _probe_storage_writable(
    *,
    request: LaunchRequest,
    cwd: Path,
    env: dict[str, str],
) -> None:
    """Run a bounded non-root write probe through the app service."""

    result = _run_daemon_checked(
        [
            "docker",
            "compose",
            *request.global_args,
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "app",
            "-c",
            STORAGE_WRITE_PROBE,
        ],
        cwd=cwd,
        env=env,
    )
    if result.returncode != 0:
        raise StoragePathError(
            "uid 10001 cannot write the validated storage bind; on Linux provision "
            "the host path for uid/gid 10001, then retry"
        )


# ============================================================================
# Purpose: Validate the pinned model and host store, run a non-root write
#   probe, then execute one normalized allowlisted Compose workflow.
# Database/ORM: None.
# Standards: Routes no arbitrary Compose flags; local daemon only; final model
#   and tree identities are rechecked after preparation/probing.
# Blast Radius: Container lifecycle and durable artifact/blob host storage.
# Connections:
#   - File: scripts/validate_compose_storage_path.py -> Host path/ACL contract.
#   - File: docker-compose.yml -> Pinned rendered service model.
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Execute one supported launcher action and return its process status."""

    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        request = _parse_request(raw_arguments)
        environment = _compose_environment(os.environ.copy())
        with _isolated_env_request(request, cwd=PROJECT_ROOT) as request:
            if request.action in DAEMON_ACTIONS:
                environment = _require_local_docker_context(
                    cwd=PROJECT_ROOT,
                    env=environment,
                )

            rendered_path: Path | None = None
            project_root = PROJECT_ROOT.resolve(strict=True)
            if request.internal_storage_path or request.action in {"up", "run"}:
                model = _render_model(request, cwd=PROJECT_ROOT, env=environment)
                rendered_path = _validate_rendered_model(
                    model,
                    project_root=project_root,
                    expected_image=STORAGE_IMAGE,
                )
            if request.internal_storage_path:
                if rendered_path is None:
                    raise StoragePathError("storage resolver has no rendered source")
                storage_path = validate_storage_path(
                    rendered_path,
                    project_root=PROJECT_ROOT,
                    require_exists=True,
                )
                print(storage_path)
                return 0

            expected_image = STORAGE_IMAGE
            if request.requires_image:
                expected_image = _build_reviewed_image(
                    cwd=PROJECT_ROOT,
                    env=environment,
                )
                environment["UMS_APP_IMAGE"] = expected_image
                pinned_model = _render_model(
                    request,
                    cwd=PROJECT_ROOT,
                    env=environment,
                )
                pinned_path = _validate_rendered_model(
                    pinned_model,
                    project_root=project_root,
                    expected_image=expected_image,
                )
                if rendered_path is not None and pinned_path != rendered_path:
                    raise StoragePathError(
                        "immutable image pinning changed the rendered storage source"
                    )
                rendered_path = pinned_path

            if request.requires_storage:
                if rendered_path is None:
                    raise StoragePathError("storage action has no rendered source")
                storage_path = prepare_storage_path(
                    rendered_path,
                    project_root=PROJECT_ROOT,
                )
                with hold_storage_identity(storage_path) as identity_guard:
                    environment["UMS_APP_DATA_HOST"] = str(identity_guard.docker_source)
                    guarded_model = _render_model(
                        request,
                        cwd=PROJECT_ROOT,
                        env=environment,
                    )
                    guarded_path = _validate_rendered_model(
                        guarded_model,
                        project_root=project_root,
                        expected_image=expected_image,
                        expected_storage_source=identity_guard.docker_source,
                    )
                    if guarded_path != storage_path:
                        raise StoragePathError(
                            "guarded storage source does not resolve to the proven identity"
                        )
                    identity_guard.assert_current()
                    _probe_storage_writable(
                        request=request,
                        cwd=PROJECT_ROOT,
                        env=environment,
                    )
                    validate_storage_path(
                        storage_path,
                        project_root=PROJECT_ROOT,
                        require_exists=True,
                    )
                    identity_guard.assert_current()
                    completed = _run_daemon_checked(
                        ["docker", "compose", *request.compose_args],
                        cwd=PROJECT_ROOT,
                        env=environment,
                    )
                    identity_guard.assert_current()
                    return completed.returncode

            command = ["docker", "compose", *request.compose_args]
            if request.action in DAEMON_ACTIONS:
                completed = _run_daemon_checked(
                    command,
                    cwd=PROJECT_ROOT,
                    env=environment,
                )
            else:
                completed = _run_checked(
                    command,
                    cwd=PROJECT_ROOT,
                    env=environment,
                )
    except StoragePathError as exc:
        print(f"storage preflight failed: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"could not execute Docker Compose: {exc}", file=sys.stderr)
        return 127
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
