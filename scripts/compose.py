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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from validate_compose_storage_path import (
    STORAGE_CHILDREN,
    StorageIdentityGuard,
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
STORAGE_TARGETS = {
    "artifacts": f"{STORAGE_TARGET}/artifacts",
    "blobs": f"{STORAGE_TARGET}/blobs",
}
STORAGE_SOURCE_ENV = {
    "artifacts": "UMS_APP_ARTIFACTS_HOST",
    "blobs": "UMS_APP_BLOBS_HOST",
}
RESERVED_LAUNCHER_ENV = {
    "UMS_APP_IMAGE",
    *STORAGE_SOURCE_ENV.values(),
}
EXPECTED_SERVICES = {"postgres", "redis", "migrate", "app", "app-dev"}
APP_SERVICES = {"app", "app-dev"}
APPLICATION_IMAGE_SERVICES = {"migrate", "app", "app-dev"}
DAEMON_ACTIONS = {"up", "run", "logs", "stop", "down", "ps"}
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")
_ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?:=|:|$)")

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
    storage_services: tuple[str, ...] = ()


@dataclass(frozen=True)
class _StorageContainerCapture:
    """Validated scoped IDs plus whether Compose proved the list complete."""

    identifiers: tuple[str, ...]
    complete: bool


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


def _parse_global_arguments(
    arguments: list[str],
) -> tuple[list[str], str | None, int]:
    """Consume the leading --env-file/--profile options, returning the stop index."""
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
    return global_args, profile, index


def _parse_up_tail(
    tail: list[str],
    *,
    profile: str | None,
) -> tuple[list[str], tuple[str, ...], bool, bool]:
    """Normalize the reviewed up tail: leading detach flags, then services."""
    normalized_tail: list[str] = []
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
        action="up",
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
    return normalized_tail, services, requires_storage, requires_image


def _parse_run_tail(
    tail: list[str],
    *,
    profile: str | None,
) -> tuple[list[str], tuple[str, ...], bool, bool]:
    """Accept only the reviewed one-shot migrate run."""
    _ = profile
    if tail != ["--rm", "migrate"]:
        raise StoragePathError("the only supported one-shot run is 'run --rm migrate'")
    return list(tail), (), False, True


def _parse_logs_tail(
    tail: list[str],
    *,
    profile: str | None,
) -> tuple[list[str], tuple[str, ...], bool, bool]:
    """Normalize the reviewed logs tail: one optional follow flag, then services."""
    _ = profile
    normalized_tail: list[str] = []
    if tail and tail[0] in {"-f", "--follow"}:
        normalized_tail.append("--follow")
        tail = tail[1:]
    normalized_tail.extend(_service_tail(tail, action="logs", allow_empty=True))
    return normalized_tail, (), False, False


def _parse_stop_tail(
    tail: list[str],
    *,
    profile: str | None,
) -> tuple[list[str], tuple[str, ...], bool, bool]:
    """Require one or more reviewed services and no stop options."""
    _ = profile
    return list(_service_tail(tail, action="stop", allow_empty=False)), (), False, False


def _parse_down_tail(
    tail: list[str],
    *,
    profile: str | None,
) -> tuple[list[str], tuple[str, ...], bool, bool]:
    """Reject every down option before the daemon can see it."""
    _ = profile
    if tail:
        raise StoragePathError(
            "down options are intentionally unsupported; '-v' destroys named data"
        )
    return [], (), False, False


def _parse_config_tail(
    tail: list[str],
    *,
    profile: str | None,
) -> tuple[list[str], tuple[str, ...], bool, bool]:
    """Accept config only with the output-suppressing --quiet flag."""
    _ = profile
    if tail != ["--quiet"]:
        raise StoragePathError("config requires the output-suppressing --quiet flag")
    return list(tail), (), False, False


def _parse_ps_tail(
    tail: list[str],
    *,
    profile: str | None,
) -> tuple[list[str], tuple[str, ...], bool, bool]:
    """Reject every ps option before the daemon can see it."""
    _ = profile
    if tail:
        raise StoragePathError("ps options are not supported by this launcher")
    return [], (), False, False


