import { renderHook, act } from "@testing-library/react";
import type { ReactNode } from "react";

import { TenantProvider, useTenant } from "@/contexts/TenantContext";

function wrapper({ children }: { children: ReactNode }) {
  return <TenantProvider>{children}</TenantProvider>;
}

describe("TenantContext", () => {
  it("seeds with the bootstrap slug and null id/displayName", () => {
    const { result } = renderHook(() => useTenant(), { wrapper });
    expect(result.current.tenantSlug).toBe("ums");
    expect(result.current.id).toBeNull();
    expect(result.current.displayName).toBeNull();
  });

  it("merges id, displayName, and slug when hydrate is called with a non-bootstrap slug", () => {
    const { result } = renderHook(() => useTenant(), { wrapper });
    expect(result.current.tenantSlug).toBe("ums");
    act(() => {
      result.current.hydrate({
        id: "00000000-0000-0000-0000-0000000000ac",
        slug: "acme",
        display_name: "Acme Holdings",
      });
    });
    expect(result.current.id).toBe("00000000-0000-0000-0000-0000000000ac");
    expect(result.current.displayName).toBe("Acme Holdings");
    expect(result.current.tenantSlug).toBe("acme");
  });

  it("throws when useTenant is called outside <TenantProvider>", () => {
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => renderHook(() => useTenant())).toThrow(
      /useTenant must be used within <TenantProvider>/,
    );
    consoleSpy.mockRestore();
  });
});
