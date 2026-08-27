#!/usr/bin/env python
# ============================================================================
# Purpose: Operator CLI that creates the first UMS identity (and optional org
#   skeleton) after alembic upgrade on a fresh deployment — P0.8/P0.9.
# Database/ORM: Writes ``users``, ``org_units``, ``access_scopes``, and
#   ``user_role_assignments`` via repositories / ORM under TENANT_CTX + RLS.
# Standards: Idempotent; fail-closed on disabled accounts, org drift, and
#   inactive units; never echo database credentials (summary rebuild + argparse
#   redaction); typed domain errors mapped to exit 2.
# Blast Radius: Authorization (identity + optional global role) and org registry.
# Connections:
#   - File: Docs/21_BETA_IMPLEMENTATION_PLAN.md -> P0.8 / P0.9.
#   - File: Docs/20_DEPLOYMENT_READINESS_AUDIT.md -> H2 / H3.
#   - File: backend/ums_smart_revenue/auth/users.py -> create_user / lookup.
# ============================================================================

"""Create the first UMS operator identity (and, optionally, an org skeleton).

Run this ONCE after ``alembic upgrade head`` on a fresh deployment. It is the
P0.8/P0.9 step of ``Docs/21_BETA_IMPLEMENTATION_PLAN.md`` and closes audit
findings H2 and H3 in ``Docs/20_DEPLOYMENT_READINESS_AUDIT.md``.

What it does
------------
1. Creates one or more user accounts (``--email``, repeatable) and prints the
   **server-generated** id of each, because that id — not anything the operator
   chose — is the value the ``X-User-ID`` request header must carry.
2. With ``--role``, assigns that role at ``global`` scope to each account, so the
   identity can actually act under ``UMS_AUTHZ_SOURCE=database``.
3. With ``--org-skeleton``, creates exactly one ``SECTOR`` and one ``COMPANY``
   parented to it, which is the minimum shape that clears both
   ``MISSING_COMPANY`` and ``MISSING_SECTOR`` from ``GET /channels/issues`` and
   unblocks ``POST /channels``.

Everything is idempotent: a re-run reports EXISTING rows and changes nothing.
Every value in the summary is read back from the stored row, so the summary can
only ever describe what the database actually holds.

This script does not rename (deliberate)
----------------------------------------
``--sector-name`` / ``--company-name`` name a row this run may CREATE. They are
not a rename request, and a re-run whose names disagree with the stored rows is
REFUSED with exit 2 rather than applied or quietly ignored. Applying it would be
an unaudited registry write — the script runs before first login, so there is no
actor to attribute an org-unit rename to — and it would mean a later re-run that
merely forgot the flag silently reset an operator's real name back to
``Default Sector``. Quietly ignoring it is worse still: that is exactly the bug
this behaviour replaces, where the run exited 0 and printed the requested names
back while the database kept the old ones, so every UI screen disagreed with the
console with no signal anywhere. Rename an org unit in the database, then re-run
with the matching flag.

It does not reactivate either, for the same reasons. A stored org unit with
``active = false`` is dropped by ``org/access_index.py`` (``:32`` and ``:84``),
so a channel mapped through it still reports ``MISSING_SECTOR`` — the row is in
the table and absent from every read. That is refused with exit 2 rather than
flipped: flipping it is an unaudited registry write, and it would silently undo
a deliberate deactivation. Reactivate the row in the database and re-run.

The tenant-context trap (read this before editing)
--------------------------------------------------
On PostgreSQL every table this script writes — ``users``, ``org_units``,
``access_scopes``, ``user_role_assignments`` — is tenant-scoped and carries
FORCE ROW LEVEL SECURITY (``db/rls.py::TENANT_SCOPED_TABLES``,
``20260612_0002_force_tenant_rls``). The session hook in ``db/session.py`` runs
``SET LOCAL ROLE app_tenant`` on every transaction, and the isolation policy is
``tenant_id = app_current_tenant_id()``. If ``TENANT_CTX`` is unset when a
transaction begins, the hook CLEARS the trusted context row and every INSERT is
rejected by the policy. ``scripts/seed_demo_month.py`` — the script this one
would otherwise be copied from — does NOT set ``TENANT_CTX``; it is correct on
SQLite only.

So: the tenant is looked up and lifecycle-checked on its OWN session, that
session is closed, and only then is ``TENANT_CTX`` set — set and reset in a
try/finally exactly the way ``DefaultTenantMiddleware`` (``app.py:439-443``)
does it. The write session is opened INSIDE that block so its first transaction
begins with the contextvar already populated. Re-ordering those steps silently
reproduces the fail-closed rejection.

Residual gap this script does not close
---------------------------------------
Assigning channels to the seeded company is one
``PATCH /channels/{id}/mapping`` per channel (``api/channels.py:1425``). There is
no bulk mapping endpoint and the channel-import CSV cannot carry the mapping, so
a real roster needs a scripted loop. The summary printed at the end says so.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID, uuid5

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PATH = str(_PROJECT_ROOT / "backend")

# Deterministic namespace for the org-skeleton rows. Distinct from the demo
# seed's namespace (scripts/seed_demo_month.py) so a database that has run both
# never collides on the org_units primary key. Folding the tenant id into the
# derivation gives each tenant its own id space while same-tenant re-runs stay
# idempotent.
_BOOTSTRAP_NAMESPACE = UUID("00000000-0000-0000-0000-0000b0075747")

_DEFAULT_SECTOR_NAME = "Default Sector"
_DEFAULT_COMPANY_NAME = "Default Company"
_GLOBAL_SCOPE_TYPE = "global"
_ROLE_ASSIGNMENT_REASON = "operator bootstrap"

_BANNER = "=" * 78

# Query-parameter keys whose VALUES are safe to echo in the summary. This is an
# ALLOW-list, not a deny-list: any key not named here is masked, so a
# credential-bearing parameter nobody anticipated cannot leak merely by being
# unlisted. Every key below has an enumerated or numeric value space in libpq,
# so none of them can carry operator-chosen text. `password`, `sslpassword`,
# `passfile`, `sslkey` and `options` are absent for the obvious reason; `user`,
# `host` and anything else is absent because default-deny is the point.
_PRINTABLE_QUERY_KEYS = frozenset(
    {
        "channel_binding",
        "connect_timeout",
        "sslmode",
        "target_session_attrs",
    }
)

# Substituted for every value that is withheld. URL-safe characters only:
# ``URL.render_as_string`` percent-encodes the mask otherwise, so a `***` mask
# would come back out of the renderer as `%2A%2A%2A`.
_REDACTED = "REDACTED"

# Printed INSTEAD of the URL when the string cannot be decomposed with
# confidence. Withholding it is the fail-closed choice: a URL this function
# cannot take apart is exactly the one whose password could be anywhere in it.
_UNPRINTABLE_URL = "<withheld: not a URL this script can safely redact>"

# Argparse splits on whitespace, so a mistyped or space-broken database URL can
# leak as a password fragment (`s3cret@host:5432/db`) with no `://`. Fail-closed:
# any token that looks credential-bearing is masked, not echoed.
# Unanchored: covers URL query forms (?password= / &password=) and argparse
# fragments like ``password=s3cret`` after a whitespace split. Fail-closed —
# matching ``sslpassword=`` is acceptable for redaction. Do not assign a
# ``"pass"+"word="`` literal to a name containing password/secret/_key: that
# pattern is what SCT-A000 flags (DeepSource Secrets), even when the string is
# a detector rather than a credential.
_ARGPARSE_PASSWORD_PARAM_RE = re.compile(r"(?i)password=")
_ARGPARSE_SCHEME_PREFIXES = (
    "postgresql",
    "postgres://",
    "postgres:",
    "mysql://",
    "mysql:",
    "mariadb://",
    "mariadb:",
)


@dataclass(frozen=True)
class _UserOutcome:
    """One account the run created or found, with its server-generated id."""

    email: str
    user_id: str
    display_name: str
    created: bool
    role_key: str | None = None
    role_created: bool = False


@dataclass(frozen=True)
class _OrgUnitOutcome:
    """One org unit the run created or found.

    ``active`` carries no default on purpose: it is the field whose absence let
    an inactive skeleton be reported as healthy, so every construction site must
    read it back from the stored row rather than inherit an optimistic default.
    """

    unit_type: str
    unit_id: str
    name: str
    parent_id: str | None
    created: bool
    active: bool


def _ensure_backend_path() -> None:
    """Make the local backend package importable for direct script execution."""
    if _BACKEND_PATH not in sys.path:
        sys.path.insert(0, _BACKEND_PATH)


@lru_cache(maxsize=1)
def _load_dependencies() -> dict[str, Any]:
    """Import the backend symbols this script orchestrates, once (memoized)."""
    # FIX: Keep project imports lazy so direct script execution can adjust
    # sys.path before importing the backend package (mirrors seed_demo_month.py).
    _ensure_backend_path()

    from sqlalchemy.exc import SQLAlchemyError

    from ums_smart_revenue.auth.audit import AuditEventType
    from ums_smart_revenue.auth.audit_service import record_audit_event
    from ums_smart_revenue.auth.models import UserPrincipal
    from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS, RoleKey
    from ums_smart_revenue.auth.scopes import AccessScope
    from ums_smart_revenue.auth.sql_audit_sink import PlatformLaneAuditSink
    from ums_smart_revenue.auth.user_roles import (
        SqlAlchemyUserRoleAssignmentRepository,
        UserRoleAssignmentConflictError,
        UserRoleAssignmentError,
    )
    from ums_smart_revenue.auth.users import (
        USER_STATUS_DISABLED,
        SqlAlchemyUserAccountRepository,
        UserAccountConflictError,
        UserAccountError,
        UserAccountStorageError,
        UserAccountValidationError,
    )
    from ums_smart_revenue.config.settings import load_app_settings
    from ums_smart_revenue.db.org_models import OrgUnitORM
    from ums_smart_revenue.db.security_models import RoleORM
    from ums_smart_revenue.db.session import build_session_factory
    from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
    from ums_smart_revenue.tenancy.context import TENANT_CTX
    from ums_smart_revenue.tenancy.models import TenantStatus
    from ums_smart_revenue.tenancy.repository import (
        SqlAlchemyTenantRepository,
        TenantNotFoundError,
    )

    return {
        "AccessScope": AccessScope,
        "AuditEventType": AuditEventType,
        "PlatformLaneAuditSink": PlatformLaneAuditSink,
        "ROLE_DEFINITIONS": ROLE_DEFINITIONS,
        "RoleKey": RoleKey,
        "OrgUnitORM": OrgUnitORM,
        "RoleORM": RoleORM,
        "SQLAlchemyError": SQLAlchemyError,
        "SqlAlchemyTenantRepository": SqlAlchemyTenantRepository,
        "SqlAlchemyUserAccountRepository": SqlAlchemyUserAccountRepository,
        "SqlAlchemyUserRoleAssignmentRepository": SqlAlchemyUserRoleAssignmentRepository,
        "TENANT_CTX": TENANT_CTX,
        "TenantNotFoundError": TenantNotFoundError,
        "TenantStatus": TenantStatus,
        "UMS_TENANT_ID": UMS_TENANT_ID,
        "USER_STATUS_DISABLED": USER_STATUS_DISABLED,
        "UserAccountConflictError": UserAccountConflictError,
        "UserAccountError": UserAccountError,
        "UserAccountStorageError": UserAccountStorageError,
        "UserAccountValidationError": UserAccountValidationError,
        "UserPrincipal": UserPrincipal,
        "UserRoleAssignmentConflictError": UserRoleAssignmentConflictError,
        "UserRoleAssignmentError": UserRoleAssignmentError,
        "build_session_factory": build_session_factory,
        "load_app_settings": load_app_settings,
        "record_audit_event": record_audit_event,
    }


def _bootstrap_uuid(tenant_id: UUID, *parts: str) -> UUID:
    """Return a deterministic, tenant-scoped UUID for one bootstrap row."""
    return uuid5(_BOOTSTRAP_NAMESPACE, "|".join((tenant_id.hex, *parts)))


def _default_display_name(email: str) -> str:
    """Return the display name used when ``--display-name`` was not supplied."""
    local_part = email.split("@", maxsplit=1)[0].strip()
    return local_part or email


# ============================================================================
# Purpose: Rebuild a PRINTABLE database URL out of the components that are known
#   not to be credentials, instead of hunting down every place a credential can
#   hide. The previous revision masked only ``urlsplit``'s userinfo password,
#   and two WORKING PostgreSQL URLs walked straight past it: a password carried
#   as ``?password=`` (a legitimate libpq/psycopg parameter — it connects, exits
#   0 and printed the credential) and a password containing ``/`` (``urlsplit``
#   finds no netloc ``@`` and returned the string verbatim).
# Database/ORM: None. Pure string work on the URL; opens no connection.
# Standards: Allow-list reconstruction, fail-closed. Parsing goes through
#   SQLAlchemy's own ``make_url`` — the parser ``create_engine`` will use — so
#   the URL is redacted as the driver actually reads it rather than as a second
#   parser guesses. Every component is dropped unless it is explicitly known to
#   be safe, and anything ``make_url`` cannot parse, or parses ambiguously, is
#   withheld ENTIRELY rather than printed on a guess.
# Blast Radius: Credential disclosure on the operator console and in any
#   captured runbook log. No database, authorization or finance behaviour.
# Connections:
#   - File: scripts/bootstrap_operator.py -> main(), which prints the result.
#   - File: backend/ums_smart_revenue/db/session.py -> build_session_factory,
#     which hands the same string to create_engine.
# ============================================================================
def _redact_database_url(database_url: str) -> str:
    """Rebuild the URL from its non-credential parts, or withhold it entirely.

    Drivername, username, host, port and database name are preserved so the
    operator can still tell WHICH database was bootstrapped. The userinfo
    password becomes a mask, and every query value is masked unless its key is
    in ``_PRINTABLE_QUERY_KEYS``.
    """
    # Imported here rather than at module scope to keep this script's imports
    # lazy, exactly as _load_dependencies does.
    from sqlalchemy.engine import URL, make_url
    from sqlalchemy.exc import ArgumentError

    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        # ArgumentError is the documented parse failure. The bare ValueError is
        # not documented and was found by fuzzing this function: a non-integer
        # port ("host:notaport") reaches `int(components["port"])` inside
        # `_parse_url` and escapes as `invalid literal for int()`. A redaction
        # guard that RAISES is worse than one that over-masks — it would abort
        # the summary after the bootstrap had already committed — so both land
        # on the same withheld marker.
        return _UNPRINTABLE_URL
    # FIX: SQLAlchemy splits the userinfo on the FIRST '@', so an unescaped '@'
    # inside a password pushes the REST OF THE PASSWORD into the host:
    # 'user:p@ss@host/db' parses as password='p', host='ss@host'. Echoing that
    # host would print credential material, so withhold the whole URL instead.
    if "@" in (url.host or ""):
        return _UNPRINTABLE_URL
    safe_query: dict[str, str | tuple[str, ...]] = {
        key: value if key in _PRINTABLE_QUERY_KEYS else _REDACTED
        for key, value in url.query.items()
    }
    return URL.create(
        drivername=url.drivername,
        username=url.username,
        password=_REDACTED if url.password is not None else None,
        host=url.host,
        port=url.port,
        database=url.database,
        query=safe_query,
    ).render_as_string(hide_password=False)


# ============================================================================
# Purpose: Keep argparse usage/error text from echoing database credentials.
#   Stock argparse splits on whitespace and reprints unrecognized argv tokens,
#   so a mistyped ``--databse-url`` (or a URL broken across spaces) can leak
#   the password as a bare fragment with no ``://``. These helpers fail-closed:
#   any token that looks credential-bearing is masked via ``_redact_database_url``
#   or replaced with ``_UNPRINTABLE_URL`` / ``_REDACTED``.
# Database/ORM: None. Pure string work; opens no connection.
# Standards: Fail-closed redaction at the CLI boundary before stderr leave.
# Blast Radius: Operator stderr only. No authz, finance, or audit behavior.
# Connections:
#   - File: scripts/bootstrap_operator.py -> ``_RedactingArgumentParser.error``.
#   - File: scripts/bootstrap_operator.py -> ``_redact_database_url``.
# ============================================================================
def _argparse_token_looks_credential_bearing(token: str) -> bool:
    """Return True when *token* may carry database credentials."""
    lowered = token.lower()
    if "://" in token:
        return True
    if lowered.startswith(_ARGPARSE_SCHEME_PREFIXES):
        return True
    if _ARGPARSE_PASSWORD_PARAM_RE.search(token) is not None:
        return True
    # userinfo@host-ish remnant after argparse split on whitespace
    if "@" in token:
        userinfo, _, hostpart = token.partition("@")
        if userinfo and hostpart and (
            "." in hostpart or ":" in hostpart or "/" in hostpart or hostpart[0].isalnum()
        ):
            return True
    return False


def _redact_argparse_token(token: str) -> str:
    """Mask credential-bearing argv tokens that argparse may echo on error."""
    if not _argparse_token_looks_credential_bearing(token):
        return token
    lowered = token.lower()
    if "://" in token or lowered.startswith(_ARGPARSE_SCHEME_PREFIXES):
        return _redact_database_url(token)
    # Split password / userinfo fragments are not full URLs — withhold entirely.
    return _UNPRINTABLE_URL


def _redact_argparse_message(message: str) -> str:
    """Redact credential-bearing tokens from an argparse error message."""
    return " ".join(_redact_argparse_token(token) for token in message.split())


class _RedactingArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that never echoes raw database URLs on usage errors."""

    def error(self, message: str) -> NoReturn:
        """error."""
        self.print_usage(sys.stderr)
        safe = _redact_argparse_message(message)
        self.exit(2, f"{self.prog}: error: {safe}\n")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse operator CLI arguments for one bootstrap run."""
    parser = _RedactingArgumentParser(
        description=(
            "Create the first UMS operator user(s), optionally assign a global "
            "role, and optionally create the minimal SECTOR/COMPANY org skeleton."
        ),
    )
    parser.add_argument(
        "--email",
        action="append",
        required=True,
        metavar="EMAIL",
        help="Account email. Repeat the flag to create more than one account.",
    )
    parser.add_argument(
        "--display-name",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "Display name, paired positionally with --email. When given it must "
            "be repeated as many times as --email; otherwise the email's local "
            "part is used."
        ),
    )
    parser.add_argument(
        "--role",
        default=None,
        metavar="ROLE_KEY",
        help=(
            "Optional role key (see backend/ums_smart_revenue/auth/roles.py) to "
            "assign at global scope to every account. Omitted by default: this "
            "script never grants authority the operator did not ask for."
        ),
    )
    parser.add_argument(
        "--org-skeleton",
        action="store_true",
        help="Also create one SECTOR and one COMPANY parented to it.",
    )
    parser.add_argument(
        "--sector-name",
        default=_DEFAULT_SECTOR_NAME,
        help=f"Name for the seeded SECTOR (default: {_DEFAULT_SECTOR_NAME!r}).",
    )
    parser.add_argument(
        "--company-name",
        default=_DEFAULT_COMPANY_NAME,
        help=f"Name for the seeded COMPANY (default: {_DEFAULT_COMPANY_NAME!r}).",
    )
    parser.add_argument(
        "--tenant",
        default=None,
        type=UUID,
        help="Tenant UUID (default: the bootstrap UMS tenant).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy database URL (default: UMS_DATABASE_URL from settings).",
    )
    args = parser.parse_args(argv)
    if args.display_name is not None and len(args.display_name) != len(args.email):
        parser.error("--display-name must be repeated exactly as many times as --email")
    return args


def _resolve_accounts(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Return the (email, display_name) pairs the run should ensure."""
    display_names = args.display_name or [_default_display_name(email) for email in args.email]
    return list(zip(args.email, display_names, strict=True))