_TailParser = Callable[..., tuple[list[str], tuple[str, ...], bool, bool]]
_ACTION_TAIL_PARSERS: dict[str, _TailParser] = {
    "up": _parse_up_tail,
    "run": _parse_run_tail,
    "logs": _parse_logs_tail,
    "stop": _parse_stop_tail,
    "down": _parse_down_tail,
    "config": _parse_config_tail,
    "ps": _parse_ps_tail,
}


def _storage_services_for(
    services: tuple[str, ...],
    requires_storage: bool,
) -> tuple[str, ...]:
    """Return the app services a post-create audit must enumerate."""
    if services:
        return tuple(service for service in services if service in APP_SERVICES)
    return ("app",) if requires_storage else ()


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

    global_args, profile, index = _parse_global_arguments(arguments)
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

    tail_parser = _ACTION_TAIL_PARSERS.get(action)
    if tail_parser is None:
        raise StoragePathError(
            f"Compose action {action!r} is unsupported; this launcher is not a "
            "general Docker Compose proxy"
        )
    normalized_tail, services, requires_storage, requires_image = tail_parser(
        tail,
        profile=profile,
    )

    compose_args = (*global_args, action, *normalized_tail)
    return LaunchRequest(
        compose_args=tuple(compose_args),
        global_args=tuple(global_args),
        action=action,
        requires_storage=requires_storage,
        requires_image=requires_image,
        storage_services=_storage_services_for(services, requires_storage),
    )


def _compose_environment(source: dict[str, str]) -> dict[str, str]:
    """Reject ambient Compose controls and pin every launcher-owned boundary."""
    compose_controls = sorted(key for key in source if key.upper().startswith("COMPOSE_"))
    if compose_controls:
        raise StoragePathError(
            "ambient COMPOSE_* controls are unsupported: " + ", ".join(compose_controls)
        )
    reserved_name = next(
        (key for key in source if key.upper() in RESERVED_LAUNCHER_ENV),
        None,
    )
    if reserved_name is not None:
        raise StoragePathError(f"{reserved_name.upper()} is reserved for launcher-owned provenance")
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
        if name.startswith("COMPOSE_") or name in RESERVED_LAUNCHER_ENV:
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


def _matching_storage_child(target: str) -> str | None:
    """Return the direct child a normalized target names, if any."""
    for child, expected_target in STORAGE_TARGETS.items():
        if target == expected_target:
            return child
    return None


def _collect_storage_mounts(
    services: dict[str, object],
) -> dict[str, dict[str, dict[str, object]]]:
    """Collect the reviewed direct child binds named by every rendered service."""
    storage_mounts: dict[str, dict[str, dict[str, object]]] = {
        service_name: {} for service_name in APP_SERVICES
    }
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
            child = _matching_storage_child(target)
            if child is not None:
                if volume.get("target") != STORAGE_TARGETS[child]:
                    raise StoragePathError("rendered storage target uses an ambiguous spelling")
                if service_name not in APP_SERVICES:
                    raise StoragePathError(
                        f"rendered service {service_name} touches application storage"
                    )
                if child in storage_mounts[service_name]:
                    raise StoragePathError(
                        f"rendered service {service_name} repeats the {child} storage mount"
                    )
                storage_mounts[service_name][child] = volume
            elif target == STORAGE_TARGET or target.startswith(f"{STORAGE_TARGET}/"):
                raise StoragePathError(
                    f"rendered service {service_name} adds an unreviewed storage mount"
                )
    return storage_mounts


def _canonical_storage_children(
    storage_mounts: dict[str, dict[str, dict[str, object]]],
) -> dict[str, Path]:
    """Require both child binds on both app services with one canonical spelling."""
    if any(set(mounts) != set(STORAGE_CHILDREN) for mounts in storage_mounts.values()):
        raise StoragePathError("both app services must retain both direct child binds")
    canonical_children: dict[str, Path] = {}
    for child in STORAGE_CHILDREN:
        sources = {
            _canonical_model_source(storage_mounts[service][child].get("source"))
            for service in APP_SERVICES
        }
        if len(sources) != 1:
            raise StoragePathError(f"app and app-dev render different {child} sources")
        canonical = sources.pop()
        if canonical.name != child:
            raise StoragePathError(f"rendered {child} source does not name its exact store")
        canonical_children[child] = canonical
    return canonical_children


