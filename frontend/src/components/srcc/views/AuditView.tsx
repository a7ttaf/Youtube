import { useState } from "react";

import { useAuditSummary } from "@/lib/api/useAuditSummary";

import { Badge, ItemRow, SummaryTile, TimelinePlaceholderRow } from "../shared";

import AuditLogPanelHeader from "./AuditLogPanelHeader";
import AuditTimelineFeed from "./AuditTimelineFeed";

// Static finance audit-retention policy text. This is a frontend constant, NOT
// an aggregate from GET /audit/summary — the endpoint returns counts only.
const AUDIT_RETENTION_NOTE = {
  label: "Retention",
  value: "7 years",
  note: "Finance audit policy baseline",
};

// Placeholder shown in a numeric tile while its live count is loading, errored,
// or withheld. Keeps the tile honest instead of rendering a stale or invented
// figure.
const TILE_VALUE_PLACEHOLDER = "—";

// ============================================================================
// Purpose: Render the audit log screen shell, summary tiles, and the gated
//   timeline container. The live event feed and export controls are delegated
//   to dedicated components so the view stays shallow and the fail-closed audit
//   gate is easy to reason about.
// Database/ORM: None (frontend).
// Standards: Keep authorization gating at the component boundary and avoid
//   mounting the live audit feed when access is denied.
// Blast Radius: Audit read only.
// Connections:
//   - File: frontend/src/components/srcc/views/AuditLogPanelHeader.tsx -> filter
//     controls and CSV export.
//   - File: frontend/src/components/srcc/views/AuditTimelineFeed.tsx -> live
//     audit-event rendering and pagination.
// ============================================================================

/** Audit coverage panel header (title + subtitle). Extracted to keep nesting shallow. */
const AuditCoverageHeader = () => {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="auditCoverageTitle">Audit Coverage</strong>
        <span>Required to be present for every sensitive surface</span>
      </div>
    </div>
  );
};

/** Static audit coverage panel listing the always-audited sensitive surfaces. */
const AuditCoveragePanel = () => {
  return (
    <aside className="view-stack">
      <section className="panel" aria-labelledby="auditCoverageTitle">
        <AuditCoverageHeader />
        <div className="issue-list" role="list">
          <ItemRow
            tone="green"
            title="Revenue reads"
            sub="Every money cell view emits an audit row"
            trailing={<Badge tone="green">On</Badge>}
          />
          <ItemRow
            tone="green"
            title="Override before/after"
            sub="Both values stored with reason and approver"
            trailing={<Badge tone="green">On</Badge>}
          />
          <ItemRow
            tone="amber"
            title="Trace queries"
            sub="Filtered query is audited with allowed scope"
            trailing={<Badge tone="amber">Logged</Badge>}
          />
        </div>
      </section>
    </aside>
  );
};

/**
 * Audit event timeline gate. A non-audit viewer sees a single restricted
 * placeholder row and, critically, no hook is mounted here, so no /audit/events
 * fetch fires. Only when permitted is the live feed rendered.
 */
const AuditTimeline = ({
  canViewAudit,
  eventType,
}: {
  canViewAudit: boolean;
  eventType: string | undefined;
}) => {
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
};

/**
 * Render the four summary tiles from a resolved set of live count strings plus
 * the static retention policy tile. Pure presentation — the caller resolves the
 * count strings (live value, placeholder while loading/errored, or restricted).
 */
const AuditSummaryTilesRow = ({
  recentCount,
  sensitiveEvents,
  totalEvents,
  note,
}: {
  recentCount: string;
  sensitiveEvents: string;
  totalEvents: string;
  note: string;
}) => {
  return (
    <>
      <SummaryTile label="Events (24h)" value={recentCount} note="Audit events in the last 24 hours" />
      <SummaryTile label="High sensitivity" value={sensitiveEvents} note={note} />
      <SummaryTile label="Total events" value={totalEvents} note="Lifetime audited events for this tenant" />
      <SummaryTile {...AUDIT_RETENTION_NOTE} />
    </>
  );
};

/**
 * Live audit summary tiles. ALWAYS calls useAuditSummary() (it is only mounted
 * when the viewer may see the audit log, mirroring the events feed), so the hook
 * stays unconditional. Numeric tiles render a placeholder on loading/error and
 * the formatted live count on success; the count read exposes no per-row payload
 * and is gated by the same VIEW_AUDIT_LOG permission as the events list.
 */
const AuditSummaryTilesLive = () => {
  const { data, loading, error } = useAuditSummary();
  // Until a live count arrives (loading) or when the read failed (error/403),
  // show the placeholder rather than a stale or invented figure.
  const fmt = (value: number | undefined): string =>
    !data || loading || error || value == null
      ? TILE_VALUE_PLACEHOLDER
      : value.toLocaleString();
  const sensitiveNote = error
    ? "Audit summary unavailable"
    : "Revenue, payment, and override events flagged sensitive";
  return (
    <AuditSummaryTilesRow
      recentCount={fmt(data?.recent_count)}
      sensitiveEvents={fmt(data?.sensitive_events)}
      totalEvents={fmt(data?.total_events)}
      note={sensitiveNote}
    />
  );
};

/**
 * The audit summary tile band. A non-audit viewer fires NO summary fetch — the
 * live-tiles component (which owns the useAuditSummary hook) is simply not
 * mounted; restricted numeric tiles show a placeholder and the static retention
 * tile still renders. This mirrors the timeline's fail-closed gate exactly.
 */
const AuditSummaryTiles = ({ canViewAudit }: { canViewAudit: boolean }) => {
  if (!canViewAudit) {
    return (
      <AuditSummaryTilesRow
        recentCount={TILE_VALUE_PLACEHOLDER}
        sensitiveEvents={TILE_VALUE_PLACEHOLDER}
        totalEvents={TILE_VALUE_PLACEHOLDER}
        note="Audit summary restricted for this role"
      />
    );
  }
  return <AuditSummaryTilesLive />;
};

/**
 * The audit log screen: live summary tiles plus the gated live timeline.
 * `canViewAudit` gates both the summary fetch and whether the timeline mounts.
 * `canViewFinance` is accepted (optional) for parity with the shell prop
 * contract but the audit tiles are not finance-gated (they carry no money
 * values); AppShell may pass it for consistency with sibling views.
 */
const AuditView = ({
  canViewAudit,
}: {
  canViewAudit: boolean;
  canViewFinance?: boolean;
}) => {
  const [eventType, setEventType] = useState("");

  return (
    <section className="view-page" aria-labelledby="auditTitle">
      <div className="view-summary" aria-label="Audit summary">
        <AuditSummaryTiles canViewAudit={canViewAudit} />
      </div>

      <div className="view-grid">
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
        <AuditCoveragePanel />
      </div>
    </section>
  );
};

export default AuditView;