# ============================================================================
# Purpose: Ensure ONE user account exists for ``email`` and report the id the
#   SERVER generated for it. ``create_user`` mints the id with ``uuid4()``
#   (auth/users.py), so the operator cannot choose it — audit H3. The lookup runs
#   FIRST and the create only when the lookup misses, so a re-run reports the
#   existing id instead of raising ``UserAccountConflictError``.
# Database/ORM: ``users`` (UserORM) via SqlAlchemyUserAccountRepository — the
#   repository owns every statement; this function issues no SQL of its own.
# Standards: Repository-owned read and write, typed domain errors. The lookup is
#   a repository call rather than a raw query, so the tenant filter and the
#   normalized-email predicate stay in one place; ``create_user`` re-checks the
#   same uniqueness rule, so the look-then-create is not a TOCTOU widening.
# Blast Radius: Authorization — creates an identity. It grants NO permissions;
#   role assignment is a separate, explicitly requested step.
# Connections:
#   - File: backend/ums_smart_revenue/auth/users.py -> create_user /
#     get_user_by_email.
#   - File: Docs/20_DEPLOYMENT_READINESS_AUDIT.md -> H3 (identity footgun).
# ============================================================================
def _ensure_user(
    session: Any,
    deps: dict[str, Any],
    *,
    tenant_id: UUID,
    email: str,
    display_name: str,
) -> _UserOutcome:
    """Create the account if absent; return its server-generated id either way."""
    repository = deps["SqlAlchemyUserAccountRepository"](session, tenant_id=tenant_id)
    existing = repository.get_user_by_email(email=email)
    if existing is not None:
        return _accept_existing_user(existing, deps, email=email)
    try:
        entry = repository.create_user(
            email=email,
            display_name=display_name,
            is_service_account=False,
        )
    except deps["UserAccountConflictError"]:
        # FIX: a concurrent bootstrap for the same email committed between our
        # lookup and our insert. UserAccountConflictError is a SIBLING of
        # UserAccountStorageError under UserAccountError, so the retry envelope
        # in _run_bootstrap (`except storage_error`) does not catch it: it
        # reached main's `except ValueError` and exited 2, rolling back this
        # invocation's other work even though the account it wanted now exists.
        # That contradicts the documented idempotency contract. Re-read the
        # winner and report EXISTING -- through the SAME validation as the
        # lookup branch, so a concurrently created disabled or service account
        # still fails closed rather than being waved through.
        winner = repository.get_user_by_email(email=email)
        if winner is None:
            raise
        return _accept_existing_user(winner, deps, email=email)
    return _UserOutcome(
        email=entry.email,
        user_id=entry.id,
        display_name=entry.display_name,
        created=True,
    )


