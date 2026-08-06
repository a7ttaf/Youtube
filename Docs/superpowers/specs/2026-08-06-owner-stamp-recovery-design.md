# Owner-stamp recovery — design

Date: 2026-08-06
Status: approved for planning

## Context

PR #169 (squash `a84b885b`) made `channel_groups.content_owner_id` the scope
key for CMS group sync, with an adopt-only, monotonic contract: NULL → owner
is adoption (allowed), owner → different owner is refused
(`ChannelGroupOwnerReassignmentError`), and nothing clears a stamp. Its
handoff names two open threads this PR closes as a pair:

1. **Path A — import adoption refusal.** The import still adopts owner-NULL
   groups when a CSV row's `group_id` targets one, stamping the request's
   `content_owner_id` on evidence no stronger than a spreadsheet cell. Path B
   (shipped) only *discloses* this (`will_adopt_content_owner`). The handoff's
   verdict stands: "a CSV cell is not Google co-signing an ownership claim."
2. **Clear-stamp admin action.** A wrong stamp is unrecoverable through the
   API today. With Path A closing the import's ability to mint stamps onto
   existing groups, an explicit admin clear becomes the sole remedy for a
   wrong one — currently it requires raw SQL.

Together they complete the ownership lifecycle: **sync is the only writer of
stamps on existing groups; the admin clear is the only eraser.**

## Goal

- An import row targeting an existing **owner-NULL** group is refused at
  planning (row-level `ERROR`, blocking the apply like any other row error),
  with a reason directing the operator to run the owner's group sync first.
- A new admin action clears a group's `content_owner_id`, returning it to the
  adoptable pool so the *correct* owner's next sync re-adopts it.

## Non-goals

- Changing group **creation** semantics. A CSV `group_id` with no local
  counterpart still creates the group with the owner stamped at birth — the
  request-level `content_owner_id` covers everything the row creates, and a
  wrong birth-stamp now surfaces as `CONFLICT` at the real owner's next sync
  plan. Path A is scoped exactly to *adoption of existing groups*, per the
  handoff.
- Owner *reassignment* (owner → different owner stays refused; recovery is
  clear-then-resync).
- Touching sync semantics, membership deltas, or the lockdown.

## Part 1 — Path A: refuse import adoption

`org/channel_import.py::_blocked_group_reason` gains the third predicate the
handoff predicted. Rows whose `group_id` is in `adoptable_group_ids` (already
computed and threaded for Path B) become row errors:

```
channel group {group_id} exists without a content owner; run
POST /channels/groups/sync for content owner {content_owner_id} to adopt it,
or clear/archive the group if it is stale
```

Consequences, deliberate:

- **Blocking, not warning.** Row errors block the whole apply (the import's
  all-or-nothing contract). A warning that still stamps would be Path B with
  extra steps.
- **`will_adopt_content_owner` is removed**, along with its rendering. Path A
  makes it unreachable — a permanently-false response field is API noise, the
  API is internal, and nothing in the frontend consumes it. The
  `list_adoptable_cms_group_ids` store read stays: planning still needs to
  know which keys are adoptable to produce the refusal.
- **Write boundary keeps its own guard.** `require_adoptable_owner` still
  protects the store; planning refusal is UX, the store contract is safety.
  A group whose stamp is cleared between plan and apply now fails the apply's
  locked recheck the same way other mid-flight surprises do.
