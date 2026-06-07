import { ApiError, useApiClient } from "@/lib/api/client";
import { type AuditEventPagination, type AuditLogEntry } from "@/lib/api/types";
import { useEffect, useState } from "react";
import type { Severity } from "@/lib/mock/data";
import { AUDIT_SUMMARY } from "@/lib/mock/data";
import { Badge, Dot, ItemRow, SummaryTile, formatTimestamp } from "../shared";
import { describeError } from "./CommandView";
import { buildAuditEventsExportUrl, useAuditEvents } from "@/lib/api/useAudit";

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
 * Checks whether the response headers indicate a truncated export.
 * @param headers - The Headers object from the fetch response.
 * @returns True if the X-Truncated header equals "true", case-insensitive; otherwise false.
 */
/** Read the CSV export truncation flag from the backend response headers. */
function hasTruncatedExportHeader(headers: Headers): boolean { // skipcq: JS-0067
  return (headers.get("X-Truncated") ?? "").toLowerCase() === "true";
}

// ============================================================================
// Purpose: The REAL-data Audit Log screen, extracted from AppShell. The timeline
//   is wired to GET /audit/events via useAuditEvents; the summary tiles and the
//   coverage panel stay static context (the endpoint exposes no aggregate counts
//   and there is no coverage-config read route). Redaction is SERVER-DRIVEN: when
//   an event's details_redacted flag is true the backend has already replaced
//   details with {}, so this view renders a "Sensitive (redacted)" indicator and
//   NEVER offers a reveal control or attempts to surface a redacted payload.
// Database/ORM: None (frontend) — consumes GET /audit/events. Note: each read of
//   that endpoint itself writes one AUDIT_LOG_VIEWED row server-side, so the hook
//   is memoized to fetch once per mount/filter change (no poll, no loop).
// Standards: No client-side authorization is invented. canViewAudit gates the
//   timeline at the COMPONENT boundary (the restricted branch mounts no hook, so
//   it fires no fetch — fail-closed). Whether sensitive payloads are visible is
//   the backend's decision (VIEW_SENSITIVE_AUDIT_PAYLOADS drives details_redacted)
//   — the UI only reflects it. A 403 on the read surfaces as no-permission copy.
// Blast Radius: Audit read only (self-audited server-side via the guarded route).
//   No finance number, no source-of-truth mutation, no Neo4j.
// Connections:
//   - File: frontend/src/lib/api/useAudit.ts -> the audit-events fetch hook.
//   - File: frontend/src/lib/api/types.ts -> AuditLogEntry contract.
//   - File: backend/ums_smart_revenue/api/audit.py:85 list_audit_events.
// ============================================================================

/**
 * The REAL-data Audit Log screen: static summary tiles + coverage panel for
 * context, and the live, server-audited event timeline. `canViewAudit` gates the
 * timeline (a non-audit viewer sees a restricted placeholder and triggers no
 * fetch); `canViewFinance` only drives the finance summary tiles' restricted
 * sentinel — it does NOT decide audit-payload visibility (that is server-driven).
 */
