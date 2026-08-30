import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

type TenantState = {
  tenantSlug: string;
  id: string | null;
  displayName: string | null;
  primaryCurrency: string | null;
};

type TenantHydrationPayload = {
  id: string;
  slug: string;
  display_name: string;
  primary_currency: string;
};

type TenantContextValue = TenantState & {
  hydrate: (payload: TenantHydrationPayload) => void;
};

const TenantContext = createContext<TenantContextValue | null>(null);

// ============================================================================
// Purpose: Hold the resolved tenant identity and declared currency label.
//          Initial slug is intentionally empty so the first /tenants/me
//          bootstrap call does NOT inject
//          X-UMS-Tenant — the trusted gateway / dev proxy is the source of
//          truth for tenant identity in production. A hardcoded fallback
//          ("ums") would pin every non-UMS user's bootstrap to that tenant
//          and 403 their principal load in database-auth mode.
// Database/ORM: None (frontend context only).
// Standards: initialSlug stays opt-in for tests/storybooks that need a
//            specific seed; production main.tsx uses the empty default.
// Blast Radius: Authorization — wrong default slug can mask non-UMS users
//               from discovering their real tenant. The currency is a read-only
//               label and never performs finance math. No graph projection impact.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> reads tenantSlug to decide
//     whether to inject the X-UMS-Tenant header on each request.
//   - File: frontend/vite.config.ts -> dev proxy injects X-UMS-Tenant from
//     VITE_DEV_GATEWAY_TENANT_SLUG so the bootstrap call resolves correctly.
// ============================================================================
export const TenantProvider = ({
  children,
  initialSlug = "",
}: {
  children: ReactNode;
  initialSlug?: string;
}) => {
  const [state, setState] = useState<TenantState>({
    tenantSlug: initialSlug,
    id: null,
    displayName: null,
    primaryCurrency: null,
  });

  const hydrate = useCallback((payload: TenantHydrationPayload) => {
    setState((previous) => ({
      ...previous,
      tenantSlug: payload.slug,
      id: payload.id,
      displayName: payload.display_name,
      primaryCurrency: payload.primary_currency,
    }));
  }, []);

  const value = useMemo<TenantContextValue>(
    () => ({ ...state, hydrate }),
    [state, hydrate],
  );

  return (
    <TenantContext.Provider value={value}>{children}</TenantContext.Provider>
  );
};

export const useTenant = (): TenantContextValue => {
  const value = useContext(TenantContext);
  if (value === null) {
    throw new Error("useTenant must be used within <TenantProvider>");
  }
  return value;
};