def _single_storage_root(canonical_children: dict[str, Path]) -> Path:
    """Return the one storage root shared by every direct child bind."""
    storage_roots = {source.parent for source in canonical_children.values()}
    if len(storage_roots) != 1:
        raise StoragePathError("rendered child binds do not share one storage root")
    return storage_roots.pop()


def _require_pinned_child_spellings(
    storage_mounts: dict[str, dict[str, dict[str, object]]],
    expected_storage_sources: dict[str, Path] | None,
) -> None:
    """Require Compose to render the launcher's pinned child source spellings."""
    if expected_storage_sources is None:
        return
    if set(expected_storage_sources) != set(STORAGE_CHILDREN):
        raise StoragePathError("launcher did not provide every pinned child source")
    for child in STORAGE_CHILDREN:
        expected_spelling = os.path.normcase(str(expected_storage_sources[child]))
        rendered_spellings = {
            os.path.normcase(str(storage_mounts[service][child].get("source")))
            for service in APP_SERVICES
        }
        if rendered_spellings != {expected_spelling}:
            raise StoragePathError(
                f"Compose rewrote the OS-pinned {child} source before daemon handoff"
            )


def _reject_alternate_storage_access(
    service_name: str,
    service: object,
    *,
    storage_path: Path,
    canonical_children: dict[str, Path],
) -> None:
    """Reject any bind reaching the durable tree outside the reviewed children."""
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
        expected_child = next(
            (
                child
                for child, expected_target in STORAGE_TARGETS.items()
                if service_name in APP_SERVICES and target == expected_target
            ),
            None,
        )
        is_expected_child = (
            expected_child is not None and source == canonical_children[expected_child]
        )
        if source is not None and _path_overlap(source, storage_path) and not is_expected_child:
            raise StoragePathError(
                f"rendered service {service_name} has alternate access to storage"
            )


def _reject_service_privileges(service_name: str, service: dict[str, object]) -> None:
    """Reject every container privilege the reviewed model never grants."""
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


def _require_reviewed_image_service(
    service_name: str,
    service: dict[str, object],
    *,
    project_root: Path,
    expected_image: str,
) -> None:
    """Prove the image identity, non-root user, reviewed build, and boundaries."""
    if service.get("image") != expected_image:
        raise StoragePathError(f"rendered service {service_name} changes the application image")
    if service.get("user") != "10001:10001":
        raise StoragePathError(f"rendered service {service_name} must run exactly as uid/gid 10001")
    if service.get("entrypoint") != ["/usr/bin/tini", "--"]:
        raise StoragePathError(
            f"rendered service {service_name} changes the application entrypoint"
        )
    build = service.get("build")
    if not isinstance(build, dict) or set(build) != {"context", "dockerfile"}:
        raise StoragePathError(f"rendered service {service_name} changes the reviewed image build")
    if (
        _canonical_model_source(build.get("context")) != project_root
        or build.get("dockerfile") != "Dockerfile"
    ):
        raise StoragePathError(f"rendered service {service_name} redirects the image build")
    if service.get("pull_policy") != "never":
        raise StoragePathError(f"rendered service {service_name} permits mutable image resolution")
    expected_profiles = ["dev"] if service_name == "app-dev" else None
    if service.get("profiles") != expected_profiles:
        raise StoragePathError(f"rendered service {service_name} changes its profile boundary")
    _reject_service_privileges(service_name, service)
    if service.get("network_mode") == "host" or service.get("pid") == "host":
        raise StoragePathError(f"rendered service {service_name} joins a host namespace")


def _require_storage_bind_options(
    service_name: str,
    service: dict[str, object],
    storage_mounts: dict[str, dict[str, dict[str, object]]],
) -> None:
    """Require every durable child bind to stay an explicit non-readonly bind."""
    for child, mount in storage_mounts[service_name].items():
        if mount.get("type") != "bind":
            raise StoragePathError(
                f"rendered service {service_name} changes {child} to a non-bind volume"
            )
        if mount.get("bind") != {"create_host_path": False}:
            raise StoragePathError(
                f"rendered service {service_name} changes safe {child} bind options"
            )
        if mount.get("read_only") not in (None, False):
            raise StoragePathError(
                f"rendered service {service_name} makes {child} storage read-only"
            )


