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
//   torn down mid-request can never leave the shell permanently locked. The
//   default context value is inert (reason null, arming a no-op) so a
//   component rendered outside the provider — every existing unit test —
//   behaves exactly as it did before.
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

const INERT: WriteInFlightValue = { reason: null, setReason: () => {} };

const WriteInFlightContext = createContext<WriteInFlightValue>(INERT);

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

/** Read the latch: the blocking reason, or null when navigation is free. */
export const useWriteInFlightReason = (): string | null => {
  return useContext(WriteInFlightContext).reason;
};

/**
 * Hold the latch for as long as `active` is true, then release it.
 *
 * The cleanup is the load-bearing half: it runs on deactivation AND on
 * unmount, so a flow that is torn down while its request is still pending
 * cannot strand the shell with navigation disabled forever.
 */
export const useBlockNavigationWhile = (active: boolean, reason: string): void => {
  const { setReason } = useContext(WriteInFlightContext);
  const release = useCallback(() => setReason(null), [setReason]);
  useEffect(() => {
    if (!active) {
      return undefined;
    }
    setReason(reason);
    return release;
  }, [active, reason, setReason, release]);
};
