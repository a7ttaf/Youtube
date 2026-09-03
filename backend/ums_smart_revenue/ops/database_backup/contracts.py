"""Strict on-disk contract for one PostgreSQL backup."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util.exc import CommandError

BACKUP_SCHEMA = "ums-database-backup/v2"
DUMP_NAME = "database.dump"
ROLES_NAME = "roles.sql"
MANIFEST_NAME = "database-manifest.json"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MINIMUM_SECURITY_REVISION = "20260825_0002"

# These are table identities, not row-count assumptions. The observed floor is
# measured from the exported snapshot and written into every manifest.
SEED_TABLES = frozenset(
    {
        "public.alembic_version",
        "public.currencies",
        "public.tenants",
        "public.roles",
        "public.permissions",
        "public.role_permission_assignments",
    }
)


class BackupToolError(RuntimeError):
    """Typed, operator-safe backup or restore failure."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        """Bind one operator-safe message to its process exit code.

        Args:
            message: Safe, operator-facing failure description; never
                includes secrets, SQL values, or infrastructure detail.
            exit_code: Process status the CLI reports for this failure.

        Returns:
            ``None``.
        """
        super().__init__(message)
        self.exit_code = exit_code


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 digest of one regular file.

    Args:
        path: Path. Filesystem path the operation reads or writes.

    Returns:
        Lowercase SHA-256 hex digest of the file.

    Raises:
        OSError: propagated when the file cannot be opened, read, or hashed.
    """
    with path.open("rb") as stream:
        return sha256_stream(stream)


def sha256_stream(stream: BinaryIO) -> str:
    """Hash one pinned binary stream and rewind it for its next consumer.

    Args:
        stream: BinaryIO. Pinned binary stream consumed as input.

    Returns:
        Lowercase SHA-256 hex digest of the stream.

    Raises:
        OSError: propagated when the stream cannot be read or hashed.
    """
    digest = hashlib.sha256()
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _require_exact_keys(body: dict[str, Any], expected: set[str], label: str) -> None:
    """Require a payload to carry exactly the expected fields."""
    actual = set(body)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise BackupToolError(
            f"{label} has the wrong fields (missing={missing}, extra={extra})",
            exit_code=8,
        )


def _require_string(value: object, label: str) -> str:
    """Require a non-empty string field."""
    if not isinstance(value, str) or not value:
        raise BackupToolError(f"{label} must be a non-empty string", exit_code=8)
    return value


