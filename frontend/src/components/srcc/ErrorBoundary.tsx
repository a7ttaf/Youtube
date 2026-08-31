import { Component, createRef, type ErrorInfo, type ReactNode } from "react";

import { Badge } from "./shared";

// ============================================================================
// Purpose: Contain an active-view render crash to one sanitized recovery panel
//   while the navigation shell remains mounted.
// Database/ORM: None (frontend presentation only).
// Standards: Only an allowlisted error category and an opaque correlation ID
//   reach approved telemetry or the DOM. Raw errors, names, messages, stacks,
//   component stacks, and object payloads never leave this component. Recovery
//   reloads the document rather than retrying a write-capable subtree in place.
// Blast Radius: Frontend availability, recovery, focus, and diagnostic privacy.
//   No finance, authorization, audit, export, or write contract changes.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx -> keyed view wrapper and
//     shell-wide write latch that disables reload until the write settles.
//   - File: frontend/src/main.tsx -> shares the sanitized reporting contract.
// ============================================================================

const ERROR_CATEGORIES = [
  "AggregateError",
  "Error",
  "EvalError",
  "RangeError",
  "ReferenceError",
  "SyntaxError",
  "TypeError",
  "URIError",
] as const;

export type ErrorBoundaryCategory = (typeof ERROR_CATEGORIES)[number];

/** Safe telemetry payload; it intentionally has no Error or stack fields. */
export type ErrorBoundaryReport = Readonly<{
  category: ErrorBoundaryCategory;
  correlationId: string;
}>;

/** Injectable browser entropy surface used by the correlation-id generator. */
export type CorrelationEntropySource = Readonly<{
  randomUUID?: () => string;
  getRandomValues?: (array: Uint32Array) => Uint32Array;
}>;

type ErrorBoundaryProps = {
  children: ReactNode;
  /** True while a non-abortable write below the boundary has not settled. */
  recoveryDisabled?: boolean;
  /** Performs a full document reload so server-backed state is reconciled. */
  onReload?: () => void;
  /** Optional approved sink; it receives only ErrorBoundaryReport. */
  onReport?: (report: ErrorBoundaryReport) => void;
};

type ErrorBoundaryState = {
  errorCategory: ErrorBoundaryCategory | null;
  correlationId: string | null;
};

const ERROR_CATEGORY_ALLOWLIST: ReadonlySet<string> = new Set(ERROR_CATEGORIES);
const FALLBACK_TITLE_ID = "viewErrorBoundaryTitle";
const FALLBACK_MESSAGE_ID = "viewErrorBoundaryMessage";
const FALLBACK_REFERENCE_ID = "viewErrorBoundaryReference";
const FALLBACK_MESSAGE =
  "Something in this section failed to render. Reload to reconcile the latest " +
  "server state. A write may already have committed, so confirm its outcome " +
  "before retrying any action.";
const WRITE_RECOVERY_NOTE =
  "Wait for the active write to finish before reloading or leaving this section.";

const CORRELATION_UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CORRELATION_SENTINEL = 0xa5a5a5a5;
let correlationSequence = 0;

// ============================================================================
// Purpose: Read fresh browser entropy without letting restricted or hostile
//   execution contexts break the render fallback.
// Database/ORM: None.
// Standards: No error, user, tenant, finance, or timestamp data enters the ID.
// Blast Radius: Correlation uniqueness and telemetry support only.
// Connections:
//   - File: frontend/tests/components/srcc/ErrorBoundary.test.tsx -> injected
//     deterministic and hostile entropy coverage.
// ============================================================================
const browserEntropySource = (): CorrelationEntropySource | undefined => {
  try {
    return globalThis.crypto as unknown as CorrelationEntropySource;
  } catch {
    return undefined;
  }
};

/** Return a 128-bit hex fragment only when Web Crypto supplies fresh entropy. */
const randomValuesPartOf = (
  source: CorrelationEntropySource | undefined,
): string | null => {
  try {
    if (typeof source?.getRandomValues !== "function") return null;
    const words = new Uint32Array([
      CORRELATION_SENTINEL,
      CORRELATION_SENTINEL,
      CORRELATION_SENTINEL,
      CORRELATION_SENTINEL,
    ]);
    source.getRandomValues(words);
    if (words.every((word) => word === CORRELATION_SENTINEL)) return null;
    return Array.from(words, (word) => word.toString(16).padStart(8, "0")).join("");
  } catch {
    return null;
  }
};

/** Return a non-throwing final entropy fragment when Web Crypto is unavailable. */
const lastResortEntropyPart = (): string => {
  try {
    const random = Math.random();
    if (Number.isFinite(random)) {
      return Math.trunc(random * 2 ** 32).toString(36);
    }
  } catch {
    // The final reference must remain available in a hostile environment.
  }
  return "noentropy";
};

// ============================================================================
// Purpose: Generate a non-sensitive reference for one render failure.
// Database/ORM: None.
// Standards: Prefer UUID, then fresh getRandomValues entropy, then a
//   non-throwing last resort. The value contains no thrown or application data.
// Blast Radius: Telemetry correlation and operator support only.
// Connections:
//   - File: frontend/src/main.tsx -> sanitized root-level reports.
//   - File: frontend/tests/components/srcc/ErrorBoundary.test.tsx -> generator
//     regression coverage.
// ============================================================================
export const correlationIdOf = (
  source: CorrelationEntropySource | undefined = browserEntropySource(),
): string => {
  try {
    const uuid = source?.randomUUID?.();
    if (typeof uuid === "string" && CORRELATION_UUID_PATTERN.test(uuid)) {
      return uuid;
    }
  } catch {
    // Fall through to fresh random values when UUID generation is unavailable.
  }

  const randomValuesPart = randomValuesPartOf(source);
  if (randomValuesPart !== null) return `view-error-${randomValuesPart}`;

  correlationSequence += 1;
  return `view-error-${lastResortEntropyPart()}-${correlationSequence.toString(36)}`;
};