def _require_app_dev_backend_bind(
    volumes: list[object],
    *,
    project_root: Path,
) -> None:
    """Require the one reviewed read-only backend source bind on app-dev."""
    backend_mounts = [
        volume
        for volume in volumes
        if isinstance(volume, dict) and volume.get("target") == "/srv/app/backend"
    ]
    if len(backend_mounts) != 1:
        raise StoragePathError("rendered app-dev backend bind is missing or repeated")
    backend_mount = backend_mounts[0]
    if not isinstance(backend_mount, dict):
        raise StoragePathError("rendered app-dev backend bind is invalid")
    if (
        backend_mount.get("type") != "bind"
        or _canonical_model_source(backend_mount.get("source")) != project_root / "backend"
        or backend_mount.get("target") != "/srv/app/backend"
        or backend_mount.get("read_only") is not True
        or backend_mount.get("bind") != {}
    ):
        raise StoragePathError("rendered app-dev changes the reviewed read-only source bind")


def _require_volume_layout(
    service_name: str,
    service: dict[str, object],
    *,
    project_root: Path,
) -> None:
    """Require the exact reviewed volume count and the app-dev source bind."""
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        raise StoragePathError(f"rendered service {service_name} has invalid volumes")
    expected_volume_count = 3 if service_name == "app-dev" else 2
    if len(volumes) != expected_volume_count:
        raise StoragePathError(f"rendered service {service_name} changes its exact volume layout")
    if service_name == "app-dev":
        _require_app_dev_backend_bind(volumes, project_root=project_root)


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
    expected_storage_sources: dict[str, Path] | None = None,
) -> Path:
    """Validate the exact storage projection and return its canonical source."""
    if model.get("name") != PROJECT_NAME:
        raise StoragePathError("rendered Compose project name differs from the pinned name")
    services = model.get("services")
    if not isinstance(services, dict):
        raise StoragePathError("rendered Compose model has no services object")
    if set(services) != EXPECTED_SERVICES:
        raise StoragePathError("rendered Compose service set differs from the reviewed model")

    storage_mounts = _collect_storage_mounts(services)
    canonical_children = _canonical_storage_children(storage_mounts)
    storage_path = _single_storage_root(canonical_children)
    _require_pinned_child_spellings(storage_mounts, expected_storage_sources)

    for service_name, service in services.items():
        _reject_alternate_storage_access(
            service_name,
            service,
            storage_path=storage_path,
            canonical_children=canonical_children,
        )
        if service_name not in APPLICATION_IMAGE_SERVICES:
            continue
        _require_reviewed_image_service(
            service_name,
            service,
            project_root=project_root,
            expected_image=expected_image,
        )
        if service_name == "migrate":
            if service.get("volumes", []):
                raise StoragePathError("rendered migrate service adds an unexpected volume")
            continue

        _require_storage_bind_options(service_name, service, storage_mounts)
        _require_volume_layout(service_name, service, project_root=project_root)

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


def _detached_storage_up_args(request: LaunchRequest) -> tuple[str, ...]:
    """Return the reviewed up command with a deterministic create phase."""
    action_index = len(request.global_args)
    arguments = list(request.compose_args)
    if request.action != "up" or arguments[action_index] != "up":
        raise StoragePathError("storage container creation requires the reviewed up action")
    if "--detach" not in arguments[action_index + 1 :]:
        arguments.insert(action_index + 1, "--detach")
    return tuple(arguments)


def _inspected_host_source(raw_source: object) -> Path:
    """Normalize one Docker-reported source spelling back to a host path."""
    if not isinstance(raw_source, str):
        raise StoragePathError("Docker reported a non-string bind source")
    normalized = raw_source
    if os.name == "nt":
        normalized = raw_source.replace("\\", "/")
        match = re.fullmatch(
            r"/(?:host_mnt|run/desktop/mnt/host)/(?P<drive>[A-Za-z])(?:/(?P<tail>.*))?",
            normalized,
        )
        if match is not None:
            tail = match.group("tail") or ""
            normalized = f"{match.group('drive')}:{os.sep}{tail.replace('/', os.sep)}"
    source = Path(normalized)
    if not source.is_absolute():
        raise StoragePathError("Docker reported a non-absolute bind source")
    return source


