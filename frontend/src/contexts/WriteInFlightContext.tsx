import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

// ============================================================================
// Purpose: One shell-wide latch saying "a write the UI cannot abort is in
//   flight, and unmounting its flow would hide the outcome". A stepper arms it
//   while its apply request is pending; the shell's primary navigation reads
//   it and refuses to switch views until it clears. Without this a flow can
//   only guard the exits it renders ITSELF — its own Cancel and Back — while
//   the sidebar sits outside its tree and unmounts it anyway (review #184).
// Database/ORM: None (frontend state only).
// Standards: The latch stores the operator REASON, not a boolean: whatever
//   blocks navigation must be able to say why, and a null/non-null string
//   cannot drift out of sync with its explanation the way a separate
//   boolean + message pair can. Arming is IMPERATIVE, not effect-driven:
//   useWriteInFlightControl exposes arm()/release() that a flow calls on the
//   same tick it dispatches its request. An effect would arm one commit LATE,
//   leaving a window in which the POST is running and the sidebar is still
//   clickable — the exact hole this latch exists to close, so do not "tidy"
//   it back into a declarative hook. The request's promise owns release even
//   if an error boundary unmounts the flow: unmount must not make navigation
//   live while the unabortable write can still commit out of sight.
//   "No provider" is modelled as a null context rather than a stand-in object
//   holding a do-nothing setter: absence is a real state worth naming, and
//   both hooks read it as "make no claim, block nothing", so a component
//   rendered outside the provider — every existing unit test — behaves
//   exactly as it did before.
// Blast Radius: Whether the sidebar's nav buttons are clickable. No
//   authorization meaning, no data, no requests.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx -> provides the latch
//       and disables NavSection's buttons while it is armed.
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       arms it for the duration of the import apply POST.
// ============================================================================

type WriteInFlightValue = {
  /** Operator-facing reason navigation is blocked, or null when it is not. */
  reason: string | null;
  /** Arm (non-null) or release (null) the latch. */
  setReason: (reason: string | null) => void;
};

// null = rendered outside any provider. Both hooks treat that as "nothing to
// latch": no blocking reason, and arming is skipped rather than dispatched
// into a placeholder.
const WriteInFlightContext = createContext<WriteInFlightValue | null>(null);

/**
 * Own the latch state. The SHELL calls this, because the shell both provides
 * the latch and reads it (its nav is what gets disabled) — and a component
 * cannot consume a context it provides in the same render.
 */
export const useWriteInFlightLatch = (): WriteInFlightValue => {
  const [reason, setReason] = useState<string | null>(null);
  return useMemo(() => ({ reason, setReason }), [reason]);
};

/** Provide the shell-wide write latch to everything below it. */
export const WriteInFlightProvider = ({
  value,
  children,
}: {
  value: WriteInFlightValue;
  children: ReactNode;
}) => {
  return (
    <WriteInFlightContext.Provider value={value}>{children}</WriteInFlightContext.Provider>
  );
};

export type WriteInFlightControl = {
  /** Latch navigation with this operator reason. */
  arm: (reason: string) => void;
  /** Release the latch. */
  release: () => void;
};

// ============================================================================
// Purpose: Imperative latch control for a flow that is about to start a write
//   it cannot abort. `arm()` disables shell navigation, `release()` frees it,
//   and both are called by the flow rather than derived from its state.
// Database/ORM: None (frontend) — sets a context value that gates navigation.
//   No request, no persistence.
// Standards: Deliberately NOT an effect-driven
//   `useBlockNavigationWhile(active, reason)`. A passive effect arms one
//   commit LATE: the click handler that starts the request returns, React
//   commits with navigation still enabled, and only then do effects run — a
//   real window in which an operator who clicks Apply and immediately picks a
//   sidebar destination unmounts the flow while the write proceeds. Calling
//   `arm()` inside the same event handler that sets the flow's in-flight
//   state puts both updates in ONE batch, so the shell's nav and the flow's
//   buttons disable in the same commit; `release()` in the request's
//   `finally` frees them together. Only `setReason` is pulled out of the
//   context, never the whole value: that object is re-memoized on every
//   reason change, so depending on it would give these callbacks a new
//   identity each time the latch armed. The setter
//   comes from useState and is identity-stable, so everything derived from it
//   is too.
// Blast Radius: Whether a committing import can be navigated away from, and
//   whether the shell can be left permanently unnavigable. The hook exposes
//   no abort, so leaving mid-write neither stops nor invalidates a POST that
//   still commits — the operator would land elsewhere while the registry
//   changes under them. The async apply handler continues after React unmounts
//   it and releases in `finally`; clearing from an effect cleanup is unsafe
//   because a render crash can unmount the flow before that promise settles.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx -> the sidebar whose
//       destinations this latch disables.
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx -> the
//       apply handler that arms in the click and releases in `finally`.
// ============================================================================
export const useWriteInFlightControl = (): WriteInFlightControl => {
  const setReason = useContext(WriteInFlightContext)?.setReason;
  const arm = useCallback(
    (reason: string) => setReason?.(reason),
    [setReason],
  );
  const release = useCallback(() => setReason?.(null), [setReason]);
  // FIX: Do not release on component unmount. A view error boundary unmounts
  // the crashed flow while its non-abortable apply promise keeps running; an
  // effect cleanup made shell navigation live and hid a still-committing write.
  // The promise's `finally` retains this stable callback and releases exactly
  // when the request settles, even though React has removed the component.
  return useMemo(() => ({ arm, release }), [arm, release]);
};