def _accept_existing_user(existing: Any, deps: dict[str, Any], *, email: str) -> _UserOutcome:
    """Validate an already-stored account and report it as EXISTING.

    Shared by the initial lookup and the concurrent-create-conflict reload so
    both paths apply identical fail-closed guards.
    """
    # FIX: A disabled account must not be treated as a usable EXISTING
    # operator. Runtime principals fail-closed on disabled status, but this
    # path previously returned the id and could still attach --role.
    if existing.status == deps["USER_STATUS_DISABLED"]:
        raise deps["UserAccountValidationError"](
            f"User {email!r} exists with status=disabled. This script creates "
            "accounts and never reactivates them — flipping the flag would be "
            "an unaudited identity write before first login, and it would "
            "silently undo a deliberate deactivation. Nothing was changed. "
            "Reactivate the users row in the database and re-run."
        )
    if existing.is_service_account:
        raise deps["UserAccountValidationError"](
            f"User {email!r} is a service account. bootstrap_operator creates "
            "human operator accounts only — pass a human email or provision "
            "the service actor through the connector runbook instead."
        )
    return _UserOutcome(
        email=existing.email,
        user_id=existing.id,
        display_name=existing.display_name,
        created=False,
    )


# ============================================================================
# Purpose: Assign ``role_key`` at ``global`` scope to one bootstrapped account,
#   then emit ``USER_ROLE_CHANGED`` in the same tenant-lane unit of work when
#   the assignment is new. The account assigns the role to ITSELF
#   (``assigned_by`` is its own id) because a fresh database has no prior actor
#   row, and ``assign_role`` requires the actor to exist in the tenant. The same
#   self-actor is stamped onto the audit row so the first privilege grant is
#   never silent (Docs/12_BACKEND_API_SPEC.md).
# Database/ORM: ``user_role_assignments`` + ``access_scopes`` (the global scope
#   row is created on demand by the repository's ``_get_or_create_scope``), both
#   tenant-scoped and RLS-governed; ``audit_logs`` via ``PlatformLaneAuditSink``
#   (tenant session + elevated append).
# Standards: Repository-owned write, so the service-only-role guard, the
#   scope-compatibility check and the duplicate-assignment guard all still run.
#   An already-assigned role surfaces as the typed conflict and is reported as
#   EXISTING with no second audit event. Audit failures propagate so the session
#   rolls back an unaudited privileged grant.
# Blast Radius: Authorization + audit — this is the step that gives the identity
#   power. It runs ONLY when the operator passed ``--role``.
# Connections:
#   - File: backend/ums_smart_revenue/auth/user_roles.py -> assign_role.
#   - File: backend/ums_smart_revenue/api/users.py -> ``_audit_role_change`` shape.
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260825_0001_security_role_permission_seed.py -> seeds the FK parent rows.
# ============================================================================
def _assign_global_role(
    session: Any,
    deps: dict[str, Any],
    *,
    tenant_id: UUID,
    user: _UserOutcome,
    role_key: str,
    audit_sink: Any,
) -> _UserOutcome:
    """Assign ``role_key`` globally to ``user``; report whether it was new."""
    repository = deps["SqlAlchemyUserRoleAssignmentRepository"](session, tenant_id=tenant_id)
    try:
        assignment = repository.assign_role(
            user_id=user.user_id,
            role_key=role_key,
            scope_type=_GLOBAL_SCOPE_TYPE,
            scope_id=None,
            assigned_by=user.user_id,
            reason=_ROLE_ASSIGNMENT_REASON,
        )
    except deps["UserRoleAssignmentConflictError"]:
        return _UserOutcome(
            email=user.email,
            user_id=user.user_id,
            display_name=user.display_name,
            created=user.created,
            role_key=role_key,
            role_created=False,
        )
    actor = deps["UserPrincipal"](
        user_id=user.user_id,
        email=user.email,
        tenant_id=str(tenant_id),
    )
    deps["record_audit_event"](
        sink=audit_sink,
        actor=actor,
        event_type=deps["AuditEventType"].USER_ROLE_CHANGED,
        entity_type="user_role_assignment",
        entity_id=str(assignment.id),
        scope=deps["AccessScope"].global_scope(),
        reason=_ROLE_ASSIGNMENT_REASON,
        details={
            "action": "assigned",
            "target_user_id": assignment.user_id,
            "role_key": assignment.role_key,
            "scope_type": assignment.scope_type,
            "scope_id": assignment.scope_id,
            "active": assignment.active,
        },
    )
    return _UserOutcome(
        email=user.email,
        user_id=user.user_id,
        display_name=user.display_name,
        created=user.created,
        role_key=role_key,
        role_created=True,
    )


