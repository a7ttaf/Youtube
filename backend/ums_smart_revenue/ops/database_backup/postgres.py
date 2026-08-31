"""PostgreSQL and Docker adapters for backup and restore tooling."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

import psycopg
from psycopg import Connection, sql

from ums_smart_revenue.ops.database_backup.contracts import (
    BackupToolError,
    DatabaseIdentity,
    DatabaseLocale,
    SequenceRecord,
    SourceRecord,
    TableRecord,
)
from ums_smart_revenue.ops.database_backup.semantic import (
    authorization_catalog_digest,
    canonical_authorization_payload,
    payload_from_database_rows,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$.-]*$")
_SAFE_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")
_TOOL_LABEL = "ums.smart-revenue.database-client"
_OFFICIAL_CLIENT_BIN = "/usr/local/bin"
_LIBPQ_OVERRIDE_ENV = (
    "PGAPPNAME",
    "PGCHANNELBINDING",
    "PGCONNECT_TIMEOUT",
    "PGDATABASE",
    "PGGSSENCMODE",
    "PGHOST",
    "PGHOSTADDR",
    "PGOPTIONS",
    "PGPASSFILE",
    "PGPASSWORD",
    "PGPORT",
    "PGREQUIREAUTH",
    "PGREQUIREPEER",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGSSLCERT",
    "PGSSLCRL",
    "PGSSLCRLDIR",
    "PGSSLKEY",
    "PGSSLMODE",
    "PGSSLROOTCERT",
    "PGTARGETSESSIONATTRS",
    "PGUSER",
)


@dataclass(frozen=True)
class ContainerConnection:
    """Secret-safe connection material read from a PostgreSQL container."""

    container: str
    host: str
    port: int
    database: str
    user: str
    password: str
    image_id: str
    image_reference: str


@dataclass(frozen=True)
class TargetContract:
    """Database properties that must match before archive replay."""

    database: str
    server_version_num: int
    locale: DatabaseLocale


def _safe_detail(stderr: str) -> str:
    """Collapse native stderr without echoing command lines or environment."""
    detail = " ".join(line.strip() for line in stderr.splitlines() if line.strip())
    return detail[:800] or "no diagnostic text"


class CommandRunner:
    """Bounded native-command runner with stable, secret-safe failures."""

    def __init__(self, *, timeout_seconds: int = 300) -> None:
        if timeout_seconds < 1:
            raise BackupToolError("command timeout must be at least one second", exit_code=2)
        self.timeout_seconds = timeout_seconds

    def text(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        environment: Mapping[str, str] | None = None,
        exit_code: int = 5,
    ) -> str:
        command_environment = None
        if environment is not None:
            command_environment = os.environ.copy()
            command_environment.update(environment)
        try:
            completed = subprocess.run(
                list(argv),
                input=stdin,
                capture_output=True,
                text=True,
                env=command_environment,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupToolError(
                f"native command could not complete: {type(exc).__name__}",
                exit_code=exit_code,
            ) from exc
        if completed.returncode != 0:
            raise BackupToolError(
                f"native command failed: {_safe_detail(completed.stderr)}",
                exit_code=exit_code,
            )
        return completed.stdout

    def binary_to_file(
        self,
        argv: Sequence[str],
        destination: Path,
        *,
        environment: Mapping[str, str] | None = None,
        exit_code: int = 5,
    ) -> None:
        command_environment = None
        if environment is not None:
            command_environment = os.environ.copy()
            command_environment.update(environment)
        try:
            with destination.open("xb") as stream:
                completed = subprocess.run(
                    list(argv),
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    env=command_environment,
                    timeout=self.timeout_seconds,
                    check=False,
                )
        except FileExistsError as exc:
            raise BackupToolError(
                f"refusing to overwrite staging file {destination.name}", exit_code=2
            ) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupToolError(
                f"native dump command could not complete: {type(exc).__name__}",
                exit_code=exit_code,
            ) from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")
            raise BackupToolError(
                f"native dump command failed: {_safe_detail(stderr)}",
                exit_code=exit_code,
            )

    def file_to_text(
        self,
        argv: Sequence[str],
        source: Path,
        *,
        environment: Mapping[str, str] | None = None,
        exit_code: int = 6,
    ) -> str:
        try:
            with source.open("rb") as stream:
                return self.stream_to_text(
                    argv,
                    stream,
                    environment=environment,
                    exit_code=exit_code,
                )
        except OSError as exc:
            raise BackupToolError(
                f"native archive command could not complete: {type(exc).__name__}",
                exit_code=exit_code,
            ) from exc

    def stream_to_text(
        self,
        argv: Sequence[str],
        source: BinaryIO,
        *,
        environment: Mapping[str, str] | None = None,
        exit_code: int = 6,
    ) -> str:
        """Run a command from one already-verified and pinned binary stream."""
        command_environment = None
        if environment is not None:
            command_environment = os.environ.copy()
            command_environment.update(environment)
        try:
            source.seek(0)
            completed = subprocess.run(
                list(argv),
                stdin=source,
                capture_output=True,
                env=command_environment,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupToolError(
                f"native archive command could not complete: {type(exc).__name__}",
                exit_code=exit_code,
            ) from exc
        if completed.returncode != 0:
            raise BackupToolError(
                "native archive command failed; diagnostic output was suppressed",
                exit_code=exit_code,
            )
        return completed.stdout.decode("utf-8", errors="replace")

    def file_input(
        self,
        argv: Sequence[str],
        source: Path,
        *,
        environment: Mapping[str, str] | None = None,
        exit_code: int = 6,
    ) -> str:
        return self.file_to_text(
            argv,
            source,
            environment=environment,
            exit_code=exit_code,
        )


def _container_inspect(runner: CommandRunner, container: str) -> dict[str, object]:
    if not _SAFE_CONTAINER_RE.fullmatch(container):
        raise BackupToolError("container name/id contains unsupported characters", exit_code=2)
    raw = runner.text(["docker", "container", "inspect", container], exit_code=3)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupToolError("docker inspect returned malformed JSON", exit_code=3) from exc
    if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], dict):
        raise BackupToolError("docker inspect did not identify exactly one container", exit_code=3)
    return body[0]


def resolve_rehearsal_image(
    runner: CommandRunner,
    *,
    operator_reference: str,
    expected_image_id: str,
) -> str:
    """Resolve an operator-selected local PostgreSQL image to the manifest id."""
    if not operator_reference or any(character in operator_reference for character in "\r\n\x00"):
        raise BackupToolError("--rehearse-image must be a local image reference", exit_code=2)
    raw = runner.text(["docker", "image", "inspect", operator_reference], exit_code=3)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupToolError("docker image inspect returned malformed JSON", exit_code=3) from exc
    if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], dict):
        raise BackupToolError("rehearsal image did not resolve exactly once", exit_code=3)
    image = body[0]
    image_id = image.get("Id")
    if image_id != expected_image_id:
        raise BackupToolError(
            "operator-selected rehearsal image does not match the backup source image id",
            exit_code=2,
        )
    config = image.get("Config")
    if not isinstance(config, dict):
        raise BackupToolError("rehearsal image has no readable config", exit_code=3)
    environment = config.get("Env")
    entrypoint = config.get("Entrypoint")
    command = config.get("Cmd")
    has_pg_major = isinstance(environment, list) and any(
        isinstance(item, str) and item.startswith("PG_MAJOR=") for item in environment
    )
    entrypoint_text = (
        " ".join(entrypoint) if isinstance(entrypoint, list) else str(entrypoint or "")
    )
    command_text = " ".join(command) if isinstance(command, list) else str(command or "")
    if (
        not has_pg_major
        or "docker-entrypoint" not in entrypoint_text
        or "postgres" not in command_text
    ):
        raise BackupToolError(
            "operator-selected rehearsal image is not a PostgreSQL image", exit_code=2
        )
    assert isinstance(image_id, str)
    return image_id


def create_rehearsal_container(
    runner: CommandRunner,
    *,
    image_id: str,
    database: str,
    user: str,
    name: str,
    ownership_token: str,
) -> None:
    """Create a password-protected target with an ephemeral loopback port."""
    if not all(_IDENTIFIER_RE.fullmatch(value) for value in (database, user)):
        raise BackupToolError("manifest database/user is unsafe for rehearsal", exit_code=2)
    if not _SAFE_CONTAINER_RE.fullmatch(name):
        raise BackupToolError("rehearsal container name is unsafe", exit_code=2)
    if not re.fullmatch(r"[0-9a-f]{32}", ownership_token):
        raise BackupToolError("rehearsal ownership token is unsafe", exit_code=2)
    if name != f"ums-db-restore-rehearsal-{ownership_token}":
        raise BackupToolError("rehearsal name is not bound to its ownership token", exit_code=2)
    password = secrets.token_urlsafe(48)
    runner.text(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"ums.smart-revenue.rehearsal={ownership_token}",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_PASSWORD",
            "--env",
            f"POSTGRES_DB={database}",
            "--env",
            f"POSTGRES_USER={user}",
            image_id,
        ],
        environment={"POSTGRES_PASSWORD": password},
        exit_code=4,
    )


def remove_rehearsal_container(runner: CommandRunner, name: str, *, ownership_token: str) -> None:
    """Remove exactly the uniquely named container created by this rehearsal."""
    if not name.startswith("ums-db-restore-rehearsal-") or not _SAFE_CONTAINER_RE.fullmatch(name):
        raise BackupToolError("refusing to remove an unrecognized container name", exit_code=7)
    if not re.fullmatch(r"[0-9a-f]{32}", ownership_token):
        raise BackupToolError("refusing cleanup with an invalid ownership token", exit_code=7)
    if name != f"ums-db-restore-rehearsal-{ownership_token}":
        raise BackupToolError("refusing cleanup for an ownership/name mismatch", exit_code=7)
    listed = runner.text(
        [
            "docker",
            "container",
            "ls",
            "--all",
            "--filter",
            f"name=^/{name}$",
            "--format",
            "{{.ID}}",
        ],
        exit_code=7,
    )
    identities = [line.strip() for line in listed.splitlines() if line.strip()]
    if not identities:
        return
    if len(identities) != 1 or not _SAFE_CONTAINER_RE.fullmatch(identities[0]):
        raise BackupToolError(
            "rehearsal cleanup did not resolve exactly one container", exit_code=7
        )
    inspect = _container_inspect(runner, identities[0])
    config = inspect.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or labels.get("ums.smart-revenue.rehearsal") != ownership_token:
        raise BackupToolError("refusing to remove a rehearsal with foreign ownership", exit_code=7)
    immutable_id = inspect.get("Id")
    if not isinstance(immutable_id, str) or not re.fullmatch(r"[0-9a-f]{64}", immutable_id):
        raise BackupToolError("rehearsal immutable container id is unavailable", exit_code=7)
    runner.text(
        ["docker", "container", "rm", "--force", "--volumes", immutable_id],
        exit_code=7,
    )


def _container_environment(inspect: dict[str, object]) -> dict[str, str]:
    config = inspect.get("Config")
    if not isinstance(config, dict) or not isinstance(config.get("Env"), list):
        raise BackupToolError("container has no readable environment contract", exit_code=3)
    result: dict[str, str] = {}
    for item in config["Env"]:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            result[key] = value
    return result


def _published_postgres_endpoint(inspect: dict[str, object]) -> tuple[str, int]:
    network = inspect.get("NetworkSettings")
    ports = network.get("Ports") if isinstance(network, dict) else None
    bindings = ports.get("5432/tcp") if isinstance(ports, dict) else None
    if not isinstance(bindings, list) or not bindings:
        raise BackupToolError(
            "PostgreSQL container must publish port 5432 to the host for snapshot export",
            exit_code=3,
        )
    candidates: list[tuple[str, int]] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            raise BackupToolError("container has a malformed PostgreSQL port binding", exit_code=3)
        host_ip = binding.get("HostIp")
        host_port = binding.get("HostPort")
        if not isinstance(host_ip, str) or not isinstance(host_port, str):
            raise BackupToolError("container has a malformed PostgreSQL port binding", exit_code=3)
        if not host_port.isdecimal() or not 1 <= int(host_port) <= 65535:
            raise BackupToolError("container has an invalid PostgreSQL host port", exit_code=3)
        if host_ip not in {"127.0.0.1", "::1"}:
            raise BackupToolError(
                "container must publish PostgreSQL only on 127.0.0.1 or ::1",
                exit_code=3,
            )
        candidates.append((host_ip, int(host_port)))
    if not candidates:
        raise BackupToolError(
            "container must publish PostgreSQL only on 127.0.0.1 or ::1",
            exit_code=3,
        )
    host, port = sorted(candidates)[0]
    return host, port


# PostgreSQL 18 creates these NOLOGIN roles in the bootstrap catalog. Pinning
# the names and their attribute matrix prevents a supposedly fresh target from
# carrying a modified predefined role with elevated authority.
_PG18_PREDEFINED_ROLES = (
    "pg_checkpoint",
    "pg_create_subscription",
    "pg_database_owner",
    "pg_execute_server_program",
    "pg_maintain",
    "pg_monitor",
    "pg_read_all_data",
    "pg_read_all_settings",
    "pg_read_all_stats",
    "pg_read_server_files",
    "pg_signal_autovacuum_worker",
    "pg_signal_backend",
    "pg_stat_scan_tables",
    "pg_use_reserved_connections",
    "pg_write_all_data",
    "pg_write_server_files",
)

_PG18_PREDEFINED_MEMBERSHIPS = (
    "pg_read_all_settings",
    "pg_read_all_stats",
    "pg_stat_scan_tables",
)


# ============================================================================
# Purpose: Resolve the exact Compose PostgreSQL container into a host connection
#   and immutable image identity without returning or logging its password.
# Database/ORM: Reads Docker container metadata only; no database access yet.
# Standards: Explicit container, loopback host port, strict required env fields.
# Blast Radius: Backup source selection; a wrong container is caught again by
#   persistent database identity binding before publication.
# Connections:
#   - File: docker-compose.yml -> postgres environment and loopback port contract.
#   - File: backend/ums_smart_revenue/ops/database_backup/backup.py -> snapshot.
# ============================================================================
def resolve_container_connection(runner: CommandRunner, container: str) -> ContainerConnection:
    inspect = _container_inspect(runner, container)
    if inspect.get("Path") != "docker-entrypoint.sh" or inspect.get("Args") != ["postgres"]:
        raise BackupToolError(
            "PostgreSQL container command differs from the official image default",
            exit_code=3,
        )
    environment = _container_environment(inspect)
    missing = [
        key
        for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
        if not environment.get(key)
    ]
    if missing:
        raise BackupToolError(
            f"container is missing required PostgreSQL fields: {', '.join(missing)}",
            exit_code=3,
        )
    database = environment["POSTGRES_DB"]
    user = environment["POSTGRES_USER"]
    if not _IDENTIFIER_RE.fullmatch(database) or not _IDENTIFIER_RE.fullmatch(user):
        raise BackupToolError("database/user contains unsupported characters", exit_code=3)
    image_id = inspect.get("Image")
    container_id = inspect.get("Id")
    config = inspect.get("Config")
    image_reference = config.get("Image") if isinstance(config, dict) else None
    if not isinstance(image_id, str) or not _IMAGE_ID_RE.fullmatch(image_id):
        raise BackupToolError("container image is not identified by SHA-256", exit_code=3)
    if (
        not isinstance(container_id, str)
        or len(container_id) != 64
        or any(character not in "0123456789abcdef" for character in container_id)
    ):
        raise BackupToolError("container immutable id is unavailable", exit_code=3)
    if not isinstance(image_reference, str) or not image_reference:
        raise BackupToolError("container image reference is unavailable", exit_code=3)
    host, port = _published_postgres_endpoint(inspect)
    return ContainerConnection(
        container=container_id,
        host=host,
        port=port,
        database=database,
        user=user,
        password=environment["POSTGRES_PASSWORD"],
        image_id=image_id,
        image_reference=image_reference,
    )


def _connect(source: ContainerConnection) -> Connection[tuple[object, ...]]:
    try:
        return psycopg.connect(
            host=source.host,
            port=source.port,
            dbname=source.database,
            user=source.user,
            password=source.password,
            connect_timeout=10,
        )
    except psycopg.Error as exc:
        raise BackupToolError(
            "could not connect to the selected PostgreSQL container through its host port",
            exit_code=4,
        ) from exc


# ============================================================================
# Purpose: Refuse a restore endpoint that accepts a fresh wrong password.
# Database/ORM: PostgreSQL authentication only; no SQL is issued.
# Standards: Random secret kept out of argv/logs; only SQLSTATE 28P01 passes.
# Blast Radius: Restored finance/auth data exposure on the host loopback port.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/restore.py -> prewrite.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> endpoint contract.
# ============================================================================
def require_password_authentication(source: ContainerConnection) -> None:
    """Prove the loopback target rejects a fresh deliberately wrong password."""
    wrong_password = "ums-wrong-password-" + secrets.token_hex(32)
    unexpected: Connection[tuple[object, ...]] | None = None
    try:
        unexpected = psycopg.connect(
            host=source.host,
            port=source.port,
            dbname=source.database,
            user=source.user,
            password=wrong_password,
            connect_timeout=10,
        )
    except psycopg.Error as exc:
        # SQLSTATE 28P01 is the rigorous signal, but psycopg on Windows wraps
        # libpq connection failures without propagating the SQLSTATE (observed
        # sqlstate None for the server's FATAL 28P01). The server's constant
        # "password authentication failed" text is the same PostgreSQL error;
        # anything else (refused, timeout, TLS) stays "could not prove".
        if exc.sqlstate == "28P01" or "password authentication failed" in str(exc):
            return
        raise BackupToolError(
            "could not prove the restore target enforces password authentication",
            exit_code=4,
        ) from exc
    finally:
        if unexpected is not None:
            unexpected.close()
    raise BackupToolError(
        "restore target accepted a deliberately wrong password; passwordless access is unsafe",
        exit_code=2,
    )


def _database_locale(connection: Connection[tuple[object, ...]]) -> DatabaseLocale:
    row = connection.execute(
        """
        SELECT
            pg_encoding_to_char(d.encoding),
            COALESCE(to_jsonb(d)->>'datcollate', ''),
            COALESCE(to_jsonb(d)->>'datctype', ''),
            COALESCE(to_jsonb(d)->>'datlocprovider', ''),
            COALESCE(to_jsonb(d)->>'datlocale', ''),
            COALESCE(to_jsonb(d)->>'daticurules', ''),
            COALESCE(to_jsonb(d)->>'datcollversion', '')
        FROM pg_catalog.pg_database AS d
        WHERE d.datname = current_database()
        """
    ).fetchone()
    if row is None or len(row) != 7 or any(not isinstance(value, str) for value in row):
        raise BackupToolError("could not read the source database locale", exit_code=4)
    encoding, collate, ctype, provider, locale, icu_rules, collation_version = cast(
        tuple[str, str, str, str, str, str, str], row
    )
    return DatabaseLocale(
        encoding=encoding,
        collate=collate,
        ctype=ctype,
        provider=provider,
        locale=locale,
        icu_rules=icu_rules,
        collation_version=collation_version,
    )


def _table_names(connection: Connection[tuple[object, ...]]) -> list[tuple[str, str]]:
    rows = connection.execute(
        """
        SELECT n.nspname, c.relname
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'm')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
          AND n.nspname NOT LIKE 'pg\\_temp\\_%' ESCAPE '\\'
          AND n.nspname NOT LIKE 'pg\\_toast\\_temp\\_%' ESCAPE '\\'
        ORDER BY n.nspname, c.relname
        """
    ).fetchall()
    result: list[tuple[str, str]] = []
    for schema, name in rows:
        if not isinstance(schema, str) or not isinstance(name, str):
            raise BackupToolError("database returned a malformed table identity", exit_code=4)
        result.append((schema, name))
    return result


# ============================================================================
# Purpose: Reject source data that a logical local PostgreSQL dump cannot own.
# Database/ORM: pg_class/pg_namespace foreign-table inventory; read-only.
# Standards: Exact catalog query and typed fail-closed errors.
# Blast Radius: Recovery completeness for externally stored relation rows.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/backup.py -> capture.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> supported source scope.
# ============================================================================
def require_no_foreign_tables(connection: Connection[tuple[object, ...]]) -> None:
    """Refuse foreign tables because pg_dump cannot capture their external rows."""
    row = connection.execute(
        """
        SELECT count(*)
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE c.relkind = 'f'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
          AND n.nspname NOT LIKE 'pg\\_temp\\_%' ESCAPE '\\'
          AND n.nspname NOT LIKE 'pg\\_toast\\_temp\\_%' ESCAPE '\\'
        """
    ).fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise BackupToolError("could not inspect source foreign tables", exit_code=4)
    if row[0] != 0:
        raise BackupToolError(
            "source contains foreign tables whose external rows cannot be backed up",
            exit_code=8,
        )


# ============================================================================
# Purpose: Verify no second database session is present inside the operator's
#   mandatory source-writer quiescence window.
# Database/ORM: pg_stat_activity only; no application-table write or mutation.
# Standards: All other sessions fail closed; operator acknowledgement remains
#   primary because a future writer cannot be predicted from a point-in-time query.
# Blast Radius: Snapshot, sequence, finance, auth, and audit consistency.
# Connections:
#   - File: scripts/backup_database.py -> explicit quiescence acknowledgement.
#   - File: backend/ums_smart_revenue/ops/database_backup/backup.py -> post-dump.
# ============================================================================
def require_source_quiescent(connection: Connection[tuple[object, ...]]) -> None:
    """Refuse a source with another database session during the writer-stop window."""
    row = connection.execute(
        """
        SELECT count(*)
        FROM pg_catalog.pg_stat_activity
        WHERE datid = (SELECT oid FROM pg_catalog.pg_database
                       WHERE datname = current_database())
          AND pid <> pg_backend_pid()
        """
    ).fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise BackupToolError("could not prove the source is quiescent", exit_code=4)
    if row[0] != 0:
        raise BackupToolError(
            "source has other database sessions; stop writers and retry",
            exit_code=8,
        )


# ============================================================================
# Purpose: Fence catalog rewrites and non-MVCC TRUNCATE before establishing the
#   exported snapshot, then hold explicit read-compatible relation locks.
# Database/ORM: pg_class and every local ordinary/partitioned table/materialized
#   view; locks only, with no SQL data or catalog writes.
# Standards: Deterministic identifiers through psycopg.sql; held until rollback.
# Blast Radius: Snapshot consistency under concurrent DDL/TRUNCATE.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/backup.py -> counts/dump.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> capture contract.
# ============================================================================
def _lock_export_relations(connection: Connection[tuple[object, ...]]) -> None:
    """Fence catalog rewrites/TRUNCATE before the repeatable-read snapshot exists."""
    # SHARE on pg_class conflicts with the catalog RowExclusive work needed by
    # TRUNCATE/DDL. Acquiring it before the first SELECT closes PostgreSQL's
    # non-MVCC TRUNCATE gap; per-relation ACCESS SHARE then documents and holds
    # the ordinary pg_dump compatibility lock explicitly.
    connection.execute("LOCK TABLE pg_catalog.pg_class IN SHARE MODE")
    relations = _table_names(connection)
    if relations:
        targets = sql.SQL(", ").join(
            sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))
            for schema, name in relations
        )
        connection.execute(sql.SQL("LOCK TABLE {} IN ACCESS SHARE MODE").format(targets))


# ============================================================================
# Purpose: Capture sequence parameters and non-MVCC state on both sides of the
#   dump so a missed writer cannot silently publish a collision-prone archive.
# Database/ORM: Reads pg_class, pg_namespace, pg_sequence, and each sequence.
# Standards: Writer quiescence remains primary; exact pre/post equality is a
#   fail-closed defense and the same records are verified after restore.
# Blast Radius: Primary-key/default generators and any application sequence.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/backup.py -> pre/post.
#   - File: backend/ums_smart_revenue/ops/database_backup/contracts.py -> record.
# ============================================================================
def snapshot_sequences(
    connection: Connection[tuple[object, ...]],
) -> tuple[SequenceRecord, ...]:
    rows = connection.execute(
        """
        SELECT n.nspname, c.relname, format_type(s.seqtypid, NULL),
               s.seqstart, s.seqincrement, s.seqmin, s.seqmax,
               s.seqcache, s.seqcycle
        FROM pg_catalog.pg_sequence AS s
        JOIN pg_catalog.pg_class AS c ON c.oid = s.seqrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
          AND n.nspname NOT LIKE 'pg\\_temp\\_%' ESCAPE '\\'
          AND n.nspname NOT LIKE 'pg\\_toast\\_temp\\_%' ESCAPE '\\'
        ORDER BY n.nspname, c.relname
        """
    ).fetchall()
    records: list[SequenceRecord] = []
    for row in rows:
        if (
            len(row) != 9
            or not all(isinstance(value, str) for value in row[:3])
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in row[3:8])
            or not isinstance(row[8], bool)
        ):
            raise BackupToolError("database returned malformed sequence metadata", exit_code=4)
        schema, name, data_type = cast(tuple[str, str, str], row[:3])
        state = connection.execute(
            sql.SQL("SELECT last_value, is_called FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(name)
            )
        ).fetchone()
        if (
            state is None
            or isinstance(state[0], bool)
            or not isinstance(state[0], int)
            or not isinstance(state[1], bool)
        ):
            raise BackupToolError(f"could not read sequence state for {schema}.{name}", exit_code=4)
        records.append(
            SequenceRecord(
                schema=schema,
                name=name,
                data_type=data_type,
                start_value=cast(int, row[3]),
                increment_by=cast(int, row[4]),
                min_value=cast(int, row[5]),
                max_value=cast(int, row[6]),
                cache_size=cast(int, row[7]),
                cycle=cast(bool, row[8]),
                last_value=state[0],
                is_called=state[1],
            )
        )
    return tuple(records)


def target_sequences(source: ContainerConnection) -> tuple[SequenceRecord, ...]:
    """Read restored sequence parameters/state for exact manifest verification."""
    connection = _connect(source)
    try:
        return snapshot_sequences(connection)
    except psycopg.Error as exc:
        raise BackupToolError("could not verify restored sequences", exit_code=7) from exc
    finally:
        connection.close()


# ============================================================================
# Purpose: Prove stored roles, permission metadata, and grant edges match the
#   runtime authorization registries before backup, then hash their exact shape.
# Database/ORM: roles, permissions, role_permission_assignments; read-only.
# Standards: Deterministic ordering, strict scalar validation, exact equality.
# Blast Radius: Authorization; unsafe or missing grant edges refuse recovery.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/semantic.py -> expected.
#   - File: backend/ums_smart_revenue/ops/database_backup/restore.py -> verifies.
# ============================================================================
def snapshot_authorization_catalog_digest(
    connection: Connection[tuple[object, ...]], *, require_canonical: bool
) -> str:
    """Hash exact authorization metadata/edges and optionally require registries."""
    role_rows = connection.execute(
        """
        SELECT key, label, description, service_only
        FROM public.roles ORDER BY key
        """
    ).fetchall()
    permission_rows = connection.execute(
        """
        SELECT key, label, sensitive, audit_on_use
        FROM public.permissions ORDER BY key
        """
    ).fetchall()
    assignment_rows = connection.execute(
        """
        SELECT role_key, permission_key
        FROM public.role_permission_assignments
        ORDER BY role_key, permission_key
        """
    ).fetchall()
    if (
        any(
            len(row) != 4
            or not all(isinstance(value, str) for value in row[:3])
            or not isinstance(row[3], bool)
            for row in role_rows
        )
        or any(
            len(row) != 4
            or not all(isinstance(value, str) for value in row[:2])
            or not all(isinstance(value, bool) for value in row[2:])
            for row in permission_rows
        )
        or any(
            len(row) != 2 or not all(isinstance(value, str) for value in row)
            for row in assignment_rows
        )
    ):
        raise BackupToolError("authorization catalog rows are malformed", exit_code=8)
    payload = payload_from_database_rows(
        roles=cast(Sequence[tuple[str, str, str, bool]], role_rows),
        permissions=cast(Sequence[tuple[str, str, bool, bool]], permission_rows),
        assignments=cast(Sequence[tuple[str, str]], assignment_rows),
    )
    if require_canonical and payload != canonical_authorization_payload():
        raise BackupToolError(
            "database authorization catalog does not match the runtime registries",
            exit_code=8,
        )
    return authorization_catalog_digest(payload)


def target_authorization_catalog_digest(source: ContainerConnection) -> str:
    """Read restored authorization semantics without assuming current timestamps."""
    connection = _connect(source)
    try:
        return snapshot_authorization_catalog_digest(connection, require_canonical=False)
    except psycopg.Error as exc:
        raise BackupToolError(
            "could not verify restored authorization catalog", exit_code=7
        ) from exc
    finally:
        connection.close()


def snapshot_table_counts(
    connection: Connection[tuple[object, ...]],
) -> tuple[TableRecord, ...]:
    """Count every user table through the same exported snapshot as pg_dump."""
    records: list[TableRecord] = []
    for schema, name in _table_names(connection):
        query = sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier(schema), sql.Identifier(name)
        )
        row = connection.execute(query).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
            raise BackupToolError(f"could not count {schema}.{name}", exit_code=4)
        records.append(TableRecord(schema=schema, name=name, rows=row[0]))
    return tuple(records)


def target_table_counts(source: ContainerConnection) -> tuple[TableRecord, ...]:
    """Read committed row counts from a restored target for exact verification."""
    connection = _connect(source)
    try:
        return snapshot_table_counts(connection)
    except psycopg.Error as exc:
        raise BackupToolError("could not verify restored table counts", exit_code=7) from exc
    finally:
        connection.close()


def target_contract(source: ContainerConnection) -> TargetContract:
    """Read target version/database/locale without requiring application tables."""
    connection = _connect(source)
    try:
        row = connection.execute(
            "SELECT current_database(), current_setting('server_version_num')::integer"
        ).fetchone()
        if (
            row is None
            or not isinstance(row[0], str)
            or isinstance(row[1], bool)
            or not isinstance(row[1], int)
        ):
            raise BackupToolError("could not read target database properties", exit_code=4)
        return TargetContract(
            database=row[0], server_version_num=row[1], locale=_database_locale(connection)
        )
    except psycopg.Error as exc:
        raise BackupToolError("could not read target database properties", exit_code=4) from exc
    finally:
        connection.close()


# FIX (real-PG18 validation 2026-09-01): the previous query arms compared the
# target against an idealized "fresh cluster" fingerprint (catalog rows at
# frozen xmin = 1, owners = bootstrap role, ACLs equal to acldefault or
# pg_init_privs). A genuine postgres:18-alpine initdb violates that model
# wholesale: 2,415 catalog rows carry xmin <> 1, 62 pg_class ACLs differ from
# initprivs, and 3 namespace conditions fire on a brand-new cluster — 3,956
# "dirty" conditions with zero user objects, so EVERY rehearsal and restore
# on this image refused. There is no static per-image fingerprint of that
# shape to compare against. The check now proves cleanliness the
# image-independent way: no user-reachable objects anywhere (non-system
# namespaces, catalog namespaces with user OIDs, unexpected
# languages/extensions/objects) and sane session settings.
_USER_OBJECT_COUNT_SQL = r"""
SELECT (
    (SELECT count(*) FROM pg_catalog.pg_class c
     JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\'
       AND n.nspname NOT LIKE 'pg\_toast\_temp\_%' ESCAPE '\'
       AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f'))
  + (SELECT count(*) FROM pg_catalog.pg_type t
     JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\'
       AND n.nspname NOT LIKE 'pg\_toast\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_proc p
     JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\'
       AND n.nspname NOT LIKE 'pg\_toast\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_namespace n
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast', 'public')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\'
       AND n.nspname NOT LIKE 'pg\_toast\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_extension ext
     WHERE ext.extname <> 'plpgsql')
  -- (the plpgsql extension comment is deliberately not compared: this
  -- image installs plpgsql with a NULL comment while pg_available_extensions
  -- advertises one, so comment equality is not a fresh-cluster invariant)
  + (SELECT CASE
       WHEN count(*) = 1
        AND bool_and(ext.extnamespace = 'pg_catalog'::regnamespace)
        AND bool_and(ext.extowner = (SELECT oid FROM pg_catalog.pg_roles
                                     WHERE rolname = current_user))
        AND bool_and(NOT ext.extrelocatable)
        AND bool_and(ext.extversion = available.default_version)
       THEN 0 ELSE 1 END
     FROM pg_catalog.pg_extension ext
     JOIN pg_catalog.pg_available_extensions available
       ON available.name = ext.extname
     WHERE ext.extname = 'plpgsql')
  + (SELECT CASE
       WHEN count(*) = 4
        AND count(*) FILTER (
          WHERE d.classid = 'pg_catalog.pg_language'::regclass
            AND EXISTS (
              SELECT 1 FROM pg_catalog.pg_language l
              WHERE l.oid = d.objid AND l.lanname = 'plpgsql'
            )
        ) = 1
        AND count(*) FILTER (
          WHERE d.classid = 'pg_catalog.pg_proc'::regclass
            AND EXISTS (
              SELECT 1 FROM pg_catalog.pg_proc p
              WHERE p.oid = d.objid
                AND p.proname = 'plpgsql_call_handler'
                AND pg_catalog.pg_get_function_identity_arguments(p.oid) = ''
            )
        ) = 1
        AND count(*) FILTER (
          WHERE d.classid = 'pg_catalog.pg_proc'::regclass
            AND EXISTS (
              SELECT 1 FROM pg_catalog.pg_proc p
              WHERE p.oid = d.objid
                AND p.proname = 'plpgsql_inline_handler'
                AND pg_catalog.pg_get_function_identity_arguments(p.oid) = 'internal'
            )
        ) = 1
        AND count(*) FILTER (
          WHERE d.classid = 'pg_catalog.pg_proc'::regclass
            AND EXISTS (
              SELECT 1 FROM pg_catalog.pg_proc p
              WHERE p.oid = d.objid
                AND p.proname = 'plpgsql_validator'
                AND pg_catalog.pg_get_function_identity_arguments(p.oid) = 'oid'
            )
        ) = 1
       THEN 0 ELSE 1 END
     FROM pg_catalog.pg_depend d
     JOIN pg_catalog.pg_extension ext
       ON d.refclassid = 'pg_catalog.pg_extension'::regclass
      AND d.refobjid = ext.oid
     WHERE ext.extname = 'plpgsql'
       AND d.deptype = 'e')
  + (SELECT count(*) FROM pg_catalog.pg_collation col
     JOIN pg_catalog.pg_namespace n ON n.oid = col.collnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_conversion cvt
     JOIN pg_catalog.pg_namespace n ON n.oid = cvt.connamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_operator op
     JOIN pg_catalog.pg_namespace n ON n.oid = op.oprnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_opclass oc
     JOIN pg_catalog.pg_namespace n ON n.oid = oc.opcnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_opfamily opf
     JOIN pg_catalog.pg_namespace n ON n.oid = opf.opfnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_ts_config tsc
     JOIN pg_catalog.pg_namespace n ON n.oid = tsc.cfgnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_ts_dict tsd
     JOIN pg_catalog.pg_namespace n ON n.oid = tsd.dictnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_ts_parser tsp
     JOIN pg_catalog.pg_namespace n ON n.oid = tsp.prsnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_ts_template tst
     JOIN pg_catalog.pg_namespace n ON n.oid = tst.tmplnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_statistic_ext se
     JOIN pg_catalog.pg_namespace n ON n.oid = se.stxnamespace
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_temp\_%' ESCAPE '\')
  + (SELECT count(*) FROM pg_catalog.pg_event_trigger)
  + (SELECT count(*) FROM pg_catalog.pg_foreign_data_wrapper)
  + (SELECT count(*) FROM pg_catalog.pg_foreign_server)
  + (SELECT count(*) FROM pg_catalog.pg_default_acl)
  + (SELECT count(*) FROM pg_catalog.pg_largeobject_metadata)
  + (SELECT count(*) FROM pg_catalog.pg_publication)
  + (SELECT count(*) FROM pg_catalog.pg_replication_slots)
  + (SELECT count(*) FROM pg_catalog.pg_replication_origin)
  + (SELECT count(*) FROM pg_catalog.pg_prepared_xacts)
  + (SELECT count(*) FROM pg_catalog.pg_subscription s
     WHERE s.subdbid = (SELECT oid FROM pg_catalog.pg_database
                        WHERE datname = current_database()))
  + (SELECT count(*) FROM pg_catalog.pg_transform)
  + (SELECT count(*) FROM pg_catalog.pg_db_role_setting)
  + (SELECT count(*) FROM pg_catalog.pg_parameter_acl)
  + (SELECT count(*) FROM pg_catalog.pg_seclabel)
  + (SELECT count(*) FROM pg_catalog.pg_shseclabel)
  + (SELECT count(*) FROM pg_catalog.pg_file_settings
     WHERE sourcefile LIKE '%/postgresql.auto.conf')
  + (SELECT count(*) FROM pg_catalog.pg_cast c WHERE c.oid >= 16384)
  + (SELECT count(*) FROM pg_catalog.pg_language l WHERE l.oid >= 16384)
  + (SELECT count(*) FROM pg_catalog.pg_am am WHERE am.oid >= 16384)
  + (SELECT CASE WHEN
         current_setting('session_replication_role') = 'origin'
     AND current_setting('row_security') = 'on'
     AND current_setting('fsync') = 'on'
     AND current_setting('full_page_writes') = 'on'
     AND current_setting('synchronous_commit') = 'on'
     AND current_setting('default_transaction_read_only') = 'off'
     AND current_setting('transaction_read_only') = 'off'
     AND current_setting('allow_system_table_mods') = 'off'
     AND current_setting('ignore_system_indexes') = 'off'
     AND current_setting('zero_damaged_pages') = 'off'
     AND current_setting('default_tablespace') = ''
     AND current_setting('temp_tablespaces') = ''
     AND current_setting('shared_preload_libraries') = ''
     AND current_setting('session_preload_libraries') = ''
     AND current_setting('local_preload_libraries') = ''
     AND current_setting('check_function_bodies') = 'on'
     AND current_setting('standard_conforming_strings') = 'on'
     AND current_setting('lo_compat_privileges') = 'off'
       THEN 0 ELSE 1 END)
  + (SELECT CASE
       WHEN d.datdba = (SELECT oid FROM pg_catalog.pg_roles
                        WHERE rolname = current_user)
        AND d.datacl IS NULL
       THEN 0 ELSE 1 END
     FROM pg_catalog.pg_database d
     WHERE d.datname = current_database())
  -- fire the moment anything user-side exists.
  + (SELECT CASE WHEN count(*) = 4 THEN 0 ELSE 1 END
     FROM pg_catalog.pg_language
     WHERE lanname IN ('internal', 'c', 'sql', 'plpgsql'))
  + (SELECT count(*) FROM pg_catalog.pg_class c
     JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname IN ('pg_catalog', 'information_schema')
       AND c.oid >= 16384)
  + (SELECT count(*) FROM pg_catalog.pg_proc p
     JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname IN ('pg_catalog', 'information_schema')
       AND p.oid >= 16384)
  + (SELECT count(*) FROM pg_catalog.pg_type t
     JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname IN ('pg_catalog', 'information_schema')
       AND t.oid >= 16384)
)::bigint
"""


def require_clean_target(source: ContainerConnection) -> None:
    """Fail closed if target-local catalogs differ from fresh PostgreSQL 18."""
    connection = _connect(source)
    try:
        row = connection.execute(_USER_OBJECT_COUNT_SQL).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
            raise BackupToolError("could not prove the restore target is clean", exit_code=4)
        if row[0] != 0:
            raise BackupToolError(
                f"restore target is not clean ({row[0]} dirty catalog conditions); "
                "refusal has no override",
                exit_code=2,
            )
    except psycopg.Error as exc:
        raise BackupToolError("could not prove the restore target is clean", exit_code=4) from exc
    finally:
        connection.close()


# ============================================================================
# Purpose: Prove direct restore targets are dedicated fresh PostgreSQL clusters,
#   not merely empty databases sharing cluster-wide roles with other workloads.
# Database/ORM: Reads cluster-wide databases, roles, memberships, and
#   tablespaces; no mutation.
# Standards: Exact PostgreSQL 18 bootstrap rows, including the three stock
#   pg_monitor memberships; any additional or altered catalog row fails closed.
# Blast Radius: Authorization, tablespaces, and unrelated databases; role SQL
#   cannot affect a shared or previously prepared cluster.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/restore.py -> prewrite.
#   - File: scripts/compose_restore_roles.sql -> later creates two NOLOGIN roles.
# ============================================================================
def require_dedicated_cluster(source: ContainerConnection) -> None:
    """Refuse anything except the exact fresh PostgreSQL 18 cluster shape."""
    connection = _connect(source)
    try:
        databases = connection.execute(
            """
            SELECT
                d.datname,
                pg_catalog.pg_get_userbyid(d.datdba),
                d.datistemplate,
                d.datallowconn,
                d.datconnlimit,
                tablespace.spcname,
                CASE
                  WHEN d.datname IN ('template0', 'template1') THEN
                    (SELECT count(*) FROM pg_catalog.aclexplode(d.datacl)) = 4
                    AND NOT EXISTS (
                      SELECT 1
                      FROM pg_catalog.aclexplode(d.datacl) acl
                      WHERE acl.is_grantable
                         OR acl.grantor <> d.datdba
                         OR NOT (
                              (acl.grantee = 0
                               AND acl.privilege_type = 'CONNECT')
                           OR (acl.grantee = d.datdba
                               AND acl.privilege_type IN
                                   ('CONNECT', 'CREATE', 'TEMPORARY'))
                         )
                    )
                  ELSE d.datacl IS NULL
                -- database comments are deliberately not compared: this
                -- postgres:18-alpine build initializes pg_database with NULL
                -- comments, and comment text carries no security property.
                END AS stock_acl
            FROM pg_catalog.pg_database d
            JOIN pg_catalog.pg_tablespace tablespace
              ON tablespace.oid = d.dattablespace
            ORDER BY d.datname
            """
        ).fetchall()
        role_rows = connection.execute(
            """
            SELECT
                rolname,
                rolsuper,
                rolinherit,
                rolcreaterole,
                rolcreatedb,
                rolcanlogin,
                rolreplication,
                rolbypassrls,
                rolconnlimit,
                rolpassword LIKE 'SCRAM-SHA-256$%',
                rolvaliduntil IS NULL,
                NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_db_role_setting settings
                    WHERE settings.setrole = authid.oid
                ),
                pg_catalog.shobj_description(
                    authid.oid, 'pg_catalog.pg_authid'
                ) IS NULL
            FROM pg_catalog.pg_authid authid
            WHERE authid.rolname !~ '^pg_'
            ORDER BY authid.rolname
            """
        ).fetchall()
        predefined_role_rows = connection.execute(
            """
            SELECT
                rolname,
                rolsuper,
                rolinherit,
                rolcreaterole,
                rolcreatedb,
                rolcanlogin,
                rolreplication,
                rolbypassrls,
                rolconnlimit,
                rolpassword IS NULL,
                rolvaliduntil IS NULL,
                NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_db_role_setting settings
                    WHERE settings.setrole = authid.oid
                ),
                pg_catalog.shobj_description(
                    authid.oid, 'pg_catalog.pg_authid'
                ) IS NULL
            FROM pg_catalog.pg_authid authid
            WHERE authid.rolname ~ '^pg_'
            ORDER BY authid.rolname
            """
        ).fetchall()
        membership_rows = connection.execute(
            """
            SELECT
                granted.rolname,
                member.rolname,
                grantor.rolname,
                memberships.admin_option,
                memberships.inherit_option,
                memberships.set_option
            FROM pg_catalog.pg_auth_members memberships
            JOIN pg_catalog.pg_roles granted
              ON granted.oid = memberships.roleid
            JOIN pg_catalog.pg_roles member
              ON member.oid = memberships.member
            JOIN pg_catalog.pg_roles grantor
              ON grantor.oid = memberships.grantor
            ORDER BY granted.rolname, member.rolname, grantor.rolname
            """
        ).fetchall()
        tablespace_rows = connection.execute(
            """
            SELECT
                tablespace.spcname,
                pg_catalog.pg_get_userbyid(tablespace.spcowner),
                tablespace.spcacl IS NULL,
                tablespace.spcoptions IS NULL,
                pg_catalog.pg_tablespace_location(tablespace.oid),
                pg_catalog.shobj_description(
                    tablespace.oid, 'pg_catalog.pg_tablespace'
                ) IS NULL
            FROM pg_catalog.pg_tablespace tablespace
            ORDER BY tablespace.spcname
            """
        ).fetchall()
    except psycopg.Error as exc:
        raise BackupToolError(
            "could not prove the restore cluster is dedicated", exit_code=4
        ) from exc
    finally:
        connection.close()
    expected_databases: list[tuple[str, str, bool, bool, int, str, bool]] = [
        ("postgres", source.user, False, True, -1, "pg_default", True),
        ("template0", source.user, True, False, -1, "pg_default", True),
        ("template1", source.user, True, True, -1, "pg_default", True),
    ]
    if source.database != "postgres":
        expected_databases.append(
            (source.database, source.user, False, True, -1, "pg_default", True)
        )
    expected_databases.sort(key=lambda row: row[0])
    if databases != expected_databases:
        raise BackupToolError(
            "restore cluster database catalog differs from a fresh container",
            exit_code=2,
        )
    expected_bootstrap_role = (
        source.user,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        -1,
        True,
        True,
        True,
        True,
    )
    if role_rows != [expected_bootstrap_role]:
        raise BackupToolError(
            "restore cluster contains non-system roles or an unsafe bootstrap role",
            exit_code=2,
        )
    expected_predefined_roles = [
        (
            role,
            False,
            True,
            False,
            False,
            False,
            False,
            False,
            -1,
            True,
            True,
            True,
            True,
        )
        for role in _PG18_PREDEFINED_ROLES
    ]
    if predefined_role_rows != expected_predefined_roles:
        raise BackupToolError(
            "restore cluster predefined roles differ from PostgreSQL 18 defaults",
            exit_code=2,
        )
    expected_memberships = [
        (role, "pg_monitor", source.user, False, True, True)
        for role in _PG18_PREDEFINED_MEMBERSHIPS
    ]
    if membership_rows != expected_memberships:
        raise BackupToolError(
            "restore cluster role memberships differ from PostgreSQL 18 defaults",
            exit_code=2,
        )
    expected_tablespaces = [
        ("pg_default", source.user, True, True, "", True),
        ("pg_global", source.user, True, True, "", True),
    ]
    if tablespace_rows != expected_tablespaces:
        raise BackupToolError(
            "restore cluster tablespaces differ from a fresh container",
            exit_code=2,
        )
    if source.database != "postgres":
        require_clean_target(
            ContainerConnection(
                container=source.container,
                host=source.host,
                port=source.port,
                database="postgres",
                user=source.user,
                password=source.password,
                image_id=source.image_id,
                image_reference=source.image_reference,
            )
        )


def target_migration_heads(source: ContainerConnection) -> tuple[str, ...]:
    """Read the restored Alembic heads after archive replay."""
    connection = _connect(source)
    try:
        rows = connection.execute(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
        ).fetchall()
    except psycopg.Error as exc:
        raise BackupToolError("could not read restored Alembic heads", exit_code=7) from exc
    finally:
        connection.close()
    heads = tuple(row[0] for row in rows if len(row) == 1 and isinstance(row[0], str))
    if len(heads) != len(rows):
        raise BackupToolError("restored Alembic heads are malformed", exit_code=7)
    return heads


def snapshot_source_record(
    connection: Connection[tuple[object, ...]],
    source: ContainerConnection,
) -> SourceRecord:
    """Read source provenance inside the held repeatable-read transaction."""
    row = connection.execute(
        """
        SELECT
            (SELECT system_identifier::text FROM pg_catalog.pg_control_system()),
            current_database(),
            current_user,
            current_setting('server_version_num')::integer
        """
    ).fetchone()
    if (
        row is None
        or not isinstance(row[0], str)
        or not isinstance(row[1], str)
        or not isinstance(row[2], str)
        or isinstance(row[3], bool)
        or not isinstance(row[3], int)
    ):
        raise BackupToolError("could not read the source database identity", exit_code=4)
    migration_rows = connection.execute(
        "SELECT version_num FROM public.alembic_version ORDER BY version_num"
    ).fetchall()
    migration_heads = tuple(
        row[0] for row in migration_rows if len(row) == 1 and isinstance(row[0], str)
    )
    if len(migration_heads) != len(migration_rows) or len(migration_heads) != 1:
        raise BackupToolError(
            "source must have exactly one readable Alembic head before backup",
            exit_code=4,
        )
    return SourceRecord(
        identity=DatabaseIdentity(system_identifier=row[0], database=row[1]),
        server_version_num=row[3],
        image_id=source.image_id,
        image_reference=source.image_reference,
        user=row[2],
        locale=_database_locale(connection),
        migration_heads=migration_heads,
    )


@contextmanager
def exported_snapshot(
    source: ContainerConnection,
) -> Iterator[tuple[Connection[tuple[object, ...]], str]]:
    """Hold a quiescent, non-mutating locked snapshot through pg_dump."""
    connection = _connect(source)
    try:
        connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        _lock_export_relations(connection)
        # FIX: Sequence operations are non-MVCC and cannot be fenced by the
        # exported snapshot. The mandatory writer stop is checked before the
        # snapshot is exported; backup.py checks it again after pg_dump.
        require_source_quiescent(connection)
        require_no_foreign_tables(connection)
        row = connection.execute("SELECT pg_export_snapshot()").fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise BackupToolError("PostgreSQL did not export a snapshot", exit_code=4)
        yield connection, row[0]
        connection.rollback()
    except psycopg.Error as exc:
        connection.rollback()
        raise BackupToolError("PostgreSQL snapshot operation failed", exit_code=4) from exc
    finally:
        connection.close()


def dump_snapshot(
    runner: CommandRunner,
    source: ContainerConnection,
    snapshot_id: str,
    destination: Path,
) -> None:
    """Stream a custom-format pg_dump for the exported snapshot to the host."""
    runner.binary_to_file(
        [
            "docker",
            "exec",
            source.container,
            "pg_dump",
            "--format=custom",
            "--compress=6",
            "--no-owner",
            "--no-password",
            f"--snapshot={snapshot_id}",
            f"--username={source.user}",
            f"--dbname={source.database}",
        ],
        destination,
    )


def dump_toc_entries(runner: CommandRunner, container: str, dump_source: Path | BinaryIO) -> int:
    """Prove the archive is readable and return its non-comment TOC size."""
    argv = ["docker", "exec", "-i", container, "pg_restore", "--list"]
    if isinstance(dump_source, Path):
        listing = runner.file_to_text(argv, dump_source, exit_code=6)
    else:
        listing = runner.stream_to_text(argv, dump_source, exit_code=6)
    entries = sum(1 for line in listing.splitlines() if line.strip() and not line.startswith(";"))
    if entries < 1:
        raise BackupToolError("pg_restore listed no archive entries", exit_code=6)
    return entries


def wait_for_postgres(
    runner: CommandRunner,
    *,
    source: ContainerConnection,
    wait_seconds: int,
) -> None:
    """Wait for both in-container readiness and the published host endpoint."""
    deadline = time.monotonic() + max(1, wait_seconds)
    while True:
        try:
            runner.text(
                [
                    "docker",
                    "exec",
                    source.container,
                    "pg_isready",
                    "--quiet",
                    f"--username={source.user}",
                    f"--dbname={source.database}",
                ],
                exit_code=4,
            )
            connection = _connect(source)
            connection.close()
            return
        except BackupToolError:
            if time.monotonic() >= deadline:
                raise BackupToolError("target PostgreSQL did not become ready", exit_code=4)
            time.sleep(0.5)


def apply_sql_file(
    runner: CommandRunner,
    *,
    container: str,
    user: str,
    database: str,
    source: Path | BinaryIO,
) -> None:
    """Apply one prevalidated SQL path or pinned stream through psql stdin."""
    argv = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "--no-psqlrc",
        "--no-password",
        "--quiet",
        "--tuples-only",
        "--no-align",
        "--set=ON_ERROR_STOP=1",
        f"--username={user}",
        f"--dbname={database}",
    ]
    if isinstance(source, Path):
        runner.file_to_text(argv, source, exit_code=6)
    else:
        runner.stream_to_text(argv, source, exit_code=6)


def restore_dump(
    runner: CommandRunner,
    *,
    container: str,
    user: str,
    database: str,
    source: Path | BinaryIO,
) -> None:
    """Restore one archive atomically into an already-proven clean database."""
    argv = [
        "docker",
        "exec",
        "-i",
        container,
        "pg_restore",
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        "--no-password",
        f"--username={user}",
        f"--dbname={database}",
    ]
    if isinstance(source, Path):
        runner.file_input(argv, source, exit_code=6)
    else:
        runner.stream_to_text(argv, source, exit_code=6)


def fsync_file(path: Path) -> None:
    """Flush one artifact's bytes before its directory can be published."""
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
