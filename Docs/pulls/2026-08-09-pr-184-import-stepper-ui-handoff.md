# PR #184 — CSV import stepper in Registry (PR-B) — delivery handoff

Branch `feat/import-stepper-ui`. Written at head `01aa2b58`; every figure
below was re-measured at the head that carries it, most recently after the
round-50 test additions. They are measured, not planned — and the frontend
count moves whenever a review round adds a test, so it is refreshed with the
tests rather than at the end.

## Scope

The Registry gains a three-step CSV import stepper over `ActionStepper`
(Upload → Preview → Applied), driving the existing `POST /channels/import`
route in both modes: a read-only dry run that renders the per-row plan, and an
apply that is bound to the plan the operator reviewed.

The session gains a `can_import_channels` capability, and the shell gains a
cross-document guard so an import whose outcome was never established cannot be
silently repeated.

**Non-goals.** No new backend route, no new persisted field, no migration, and
no widening of what the import may write — every backend behaviour this PR adds
is a **refusal**. No revenue math, no allocation, no month-close. The permission
model is untouched.

The **audit contract is not** — `_channel_audit_details()` now adds
`revenue_source_status: {from, to}` to `CHANNEL_CREATED` / `CHANNEL_UPDATED`
details when the write re-classified the source. It is additive to a persisted
JSON payload, and it exists so the trail can reconstruct the finance-source
transition the preview discloses; without it a `CHANNEL_UPDATED` event could
record the `revenue_required` flip while omitting the classification that flip
replaced. A reader of older events sees the key absent, which is the same thing
it means on a write that re-classified nothing.

The planner and the write boundary are **not** among the non-goals, and an
earlier draft of this section wrongly said they were. Both changed
substantially: the planner gained `group_action` and `revenue_source_status`
disclosure plus a fourth bulk group lookup, and the write boundary gained
reviewed-pre-state enforcement for bound applies, a group-effect divergence
guard that runs for **all** callers, and batched per-key group writes. See
**Behaviour changes** and **Rollback / reset** below; the unbound "the file
wins" rule of review #159 is what remains untouched.

## Files changed (34)

Counted from `git diff --name-only $(git merge-base origin/main HEAD)..HEAD`,
not from memory. It has moved repeatedly under review — 30, then 32 when round
51 added the two registry files below, then 33 when round 53 added a domain-side
apply test, then 34 when the duplicate-repeat rule brought
`tests/org/test_channel_import_parser.py` into the diff.

- **Backend (8)** — `api/channels.py` (fingerprint widened to include the
  server-resolved tenant; `expected_plan_fingerprint`; `group_action` and
  `revenue_source_status` on every plan row; contract corrections),
  `api/session.py` (`can_import_channels`), `org/channel_import.py`,
  `org/channel_import_apply.py`, `org/channel_groups.py`,
  `org/sql_channel_groups.py`, and — from the compare-and-update change —
  `org/channel_registry.py` (the `require_pre_state` ordering contract on the
  `ChannelRegistryStore` protocol, plus the in-memory adapter) and
  `org/sql_channel_registry.py` (the same guard inside the row lock).
- **Frontend (13)** — the stepper (`views/RegistryImportFlow.tsx`), its host
  (`views/RegistryView.tsx`), the shell (`AppShell.tsx`), the typed API
  boundary (`lib/api/useChannelImport.ts`, `lib/api/types.ts`), two contexts
  (`UnsettledImportContext.tsx`, `WriteInFlightContext.tsx`),
  `ActionStepper.tsx`, and five test files.
- **Backend tests (8)** — `tests/api/` (`test_channels_import_api.py`,
  `test_channels_import_postgres.py`, `test_channel_group_sync_postgres.py`,
  `test_session_api.py`) and `tests/org/` (`test_channel_import_planner.py`,
  `test_sql_channel_groups.py`, `test_channel_import_apply.py`,
  `test_channel_import_parser.py`).