# ============================================================================
# Purpose: Decide whether a PRE-EXISTING bootstrap org row still matches what
#   this run asked for, and return an operator message when it does not. A
#   divergence is REFUSED — never applied, and never quietly ignored. Applying
#   it would be an unaudited registry rename (this script runs before first
#   login and has no actor to attribute one to), and it would let a later re-run
#   that merely omitted ``--sector-name`` reset a real name back to the default.
#   Ignoring it quietly is the bug this replaces: exit 0 plus a summary echoing
#   names the database never took.
# Database/ORM: ``org_units`` (OrgUnitORM) — inspects the already-loaded row
#   only. Issues no SQL and writes nothing.
# Standards: Fail-closed. Returns a message instead of raising so the caller
#   keeps the single ValueError -> exit 2 path ``_require_seeded_role`` uses.
#   The tenant check is not decoration: ``org_units`` is keyed on ``id`` alone
#   (org_models.py:35), so ``session.get`` can reach another tenant's row
#   wherever RLS is not enforcing, and treating that as "already exists" would
#   skip this tenant's row and still report success. Neither is the ``active``
#   check: an inactive unit is invisible to every org read, so accepting one
#   would have the script assert a skeleton the API contradicts.
# Blast Radius: Registry/org mapping. Refuses a write; can never perform one.
# Connections:
#   - File: backend/ums_smart_revenue/org/access_index.py -> the COMPANY->SECTOR
#     walk, the ``if unit.active`` filter at :32 and the ``active.is_(True)``
#     predicate at :84 — why a wrong parent OR an inactive row still reports
#     MISSING_SECTOR.
#   - File: backend/ums_smart_revenue/db/org_models.py -> the id-only primary key.
# ============================================================================
def _org_unit_drift(
    row: Any,
    *,
    tenant_id: UUID,
    unit_type: str,
    name: str,
    parent_id: UUID | None,
    name_flag: str,
) -> str | None:
    """Return an operator message when the stored row differs from the request."""
    if row.tenant_id != tenant_id:
        return (
            f"Org unit {row.id} already exists under tenant {row.tenant_id}, not "
            f"{tenant_id}. Nothing was changed."
        )
    if row.type != unit_type:
        return (
            f"Org unit {row.id} is stored with type {row.type!r}, but this run "
            f"expects the bootstrap {unit_type}. Nothing was changed; inspect the "
            "org_units row before re-running --org-skeleton."
        )
    # FIX: active was not compared, so a seeded skeleton whose row had been
    # deactivated was reported as EXISTING alongside the unconditional claim
    # that it "clears BOTH MISSING_COMPANY and MISSING_SECTOR" — while
    # GET /channels/issues returned MISSING_SECTOR for every channel mapped
    # through it. Same defect class this function already closed for parent_id,
    # and more reachable: active is an ordinary column, whereas the cross-tenant
    # case needs a deliberately planted uuid5 collision. The comparison is
    # against the literal True that _ensure_org_unit inserts.
    if not row.active:
        return (
            f"The {unit_type} bootstrap row {row.id} is stored with active=false. "
            "OrgAccessIndex drops inactive units (org/access_index.py:32,84), so "
            "a channel mapped through it still reports MISSING_SECTOR on "
            "GET /channels/issues: the skeleton would be present in the table and "
            "absent from every read. This script creates org units and never "
            "reactivates them — flipping the flag would be an unaudited registry "
            "write (there is no actor to attribute one to before first login), and "
            "it would silently undo a deliberate deactivation. Nothing was "
            "changed. Reactivate the org_units row in the database and re-run "
            "--org-skeleton."
        )
    if row.name != name:
        return (
            f"The {unit_type} bootstrap row {row.id} is stored as {row.name!r}, "
            f"but this run asked for {name!r}. This script creates org units and "
            "never renames them: the rename would be an unaudited registry write, "
            "and a later re-run that simply omitted the flag would silently reset "
            f"the name back to the default. Nothing was changed. Pass {name_flag} "
            f"{row.name!r} to accept the stored name, or rename the unit "
            "deliberately in the database and re-run."
        )
    stored_parent = str(row.parent_id) if row.parent_id is not None else None
    wanted_parent = str(parent_id) if parent_id is not None else None
    if stored_parent != wanted_parent:
        return (
            f"The {unit_type} bootstrap row {row.id} has parent_id "
            f"{stored_parent}, not the expected {wanted_parent}. A COMPANY that is "
            "not parented to the seeded SECTOR still reports MISSING_SECTOR on "
            "GET /channels/issues, so this is not cosmetic. Nothing was changed; "
            "repair the org_units row before re-running --org-skeleton."
        )
    return None


