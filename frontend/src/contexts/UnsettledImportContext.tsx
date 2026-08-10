import { useCallback, useMemo, useSyncExternalStore } from "react";

// ============================================================================
// Purpose: Remember that a bulk import's outcome was never established. The
//   import flow raises this BEFORE it dispatches the apply and clears it only
//   once a response settles the question; the registry view renders the
//   warning and keeps "Import CSV" shut while it stands.
// Database/ORM: None (frontend state only).
// Standards: Held in a MODULE-level store, not component state and not a
//   context value, because every narrower home has a hole. Component state
//   dies with the flow — an operator who leaves by the sidebar never runs its
//   exit handler. View state dies with the view — the notice's own advice is
//   to open the Audit trail, which unmounts Registry. A provider fixes those
//   two but still leaves consumers under NO provider each holding a private
//   copy, so the flow's raise never reaches the view's notice. One store, read
//   through useSyncExternalStore, has none of those seams (review #184).
//   Mirrored into localStorage so it also survives the DOCUMENT: a closed tab
//   or a reload discards the pending fetch while the backend goes on
//   committing. localStorage rather than sessionStorage precisely because
//   sessionStorage is per-tab and a second tab is one of the cases to cover;
//   a `storage` listener carries a raise or an acknowledgement between tabs.
//   Storage is a MIRROR. When a browser refuses it (private mode, blocked
//   cookies, quota), the in-memory flag governs, so the failure mode is a
//   guard that no longer crosses documents — never one that silently fails
//   OPEN on the control it protects.
//   Tracks pending applies BY IDENTITY, not as one boolean. Two tabs can each
//   hold a preview, and a single flag cannot tell their requests apart: the
//   first to settle would clear the other's protection, so a later lost
//   response would find the guard already down (review #184, codex P1). Each
//   apply carries its own id; settling removes only that id.
//   One KEY PER APPLY, never a shared array. An array needs read-modify-write,
//   and that is not atomic across documents: two tabs dispatching before
//   either sees the other's storage event both read the same list and the
//   second setItem drops the first id — after which one settle takes the guard
//   down while a genuinely unknown write is still outstanding. Per-key writes
//   touch only the entry they own, so there is no shared cell to lose a write
//   on and nothing to serialize (review #184, codex P1).
//   Cleared ONLY by an established outcome: a 2xx apply, a definite rejection,
//   or the operator stating they have checked the audit trail — which clears
//   ALL of them, because that is the claim the operator is actually making. A
//   registry reload must not clear anything, because a fresh GET cannot prove
//   the write landed.
//   SCOPED to the tenant + principal. localStorage is origin-wide and outlives
//   sign-out, so an unscoped record followed a shared browser into the next
//   session: a different operator, or the same one on another tenant, would be
//   blocked by an import they cannot reconcile - and their acknowledgement
//   would clear the original operator's protection, after which returning to
//   the first tenant permits the duplicate this exists to stop (review #184,
//   codex P2). Every read, write and sweep is confined to one scope, and the
//   scope's two halves are percent-encoded over a strict alphabet so neither
//   the `~` separator nor the `.` key delimiter can occur inside them — a
//   prefix match must not be able to cross a scope boundary.
//   ADMISSION is atomic where the platform allows it. Reading `unsettled` and
//   then creating the key are two operations, so two tabs could both observe
//   false and both dispatch — for an all-UNCHANGED roster both POSTs return
//   200 and each appends a CHANNEL_IMPORTED (review #184, codex P1). admit()
//   does the check and the write inside one Web Lock held on the scope, which
//   IS cross-document, so at most one tab is admitted. Where navigator.locks
//   is unavailable it degrades to the same check-then-set without the lock and
//   says so here rather than pretending: that is narrower than the race, not
//   free of it.
//   Not a substitute for durable server-side idempotency and does not claim to
//   be — it is a client-side guard on one button. The authoritative record of
//   what committed is the CHANNEL_IMPORTED audit event, which is exactly what
//   the notice tells the operator to go and read.
// Blast Radius: Whether an operator can start a second audited bulk import
//   after one whose outcome is unknown. No requests, no authorization meaning.
// Connections:
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       raises it before dispatching the apply, clears it on a settled result.
//   - File: frontend/src/components/srcc/views/RegistryView.tsx -> renders the
//       notice and gates the Import CSV control on it.
//   - File: Docs/12_BACKEND_API_SPEC.md -> records that a re-plan is end-state
//       evidence only, which is why an unknown outcome stays unknown.
// ============================================================================