- **Docs (5)** — `01_IMPLEMENTATION_PLAN.md`, `12_BACKEND_API_SPEC.md`,
  `15_DELIVERY_BACKLOG.md`, the pre-implementation plan under
  `Docs/superpowers/plans/`, and this handoff.

## Behaviour changes

1. **Import is gated on `can_import_channels`**, derived from the session's
   capabilities. No client-side authorization is invented; the backend remains
   the authority.
2. **Apply is bound to the reviewed plan.** The flow sends
   `expected_plan_fingerprint`; the route re-plans from current state and 409s
   on divergence, returning the refreshed plan, which replaces the preview so
   approval is re-sought against reality.
3. **The fingerprint now covers the target**, not just the plan contents:
   `content_owner_id`, `cms_status`, and the **server-resolved tenant**. An
   all-CREATE roster's rows carry no owner, so a digest over contents alone let
   a preview approved for one owner (or tenant) authorise a different one.
4. **Every exit is closed while an apply is in flight** — the flow's own
   Cancel/Back and the shell's sidebar, via `WriteInFlightContext`. The hook
   exposes no abort, so leaving would neither stop nor invalidate a POST that
   still commits.
5. **An apply whose response never arrived is INDETERMINATE, not failed.**
   Apply is disabled, Back is frozen, and the flow offers a re-plan that
   reports **end state only** — "the registry now matches this roster" — and
   never authorship, because inventory equality cannot establish which request
   committed.
6. **A pending import survives the document.** A durable per-apply record,
   namespaced by tenant + principal, keeps the warning across a reload, a
   second tab, and a sidebar exit. Admission is atomic under a Web Lock where
   available.
7. **The audit trail records the source transition.** `CHANNEL_CREATED` and
   `CHANNEL_UPDATED` details gain `revenue_source_status: {from, to}` when the
   write re-derived it, kept out of `changes` because it is derived by the
   registry rather than asserted by the roster. Additive: the key is absent on
   events that reclassified nothing, and on every event written before this PR.
8. **Imports are withheld until the workspace is known.** A session without a
   tenant waits for `/tenants/me` to succeed; a failure does not settle it,
   because two tabs disagreeing about the tenant build different namespaces and
   both would dispatch. A reload re-runs the bootstrap, so this is a retry, not
   a wedge.
9. **A repeated `(youtube_channel_id, group_id)` pair is now a row error.**
   Repeating a channel is only meaningful ACROSS groups — the many-to-many
   roster the singular `group_id` column exists to express. Restating one pair,
   including two rows carrying no group at all, kept both copies: the second
   planned UNCHANGED and repeated the first's `group_action`, while the write
   pass collapses the pair, so the preview promised the group work twice and
   counted the channel twice for one membership. This is the only behaviour
   change here that **rejects input the previous code accepted**, and it cannot
   lose a successful import: the duplicate row never wrote anything, so the
   persisted result of a roster that used to import is unchanged — only the
   misleading preview becomes an explicit error naming the line to delete.
   Rosters listing one channel under several **distinct** groups are unaffected.
   The client's plan validator enforces the same uniqueness, because a check
   the backend applies to the CSV says nothing about a malformed *response*:
   without it the trusted boundary would accept a plan duplicating a pair,
   Preview would promise the group work twice, and the retained fingerprint
   would authorise the single real association.

## Tests run

The four AGENTS.md baseline gates (L113-121), run from the repository root
with the pinned toolchain:

| Gate | Result |
| --- | --- |
| `uv sync --extra dev --extra test --extra lint` | 87 resolved, 85 checked |
| `uv run ruff check backend tests scripts` | **All checks passed** |
| `uv run pytest -q` | **2817 passed**, 15 warnings (7m55s) |
| `git diff --check` | clean (exit 0) |
| `uv run mypy backend` | clean for this PR's files (see note) |

Frontend, and the PR-scope analyzer:

| Suite | Result |
| --- | --- |
| `bun run test` (frontend) | **472 passed**, 41 files |
| `bunx tsc --noEmit` | clean |
| `bun run build` | clean |
| DeepSource (PR scope) | `[]` |
| CI checks | 6 required contexts, all SUCCESS |

