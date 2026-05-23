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
};

type TenantHydrationPayload = {
  id: string;
  slug: string;
  display_name: string;
};

type TenantContextValue = TenantState & {
  hydrate: (payload: TenantHydrationPayload) => void;
};

const TenantContext = createContext<TenantContextValue | null>(null);

export function TenantProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<TenantState>({
    tenantSlug: "ums",
    id: null,
    displayName: null,
  });

  const hydrate = useCallback((payload: TenantHydrationPayload) => {
    setState((previous) => ({
      ...previous,
      tenantSlug: payload.slug,
      id: payload.id,
      displayName: payload.display_name,
    }));
  }, []);

  const value = useMemo<TenantContextValue>(
    () => ({ ...state, hydrate }),
    [state, hydrate],
  );

  return (
    <TenantContext.Provider value={value}>{children}</TenantContext.Provider>
  );
}

export function useTenant(): TenantContextValue {
  const value = useContext(TenantContext);
  if (value === null) {
    throw new Error("useTenant must be used within <TenantProvider>");
  }
  return value;
}
