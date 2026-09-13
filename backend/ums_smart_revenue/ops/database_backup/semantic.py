"""Canonical authorization semantics used by database recovery gates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from ums_smart_revenue.auth.permissions import PERMISSION_DEFINITIONS
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS
from ums_smart_revenue.auth.seed import initial_role_permission_rows

AuthorizationPayload = dict[str, list[dict[str, str | bool]]]


# ============================================================================
# Purpose: Materialize the runtime authorization registries in the same stable
#   scalar shape stored by roles, permissions, and role_permission_assignments.
# Database/ORM: Canonical metadata for the three global authorization tables.
# Standards: Runtime registries are the only expected-value source; ordering and
#   JSON encoding are deterministic and no row-count literal is duplicated.
# Blast Radius: Authorization recovery; catalog drift causes backup refusal.
# Connections:
#   - File: backend/ums_smart_revenue/auth/roles.py -> role metadata registry.
#   - File: backend/ums_smart_revenue/auth/permissions.py -> permission registry.
#   - File: backend/ums_smart_revenue/auth/seed.py -> exact grant edges.
# ============================================================================
def canonical_authorization_payload() -> AuthorizationPayload:
    """Return the exact runtime authorization catalog in database column shape.

    Returns:
        ``AuthorizationPayload``.
    """
    roles: list[dict[str, str | bool]] = [
        {
            "key": role.value,
            "label": definition.label,
            "description": definition.description,
            "service_only": definition.service_only,
        }
        for role, definition in sorted(ROLE_DEFINITIONS.items(), key=lambda item: item[0].value)
    ]
    permissions: list[dict[str, str | bool]] = [
        {
            "key": permission.value,
            "label": definition.label,
            "sensitive": definition.sensitive,
            "audit_on_use": definition.audit_on_use,
        }
        for permission, definition in sorted(
            PERMISSION_DEFINITIONS.items(), key=lambda item: item[0].value
        )
    ]
    assignments: list[dict[str, str | bool]] = [
        {
            "role_key": row["role"],
            "permission_key": row["permission"],
        }
        for row in initial_role_permission_rows()
    ]
    return {
        "roles": roles,
        "permissions": permissions,
        "role_permission_assignments": assignments,
    }


def authorization_catalog_digest(payload: AuthorizationPayload) -> str:
    """Hash one normalized catalog without timestamps or database-generated ids.

    Args:
        payload: AuthorizationPayload. Canonical authorization catalog payload bytes.

    Returns:
        ``str``.
    """
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def payload_from_database_rows(
    *,
    roles: Sequence[tuple[str, str, str, bool]],
    permissions: Sequence[tuple[str, str, bool, bool]],
    assignments: Sequence[tuple[str, str]],
) -> AuthorizationPayload:
    """Normalize validated PostgreSQL scalar rows for exact registry comparison.

    Args:
        roles: Sequence[tuple[str, str, str, bool]].
        permissions: Sequence[tuple[str, str, bool, bool]].
        assignments: Sequence[tuple[str, str]].

    Returns:
        ``AuthorizationPayload``.
    """
    return {
        "roles": [
            {
                "key": key,
                "label": label,
                "description": description,
                "service_only": service_only,
            }
            for key, label, description, service_only in roles
        ],
        "permissions": [
            {
                "key": key,
                "label": label,
                "sensitive": sensitive,
                "audit_on_use": audit_on_use,
            }
            for key, label, sensitive, audit_on_use in permissions
        ],
        "role_permission_assignments": [
            {"role_key": role_key, "permission_key": permission_key}
            for role_key, permission_key in assignments
        ],
    }