The six are the whole required set — `main`'s branch protection lists exactly
`DeepSource: Docker / JavaScript / Python / SQL / Secrets / Shell`. Worth
stating because the head commit reports **zero** check-runs where earlier
commits on this branch reported 24, 8 and 2, which looks like a gate that
stopped running. It is not: every one of those check-runs is named `claude`
(the `claude.yml` bot workflow, which fires on comment and review events rather
than on push), `.github/workflows/` contains no test workflow, and the six
statuses are present on every commit on this branch.

**`uv run pytest -q` needs `UMS_TEST_DATABASE_URL`** and does not skip without
it: the repository's no-skip policy makes the Postgres-tier tests raise, so a
bare run reports 21 failures and 103 errors that are all the same missing
prerequisite. The figure above is the documented command with that variable
set against a disposable Postgres container. Recorded rather than smoothed
over, because a passing total and "21 failed" are the same command on the same
commit, and the difference is entirely environmental.

**`mypy` is not one of the four gates, but DeepSource enforces it.** The
"DeepSource: Python" check runs mypy and reports TYP-050 as a failure, so a
type regression passes all four baseline gates and turns the PR red only after
the push — which is exactly what happened once on this branch. `uv run mypy` is
therefore run here after every backend edit. Full-tree it leaves ONE error, in
`backend/ums_smart_revenue/devtools/pytest_policy_gate.py:597`, which is
byte-identical to `origin/main` and untouched by this PR. Verified with a
command that actually runs -- the earlier draft wrote `diff -` with no second
operand, and the placeholder was swallowed as an HTML tag:

```bash
git show origin/main:backend/ums_smart_revenue/devtools/pytest_policy_gate.py | diff - backend/ums_smart_revenue/devtools/pytest_policy_gate.py
```

Running `ruff` the documented way also caught something a narrower invocation
had not: `uv run --project backend ruff check backend/ tests/` reported clean
while `uv run ruff check backend tests scripts` found an N802 violation in a
test name added this round. Same ruff 0.16.1 either way — the scope and the
config resolution differ, which is exactly why the gate is specified with
those arguments.

The backend suite has been re-run in full after every backend change rather
than assumed — twice over, when the first pass after the round-51 write-boundary
change surfaced a Postgres failure (a monkeypatched `update_inventory` wrapper
that had not grown the new keyword). The total has climbed with each round's
added tests -- 2807 at the first full run, 2817 at the figure in the gate table
above, which is the only one that describes the current commit. Earlier numbers
appear in this document only inside sentences about the round that produced
them.

One caveat that cost a full run to learn: this total is only trustworthy
against a **fresh** Postgres container. Re-running the suite against a reused
one reported 23 failures — every one of them an RLS/migration test, none of
them touching the code under review — because the container carried a stray
schema from an earlier round. A disposable container returns 2817. A stale
container fails loudly enough to look like a regression, so the number above is
recorded together with the condition that produces it.

**Failures encountered and fixed during review**, recorded because they are the
useful part: twenty-two fixtures across five files carried shapes the backend
cannot emit — partial count maps, CREATE rows with no source-status
disclosure, UPDATE/UNCHANGED rows spread from a CREATE (so `from: null`), an
apply answered with a dry-run body, a 409 detail missing header fields, and
stub channel ids no roster could contain. Each
was corrected rather than worked around; several were only exposed *because* a
validation tightening landed.

**Three tests were written and deleted**, each because it asserted something
unassertable or passed for the wrong reason: a relabelled-UPDATE detection that
no client-side check can perform; an absence-only assertion that passed before
the click dispatched; and an empty-capture branch unreachable from a rendered
click (Testing Library flushes effects first). The third was replaced by a
directly-pinned unit test of the extracted rule.

## Risks

