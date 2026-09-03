"""Boundary tests for Docker/PostgreSQL database-backup adapters."""

from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import psycopg
import pytest

from ums_smart_revenue.ops.database_backup import postgres
from ums_smart_revenue.ops.database_backup.contracts import BackupToolError
from ums_smart_revenue.ops.database_backup.semantic import (
    authorization_catalog_digest,
    canonical_authorization_payload,
)


def _exactly_one(items):
    """Return the one expected item; a dry iterator fails the test."""
    try:
        return next(items)
    except StopIteration as exc:
        raise AssertionError("expected one more canned item; got none") from exc


class _TextRunner:
    """Command runner stub returning canned text output."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[tuple[list[str], str | None, int]] = []
        self.environments: list[Mapping[str, str] | None] = []

    def text(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        environment: Mapping[str, str] | None = None,
        exit_code: int = 5,
    ) -> str:
        """Record argv and return canned standard output."""
        self.calls.append((argv, stdin, exit_code))
        self.environments.append(environment)
        return self.replies.pop(0) if self.replies else ""


def _container_inspect(*, host_ip: str | tuple[str, ...] = "127.0.0.1") -> str:
    """Build a docker inspect payload for a source container."""
    host_ips = (host_ip,) if isinstance(host_ip, str) else host_ip
    return json.dumps(
        [
            {
                "Id": "c" * 64,
                "Path": "docker-entrypoint.sh",
                "Args": ["postgres"],
                "Config": {
                    "Env": [
                        "POSTGRES_DB=ums",
                        "POSTGRES_USER=postgres",
                        "POSTGRES_PASSWORD=do-not-print",
                    ],
                    "Image": "postgres:18-alpine@sha256:" + "b" * 64,
                },
                "Image": "sha256:" + "a" * 64,
                "NetworkSettings": {
                    "Ports": {
                        "5432/tcp": [
                            {"HostIp": binding, "HostPort": "55432"} for binding in host_ips
                        ]
                    }
                },
            }
        ]
    )


def _image_inspect(*, image_id: str | None = None, postgres_image: bool = True) -> str:
    """Build a docker image inspect payload."""
    environment = ["PG_MAJOR=18"] if postgres_image else ["APP=other"]
    entrypoint = ["docker-entrypoint.sh"] if postgres_image else ["other"]
    command = ["postgres"] if postgres_image else ["other"]
    return json.dumps(
        [
            {
                "Id": image_id or "sha256:" + "a" * 64,
                "Config": {
                    "Env": environment,
                    "Entrypoint": entrypoint,
                    "Cmd": command,
                },
            }
        ]
    )


def test_container_resolution_keeps_password_internal_and_requires_loopback() -> None:
    """Container resolution keeps the password internal and needs loopback."""
    runner = _TextRunner([_container_inspect()])
    source = postgres.resolve_container_connection(runner, "ums-postgres")  # type: ignore[arg-type]
    assert source.host == "127.0.0.1"
    assert source.port == 55432
    assert source.container == "c" * 64
    assert source.password == "do-not-print"
    assert "do-not-print" not in repr(runner.calls)

    runner = _TextRunner([_container_inspect(host_ip="0.0.0.0")])
    with pytest.raises(BackupToolError, match="127.0.0.1"):
        postgres.resolve_container_connection(runner, "ums-postgres")  # type: ignore[arg-type]

    malformed = json.loads(_container_inspect())
    malformed[0]["NetworkSettings"]["Ports"]["5432/tcp"].append(None)
    runner = _TextRunner([json.dumps(malformed)])
    with pytest.raises(BackupToolError, match="malformed"):
        postgres.resolve_container_connection(runner, "ums-postgres")  # type: ignore[arg-type]

    invalid_image = json.loads(_container_inspect())
    invalid_image[0]["Image"] = "sha256:short"
    runner = _TextRunner([json.dumps(invalid_image)])
    with pytest.raises(BackupToolError, match="identified by SHA-256"):
        postgres.resolve_container_connection(runner, "ums-postgres")  # type: ignore[arg-type]

    runner = _TextRunner([_container_inspect(host_ip=("127.0.0.1", "0.0.0.0"))])
    with pytest.raises(BackupToolError, match="127.0.0.1"):
        postgres.resolve_container_connection(runner, "ums-postgres")  # type: ignore[arg-type]


@pytest.mark.parametrize("container", ["", "unsafe/name", "name\nother", " name"])
def test_container_resolution_rejects_unsafe_names(container: str) -> None:
    """Container resolution rejects unsafe container names."""
    with pytest.raises(BackupToolError, match="unsupported characters"):
        postgres.resolve_container_connection(_TextRunner(), container)  # type: ignore[arg-type]


def test_container_resolution_rejects_postgres_command_overrides() -> None:
    """Container resolution rejects Postgres command overrides."""
    body = json.loads(_container_inspect())
    body[0]["Args"] = ["postgres", "-c", "session_replication_role=replica"]
    with pytest.raises(BackupToolError, match="official image default"):
        postgres.resolve_container_connection(  # type: ignore[arg-type]
            _TextRunner([json.dumps(body)]), "ums-postgres"
        )


def test_native_command_failure_never_echoes_argv_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native command failure never echoes the secret-bearing argv."""

    def _failed(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return a completed process with a failing exit code."""
        return subprocess.CompletedProcess([], 1, "", "safe failure")

    monkeypatch.setattr(subprocess, "run", _failed)
    runner = postgres.CommandRunner(timeout_seconds=1)
    with pytest.raises(BackupToolError, match="safe failure") as captured:
        runner.text(["tool", "--password=operator-secret"])
    assert "operator-secret" not in str(captured.value)


def test_rehearsal_image_requires_operator_reference_to_match_immutable_id() -> None:
    """Rehearsal image resolution binds the reference to the immutable id."""
    expected = "sha256:" + "a" * 64
    accepted = postgres.resolve_rehearsal_image(
        _TextRunner([_image_inspect()]),  # type: ignore[arg-type]
        operator_reference="postgres:18-alpine@sha256:operator",
        expected_image_id=expected,
    )
    assert accepted == expected

    with pytest.raises(BackupToolError, match="does not match"):
        postgres.resolve_rehearsal_image(
            _TextRunner([_image_inspect(image_id="sha256:" + "c" * 64)]),  # type: ignore[arg-type]
            operator_reference="postgres:18-alpine@sha256:wrong",
            expected_image_id=expected,
        )


def test_rehearsal_image_rejects_non_postgres_config() -> None:
    """Rehearsal image resolution rejects a non-Postgres image config."""
    with pytest.raises(BackupToolError, match="not a PostgreSQL image"):
        postgres.resolve_rehearsal_image(
            _TextRunner([_image_inspect(postgres_image=False)]),  # type: ignore[arg-type]
            operator_reference="untrusted:latest",
            expected_image_id="sha256:" + "a" * 64,
        )


def test_rehearsal_container_creation_uses_direct_argv_and_loopback() -> None:
    """Rehearsal container creation uses direct argv against loopback."""
    runner = _TextRunner()
    postgres.create_rehearsal_container(
        runner,  # type: ignore[arg-type]
        image_id="sha256:" + "a" * 64,
        database="ums",
        user="postgres",
        name="ums-db-restore-rehearsal-" + "a" * 32,
        ownership_token="a" * 32,
    )
    argv, stdin, exit_code = runner.calls[0]
    assert argv[:3] == ["docker", "run", "--detach"]
    assert "127.0.0.1::5432" in argv
    assert "ums.smart-revenue.rehearsal=" + "a" * 32 in argv
    assert "POSTGRES_PASSWORD" in argv
    assert not any(value.startswith("POSTGRES_PASSWORD=") for value in argv)
    assert "POSTGRES_HOST_AUTH_METHOD=trust" not in argv
    assert runner.environments[0] is not None
    assert len(runner.environments[0]["POSTGRES_PASSWORD"]) >= 48
    assert stdin is None
    assert exit_code == 4


def test_restore_target_must_reject_a_deliberately_wrong_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore target must reject a deliberately wrong password."""
    source = postgres.ContainerConnection(
        container="target",
        host="127.0.0.1",
        port=5432,
        database="ums",
        user="postgres",
        password="correct",
        image_id="sha256:" + "a" * 64,
        image_reference="postgres:18-alpine",
    )

    class _UnexpectedConnection:
        """Connection stub that fails the test if it is ever used."""

        @staticmethod
        def close() -> None:
            """Mark the unexpected connection as closed."""
            return None

    monkeypatch.setattr(psycopg, "connect", lambda **_kwargs: _UnexpectedConnection())
    with pytest.raises(BackupToolError, match="accepted a deliberately wrong password"):
        postgres.require_password_authentication(source)

    def _reject(**_kwargs: object) -> object:
        """Fail the test if a psycopg connection is attempted."""
        raise psycopg.errors.InvalidPassword("password authentication failed")

    monkeypatch.setattr(psycopg, "connect", _reject)
    postgres.require_password_authentication(source)


class _Rows:
    """Cursor result stub returning canned rows."""

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._canned_rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return the canned rows."""
        return self._canned_rows


class _AuthorizationConnection:
    """Connection stub answering the authorization catalog probe."""

    def __init__(self, *, unsafe_edge: bool = False) -> None:
        payload = canonical_authorization_payload()
        self.roles = [tuple(row.values()) for row in payload["roles"]]
        self.permissions = [tuple(row.values()) for row in payload["permissions"]]
        self.assignments = [tuple(row.values()) for row in payload["role_permission_assignments"]]
        if unsafe_edge:
            self.assignments.append(("beta_operator", "connectors.run_jobs"))
            self.assignments.sort()

    def execute(self, query: str) -> _Rows:
        """Answer the catalog query with canned rows."""
        if "FROM public.roles" in query:
            return _Rows(self.roles)
        if "FROM public.permissions" in query:
            return _Rows(self.permissions)
        if "FROM public.role_permission_assignments" in query:
            return _Rows(self.assignments)
        raise AssertionError(query)


def test_authorization_catalog_gate_requires_exact_runtime_semantics() -> None:
    """The authorization gate requires exact runtime catalog semantics."""
    expected = authorization_catalog_digest(canonical_authorization_payload())
    assert (
        postgres.snapshot_authorization_catalog_digest(  # type: ignore[arg-type]
            _AuthorizationConnection(), require_canonical=True
        )
        == expected
    )
    with pytest.raises(BackupToolError, match="runtime registries"):
        postgres.snapshot_authorization_catalog_digest(  # type: ignore[arg-type]
            _AuthorizationConnection(unsafe_edge=True), require_canonical=True
        )


def test_rehearsal_cleanup_refuses_foreign_container_names() -> None:
    """Rehearsal cleanup refuses container names it did not create."""
    runner = _TextRunner()
    with pytest.raises(BackupToolError, match="unrecognized"):
        postgres.remove_rehearsal_container(  # type: ignore[arg-type]
            runner, "production-postgres", ownership_token="a" * 32
        )
    assert runner.calls == []


def test_rehearsal_cleanup_requires_its_ownership_label() -> None:
    """Rehearsal cleanup requires its own ownership label."""
    runner = _TextRunner(
        [
            "abc123\n",
            json.dumps(
                [
                    {
                        "Id": "c" * 64,
                        "Config": {
                            "Env": [],
                            "Labels": {"ums.smart-revenue.rehearsal": "b" * 32},
                        },
                    }
                ]
            ),
        ]
    )
    with pytest.raises(BackupToolError, match="foreign ownership"):
        postgres.remove_rehearsal_container(  # type: ignore[arg-type]
            runner,
            "ums-db-restore-rehearsal-" + "a" * 32,
            ownership_token="a" * 32,
        )
    assert not any("rm" in call[0] for call in runner.calls)


def test_clean_target_query_covers_non_table_user_objects() -> None:
    """The clean-target query covers non-table user objects."""
    query = postgres._USER_OBJECT_COUNT_SQL
    for catalog in (
        "pg_collation",
        "pg_conversion",
        "pg_operator",
        "pg_opclass",
        "pg_opfamily",
        "pg_ts_config",
        "pg_ts_dict",
        "pg_ts_parser",
        "pg_ts_template",
        "pg_statistic_ext",
        "pg_event_trigger",
        "pg_foreign_data_wrapper",
        "pg_foreign_server",
        "pg_default_acl",
        "pg_largeobject_metadata",
        "pg_publication",
        "pg_replication_slots",
        "pg_replication_origin",
        "pg_prepared_xacts",
        "pg_subscription",
        "pg_transform",
        "pg_db_role_setting",
        "pg_parameter_acl",
        "pg_seclabel",
        "pg_shseclabel",
        "pg_file_settings",
        "pg_cast",
        "pg_language",
        "pg_am",
    ):
        assert catalog in query

    # The check must stay calibrated for a genuine fresh PostgreSQL 18
    # cluster: no idealized-fingerprint arms (real-PG validation 2026-09-01
    # measured 3,956 false conditions from xmin/ACL-default/initprivs/comment
    # comparisons on an unmodified postgres:18-alpine cluster).
    for stale_arm in (
        "xmin <> '1'::xid",
        "pg_init_privs",
        "acldefault",
        "obj_description",
    ):
        assert stale_arm not in query, f"fingerprint arm reintroduced: {stale_arm}"

    assert "ext.extname <> 'plpgsql'" in query
    assert "ext.extnamespace = 'pg_catalog'::regnamespace" in query
    assert "t.typtype IN" not in query
    assert "('pg_catalog', 'information_schema', 'pg_toast', 'public')" in query

    source = inspect.getsource(postgres.require_dedicated_cluster)
    assert "pg_auth_members" in source
    assert "WHERE datallowconn" not in source
    for contract in (
        "d.datistemplate",
        "d.datallowconn",
        "d.datconnlimit",
        "pg_tablespace",
        "pg_tablespace_location",
        "pg_authid",
        "SCRAM-SHA-256$%",
        "inherit_option",
        "set_option",
        "shobj_description",
    ):
        assert contract in source


def test_snapshot_fences_catalog_and_relations_before_export() -> None:
    """The snapshot fences catalog and relations before the export runs."""
    snapshot_source = inspect.getsource(postgres.exported_snapshot)
    lock_source = inspect.getsource(postgres._lock_export_relations)

    begin = snapshot_source.index("BEGIN ISOLATION LEVEL REPEATABLE READ")
    lock_call = snapshot_source.index("_lock_export_relations")
    quiescent = snapshot_source.index("require_source_quiescent")
    foreign = snapshot_source.index("require_no_foreign_tables")
    export = snapshot_source.index("pg_export_snapshot")
    assert begin < lock_call < quiescent < foreign < export

    catalog_lock = lock_source.index("LOCK TABLE pg_catalog.pg_class IN SHARE MODE")
    enumeration = lock_source.index("_table_names")
    relation_lock = lock_source.index("ACCESS SHARE MODE")
    assert catalog_lock < enumeration < relation_lock


def test_dedicated_cluster_accepts_only_stock_pg18_memberships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dedicated cluster accepts only stock Postgres 18 memberships."""
    source = postgres.ContainerConnection(
        container="c" * 64,
        host="127.0.0.1",
        port=55432,
        database="ums",
        user="postgres",
        password="secret",
        image_id="sha256:" + "a" * 64,
        image_reference="postgres:18-alpine@sha256:" + "b" * 64,
    )
    # Seven-column shape matches the query: database comments are not
    # compared (this postgres:18-alpine build initializes them NULL).
    databases = [
        ("postgres", "postgres", False, True, -1, "pg_default", True),
        ("template0", "postgres", True, False, -1, "pg_default", True),
        ("template1", "postgres", True, True, -1, "pg_default", True),
        ("ums", "postgres", False, True, -1, "pg_default", True),
    ]
    bootstrap_roles = [
        (
            "postgres",
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
    ]
    predefined_roles = [
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
        for role in postgres._PG18_PREDEFINED_ROLES
    ]
    memberships = [
        (role, "pg_monitor", "postgres", False, True, True)
        for role in postgres._PG18_PREDEFINED_MEMBERSHIPS
    ]
    tablespaces = [
        ("pg_default", "postgres", True, True, "", True),
        ("pg_global", "postgres", True, True, "", True),
    ]

    class _Rows:
        """Cursor result stub returning canned rows."""

        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self._canned_rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            """Return the canned rows."""
            return self._canned_rows

    class _Connection:
        """Connection stub serving canned catalog rows."""

        def __init__(self) -> None:
            self.replies = iter(
                [
                    databases,
                    bootstrap_roles,
                    predefined_roles,
                    memberships,
                    tablespaces,
                ]
            )

        def execute(self, _query: str) -> _Rows:
            """Return the canned rows for any query."""
            return _Rows(_exactly_one(self.replies))

        @staticmethod
        def close() -> None:
            """Mark the connection closed."""
            return None

    monkeypatch.setattr(postgres, "_connect", lambda _source: _Connection())
    monkeypatch.setattr(postgres, "require_clean_target", lambda _source: None)

    postgres.require_dedicated_cluster(source)
    assert len(memberships) == 3

    memberships.append(("pg_read_all_data", "postgres", "postgres", False, True, True))
    with pytest.raises(BackupToolError, match="memberships differ"):
        postgres.require_dedicated_cluster(source)


def test_file_runner_opens_restore_source_as_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file runner opens a restore source as binary."""
    source = tmp_path / "database.dump"
    source.write_bytes(b"PGDMP")
    observed: list[bytes] = []

    def _consume(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Consume the handed stream without touching the filesystem."""
        stream = kwargs["stdin"]
        observed.append(stream.read())
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _consume)
    runner = postgres.CommandRunner(timeout_seconds=1)
    assert runner.file_input(["pg_restore"], source) == ""
    assert observed == [b"PGDMP"]
