# ============================================================================
# Purpose: The API-boundary permission gate shared by the finance route
#   modules — translate a has_permission denial into the uniform HTTP 403.
#   Extracted from api/revenue.py so sibling route modules (reconciliation)
#   stop importing another route module's internals (the api-layering
#   refactor).
# Database/ORM: None; pure policy evaluation over the already-loaded
#   principal and the caller-supplied org-access index.
# Standards: Fail closed; the 403 detail carries ONLY the missing
#   permission value — the same message whether the target exists or not, so
#   unauthorized probes and unauthorized reads stay indistinguishable
#   (multiple recorded review rulings depend on this exact message shape).
#   raise_missing_permission is the unconditional variant for gates that
#   decide denial themselves.
# Blast Radius: Every route gate that imports these; a behavior change here
#   changes 403 semantics across the finance API surface.
# Connections:
#   - File: backend/ums_smart_revenue/auth/policy.py -> has_permission, the
#     policy evaluation this gate wraps.
#   - File: backend/ums_smart_revenue/api/revenue.py -> route gates and the
#     deny-only snapshot re-checks; the module these helpers came from.
#   - Consumers: the finance route modules that previously carried private
#     byte-identical copies (reconciliation, exports, adsense, allocation,
#     audit, channel_account_links, exchange_rates, finance_close, reports).
#     users.py's _require_permission_grant_policy is a DIFFERENT check
#     (grant-policy shape, not scoped access) and deliberately stays local.
#   - Naming: auth/api_guards.py exports an unrelated require_permission
#     GUARD FACTORY (returns a callable over GuardContext, raises
#     AccessDeniedError). Call shapes differ, so a wrong import fails loudly.
# ============================================================================
"""Shared API-boundary permission gates: uniform HTTP 403 on denial."""

from fastapi import HTTPException, status

from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex


def require_permission(
    user: UserPrincipal,
    permission: Permission,
    scope: AccessScope,
    org_index: OrgAccessIndex | None = None,
) -> None:
    """Raise HTTP 403 if the principal does not hold the given permission for the given scope."""
    if not has_permission(user, permission, scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


def raise_missing_permission(permission: Permission) -> None:
    """Unconditionally raise HTTP 403 for a missing permission without revealing caller details."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {permission.value}",
    )
