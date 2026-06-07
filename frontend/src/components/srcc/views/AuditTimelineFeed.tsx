import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

import { ApiError } from "@/lib/api/client";
import { type AuditEventPagination, type AuditLogEntry } from "@/lib/api/types";
import type { Severity } from "@/lib/mock/data";

import { Badge, Dot, TimelinePlaceholderRow, formatTimestamp } from "../shared";

import { describeError } from "./CommandView";
import { useAuditEvents } from "@/lib/api/useAudit";

/**
 * Append a new audit page to the existing row set, deduping overlaps when the
 * cursor window repeats the last seen row.
 */
const appendAuditRows = (
  previous: AuditLogEntry[],
  items: AuditLogEntry[],
  cursorCreatedAt: string | undefined,
  cursorId: string | undefined,
): AuditLogEntry[] => {
  if (!(cursorCreatedAt && cursorId)) return items;
  const seen = new Set(previous.map((row) => row.id));
  const appended = items.filter((row) => !seen.has(row.id));
  return [...previous, ...appended];
};

/** Format the entity portion of the event subtitle (entity_type + optional id). */
const fmtEntityPart = (entity_type: string | null, entity_id: string | null): string | null => {
  if (!entity_type) return null;
  return entity_id ? `${entity_type}=${entity_id}` : entity_type;
};

/** Format the scope portion of the event subtitle (scope_type + optional id). */
const fmtScopePart = (scope_type: string | null, scope_id: string | null): string | null => {
  if (!scope_type) return null;
  return scope_id ? `${scope_type}:${scope_id}` : `scope=${scope_type}`;
};

/**
 * Build the subtitle line from the audit event's REAL non-null fields only - no
 * invented data. Renders the entity, scope, actor, and reason that are present.
 */
const buildEventSub = (event: AuditLogEntry): string => {
  const parts = [
    fmtEntityPart(event.entity_type, event.entity_id),
    fmtScopePart(event.scope_type, event.scope_id),
    event.user_id ? `actor=${event.user_id}` : null,
    event.reason ? `reason=${event.reason}` : null,
  ].filter(Boolean) as string[];
  // Honest fallback when the event carries no contextual fields at all.
  return parts.length > 0 ? parts.join(" · ") : "No additional context recorded";
};

/**
 * Render a safe summary of non-redacted event details - top-level primitive
 * values only (string, number, boolean). Nested objects are intentionally skipped
 * to avoid surfacing raw structured data in the UI without schema knowledge.
 */
const renderDetailsLine = (details: Record<string, unknown>): string | null => {
  const pairs = Object.entries(details)
    .filter(([, v]) => v !== null && v !== undefined && typeof v !== "object")
    .map(([k, v]) => `${k}=${String(v)}`);
  return pairs.length > 0 ? pairs.join(" · ") : null;
};

/**
 * Optional details note for a timeline entry. When details_redacted is true,
 * shows the server-driven redaction notice. When false and non-empty, renders
 * top-level primitive k=v pairs. Returns null when there is nothing to show.
 * Extracted from AuditTimelineItem to keep its cyclomatic complexity below the
 * analysis threshold.
 */
const DetailsNote = ({ event }: { event: AuditLogEntry }) => {
  if (event.details_redacted) {
    return (
      <span className="item-sub" role="note">
        Sensitive (redacted) - payload withheld by the audit service
      </span>
    );
  }
  const line = renderDetailsLine(event.details);
  if (!line) return null;
  return <span className="item-sub" role="note">{line}</span>;
};

/**
 * A single live audit timeline entry. The tone reflects the server-driven
 * `sensitive` flag; when `details_redacted` is true the row shows a clear
 * "Sensitive (redacted)" indicator and renders NO details payload and NO reveal
 * control - the redaction decision is the backend's, never undone client-side.
 * When `details_redacted` is false and the details object is non-empty, the
 * top-level primitive fields are rendered as a safe key=value summary.
 */
const AuditTimelineItem = ({ event }: { event: AuditLogEntry }) => {
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
};

/**
 * Error state for the live audit timeline. Keeps the audit-specific 403 copy
 * local so the shared finance helper remains unchanged.
 */
const AuditTimelineFeedErrorState = ({ error }: { error: ApiError | Error }) => {
  const described = describeError(error);
  const is403 = error instanceof ApiError && error.status === 403;
  const detail = is403 ? "Your role cannot view the audit log." : described.detail;
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
};

/** Loading state shown before the first audit page arrives. */
const AuditTimelineFeedLoadingState = () => {
  return (
    <div className="timeline" role="list" aria-busy="true">
      <div className="timeline-item" role="listitem">
        <TimelinePlaceholderRow
          tone="blue"
          title="Loading audit events..."
          sub="Reading the audit log (this read is itself audited)"
          badge="Loading"
        />
      </div>
    </div>
  );
};

/** Empty state shown when the current filter has no matching audit events. */
const AuditTimelineFeedEmptyState = () => {
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
};

