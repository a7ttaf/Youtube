import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

// ============================================================================
// Purpose: Remember, at SHELL level, that a bulk import's outcome was never
//   learned. The registry view raises this when the import stepper exits with
//   a lost apply response, and it must outlive that view: the operator is told
//   to go check the audit trail, and navigating there unmounts the whole
//   Registry subtree.
// Database/ORM: None (frontend state only).
// Standards: Held ABOVE the view that raises it, because the view is exactly
//   what disappears. Keeping it in RegistryView meant a trip to Audit and back
//   produced a fresh view with the flag cleared and "Import CSV" live again
//   while the original request might still be committing — the duplicate
//   CHANNEL_IMPORTED this exists to prevent (review #184, codex P1).
//   Latching navigation instead is NOT the alternative: the warning's own
//   advice is to open the audit trail, so blocking that navigation would tell
//   the operator to do something the shell refuses to let them do.
//   Cleared only by an explicit acknowledgement. Neither a registry reload nor
//   a BROWSER reload may clear it — neither can prove the write landed — so
//   the flag is mirrored into localStorage and survives document lifetimes and
//   second tabs, not just view changes (review #184, codex P1). localStorage
//   rather than sessionStorage precisely because sessionStorage is per-tab and
//   a second tab is one of the cases that has to be covered.
//   Storage is a MIRROR, never the source of truth for the current document:
//   React state stays authoritative in memory, so a browser that refuses
//   storage (private mode, disabled cookies, a quota error) degrades to the
//   previous shell-lifetime behaviour instead of failing OPEN on the control.
//   Not a substitute for durable server-side idempotency, and does not claim
//   to be — it is a client-side guard on one button. The authoritative record
//   of what committed remains the CHANNEL_IMPORTED audit event, which is what
//   the notice tells the operator to go and read.
//   "No provider" falls back to LOCAL state rather than a no-op, so a
//   RegistryView rendered on its own still shows the notice and still blocks
//   the duplicate; it simply has no other view to survive to.
// Blast Radius: Whether an operator can start a second audited bulk import
//   after one whose outcome is unknown. No requests, no authorization meaning.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx -> owns the state and
//       provides it, so it persists across view navigation.
//   - File: frontend/src/components/srcc/views/RegistryView.tsx -> raises it,
//       renders the notice, and gates the Import CSV control on it.
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       exits with ImportExitOutcome "unknown", which is what raises it.
// ============================================================================

export type UnsettledImportValue = {
  /** True while an import of unknown outcome is unaccounted for. */
  unsettled: boolean;
  /** Raise the flag (an apply whose response never arrived). */
  markUnsettled: () => void;
  /** Retire it — the operator states they have checked the audit trail. */
  acknowledge: () => void;
};

const UnsettledImportContext = createContext<UnsettledImportValue | null>(null);

/** The mirror's key. Read on mount, written on mark, removed on acknowledge. */
export const UNSETTLED_IMPORT_STORAGE_KEY = "ums.unsettledChannelImport";

/**
 * Every storage touch is wrapped: a browser can refuse localStorage outright
 * (private mode, blocked cookies) or throw on write (quota), and neither may
 * take down the shell. A refusal costs cross-document persistence — the flag
 * still works for this document's lifetime — so the failure mode is a weaker
 * guard, never a silently absent one.
 */
const readStoredFlag = (): boolean => {
  try {
    return globalThis.localStorage?.getItem(UNSETTLED_IMPORT_STORAGE_KEY) !== null;
  } catch {
    return false;
  }
};

const writeStoredFlag = (raised: boolean): void => {
  try {
    if (raised) {
      globalThis.localStorage?.setItem(UNSETTLED_IMPORT_STORAGE_KEY, "1");
    } else {
      globalThis.localStorage?.removeItem(UNSETTLED_IMPORT_STORAGE_KEY);
    }
  } catch {
    // Mirror unavailable; in-memory state below remains authoritative.
  }
};

/**
 * Own the flag. The SHELL calls this: it is the component that does NOT
 * unmount when the operator switches views, which is the entire point.
 *
 * The initializer is LAZY so the storage read happens once per mount rather
 * than on every render, and the `storage` listener carries an acknowledgement
 * or a fresh raise made in ANOTHER tab into this one — that event only fires
 * for other documents, so it cannot loop against this hook's own writes.
 */
export const useUnsettledImportLatch = (): UnsettledImportValue => {
  const [unsettled, setUnsettled] = useState<boolean>(readStoredFlag);

  const markUnsettled = useCallback(() => {
    writeStoredFlag(true);
    setUnsettled(true);
  }, []);
  const acknowledge = useCallback(() => {
    writeStoredFlag(false);
    setUnsettled(false);
  }, []);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== UNSETTLED_IMPORT_STORAGE_KEY) {
        return;
      }
      // A null key means the whole store was cleared. Re-read rather than
      // trusting newValue, so this stays correct either way.
      setUnsettled(readStoredFlag());
    };
    globalThis.addEventListener?.("storage", onStorage);
    return () => globalThis.removeEventListener?.("storage", onStorage);
  }, []);

  return useMemo(
    () => ({ unsettled, markUnsettled, acknowledge }),
    [unsettled, markUnsettled, acknowledge],
  );
};

/** Provide the shell-wide unsettled-import flag to everything below it. */
export const UnsettledImportProvider = ({
  value,
  children,
}: {
  value: UnsettledImportValue;
  children: ReactNode;
}) => {
  return (
    <UnsettledImportContext.Provider value={value}>{children}</UnsettledImportContext.Provider>
  );
};

/**
 * Read the flag, preferring the shell's if one is provided.
 *
 * The local fallback is not a courtesy to tests: it is what keeps a
 * provider-less render CORRECT rather than merely silent. A no-op stand-in
 * would leave "Import CSV" enabled after an unknown outcome — a fail-OPEN
 * default on the one control this guards. Both hooks always run, so the rules
 * of hooks hold whichever branch is returned.
 */
export const useUnsettledImport = (): UnsettledImportValue => {
  const shared = useContext(UnsettledImportContext);
  const fallback = useUnsettledImportLatch();
  return shared ?? fallback;
};
