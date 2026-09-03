import { Component, createRef, type ErrorInfo, type ReactNode } from "react";

import { Badge } from "./shared";

// ============================================================================
// Purpose: Contain a render-time crash to the guarded view subtree while the
//   surrounding authenticated shell remains mounted. Navigation remains
//   subject to the shell's active-write latch while an unabortable request is
//   unresolved.
// Database/ORM: None (frontend presentation only).
// Standards: Class lifecycle hooks are required for render-error boundaries.
//   Error reporting is a safe event containing only an allowlisted category and
//   correlation ID; raw errors, messages, and component stacks never leave this
//   component, including in production logs. Recovery reloads the document so
//   server-backed state is fetched again before an operator retries an action.
// Blast Radius: View availability, recovery, and client-side observability;
//   no authorization decision or finance calculation is changed here.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx -> supplies the active
//     view reset key and mounts the routed view subtree inside this boundary.
//   - File: frontend/src/components/srcc/shared.tsx -> Badge styling for the
//     allowlisted error category.
//   - File: frontend/src/styles.css -> panel, focus, and action-row styles.
// ============================================================================

/** The category every value the allowlist cannot confirm normalizes to. One
 * constant, because the allowlist entry and the fallback returns must never
 * drift apart — a renamed literal in one place and not the others would let
 * an unknown error name render as a badge the panel never declared. */
const FALLBACK_ERROR_CATEGORY = "Error";

const ERROR_CATEGORIES = [
  "AggregateError",
  FALLBACK_ERROR_CATEGORY,
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
  /** Changes clear a stale fallback without remounting the guarded subtree. */
  resetKey?: string;
  /** Performs a full document reload so all server-backed state is reconciled. */
  onReload?: () => void;
  /** Optional approved sink; it receives only ErrorBoundaryReport. */
  onReport?: (report: ErrorBoundaryReport) => void;
};

type ErrorBoundaryState = {
  errorCategory: ErrorBoundaryCategory | null;
  correlationId: string | null;
  failedResetKey: string | null;
};

const ERROR_CATEGORY_ALLOWLIST: ReadonlySet<string> = new Set(ERROR_CATEGORIES);
const DEFAULT_RESET_KEY = "__default__";
const FALLBACK_TITLE_ID = "viewErrorBoundaryTitle";
const FALLBACK_MESSAGE_ID = "viewErrorBoundaryMessage";
const FALLBACK_REFERENCE_ID = "viewErrorBoundaryReference";
const FALLBACK_MESSAGE =
  "Something in this section failed to render. Reload to reconcile the latest " +
  "server state. A write may already have committed, so confirm its outcome " +
  "before retrying any action.";

const CORRELATION_UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CORRELATION_SENTINEL = 0xa5a5a5a5;
let correlationSequence = 0;

/** Read browser crypto without trusting access to the global getter. */
const browserEntropySource = (): CorrelationEntropySource | undefined => {
  try {
    return globalThis.crypto as unknown as CorrelationEntropySource;
  } catch {
    return undefined;
  }
};

/** Return a 128-bit hex fragment only when Web Crypto supplies real entropy. */
const randomValuesPartOf = (
  source: CorrelationEntropySource | undefined,
): string | null => {
  try {
    if (typeof source?.getRandomValues !== "function") {
      return null;
    }
    const words = new Uint32Array([
      CORRELATION_SENTINEL,
      CORRELATION_SENTINEL,
      CORRELATION_SENTINEL,
      CORRELATION_SENTINEL,
    ]);
    source.getRandomValues(words);
    if (words.every((word) => word === CORRELATION_SENTINEL)) {
      return null;
    }
    return Array.from(words, (word) => word.toString(16).padStart(8, "0")).join(
      "",
    );
  } catch {
    return null;
  }
};

/** Return a non-throwing final entropy fragment when browser crypto is unavailable. */
const lastResortEntropyPart = (): string => {
  try {
    const random = Math.random();
    if (Number.isFinite(random)) {
      return Math.trunc(random * 2 ** 32).toString(36);
    }
  } catch {
    // The final reference still must not throw when browser primitives are
    // unavailable or hostile.
  }
  return "noentropy";
};

/** Generate a non-sensitive reference without retaining raw error data. */
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
  if (randomValuesPart !== null) {
    return `view-error-${randomValuesPart}`;
  }

  correlationSequence += 1;
  return `view-error-${lastResortEntropyPart()}-${correlationSequence.toString(36)}`;
};

