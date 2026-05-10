from ums_smart_revenue.auth.permissions import PERMISSION_DEFINITIONS
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS


def role_metadata() -> list[dict[str, object]]:
    return [
        {
            "key": role.value,
            "label": definition.label,
            "description": definition.description,
            "serviceOnly": definition.service_only,
            "permissions": sorted(permission.value for permission in ROLE_PERMISSIONS.get(role, [])),
        }
        for role, definition in sorted(ROLE_DEFINITIONS.items(), key=lambda item: item[0].value)
    ]


def permission_metadata() -> list[dict[str, object]]:
    return [
        {
            "key": permission.value,
            "label": definition.label,
            "sensitive": definition.sensitive,
            "auditOnUse": definition.audit_on_use,
        }
        for permission, definition in sorted(PERMISSION_DEFINITIONS.items(), key=lambda item: item[0].value)
    ]

