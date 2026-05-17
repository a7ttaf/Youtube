"""Constants for the tenancy domain that downstream layers depend on.

Keeping these in a tiny, dependency-free module means the Alembic
migrations + the ORM models + the application code all reference the
*same* string, with zero risk of drift across copy-pasted literals.
"""

from __future__ import annotations

#: The deterministic UUID seeded as tenant #1 by migration
#: ``20260516_0001_tenants_foundation``. Used as the
#: ``server_default`` for every ``tenant_id`` column added in
#: ``20260517_0001`` so that existing data, pre-existing test fixtures,
#: and any insert paths that haven't yet learned about the resolver
#: continue to write to tenant ``ums`` rather than failing.
UMS_TENANT_ID: str = "00000000-0000-0000-0000-000000000001"