/**
 * Key PREFIX. Each pending apply gets `${prefix}.${scope}.${applyId}`; the
 * presence of such a key is the record, and its value is never read.
 */
export const UNSETTLED_IMPORT_STORAGE_KEY = "ums.unsettledChannelImport";

/**
 * Presence flags. Each half of a scope is `1<encoded>` when the component is
 * known and `0` when it is not, so a KNOWN component is never conflated with a
 * missing one and no real value can impersonate the sentinel — the flag is a
 * fixed leading character outside the encoded alphabet's control.
 */
const KNOWN = "1";
const MISSING = "0";

/**
 * The scope when NEITHER component is known - a standalone render, or a shell
 * with no session at all. Deliberately a real, NAMED scope rather than an
 * empty string: records made without an identity must not be visible to an
 * identified operator, and vice versa.
 */
export const UNSCOPED_IMPORT_SCOPE = `${MISSING}~${MISSING}`;

/**
 * Percent-encode everything outside an unreserved alphabet, INCLUDING the
 * characters `encodeURIComponent` leaves alone: `.` `~` `!` `*` `'` `(` `)`.
 *
 * That matters because the scope is used as a key PREFIX and matched with
 * `startsWith`. With `.` and `~` surviving encoding, a tenant slug shaped like
 * `ums~.child` produced keys beginning with `ums~.` — the prefix of a
 * DIFFERENT operator's scope — so that operator would see, block on, and
 * `acknowledgeAll()` away someone else's pending imports (review #184).
 * After this, no encoded component can contain the separator or the delimiter,
 * so a prefix match cannot cross a scope boundary.
 */
const encodeScopePart = (value: string): string => {
  return [...value]
    .map((character) =>
      /[A-Za-z0-9_-]/.test(character)
        ? character
        : [...new TextEncoder().encode(character)]
            .map((byte) => `%${byte.toString(16).toUpperCase().padStart(2, "0")}`)
            .join(""),
    )
    .join("");
};

// ============================================================================
// Purpose: The tenancy + principal ISOLATION BOUNDARY for durable pending-write
//   records. Produces the namespace every unsettled-import key lives under, so
//   one operator's pending import is visible only to that operator, in that
//   tenant.
// Database/ORM: None (frontend) — a pure function over identity values. What
//   it namespaces is localStorage, and through it which POST /channels/import
//   the admission gate will refuse.
// Standards: Both halves go through encodeScopePart, so `~` is unambiguously
//   the separator and `.` unambiguously the key delimiter, and no encoded
//   component can contain either — a prefix match must not cross a scope.
//   Components are kept INDEPENDENTLY behind a presence flag: a missing half
//   never pools a known half with someone else's, and no real id can
//   impersonate the missing sentinel because the flag is a fixed leading
//   character.
//   The tenant is the IMMUTABLE database id, never the routing slug: a slug
//   rename while an apply is unsettled would mint a fresh scope, hiding the
//   warning and handing admission an empty bucket for a request whose outcome
//   nobody knows.
// Blast Radius: Cross-tenant and cross-operator isolation of the duplicate-
//   import guard. A mistake here lets one operator block on, or acknowledge
//   away, another tenant's unsettled import — and an acknowledgement is what
//   re-enables dispatch, so it decides whether a duplicate audited write
//   becomes reachable. No authorization meaning: the backend's permission
//   checks and its own tenant scoping are unaffected either way.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx -> supplies the resolved
//       tenant id and the authenticated user id.
//   - File: frontend/src/contexts/TenantContext.tsx -> the resolved tenant id
//       used when the session body has not carried one.
// ============================================================================
/**
 * One operator on one tenant, encoded so the pair cannot be confused with any
 * other pair.
 *
 * Each component is kept INDEPENDENTLY. Collapsing to a single "unknown"
 * bucket whenever either was missing was wrong in a state the app really
 * reaches: `SessionMe.tenant` is nullable and `hasActiveSession` still admits
 * such a session, so during bootstrap or a degraded tenant context two
 * different operators — both with a known `userId` — shared one bucket. One
 * blocked the other, and the second operator's acknowledgeAll() retired the
 * first's protection (review #184). A component is only ever pooled with other
 * MISSING components, never with a known one.
 *
 * `tenantId` is the tenant's immutable database id, not its routing slug: a
 * slug rename while an apply is unsettled would otherwise mint a fresh scope,
 * hiding the warning and handing admission an empty bucket for a request whose
 * outcome is still unknown.
 */