// ============================================================================
// Purpose: Normalize any thrown value to a safe, allowlisted error category.
// Database/ORM: None.
// Standards: Hostile, malformed, custom, or throwing Error.name values collapse
//   to Error; arbitrary names never enter the DOM or telemetry.
// Blast Radius: Error-card category and diagnostic privacy only.
// Connections:
//   - File: frontend/tests/components/srcc/ErrorBoundary.test.tsx -> hostile
//     payload and name-getter regression coverage.
// ============================================================================
export const errorCategoryOf = (error: unknown): ErrorBoundaryCategory => {
  try {
    if (!(error instanceof Error)) return "Error";
    const candidate = error.name;
    if (typeof candidate !== "string") return "Error";
    const normalized = candidate.trim();
    return ERROR_CATEGORY_ALLOWLIST.has(normalized)
      ? (normalized as ErrorBoundaryCategory)
      : "Error";
  } catch {
    return "Error";
  }
};

// ============================================================================
// Purpose: Build the only payload allowed across a render-error reporting
//   boundary, shared by the view boundary and React root handlers.
// Database/ORM: None.
// Standards: The frozen result retains no raw error or component-stack data.
// Blast Radius: Client telemetry shape and operator correlation only.
// Connections:
//   - File: frontend/src/main.tsx -> root-level React error callbacks.
// ============================================================================
export const safeErrorReportOf = (
  error: unknown,
  source?: CorrelationEntropySource,
): ErrorBoundaryReport =>
  Object.freeze({
    category: errorCategoryOf(error),
    correlationId: correlationIdOf(source),
  });

// ============================================================================
// Purpose: Request a complete document reload after a render failure so every
//   server-backed read is reconciled before another operator action.
// Database/ORM: None; the subsequent application bootstrap re-fetches state.
// Standards: Never retry a possibly write-capable child subtree in place.
// Blast Radius: Full frontend reload; no backend mutation from this helper.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx -> fallback action.
// ============================================================================
export const reloadDocumentForRecovery = (): void => {
  window.location.reload();
};

/** Catch render failures and expose only a sanitized, reload-based fallback. */
class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  private readonly fallbackRef = createRef<HTMLElement>();

  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { errorCategory: null, correlationId: null };
  }

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { errorCategory: errorCategoryOf(error), correlationId: null };
  }

  componentDidCatch(error: unknown, _info: ErrorInfo): void {
    const report = safeErrorReportOf(error);
    this.setState({
      errorCategory: report.category,
      correlationId: report.correlationId,
    });

    try {
      if (this.props.onReport) {
        this.props.onReport(report);
      } else {
        console.error("[ErrorBoundary] view render failed", report);
      }
    } catch {
      // FIX: An approved sink must not become a second render failure, and the
      // fallback diagnostic remains restricted to the already-sanitized event.
      try {
        console.error("[ErrorBoundary] report delivery failed", report);
      } catch {
        // A hostile console must not prevent the recovery UI from remaining up.
      }
    }

    // componentDidCatch runs after the fallback has committed. Transfer focus
    // so keyboard and screen-reader users land on the recovery region.
    try {
      this.fallbackRef.current?.focus();
    } catch {
      // Focus failure is non-fatal; the alert remains visible and labelled.
    }
  }

  private readonly handleReload = (): void => {
    // FIX: A disabled button protects pointer/keyboard input; this handler guard
    // also prevents programmatic invocation while an unabortable write is live.
    if (this.props.recoveryDisabled) return;
    (this.props.onReload ?? reloadDocumentForRecovery)();
  };

  render(): ReactNode {
    const { errorCategory, correlationId } = this.state;
    if (errorCategory === null) return this.props.children;

    return (
      <section
        ref={this.fallbackRef}
        className="panel"
        role="alert"
        tabIndex={-1}
        aria-labelledby={FALLBACK_TITLE_ID}
        aria-describedby={`${FALLBACK_MESSAGE_ID} ${FALLBACK_REFERENCE_ID}`}
        data-testid="view-error-fallback"
      >
        <div className="panel-header">
          <div className="panel-title">
            <strong id={FALLBACK_TITLE_ID}>This section could not be displayed</strong>
            <span id={FALLBACK_MESSAGE_ID}>{FALLBACK_MESSAGE}</span>
            <span id={FALLBACK_REFERENCE_ID} data-testid="view-error-correlation-id">
              Reference: {correlationId ?? "pending"}
            </span>
          </div>
          <Badge tone="red">{errorCategory}</Badge>
        </div>
        <div className="action-row">
          {this.props.recoveryDisabled ? <span>{WRITE_RECOVERY_NOTE}</span> : null}
          <button
            className="ghost-button"
            type="button"
            disabled={this.props.recoveryDisabled}
            title={this.props.recoveryDisabled ? WRITE_RECOVERY_NOTE : undefined}
            onClick={this.handleReload}
          >
            Reload and reconcile
          </button>
        </div>
      </section>
    );
  }
}

export default ErrorBoundary;