# FIX: Compose can emit valid created IDs beside malformed output or a nonzero
# status. Preserve only those whole-line IDs for remediation, while keeping the
# overall enumeration untrusted so the launcher cannot return apparent safety.
# ============================================================================
# Purpose: Capture every syntactically valid ID emitted for the scoped app
#   service while retaining whether Compose proved that enumeration complete.
# Database/ORM: None.
# Standards: Whole-line lowercase container IDs only; malformed tokens and a
#   nonzero Compose status fail closed without discarding valid captured IDs.
# Blast Radius: Application container inspection and automatic remediation.
# Connections:
#   - File: docker-compose.yml -> Supplies the one explicitly scoped service.
#   - File: tests/scripts/test_compose_storage_preflight.py -> Partial-output proofs.
# ============================================================================
def _capture_storage_container_ids(
    request: LaunchRequest,
    *,
    cwd: Path,
    env: dict[str, str],
) -> _StorageContainerCapture:
    """Capture only valid scoped IDs and separately report enumeration trust."""
    if len(request.storage_services) != 1:
        raise StoragePathError("storage startup does not identify exactly one app service")
    result = _run_daemon_checked(
        [
            "docker",
            "compose",
            *request.global_args,
            "ps",
            "--all",
            "--quiet",
            request.storage_services[0],
        ],
        cwd=cwd,
        env=env,
        capture=True,
    )
    raw_output = result.stdout
    if raw_output is not None and not isinstance(raw_output, str):
        return _StorageContainerCapture(identifiers=(), complete=False)

    identifiers: list[str] = []
    complete = result.returncode == 0
    for line in (raw_output or "").splitlines():
        identifier = line.strip()
        if not identifier:
            continue
        if _CONTAINER_ID.fullmatch(identifier) is None:
            complete = False
            continue
        if identifier not in identifiers:
            identifiers.append(identifier)
    return _StorageContainerCapture(identifiers=tuple(identifiers), complete=complete)


def _remove_container_ids(
    identifiers: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
) -> None:
    """Remove only application containers whose IDs were proven by Compose ps."""
    if not identifiers:
        return
    removed = _run_daemon_checked(
        ["docker", "container", "rm", "--force", *identifiers],
        cwd=cwd,
        env=env,
        capture=True,
    )
    if removed.returncode != 0:
        raise StoragePathError("automatic removal of an unsafe application container failed")


def _observed_storage_mounts(
    identifier: str,
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, dict[str, object]]:
    """Return the daemon's persisted mounts for both durable child targets."""
    inspected = _run_daemon_checked(
        [
            "docker",
            "container",
            "inspect",
            identifier,
            "--format",
            "{{json .Mounts}}",
        ],
        cwd=cwd,
        env=env,
        capture=True,
    )
    if inspected.returncode != 0:
        raise StoragePathError("cannot inspect the created application container mounts")
    try:
        mounts = json.loads((inspected.stdout or "").strip())
    except (TypeError, ValueError) as exc:
        raise StoragePathError(
            "created application container mounts were not machine-readable"
        ) from exc
    if not isinstance(mounts, list):
        raise StoragePathError("created application container has no mount inventory")

    observed: dict[str, dict[str, object]] = {}
    for mount in mounts:
        if not isinstance(mount, dict):
            raise StoragePathError("created application container has an invalid mount")
        target = _normalized_target(mount.get("Destination"))
        if target in STORAGE_TARGETS.values():
            if mount.get("Destination") != target or target in observed:
                raise StoragePathError(
                    "created application container repeats or rewrites a storage target"
                )
            observed[target] = mount
        elif target == STORAGE_TARGET or target.startswith(f"{STORAGE_TARGET}/"):
            raise StoragePathError("created application container has an unreviewed storage mount")
    if set(observed) != set(STORAGE_TARGETS.values()):
        raise StoragePathError("created application container is missing a durable child bind")
    return observed


