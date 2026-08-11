# PR #184 — CSV import stepper in Registry (PR-B) — delivery handoff

Branch `feat/import-stepper-ui`. Written at head `01aa2b58`; the validation
figures below were measured, not planned.

## Scope

The Registry gains a three-step CSV import stepper over `ActionStepper`
(Upload → Preview → Applied), driving the existing `POST /channels/import`
route in both modes: a read-only dry run that renders the per-row plan, and an
apply that is bound to the plan the operator reviewed.

The session gains a `can_import_channels` capability, and the shell gains a
cross-document guard so an import whose outcome was never established cannot be
silently repeated.

**Non-goals.** No new backend route and no change to what the import writes.
The planner, the write boundary, the permission model and the audit contract
are untouched except where a review finding required the API response
*contract* to be corrected. No revenue math, no allocation, no month-close.

## Files changed (29)

- **Backend (6)** — `api/channels.py` (fingerprint widened to include the
  server-resolved tenant; contract corrections), `api/session.py`
  (`can_import_channels`), `org/channel_import.py`,
  `org/channel_import_apply.py`, `org/channel_groups.py`,
  `org/sql_channel_groups.py`.
- **Frontend (13)** — the stepper (`views/RegistryImportFlow.tsx`), its host
  (`views/RegistryView.tsx`), the shell (`AppShell.tsx`), the typed API
  boundary (`lib/api/useChannelImport.ts`, `lib/api/types.ts`), two contexts
  (`UnsettledImportContext.tsx`, `WriteInFlightContext.tsx`),
  `ActionStepper.tsx`, and five test files.
- **Backend tests (6)**, **Docs (4)**.

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
7. **Imports are withheld until the workspace is known.** A session without a
   tenant waits for `/tenants/me` to succeed; a failure does not settle it,
   because two tabs disagreeing about the tenant build different namespaces and
   both would dispatch. A reload re-runs the bootstrap, so this is a retry, not
   a wedge.

## Tests run

| Suite | Result |
| --- | --- |
| `bun run test` (frontend) | **443 passed**, 41 files |
| `bunx tsc --noEmit` | clean |
| `bun run build` | clean |
| `uv run --project backend pytest -q` | **2807 passed**, 15 warnings (8m38s) |
| DeepSource (PR scope) | `[]` |
| CI checks | 6 pass, 1 skipping |

The backend suite was re-run after the last backend edit rather than assumed;
that edit was comment-only and the result was unchanged at 2807.

**Failures encountered and fixed during review**, recorded because they are the
useful part: nineteen fixtures across five files carried shapes the backend
cannot emit — partial count maps, CREATE rows with no source-status
disclosure, UPDATE/UNCHANGED rows spread from a CREATE (so `from: null`), an
apply answered with a dry-run body, a 409 detail missing header fields. Each
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

## Rollback / reset

Frontend-only revert is safe and sufficient for the UI: the stepper is additive
and reached solely through Registry's "Import CSV", which is itself gated on
`can_import_channels`. Reverting the frontend leaves the backend route exactly
as it was before this PR.

The backend changes are the fingerprint widening and the `can_import_channels`
capability. Reverting the fingerprint widening **weakens** the apply guard
(a preview approved for one owner or tenant would again satisfy an apply
directed at another), so it should not be reverted independently to fix a
frontend problem.

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
