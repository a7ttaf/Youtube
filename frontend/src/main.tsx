import { StrictMode, type ErrorInfo } from "react";
import { createRoot } from "react-dom/client";

import AppShell from "@/components/srcc/AppShell";
import { safeErrorReportOf } from "@/components/srcc/ErrorBoundary";
import { SessionProvider } from "@/contexts/SessionContext";
import { TenantProvider } from "@/contexts/TenantContext";
import "@/styles.css";

// ============================================================================
// Purpose: Replace React 19's default root error reporters with callbacks that
//   never forward raw errors, messages, or component stacks to the console.
// Database/ORM: None (frontend bootstrap and observability only).
// Standards: Boundary-owned errors use a deliberate no-op to avoid duplicate
//   reporting; errors outside that boundary emit only the shared allowlisted
//   category and opaque correlation ID. Diagnostic callbacks never throw.
// Blast Radius: Root-level render/recovery telemetry and console hygiene;
//   no authorization or finance behavior is changed here.
// Connections:
//   - File: frontend/src/components/srcc/ErrorBoundary.tsx -> shared safe
//     category and correlation-id contract.
//   - File: frontend/tests/main.test.ts -> production/dev raw-secret probes.
// ============================================================================
const emitSafeRootReport = (
  kind: "uncaught" | "recoverable",
  error: unknown,
): void => {
  try {
    const report = safeErrorReportOf(error);
    console.error(`[ReactRoot] ${kind} render failure`, report);
  } catch {
    // A diagnostic sink must never become a second uncaught application error.
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
