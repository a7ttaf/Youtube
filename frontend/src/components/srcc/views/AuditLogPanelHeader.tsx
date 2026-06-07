import { useState } from "react";

import { useApiClient } from "@/lib/api/client";
import { buildAuditEventsExportUrl } from "@/lib/api/useAudit";

import { describeError } from "./CommandView";

// Event-type filter options use REAL AuditEventType values (no severity facet
// exists). Labels and values match the Track C spec section 2 table exactly.
const AUDIT_EVENT_TYPE_OPTIONS = [
  { label: "All event types", value: "" },
  { label: "Exports", value: "EXPORT_DOWNLOADED" },
  { label: "Mapping changes", value: "CHANNEL_UPDATED" },
  { label: "Month locks", value: "MONTH_LOCKED" },
  { label: "Allocations committed", value: "ALLOCATION_COMMITTED" },
  { label: "Logins", value: "LOGIN" },
];

/**
 * Read the CSV export truncation flag from the backend response headers.
 * @param headers The response headers to inspect.
 * @returns True when X-Truncated equals "true", ignoring case.
 */
const hasTruncatedExportHeader = (headers: Headers): boolean => {
  return (headers.get("X-Truncated") ?? "").toLowerCase() === "true";
};

// ============================================================================
// Purpose: Trigger a browser save of a blob via a temporary object URL + <a download>.
//   Revokes the URL afterward to avoid leaking it. Extracted so the download hook
//   stays readable.
// Database/ORM: None.
// Standards: Keep browser side-effects isolated and reversible.
// Blast Radius: Audit read/export only.
// ============================================================================
const saveBlobAsFile = (
  blob: Blob,
  filename: string,
): void => {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
};

// ============================================================================
// Purpose: Own the audit CSV download flow and surface truncation and errors
//   next to the filter controls while keeping the panel header declarative.
// Database/ORM: None.
// Standards: Keep browser side-effects isolated; preserve the read-only audit
//   export and shared error handling.
// Blast Radius: Audit read/export only.
// ============================================================================
const useAuditExportDownload = (eventType: string) => {
  const client = useApiClient();
  const [busy, setBusy] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);

  /** Normalize the download failure into the shared audit-screen detail copy. */
  const describeDownloadError = (caught: unknown): string => {
    const asError = caught instanceof Error ? caught : new Error("Download failed");
    return describeError(asError).detail;
  };

  /** Download the current audit slice as CSV and surface truncation state. */
  const download = (): void => {
    if (busy) return;
    setBusy(true);
    setErrorDetail(null);
    setTruncated(false);
    /**
     * Read the current audit slice as CSV, save it, and mark truncation state.
     */
    const downloadAuditCsv = async (): Promise<void> => {
      const url = buildAuditEventsExportUrl(eventType);
      const { blob, headers } = await client.getBlob(url);
      saveBlobAsFile(blob, "audit-events.csv");
      setTruncated(hasTruncatedExportHeader(headers));
    };
    downloadAuditCsv()
      .catch((caught) => {
        setErrorDetail(describeDownloadError(caught));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return { download, busy, truncated, errorDetail };
};

// ============================================================================
// Purpose: Render the audit log controls that drive server-side filtering and
//   CSV export.
// Database/ORM: None.
// Standards: Keep auth / tenant header behavior in the shared API client and
//   avoid inventing client-side permissions.
// Blast Radius: Audit read/export only.
// ============================================================================
const AuditLogPanelHeader = ({
  canViewAudit,
  eventType,
  onEventType,
}: {
  canViewAudit: boolean;
  eventType: string;
  onEventType: (eventType: string) => void;
}) => {
  const isDisabled = !canViewAudit;
  const { download, busy, truncated, errorDetail } = useAuditExportDownload(eventType);

  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="auditTitle">Audit Log</strong>
        <span>Every sensitive action records actor, permission, scope, target, and result</span>
      </div>
      <div className="view-actions">
        <label className="sr-only" htmlFor="auditEventTypeFilter">
          Audit event type
        </label>
        <select
          id="auditEventTypeFilter"
          className="control"
          aria-label="Audit event type"
          value={eventType}
          onChange={(e) => onEventType(e.currentTarget.value)}
          disabled={isDisabled}
        >
          {AUDIT_EVENT_TYPE_OPTIONS.map((option) => (
            <option value={option.value} key={option.value || "ALL"}>
              {option.label}
            </option>
          ))}
        </select>
        <button
          className="ghost-button"
          type="button"
          onClick={download}
          disabled={isDisabled || busy}
        >
          {busy ? "Downloading..." : "Download Audit View"}
        </button>
        {truncated && (
          <span className="item-sub" role="status" style={{ color: "var(--amber, #b45309)" }}>
            Export truncated at 10,000 rows - narrow the filter
          </span>
        )}
        {errorDetail && (
          <span className="item-sub" role="alert" style={{ color: "var(--red, #b91c1c)" }}>
            {errorDetail}
          </span>
        )}
      </div>
    </div>
  );
};

export default AuditLogPanelHeader;