# ============================================================================
# Purpose: Ensure ONE org unit exists at its deterministic id, and report the
#   row's OWN stored values. Every field of the outcome is read back from the
#   ORM row rather than echoed from the CLI arguments, so the summary can only
#   ever describe what the database actually holds.
# Database/ORM: ``org_units`` (OrgUnitORM) written directly. ``org_units`` has
#   NO repository and NO API writer anywhere in the codebase (audit H2); the
#   only other writer is scripts/seed_demo_month.py, whose guarded-insert shape
#   this mirrors. Building POST /org-units was explicitly ruled out of the beta.
# Standards: Deterministic ``uuid5`` id, insert guarded by a primary-key lookup
#   so a re-run mutates nothing, and the insert is flushed before the caller
#   moves on so the self-referential composite FK
#   ``(tenant_id, parent_id) -> (tenant_id, id)`` resolves for the child.
# Blast Radius: Registry/org mapping only. No finance math, no authorization
#   change, no audit event (this is a pre-first-login bootstrap, and there is no
#   actor to attribute an event to yet).
# Connections:
#   - File: backend/ums_smart_revenue/db/org_models.py -> OrgUnitORM constraints.
#   - File: scripts/seed_demo_month.py -> the guarded-insert pattern lifted here.
# ============================================================================
def _ensure_org_unit(
    session: Any,
    org_orm: Any,
    *,
    unit_id: UUID,
    tenant_id: UUID,
    parent_id: UUID | None,
    unit_type: str,
    name: str,
    name_flag: str,
) -> _OrgUnitOutcome:
    """Create the unit if absent; report the stored row's own values either way."""
    from sqlalchemy.exc import IntegrityError

    row = session.get(org_orm, unit_id)
    created = row is None
    if created:
        candidate = org_orm(
            id=unit_id,
            tenant_id=tenant_id,
            parent_id=parent_id,
            type=unit_type,
            name=name,
            # A bootstrap unit is always created active; ``_org_unit_drift``
            # refuses a pre-existing row that is not, because an inactive unit
            # is dropped by every org read.
            active=True,
        )
        # FIX: org ids are DETERMINISTIC (_bootstrap_uuid over tenant + role),
        # so two concurrent --org-skeleton runs -- even for different operator
        # emails -- compute the same id, both miss the get() above, and the
        # loser's flush raises IntegrityError. Unwrapped, that escaped to main's
        # `except SQLAlchemyError` and rolled back the WHOLE invocation,
        # including its freshly created account and role, even though the
        # skeleton it wanted now exists. The savepoint confines the failed
        # insert so the enclosing transaction stays usable, exactly as
        # SqlAlchemyUserRoleAssignmentRepository._get_or_create_scope does for
        # concurrent access-scope creators.
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
            row = candidate
        except IntegrityError:
            # The savepoint rolled back; re-read the winning row and fall
            # through to the SAME drift validation an ordinary EXISTING row
            # gets, so a concurrent writer that created a drifted or inactive
            # unit still fails closed instead of being silently accepted.
            row = session.get(org_orm, unit_id)
            if row is None:
                raise
            created = False
    if not created:
        drift = _org_unit_drift(
            row,
            tenant_id=tenant_id,
            unit_type=unit_type,
            name=name,
            parent_id=parent_id,
            name_flag=name_flag,
        )
        if drift is not None:
            raise ValueError(drift)
    # FIX: Report every field from the ROW, not from the CLI arguments. The
    # previous revision built the EXISTING outcome from ``name`` and
    # ``sector_id``, so a re-run with --sector-name "RENAMED SECTOR" printed
    # "EXISTING SECTOR name='RENAMED SECTOR'" and exit 0 while the database
    # still held 'Default Sector'; ``parent_id`` was echoed the same way. The
    # operator was told a rename had happened that never had. ``_ensure_user``
    # already read back from its row — the two paths in this file now agree.
    return _OrgUnitOutcome(
        unit_type=row.type,
        unit_id=str(row.id),
        name=row.name,
        parent_id=str(row.parent_id) if row.parent_id is not None else None,
        created=created,
        active=row.active,
    )


