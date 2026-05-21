from collections.abc import Callable
from dataclasses import dataclass

from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex


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
        if not has_permission(
            context.user, permission, context.scope, context.org_index
        ):
            raise AccessDeniedError(
                message or f"Missing permission: {permission.value}"
            )

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


def guarded_call[T](
    guard: Callable[[GuardContext], None],
    context: GuardContext,
    handler: Callable[[], T],
) -> T:
    guard(context)
    return handler()
