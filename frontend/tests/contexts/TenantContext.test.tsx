import { renderHook, act } from "@testing-library/react";
import type { ReactNode } from "react";

import { TenantProvider, useTenant } from "@/contexts/TenantContext";
import type { TenantRead } from "@/lib/api/types";

const wrapper = ({ children }: { children: ReactNode }) => {
  return <TenantProvider>{children}</TenantProvider>;
};

describe("TenantContext", () => {
  it("seeds with an empty slug and null identity/currency so bootstrap is not pinned", () => {
    const { result } = renderHook(() => useTenant(), { wrapper });
    expect(result.current.tenantSlug).toBe("");
    expect(result.current.id).toBeNull();
    expect(result.current.displayName).toBeNull();
    expect(result.current.primaryCurrency).toBeNull();
  });

  it("honors initialSlug when callers explicitly seed a tenant (tests, storybooks)", () => {
    function seededWrapper({ children }: { children: ReactNode }) {
      return <TenantProvider initialSlug="ums">{children}</TenantProvider>;
    }
    const { result } = renderHook(() => useTenant(), { wrapper: seededWrapper });
    expect(result.current.tenantSlug).toBe("ums");
  });

  it("merges id, displayName, currency, and slug when hydrate is called with a non-bootstrap slug", () => {
    const { result } = renderHook(() => useTenant(), { wrapper });
    expect(result.current.tenantSlug).toBe("");
    const tenant = {
      id: "00000000-0000-0000-0000-0000000000ac",
      slug: "acme",
      display_name: "Acme Holdings",
      primary_currency: "EGP",
    } satisfies TenantRead;
    act(() => {
      result.current.hydrate(tenant);
    });
    expect(result.current.id).toBe("00000000-0000-0000-0000-0000000000ac");
    expect(result.current.displayName).toBe("Acme Holdings");
    expect(result.current.primaryCurrency).toBe("EGP");
    expect(result.current.tenantSlug).toBe("acme");
  });

  it("throws when useTenant is called outside <TenantProvider>", () => {
    const consoleSpy = vi.spyOn(console, "error").mockReturnValue();
    expect(() => renderHook(() => useTenant())).toThrow(
      /useTenant must be used within <TenantProvider>/,
    );
    consoleSpy.mockRestore();
  });
});