def _require_observed_child_sources(
    observed: dict[str, dict[str, object]],
    *,
    expected_sources: dict[str, Path],
) -> None:
    """Prove both durable child binds persisted their canonical host sources."""
    for child, target in STORAGE_TARGETS.items():
        mount = observed[target]
        if mount.get("Type") != "bind" or mount.get("RW") is not True:
            raise StoragePathError(f"created application container changed the {child} bind mode")
        inspected_source = _inspected_host_source(mount.get("Source"))
        expected_source = expected_sources[child]
        exact_spelling = os.path.normcase(os.path.normpath(str(inspected_source)))
        expected_spelling = os.path.normcase(os.path.normpath(str(expected_source)))
        if (
            exact_spelling != expected_spelling
            or inspected_source.resolve(strict=False) != expected_source
        ):
            raise StoragePathError(
                f"created application container persisted the wrong {child} source"
            )


# ============================================================================
# Purpose: Inspect Docker's persisted mount inventory after container creation
#   and prove both storage targets retain their durable canonical host sources.
# Database/ORM: None.
# Standards: Machine-readable daemon output only; exact service/container IDs;
#   wrong, missing, duplicate, or read-only binds fail closed.
# Blast Radius: Application container lifecycle and durable artifact/blob paths.
# Connections:
#   - File: docker-compose.yml -> Declares the two reviewed child bind targets.
#   - File: scripts/validate_compose_storage_path.py -> Supplies proven sources.
# ============================================================================
def _inspect_container_storage_mounts(
    identifier: str,
    *,
    cwd: Path,
    env: dict[str, str],
    expected_sources: dict[str, Path],
) -> None:
    """Prove one captured container has both exact durable child binds."""
    observed = _observed_storage_mounts(identifier, cwd=cwd, env=env)
    _require_observed_child_sources(observed, expected_sources=expected_sources)


# ============================================================================
# Purpose: Audit every captured app container after a detached create attempt
#   and remove only containers whose persisted storage mounts are unsafe.
# Database/ORM: None.
# Standards: Exact Source/Destination/RW proof per validated container ID;
#   captured-ID cleanup only; no volume deletion; unsafe cleanup fails closed.
# Blast Radius: Application container lifecycle and durable host storage.
# Connections:
#   - File: docker-compose.yml -> Declares the reviewed storage mount contract.
#   - File: tests/scripts/test_compose_storage_preflight.py -> Adversarial proof.
# ============================================================================
def _audit_current_storage_containers(
    request: LaunchRequest,
    *,
    cwd: Path,
    env: dict[str, str],
    expected_sources: dict[str, Path],
    require_exactly_one: bool,
) -> StoragePathError | None:
    """Inspect captured app IDs and remove only IDs that violate the contract."""
    capture = _capture_storage_container_ids(request, cwd=cwd, env=env)
    identifiers = capture.identifiers
    issue: StoragePathError | None = None
    mismatching: list[str] = []
    if require_exactly_one and len(identifiers) != 1:
        issue = StoragePathError(
            "Compose did not create exactly one reviewed application container"
        )

    for identifier in identifiers:
        try:
            _inspect_container_storage_mounts(
                identifier,
                cwd=cwd,
                env=env,
                expected_sources=expected_sources,
            )
        except StoragePathError as exc:
            if issue is None:
                issue = exc
            if identifier not in mismatching:
                mismatching.append(identifier)

    if mismatching:
        try:
            _remove_container_ids(tuple(mismatching), cwd=cwd, env=env)
        except StoragePathError as cleanup_error:
            raise StoragePathError(
                "container mount verification failed and scoped removal also failed"
            ) from cleanup_error
    if not capture.complete:
        raise StoragePathError(
            "cannot prove complete enumeration of the scoped application container"
        )
    return issue


def _verify_created_storage_mounts(
    request: LaunchRequest,
    *,
    cwd: Path,
    env: dict[str, str],
    expected_sources: dict[str, Path],
) -> None:
    """Require exactly one app container with durable canonical child binds."""
    issue = _audit_current_storage_containers(
        request,
        cwd=cwd,
        env=env,
        expected_sources=expected_sources,
        require_exactly_one=True,
    )
    if issue is not None:
        raise issue


def _remove_request_storage_containers(
    request: LaunchRequest,
    *,
    cwd: Path,
    env: dict[str, str],
) -> None:
    """Remove captured scoped app containers after an identity failure."""
    capture = _capture_storage_container_ids(request, cwd=cwd, env=env)
    _remove_container_ids(capture.identifiers, cwd=cwd, env=env)
    if not capture.complete:
        raise StoragePathError(
            "cannot prove complete enumeration of the scoped application container"
        )


