import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ErrorBoundary from "@/components/srcc/ErrorBoundary";

const SAFE_DIAGNOSTIC = "[ErrorBoundary] view_render_failed";
let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

const renderBoundary = (child: ReactNode) =>
  render(<ErrorBoundary>{child}</ErrorBoundary>, {
    onCaughtError: () => undefined,
  });

const explodingComponent = (thrown: unknown): (() => ReactNode) =>
  function Exploding(): ReactNode {
    throw thrown;
  };

const expectOnlySafeDiagnostics = (): void => {
  expect(consoleErrorSpy).toHaveBeenCalled();
  expect(
    consoleErrorSpy.mock.calls.every(
      (call) => call.length === 1 && call[0] === SAFE_DIAGNOSTIC,
    ),
  ).toBe(true);
};

describe("ErrorBoundary", () => {
  it("passes a healthy subtree through", () => {
    renderBoundary(<p>healthy content</p>);

    expect(screen.getByText("healthy content")).toBeInTheDocument();
    expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument();
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it("uses fixed public copy and never exposes an Error payload", () => {
    const sensitive = new Error("payment-token=message-secret");
    sensitive.name = "TenantLedgerSecretError";
    sensitive.stack = "stack-secret at /private/customer-ledger.ts:91";
    const Exploding = explodingComponent(sensitive);
    renderBoundary(<Exploding />);

    const fallback = screen.getByTestId("view-error-fallback");
    expect(fallback).toHaveAttribute("role", "alert");
    expect(within(fallback).getByText("This section could not be displayed"))
      .toBeInTheDocument();
    expect(within(fallback).getByText(/rest of the control center is still working/iu))
      .toBeInTheDocument();
    expect(within(fallback).getByText("Render error")).toBeInTheDocument();
    expect(fallback.textContent).not.toMatch(
      /TenantLedgerSecretError|payment-token=message-secret|stack-secret/u,
    );
    expectOnlySafeDiagnostics();
  });

  it("does not inspect or expose a hostile non-Error payload", () => {
    const Exploding = explodingComponent({
      name: "ObjectSecretError",
      message: "bank-account=object-secret",
      stack: "object-stack-secret",
      data: { accessToken: "object-token-secret" },
    });
    renderBoundary(<Exploding />);

    const fallback = screen.getByTestId("view-error-fallback");
    expect(fallback.textContent).not.toMatch(
      /ObjectSecretError|object-secret|object-stack-secret|object-token-secret/u,
    );
    expectOnlySafeDiagnostics();
  });

  it("recovers on an explicit retry when the child stops throwing", () => {
    const control = { shouldThrow: true };
    const Controlled = (): ReactNode => {
      if (control.shouldThrow) throw new Error("controlled-secret");
      return <p>recovered content</p>;
    };
    renderBoundary(<Controlled />);

    control.shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(screen.getByText("recovered content")).toBeInTheDocument();
    expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument();
  });

  it("returns to the fallback when a deterministic retry throws again", () => {
    const Exploding = explodingComponent(new RangeError("still-secret"));
    renderBoundary(<Exploding />);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(screen.getByTestId("view-error-fallback")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("still-secret");
    expectOnlySafeDiagnostics();
  });
});
