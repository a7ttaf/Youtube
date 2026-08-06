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
reason 422; **NUL in the reason 422**. Works on archived groups — verified
`get_group` sees inactive rows (the neighbouring list reads do not; using one
of those would have made an archived group's stamp permanently unclearable).
Audit: one `GROUP_UPDATED` via the **atomic sink** (the #169 invariant — this
route writes a domain row + an audit row in one request) with
`{"action": "content_owner_cleared", "cms_group_id", "previous_content_owner_id"}`.

**Store.** `clear_content_owner(*, group_id)` on the Protocol and both
stores, returning `ClearedContentOwner(group, previous_content_owner_id)`;
SQL takes the row via the existing `for_update=True` idiom so a concurrent
sync-adopt serializes; `ChannelGroupNoOwnerStampError` when NULL.
Deliberately not routed through `require_adoptable_owner` — that guard
governs setting; this is the sanctioned eraser.

## Review fixes (post-first-push)

Seven findings across three review rounds. The first two are defects, each
fixed with a proof that fails without the fix; the third is a structural one
that was initially declined and then implemented on Mahmoud's call; the last
four were introduced BY that implementation and caught only because the
review re-ran against the fix:

1. **NUL in the query reason was an unhandled 500.** `_normalize_query_reason`
   stripped and blank-checked but did not reject `\x00`, and the reason lands
   in `audit_logs.reason` — a Postgres text column. Reproduced against Postgres
   18 before fixing: `psycopg.DataError: PostgreSQL text fields cannot contain
   NUL (0x00) bytes` raised out of the audit insert, with the stamp already
   cleared in the same transaction. `api/channels.py` rejects exactly this on
   both of its reason fields, citing `audit_logs.reason`; the query-parameter
   helper now matches (422 `reason contains a NUL character`). The fix lands in
   the shared helper, so it also closes the same hole on the pre-existing
   `DELETE /groups/{id}/members/{channel_id}` route. Guards at the API tier
   for both routes, following the import's existing NUL tests.
2. **The audited `previous_content_owner_id` could be stale.** The route read
   the group unlocked (it needs the 404 before taking a write lock) and audited
   *that* value, while the erase happens later under `FOR NO KEY UPDATE`.
   Ownership is monotonic, so exactly one interleaving diverges: the pre-read
   sees NULL, a sync-adopt commits, and the clear then erases a stamp the
   pre-read never saw — the trail records `null` for an owner that was really
   removed. `clear_content_owner` now returns the value read under its own
   lock and the route audits that. New Postgres-tier test stages the adopt in
   the pre-read window deterministically (the real store, subclassed only to
   sequence a second session at that seam); it fails on the pre-fix route with
   `assert None == 'WrongOwnerDDDDDDDDDDDD'`.

3. **The route reached into the store directly.** Initially declined on
   consistency grounds — every sibling route in `api/groups.py` calls the
   store directly, so extracting only this one looked like it would fragment
   the module. Mahmoud overruled that, correctly: consistency with an existing
   pattern is not a reason to keep new code in it. The store read, the locked
   write, and the audit row moved to
   `org/channel_group_owner_recovery.clear_group_owner_stamp`, following the
   same shape as `channel_import_apply` and `channel_group_sync_apply` (store
   + sink + actor + scope in, typed errors out). The handler is now permission
   gate → reason validation → one domain call → error-to-status mapping →
   response shaping, and touches neither the registry nor the audit sink.

   The payoff is concrete, not stylistic: `tests/org/test_channel_group_owner_recovery.py`
   exercises the whole behaviour — outcome, audit details, both typed failures,
   and the return to the adoptable pool — with no FastAPI, no `TestClient`, and
   no database, in 0.06s. That tier did not exist before, because the behaviour
   was not reachable outside HTTP.

   One contract detail changed with the move: the 404 detail is now canned
   ("Group not found") for BOTH the unknown-group case and the vanished-row
   race, where the race previously surfaced `str(exc)` through
   `_registry_not_found`. That matches what the route's own header already
   promised — `str(exc)` never reaches HTTP.

4. **The route returned an assembled dict.** Replaced with a declared
   `ClearContentOwnerResponse` (group fields + `content_owner_id` +
   `GroupAuditEventResponse`). The emitted key set was checked identical to
   the previous `to_api()`-plus-two-keys payload, so no client sees a change.
   The audit event is projected field-by-field from the `AuditRecord` rather
   than splatting `audit_record_to_api`'s `dict[str, object]` — mypy rejects
   that splat, and the explicit form makes omitting `user_id`, `details`, and
   `permission` a checked decision instead of a consequence of whichever keys
   that helper happens to return.
5. **The service signalled "not found" with a bare `KeyError`.** A non-HTTP
   caller cannot tell that apart from a lookup bug inside store internals it
   does not own. Now `ChannelGroupNotFoundError`, with the store's own
   `KeyError` translated at the service boundary so the untyped signal never
   escapes. It subclasses `LookupError` and deliberately NOT `KeyError`:
   inheriting from `KeyError` would let a bare `except KeyError` keep
   swallowing it, which is the ambiguity the typed error exists to remove.
   Pinned by `test_typed_not_found_does_not_masquerade_as_a_keyerror`.
6. **Missing contract block on the new service entry point.** The module had
   one at the top; the convention (`sql_channel_groups.py`,
   `channel_import_apply.py`) is per-function. Added directly above
   `clear_group_owner_stamp`.
7. **Missing return annotation** on the `_clear` test helper.

Findings 4–7 are the useful lesson of this PR: the FIX for a review finding
needs reviewing as much as the original code did. All four came from the
service extraction in finding 3, and none would have surfaced if the review
had stopped at the first green re-run.

## Recovery loop, end to end

wrong stamp → `DELETE /groups/{id}/content-owner` → correct owner's sync
re-adopts. Proven twice: SQLite (route-level round-trip with a fake CMS
snapshot) and Postgres (real engine, real RLS lanes).

## Validation

All local against Postgres 18.

| Gate | Result |
| --- | --- |
| Full suite `pytest -q` with Postgres | 2697 passed, 0 failed (exit 0) |
| `ruff check backend tests` | All checks passed |
| `ruff format --check` | 460 files already formatted |
| `mypy` on the changed backend modules | No issues found |
| 100-char guard | No violations |
| Alembic (no migration in PR) | single head `20260805_0001` |
| `git diff --check` | Clean |

The count rose from 2688 to 2697 with the review fixes: the Postgres-tier
stale-audit guard, two API-tier NUL-reason guards (the clear route and the
member-remove route the shared helper also protects), an in-memory store
assertion on the erased owner id, the four domain-tier tests the service
extraction made possible, and the guard proving the typed not-found error
does not masquerade as a `KeyError`.

Postgres-tier tests must not run concurrently with another pytest session
against the same container — `_purge_test_rows` is module-scoped and deletes
rows a parallel run seeded. The number above is from a run with nothing else
touching the database.

Postgres tier (4 new tests): clear persists + re-adoption on the real
engine; **clear-vs-adopt serialization proven with `pg_blocking_pids()`**
(the blocked backend is observed reporting the clearing backend as its
blocker; a mutant probe with the lock dropped fails the test, so the proof
is falsifiable, not decorative); **the audit row names the owner read under
the lock, not the route's unlocked pre-read** (an adopt is staged in the
pre-read window; the pre-fix route records `null` for an owner it really
erased); lost-commit audit atomicity for the new route (in-flight probe
shows the audit row existed inside the transaction and died with it).

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