# ============================================================================
# Purpose: Create the minimal org hierarchy — one SECTOR plus one COMPANY whose
#   ``parent_id`` is that sector. Both rows are required: a COMPANY without a
#   SECTOR parent is absent from ``OrgAccessIndex.company_sector``, so a mapped
#   channel reports ``MISSING_SECTOR`` instead of ``MISSING_COMPANY`` — the same
#   HIGH issue under a different label, which reads as "the fix did not work".
# Database/ORM: ``org_units`` (OrgUnitORM), via ``_ensure_org_unit``.
# Standards: The SECTOR is ensured (and flushed) first so the COMPANY's
#   composite parent FK resolves. A drift on the SECTOR raises before the
#   COMPANY is touched, so a refused run writes nothing at all.
# Blast Radius: Registry/org mapping only.
# Connections:
#   - File: backend/ums_smart_revenue/org/access_index.py -> the COMPANY->SECTOR
#     walk that decides whether a mapped channel is issue-free.
#   - File: Docs/21_BETA_IMPLEMENTATION_PLAN.md -> P0.9.
# ============================================================================
def _ensure_org_skeleton(
    session: Any,
    deps: dict[str, Any],
    *,
    tenant_id: UUID,
    sector_name: str,
    company_name: str,
) -> list[_OrgUnitOutcome]:
    """Create the SECTOR and its child COMPANY if absent; report both stored rows."""
    org_orm = deps["OrgUnitORM"]
    sector_id = _bootstrap_uuid(tenant_id, "org", "sector")
    company_id = _bootstrap_uuid(tenant_id, "org", "company")
    sector = _ensure_org_unit(
        session,
        org_orm,
        unit_id=sector_id,
        tenant_id=tenant_id,
        parent_id=None,
        unit_type="SECTOR",
        name=sector_name,
        name_flag="--sector-name",
    )
    company = _ensure_org_unit(
        session,
        org_orm,
        unit_id=company_id,
        tenant_id=tenant_id,
        parent_id=sector_id,
        unit_type="COMPANY",
        name=company_name,
        name_flag="--company-name",
    )
    return [sector, company]


# ============================================================================
# Purpose: Reject a ``--role`` the platform does not define, or one whose
#   catalog row has not been seeded yet, BEFORE any account is created. Without
#   this the run would create the user and then die on an opaque foreign-key
#   violation from ``user_role_assignments.role_key -> roles.key``, which reads
#   like a bug in the script rather than "you have not run the migrations".
# Database/ORM: ``roles`` (RoleORM) — a platform-wide catalog read, no writes.
# Standards: Fail-closed. An unknown key is rejected against the RoleKey enum
#   rather than passed through to the database, and an unseeded key is rejected
#   rather than assumed harmless. Returns a message instead of raising so the
#   caller keeps one error path.
# Blast Radius: Authorization — the gate in front of the only privilege-granting
#   step this script performs.
# Connections:
#   - File: backend/ums_smart_revenue/auth/roles.py -> RoleKey / ROLE_DEFINITIONS.
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260825_0001_security_role_permission_seed.py -> seeds the roles table.
# ============================================================================
def _require_seeded_role(session: Any, deps: dict[str, Any], role_key: str) -> str | None:
    """Return an operator message when ``role_key`` is unknown or unseeded."""
    role_keys = {role.value for role in deps["RoleKey"]}
    if role_key not in role_keys:
        return f"--role must be one of: {', '.join(sorted(role_keys))}"
    if session.get(deps["RoleORM"], role_key) is None:
        return (
            f"Role '{role_key}' has no row in the roles table. Run "
            "`alembic upgrade head` first — migration 20260825_0001 seeds the "
            "role and permission catalog that every role assignment depends on."
        )
    return None


# ============================================================================
# Purpose: Resolve and lifecycle-gate the tenant on its OWN short-lived session,
#   BEFORE any tenant context is set. ``tenants`` is platform-wide (not in
#   TENANT_SCOPED_TABLES), so it is readable with no tenant context; every other
#   table this script touches is not. Using a separate session guarantees the
#   write session's first transaction begins with TENANT_CTX already populated,
#   which is what the RLS hook in db/session.py reads.
# Database/ORM: ``tenants`` via SqlAlchemyTenantRepository.get_by_id.
# Standards: Replays the ACTIVE-only gate TenantResolverMiddleware enforces on
#   web requests, so a suspended or archived tenant cannot be bootstrapped into.
#   Returns a typed (tenant, error) pair instead of raising, so ``main`` owns the
#   exit code.
# Blast Radius: Authorization — the lifecycle gate. No writes.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/repository.py -> get_by_id.
#   - File: backend/ums_smart_revenue/tenancy/resolver.py -> the same gate for
#     web requests.
# ============================================================================
def _load_active_tenant(
    session_factory: Any,
    deps: dict[str, Any],
    tenant_id: UUID,
) -> tuple[Any | None, str | None]:
    """Return the ACTIVE tenant for ``tenant_id``, or an operator error message."""
    with session_factory() as session:
        try:
            tenant = deps["SqlAlchemyTenantRepository"](session).get_by_id(tenant_id)
        except deps["TenantNotFoundError"]:
            return None, (
                f"No tenant with id {tenant_id}. Run `alembic upgrade head` "
                "first: migration 20260516_0001 seeds the bootstrap UMS tenant."
            )
        except deps["SQLAlchemyError"] as exc:
            # FIX: The most likely first-run mistake is pointing this script at a
            # database that has never been migrated, where the read raises a raw
            # "relation tenants does not exist". Turn that traceback into an
            # actionable line. The exception TEXT is deliberately not echoed: a
            # connection failure can carry the host, port and username from the
            # URL, which the summary otherwise takes care to redact.
            return None, (
                f"{type(exc).__name__}: could not read the tenants table. "
                "Check the database is reachable and that `alembic upgrade head` "
                "has been run against it."
            )
        if tenant.status != deps["TenantStatus"].ACTIVE:
            return None, f"Tenant {tenant_id} is {tenant.status.value}, not ACTIVE."
    return tenant, None


