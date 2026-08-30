import { Component, type ReactNode } from "react";

import { Badge } from "./shared";

// ============================================================================
// Purpose: Contain an active-view render crash to one fixed-copy panel while
//   the navigation shell stays mounted and offers explicit recovery.
// Database/ORM: None (frontend presentation only).
// Standards: Boolean-only state; thrown names, messages, stacks, component
//   stacks, and object payloads never reach the DOM or console. Recovery is
//   operator-driven so deterministic failures cannot spin in a retry loop.
// Blast Radius: Frontend availability and diagnostic privacy. No finance,
//   authorization, audit, export, or write behavior is changed.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx -> keyed view wrapper.
//   - File: frontend/src/main.tsx -> suppresses React's raw caught-error log.
// ============================================================================

type ErrorBoundaryProps = {
  children: ReactNode;
  /** True while a non-abortable write below the boundary has not settled. */
  recoveryDisabled?: boolean;
};

type ErrorBoundaryState = {
  hasError: boolean;
};

const FALLBACK_TITLE_ID = "viewErrorBoundaryTitle";
const FALLBACK_MESSAGE =
  "Something in this section failed to render. The rest of the control center is " +
  "still working — try again, or switch to another view.";
const RENDER_FAILURE_DIAGNOSTIC = "[ErrorBoundary] view_render_failed";
const WRITE_RECOVERY_NOTE =
  "Wait for the active write to finish before retrying or leaving this section.";

class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(): void {
    // FIX: Ignore React's raw error and component-stack arguments. Either can
    // contain finance values, identifiers, credentials, or private row data.
    console.error(RENDER_FAILURE_DIAGNOSTIC);
  }

  private readonly handleRetry = (): void => {
    this.setState({ hasError: false });
  };

  render(): ReactNode {
    if (!this.state.hasError) {
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
          <Badge tone="red">Render error</Badge>
        </div>
        <div className="action-row">
          {this.props.recoveryDisabled ? <span>{WRITE_RECOVERY_NOTE}</span> : null}
          <button
            className="ghost-button"
            type="button"
            disabled={this.props.recoveryDisabled}
            title={this.props.recoveryDisabled ? WRITE_RECOVERY_NOTE : undefined}
            onClick={this.handleRetry}
          >
            Try again
          </button>
        </div>
      </section>
    );
  }
}

export default ErrorBoundary;
