import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type { SessionMe } from "@/lib/api/types";

// The hydration lifecycle of the authenticated session.
//   loading -> the one-shot GET /session/me has not yet settled
//   ready   -> the session hydrated; capabilities are authoritative
//   error   -> the call rejected (401/403/network); fail closed
export type SessionStatus = "loading" | "ready" | "error";

type SessionState = {
  status: SessionStatus;
  session: SessionMe | null;
  error: ApiError | Error | null;
};

type SessionContextValue = SessionState & {
  hydrate: (payload: SessionMe) => void;
  fail: (error: ApiError | Error) => void;
};

const INITIAL_STATE: SessionState = {
  status: "loading",
  session: null,
  error: null,
};

const SessionContext = createContext<SessionContextValue | null>(null);

// ============================================================================
// Purpose: Hold the authenticated principal's hydrated session (identity +
//          backend-DERIVED capability booleans). The SPA gates every UI surface
//          on session.capabilities, never on a guessed role string. The initial
//          state is `loading` with a null session so the shell renders a loading
//          state — NOT a permanent access-denied screen — until the one-shot
//          /session/me bootstrap settles, then either `ready` (gate by
//          capabilities) or `error` (fail closed -> access denied).
// Database/ORM: None (frontend context only).
// Standards: Capabilities are authoritative from the backend session; this
//            context never fabricates one. Mirrors TenantProvider's small-context
//            shape (state + hydrate) — no new state library is introduced.
// Blast Radius: Authorization (UI gating). A wrong default would render the
//               dashboard before the principal is known; the `loading` initial
//               state plus fail-closed `error` path prevent that. No graph
//               projection impact detected.
// Connections:
//   - File: frontend/src/lib/api/types.ts -> SessionMe / SessionCapabilities.
//   - File: frontend/src/components/srcc/AppShell.tsx -> consumes capabilities.
//   - File: backend/ums_smart_revenue/api/session.py -> GET /session/me contract.
// ============================================================================
export function SessionProvider({
  children,
  initialSession = null,
}: {
  children: ReactNode;
  // Opt-in seed for tests/storybooks that need a specific session without a
  // network round-trip. Production main.tsx uses the null default and bootstraps.
  initialSession?: SessionMe | null;
}) {
  const [state, setState] = useState<SessionState>(() =>
    initialSession
      ? { status: "ready", session: initialSession, error: null }
      : INITIAL_STATE,
  );

  const hydrate = useCallback((payload: SessionMe) => {
    setState({ status: "ready", session: payload, error: null });
  }, []);

  const fail = useCallback((error: ApiError | Error) => {
    // FIX: drop any previously hydrated session on failure so a transient
    // error can never leave a stale principal's capabilities live — fail closed.
    setState({ status: "error", session: null, error });
  }, []);

  const value = useMemo<SessionContextValue>(
    () => ({ ...state, hydrate, fail }),
    [state, hydrate, fail],
  );

  return (
    <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
  );
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error("useSession must be used within <SessionProvider>");
  }
  return value;
}

export type SessionBootstrap = {
  status: SessionStatus;
  session: SessionMe | null;
  error: ApiError | Error | null;
};

// ============================================================================
// Purpose: Fire GET /session/me ONCE on mount and hydrate SessionContext with
//          the authenticated principal's identity + capabilities, or mark the
//          session failed (fail closed) on any rejection. Returns the live
//          {status, session, error} so the shell renders loading -> dashboard
//          (gated by capabilities) -> or access-denied.
// Database/ORM: None (frontend API call only).
// Standards: useRef re-entry guard keeps the fetch count at 1 under React
//            StrictMode (mirrors useTenantBootstrap). The call relies entirely
//            on the trusted gateway / dev-proxy headers — NO principal claims
//            are sent from the client. The guard is reset on failure so a
//            later provider rebuild can retry; once `ready` it never refetches.
// Blast Radius: Authorization (the gating source). Read-only; no finance write.
//               No graph projection impact detected.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET helper.
//   - File: frontend/src/contexts/SessionContext.tsx -> hydrate()/fail().
//   - File: backend/ums_smart_revenue/api/session.py -> GET /session/me.
// ============================================================================
export function useSessionBootstrap(): SessionBootstrap {
  const session = useSession();
  const client = useApiClient();
  const hasRequestedRef = useRef(false);

  const { status, hydrate, fail } = session;

  useEffect(() => {
    // Once the session is already resolved (seeded for a test, or hydrated by a
    // prior successful call) there is nothing to bootstrap.
    if (status === "ready") return;
    if (hasRequestedRef.current) return;
    hasRequestedRef.current = true;
    client
      .get<SessionMe>("/session/me")
      .then((payload) => {
        hydrate(payload);
      })
      .catch((error: unknown) => {
        // FIX: clear the one-shot guard on failure so a future provider rebuild
        // can retry the bootstrap; without this a transient 5xx/network error on
        // first load would permanently pin the shell to the access-denied state.
        hasRequestedRef.current = false;
        fail(error as ApiError | Error);
      });
  }, [client, status, hydrate, fail]);

  return { status: session.status, session: session.session, error: session.error };
}
