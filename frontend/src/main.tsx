import { StrictMode, type ErrorInfo } from "react";
import { createRoot } from "react-dom/client";

import AppShell from "@/components/srcc/AppShell";
import { safeErrorReportOf, type ErrorBoundaryReport } from "@/components/srcc/ErrorBoundary";
import { SessionProvider } from "@/contexts/SessionContext";
import { TenantProvider } from "@/contexts/TenantContext";
import "@/styles.css";

// ============================================================================
// Purpose: Replace React 19's default root error reporters with callbacks that
//   never forward raw errors, messages, or component stacks to the console.
// Database/ORM: None (frontend bootstrap and observability only).
// Standards: Boundary-owned errors use a deliberate no-op to avoid duplicate
//   reporting; errors outside that boundary emit only the shared allowlisted
//   category and opaque correlation ID. Diagnostic callbacks never throw: a
//   failure on the reporting path itself is RECORDED in an in-memory trail
//   and degraded to a different channel, never silently discarded and never
//   re-raised into React.
// Blast Radius: Root-level render/recovery telemetry and console hygiene;
//   no authorization or finance behavior is changed here.
// Connections:
//   - File: frontend/src/components/srcc/ErrorBoundary.tsx -> shared safe
//     category and correlation-id contract.
//   - File: frontend/tests/main.test.ts -> production/dev raw-secret probes
//     and the sink-failure recording contract.
// ============================================================================

/** Cap on the in-memory sink-failure trail, so a pathologically broken
 * console in a long-lived tab cannot grow it without bound. */
const MAX_RECORDED_REPORT_FAILURES = 100;

/**
 * Failures that escaped while PRODUCING or EMITTING a root report, oldest
 * first, capped. The reporting path must never raise (a throwing sink would
 * become a second uncaught application error), but a swallowed failure would
 * hide broken diagnostics — so every escape lands here, observable by any
 * in-page inspector and by the tests. Raw failure objects stay in memory
 * only; nothing on this path forwards them to the console.
 */
const rootReportSinkFailures: unknown[] = [];

/**
 * Snapshot of the sink-failure trail, oldest first. A defensive COPY, not the
 * backing array; the internal array never escapes.
 */
// FIX: the accessor returned the live backing array behind a compile-time
// readonly annotation, so a caller could freeze it and the next recording
// push would throw from the root error callback — it returns a fresh copy now.
export const recordedRootReportFailures = (): readonly unknown[] => {
  return [...rootReportSinkFailures];
};

/**
 * Record an escape from the reporting path itself — the observable handling
 * that replaces a silent catch. The degraded notice goes to console.warn, a
 * DIFFERENT channel, because console.error may be the very sink that just
 * failed; if even warn is gone, the failure of THAT channel is recorded too
 * and the trail remains the record. Nothing here rethrows into React.
 */
const recordRootReportFailure = (reportingFailure: unknown): void => {
  if (rootReportSinkFailures.length < MAX_RECORDED_REPORT_FAILURES) {
    rootReportSinkFailures.push(reportingFailure);
  }
  try {
    // Safe payload only: kind and the trail size — never the failure's own
    // message, which has not passed the same sanitization as a report.
    console.warn("[ReactRoot] root error report could not be emitted", {
      recordedFailures: rootReportSinkFailures.length,
    });
  } catch (warnFailure) {
    if (rootReportSinkFailures.length < MAX_RECORDED_REPORT_FAILURES) {
      rootReportSinkFailures.push(warnFailure);
    }
  }
};

/** Build the safe report and emit it to console.error; a failure anywhere on
 * this path is recorded and degraded, never re-raised into React. */
const emitSafeRootReport = (
  kind: "uncaught" | "recoverable",
  error: unknown,
): void => {
  let report: ErrorBoundaryReport | null = null;
  try {
    report = safeErrorReportOf(error);
  } catch (reportFailure) {
    // Report construction failed before any sink was touched; record the
    // escape and fall through to a degraded, still-safe notice.
    recordRootReportFailure(reportFailure);
  }
  if (report !== null) {
    try {
      console.error(`[ReactRoot] ${kind} render failure`, report);
    } catch (sinkFailure) {
      // The primary sink failed mid-emit; record the escape instead of
      // discarding it or re-entering the same failing channel.
      recordRootReportFailure(sinkFailure);
    }
  }
};

/** ErrorBoundary.componentDidCatch owns safe reporting for caught subtree errors. */
export const onCaughtError = (_error: unknown, _info: ErrorInfo): void => undefined;

/** Report an error React could not contain inside an ErrorBoundary. */
export const onUncaughtError = (error: unknown, _info: ErrorInfo): void => {
  emitSafeRootReport("uncaught", error);
};

/** Keep React's recoverable path from falling back to raw console diagnostics. */
export const onRecoverableError = (error: unknown, _info: ErrorInfo): void => {
  emitSafeRootReport("recoverable", error);
};

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("Root element #root not found in document");
}
createRoot(rootEl, {
  onCaughtError,
  onUncaughtError,
  onRecoverableError,
}).render(
  <StrictMode>
    <SessionProvider>
      <TenantProvider>
        <AppShell />
      </TenantProvider>
    </SessionProvider>
  </StrictMode>,
);
