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
//   boolean + message pair can. Arming is effect-driven and self-releasing —
//   useBlockNavigationWhile clears on deactivation AND on unmount, so a flow
//   torn down mid-request can never leave the shell permanently locked.
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

/**
 * Imperative latch control for a flow that is about to start a write.
 *
 * Deliberately NOT an effect-driven `useBlockNavigationWhile(active, reason)`.
 * A passive effect arms one commit LATE: the click handler that starts the
 * request returns, React commits with navigation still enabled, and only then
 * do effects run — a real window in which an operator who clicks Apply and
 * immediately picks a sidebar destination unmounts the flow while the write
 * proceeds, which is precisely what the latch exists to prevent (review #184,
 * reported independently by greptile, qodo and codex).
 *
 * Calling `arm()` inside the same event handler that sets the flow's own
 * in-flight state puts both updates in one batch, so the shell's nav and the
 * flow's buttons disable in the SAME commit. `release()` in the request's
 * `finally` frees them together too.
 *
 * Only `setReason` is pulled out of the context, never the whole value: the
 * value object is re-memoized on every reason change, so depending on it
 * would give these callbacks — and the unmount guard below — a new identity
 * each time the latch armed. The setter comes from useState and is
 * identity-stable, so everything derived from it is too.
 */
export const useWriteInFlightControl = (): WriteInFlightControl => {
  const setReason = useContext(WriteInFlightContext)?.setReason;
  const arm = useCallback(
    (reason: string) => setReason?.(reason),
    [setReason],
  );
  const release = useCallback(() => setReason?.(null), [setReason]);
  // Unmount guard, and the reason `release` must stay identity-stable: a flow
  // torn down while its request is still pending would otherwise strand the
  // shell with navigation dead forever. Nothing else can clear the latch,
  // because the component that armed it is gone.
  useEffect(() => release, [release]);
  return useMemo(() => ({ arm, release }), [arm, release]);
};
