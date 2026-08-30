import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ErrorBoundary from "@/components/srcc/ErrorBoundary";

// ============================================================================
// Purpose: Prove the boundary's safe category/report contract, accessible
//   fallback, navigation reset, and reconciliation-only recovery action.
// Database/ORM: None (frontend test coverage only).
// Standards: Error messages and component stacks are treated as sensitive;
//   tests inspect only the allowlisted telemetry payload and never require a
//   child remount after a possibly committed write.
// Blast Radius: Regression coverage for view availability, privacy, recovery,
//   focus management, and operator guidance.
// Connections:
//   - File: frontend/src/components/srcc/ErrorBoundary.tsx -> implementation.
//   - File: frontend/src/components/srcc/AppShell.tsx -> production reset key
//     and full-document recovery callback.
// ============================================================================

let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  // React may print caught errors in the test environment; the boundary's own
  // safe report is inspected below without polluting the test output.
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

/** Select only the boundary-owned report, excluding React's test diagnostics. */
const boundaryReports = (): unknown[][] =>
  consoleErrorSpy.mock.calls.filter((call) =>
    String(call[0]).includes("[ErrorBoundary] view render failed"),
  );

/** Build a child that throws the supplied value on every render. */
const explodingComponent = (thrown: unknown): (() => ReactNode) => {
  return function Exploding(): ReactNode {
    throw thrown;
  };
};

/** Build a child whose external control permits a later navigation reset. */
const controlledComponent = (control: { shouldThrow: boolean }): (() => ReactNode) => {
  return function Controlled(): ReactNode {
    if (control.shouldThrow) {
      throw new TypeError("controlled render failure");
    }
    return <p>recovered content</p>;
  };
};

describe("ErrorBoundary", () => {
  it("passes a healthy subtree through untouched", () => {
    render(
      <ErrorBoundary resetKey="command">
        <p>healthy content</p>
      </ErrorBoundary>,
    );

    expect(screen.getByText("healthy content")).toBeInTheDocument();
    expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument();
    expect(boundaryReports()).toHaveLength(0);
  });

  it("reports only an allowlisted category and correlation ID", async () => {
    const sensitiveMessage = "finance row 42 amount 999.00";
    const Exploding = explodingComponent(new TypeError(sensitiveMessage));
    const onReport = vi.fn();

    render(
      <ErrorBoundary resetKey="groups" onReport={onReport}>
        <Exploding />
      </ErrorBoundary>,
    );

    const fallback = screen.getByTestId("view-error-fallback");
    expect(within(fallback).getByText("TypeError")).toBeInTheDocument();
    expect(fallback.textContent).not.toContain(sensitiveMessage);
    expect(fallback).toHaveTextContent("A write may already have committed");

    const correlation = await screen.findByTestId("view-error-correlation-id");
    expect(correlation).toHaveTextContent(/^Reference: [0-9a-f-]{36}$/iu);
    expect(onReport).toHaveBeenCalledTimes(1);
    expect(onReport).toHaveBeenCalledWith({
      category: "TypeError",
      correlationId: expect.stringMatching(/^[0-9a-f-]{36}$/iu),
    });
    // An approved sink receives the safe event; the boundary never logs raw
    // Error/message/component-stack arguments on its behalf.
    expect(boundaryReports()).toHaveLength(0);
  });

  it("logs the same safe report when no telemetry sink is supplied", () => {
    const sensitiveMessage = "private revenue amount 123.45";
    const Exploding = explodingComponent(new RangeError(sensitiveMessage));

    render(
      <ErrorBoundary resetKey="trace">
        <Exploding />
      </ErrorBoundary>,
    );

    const reports = boundaryReports();
    expect(reports).toHaveLength(1);
    expect(reports[0]).toHaveLength(2);
    expect(reports[0][1]).toEqual({
      category: "RangeError",
      correlationId: expect.stringMatching(/^[0-9a-f-]{36}$/iu),
    });
    expect(JSON.stringify(reports[0])).not.toContain(sensitiveMessage);
  });

  it("falls back safely for a non-string Error.name", () => {
    const thrown = new Error("secret message");
    Object.defineProperty(thrown, "name", { value: undefined });
    const Exploding = explodingComponent(thrown);

    render(
      <ErrorBoundary resetKey="registry">
        <Exploding />
      </ErrorBoundary>,
    );

    const fallback = screen.getByTestId("view-error-fallback");
    expect(within(fallback).getByText("Error")).toBeInTheDocument();
    expect(fallback.textContent).not.toContain("secret message");
  });

  it("falls back safely when Error.name is a throwing getter", () => {
    const thrown = new Error("getter secret");
    Object.defineProperty(thrown, "name", {
      configurable: true,
      get: () => {
        throw new Error("name getter secret");
      },
    });
    const Exploding = explodingComponent(thrown);

    render(
      <ErrorBoundary resetKey="audit">
        <Exploding />
      </ErrorBoundary>,
    );

    expect(within(screen.getByTestId("view-error-fallback")).getByText("Error"))
      .toBeInTheDocument();
  });

  it("allows only known error categories into the fallback", () => {
    const thrown = new Error("sensitive custom name");
    thrown.name = "RevenueRow-42";
    const Exploding = explodingComponent(thrown);

    render(
      <ErrorBoundary resetKey="exports">
        <Exploding />
      </ErrorBoundary>,
    );

    const fallback = screen.getByTestId("view-error-fallback");
    expect(within(fallback).getByText("Error")).toBeInTheDocument();
    expect(fallback.textContent).not.toContain("RevenueRow-42");
  });

  it("focuses the fallback and delegates recovery to a full-reload callback", () => {
    const committedWrite = vi.fn();
    let writeHasCommitted = false;
    const WriteThenThrow = (): ReactNode => {
      // Model a successful POST whose response/render path fails afterward. The
      // guard makes the simulated write itself idempotent across React retries.
      if (!writeHasCommitted) {
        writeHasCommitted = true;
        committedWrite();
      }
      throw new TypeError("render failed after a committed write");
    };
    const onReload = vi.fn();

    render(
      <ErrorBoundary resetKey="close" onReload={onReload}>
        <WriteThenThrow />
      </ErrorBoundary>,
    );

    const fallback = screen.getByTestId("view-error-fallback");
    expect(committedWrite).toHaveBeenCalledTimes(1);
    expect(fallback).toHaveFocus();
    expect(
      within(fallback).getByRole("button", { name: "Reload and reconcile" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reload and reconcile" }));

    expect(onReload).toHaveBeenCalledTimes(1);
    // The child is not retried in place. A full reload/re-fetch owns recovery,
    // so an already-committed write cannot be duplicated by this click.
    expect(screen.getByTestId("view-error-fallback")).toBeInTheDocument();
    expect(committedWrite).toHaveBeenCalledTimes(1);
  });

  it("clears a stale fallback when the reset key changes without remounting", () => {
    const control = { shouldThrow: true };
    const Controlled = controlledComponent(control);
    const { rerender } = render(
      <ErrorBoundary resetKey="groups">
        <Controlled />
      </ErrorBoundary>,
    );

    expect(screen.getByTestId("view-error-fallback")).toBeInTheDocument();
    control.shouldThrow = false;
    rerender(
      <ErrorBoundary resetKey="command">
        <Controlled />
      </ErrorBoundary>,
    );

    expect(screen.getByText("recovered content")).toBeInTheDocument();
    expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument();
  });
});
