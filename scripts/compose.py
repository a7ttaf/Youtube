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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from validate_compose_storage_path import (
    StoragePathError,
    prepare_storage_path,
    storage_tree_identity,
    validate_storage_path,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
STORAGE_IMAGE = "ums-smart-revenue:dev"
STORAGE_TARGET = "/var/lib/ums"
EXPECTED_SERVICES = {"postgres", "redis", "migrate", "app", "app-dev"}
APP_SERVICES = {"app", "app-dev"}

# The probe runs with the exact uid/gid declared by the rendered app service.
# It deliberately performs no chown/chmod/mkdir operation: the launcher never
# gives a root container an operator-controlled host path.
STORAGE_WRITE_PROBE = r"""
set -eu
for path in /var/lib/ums /var/lib/ums/artifacts /var/lib/ums/blobs; do
  if [ -L "$path" ] || [ ! -d "$path" ]; then
    echo "storage probe: expected a real directory at $path" >&2
    exit 1
  fi
done
umask 077
artifact_probe="/var/lib/ums/artifacts/.ums-write-probe-$$"
blob_probe="/var/lib/ums/blobs/.ums-write-probe-$$"
cleanup() { rm -f "$artifact_probe" "$blob_probe"; }
trap cleanup EXIT HUP INT TERM
: > "$artifact_probe"
: > "$blob_probe"
""".strip()


@dataclass(frozen=True)
class LaunchRequest:
    """One parsed and normalized launcher invocation."""

    compose_args: tuple[str, ...]
    global_args: tuple[str, ...]
    action: str
    requires_storage: bool
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
            internal_storage_path=True,
        )

    normalized_tail: list[str]
    requires_storage = False
    if action == "up":
        normalized_tail = []
        tail_index = 0
        while tail_index < len(tail) and tail[tail_index] in {
            "-d",
            "--detach",
            "--build",
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
    elif action == "run":
        if tail != ["--rm", "migrate"]:
            raise StoragePathError("the only supported one-shot run is 'run --rm migrate'")
        normalized_tail = list(tail)
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
        if tail not in ([], ["--quiet"]):
            raise StoragePathError("config supports only the optional --quiet flag")
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
    )


def _compose_environment(source: dict[str, str]) -> dict[str, str]:
    """Pin Compose to the reviewed file and discard ambient Compose controls."""

    if source.get("COMPOSE_FILE"):
        raise StoragePathError(
            "COMPOSE_FILE overrides are unsupported; use the reviewed docker-compose.yml"
        )
    environment = {
        key: value for key, value in source.items() if not key.upper().startswith("COMPOSE_")
    }
    environment["COMPOSE_FILE"] = str(COMPOSE_FILE)
    return environment


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


def _require_local_docker_context(*, cwd: Path, env: dict[str, str]) -> None:
    """Refuse a daemon whose bind source is not on this workstation."""

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
) -> Path:
    """Validate the exact storage projection and return its canonical source."""

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

        if service_name not in APP_SERVICES:
            continue
        if service.get("image") != STORAGE_IMAGE:
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


def _ensure_image(*, request: LaunchRequest, cwd: Path, env: dict[str, str]) -> None:
    """Build the reviewed app image only when its local tag is absent."""

    inspected = _run_checked(
        ["docker", "image", "inspect", STORAGE_IMAGE],
        cwd=cwd,
        env=env,
        capture=True,
    )
    if inspected.returncode == 0:
        return
    built = _run_checked(
        ["docker", "compose", *request.global_args, "build", "app"],
        cwd=cwd,
        env=env,
    )
    if built.returncode != 0:
        raise StoragePathError("cannot build the application image for the write probe")


# ============================================================================
# Purpose: Prove uid 10001 can write both durable stores through the exact
#   rendered app service without granting a root container host-path access.
# Database/ORM: None.
# Standards: No shell interpolation on the host; no chown/chmod/mkdir in the
#   container; image-inspection output is captured and never logged.
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

    _ensure_image(request=request, cwd=cwd, env=env)
    result = _run_checked(
        [
            "docker",
            "compose",
            *request.global_args,
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "sh",
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

        if request.internal_storage_path or request.requires_storage:
            model = _render_model(request, cwd=PROJECT_ROOT, env=environment)
            rendered_path = _validate_rendered_model(
                model,
                project_root=PROJECT_ROOT.resolve(strict=True),
            )
            if request.internal_storage_path:
                storage_path = validate_storage_path(
                    rendered_path,
                    project_root=PROJECT_ROOT,
                    require_exists=True,
                )
                print(storage_path)
                return 0

            _require_local_docker_context(cwd=PROJECT_ROOT, env=environment)
            storage_path = prepare_storage_path(
                rendered_path,
                project_root=PROJECT_ROOT,
            )
            environment["UMS_APP_DATA_HOST"] = str(storage_path)

            canonical_model = _render_model(
                request,
                cwd=PROJECT_ROOT,
                env=environment,
            )
            canonical_path = _validate_rendered_model(
                canonical_model,
                project_root=PROJECT_ROOT.resolve(strict=True),
            )
            if canonical_path != storage_path:
                raise StoragePathError(
                    "canonical storage environment does not reproduce the rendered source"
                )

            before = storage_tree_identity(storage_path)
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
            if storage_tree_identity(storage_path) != before:
                raise StoragePathError(
                    "storage root or direct child identity changed during the Docker probe"
                )

        completed = subprocess.run(
            ["docker", "compose", *request.compose_args],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
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
