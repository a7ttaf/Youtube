import { Component, type ErrorInfo, type ReactNode } from "react";

import { Badge } from "./shared";

// ============================================================================
// Purpose: Contain a render-time crash to the panel it happened in. Without a
//   boundary, any component that throws during render tears down the WHOLE
//   React root: React 19 unmounts the tree, so the operator is left on a blank
//   white page with no sidebar, no way back, and nothing naming what broke.
//   With one, the failing view is replaced by a themed card while the shell
//   chrome around it (sidebar, topbar, nav) stays mounted and usable.
// Database/ORM: None (frontend, presentation only). Catches no network or data
//   error — those are already handled inside each view's own error states.
// Standards: A class component because getDerivedStateFromError /
//   componentDidCatch have no hook equivalent — this is the one place a class
//   is required. The fallback names the error CONSTRUCTOR (`error.name`) and
//   never `error.message`: a thrown message can carry interpolated row data,
//   ids, or money, and this card renders regardless of the viewer's finance
//   permission, so echoing the message would be a gate the boundary bypasses.
//   The full error and component stack still reach the developer console.
//   Recovery is explicit ("Try again" clears the caught state and re-renders
//   the children); the boundary never retries on its own, which would spin a
//   deterministic crash into an infinite render loop.
// Blast Radius: Availability of the shell under a UI bug — a crash degrades to
//   one card instead of a white page. No authorization, finance, audit, or
//   export behavior: it renders no money, grants nothing, and issues no
//   request. It does NOT make a failed write succeed; a view that crashed
//   after dispatching one is still a view whose outcome is unknown, which is
//   why the fallback copy points at retrying the SECTION, not the action.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx -> wraps <ViewRouter/>
//     with this, keyed by the active view so navigating away clears a caught
//     error by remount rather than by any state this component owns.
//   - File: frontend/src/components/srcc/shared.tsx -> Badge, so the fallback
//     uses the same design-system primitive as every other panel header.
//   - File: frontend/src/styles.css -> .panel / .panel-header / .panel-title /
//     .action-row / .ghost-button, the card styling shared with AccessDenied.
// ============================================================================

/** Props: the subtree to guard. The boundary adds no chrome of its own. */
type ErrorBoundaryProps = {
  children: ReactNode;
};

/**
 * State: the caught error's constructor name, or null while healthy. Only the
 * name is retained — see the contract above on why the message is dropped.
 */
type ErrorBoundaryState = {
  errorName: string | null;
};

/** Ties the fallback card's <section> to its own heading for screen readers. */
const FALLBACK_TITLE_ID = "viewErrorBoundaryTitle";

/** Short, non-technical explanation shown under the fallback card's heading. */
const FALLBACK_MESSAGE =
  "Something in this section failed to render. The rest of the console is " +
  "still working — try again, or switch to another view.";

/**
 * Name a thrown value by its error constructor, falling back to "Error" for a
 * non-Error throw (a bare string or object) or an Error with a blank name.
 * Total by construction: the fallback card always has something to display.
 */
const errorNameOf = (error: unknown): string => {
  if (error instanceof Error && error.name.trim() !== "") {
    return error.name;
  }
  return "Error";
};

/**
 * Catch render-time errors from the subtree below and show a themed fallback
 * card in place of the crashed view, keeping the surrounding shell mounted.
 */
class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  /** Start healthy: children render untouched until something throws. */
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { errorName: null };
  }

  /**
   * React's render-phase hook: swap to the fallback state for the next render.
   * Static and side-effect free by contract, so it only derives the name.
   */
  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { errorName: errorNameOf(error) };
  }

  /**
   * React's commit-phase hook: log the FULL error and the component stack for
   * a developer. This is the only place the message survives — it goes to the
   * console, never into the rendered card.
   */
  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error("[ErrorBoundary] view render failed:", error, info.componentStack);
  }

  /**
   * Clear the caught error so the children are attempted again. Operator-driven
   * on purpose: an automatic retry would loop forever on a deterministic crash.
   */
  private readonly handleRetry = (): void => {
    this.setState({ errorName: null });
  };

  /** Render the guarded children, or the fallback card once one has thrown. */
  render(): ReactNode {
    const { errorName } = this.state;
    if (errorName === null) {
      return this.props.children;
    }
    return (
      <section
        className="panel"
        role="alert"
        aria-labelledby={FALLBACK_TITLE_ID}
        data-testid="view-error-fallback"
      >
        <div className="panel-header">
          <div className="panel-title">
            <strong id={FALLBACK_TITLE_ID}>This section could not be displayed</strong>
            <span>{FALLBACK_MESSAGE}</span>
          </div>
          <Badge tone="red">{errorName}</Badge>
        </div>
        <div className="action-row">
          <button className="ghost-button" type="button" onClick={this.handleRetry}>
            Try again
          </button>
        </div>
      </section>
    );
  }
}

export default ErrorBoundary;