def _require_integer(value: object, label: str, *, minimum: int = 0) -> int:
    """Require a strict integer field with an optional lower bound."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BackupToolError(
            f"{label} must be an integer >= {minimum}",
            exit_code=8,
        )
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while refusing ambiguous repeated field names."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupToolError(
                f"database manifest contains a duplicate {key!r} field",
                exit_code=8,
            )
        result[key] = value
    return result


# ============================================================================
# Purpose: Prove a backup head descends from the irreversible P0-c authorization
#   repair, using the repository's real Alembic graph instead of revision text.
# Database/ORM: Alembic revision metadata only; no database connection or write.
# Standards: Exactly one known head, explicit security floor, typed fail-closed
#   errors, and no downgrade inference from lexicographic revision identifiers.
# Blast Radius: Authorization recovery; pre-floor backups cannot be rehearsed or
#   restored as if the beta-operator permission repair were present.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#       20260825_0002_beta_operator_authorization_repair.py -> security floor.
#   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> recovery contract.
# ============================================================================
def require_migration_security_floor(
    repository_root: Path, migration_heads: tuple[str, ...]
) -> None:
    """Refuse a schema whose Alembic lineage does not include the security floor.

    Args:
        repository_root: Path. Checkout root used to locate tracked SQL and Alembic state.
        migration_heads: tuple[str, ...].

    Returns:
        ``None``.

    Raises:
        BackupToolError: the manifest does not carry exactly one Alembic head, or that head is
            below the required security floor.
    """
    if len(migration_heads) != 1:
        raise BackupToolError("backup must contain exactly one Alembic head", exit_code=8)
    config_path = repository_root / "alembic.ini"
    script_location = repository_root / "backend" / "ums_smart_revenue" / "db" / "alembic"
    try:
        config = Config(str(config_path))
        config.set_main_option("script_location", str(script_location))
        revisions = tuple(
            ScriptDirectory.from_config(config).revision_map.iterate_revisions(
                migration_heads[0],
                MINIMUM_SECURITY_REVISION,
                inclusive=True,
            )
        )
    except (CommandError, RevisionError, OSError, KeyError) as exc:
        raise BackupToolError(
            "backup Alembic head is unknown or below the minimum security floor",
            exit_code=8,
        ) from exc
    if MINIMUM_SECURITY_REVISION not in {revision.revision for revision in revisions}:
        raise BackupToolError(
            "backup Alembic head is below the minimum security floor",
            exit_code=8,
        )


@dataclass(frozen=True)
class ArtifactRecord:
    """Integrity record for one immutable backup artifact."""

    name: str
    bytes: int
    sha256: str

    @classmethod
    def from_json(cls, body: object, *, label: str) -> ArtifactRecord:
        """Parse and validate an artifact record from a manifest payload.

        Args:
            body: object.
            label: str. Manifest field label used in validation errors.

        Returns:
            ``ArtifactRecord``.

        Raises:
            BackupToolError: the node violates the strict ``ums-database-backup/v2`` shape (wrong
                types, missing or unexpected keys, non-canonical values).
        """
        if not isinstance(body, dict):
            raise BackupToolError(f"{label} must be an object", exit_code=8)
        _require_exact_keys(body, {"name", "bytes", "sha256"}, label)
        name = _require_string(body["name"], f"{label}.name")
        size = _require_integer(body["bytes"], f"{label}.bytes", minimum=1)
        digest = _require_string(body["sha256"], f"{label}.sha256")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise BackupToolError(f"{label}.sha256 is not lowercase SHA-256", exit_code=8)
        return cls(name=name, bytes=size, sha256=digest)

    def to_json(self) -> dict[str, object]:
        """Return the lossless JSON representation of this artifact.

        Returns:
            ``dict[str, object]``.
        """
        return {"name": self.name, "bytes": self.bytes, "sha256": self.sha256}


@dataclass(frozen=True)
class DatabaseIdentity:
    """Stable source identity used to prevent mixing unrelated databases."""

    system_identifier: str
    database: str

    @classmethod
    def from_json(cls, body: object) -> DatabaseIdentity:
        """Parse and validate the database identity from a manifest payload.

        Args:
            body: object.

        Returns:
            ``DatabaseIdentity``.

        Raises:
            BackupToolError: the node violates the strict ``ums-database-backup/v2`` shape (wrong
                types, missing or unexpected keys, non-canonical values).
        """
        if not isinstance(body, dict):
            raise BackupToolError("source.identity must be an object", exit_code=8)
        _require_exact_keys(body, {"system_identifier", "database"}, "source.identity")
        system_identifier = _require_string(
            body["system_identifier"], "source.identity.system_identifier"
        )
        if not system_identifier.isdecimal():
            raise BackupToolError(
                "source.identity.system_identifier must contain decimal digits",
                exit_code=8,
            )
        return cls(
            system_identifier=system_identifier,
            database=_require_string(body["database"], "source.identity.database"),
        )

    def to_json(self) -> dict[str, str]:
        """Return the lossless JSON representation of this cluster identity.

        Returns:
            ``dict[str, str]``.
        """
        return {
            "system_identifier": self.system_identifier,
            "database": self.database,
        }


@dataclass(frozen=True)
class DatabaseLocale:
    """Database encoding and collation contract that restore must preserve."""

    encoding: str
    collate: str
    ctype: str
    provider: str
    locale: str
    icu_rules: str
    collation_version: str

    @classmethod
    def from_json(cls, body: object) -> DatabaseLocale:
        """Parse and validate the cluster locale from a manifest payload.

        Args:
            body: object.

        Returns:
            ``DatabaseLocale``.

        Raises:
            BackupToolError: the node violates the strict ``ums-database-backup/v2`` shape (wrong
                types, missing or unexpected keys, non-canonical values).
        """
        if not isinstance(body, dict):
            raise BackupToolError("source.locale must be an object", exit_code=8)
        expected = {
            "encoding",
            "collate",
            "ctype",
            "provider",
            "locale",
            "icu_rules",
            "collation_version",
        }
        _require_exact_keys(body, expected, "source.locale")
        values = {key: body[key] if isinstance(body[key], str) else None for key in expected}
        if any(value is None for value in values.values()):
            raise BackupToolError("source.locale fields must be strings", exit_code=8)
        assert all(isinstance(value, str) for value in values.values())
        if not values["encoding"]:
            raise BackupToolError("source.locale.encoding must not be empty", exit_code=8)
        return cls(**values)  # type: ignore[arg-type]

    def to_json(self) -> dict[str, str]:
        """Return the lossless JSON representation of this locale.

        Returns:
            ``dict[str, str]``.
        """
        return {
            "encoding": self.encoding,
            "collate": self.collate,
            "ctype": self.ctype,
            "provider": self.provider,
            "locale": self.locale,
            "icu_rules": self.icu_rules,
            "collation_version": self.collation_version,
        }


@dataclass(frozen=True)
class SourceRecord:
    """Non-secret provenance needed to select and verify a restore target."""

    identity: DatabaseIdentity
    server_version_num: int
    image_id: str
    image_reference: str
    user: str
    locale: DatabaseLocale
    migration_heads: tuple[str, ...]

    @classmethod
    def from_json(cls, body: object) -> SourceRecord:
        """Parse and validate the full source description from a payload.

        Args:
            body: object.

        Returns:
            ``SourceRecord``.

        Raises:
            BackupToolError: the node violates the strict ``ums-database-backup/v2`` shape (wrong
                types, missing or unexpected keys, non-canonical values).
        """
        if not isinstance(body, dict):
            raise BackupToolError("source must be an object", exit_code=8)
        expected = {
            "identity",
            "server_version_num",
            "image_id",
            "image_reference",
            "user",
            "locale",
            "migration_heads",
        }
        _require_exact_keys(body, expected, "source")
        raw_heads = body["migration_heads"]
        if not isinstance(raw_heads, list) or len(raw_heads) != 1:
            raise BackupToolError(
                "source.migration_heads must contain exactly one head", exit_code=8
            )
        heads = tuple(_require_string(value, "source.migration_heads[]") for value in raw_heads)
        if len(set(heads)) != len(heads):
            raise BackupToolError("source.migration_heads contains duplicates", exit_code=8)
        image_id = _require_string(body["image_id"], "source.image_id")
        digest = image_id.removeprefix("sha256:")
        if (
            not image_id.startswith("sha256:")
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise BackupToolError("source.image_id must be an immutable SHA-256 id", exit_code=8)
        return cls(
            identity=DatabaseIdentity.from_json(body["identity"]),
            server_version_num=_require_integer(
                body["server_version_num"], "source.server_version_num", minimum=1
            ),
            image_id=image_id,
            image_reference=_require_string(body["image_reference"], "source.image_reference"),
            user=_require_string(body["user"], "source.user"),
            locale=DatabaseLocale.from_json(body["locale"]),
            migration_heads=heads,
        )

    def to_json(self) -> dict[str, object]:
        """Return the lossless JSON representation of this source.

        Returns:
            ``dict[str, object]``.
        """
        return {
            "identity": self.identity.to_json(),
            "server_version_num": self.server_version_num,
            "image_id": self.image_id,
            "image_reference": self.image_reference,
            "user": self.user,
            "locale": self.locale.to_json(),
            "migration_heads": list(self.migration_heads),
        }


@dataclass(frozen=True)
class TableRecord:
    """Snapshot row count for one restored PostgreSQL table."""

    schema: str
    name: str
    rows: int

    @property
    def qualified_name(self) -> str:
        """Return the ``schema.name`` form used in verification errors."""
        return f"{self.schema}.{self.name}"

    @classmethod
    def from_json(cls, body: object, *, index: int) -> TableRecord:
        """Parse and validate one table record from the manifest payload.

        Args:
            body: object.
            index: int.

        Returns:
            ``TableRecord``.

        Raises:
            BackupToolError: the node violates the strict ``ums-database-backup/v2`` shape (wrong
                types, missing or unexpected keys, non-canonical values).
        """
        label = f"tables[{index}]"
        if not isinstance(body, dict):
            raise BackupToolError(f"{label} must be an object", exit_code=8)
        _require_exact_keys(body, {"schema", "name", "rows"}, label)
        return cls(
            schema=_require_string(body["schema"], f"{label}.schema"),
            name=_require_string(body["name"], f"{label}.name"),
            rows=_require_integer(body["rows"], f"{label}.rows"),
        )

    def to_json(self) -> dict[str, object]:
        """Return the lossless JSON representation of this table record.

        Returns:
            ``dict[str, object]``.
        """
        return {"schema": self.schema, "name": self.name, "rows": self.rows}


@dataclass(frozen=True)
class SequenceRecord:
    """Identity, parameters, and non-MVCC state for one PostgreSQL sequence."""

    schema: str
    name: str
    data_type: str
    start_value: int
    increment_by: int
    min_value: int
    max_value: int
    cache_size: int
    cycle: bool
    last_value: int
    is_called: bool

    @property
    def qualified_name(self) -> str:
        """Return the ``schema.name`` form used in verification errors."""
        return f"{self.schema}.{self.name}"

    @classmethod
    def from_json(cls, body: object, *, index: int) -> SequenceRecord:
        """Parse and validate one sequence record from the manifest payload.

        Args:
            body: object.
            index: int.

        Returns:
            ``SequenceRecord``.

        Raises:
            BackupToolError: the node violates the strict ``ums-database-backup/v2`` shape (wrong
                types, missing or unexpected keys, non-canonical values).
        """
        label = f"sequences[{index}]"
        if not isinstance(body, dict):
            raise BackupToolError(f"{label} must be an object", exit_code=8)
        fields = {
            "schema",
            "name",
            "data_type",
            "start_value",
            "increment_by",
            "min_value",
            "max_value",
            "cache_size",
            "cycle",
            "last_value",
            "is_called",
        }
        _require_exact_keys(body, fields, label)
        if not isinstance(body["cycle"], bool) or not isinstance(body["is_called"], bool):
            raise BackupToolError(f"{label} boolean fields must be booleans", exit_code=8)

        def integer(field: str, *, minimum: int | None = None) -> int:
            """Read a strict integer field from the payload being validated.

            Args:
                field: str. Manifest field name used in validation errors.
                minimum: int | None. Smallest value the integer field may hold.

            Returns:
                The validated integer value.

            Raises:
                BackupToolError: the field is not an integer or is below its required minimum.
            """
            value = body[field]
            if isinstance(value, bool) or not isinstance(value, int):
                raise BackupToolError(f"{label}.{field} must be an integer", exit_code=8)
            if minimum is not None and value < minimum:
                raise BackupToolError(
                    f"{label}.{field} must be an integer >= {minimum}", exit_code=8
                )
            return value

        return cls(
            schema=_require_string(body["schema"], f"{label}.schema"),
            name=_require_string(body["name"], f"{label}.name"),
            data_type=_require_string(body["data_type"], f"{label}.data_type"),
            start_value=integer("start_value"),
            increment_by=integer("increment_by"),
            min_value=integer("min_value"),
            max_value=integer("max_value"),
            cache_size=integer("cache_size", minimum=1),
            cycle=body["cycle"],
            last_value=integer("last_value"),
            is_called=body["is_called"],
        )

    def to_json(self) -> dict[str, object]:
        """Return the lossless JSON representation of this manifest.

        Returns:
            ``dict[str, object]``.
        """
        return {
            "schema": self.schema,
            "name": self.name,
            "data_type": self.data_type,
            "start_value": self.start_value,
            "increment_by": self.increment_by,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "cache_size": self.cache_size,
            "cycle": self.cycle,
            "last_value": self.last_value,
            "is_called": self.is_called,
        }


@dataclass(frozen=True)
class BackupManifest:
    """Complete, validated semantic description of one database dump."""

    created_at: str
    source: SourceRecord
    tables: tuple[TableRecord, ...]
    sequences: tuple[SequenceRecord, ...]
    authorization_catalog_sha256: str
    seed_floor: dict[str, int]
    artifacts: tuple[ArtifactRecord, ...]
    dump_toc_entries: int

    # ============================================================================
    # Purpose: Parse an untrusted on-disk manifest without coercing JSON shapes.
    # Database/ORM: None; validates the restore boundary before database mutation.
    # Standards: Exact-key schema, strict scalar types, unique sorted identities.
    # Blast Radius: Restore safety; malformed or ambiguous manifests fail closed.
    # Connections:
    #   - File: backend/ums_smart_revenue/ops/database_backup/restore.py -> load gate.
    #   - File: Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md -> manifest contract.
    # ============================================================================
    @classmethod
    def from_json(cls, body: object) -> BackupManifest:
        """Parse and strictly validate a complete backup manifest payload.

        Args:
            body: object.

        Returns:
            ``BackupManifest``.

        Raises:
            BackupToolError: the node violates the strict ``ums-database-backup/v2`` shape (wrong
                types, missing or unexpected keys, non-canonical values).
        """
        if not isinstance(body, dict):
            raise BackupToolError("database manifest must be an object", exit_code=8)
        expected = {
            "schema",
            "status",
            "created_at",
            "source",
            "tables",
            "sequences",
            "authorization_catalog_sha256",
            "seed_floor",
            "artifacts",
            "dump_toc_entries",
        }
        _require_exact_keys(body, expected, "database manifest")
        if body["schema"] != BACKUP_SCHEMA or body["status"] != "complete":
            raise BackupToolError("database manifest schema/status is not restorable", exit_code=8)

        raw_tables = body["tables"]
        if not isinstance(raw_tables, list) or not raw_tables:
            raise BackupToolError("tables must be a non-empty list", exit_code=8)
        tables = tuple(
            TableRecord.from_json(row, index=index) for index, row in enumerate(raw_tables)
        )
        table_names = [record.qualified_name for record in tables]
        if table_names != sorted(table_names) or len(set(table_names)) != len(table_names):
            raise BackupToolError("tables must be sorted and unique", exit_code=8)

        raw_sequences = body["sequences"]
        if not isinstance(raw_sequences, list):
            raise BackupToolError("sequences must be a list", exit_code=8)
        sequences = tuple(
            SequenceRecord.from_json(row, index=index) for index, row in enumerate(raw_sequences)
        )
        sequence_names = [record.qualified_name for record in sequences]
        if sequence_names != sorted(sequence_names) or len(set(sequence_names)) != len(
            sequence_names
        ):
            raise BackupToolError("sequences must be sorted and unique", exit_code=8)

        authorization_digest = _require_string(
            body["authorization_catalog_sha256"], "authorization_catalog_sha256"
        )
        if len(authorization_digest) != 64 or any(
            character not in "0123456789abcdef" for character in authorization_digest
        ):
            raise BackupToolError(
                "authorization_catalog_sha256 is not lowercase SHA-256", exit_code=8
            )

        raw_floor = body["seed_floor"]
        if not isinstance(raw_floor, dict) or not raw_floor:
            raise BackupToolError("seed_floor must be a non-empty object", exit_code=8)
        if any(not isinstance(key, str) or "." not in key or not key for key in raw_floor):
            raise BackupToolError("seed_floor keys must be qualified table names", exit_code=8)
        seed_floor = {
            key: _require_integer(value, f"seed_floor.{key}", minimum=1)
            for key, value in raw_floor.items()
        }
        missing_security_floor = sorted(SEED_TABLES - set(seed_floor))
        extra_security_floor = sorted(set(seed_floor) - SEED_TABLES)
        if missing_security_floor or extra_security_floor:
            raise BackupToolError(
                "seed_floor table identities do not match the required security floor "
                f"(missing={missing_security_floor}, extra={extra_security_floor})",
                exit_code=8,
            )
        observed = {record.qualified_name: record.rows for record in tables}
        if any(observed.get(key) != value for key, value in seed_floor.items()):
            raise BackupToolError(
                "seed_floor does not match the snapshot table counts", exit_code=8
            )

        raw_artifacts = body["artifacts"]
        if not isinstance(raw_artifacts, list):
            raise BackupToolError("artifacts must be a list", exit_code=8)
        artifacts = tuple(
            ArtifactRecord.from_json(record, label=f"artifacts[{index}]")
            for index, record in enumerate(raw_artifacts)
        )
        names = [record.name for record in artifacts]
        if names != [DUMP_NAME, ROLES_NAME]:
            raise BackupToolError(
                f"artifacts must be exactly [{DUMP_NAME!r}, {ROLES_NAME!r}]",
                exit_code=8,
            )
        return cls(
            created_at=_require_string(body["created_at"], "created_at"),
            source=SourceRecord.from_json(body["source"]),
            tables=tables,
            sequences=sequences,
            authorization_catalog_sha256=authorization_digest,
            seed_floor=seed_floor,
            artifacts=artifacts,
            dump_toc_entries=_require_integer(
                body["dump_toc_entries"], "dump_toc_entries", minimum=1
            ),
        )

    @classmethod
    def load(cls, path: Path) -> BackupManifest:
        """Load a manifest from disk, refusing duplicate JSON keys.

        Args:
            path: Path. Filesystem path the operation reads or writes.

        Returns:
            ``BackupManifest``.

        Raises:
            BackupToolError: the manifest exceeds its size limit, cannot be read, or is not a strict
                ``ums-database-backup/v2`` document.
        """
        try:
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                raise BackupToolError(f"{path.name} exceeds the manifest size limit", exit_code=8)
            raw = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except BackupToolError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise BackupToolError(f"cannot read {path.name}: {exc}", exit_code=8) from exc
        return cls.from_json(raw)

    def to_json(self) -> dict[str, object]:
        """Return the lossless JSON representation of this manifest.

        Returns:
            ``dict[str, object]``.
        """
        return {
            "schema": BACKUP_SCHEMA,
            "status": "complete",
            "created_at": self.created_at,
            "source": self.source.to_json(),
            "tables": [record.to_json() for record in self.tables],
            "sequences": [record.to_json() for record in self.sequences],
            "authorization_catalog_sha256": self.authorization_catalog_sha256,
            "seed_floor": dict(sorted(self.seed_floor.items())),
            "artifacts": [record.to_json() for record in self.artifacts],
            "dump_toc_entries": self.dump_toc_entries,
        }


def verify_artifacts(directory: Path, manifest: BackupManifest) -> None:
    """Verify both required artifacts before any restore-side database write.

    Args:
        directory: Path. Host directory the operation validates or writes.
        manifest: BackupManifest. Parsed backup manifest validated or written by this call.

    Returns:
        ``None``.

    Raises:
        BackupToolError: a manifest artifact is missing, is not a regular file, or fails its
            recorded size/SHA-256 validation.
    """
    for record in manifest.artifacts:
        path = directory / record.name
        if not path.is_file() or path.is_symlink():
            raise BackupToolError(
                f"backup artifact {record.name} is not a regular file", exit_code=8
            )
        if path.stat().st_size != record.bytes or sha256_file(path) != record.sha256:
            raise BackupToolError(
                f"backup artifact {record.name} failed integrity validation", exit_code=8
            )


def _is_redirect_status(status: os.stat_result) -> bool:
    """Report whether a stat result denotes a symlink or reparse point."""
    attributes = getattr(status, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(status.st_mode) or bool(attributes & reparse)


# ============================================================================
# Purpose: Pin both verified backup artifacts to their already-open filesystem
#   identities so later restore consumers never reopen attacker-swappable paths.
# Database/ORM: None; this is the restore-side local artifact trust boundary.
# Standards: lstat/fstat identity binding, regular-file and reparse refusal,
#   exact byte-size/SHA-256 proof, typed failures, and deterministic cleanup.
# Blast Radius: Restore safety; unverified role SQL or dump bytes cannot be read
#   through a post-verification path replacement.
# Connections:
#   - File: backend/ums_smart_revenue/ops/database_backup/restore.py -> lifetime.
#   - File: backend/ums_smart_revenue/ops/database_backup/postgres.py -> consumers.
# ============================================================================
@contextmanager
def open_verified_artifacts(
    directory: Path, manifest: BackupManifest
) -> Iterator[dict[str, BinaryIO]]:
    """Yield pinned, rewound artifact streams after strict integrity proof.

    Args:
        directory: Path. Host directory the operation validates or writes.
        manifest: BackupManifest. Parsed backup manifest validated or written by this call.

    Returns:
        ``Iterator[dict[str, BinaryIO]]``.

    Raises:
        BackupToolError: a manifest artifact is missing, is not a regular file, fails its
            size/SHA-256 validation, or changed between open and verification.
    """
    streams: dict[str, BinaryIO] = {}
    with ExitStack() as stack:
        for record in manifest.artifacts:
            path = directory / record.name
            try:
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode) or _is_redirect_status(before):
                    raise BackupToolError(
                        f"backup artifact {record.name} is not a regular file",
                        exit_code=8,
                    )
                stream = stack.enter_context(path.open("rb"))
                opened = os.fstat(stream.fileno())
                after = path.lstat()
            except BackupToolError:
                raise
            except OSError as exc:
                raise BackupToolError(
                    f"backup artifact {record.name} could not be pinned",
                    exit_code=8,
                ) from exc
            before_identity = (before.st_dev, before.st_ino)
            if (
                before_identity != (opened.st_dev, opened.st_ino)
                or before_identity != (after.st_dev, after.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(after.st_mode)
                or _is_redirect_status(after)
            ):
                raise BackupToolError(
                    f"backup artifact {record.name} changed while it was opened",
                    exit_code=8,
                )
            if opened.st_size != record.bytes or sha256_stream(stream) != record.sha256:
                raise BackupToolError(
                    f"backup artifact {record.name} failed integrity validation",
                    exit_code=8,
                )
            streams[record.name] = stream
        yield streams
