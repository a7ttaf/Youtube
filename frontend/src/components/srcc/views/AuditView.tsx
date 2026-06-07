import { useState } from "react";

import type { Severity } from "@/lib/mock/data";
import { AUDIT_SUMMARY } from "@/lib/mock/data";

import { Badge, Dot, ItemRow, SummaryTile } from "../shared";

import AuditLogPanelHeader from "./AuditLogPanelHeader";
import AuditTimelineFeed from "./AuditTimelineFeed";

/**
 * Compact placeholder row used by the restricted timeline branch.
 * Kept local so the gated view can render without mounting the live feed hook.
 */
const TimelinePlaceholderRow = ({
  tone,
  title,
  sub,
  badge,
}: {
  tone: Severity;
  title: string;
  sub: string;
  badge: string;
}) => {
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
};

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
 * The audit log screen: static summary tiles plus the gated live timeline.
 * `canViewAudit` controls only whether the timeline mounts; `canViewFinance`
 * only affects the summary tiles.
 */
const AuditView = ({
  canViewAudit,
  canViewFinance,
}: {
  canViewAudit: boolean;
  canViewFinance: boolean;
}) => {
  const [eventType, setEventType] = useState("");

  return (
    <section className="view-page" aria-labelledby="auditTitle">
      <div className="view-summary" aria-label="Audit summary (static context)">
        {AUDIT_SUMMARY.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>
      {/* Summary counts above are static context - no live aggregate endpoint yet. */}
      <span className="item-sub" role="note" style={{ marginBottom: "0.5rem", display: "block" }}>
        Summary counts above are reference figures - live aggregate endpoint coming.
      </span>

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
