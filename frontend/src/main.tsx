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
//   failure on the reporting path itself is RECORDED in an in-memory trail as
//   sanitized stage/timestamp metadata (the raw failure object is dropped)
//   and degraded to a different channel — never silently discarded, never
//   re-raised into React, never retained in raw form.
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

/** Which step of the reporting pipeline failed, named ONCE so the stage type
 * and the recording call sites cannot drift apart on a spelling. Fixed,
 * allowlisted identifiers — never the failure's own message, stack, or
 * fields, which can carry exactly the raw error data the sanitized-report
 * contract keeps off every channel. */
const REPORT_FAILURE_REPORT_BUILD = "report-build";
const REPORT_FAILURE_PRIMARY_SINK = "primary-sink";
const REPORT_FAILURE_WARN_FALLBACK = "warn-fallback";

type RootReportFailureStage =
  | typeof REPORT_FAILURE_REPORT_BUILD
  | typeof REPORT_FAILURE_PRIMARY_SINK
  | typeof REPORT_FAILURE_WARN_FALLBACK;

/** A safe, non-retaining record of one failure on the reporting path. */
type RootReportFailure = Readonly<{
  stage: RootReportFailureStage;
  /** When the failure was recorded, in ms since the epoch; null when the
   * clock itself was unavailable, so the entry stays truthful. */
  at: number | null;
}>;

/**
 * The recording clock, guarded. `Date.now` is an overridable global, and a
 * hostile or failing one must never throw out of a root error callback —
 * which is the guarantee this whole reporting path exists to keep. A dead
 * clock becomes an explicit null timestamp, not an exception and not a
 * fabricated time.
 */
const safeTimestampOf = (): number | null => {
  try {
    const now = Date.now();
    return Number.isFinite(now) ? now : null;
  } catch {
    // Explicit conversion, not a swallow: the unavailable clock degrades to
    // a null timestamp and the failure's stage is still recorded.
    return null;
  }
};

/**
 * Failures that escaped while PRODUCING or EMITTING a root report, oldest
 * first, keeping the most recent MAX_RECORDED_REPORT_FAILURES entries. The
 * reporting path must never raise (a throwing sink would
 * become a second uncaught application error), but a swallowed failure would
 * hide broken diagnostics — so every escape lands here as SAFE METADATA ONLY.
 * The caught failure objects are never retained anywhere: a reporting failure
 * can carry the very messages, stacks, and custom fields this module exists
 * to keep out of every channel.
 */
const rootReportSinkFailures: RootReportFailure[] = [];

/**
 * Snapshot of the sink-failure trail, oldest first. A defensive COPY, not the
 * backing array; the internal array never escapes, so it can never be mutated
 * or frozen from outside (a frozen backing array would make the next push
 * throw from the root error callback).
 */
// FIX: the trail stored the raw caught failure objects and handed them out of
// the module; a reporting failure can carry sensitive data the sanitize
// contract keeps off every channel, so entries are now stage + timestamp only.
export const recordedRootReportFailures = (): readonly RootReportFailure[] => {
  return rootReportSinkFailures.map((failure) => ({ ...failure }));
};

/**
 * Record an escape from the reporting path itself — the observable handling
 * that replaces a silent catch. Every step is guarded (the clock helper, the
 * push on a never-escaping array, and the warn channel below), because this
 * runs inside catch blocks of the root error callbacks: NOTHING here may
 * throw into React. The degraded notice goes to console.warn, a DIFFERENT
 * channel, because console.error may be the very sink that just failed; if
 * even warn is gone, that failure is recorded too and the trail remains the
 * record. The caught failure object is dropped, never stored.
 */
const recordRootReportFailure = (stage: RootReportFailureStage): void => {
  // Aging, not skipping. Once the trail is saturated the OLDEST entry is
  // evicted so the newest failure is still recorded — a silent skip at the
  // cap would swallow exactly the evidence the trail exists to keep, and a
  // catch that records nothing violates the no-silent-swallow rule.
  if (rootReportSinkFailures.length >= MAX_RECORDED_REPORT_FAILURES) {
    rootReportSinkFailures.shift();
  }
  rootReportSinkFailures.push({ stage, at: safeTimestampOf() });
  try {
    // Safe payload only: the trail size — never a raw failure's contents.
    console.warn("[ReactRoot] root error report could not be emitted", {
      recordedFailures: rootReportSinkFailures.length,
    });
  } catch {
    // Even the warn channel is gone; this fallback failure is recorded with
    // the same aging instead of being dropped. Push directly rather than
    // recursing into this helper — retrying warn here would loop forever.
    if (rootReportSinkFailures.length >= MAX_RECORDED_REPORT_FAILURES) {
      rootReportSinkFailures.shift();
    }
    rootReportSinkFailures.push({
      stage: REPORT_FAILURE_WARN_FALLBACK,
      at: safeTimestampOf(),
    });
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
  } catch {
    // Report construction failed before any sink was touched; record the
    // escape and fall through to a degraded, still-safe notice.
    recordRootReportFailure(REPORT_FAILURE_REPORT_BUILD);
  }
  if (report !== null) {
    try {
      console.error(`[ReactRoot] ${kind} render failure`, report);
    } catch {
      // The primary sink failed mid-emit; record the escape instead of
      // discarding it or re-entering the same failing channel.
      recordRootReportFailure(REPORT_FAILURE_PRIMARY_SINK);
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
