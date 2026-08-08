from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex

T = TypeVar("T")


class AccessDeniedError(PermissionError):
    pass


@dataclass(frozen=True)
class GuardContext:
    user: UserPrincipal
    scope: AccessScope
    org_index: OrgAccessIndex


def require_permission(
    permission: Permission,
    *,
    message: str | None = None,
) -> Callable[[GuardContext], None]:
    def guard(context: GuardContext) -> None:
        if not has_permission(context.user, permission, context.scope, context.org_index):
            raise AccessDeniedError(message or f"Missing permission: {permission.value}")

    return guard


def require_predicate(
    predicate: Callable[[UserPrincipal], bool],
    *,
    message: str,
) -> Callable[[GuardContext], None]:
    def guard(context: GuardContext) -> None:
        if not predicate(context.user):
            raise AccessDeniedError(message)

    return guard


def guarded_call(  # noqa: UP047 - DeepSource/Pylint does not parse PEP 695 syntax yet.
    guard: Callable[[GuardContext], None],
    context: GuardContext,
    handler: Callable[[], T],
) -> T:
    """Run ``guard`` against ``context``, then return the result of ``handler()``.

    Fail-closed: the guard runs first and, when it denies, raises
    :class:`AccessDeniedError` before ``handler`` is invoked, so no guarded work
    runs on a denied call. What counts as a denial is the guard's to decide --
    :func:`require_permission` denies a missing permission, while
    :func:`require_predicate` denies whenever the caller's predicate is false.
    Any other exception raised by the guard propagates unchanged; ``handler``
    exceptions are likewise left to the caller.
    """
    guard(context)
    return handler()