# ============================================================================
# Purpose: Fail closed if the held host-storage identity changes after Compose
#   may have created an application container.
# Database/ORM: None.
# Standards: Re-enumerate only the scoped app service; remove containers by
#   validated ID without deleting volumes; preserve the identity failure.
# Blast Radius: Application container lifecycle and host storage integrity.
# Connections:
#   - File: scripts/validate_compose_storage_path.py -> Owns identity checking.
#   - File: docker-compose.yml -> Declares the guarded child bind sources.
# ============================================================================
def _assert_storage_identity_or_remove(
    request: LaunchRequest,
    *,
    cwd: Path,
    env: dict[str, str],
    identity_guard: StorageIdentityGuard,
    cleanup_message: str,
) -> None:
    """Remove scoped app containers if the held storage identity changed."""
    try:
        identity_guard.assert_current()
    except StoragePathError:
        try:
            _remove_request_storage_containers(request, cwd=cwd, env=env)
        except StoragePathError as cleanup_error:
            raise StoragePathError(cleanup_message) from cleanup_error
        raise


# FIX: `/proc/<launcher-pid>/fd/<child-fd>` kept the initial mount identity but
# left a dead or reusable source in Docker's restart configuration. Containers
# now persist canonical child paths; held descriptors, trusted parent modes,
# pathname identity checks, and Docker inspect protect the creation boundary.
# ============================================================================
# Purpose: Reconcile one storage-bearing app container in a detached create
#   phase, prove its persisted mounts, then preserve requested attach behavior.
# Database/ORM: None.
# Standards: Identity checks bracket every Compose up; every detached attempt
#   audits captured app IDs; mount mismatches remove only those IDs, never data.
# Blast Radius: Application container creation, restart, and storage durability.
# Connections:
#   - File: docker-compose.yml -> Supplies restart policy and canonical binds.
#   - File: tests/scripts/test_compose_storage_preflight.py -> Lifecycle proofs.
# ============================================================================
def _run_guarded_storage_up(
    request: LaunchRequest,
    *,
    cwd: Path,
    env: dict[str, str],
    identity_guard: StorageIdentityGuard,
    expected_sources: dict[str, Path],
) -> subprocess.CompletedProcess[str]:
    """Create, inspect, and optionally attach to one durable app container."""
    create_args = _detached_storage_up_args(request)
    identity_guard.assert_current()
    created = _run_daemon_checked(
        ["docker", "compose", *create_args],
        cwd=cwd,
        env=env,
    )
    # FIX: Compose may return nonzero after creating or reconciling the app.
    # Audit every captured ID and remove only unsafe ones before preserving the
    # original command status; an unsafe audit or cleanup still fails closed.
    if created.returncode != 0:
        _audit_current_storage_containers(
            request,
            cwd=cwd,
            env=env,
            expected_sources=expected_sources,
            require_exactly_one=False,
        )
        _assert_storage_identity_or_remove(
            request,
            cwd=cwd,
            env=env,
            identity_guard=identity_guard,
            cleanup_message=(
                "failed storage creation changed identity and automatic removal also failed"
            ),
        )
        return created
    created_issue = _audit_current_storage_containers(
        request,
        cwd=cwd,
        env=env,
        expected_sources=expected_sources,
        require_exactly_one=True,
    )
    _assert_storage_identity_or_remove(
        request,
        cwd=cwd,
        env=env,
        identity_guard=identity_guard,
        cleanup_message=("storage identity verification failed and automatic removal also failed"),
    )
    if created_issue is not None:
        raise created_issue

    if create_args == request.compose_args:
        return created

    _assert_storage_identity_or_remove(
        request,
        cwd=cwd,
        env=env,
        identity_guard=identity_guard,
        cleanup_message=("storage identity verification failed and automatic removal also failed"),
    )
    attached = _run_daemon_checked(
        ["docker", "compose", *request.compose_args],
        cwd=cwd,
        env=env,
    )
    _assert_storage_identity_or_remove(
        request,
        cwd=cwd,
        env=env,
        identity_guard=identity_guard,
        cleanup_message=("attached storage verification failed and automatic removal also failed"),
    )
    _verify_created_storage_mounts(
        request,
        cwd=cwd,
        env=env,
        expected_sources=expected_sources,
    )
    return attached