# ============================================================================
# Purpose: Run every bootstrap write inside ONE transaction, with TENANT_CTX set
#   and reset in a try/finally exactly as DefaultTenantMiddleware does it
#   (app.py:439-443). The write session is opened INSIDE the contextvar block so
#   its first transaction — and therefore the after_begin RLS hook — sees the
#   tenant. Set the contextvar after opening the session and every INSERT is
#   rejected by the tenant-isolation policy on PostgreSQL.
# Database/ORM: ``users``, ``access_scopes``, ``user_role_assignments`` and
#   ``org_units`` — all tenant-scoped, all FORCE RLS after 20260612_0002.
# Standards: One session, one commit; the session context manager rolls back on
#   any exception, and the contextvar token is reset on every exit path so the
#   process never leaks a tenant into later work.
# Blast Radius: Authorization (identity + optional role), audit
#   (``USER_ROLE_CHANGED`` on new ``--role`` grants), and registry (org units).
#   No finance math, no schema change.
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> the after_begin hook that
#     reads get_current_tenant() and pins app_tenant.
#   - File: backend/ums_smart_revenue/tenancy/context.py -> TENANT_CTX.
#   - File: backend/ums_smart_revenue/app.py -> DefaultTenantMiddleware, the
#     set/reset pattern this mirrors.
# ============================================================================
def _run_bootstrap(
    session_factory: Any,
    deps: dict[str, Any],
    *,
    tenant: Any,
    accounts: list[tuple[str, str]],
    role_key: str | None,
    org_skeleton: bool,
    sector_name: str,
    company_name: str,
) -> tuple[list[_UserOutcome], list[_OrgUnitOutcome]]:
    """Create the accounts, optional role grants, and optional org skeleton."""
    tenant_ctx = deps["TENANT_CTX"]
    token = tenant_ctx.set(tenant)
    storage_error = deps["UserAccountStorageError"]
    try:
        for attempt_index in range(2):
            try:
                with session_factory() as session:
                    # FIX: Establish a real outer transaction BEFORE any repository
                    # ``begin_nested()`` (``create_user`` / ``assign_role``). On SQLite
                    # StaticPool, calling ``begin_nested()`` as the first session
                    # operation leaves writes that ``session.rollback()`` cannot undo
                    # (connection stays ``in_transaction`` after Session rollback, and
                    # engine dispose / sqlite3 close then persists the user row). That
                    # broke the "nothing was committed" promise when ``--org-skeleton``
                    # failed after account creation (no ``org_units`` table).
                    if not session.in_transaction():
                        session.begin()
                    if role_key is not None:
                        message = _require_seeded_role(session, deps, role_key)
                        if message is not None:
                            raise ValueError(message)
                    audit_sink = (
                        deps["PlatformLaneAuditSink"](session, tenant_id=tenant.id)
                        if role_key is not None
                        else None
                    )
                    users: list[_UserOutcome] = []
                    for email, display_name in accounts:
                        user = _ensure_user(
                            session,
                            deps,
                            tenant_id=tenant.id,
                            email=email,
                            display_name=display_name,
                        )
                        if role_key is not None:
                            user = _assign_global_role(
                                session,
                                deps,
                                tenant_id=tenant.id,
                                user=user,
                                role_key=role_key,
                                audit_sink=audit_sink,
                            )
                        users.append(user)
                    org_units: list[_OrgUnitOutcome] = []
                    if org_skeleton:
                        org_units = _ensure_org_skeleton(
                            session,
                            deps,
                            tenant_id=tenant.id,
                            sector_name=sector_name,
                            company_name=company_name,
                        )
                    session.commit()
                    return users, org_units
            except storage_error:
                if attempt_index + 1 >= 2:
                    raise
    finally:
        tenant_ctx.reset(token)
    raise RuntimeError("unreachable bootstrap retry state")


def _print_user_summary(users: list[_UserOutcome], *, role_key: str | None) -> None:
    """Print the account table and the unmissable X-User-ID warning."""
    print("Users")
    print("-----")
    for user in users:
        state = "CREATED " if user.created else "EXISTING"
        print(f"  {state}  {user.email}  id={user.user_id}  name={user.display_name!r}")
    print()
    print(_BANNER)
    print("  X-User-ID — COPY THESE IDS EXACTLY")
    print(_BANNER)
    print("  Each id above was generated by the SERVER when the row was created;")
    print("  you cannot choose it. Any other value in the X-User-ID header is a")
    print("  DIFFERENT identity: under UMS_AUTHZ_SOURCE=database the principal")
    print("  lookup fails outright, and under the trusted-header default your")
    print("  actions are attributed to an id that has no user row at all.")
    for user in users:
        print(f"    {user.email}  ->  X-User-ID: {user.user_id}")
    print(_BANNER)
    print()
    print("Roles")
    print("-----")
    if role_key is None:
        print("  NO ROLE ASSIGNED.")
        print("  Under UMS_AUTHZ_SOURCE=database these accounts hold zero")
        print("  permissions and every guarded endpoint will refuse them.")
        print("  Re-run with --role <key> (keys: backend/ums_smart_revenue/auth/roles.py)")
        print("  when you have decided which role the operator should hold.")
        return
    for user in users:
        state = "ASSIGNED" if user.role_created else "EXISTING"
        print(f"  {state}  {user.email}  role={role_key}  scope=global")


def _print_org_summary(org_units: list[_OrgUnitOutcome]) -> None:
    """Print the org-skeleton table and the per-channel mapping caveat."""
    if not org_units:
        return
    company_ids = [unit.unit_id for unit in org_units if unit.unit_type == "COMPANY"]
    print()
    print("Org units")
    print("---------")
    for unit in org_units:
        state = "CREATED " if unit.created else "EXISTING"
        parent = f" parent={unit.parent_id}" if unit.parent_id else ""
        inactive = "" if unit.active else " active=False"
        print(
            f"  {state}  {unit.unit_type:<7} id={unit.unit_id} name={unit.name!r}{parent}{inactive}"
        )
    # FIX: the "clears BOTH" claim used to be unconditional, so an inactive row
    # was announced as healthy while GET /channels/issues returned
    # MISSING_SECTOR. _org_unit_drift now refuses that state before the summary
    # is reached; this branch is the second line of defence, so relaxing the
    # refusal later cannot silently restore the false claim.
    if all(unit.active for unit in org_units):
        print("  A COMPANY parented to a SECTOR is the minimum shape that clears BOTH")
        print("  MISSING_COMPANY and MISSING_SECTOR from GET /channels/issues.")
    else:
        print("  WARNING: an org unit above is INACTIVE. OrgAccessIndex drops inactive")
        print("  units, so a channel mapped through it still reports MISSING_SECTOR on")
        print("  GET /channels/issues. This skeleton does NOT clear the issue.")
    for company_id in company_ids:
        print("  Map a channel to this company with:")
        print("    PATCH /channels/{youtube_channel_id}/mapping")
        print(f'    {{"primary_company_id": "{company_id}", "reason": "<why>"}}')
    print("  There is no bulk mapping endpoint and the channel-import CSV cannot")
    print("  carry the mapping, so a real roster needs a scripted loop.")