- **The plan fingerprint is not client-verifiable.** A response that keeps a
  valid token while substituting the plan contents would have the operator
  approve one plan and the backend commit another. Reaching it needs an
  adversary who can rewrite the response body while leaving the token intact.
  **Open, escalated, and deliberately unresolved** —
  [#issuecomment-5246382660](https://github.com/a7ttaf/Youtube/pull/184#issuecomment-5246382660)
  carries two options and the reason I did not pick one unilaterally
  (client-side recomputation must mirror Python's `ensure_ascii=True`
  canonicalization byte-for-byte, and this catalogue's channel names are
  routinely non-ASCII, so drift would reject every preview).
- **The guard is client-side.** It is not a substitute for durable server-side
  idempotency and does not claim to be; the authoritative record of what
  committed is the `CHANNEL_IMPORTED` audit event, which is what the notice
  tells the operator to read.
- **`can_import_channels` is `MANAGE_CHANNELS ∧ MANAGE_GROUPS`, but neither
  seeded role holds `VIEW_AUDIT_LOG`.** The notice is capability-aware and
  degrades correctly, but the operators who can import are exactly the ones who
  cannot read the audit trail the notice points at. The permissions grant is an
  owner decision, raised at
  [#issuecomment-5240518128](https://github.com/a7ttaf/Youtube/pull/184#issuecomment-5240518128).
- **Where `navigator.locks` is unavailable**, admission degrades to the same
  check-then-set without the lock — narrower than the race, not free of it.
  Stated in the store's contract rather than papered over.

## Migration / backfill

`No migration/backfill required.`

Evidence: `git diff --name-only $(git merge-base main HEAD)..HEAD` returns no
file under `backend/ums_smart_revenue/db/alembic/versions/`, and does not touch
`backend/ums_smart_revenue/db/org_models.py`. No column, constraint, index or
enum is added, altered or dropped, and no existing row is rewritten.

The backend files this PR does change are read/write **paths**, not schema:

- `api/channels.py` — the import route's request validation, the response
  contract, and `_plan_fingerprint`'s inputs. No DDL, and the fingerprint is
  computed in-process and never persisted.
- `api/session.py` — derives `can_import_channels` from capabilities already
  stored; no new persisted field.
- `org/channel_import.py`, `org/channel_import_apply.py` — planning and the
  write boundary, over existing columns.
- `org/channel_groups.py`, `org/sql_channel_groups.py` — group queries and the
  membership write, over existing tables.

Tables written by this PR's code path — `youtube_channels`, `channel_groups`,
`channel_group_members`, `audit_logs` — all pre-date it and are unchanged in
shape.

The write-through of UPDATE **and** UNCHANGED rows for unbound callers — "the
file wins", review #159 — **predates this PR** and is unchanged by it; it is
present at the merge base. What this PR changes about persisted data is
subtractive in both cases, so neither can require a backfill:

- A **bound** apply (one carrying `expected_plan_fingerprint`) 409s instead of
  writing when the locked pre-state is not the one the operator reviewed.
- **Any** apply 409s instead of writing when the group effect observed under
  the row lock contradicts the previewed one.

Both refuse the entire transaction; neither writes a value, a column or a row
that the pre-PR code would not have written.

## Rollback / reset

This PR is **not** frontend-only, and the rollback story has to say so: the
backend diff is **1866 insertions across eight files as of `9a0ac728`**
(`git diff --stat $(git merge-base origin/main HEAD)..HEAD -- backend/`), and a
frontend revert leaves all of it running.

The figure names the commit it measured on purpose. It has been refreshed eight
times under review, because it moves whenever backend code does — including
**comment-only** rounds, which is the case that keeps catching it out: 1,808
became 1,860 when a round added nothing but contract blocks, and 1,866 when a
later round corrected six lines of one of them. `--stat` counts lines, not
behaviour, so "no executable change" is not a reason to skip re-running it.

Naming the commit is what makes the line trustworthy between rounds: if HEAD is
no longer `9a0ac728`, this is a measurement of an earlier tree rather than a
wrong one, and the command above re-derives it. `Docs/` is outside the
`-- backend/` pathspec, so editing this document never moves it.

### Reverting the frontend only

Safe and sufficient to withdraw the **UI**: the stepper is additive and reached
solely through Registry's "Import CSV", itself gated on `can_import_channels`.
No other view imports it, and the pending-import guard is confined to the two
contexts and the shell.

What **remains active** on the API afterwards, because nothing in it depends on
the stepper being deployed:

- `can_import_channels` continues to be derived in the session payload.
- `plan_fingerprint` keeps its widened inputs, so tokens differ from pre-PR
  ones for the same roster. Nothing persists a fingerprint, so this strands no
  stored value — only in-flight previews, which a reload re-fetches.
- `expected_plan_fingerprint` is still **accepted**, and a bound apply still
  409s on a drifted reviewed pre-state. No client sends it once the frontend is
  reverted, so applies fall back to the unbound "the file wins" behaviour of
  review #159 — unchanged by this PR.
- Every plan row still carries `group_action` and `revenue_source_status`.
  These are additive response fields; a pre-PR client ignores them.
- New `CHANNEL_UPDATED` / `CHANNEL_CREATED` audit rows keep carrying
  `revenue_source_status` in their details JSON. Reverting the backend stops
  new events carrying it but cannot un-write the ones already recorded, so any
  consumer of that payload must treat the key as optional — which it already is
  for every event written before this PR.
- The registry write boundary stays a **compare-and-update**: the
  `ChannelRegistryStore` protocol requires `require_pre_state` to run before
  any mutation. This is invisible to callers that pass nothing, so a reverted
  frontend neither uses nor is affected by it.
- **The one behaviour a reverted frontend cannot opt out of**: the write
  boundary re-checks the planned group effect under the group row lock for
  *every* caller, bound or unbound, because the route always performs the
  `list_owned_cms_group_ids` read. A group that appears or vanishes between
  preview and apply now aborts the whole import (409) where it previously
  proceeded. Group writes are also batched one-resolve-per-key rather than
  per-row.

### Reverting the backend

**Order matters: revert the frontend first.** A backend-only revert breaks the
deployed stepper at its shape gate: a payload whose rows omit `group_action`
and `revenue_source_status` — the pre-PR wire shape — is rejected with
`ChannelImportShapeError`, so every preview would fail and the import would be
unusable rather than merely un-improved. Four independent checks reject it (the
two `PLAN_ROW_FIELDS` entries, `hasConsistentGroupEffect`, and the two source
predicates), so this is not a single guard that could be relaxed in passing.
Pinned by `rejects a PRE-DISCLOSURE payload that omits the fields entirely`
in `useChannelImport.test.tsx`, which fails only against a client requiring
none of the disclosure.

Two pieces should **not** be reverted independently to fix a frontend problem:

- **The fingerprint widening.** Reverting it *weakens* the apply guard — a
  preview approved for one content owner or tenant would again satisfy an apply
  directed at another.
- **The reviewed-pre-state enforcement.** Reverting it silently downgrades
  every bound apply to "the file wins", which is the opposite of what an
  operator who reviewed a diff approved.

`api/session.py` is the one backend change that is independently and safely
revertible: it removes a derived boolean, the frontend gate closes, and the
route's own permission checks are untouched.

Nothing here requires a data fix — see **Migration / backfill** above.
Operators holding a stale pending-import record after a rollback can clear it
from `localStorage` under the `ums.unsettledChannelImport.` prefix; no server
state is involved.

## Next recommendations

1. **Decide the fingerprint verifiability question** (above). It is the only
   review finding on this PR left unimplemented.
2. **Grant `VIEW_AUDIT_LOG`** to the import-capable roles, or accept that the
   notice's primary remedy is unavailable to them.
3. **Consider server-side single-use approval tokens.** It would close the
   fingerprint gap without duplicating canonicalization across two languages,
   and would make the client-side guard a convenience rather than the only
   duplicate-write protection.