- **Legacy label-keyed groups** (owner-NULL, minted by pre-sync CSVs, keys
  that don't exist upstream) become un-importable-into: sync will never adopt
  them (their key never appears upstream), so the refusal's "run sync" remedy
  does not apply. That is intended pressure: the remedies are archive the
  stale group (`active`-only PATCH is allowed) or move the CSV to real
  YouTube group ids. The refusal text's "or clear/archive the group if it is
  stale" names it.

## Part 2 — clear-stamp admin action

**Route:** `DELETE /groups/{group_id}/content-owner?reason=...` in
`api/groups.py`, following the `remove_group_member` shape (reason as a
required query param, same normalization).

**Permission:** `MANAGE_GROUPS` at **global** scope, fail-closed — stricter
than the per-group manageability check the other group routes use, because
clearing a stamp changes which *content owner's sync* governs the group,
which is tenant-level governance, not group curation. (Same gate as the sync
route itself.)

**Semantics:**

- Clears `content_owner_id` only. `cms_group_id`, name, members, `active` are
  untouched. The group remains synced-locked for manual edits (it still
  carries `cms_group_id`) and becomes adoptable by the next sync whose
  upstream snapshot contains its key — that is the recovery loop:
  wrong stamp → clear → correct owner's sync re-adopts with Google as the
  evidence.
- 404 unknown/cross-tenant group (existing `_registry_not_found` mapping).
- **409 when the group has no stamp** (`content_owner_id IS NULL`): nothing
  to clear. Typed detail; idempotent-200 would hide operator confusion about
  which group they are pointing at.
- Store: new `clear_content_owner(group_id)` on the Protocol + both stores.
  SQL implementation reads the row `FOR UPDATE` (the `get_group_by_cms_id`
  precedent) so a concurrent sync adopt and a clear serialize; whichever
  commits second wins its transaction's view, and both end states are
  legitimate (cleared, or stamped by the sync that raced — the operator
  re-clears if the race stamped the wrong owner again).
- Deliberately **not** wired through `require_adoptable_owner` — that guard
  governs setting; this is the erase path it must not block.

**Audit:** one `GROUP_UPDATED` (reason required — satisfied by the query
param) with details
`{"action": "content_owner_cleared", "cms_group_id": ..., "previous_content_owner_id": ...}`.
No new event type: this is group governance, not a sync run, and
`GROUP_UPDATED` is already the per-group governance event. Atomicity: the
route uses `current_atomic_audit_sink` — it writes a domain row + an audit
row in one request, so the #169 invariant applies.

## Error taxonomy

| Condition | Status |
| --- | --- |
| Missing global `MANAGE_GROUPS` | 403 |
| Unknown / cross-tenant group | 404 |
| Group has no owner stamp | 409 |
| Blank reason | 422 (existing query-reason normalization) |
| Import row targeting an adoptable group | row `ERROR` → apply 422 |

## Testing

- **Pure planner:** adoptable key → row error with the directive reason; the
  other two predicates unchanged; `will_adopt_content_owner` gone from the
  dataclass and rendering (grep-level absence check in tests).
- **Import API (SQLite):** a roster targeting an owner-NULL group 422s on
  apply AND on dry-run shows the row error; the same roster after the group
  is cleared-and-resynced (simulated by stamping via the store) imports
  cleanly under the right owner; birth-creation path unchanged.
- **Groups API (SQLite):** 403 without global grant; 404 unknown; 409
  no-stamp; happy path clears, audits (`action`, `previous_content_owner_id`),
  and leaves name/members/active/cms key untouched; the cleared group is
  adoptable again (a following sync apply adopts it); manual groups (no
  `cms_group_id`) with a NULL owner also 409 (no stamp) — the route does not
  care whether the group is synced, only whether a stamp exists.
- **Postgres tier:** clear + concurrent-adopt serialization via `FOR UPDATE`
  (two-session test in the existing PG-tier style), and audit atomicity for
  the new route (lost-commit test per the #169 invariant).

## Tracker updates (per-PR rule)

- `Docs/15_DELIVERY_BACKLOG.md`: extend the CMS-group-sync entry — Path A
  shipped (import no longer adopts; sync is the only stamp-writer on existing
  groups) and the clear-stamp remedy exists.
- `Docs/01_IMPLEMENTATION_PLAN.md`: one-line note on the group-mapping item.

## Rollback

Revert the branch. No migration. Reverting restores Path B behaviour
(disclosed adoption) and removes the clear route; stamps already cleared stay
cleared — which is correct data either way.