/** Loaded timeline state with rows, pagination, and the append-more action. */
const AuditTimelineFeedList = ({
  events,
  hasMore,
  loading,
  loadMore,
}: {
  events: AuditLogEntry[];
  hasMore: boolean;
  loading: boolean;
  loadMore: () => void;
}) => {
  const loadMoreLabel = loading ? "Loading more..." : "Load More";
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
      {loading && (
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
};

/**
 * The loaded-or-placeholder audit timeline. This component keeps the state
 * branching small and delegates the row rendering to the specialized state
 * components above.
 */
const AuditTimelineFeedContent = ({
  error,
  events,
  loading,
  hasMore,
  loadMore,
}: {
  error: ApiError | Error | null;
  events: AuditLogEntry[];
  loading: boolean;
  hasMore: boolean;
  loadMore: () => void;
}) => {
  if (error) return <AuditTimelineFeedErrorState error={error} />;
  if (loading && events.length === 0) return <AuditTimelineFeedLoadingState />;
  if (events.length === 0) return <AuditTimelineFeedEmptyState />;
  return (
    <AuditTimelineFeedList
      events={events}
      hasMore={hasMore}
      loading={loading}
      loadMore={loadMore}
    />
  );
};

// ============================================================================
// Purpose: Own the audit-timeline cursor state, page stitching, and pagination
//   actions behind a small hook so the visual component stays simple. The hook
//   still calls useAuditEvents() unconditionally and preserves the existing
//   append-more behavior.
// Database/ORM: None.
// Standards: Keep fetch/state orchestration separate from rendering; preserve
//   the read-only audit flow and fail-closed behavior.
// Blast Radius: Audit read only.
// ============================================================================
type AuditTimelineFeedState = {
  error: ApiError | Error | null;
  events: AuditLogEntry[];
  hasMore: boolean;
  loading: boolean;
  loadMore: () => void;
};

type AuditTimelinePage = {
  items: AuditLogEntry[];
  pagination: AuditEventPagination;
};

/**
 * Merge a fetched audit page into the accumulated row set while removing
 * duplicate rows from overlapping cursor windows.
 */
const syncAuditTimelinePage = (
  page: AuditTimelinePage,
  cursorCreatedAt: string | undefined,
  cursorId: string | undefined,
  setRows: Dispatch<SetStateAction<AuditLogEntry[]>>,
  setPagination: Dispatch<SetStateAction<AuditEventPagination | null>>,
): void => {
  setRows((previous) => appendAuditRows(previous, page.items, cursorCreatedAt, cursorId));
  setPagination(page.pagination);
};

/**
 * Advance the timeline cursor when more data is available and no request is
 * already in flight.
 */
const advanceAuditTimelineCursor = (
  nextCursor: AuditEventPagination["next_cursor"] | undefined,
  loading: boolean,
  setCursorCreatedAt: Dispatch<SetStateAction<string | undefined>>,
  setCursorId: Dispatch<SetStateAction<string | undefined>>,
): void => {
  if (!nextCursor || loading) return;
  setCursorCreatedAt(nextCursor.created_at);
  setCursorId(nextCursor.id);
};

/**
 * Resolve the audit timeline into the current page, cursor, and load-more
 * action. The hook stays unconditional; only the consumer decides when to
 * mount it.
 */
export const useAuditTimelineFeedState = (eventType: string | undefined): AuditTimelineFeedState => {
  const [rows, setRows] = useState<AuditLogEntry[]>([]);
  const [pagination, setPagination] = useState<AuditEventPagination | null>(null);
  const [cursorCreatedAt, setCursorCreatedAt] = useState<string>();
  const [cursorId, setCursorId] = useState<string>();

  useEffect(() => {
    setRows([]);
    setPagination(null);
    setCursorCreatedAt(() => undefined);
    setCursorId(() => undefined);
  }, [eventType]);

  const { data, loading, error } = useAuditEvents({
    event_type: eventType,
    cursor_created_at: cursorCreatedAt,
    cursor_id: cursorId,
  });

  useEffect(() => {
    if (!data) return;
    syncAuditTimelinePage(data, cursorCreatedAt, cursorId, setRows, setPagination);
  }, [data, cursorCreatedAt, cursorId]);

  const hasMore = Boolean(pagination?.has_more && pagination.next_cursor);
  const nextCursor = pagination?.next_cursor;

  /**
   * Request the next cursor window when the feed is idle and a next page exists.
   */
  const loadMore = (): void => {
    advanceAuditTimelineCursor(nextCursor, loading, setCursorCreatedAt, setCursorId);
  };

  return {
    error,
    events: rows,
    hasMore,
    loading,
    loadMore,
  };
};

/**
 * The live audit-event feed. ALWAYS calls useAuditEvents() (it is only mounted
 * when the viewer may see the audit log), so the hook stays unconditional.
 * Maps loading / error (403 -> "No permission") / empty / loaded states, and
 * consumes backend `pagination.next_cursor` for a "Load More" append flow.
 */
const AuditTimelineFeed = ({ eventType }: { eventType: string | undefined }) => {
  const state = useAuditTimelineFeedState(eventType);
  return <AuditTimelineFeedContent {...state} />;
};

export default AuditTimelineFeed;
