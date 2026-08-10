import {
  createContext,
  useCallback,
  useContext,
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
//   Cleared only by an explicit acknowledgement. A reload must not clear it —
//   a fresh GET still cannot prove the write landed.
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

/**
 * Own the flag. The SHELL calls this: it is the component that does NOT
 * unmount when the operator switches views, which is the entire point.
 */
export const useUnsettledImportLatch = (): UnsettledImportValue => {
  const [unsettled, setUnsettled] = useState(false);
  const markUnsettled = useCallback(() => setUnsettled(true), []);
  const acknowledge = useCallback(() => setUnsettled(false), []);
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
