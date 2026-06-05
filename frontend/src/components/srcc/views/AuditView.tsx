import { ApiError } from "@/lib/api/client";
import type { AuditLogEntry } from "@/lib/api/types";
import { useAuditEvents } from "@/lib/api/useAudit";
import type { Severity } from "@/lib/mock/data";
import { AUDIT_SUMMARY } from "@/lib/mock/data";
import { Badge, Dot, ItemRow, SummaryTile, formatTimestamp } from "../shared";
import { describeError } from "./CommandView";

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
 * The audit-log main panel: header (title + severity filter / download actions)
 * and the live audit timeline. Extracted so the AuditView JSX tree stays shallow.
 */
function AuditLogPanel({ canViewAudit }: { canViewAudit: boolean }) { // skipcq: JS-0067
  return (
    <section className="panel">
      <AuditLogPanelHeader />
      <AuditTimeline canViewAudit={canViewAudit} />
    </section>
  );
}

/**
 * Audit-log panel header: title/subtitle and the severity filter + download
 * actions. Neither control is wired yet — there is no server-side severity facet
 * and no audit-export route/handler — so BOTH stay UNCONDITIONALLY disabled
 * placeholders rather than presenting a control that does nothing. (`canViewAudit`
 * is no longer needed here: a non-audit viewer never reaches the live panel.)
 */
function AuditLogPanelHeader() { // skipcq: JS-0067
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="auditTitle">Audit Log</strong>
        <span>Every sensitive action records actor, permission, scope, target, and result</span>
      </div>
      <div className="view-actions">
        <select className="control" aria-label="Audit severity" disabled>
          <option>All sensitive</option><option>Denied</option><option>Exports</option>
        </select>
        {/* No audit-export route exists yet — disabled for ALL users (a button
            that did nothing was misleading) until a real handler is built. */}
        <button className="ghost-button" type="button" disabled>Download Audit View</button>
      </div>
    </div>
  );
}

/**
 * Reusable inner content for timeline placeholder rows (restricted, error, loading,
 * empty). Rendered as a fragment inside the caller's `timeline-item` div so each
 * state can control its own wrapper attributes (role, aria-busy, etc.) independently.
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
 * placeholder row and — critically — NO hook is mounted here, so no /audit/events
 * fetch fires (fail-closed). Only when permitted is the always-fetching feed
 * mounted, keeping the useAuditEvents call unconditional (rules-of-hooks safe).
 */
function AuditTimeline({ canViewAudit }: { canViewAudit: boolean }) { // skipcq: JS-0067
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
  return <AuditTimelineFeed />;
}

/**
 * The live audit-event feed. ALWAYS calls useAuditEvents() (it is only mounted
 * when the viewer may see the audit log), so the hook stays unconditional. Maps
 * loading / error (403 -> "No permission") / empty / loaded states.
 *
 * FIRST PAGE ONLY: this renders the default page (newest first, backend default
 * limit) and does NOT yet consume `pagination.next_cursor`. A future "Load More"
 * would pass `next_cursor.{created_at, id}` back as `cursor_created_at`/`cursor_id`
 * to `useAuditEvents` to fetch the next page.
 */
function AuditTimelineFeed() { // skipcq: JS-0067, JS-R1005
  const { data, loading, error } = useAuditEvents();

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

  if (loading && !data) {
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

  const events = data?.items ?? [];
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
 * A single live audit timeline entry. The tone reflects the server-driven
 * `sensitive` flag; when `details_redacted` is true the row shows a clear
 * "Sensitive (redacted)" indicator and renders NO details payload and NO reveal
 * control — the redaction decision is the backend's, never undone client-side.
 * When `details_redacted` is false and the details object is non-empty, the
 * top-level primitive fields are rendered as a safe key=value summary.
 */
function AuditTimelineItem({ event }: { event: AuditLogEntry }) { // skipcq: JS-0067
  const tone: Severity = event.sensitive ? "red" : "green";
  const detailsLine = event.details_redacted ? null : renderDetailsLine(event.details);
  return (
    <div className="timeline-item" role="listitem">
      <span className="timeline-time">{formatTimestamp(event.created_at)}</span>
      <Dot tone={tone} />
      <span>
        <span className="item-title">{event.event_type}</span>
        <span className="item-sub">{buildEventSub(event)}</span>
        {event.details_redacted ? (
          <span className="item-sub" role="note">
            Sensitive (redacted) — payload withheld by the audit service
          </span>
        ) : detailsLine ? (
          <span className="item-sub" role="note">{detailsLine}</span>
        ) : null}
      </span>
      <Badge tone={tone}>{event.sensitive ? "Sensitive" : "Logged"}</Badge>
    </div>
  );
}

/** Static audit coverage panel listing the always-audited sensitive surfaces. */
function AuditCoveragePanel() { // skipcq: JS-0067
  return (
    <aside className="view-stack">
      <section className="panel">
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
        <strong>Audit Coverage</strong>
        <span>Required to be present for every sensitive surface</span>
      </div>
    </div>
  );
}
