import { act, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/srcc/AppShell", () => ({
  default: ({ initialView }: { initialView?: string }) => (
    <div data-testid="shell-view">{initialView}</div>
  ),
}));

import { AppRouter } from "@/router/AppRouter";

describe("AppRouter", () => {
  it("replaces an invalid single-segment route with the canonical command view", async () => {
    const router = createMemoryRouter(
      [{ path: "*", element: <AppRouter /> }],
      { initialEntries: ["/not-a-view"] },
    );
    const rendered = render(<RouterProvider router={router} />);

    await waitFor(() => expect(router.state.location.pathname).toBe("/command"));
    expect(router.state.historyAction).toBe("REPLACE");
    expect(screen.getByTestId("shell-view")).toHaveTextContent("command");

    await act(async () => {
      await router.navigate(-1);
    });
    expect(router.state.location.pathname).toBe("/command");

    rendered.unmount();
    router.dispose();
  });
});
