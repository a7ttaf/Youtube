import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "@/lib/api/client";
import type { SessionMe } from "@/lib/api/types";
import {
  createSessionQueryScope,
  SESSION_ME_QUERY_KEY,
  useSessionMeQuery,
} from "@/lib/query/session";
import type { SessionQueryScope } from "@/lib/query/session";

// The hydration lifecycle of the authenticated session.
//   loading -> the shared GET /session/me query has not yet settled
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
  clearSession: () => void;
  queryScope: SessionQueryScope;
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
//          state — NOT a permanent access-denied screen — until the shared
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
export const SessionProvider = ({
  children,
  initialSession = null,
}: {
  children: ReactNode;
  // Opt-in seed for tests/storybooks that need a specific session without a
  // network round-trip. Production main.tsx uses the null default and bootstraps.
  initialSession?: SessionMe | null;
}) => {
  const queryClient = useQueryClient();
  const [queryScope] = useState(createSessionQueryScope);
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

  // ============================================================================
  // Purpose: Clear the session query and hydrated principal at an explicit
  //          logout/auth-boundary transition before any replacement identity
  //          can be observed by the shell.
  // Database/ORM: None (frontend query cache and context state only).
  // Standards: Remove every session-prefixed query, then reset to the
  //            fail-closed loading state; no stale principal survives logout.
  // Blast Radius: Authorization state and session cache only; no API request.
  // Connections:
  //   - File: frontend/src/lib/query/session.ts -> session query-key prefix.
  //   - File: frontend/src/components/srcc/AppShell.tsx -> loading/error gates
  //     before any capability-controlled view renders.
  // ============================================================================
  const clearSession = useCallback(() => {
    // FIX: The former constant-key cache survived auth transitions; purge every
    // scoped entry before reopening bootstrap in the fail-closed loading state.
    queryClient.removeQueries({ queryKey: SESSION_ME_QUERY_KEY });
    setState(INITIAL_STATE);
  }, [queryClient]);

  // A provider unmount ends this authenticated lifetime. Remove every tenant
  // key in its namespace so the old principal cannot remain in a shared
  // QueryClient while a replacement provider mounts.
  useEffect(() => {
    const key = [...SESSION_ME_QUERY_KEY, queryScope] as const;
    return () => {
      queryClient.removeQueries({ queryKey: key });
    };
  }, [queryClient, queryScope]);

  const value = useMemo<SessionContextValue>(
    () => ({ ...state, hydrate, fail, clearSession, queryScope }),
    [state, hydrate, fail, clearSession, queryScope],
  );

  return (
    <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
  );
};

/** Return the current session context value; must be called inside SessionProvider. */
export const useSession = (): SessionContextValue => {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error("useSession must be used within <SessionProvider>");
  }
  return value;
};

export type SessionBootstrap = {
  status: SessionStatus;
  session: SessionMe | null;
  error: ApiError | Error | null;
};

// ============================================================================
// Purpose: Project the shared session query into the fail-closed context state.
// Database/ORM: None (frontend API query only).
// Standards: TanStack Query owns request de-duplication and StrictMode safety;
//            this adapter owns the loading/ready/error UI contract and never
//            fabricates principal claims. A rejected request clears identity.
// Blast Radius: Authorization (the gating source). Read-only; no finance write.
// Connections:
//   - File: frontend/src/lib/query/session.ts -> shared /session/me query.
//   - File: frontend/src/contexts/SessionContext.tsx -> hydrate()/fail().
//   - File: backend/ums_smart_revenue/api/session.py -> GET /session/me.
// ============================================================================
export const useSessionBootstrap = (): SessionBootstrap => {
  const session = useSession();
  const { status, hydrate, fail, queryScope } = session;
  const query = useSessionMeQuery(status === "loading", queryScope);

  useEffect(() => {
    if (status !== "loading") return;
    if (query.isSuccess) {
      hydrate(query.data);
      return;
    }
    if (query.isError) {
      // FIX: clear any prior identity on query failure so a stale principal can
      // never remain authorized while the session is unavailable.
      fail(query.error as ApiError | Error);
    }
  }, [fail, hydrate, query.data, query.error, query.isError, query.isSuccess, status]);

  return { status: session.status, session: session.session, error: session.error };
};