# ============================================================================
# Purpose: Operator entrypoint. Resolve settings + database URL, gate the tenant,
#   run the bootstrap writes under tenant context, and print the runbook-facing
#   summary. Translates operator/domain errors into exit code 2.
# Database/ORM: One read-only tenant lookup session plus one write session; all
#   SQL is owned by the repositories and the OrgUnitORM inserts.
# Standards: Thin entrypoint; typed domain errors -> exit 2. Every reachable
#   engine-construction failure exits 2 rather than raising: an unparseable URL
#   and an unknown dialect through SQLAlchemyError, an uninstalled DBAPI module
#   through ImportError, a non-numeric port through the bare ValueError
#   SQLAlchemy's parser raises. The SUMMARY does not echo the database URL: it
#   REBUILDS it from non-credential components via ``_redact_database_url``, so
#   no password — userinfo, query parameter or otherwise — reaches the console,
#   and a URL that cannot be decomposed is withheld instead of guessed at.
#   Argparse usage errors are covered by ``_RedactingArgumentParser`` /
#   ``_redact_argparse_message``, which fail-closed on credential-bearing tokens
#   including whitespace-split password fragments.
# Blast Radius: Operator surface. The writes it drives touch authorization
#   (identity, optional role) and the org registry.
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> build_session_factory.
#   - File: Docs/21_BETA_IMPLEMENTATION_PLAN.md -> P0.8 / P0.9.
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Bootstrap the operator identity and return the operator exit code."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    deps = _load_dependencies()
    try:
        settings = deps["load_app_settings"]()
    except ValueError:
        # Settings validation text can echo rejected values such as database
        # URLs, so keep the terminal message stable and value-free.
        print("ValueError: invalid operator settings", file=sys.stderr)
        return 2

    database_url = args.database_url or settings.database_url
    if not database_url:
        print(
            "A database URL is required: pass --database-url or set UMS_DATABASE_URL.",
            file=sys.stderr,
        )
        return 2

    tenant_id = args.tenant or UUID(deps["UMS_TENANT_ID"])
    try:
        session_factory = deps["build_session_factory"](database_url)
    except deps["SQLAlchemyError"] as exc:
        # FIX: A malformed URL dies inside create_engine BEFORE any handling
        # below is reachable, so the operator got a raw traceback instead of the
        # exit 2 this entrypoint promises. Reproduced with `--database-url
        # not-a-url` -> `sqlalchemy.exc.ArgumentError: Could not parse SQLAlchemy
        # URL from given URL string`. The exception TEXT is not echoed, for the
        # same reason the summary redacts the URL: an unparseable string is
        # still a string the operator may have pasted a password into.
        print(
            f"{type(exc).__name__}: --database-url / UMS_DATABASE_URL is not a "
            "usable SQLAlchemy URL. Expected the form "
            "postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME.",
            file=sys.stderr,
        )
        return 2
    except ImportError as exc:
        # FIX: a REGISTERED dialect whose DBAPI module is absent raises
        # ModuleNotFoundError out of create_engine, not SQLAlchemyError, so the
        # handler above never saw it and the operator got a raw traceback and
        # exit 1 — breaking any runbook that branches on the promised exit 2.
        # Unknown DIALECTS were already correct (NoSuchModuleError -> exit 2);
        # this matches them. Reproduced with `--database-url
        # postgresql+psycopg2://...` -> `ModuleNotFoundError: No module named
        # 'psycopg2'`, which is the most commonly pasted PostgreSQL prefix while
        # this project ships psycopg v3. Only ``exc.name`` is echoed: it is the
        # module name SQLAlchemy's own dialect tried to import, never operator
        # text, so it cannot carry the credential the URL may hold.
        missing = getattr(exc, "name", None) or "the DBAPI module"
        print(
            f"{type(exc).__name__}: {missing} is not installed, so "
            "--database-url / UMS_DATABASE_URL names a driver this deployment "
            "cannot load. This project ships psycopg v3: use the "
            "postgresql+psycopg:// prefix, not postgresql+psycopg2://.",
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        # FIX: SQLAlchemy's own URL parser reaches int(components["port"]) and
        # raises a BARE ValueError -- not ArgumentError -- for a non-numeric
        # port, so neither handler above caught it and the operator got a raw
        # traceback and exit 1. Same contract violation as the ImportError case
        # one exception type over, found by fuzzing _redact_database_url and
        # then reproduced against main() with `--database-url
        # postgresql+psycopg://u:p@127.0.0.1:notaport/ums`. Kept LAST so the
        # narrower handlers above still win; ValueError is broad enough that
        # catching it earlier would swallow them.
        #
        # The exception text is not echoed. It is not known to carry the
        # credential today -- the reproduction above shows the password absent
        # from the traceback -- but this handler exists precisely because the
        # parser surprises us, so it withholds by default rather than relying
        # on that staying true.
        print(
            f"{type(exc).__name__}: --database-url / UMS_DATABASE_URL could not "
            "be parsed. Check the port is numeric and the URL has the form "
            "postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME.",
            file=sys.stderr,
        )
        return 2
    tenant, tenant_error = _load_active_tenant(session_factory, deps, tenant_id)
    if tenant is None:
        print(tenant_error, file=sys.stderr)
        return 2

    try:
        accounts = _resolve_accounts(args)
        users, org_units = _run_bootstrap(
            session_factory,
            deps,
            tenant=tenant,
            accounts=accounts,
            role_key=args.role,
            org_skeleton=args.org_skeleton,
            sector_name=args.sector_name,
            company_name=args.company_name,
        )
    except ValueError as exc:
        print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
        # FIX: The repository maps EVERY SQLAlchemy failure — including a
        # PostgreSQL row-level-security rejection — onto the deliberately
        # value-free "storage unavailable" message. That is correct fail-closed
        # behaviour and is not softened here, but on its own it leaves the
        # operator with no idea which of two very different causes they hit.
        # UserAccountError / UserRoleAssignmentError both subclass ValueError,
        # so naming them alongside ValueError only triggered PYL-W0714.
        if isinstance(exc, deps["UserAccountStorageError"]):
            print(
                "On PostgreSQL this also covers a row-level-security rejection: "
                "check that the login in UMS_DATABASE_URL is a member of the "
                "app_tenant / app_platform roles created by migration "
                "20260608_0001, and that `alembic upgrade head` has been run.",
                file=sys.stderr,
            )
        return 2
    except deps["SQLAlchemyError"] as exc:
        # FIX: The tuple above covered only the repository-raised domain errors.
        # ``_ensure_org_unit`` writes org_units with direct ORM SQL and
        # ``_require_seeded_role`` reads roles with ``session.get``, neither
        # behind a repository that maps SQLAlchemy failures onto a typed error,
        # and ``UserRoleAssignmentError`` derives from ValueError rather than
        # wrapping SQLAlchemy at all (auth/user_roles.py:55). A mid-run
        # connection loss, statement timeout, deadlock or permission-denied on
        # those paths therefore escaped as a raw traceback — reproduced against a
        # database missing org_units: `sqlalchemy.exc.OperationalError:
        # (sqlite3.OperationalError) no such table: org_units`. The exception
        # TEXT is deliberately not echoed here either: a connection failure can
        # carry the host, port and username the summary takes care to redact,
        # and a statement error can carry bound parameter values.
        print(
            f"{type(exc).__name__}: the bootstrap transaction failed against the "
            "database and nothing was committed. Check the database is still "
            "reachable, that `alembic upgrade head` has been run against it, and "
            "that the login in UMS_DATABASE_URL is a member of the app_tenant / "
            "app_platform roles created by migration 20260608_0001.",
            file=sys.stderr,
        )
        return 2

    print(_BANNER)
    print(f"UMS operator bootstrap — tenant {tenant.id} ({tenant.slug})")
    print(f"database: {_redact_database_url(database_url)}")
    print(_BANNER)
    _print_user_summary(users, role_key=args.role)
    _print_org_summary(org_units)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
