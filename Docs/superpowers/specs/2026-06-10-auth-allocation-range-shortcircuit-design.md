# Authorization iteration cap: global-grant short-circuit (PR #57 N10)

**Date:** 2026-06-10
**Branch:** `feat/auth-allocation-range-shortcircuit` (off main `e92efd2`, #88)
**Status:** Approved direction (Mahmoud: "proceed", recommendations pre-approved)

## Problem

`_require_allocation_permission_for_range(user, start, end)` in
`backend/ums_smart_revenue/api/channel_account_links.py` (~lines 99-121) short-circuits only the
**open-ended** (`end is None`) path with a single global permission check. The **bounded** path
loops per month with a `finance_month`-scoped check and has NO global-grant short-circuit. A link
whose `effective_month_end` is a far-future value (operator-settable up to `9999-12` — the propose
validator caps only month 1..12, not the year) drives ~95k in-memory authorization iterations even
for a caller who holds the permission at **global** scope. The repository already de-iterated the
analogous month-lock DoS (`finance/channel_account_links.py:236-295`); this is its authz-layer twin.

## Why the short-circuit is provably safe (no added permissiveness)

Authorization reduces to `OrgAccessIndex.contains(granted_scope, target_scope)`
(`auth/policy.py:41-54`). `auth/scopes.py:57-59` returns `True` unconditionally when
`granted_scope.type == GLOBAL`. Therefore a `CHANGE_ALLOCATION_RULE` grant at `global_scope()` is a
strict superset of any per-`finance_month` grant: if the global check passes, **every** per-month
check would also pass. Replacing ~95k passing per-month checks with one global check yields the
**identical** authorization decision — pure dead-iteration elimination, not a policy relaxation.

**Safety precondition (must hold):** the short-circuit checks the SAME permission
(`CHANGE_ALLOCATION_RULE`) at `global_scope()` using the **non-raising** `has_permission(...)`, and
`return`s only on success. On failure it MUST fall through to the existing per-month loop, so a
legitimately month-scoped caller is still gated month-by-month. (A `disabled` user fails
`has_permission` first — `policy.py:36-37` — so a disabled global-grant holder falls through and is
correctly denied at the first month. Fail-closed preserved.)

## Change

In `_require_allocation_permission_for_range`, at the top of the bounded branch (before the
`for month in _iter_months(...)` loop), add:

```py
# FIX (PR #57 N10): a CHANGE_ALLOCATION_RULE grant at global scope authorizes
# every finance month (OrgAccessIndex.contains returns True for a GLOBAL granted
# scope against any target, auth/scopes.py), so it is a strict superset of the
# per-month checks below. Short-circuit it once to avoid ~95k in-memory authz
# iterations for an authorized far-future bounded effective_month_end (e.g.
# 9999-12). Use the NON-raising has_permission so a non-global caller falls
# through to the per-month loop and is still gated month-by-month.
if has_permission(user, Permission.CHANGE_ALLOCATION_RULE, AccessScope.global_scope()):
    return
```

Leave the open-ended (`end is None`) branch untouched (it must stay global-only fail-closed —
a finite month set can never cover `[start, ∞)`). Do NOT use the raising `_require_permission` for
the short-circuit (that would 403 a legitimately month-scoped caller instead of falling through).

## Out of scope (deferred)

The propose-time range cap (rejecting a far-future `effective_month_end` at the validation
boundary) is a separate optional hardening with its own product/validation review — NOT built here.
This PR removes the authz iteration cost for the caller who triggers it (the global-grant holder)
without changing which ranges are acceptable.

## Tests (authz matrix — `tests/api/test_channel_account_links_api.py`)

Preserve the existing assertions at ~358-404. Add (use `monkeypatch` on the module's `_iter_months`
/ `has_permission` to assert the short-circuit):
- **(a) missing auth → fail-closed:** no `CHANGE_ALLOCATION_RULE` grant, bounded range → 403; also a
  **disabled** global-grant holder → falls through → 403.
- **(b) month-scoped grant only → still iterates / correct 403 on an uncovered month:** preserve the
  existing `2026-01..2026-06` → 403; assert `_iter_months` WAS called (loop ran, short-circuit did
  not fire).
- **(c) global-scoped grant on a bounded range → passes WITHOUT per-month iteration:** assert no
  raise AND `_iter_months` was NEVER called (spy). Use a far-future bound (`2026-01..9999-12`) so a
  regression would hang/timeout — making the optimization assertion self-evident.
- **(d) bounded-range correctness preserved for non-global callers:** a month-scoped caller granted a
  contiguous range passes every covered month; extending past the grant → 403 at the uncovered month.

Note in the PR: `_require_allocation_permission_for_range` does no storage I/O (`has_permission` is
pure in-memory over the loaded `UserPrincipal`), so there is no per-function fail-closed
storage-error path to test here — that lives in principal loading and is already covered.

## Blast-radius review (authorization)

- **Authorization more permissive?** **No** — the short-circuit produces the identical decision for
  every principal (global grant already passes every per-month check; proven above). It is strictly
  dead-iteration elimination. Net authz outcome unchanged.
- **Tables/models / migration:** none. Pure in-memory predicate change. No schema, no migration.
- **PostgreSQL source of truth / Neo4j:** unaffected. `No graph projection impact detected.`
- **Finance results / locks / overrides:** unaffected (this is the authz gate, not the allocation
  math).
- **Backward compatible / rollback:** yes — behavior-preserving optimization; code-only revert.