/** Normalize any thrown value to a safe, allowlisted category. */
export const errorCategoryOf = (error: unknown): ErrorBoundaryCategory => {
  try {
    if (!(error instanceof Error)) {
      return FALLBACK_ERROR_CATEGORY;
    }
    const candidate = error.name;
    if (typeof candidate !== "string") {
      return FALLBACK_ERROR_CATEGORY;
    }
    const normalized = candidate.trim();
    return ERROR_CATEGORY_ALLOWLIST.has(normalized)
      ? (normalized as ErrorBoundaryCategory)
      : FALLBACK_ERROR_CATEGORY;
  } catch {
    return FALLBACK_ERROR_CATEGORY;
  }
};

/** Build the only payload allowed across the render-error reporting boundary. */
export const safeErrorReportOf = (
  error: unknown,
  source?: CorrelationEntropySource,
): ErrorBoundaryReport =>
  Object.freeze({
    category: errorCategoryOf(error),
    correlationId: correlationIdOf(source),
  });

/** Return the stable reset identity used to clear a prior view failure. */
const resetKeyOf = ({ resetKey }: ErrorBoundaryProps): string =>
  resetKey ?? DEFAULT_RESET_KEY;

/** Reload the document so recovery re-fetches server-backed state. */
export const reloadDocumentForRecovery = (): void => {
  window.location.reload();
};

/**
 * Catch render-time errors from the guarded subtree and show a safe fallback.
 * Navigation clears a stale fallback through resetKey; the action itself always
 * performs a full reload rather than remounting a write-capable child.
 */
class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  private readonly fallbackRef = createRef<HTMLElement>();

  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = {
      errorCategory: null,
      correlationId: null,
      failedResetKey: null,
    };
  }

  /** Clear a prior view failure when navigation changes the guarded identity. */
  static getDerivedStateFromProps(
    props: ErrorBoundaryProps,
    state: ErrorBoundaryState,
  ): ErrorBoundaryState | null {
    if (
      state.errorCategory !== null &&
      state.failedResetKey !== null &&
      state.failedResetKey !== resetKeyOf(props)
    ) {
      return {
        errorCategory: null,
        correlationId: null,
        failedResetKey: null,
      };
    }
    return null;
  }

  /** Derive only the safe category during React's render-error recovery pass. */
  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return {
      errorCategory: errorCategoryOf(error),
      correlationId: null,
      failedResetKey: null,
    };
  }

  /**
   * Emit only the safe category/reference pair. The ErrorInfo argument is
   * deliberately ignored: component stacks can disclose implementation detail
   * and are not needed to reconcile or correlate the failure.
   */
  componentDidCatch(error: unknown, _info: ErrorInfo): void {
    const report = safeErrorReportOf(error);
    this.setState({
      errorCategory: report.category,
      correlationId: report.correlationId,
      failedResetKey: resetKeyOf(this.props),
    });

    try {
      if (this.props.onReport) {
        this.props.onReport(report);
      } else {
        console.error("[ErrorBoundary] view render failed", report);
      }
    } catch {
      // A custom sink must not become a second render failure. The fallback
      // report remains safe if the sink itself rejects the event.
      try {
        console.error("[ErrorBoundary] report delivery failed", report);
      } catch {
        // The console itself is the failing sink here. Mark the already
        // rendered card as the record of the failed delivery instead of
        // letting a second throw escape the lifecycle method — that throw
        // would unmount the surviving shell this boundary exists to keep.
        this.fallbackRef.current?.setAttribute("data-report-delivery", "failed");
      }
    }

    // componentDidCatch runs after the fallback DOM is committed, so focus the
    // actionable recovery region for keyboard and screen-reader users.
    this.fallbackRef.current?.focus();
  }

  /** Reload/re-fetch instead of retrying a possibly committed child write. */
  private readonly handleReload = (): void => {
    (this.props.onReload ?? reloadDocumentForRecovery)();
  };

  /** Render healthy children or the sanitized recovery card after a crash. */
  render(): ReactNode {
    const { errorCategory, correlationId } = this.state;
    if (errorCategory === null) {
      return this.props.children;
    }

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
          <button className="ghost-button" type="button" onClick={this.handleReload}>
            Reload and reconcile
          </button>
        </div>
      </section>
    );
  }
}

export default ErrorBoundary;