export const importScopeFor = (
  tenantId: string | null | undefined,
  userId: string | null | undefined,
): string => {
  const tenantPart = tenantId ? `${KNOWN}${encodeScopePart(tenantId)}` : MISSING;
  const userPart = userId ? `${KNOWN}${encodeScopePart(userId)}` : MISSING;
  return `${tenantPart}~${userPart}`;
};

/**
 * The guard when storage is not usable, and ONLY then. It is written
 * exclusively on a storage failure and dropped when a clear lands in the
 * mirror, so a browser where storage works never accumulates a second,
 * divergent copy of the truth.
 */
let memoryIds: readonly string[] = [];

const listeners = new Set<() => void>();

const notify = (): void => {
  for (const listener of listeners) {
    listener();
  }
};

/**
 * Every storage touch below is wrapped: a browser can refuse localStorage
 * outright or throw on write, and neither may take down the shell — the cost
 * of a refusal is cross-document persistence, not the guard itself.
 *
 * The KEY is the record; its value is never read. That is what removes the
 * read-modify-write, and with it the lost-update race and any need to parse
 * (so there is no corrupt-value branch to get wrong either).
 */
const prefixFor = (scope: string): string => `${UNSETTLED_IMPORT_STORAGE_KEY}.${scope}.`;

const keyFor = (scope: string, applyId: string): string => `${prefixFor(scope)}${applyId}`;

const isApplyKey = (key: string): boolean => {
  return key.startsWith(`${UNSETTLED_IMPORT_STORAGE_KEY}.`);
};

const storedApplyKeys = (scope: string): string[] => {
  const store = globalThis.localStorage;
  const prefix = prefixFor(scope);
  const keys: string[] = [];
  for (let index = 0; index < store.length; index += 1) {
    const key = store.key(index);
    if (key !== null && key.startsWith(prefix)) {
      keys.push(key);
    }
  }
  return keys;
};

/**
 * The UNION of both sources, never one in preference to the other.
 *
 * Preferring a non-empty mirror was a fail-open: with apply A already stored
 * and a quota error swallowing B's write, B lives only in memory. Reading the
 * mirror alone reports {A}; A then settles, its removeItem succeeds, and the
 * guard drops while B may still be committing (review #184, codex P1). The
 * two sources are partial views of one set, so the set is what to read.
 */
const readIds = (scope: string): readonly string[] => {
  let stored: readonly string[] = [];
  try {
    stored = storedApplyKeys(scope);
  } catch {
    // Mirror unreadable; memoryIds alone is the guard for this document.
  }
  const inMemory = memoryIds.filter((id) => id.startsWith(prefixFor(scope)));
  return stored.length === 0 ? inMemory : [...new Set([...stored, ...inMemory])];
};

/** The snapshot useSyncExternalStore reads. A boolean, so it needs no cache. */
const readFlag = (scope: string): boolean => {
  return readIds(scope).length > 0;
};

// Held under the FULL key, so the in-memory set carries its scope exactly as
// the mirror does and cannot leak across operators either.
const rememberInMemory = (key: string, pending: boolean): void => {
  memoryIds = pending
    ? [...memoryIds.filter((id) => id !== key), key]
    : memoryIds.filter((id) => id !== key);
};

