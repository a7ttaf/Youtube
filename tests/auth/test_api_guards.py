"""Guard-composition contract for ``ums_smart_revenue.auth.api_guards``.

Pins the fail-closed promise documented on ``guarded_call``: a denying guard
raises before the handler runs, so no protected work executes on a denied call.
"""

import pytest

from ums_smart_revenue.auth.api_guards import (
    AccessDeniedError,
    GuardContext,
    guarded_call,
    require_permission,
    require_predicate,
)
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex


def _principal(*grants: PermissionGrant, disabled: bool = False) -> UserPrincipal:
    return UserPrincipal(
        user_id="user-1",
        email="user@example.com",
        role_assignments=[],
        direct_permissions=list(grants),
        disabled=disabled,
    )


def _context(user: UserPrincipal) -> GuardContext:
    return GuardContext(
        user=user,
        scope=AccessScope.global_scope(),
        org_index=OrgAccessIndex(),
    )


def _granted(permission: Permission) -> PermissionGrant:
    return PermissionGrant(permission=permission, scope=AccessScope.global_scope())


def test_require_permission_passes_when_the_principal_holds_the_grant():
    context = _context(_principal(_granted(Permission.VIEW_REVENUE)))

    assert require_permission(Permission.VIEW_REVENUE)(context) is None


def test_require_permission_denies_when_the_grant_is_missing():
    context = _context(_principal())

    with pytest.raises(AccessDeniedError, match="finance.view_revenue"):
        require_permission(Permission.VIEW_REVENUE)(context)


def test_require_permission_denies_a_disabled_principal_holding_the_grant():
    context = _context(_principal(_granted(Permission.VIEW_REVENUE), disabled=True))

    with pytest.raises(AccessDeniedError):
        require_permission(Permission.VIEW_REVENUE)(context)


def test_require_predicate_denies_with_the_supplied_message():
    guard = require_predicate(lambda user: not user.disabled, message="principal is disabled")
    context = _context(_principal(disabled=True))

    with pytest.raises(AccessDeniedError, match="principal is disabled"):
        guard(context)


def test_guarded_call_returns_the_handler_result_when_the_guard_passes():
    context = _context(_principal(_granted(Permission.VIEW_REVENUE)))

    result = guarded_call(
        require_permission(Permission.VIEW_REVENUE),
        context,
        lambda: "handler-ran",
    )

    assert result == "handler-ran"


def test_guarded_call_never_runs_the_handler_when_the_guard_denies():
    context = _context(_principal())
    calls: list[str] = []

    def handler() -> str:
        calls.append("handler-ran")
        return "handler-ran"

    with pytest.raises(AccessDeniedError):
        guarded_call(require_permission(Permission.VIEW_REVENUE), context, handler)

    assert calls == []


def test_guarded_call_propagates_a_non_access_error_raised_by_the_guard():
    context = _context(_principal())
    calls: list[str] = []

    def exploding_guard(_: GuardContext) -> None:
        raise RuntimeError("org index unavailable")

    def handler() -> str:
        calls.append("handler-ran")
        return "handler-ran"

    with pytest.raises(RuntimeError, match="org index unavailable"):
        guarded_call(exploding_guard, context, handler)

    assert calls == []
