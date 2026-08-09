import { useCallback, useRef, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type { ConnectorTestResult } from "@/lib/api/types";

// FIXED reason recorded on the CONNECTOR_TESTED audit event for the one-click
// probe; the operator does not type a reason for a health check.
const TEST_CONNECTION_REASON = "operator connection health check";

/** Stable per-row key combining the connector key and account id. */
const probeKey = (connector_key: string, account_id: string): string =>
  `${connector_key}::${account_id}`;

/**
 * Remove a single key from a record without mutating the original object.
 */
const omitRecordKey = <T extends Record<string, unknown>>(
  record: T,
  key: string,
): T => {
  return Object.fromEntries(
    Object.entries(record).filter(([entryKey]) => entryKey !== key),
  ) as T;
};

/** Pull a safe `detail` string off an ApiError body when present. */
const extractDetail = (error: ApiError): string | null => {
  const body = error.body;
  if (
    typeof body === "object" &&
    body !== null &&
    typeof (body as { detail?: unknown }).detail === "string"
  ) {
    return (body as { detail: string }).detail;
  }
  return null;
};

export type UseConnectorTestState = {
  // Per-row latest probe result, keyed by `${connector_key}::${account_id}`.
  results: Record<string, ConnectorTestResult>;
  // Per-row typed error (non-404 failures), keyed the same way.
  errors: Record<string, ApiError | Error>;
  // Per-row in-flight flag, keyed the same way.
  pending: Record<string, boolean>;
  // Trigger the probe for one credential row. Resolves with the ConnectorTestResult
  // (a 404 is mapped to a not_found result, NOT a throw), with `null` when a
  // same-row in-flight probe is already running (the duplicate is dropped), or
  // rejects with the typed error already captured in `errors`.
  test: (
    connector_key: string,
    account_id: string,
  ) => Promise<ConnectorTestResult | null>;
};

// ============================================================================
// Purpose: Imperative action hook for the per-credential "Test" connection
//   probe. POSTs a FIXED reason to /connectors/credentials/{key}/{account}/test
//   on the production useApiClient and tracks per-row {results, errors, pending}
//   so each credential row reflects only its own probe. The backend audits a
//   CONNECTOR_TESTED event; this only refreshes OAuth (no live data pull).
// Database/ORM: None (frontend) — calls the backend test-connection route, which
//   records a CONNECTOR_TESTED audit event server-side.
// Standards: the body is JSON-encoded by useApiClient. A 404 (not_found
//   credential) is a MODELED outcome of the probe, so the typed ApiError(404) is
//   caught and mapped to a not_found ConnectorTestResult rather than surfaced as
//   an unhandled error; every other HTTP error (403/422/5xx) stays a typed error
//   for the view to translate via describeError. No client-side authorization is
//   invented — the backend MANAGE_CONNECTORS gate is authoritative.
// Blast Radius: Audit write only (no finance number, no live connector pull) via
//   the backend's own guarded, audited route. No graph projection impact.
// Dedupe: a per-row synchronous in-flight latch drops a same-row duplicate probe
//   before the POST fires (a double-click would otherwise record two
//   CONNECTOR_TESTED events). Distinct rows probe independently.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST + ApiError.
//   - File: frontend/src/lib/api/types.ts -> ConnectorTestResult.
//   - File: backend/ums_smart_revenue/api/connectors.py -> test_connector_connection.
// ============================================================================
export const useConnectorTest = (): UseConnectorTestState => {
  const client = useApiClient();
  const [results, setResults] = useState<Record<string, ConnectorTestResult>>({});
  const [errors, setErrors] = useState<Record<string, ApiError | Error>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  // Synchronous per-row in-flight latch so a same-row duplicate probe is dropped
  // before its POST fires (the state `pending` guard cannot see it in time).
  const inFlightRef = useRef<Record<string, boolean>>({});

  const test = useCallback(
    (
      connector_key: string,
      account_id: string,
    ): Promise<ConnectorTestResult | null> => {
      const key = probeKey(connector_key, account_id);
      // Drop a same-row duplicate probe before the POST fires; a double-click
      // before re-render would otherwise record two CONNECTOR_TESTED events.
      if (inFlightRef.current[key]) return Promise.resolve(null);
      inFlightRef.current[key] = true;
      // Clear the prior result/error for this row and latch it as in flight.
      setResults((prev) => omitRecordKey(prev, key));
      setErrors((prev) => omitRecordKey(prev, key));
      setPending((prev) => ({ ...prev, [key]: true }));

      const path = `/connectors/credentials/${encodeURIComponent(
        connector_key,
      )}/${encodeURIComponent(account_id)}/test`;

      return client
        .post<ConnectorTestResult>(path, { reason: TEST_CONNECTION_REASON })
        .then((result) => {
          setResults((prev) => ({ ...prev, [key]: result }));
          return result;
        })
        .catch((caught: unknown) => {
          // FIX: a 404 means the credential was not found — a MODELED probe
          // outcome, not an exceptional failure. Map it to a not_found result so
          // the row shows the red not_found badge instead of an unhandled throw.
          if (caught instanceof ApiError && caught.status === 404) {
            const notFound: ConnectorTestResult = {
              connector_key,
              account_id,
              status: "not_found",
              detail: extractDetail(caught) ?? "Credential not found.",
              audit_event: {},
            };
            setResults((prev) => ({ ...prev, [key]: notFound }));
            return notFound;
          }
          const typed =
            caught instanceof Error ? caught : new Error(String(caught));
          setErrors((prev) => ({ ...prev, [key]: typed }));
          throw typed;
        })
        .finally(() => {
          inFlightRef.current[key] = false;
          setPending((prev) => ({ ...prev, [key]: false }));
        });
    },
    [client],
  );

  return { results, errors, pending, test };
};