/**
 * Record one apply as pending. Touches ONLY this apply's key, so a concurrent
 * tab doing the same thing cannot drop it and it cannot drop theirs.
 *
 * Returns whether the record became DURABLE. A memory-only record is not a
 * successful claim: the Web Lock is released the moment admission returns, so
 * another tab cannot see it, and a reload erases it while the backend request
 * may still be committing. Callers admitting a write must treat `false` as a
 * refusal (review #184).
 */
const addId = (scope: string, applyId: string): boolean => {
  try {
    globalThis.localStorage.setItem(keyFor(scope, applyId), "1");
    return true;
  } catch {
    // Storage refused this write. Keep it in memory so THIS document's own
    // guards (the disabled Apply button, the notice) still hold, and report
    // the failure so an admission decision can refuse.
    rememberInMemory(keyFor(scope, applyId), true);
    return false;
  }
};

/** Retire one apply. Same per-key isolation, in the other direction. */
const removeId = (scope: string, applyId: string): void => {
  try {
    globalThis.localStorage.removeItem(keyFor(scope, applyId));
  } catch {
    // Ignored: rememberInMemory below is the whole guard when storage refuses.
  }
  // Drops ONLY this id. It used to clear the whole in-memory set, which meant
  // one apply settling took an unrelated memory-only apply down with it.
  rememberInMemory(keyFor(scope, applyId), false);
};

/**
 * Retire every apply this document can see. Removal-only, so a key another tab
 * adds while this runs simply survives — the guard stays UP, which is the safe
 * direction for a race on an operator's "I have checked" statement.
 */
const removeAllIds = (scope: string): void => {
  try {
    for (const key of storedApplyKeys(scope)) {
      globalThis.localStorage.removeItem(key);
    }
  } catch {
    // Ignored: the in-memory clear below still applies.
  }
  // THIS scope only. An operator's "I have checked" is a statement about their
  // own imports; it must not retire another principal's or another tenant's.
  memoryIds = memoryIds.filter((id) => !id.startsWith(prefixFor(scope)));
};

// ============================================================================
// Purpose: The admission decision for an audited bulk import - atomically
//   claim the right to dispatch one apply, or refuse. Returns true with
//   `applyId` recorded as pending; false having written NOTHING, in which case
//   the caller must not send the request.
// Database/ORM: None (frontend) - reads and writes only this scope's
//   localStorage records. It issues no request of its own; what it gates is
//   POST /channels/import with dry_run=false.
// Standards: The check and the write happen inside ONE Web Lock held on the
//   scope. Doing them as two steps - read `unsettled`, then `trackApply` - is
//   the bug this replaces: two tabs both observe "nothing pending" and both
//   dispatch, and for an all-UNCHANGED roster both POSTs return 200 and each
//   appends a CHANNEL_IMPORTED (review #184). Web Locks is the only
//   cross-document mutual-exclusion primitive a page has; storage events are
//   notifications, not exclusion, and cannot close this window - so this must
//   not be "simplified" back into a read followed by a write.
//   Admission FAILS CLOSED when the record cannot be persisted. A memory-only
//   record is not a claim: the lock is released the moment admission returns,
//   so no other document can see it, and a reload erases it while the request
//   may still commit. Refusing is the safe direction here even though it costs
//   the operator the feature until storage works — the alternative hands out a
//   claim nobody else can honour, on an audited write.
//   FAIL-OPEN LIMITATION, stated because it is real: where navigator.locks is
//   unavailable this degrades to the same check-then-set WITHOUT the lock.
//   That is narrower than the race, not free of it. It fails open rather than
//   closed on purpose - refusing every apply on such a browser would lock the
//   operator out of the feature entirely - and it is one more reason this is
//   not a substitute for durable server-side idempotency, which is the only
//   thing that closes the window across browsers and devices.
// Blast Radius: Whether a second audited bulk import can be dispatched while
//   one is outstanding, and therefore whether a duplicate CHANNEL_IMPORTED can
//   be appended for a roster that already committed. No authorization meaning:
//   the backend's own permission checks and fingerprint guard are unaffected.
// Connections:
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       awaits this before dispatching the apply; a refusal shows
//       OTHER_APPLY_PENDING_NOTE and sends no request.
//   - File: backend/ums_smart_revenue/api/channels.py -> import_channels, the
//       route whose CHANNEL_IMPORTED event a duplicate would append to.
// ============================================================================
const admitApply = (scope: string, applyId: string): Promise<AdmissionResult> => {
  const claim = (): AdmissionResult => {
    if (readFlag(scope)) {
      return "other-apply-pending";
    }
    // Fail CLOSED on a storage failure. A memory-only record cannot be seen by
    // another tab and does not survive a reload, so admitting on one would
    // hand out a claim that no other document can honour — for an audited
    // write that may commit anyway.
    if (addId(scope, applyId)) {
      return "admitted";
    }
    // And RETIRE the record addId kept in memory. A refused admission dispatches
    // NOTHING, so leaving it would mark this document unsettled over a request
    // that never happened: the next attempt would be refused as
    // "other-apply-pending", and leaving the flow would warn that an import may
    // still be committing until the operator reloaded or falsely acknowledged
    // it (review #184). Retiring here and not in the caller keeps the rule with
    // the decision — trackApply's own unconditional path still retains, because
    // there a request really is in flight.
    removeId(scope, applyId);
    return "not-durable";
  };
  const locks = globalThis.navigator?.locks;
  if (locks === undefined) {
    // No cross-document exclusion available. Same check, same order, without
    // the guarantee — honestly narrower than the race rather than closed.
    // Wrapped so both branches return a promise and callers cannot come to
    // depend on this one resolving synchronously.
    return Promise.resolve(claim());
  }
  return locks.request(`${UNSETTLED_IMPORT_STORAGE_KEY}.${scope}`, claim);
};

