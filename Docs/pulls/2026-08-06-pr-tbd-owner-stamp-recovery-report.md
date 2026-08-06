# PR TBD — Owner-Stamp Recovery — Report

Branch: `feat/owner-stamp-recovery`
Base: `origin/main` at `a84b885b` (PR #169 squash)
Date: 2026-08-06

## Scope

Close the group-ownership lifecycle PR #169 opened. Two coupled changes its
handoff named as the remaining threads:

1. **Path A — the import refuses adoption.** A CSV row whose `group_id`
   targets an existing owner-NULL group is now a row-level `ERROR` (blocking
   the whole apply, per the import's all-or-nothing contract) with a reason
   naming the remedy: run that owner's `POST /channels/groups/sync`. The
   handoff's verdict implemented: a CSV cell is not Google co-signing an
   ownership claim. The Path B disclosure field `will_adopt_content_owner`
   is removed from the import response — Path A makes it unreachable. It
   **remains on the group-sync response**, where adoption is legitimate.
2. **Clear-stamp admin action.** `DELETE /groups/{group_id}/content-owner`
   (reason as query param) erases a wrong stamp, returning the group to the
   adoptable pool so the correct owner's next sync re-adopts it with Google
   as the evidence. Previously a wrong stamp required raw SQL.

End state: **sync is the only writer of owner stamps on existing groups; the
admin clear is the only eraser.** Group creation (a CSV `group_id` with no
local counterpart) still stamps at birth — a wrong birth-stamp surfaces as
`CONFLICT` at the real owner's next sync plan. No migration.

## Behaviour

**Path A planning refusal.** Third predicate in `_blocked_group_reason`
(joining archived and foreign-owner): adoptable keys produce
`channel group {id} exists without a content owner; run POST
/channels/groups/sync for content owner {co} to adopt it, or clear/archive
the group if it is stale`. Dry-run shows it; apply 422s.

**Write-boundary fail-closed.** Planning refusal covers what is knowable at
plan time; a stamp cleared between plan and apply would make the locked
re-read see owner-NULL and (previously) adopt after all. New typed
`ChannelImportAdoptableGroupError` → 409 with a canned detail, mirroring the
archived-group precedent. Covered by an apply-layer test and a route test.

**Clear route.** Global `MANAGE_GROUPS`, fail-closed — clearing a stamp
changes which owner's sync governs the group, which is tenant-level
governance, not group curation. Clears the stamp only (name, members,
`active`, `cms_group_id` untouched); 404 unknown; **409 when there is no
stamp to clear** (idempotent-200 would hide operator confusion); blank
reason 422. Works on archived groups — verified `get_group` sees inactive
rows (the neighbouring list reads do not; using one of those would have made
an archived group's stamp permanently unclearable). Audit: one
`GROUP_UPDATED` via the **atomic sink** (the #169 invariant — this route
writes a domain row + an audit row in one request) with
`{"action": "content_owner_cleared", "cms_group_id", "previous_content_owner_id"}`.

**Store.** `clear_content_owner(*, group_id)` on the Protocol and both
stores; SQL takes the row via the existing `for_update=True` idiom so a
concurrent sync-adopt serializes; `ChannelGroupNoOwnerStampError` when NULL.
Deliberately not routed through `require_adoptable_owner` — that guard
governs setting; this is the sanctioned eraser.

## Recovery loop, end to end

wrong stamp → `DELETE /groups/{id}/content-owner` → correct owner's sync
re-adopts. Proven twice: SQLite (route-level round-trip with a fake CMS
snapshot) and Postgres (real engine, real RLS lanes).

## Validation

All local against Postgres 18.

| Gate | Result |
| --- | --- |
| Full suite `pytest -q` with Postgres | 2688 passed, 0 failed |
| `ruff check backend tests scripts` | All checks passed |
| `ruff format --check` (14 touched `.py`) | All formatted |
| 100-char guard | No violations |
| Alembic (no migration in PR) | single head `20260805_0001` |
| `git diff --check` | Clean |

Postgres tier (3 new tests): clear persists + re-adoption on the real
engine; **clear-vs-adopt serialization proven with `pg_blocking_pids()`**
(the blocked backend is observed reporting the clearing backend as its
blocker; a mutant probe with the lock dropped fails the test, so the proof
is falsifiable, not decorative); lost-commit audit atomicity for the new
route (in-flight probe shows the audit row existed inside the transaction
and died with it).

## Notes for reviewers

- The plan's original "grep for `will_adopt` must be empty" gate was wrong —
  the group-sync surfaces legitimately keep that field (sync is the adopter
  Path A's error message points to). The implementer caught this and scoped
  the removal to import surfaces only.
- One pre-existing sharp edge documented, not fixed: `update_group` reads
  its row unlocked and relies on the CALLER holding the row lock (the sync
  apply does). A future bare-`update_group` adopter would get a
  stale-snapshot rejection rather than serializing. Correct today.
- Pre-existing, out of scope: the older group routes still use the plain
  audit sink (they predate the atomic invariant). The new route uses the
  atomic sink.
- Doc lag fixed in this PR: `Docs/12_BACKEND_API_SPEC.md` import-response
  description rewritten for Path A; a stale "no API remedy today" comment in
  `channel_group_sync_apply.py` now names the clear route.

## Rollback

Revert the branch. No migration. Reverting restores Path B (disclosed
adoption) and removes the clear route; stamps already cleared stay cleared —
correct data either way.