export default function AuditView({ // skipcq: JS-0067
  canViewAudit,
  canViewFinance,
}: {
  canViewAudit: boolean;
  canViewFinance: boolean;
}) {
  return (
    <section className="view-page" aria-labelledby="auditTitle">
      <div className="view-summary" aria-label="Audit summary (static context)">
        {AUDIT_SUMMARY.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>
      {/* Summary counts above are static context — no live aggregate endpoint yet. */}
      <span className="item-sub" role="note" style={{ marginBottom: "0.5rem", display: "block" }}>
        Summary counts above are reference figures — live aggregate endpoint coming.
      </span>

      <div className="view-grid">
        <AuditLogPanel canViewAudit={canViewAudit} />
        <AuditCoveragePanel />
      </div>
    </section>
  );
}

/**
 * Reusable inner content for timeline placeholder rows (restricted, error,
 * loading, empty). Rendered as a fragment inside the caller's `timeline-item`
 * div so each state can control its own wrapper attributes (role, aria-busy,
 * etc.) independently.
 */
function TimelinePlaceholderRow({ tone, title, sub, badge }: { // skipcq: JS-0067
  tone: Severity; title: string; sub: string; badge: string;
}) {
  return (
    <>
      <span className="timeline-time">--:--</span>
      <Dot tone={tone} />
      <span>
        <span className="item-title">{title}</span>
        <span className="item-sub">{sub}</span>
      </span>
      <Badge tone={tone}>{badge}</Badge>
    </>
  );
}

/**
 * Audit event timeline gate. A non-audit viewer sees a single restricted
 * placeholder row and - critically - NO hook is mounted here, so no
 * /audit/events fetch fires (fail-closed). Only when permitted is the
 * always-fetching feed mounted, keeping the useAuditEvents call unconditional
 * (rules-of-hooks safe).
 */
const AuditTimeline = ({
  canViewAudit,
  eventType,
}: {
  canViewAudit: boolean;
  eventType: string | undefined;
}): JSX.Element => {
  if (!canViewAudit) {
    return (
      <div className="timeline" role="list">
        <div className="timeline-item" role="listitem">
          <TimelinePlaceholderRow
            tone="red"
            title="Audit view restricted"
            sub="Audit log access requires the VIEW_AUDIT_LOG permission"
            badge="Restricted"
          />
        </div>
      </div>
    );
  }
  return <AuditTimelineFeed eventType={eventType} />;
/**
 * Hook to handle downloading audit event CSV exports.
 * @param eventType The type of audit events to export.
 * @returns An object containing the download function and related state flags.
 */
};

/**
 * The audit-log main panel: header (title + event-type filter / download
 * actions) and the live audit timeline. Extracted so the AuditView JSX tree
 * stays shallow.
 */
function AuditLogPanel({ canViewAudit }: { canViewAudit: boolean }) { // skipcq: JS-0067
  const [eventType, setEventType] = useState("");

  return (
    <section className="panel">
      <AuditLogPanelHeader
        eventType={eventType}
        onEventType={setEventType}
        canViewAudit={canViewAudit}
      />
      <AuditTimeline
        key={eventType || "all"}
        canViewAudit={canViewAudit}
        eventType={eventType || undefined}
      />
    </section>
  );
}

/**
 * Trigger a browser save of a blob via a temporary object URL + <a download>.
 * Revokes the URL afterward to avoid leaking it. Extracted so the download hook
 * stays readable.
 */
function saveBlobAsFile(blob: Blob, filename: string): void { // skipcq: JS-0067
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}

// ============================================================================
// Purpose: Download-the-audit-CSV hook. Fetches GET /audit/events/export as a
//   BLOB with ONLY the current event_type filter (never cursor params — the
//   export always represents the full filtered set from the beginning), triggers
//   a browser save, and reads the X-Truncated response header. Silent truncation
//   is unacceptable for audit work, so a truncation notice is surfaced when the
//   server caps the result; it persists until the next download attempt.
// Database/ORM: None (frontend) — consumes the read-only CSV route.
// Standards: Same auth/tenant mechanics as the JSON client (client.getBlob ->
//   buildHeaders). In-flight latch prevents double-fire; errors surface via the
//   shared describeError pattern. No client-side authorization is invented.
// Blast Radius: Audit read/export only. No finance math, no mutation, no Neo4j.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient().getBlob.
//   - File: frontend/src/lib/api/useAudit.ts -> buildAuditEventsExportUrl.
//   - File: backend/ums_smart_revenue/api/audit.py -> export route + X-Truncated.
// ============================================================================
function useAuditExportDownload(eventType: string) { // skipcq: JS-0067
  const client = useApiClient();
  const [busy, setBusy] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);

  /** Read the current audit slice as CSV, save it, and mark truncation state. */
  function downloadAuditCsv(): Promise<void> {
    const url = buildAuditEventsExportUrl(eventType);
    return client.getBlob(url).then(({ blob, headers }) => {
      saveBlobAsFile(blob, "audit-events.csv");
      setTruncated(hasTruncatedExportHeader(headers));
    });
  }

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
    void downloadAuditCsv()
      .catch((caught) => {
        setErrorDetail(describeDownloadError(caught));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return { download, busy, truncated, errorDetail };
}

/**
 * Audit-log panel header: title/subtitle and event-type filtering + download
 * controls. The dropdown is wired to the server-supported `event_type` query
 * param (no new backend facet schema), and the Download button fetches GET
 * /audit/events/export as a blob (current filter only, no cursor) so it can read
 * X-Truncated and surface truncation instead of silently dropping rows.
 */
function AuditLogPanelHeader({ // skipcq: JS-0067
  canViewAudit,
  eventType,
  onEventType,
}: {
  canViewAudit: boolean;
  eventType: string;
  onEventType: (eventType: string) => void;
}) {
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
          {busy ? "Downloading…" : "Download Audit View"}
        </button>
        {truncated && (
          <span className="item-sub" role="status" style={{ color: "var(--amber, #b45309)" }}>
            Export truncated at 10,000 rows — narrow the filter
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
}

/**
 * The live audit-event feed. ALWAYS calls useAuditEvents() (it is only mounted
 * when the viewer may see the audit log), so the hook stays unconditional. Maps
 * loading / error (403 -> "No permission") / empty / loaded states, and
 * consumes backend `pagination.next_cursor` for a "Load More" append flow.
 */
function AuditTimelineFeed({ // skipcq: JS-0067, JS-R1005
  eventType,
}: {
  eventType: string | undefined;
}) {
  const [rows, setRows] = useState<AuditLogEntry[]>([]);
  const [pagination, setPagination] = useState<AuditEventPagination | null>(null);
  const [cursorCreatedAt, setCursorCreatedAt] = useState<string>();
  const [cursorId, setCursorId] = useState<string>();

  const { data, loading, error } = useAuditEvents({
    event_type: eventType,
    cursor_created_at: cursorCreatedAt,
    cursor_id: cursorId,
  });

  useEffect(() => {
    if (!data) return;
    setRows((previous) => {
      // First page (no cursor) replaces; subsequent pages append. Dedupe by
      // event id so a StrictMode double-invoke or an overlapping cursor window
      // can never render the same audit row twice.
      if (!(cursorCreatedAt && cursorId)) return data.items;
      const seen = new Set(previous.map((row) => row.id));
      const appended = data.items.filter((row) => !seen.has(row.id));
      return [...previous, ...appended];
    });
    setPagination(data.pagination);
  }, [data, cursorCreatedAt, cursorId]);

  const hasMore = Boolean(pagination?.has_more && pagination.next_cursor);
  const nextCursor = pagination?.next_cursor;
  const loadMoreLabel = loading ? "Loading more…" : "Load More";

  /** Advance to the next cursor page when the backend exposes one. */
  const loadMore = () => {
    if (!nextCursor || loading) return;
    setCursorCreatedAt(nextCursor.created_at);
    setCursorId(nextCursor.id);
  };

  if (error) {
    const described = describeError(error);
    // describeError's 403 detail is net-revenue-specific (the helper is shared
    // with the finance screens). On the audit screen show an audit-appropriate
    // denial instead; the shared helper stays untouched so the finance screens'
    // copy (and their tests) are unaffected.
    const is403 = error instanceof ApiError && error.status === 403;
    const detail = is403
      ? "Your role cannot view the audit log."
      : described.detail;
    return (
      <div className="timeline" role="alert">
        {/* role="alert" already announces this region; an inner role="listitem"
            would be an orphan (no enclosing role="list"), so the row stays a
            plain item div. */}
        <div className="timeline-item">
          <TimelinePlaceholderRow
            tone="red"
            title={described.title}
            sub={detail}
            badge="Error"
          />
        </div>
      </div>
    );
  }

  if (loading && rows.length === 0) {
    return (
      <div className="timeline" role="list" aria-busy="true">
        <div className="timeline-item" role="listitem">
          <TimelinePlaceholderRow
            tone="blue"
            title="Loading audit events…"
            sub="Reading the audit log (this read is itself audited)"
            badge="Loading"
          />
        </div>
      </div>
    );
  }

  const events = rows;
  if (events.length === 0) {
    return (
      <div className="timeline" role="list">
        <div className="timeline-item" role="listitem">
          <TimelinePlaceholderRow
            tone="amber"
            title="No audit events recorded"
            sub="No audit events match the current filters"
            badge="Empty"
          />
        </div>
      </div>
    );
  }

  return (
    <div className="timeline" role="list">
      {events.map((event) => (
        <AuditTimelineItem key={event.id} event={event} />
      ))}
      {hasMore && (
        <div className="timeline-item" role="listitem">
          <button
            className="ghost-button"
            type="button"
            onClick={loadMore}
            disabled={loading}
          >
            {loadMoreLabel}
          </button>
        </div>
      )}
      {loading && rows.length > 0 && (
        <div className="timeline-item" role="listitem">
          <TimelinePlaceholderRow
            tone="blue"
            title="Loading more events"
            sub="Continue fetching the next cursor window."
            badge="Loading"
          />
        </div>
      )}
    </div>
  );
}

/** Format the entity portion of the event subtitle (entity_type + optional id). */
function fmtEntityPart(entity_type: string | null, entity_id: string | null): string | null { // skipcq: JS-0067
  if (!entity_type) return null;
  return entity_id ? `${entity_type}=${entity_id}` : entity_type;
}

/** Format the scope portion of the event subtitle (scope_type + optional id). */
function fmtScopePart(scope_type: string | null, scope_id: string | null): string | null { // skipcq: JS-0067
  if (!scope_type) return null;
  return scope_id ? `${scope_type}:${scope_id}` : `scope=${scope_type}`;
}

/**
 * Build the subtitle line from the audit event's REAL non-null fields only — no
 * invented data. Renders the entity, scope, actor, and reason that are present.
 */
function buildEventSub(event: AuditLogEntry): string { // skipcq: JS-0067
  const parts = [
    fmtEntityPart(event.entity_type, event.entity_id),
    fmtScopePart(event.scope_type, event.scope_id),
    event.user_id ? `actor=${event.user_id}` : null,
    event.reason ? `reason=${event.reason}` : null,
  ].filter(Boolean) as string[];
  // Honest fallback when the event carries no contextual fields at all.
  return parts.length > 0 ? parts.join(" · ") : "No additional context recorded";
}

/**
 * Render a safe summary of non-redacted event details — top-level primitive
 * values only (string, number, boolean). Nested objects are intentionally skipped
 * to avoid surfacing raw structured data in the UI without schema knowledge.
 */
function renderDetailsLine(details: Record<string, unknown>): string | null { // skipcq: JS-0067
  const pairs = Object.entries(details)
    .filter(([, v]) => v !== null && v !== undefined && typeof v !== "object")
    .map(([k, v]) => `${k}=${String(v)}`);
  return pairs.length > 0 ? pairs.join(" · ") : null;
}

/**
 * Optional details note for a timeline entry. When details_redacted is true,
 * shows the server-driven redaction notice. When false and non-empty, renders
 * top-level primitive k=v pairs. Returns null when there is nothing to show.
 * Extracted from AuditTimelineItem to keep its cyclomatic complexity below the
 * analysis threshold.
 */
function DetailsNote({ event }: { event: AuditLogEntry }) { // skipcq: JS-0067
  if (event.details_redacted) {
    return (
      <span className="item-sub" role="note">
        Sensitive (redacted) — payload withheld by the audit service
      </span>
    );
  }
  const line = renderDetailsLine(event.details);
  if (!line) return null;
  return <span className="item-sub" role="note">{line}</span>;
}

/**
 * A single live audit timeline entry. The tone reflects the server-driven
 * `sensitive` flag; when `details_redacted` is true the row shows a clear
 * "Sensitive (redacted)" indicator and renders NO details payload and NO reveal
 * control — the redaction decision is the backend's, never undone client-side.
 * When `details_redacted` is false and the details object is non-empty, the
 * top-level primitive fields are rendered as a safe key=value summary.
 */
function AuditTimelineItem({ event }: { event: AuditLogEntry }) { // skipcq: JS-0067
  const tone: Severity = event.sensitive ? "red" : "green";
  return (
    <div className="timeline-item" role="listitem">
      <span className="timeline-time">{formatTimestamp(event.created_at)}</span>
      <Dot tone={tone} />
      <span>
        <span className="item-title">{event.event_type}</span>
        <span className="item-sub">{buildEventSub(event)}</span>
        <DetailsNote event={event} />
      </span>
      <Badge tone={tone}>{event.sensitive ? "Sensitive" : "Logged"}</Badge>
    </div>
  );
}

/** Static audit coverage panel listing the always-audited sensitive surfaces. */
function AuditCoveragePanel() { // skipcq: JS-0067
  return (
    <aside className="view-stack">
      <section className="panel" aria-labelledby="auditCoverageTitle">
        <AuditCoverageHeader />
        <div className="issue-list" role="list">
          <ItemRow tone="green" title="Revenue reads" sub="Every money cell view emits an audit row"
            trailing={<Badge tone="green">On</Badge>} />
          <ItemRow tone="green" title="Override before/after" sub="Both values stored with reason and approver"
            trailing={<Badge tone="green">On</Badge>} />
          <ItemRow tone="amber" title="Trace queries" sub="Filtered query is audited with allowed scope"
            trailing={<Badge tone="amber">Logged</Badge>} />
        </div>
      </section>
    </aside>
  );
}

/** Audit coverage panel header (title + subtitle). Extracted to keep nesting shallow. */
function AuditCoverageHeader() { // skipcq: JS-0067
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="auditCoverageTitle">Audit Coverage</strong>
        <span>Required to be present for every sensitive surface</span>
      </div>
    </div>
  );
}
