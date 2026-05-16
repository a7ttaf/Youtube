"""Shared actor-identity helpers for repository writes.

Trusted-gateway header authentication can deliver `x-user-id` values that
are not standard UUIDs (system integration accounts, slug-style service
subjects, etc.). Repository methods that persist actor attribution into a
UUID column would otherwise reject those subjects with 422, even though
the principal is fully authorized.

`actor_identity_uuid` accepts both forms:
    * a literal UUID string is returned as-is
    * any other non-blank string is mapped deterministically via uuid5 in
      the trusted-gateway-actor namespace, so the same gateway identity
      always maps to the same UUID column value across calls

This keeps audit attribution stable and consistent with the long-standing
`reports.exports._actor_identity_uuid` pattern, while letting downstream
modules raise their own validation-error types via the `ValueError`
raised here when the input is missing/blank.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

GATEWAY_ACTOR_NAMESPACE = uuid5(
    NAMESPACE_URL, "ums-smart-revenue:trusted-gateway-actor"
)


def actor_identity_uuid(value: str, *, field_name: str = "actor_user_id") -> UUID:
    """Map an actor identifier to a UUID.

    Returns the parsed UUID when ``value`` is a UUID string; otherwise
    returns a deterministic uuid5 derived in
    :data:`GATEWAY_ACTOR_NAMESPACE`. Raises ``ValueError`` when ``value``
    is not a non-blank string so the caller can wrap with its module's
    domain validation error.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    try:
        return UUID(normalized)
    except ValueError:
        return uuid5(GATEWAY_ACTOR_NAMESPACE, normalized)
