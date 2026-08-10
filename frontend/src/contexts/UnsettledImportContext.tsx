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
//   Cleared ONLY by an established outcome: a 2xx apply, a definite rejection,
//   or the operator stating they have checked the audit trail. A registry
//   reload must not clear it, because a fresh GET cannot prove the write
//   landed.
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

/** The mirror's key. Its PRESENCE is the flag; the value is unused. */
export const UNSETTLED_IMPORT_STORAGE_KEY = "ums.unsettledChannelImport";

/**
 * The guard when storage is not usable, and ONLY then. It is set exclusively
 * on a storage failure and dropped as soon as a clear lands in the mirror, so
 * a browser where storage works never accumulates a second, divergent copy of
 * the truth — and one raised while storage was refusing writes cannot outlive
 * its cause.
 */
let memoryFlag = false;

const listeners = new Set<() => void>();

const notify = (): void => {
  for (const listener of listeners) {
    listener();
  }
};

/**
 * Every storage touch is wrapped. A browser can refuse localStorage outright
 * or throw on write, and neither may take down the shell — the cost of a
 * refusal is cross-document persistence, not the guard itself.
 */
const readFlag = (): boolean => {
  try {
    // Either source raising WINS. A browser that reads storage happily but
    // throws on write (quota) would otherwise lose a raise recorded only in
    // memory — a fail-open on the one control this guards.
    return (
      globalThis.localStorage.getItem(UNSETTLED_IMPORT_STORAGE_KEY) !== null || memoryFlag
    );
  } catch {
    return memoryFlag;
  }
};

const writeFlag = (raised: boolean): void => {
  try {
    if (raised) {
      globalThis.localStorage.setItem(UNSETTLED_IMPORT_STORAGE_KEY, "1");
    } else {
      globalThis.localStorage.removeItem(UNSETTLED_IMPORT_STORAGE_KEY);
      memoryFlag = false;
    }
  } catch {
    // Storage refused this write. The in-memory copy is the whole guard now.
    memoryFlag = raised;
  }
};

const subscribe = (listener: () => void): (() => void) => {
  listeners.add(listener);
  // Another TAB's write. That event never fires for this document, so it
  // cannot loop against our own writes. A null key means the whole store was
  // cleared, so re-read rather than trusting newValue.
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === UNSETTLED_IMPORT_STORAGE_KEY) {
      listener();
    }
  };
  globalThis.addEventListener?.("storage", onStorage);
  return () => {
    listeners.delete(listener);
    globalThis.removeEventListener?.("storage", onStorage);
  };
};

export type UnsettledImportValue = {
  /** True while an import of unknown outcome is unaccounted for. */
  unsettled: boolean;
  /** Raise the flag — call BEFORE dispatching an apply, not after it fails. */
  markUnsettled: () => void;
  /** Retire it: the outcome is established, or the operator has checked. */
  acknowledge: () => void;
};

/**
 * Read and control the flag. Safe to call from any number of components —
 * they all observe the same store, so a raise in the flow is visible to the
 * notice in the view without either of them being wired to the other.
 *
 * The snapshot is a boolean, so returning a freshly read value each time is
 * referentially fine for useSyncExternalStore; there is no object to cache.
 */
export const useUnsettledImport = (): UnsettledImportValue => {
  const unsettled = useSyncExternalStore(subscribe, readFlag, readFlag);
  const markUnsettled = useCallback(() => {
    writeFlag(true);
    notify();
  }, []);
  const acknowledge = useCallback(() => {
    writeFlag(false);
    notify();
  }, []);
  return useMemo(
    () => ({ unsettled, markUnsettled, acknowledge }),
    [unsettled, markUnsettled, acknowledge],
  );
};