const subscribe = (listener: () => void): (() => void) => {
  listeners.add(listener);
  // Another TAB's write. That event never fires for this document, so it
  // cannot loop against our own writes. A null key means the whole store was
  // cleared, so re-read rather than trusting newValue.
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || isApplyKey(event.key)) {
      listener();
    }
  };
  globalThis.addEventListener?.("storage", onStorage);
  return () => {
    listeners.delete(listener);
    globalThis.removeEventListener?.("storage", onStorage);
  };
};

/**
 * Why an apply was or was not admitted. A boolean could not distinguish the
 * two refusals, and they need different operator copy: one says "go back to
 * the other tab", the other says "this browser is not storing site data".
 */
export type AdmissionResult = "admitted" | "other-apply-pending" | "not-durable";

export type UnsettledImportValue = {
  /** True while any apply of unknown outcome is unaccounted for. */
  unsettled: boolean;
  /** Record an apply as pending. Call BEFORE dispatching it, not after it
   * fails — a tab closed mid-request never reaches a failure handler. Prefer
   * `admit` from a dispatch path; this is the unconditional form. */
  trackApply: (applyId: string) => void;
  /** Atomically claim the right to dispatch. Anything but "admitted" means
   * nothing durable was recorded and the caller MUST NOT dispatch. */
  admit: (applyId: string) => Promise<AdmissionResult>;
  /** Retire ONE apply: its response established success or refusal. */
  settleApply: (applyId: string) => void;
  /** Retire every pending apply — the operator states they have checked the
   * audit trail, which is a claim about all of them, not the newest. */
  acknowledgeAll: () => void;
};

let fallbackCounter = 0;