def _pin_immutable_image(
    request: LaunchRequest,
    *,
    environment: dict[str, str],
    project_root: Path,
    rendered_path: Path | None,
) -> tuple[str, Path | None]:
    """Build and pin the reviewed image, then revalidate the rendered model."""
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
            raise StoragePathError("immutable image pinning changed the rendered storage source")
        rendered_path = pinned_path
    return expected_image, rendered_path


def _run_storage_startup(
    request: LaunchRequest,
    *,
    environment: dict[str, str],
    project_root: Path,
    rendered_path: Path | None,
    expected_image: str,
) -> int:
    """Run the guarded storage workflow and return its Compose exit status."""
    if rendered_path is None:
        raise StoragePathError("storage action has no rendered source")
    storage_path = prepare_storage_path(
        rendered_path,
        project_root=PROJECT_ROOT,
    )
    completed_storage_up: subprocess.CompletedProcess[str] | None = None
    final_identity_verified = False
    # FIX: The identity context performs its own post-yield check.
    # Keep final-check cleanup inside the held guard, then arm a
    # second cleanup boundary only for a later context-exit race.
    try:
        with hold_storage_identity(storage_path) as identity_guard:
            guarded_child_sources = {
                child: identity_guard.child_source(child) for child in STORAGE_CHILDREN
            }
            for child, source in guarded_child_sources.items():
                environment[STORAGE_SOURCE_ENV[child]] = str(source)
            guarded_model = _render_model(
                request,
                cwd=PROJECT_ROOT,
                env=environment,
            )
            guarded_path = _validate_rendered_model(
                guarded_model,
                project_root=project_root,
                expected_image=expected_image,
                expected_storage_sources=guarded_child_sources,
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
            completed_storage_up = _run_guarded_storage_up(
                request,
                cwd=PROJECT_ROOT,
                env=environment,
                identity_guard=identity_guard,
                expected_sources=guarded_child_sources,
            )
            _assert_storage_identity_or_remove(
                request,
                cwd=PROJECT_ROOT,
                env=environment,
                identity_guard=identity_guard,
                cleanup_message=(
                    "final storage identity verification failed and automatic removal also failed"
                ),
            )
            final_identity_verified = True
    except StoragePathError:
        if completed_storage_up is None or not final_identity_verified:
            raise
        try:
            _remove_request_storage_containers(
                request,
                cwd=PROJECT_ROOT,
                env=environment,
            )
        except StoragePathError as cleanup_error:
            raise StoragePathError(
                "storage identity context exit failed and automatic removal also failed"
            ) from cleanup_error
        raise
    if completed_storage_up is None:
        raise StoragePathError("storage startup returned no process status")
    return completed_storage_up.returncode


def _run_compose_command(
    request: LaunchRequest,
    *,
    environment: dict[str, str],
) -> int:
    """Run one non-storage Compose command and return its exit status."""
    command = ["docker", "compose", *request.compose_args]
    if request.action == "config":
        completed = _run_checked(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            capture=True,
        )
        if completed.returncode != 0:
            raise StoragePathError(
                "the pinned Compose configuration is invalid; inspect the env file syntax"
            )
        return 0
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
    return completed.returncode


# ============================================================================
# Purpose: Validate the pinned model and host store, run a non-root write probe,
#   then create and inspect one normalized storage-bearing Compose workflow.
# Database/ORM: None.
# Standards: Routes no arbitrary Compose flags; local daemon only; final model,
#   path identities, and Docker's persisted Mounts are rechecked before success.
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

            expected_image, rendered_path = _pin_immutable_image(
                request,
                environment=environment,
                project_root=project_root,
                rendered_path=rendered_path,
            )

            if request.requires_storage:
                return _run_storage_startup(
                    request,
                    environment=environment,
                    project_root=project_root,
                    rendered_path=rendered_path,
                    expected_image=expected_image,
                )
            return _run_compose_command(request, environment=environment)
    except StoragePathError as exc:
        print(f"storage preflight failed: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"could not execute Docker Compose: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
