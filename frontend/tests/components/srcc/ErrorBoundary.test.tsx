import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ErrorBoundary from "@/components/srcc/ErrorBoundary";

// ============================================================================
// Purpose: Pin the boundary's THREE observable promises: a healthy subtree is
//   passed through untouched, a throwing subtree is replaced by a themed card
//   naming the error constructor (and NOT its message), and "Try again" clears
//   the caught state so the children are attempted again.
// Standards: React logs every caught error through console.error, so the spy
//   here is noise control — but it is also the only way to observe
//   componentDidCatch, which is asserted rather than assumed.
// ============================================================================

let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  // React prints the caught error and its component stack on every boundary
  // hit; without this the suite output is unreadable. The spy is inspected
  // below, so silencing it does not silence the assertion.
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

/** True when the boundary's own componentDidCatch line reached the console. */
const boundaryLogged = (): boolean =>
  consoleErrorSpy.mock.calls.some((call) =>
    String(call[0]).includes("[ErrorBoundary]"),
  );

/**
 * Build a component that throws `thrown` on every render. Returned from a
 * factory so each test owns its own throwing child and no module-level
 * mutable flag leaks between cases.
 */
const explodingComponent = (thrown: unknown): (() => ReactNode) => {
  return function Exploding(): ReactNode {
    throw thrown;
  };
};

/**
 * Build a component whose throwing is switched from OUTSIDE by the caller's
 * `control` box — the shape a "Try again" recovery needs, since a child that
 * always throws would simply re-trip the boundary.
 *
 * Deliberately not a "throw on the first render only" component: React 19 may
 * discard a failed concurrent render and retry the root synchronously, so a
 * render-counting child heals itself during that retry and the boundary is
 * never reached at all.
 */
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
      <ErrorBoundary>
        <p>healthy content</p>
      </ErrorBoundary>,
    );

    expect(screen.getByText("healthy content")).toBeInTheDocument();
    expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument();
    expect(boundaryLogged()).toBe(false);
  });

  it("replaces a throwing subtree with the themed fallback card", () => {
    const Exploding = explodingComponent(new TypeError("cannot read x of undefined"));
    render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    );

    const fallback = screen.getByTestId("view-error-fallback");
    // Themed like every other panel in the shell, and announced as an alert.
    expect(fallback.tagName).toBe("SECTION");
    expect(fallback).toHaveClass("panel");
    expect(fallback).toHaveAttribute("role", "alert");
    // A short human message, not a stack trace.
    expect(
      within(fallback).getByText("This section could not be displayed"),
    ).toBeInTheDocument();
    expect(within(fallback).getByText(/rest of the console is still working/iu))
      .toBeInTheDocument();
    // The error NAME is shown...
    expect(within(fallback).getByText("TypeError")).toBeInTheDocument();
    // ...and the message is NOT: it can carry interpolated row data or money,
    // and this card renders regardless of the viewer's finance permission.
    expect(fallback.textContent).not.toContain("cannot read x of undefined");
    // componentDidCatch ran and the full error reached the developer console.
    expect(boundaryLogged()).toBe(true);
  });

  it("names a non-Error throw 'Error' rather than rendering nothing", () => {
    // React hands the boundary whatever was thrown; a bare string has no
    // `.name`, and a card with a blank badge would read as a rendering bug.
    const Exploding = explodingComponent("a bare string throw");
    render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    );

    const fallback = screen.getByTestId("view-error-fallback");
    expect(within(fallback).getByText("Error")).toBeInTheDocument();
    expect(fallback.textContent).not.toContain("a bare string throw");
  });

  it("clears the caught error when Try again is pressed", () => {
    const control = { shouldThrow: true };
    const Controlled = controlledComponent(control);
    render(
      <ErrorBoundary>
        <Controlled />
      </ErrorBoundary>,
    );

    expect(screen.getByTestId("view-error-fallback")).toBeInTheDocument();
    expect(screen.queryByText("recovered content")).not.toBeInTheDocument();

    // Whatever made the view throw is gone; the retry must actually re-render
    // the children rather than keep showing the card.
    control.shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(screen.getByText("recovered content")).toBeInTheDocument();
    expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument();
  });

  it("re-shows the fallback when the retried child throws again", () => {
    // The boundary resets its state, it does not paper over a broken child:
    // a deterministic crash must land back on the card rather than loop.
    const Exploding = explodingComponent(new RangeError("still broken"));
    render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    const fallback = screen.getByTestId("view-error-fallback");
    expect(within(fallback).getByText("RangeError")).toBeInTheDocument();
  });
});