// ============================================================================
// Purpose: Mint the IDENTITY of one apply. Every durable pending-write record
//   is keyed by the id this produces, so an id is what distinguishes two
//   outstanding imports — including two in different documents.
// Database/ORM: None (frontend) — the id is a localStorage key component, never
//   sent to the backend and never part of any audited payload.
// Standards: Ids must be unique ACROSS DOCUMENTS, not merely within one. A
//   clock plus a counter satisfies the weaker property and fails the real one:
//   two tabs read the same millisecond and both start their counter at 1, mint
//   the same id, and then one tab's settleApply removes the OTHER tab's pending
//   record — after which the duplicate-import guard is silently down over a
//   write whose outcome nobody knows (review #184, qodo).
//   Preference order is by ENTROPY, and each fallback exists for a runtime the
//   one above it does not serve: crypto.randomUUID (absent outside a secure
//   context), then the per-document salt from crypto.getRandomValues (which
//   needs NO secure context, so plain HTTP still reaches it), then Math.random.
//   The salt is drawn ONCE per document at module init, so every id from one
//   document shares it and ids from two documents differ in it. Math.random is
//   last and is still worth having: a collision then needs both documents to
//   draw the same salt AND share a millisecond AND share a counter.
//   Never THROWS. A runtime missing `performance` degrades to Date.now()
//   rather than failing on the way to dispatching an apply, because throwing
//   here would block the import outright — a worse outcome than a readable
//   prefix being slightly less precise. The clock is a devtools convenience,
//   not part of the uniqueness argument.
// Blast Radius: Whether one tab can retire another tab's pending-import record.
//   A collision weakens the duplicate-write guard on an audited bulk import; it
//   carries no authorization meaning and reaches no request.
// Connections:
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx -> calls
//       newApplyId() once per apply and passes it to admit()/settleApply().
//   - File: frontend/src/contexts/UnsettledImportContext.tsx -> keyFor(), which
//       makes this id the last component of the storage key.
// ============================================================================

/**
 * Per-DOCUMENT entropy for the fallback id, drawn once.
 *
 * A clock plus a counter is unique within one document and NOT across two:
 * both tabs read the same millisecond and both start their counter at 1, so
 * they mint the same id — and since the id is the localStorage key, one tab
 * then removes the other's pending record and the cross-tab guard silently
 * weakens (review #184, qodo).
 *
 * `crypto.getRandomValues` is the right source and is NOT the one that was
 * missing: `randomUUID` requires a secure context, `getRandomValues` does not,
 * so the very case this fallback exists for — an app served over plain HTTP —
 * still has it. Math.random() is the last resort, and it is still far better
 * than nothing here, because a collision needs both documents to draw the same
 * salt AND share a millisecond AND share a counter value.
 */
const documentSalt = ((): string => {
  const buffer = globalThis.crypto?.getRandomValues?.(new Uint32Array(2));
  if (buffer !== undefined) {
    return Array.from(buffer, (part) => part.toString(36)).join("");
  }
  return Array.from({ length: 2 }, () =>
    Math.trunc(Math.random() * 2 ** 32).toString(36),
  ).join("");
})();

/** Identity for one apply. Prefixed so a stray id is obvious in devtools. */
export const newApplyId = (): string => {
  const random = globalThis.crypto?.randomUUID?.();
  if (random !== undefined) {
    return `apply-${random}`;
  }
  // No randomUUID. The salt separates documents, the counter separates applies
  // within one, and the clock is a readable prefix only — which is why a
  // runtime without `performance` degrades to Date.now() rather than throwing
  // on the way to dispatching an apply and blocking the import entirely.
  fallbackCounter += 1;
  const clock = globalThis.performance?.now?.() ?? Date.now();
  return `apply-${documentSalt}-${Math.trunc(clock)}-${fallbackCounter}`;
};

/**
 * Read and control the store. Safe to call from any number of components —
 * they all observe the same module state, so a raise in the flow is visible to
 * the notice in the view without either being wired to the other.
 */
export const useUnsettledImport = (
  scope: string = UNSCOPED_IMPORT_SCOPE,
): UnsettledImportValue => {
  const snapshot = useCallback(() => readFlag(scope), [scope]);
  const unsettled = useSyncExternalStore(subscribe, snapshot, snapshot);
  const trackApply = useCallback(
    (applyId: string) => {
      addId(scope, applyId);
      notify();
    },
    [scope],
  );
  const admit = useCallback(
    async (applyId: string) => {
      const outcome = await admitApply(scope, applyId);
      notify();
      return outcome;
    },
    [scope],
  );
  const settleApply = useCallback(
    (applyId: string) => {
      removeId(scope, applyId);
      notify();
    },
    [scope],
  );
  const acknowledgeAll = useCallback(() => {
    removeAllIds(scope);
    notify();
  }, [scope]);
  return useMemo(
    () => ({ unsettled, trackApply, admit, settleApply, acknowledgeAll }),
    [unsettled, trackApply, admit, settleApply, acknowledgeAll],
  );
};
