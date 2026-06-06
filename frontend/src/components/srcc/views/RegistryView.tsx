import { ApiError } from "@/lib/api/client";
import type { ChannelRegistryEntry } from "@/lib/api/types";
import { useChannels } from "@/lib/api/useChannels";
import type { Severity } from "@/lib/mock/data";
import { REGISTRY_CONTROLS, REGISTRY_SUMMARY } from "@/lib/mock/data";
import {
  Badge,
  Dot,
  ItemRow,
  RESTRICTED_FINANCE_VALUE,
  SummaryTile,
} from "../shared";

// ============================================================================
// Purpose: The REAL-data Channel Registry screen, extracted from AppShell. The
//   table is wired to GET /channels via useChannels; client-side derivation maps
//   the API fields to the display model (avatar initials, CMS badge tone, source
//   label, state per Option A rules, trace key, action label). Company/sector
//   columns show primary_company_id and "—" respectively — a future GET /org-units
//   or enriched endpoint will add display names. Summary tiles derive active-channel
//   and outside-CMS counts from the response; finance-gated tiles stay "—"
//   until a finance-month endpoint exists. Mock values are never shown after a
//   live fetch succeeds; loading tiles show "…" and error tiles show "—".
// Database/ORM: None (frontend) — consumes GET /channels (VIEW_ANALYTICS gate).
// Standards: No client-side authorization is invented. canManageRegistry gates
//   trace-key visibility at the COMPONENT boundary (fail-closed: non-registry
//   viewers see Restricted in the trace-key cell). All write-path actions remain
//   disabled for Phase 1 — the PATCH /channels/{id}/mapping route, bulk-import
//   route, and account-assignment routes are not yet wired; buttons are always
//   disabled regardless of canManageRegistry until those routes exist.
//   A 403 from GET /channels surfaces as a no-permission error row; no
//   client-side auth guessing occurs (VIEW_ANALYTICS gate is the backend's call).
// Blast Radius: Registry read only. No finance number, no source-of-truth mutation.
// Connections:
//   - File: frontend/src/lib/api/useChannels.ts -> the channel-list fetch hook.
//   - File: frontend/src/lib/api/types.ts -> ChannelRegistryEntry contract.
//   - File: backend/ums_smart_revenue/api/channels.py:116 list_channels.
//   - File: Docs/superpowers/specs/2026-06-05-registry-view-design.md -> spec.
// ============================================================================

// ---- Client-side derivation helpers ----------------------------------------

/** Compute up to 2-char initials from a channel display name. */
const avatarFromName = (name: string): string => // skipcq: JS-0067
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("");

const CMS_BADGE: Record<string, { text: string; tone: Severity }> = {
  INSIDE_CMS: { text: "Inside CMS", tone: "green" },
  OUTSIDE_CMS: { text: "Outside CMS", tone: "amber" },
};

/** Returns the display badge for a channel CMS status; falls back to Unmapped for unknown values. */
const cmsBadge = (cms_status: string): { text: string; tone: Severity } =>
  CMS_BADGE[cms_status] ?? { text: "Unmapped", tone: "red" };

// DB-constrained revenue_source_status values (ck_youtube_channels_revenue_source_status):
// OFFICIAL_CMS_REVENUE | OFFICIAL_MANUAL_IMPORT | ALLOCATED_FROM_PAYMENT_POOL
// | PERFORMANCE_ONLY | MISSING_REVENUE_SOURCE
const SOURCE_LABELS: Record<string, string> = {
  OFFICIAL_CMS_REVENUE: "Official CMS revenue",
  OFFICIAL_MANUAL_IMPORT: "Uploaded owner statement",
  ALLOCATED_FROM_PAYMENT_POOL: "Allocated from payment pool",
  PERFORMANCE_ONLY: "Performance only (no revenue)",
  MISSING_REVENUE_SOURCE: "Not linked",
};

/** Returns a human-readable revenue source label, falling back to the raw enum value. */
const sourceLabel = (revenue_source_status: string): string =>
  SOURCE_LABELS[revenue_source_status] ?? revenue_source_status;

/**
 * Option A state derivation — purely from existing fields, no new DB column.
 * Export block: no revenue source + revenue required (held from export).
 * Evidence due: outside CMS without a verified content-owner link.
 * Approved: everything else (source resolved + CMS status resolved).
 */
const deriveState = (ch: ChannelRegistryEntry): { text: string; tone: Severity } => {
  if (ch.revenue_required && ch.revenue_source_status === "MISSING_REVENUE_SOURCE") {
    return { text: "Export block", tone: "red" };
  }
  if (ch.cms_status === "OUTSIDE_CMS" && !ch.content_owner_id) {
    return { text: "Evidence due", tone: "amber" };
  }
  return { text: "Approved", tone: "green" };
};

/**
 * Action label derived from state. All write-path actions (Map, Assign) stay
 * disabled until the backend routes are wired. "Review" has no route yet either
 * — the button remains visible but disabled for all Phase 1 users.
 */
const deriveAction = (state: { text: string }): string => {
  if (state.text === "Export block") return "Map";
  if (state.text === "Evidence due") return "Assign";
  return "Review";
};

/**
 * Trace key: "channel:{youtube_channel_id}" when the channel has an org mapping,
 * "pending" when unmapped (no primary_company_id). The Neo4j node doesn't exist
 * for unmapped channels, so "pending" is the honest label.
 */
const traceKey = (ch: ChannelRegistryEntry): string =>
  ch.primary_company_id ? `channel:${ch.youtube_channel_id}` : "pending";

// ---- Summary tile counts (derived from fetched channels) -------------------

// ============================================================================
// Purpose: Derive display values for all four summary tiles from the live
//   channel response. Returns neutral values (loading: "…", error: "—") when
//   channels is null so mock numbers are never shown during load or on error.
//   Finance tiles (Unmapped revenue, Scoped changes) have no live source in
//   Phase 1 — they show "—" after a successful fetch rather than static mock
//   numbers, because this page is presented as off-mock and numbers-first.
//   Outside-CMS count includes ONLY channels with cms_status === "OUTSIDE_CMS",
//   matching the backend /channels/outside-cms endpoint semantics; UNKNOWN
//   channels are not counted as outside-CMS.
//   Note field: always cleared ("") — the mock subtitle strings ("300+ target
//   registry") are stale copy that contradicts live-derived values. SummaryTile
//   renders <small>{note}</small> unconditionally, so a non-empty note would
//   appear next to the live count and mislead the viewer.
// ============================================================================
const buildSummaryTiles = (
  channels: ChannelRegistryEntry[] | null,
  loading: boolean,
  baseStatic: typeof REGISTRY_SUMMARY,
): typeof REGISTRY_SUMMARY => {
  const neutral = loading ? "…" : "—";
  if (!channels) {
    return baseStatic.map((tile) => ({ ...tile, value: neutral, note: "" }));
  }
  const activeCount = channels.length;
  const outsideCmsCount = channels.filter(
    (ch) => ch.cms_status === "OUTSIDE_CMS",
  ).length;
  return baseStatic.map((tile) => {
    // FIX: always clear note — mock subtitle copy contradicts live-derived values.
    if (tile.label === "Active channels") return { ...tile, value: String(activeCount), note: "" };
    if (tile.label === "Outside CMS") return { ...tile, value: String(outsideCmsCount), note: "" };
    // Finance tiles (Unmapped revenue, Scoped changes): no live source yet.
    return { ...tile, value: "—", note: "" };
  });
};

// ---- Sub-components --------------------------------------------------------

/**
 * The real-data channel registry view: live summary tiles + the registry main
 * panel + the static side panels. useChannels() is called ONCE here and the
 * state is threaded down to both the summary tiles and the table — a second
 * hook call in a child would cause a duplicate GET /channels fetch.
 */
export default function RegistryView({ // skipcq: JS-0067
  canManageRegistry,
  canViewFinance,
}: {
  canManageRegistry: boolean;
  canViewFinance: boolean;
}) {
  const channelState = useChannels();
  const summaryTiles = buildSummaryTiles(
    channelState.data,
    channelState.loading,
    REGISTRY_SUMMARY,
  );

  return (
    <section className="view-page" aria-labelledby="registryTitle">
      <div className="view-summary" aria-label="Registry summary">
        {summaryTiles.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>

      <div className="view-grid wide-side">
        <RegistryMainPanel canManageRegistry={canManageRegistry} channelState={channelState} />
        <RegistrySidePanels canManageRegistry={canManageRegistry} />
      </div>
    </section>
  );
}

type ChannelAsyncState = ReturnType<typeof useChannels>;

/** The registry main panel: header + mapping band + registry table. */
function RegistryMainPanel({ // skipcq: JS-0067
  canManageRegistry,
  channelState,
}: {
  canManageRegistry: boolean;
  channelState: ChannelAsyncState;
}) {
  return (
    <section className="panel">
      <RegistryPanelHeader />
      <RegistryMappingBand canManageRegistry={canManageRegistry} />
      <RegistryTable canManageRegistry={canManageRegistry} channelState={channelState} />
    </section>
  );
}

/** Registry panel header: title/subtitle and the bulk-import / mapping-change actions. */
function RegistryPanelHeader() { // skipcq: JS-0067
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="registryTitle">Channel Registry</strong>
        <span>Ownership, CMS status, revenue scope, and SQL lineage identity</span>
      </div>
      <div className="view-actions">
        {/* Phase 1: no bulk-import route exists yet — disabled for all users. */}
        <button className="ghost-button" type="button" disabled>
          Bulk Import
        </button>
        {/* Phase 1: no mapping-change submission route yet — disabled for all users. */}
        <button className="primary-button" type="button" disabled>
          Request Mapping Change
        </button>
      </div>
    </div>
  );
}

/** Finance-visible mapping band; the scope badge reflects registry-edit access. */
function RegistryMappingBand({ canManageRegistry }: { canManageRegistry: boolean }) { // skipcq: JS-0067
  return (
    <div className="permission-band">
      <Dot tone="green" />
      <span>
        <strong>Finance-visible mapping layer</strong>
        <span>Company managers see only assigned companies; sector managers see assigned sectors; every mapping change writes an audit event.</span>
      </span>
      <Badge tone={canManageRegistry ? "blue" : "red"}>
        {canManageRegistry ? "Scoped" : "Read only"}
      </Badge>
    </div>
  );
}

/** Column header row. Extracted to keep nesting shallow. */
function RegistryTableHead() { // skipcq: JS-0067
  return (
    <thead>
      <tr>
        <th>Channel</th><th>Company</th><th>Sector</th><th>CMS</th>
        <th>Revenue Source</th><th>Trace Key</th><th>State</th><th>Action</th>
      </tr>
    </thead>
  );
}

/** Single-row message cell inside the registry table shell (loading, error, empty). */
function RegistryTableMessageRow({ // skipcq: JS-0067
  title,
  sub,
}: {
  title: string;
  sub: string;
}) {
  return (
    <tr>
      <td colSpan={8}>
        <span className="item-title">{title}</span>
        <span className="item-sub">{sub}</span>
      </td>
    </tr>
  );
}

/**
 * Channel registry data table. Receives the shared channelState (hoisted from
 * RegistryView) rather than calling useChannels() itself — a second hook call
 * would cause a duplicate GET /channels request. Handles loading, error (403 →
 * no-permission row), empty, and loaded states. Trace keys are withheld from
 * non-registry viewers (fail-closed: shows RESTRICTED_FINANCE_VALUE). All
 * action buttons disabled in Phase 1 — write routes not yet wired.
 */
function RegistryTable({ // skipcq: JS-R1005, JS-0067
  canManageRegistry,
  channelState,
}: {
  canManageRegistry: boolean;
  channelState: ChannelAsyncState;
}) {
  const { data: channels, loading, error } = channelState;

  if (error) {
    const is403 = error instanceof ApiError && error.status === 403;
    return (
      <div className="table-wrap" role="alert">
        <table aria-label="Channel registry">
          <RegistryTableHead />
          <tbody>
            <RegistryTableMessageRow
              title={is403 ? "No permission" : "Failed to load channels"}
              sub={
                is403
                  ? "Your role cannot view the channel registry."
                  : "An error occurred loading the channel registry. Please try again."
              }
            />
          </tbody>
        </table>
      </div>
    );
  }

  if (loading && !channels) {
    return (
      <div className="table-wrap" aria-busy="true">
        <table aria-label="Channel registry">
          <RegistryTableHead />
          <tbody>
            <RegistryTableMessageRow
              title="Loading channels…"
              sub="Reading the channel registry"
            />
          </tbody>
        </table>
      </div>
    );
  }

  const rows = channels ?? [];

  if (rows.length === 0) {
    return (
      <div className="table-wrap">
        <table aria-label="Channel registry">
          <RegistryTableHead />
          <tbody>
            <RegistryTableMessageRow
              title="No channels in registry"
              sub="No channels are registered for this tenant"
            />
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table aria-label="Channel registry">
        <RegistryTableHead />
        <tbody>
          {rows.map((ch) => (
            <RegistryRow key={ch.youtube_channel_id} channel={ch} canManageRegistry={canManageRegistry} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A single channel registry row; derives all display fields from the API shape. */
function RegistryRow({ // skipcq: JS-0067
  channel: ch,
  canManageRegistry,
}: {
  channel: ChannelRegistryEntry;
  canManageRegistry: boolean;
}) {
  const avatar = avatarFromName(ch.channel_name);
  const cms = cmsBadge(ch.cms_status);
  const state = deriveState(ch);
  const action = deriveAction(state);
  const node = traceKey(ch);

  return (
    <tr>
      <RegistryChannelCell
        name={ch.channel_name}
        code={ch.youtube_channel_id}
        avatar={avatar}
      />
      {/* Company column: show primary_company_id slug (display names need GET /org-units). */}
      <td>{ch.primary_company_id ?? "—"}</td>
      {/* Sector column: not returned by GET /channels — requires org-unit name lookup. */}
      <td>—</td>
      <td><Badge tone={cms.tone}>{cms.text}</Badge></td>
      <td>{sourceLabel(ch.revenue_source_status)}</td>
      <td>
        <span className="code-chip">
          {canManageRegistry ? node : RESTRICTED_FINANCE_VALUE}
        </span>
      </td>
      <td><Badge tone={state.tone}>{state.text}</Badge></td>
      <td>
        {/* All write-path actions (Map, Assign, Review) are disabled in Phase 1.
            Map → PATCH /channels/{id}/mapping not wired; Assign → account-assignment
            route undefined; Review → channel detail/trace view not built. */}
        <button className="mini-button" type="button" disabled>
          {action}
        </button>
      </td>
    </tr>
  );
}

/** Channel cell: avatar + name + channel ID stacked. */
function RegistryChannelCell({ // skipcq: JS-0067
  name,
  code,
  avatar,
}: {
  name: string;
  code: string;
  avatar: string;
}) {
  return (
    <td>
      <span className="channel-cell">
        <span className="avatar">{avatar}</span>
        <span>
          <span className="channel-name">{name}</span>
          <span className="channel-id">{code}</span>
        </span>
      </span>
    </td>
  );
}

/** Registry side panels: the mapping-change request form and registry controls. */
function RegistrySidePanels({ canManageRegistry }: { canManageRegistry: boolean }) { // skipcq: JS-0067
  return (
    <aside className="view-stack" aria-label="Registry side panels">
      <MappingChangeRequestPanel canManageRegistry={canManageRegistry} />
      <RegistryControlsPanel />
    </aside>
  );
}

/**
 * The mapping-change request panel. Inputs are disabled for non-registry roles.
 * Submit and Save Draft are disabled in Phase 1 for ALL users — PATCH
 * /channels/{id}/mapping is not wired yet; enabling them would silently no-op
 * for registry managers and create a misleading write-affordance.
 */
function MappingChangeRequestPanel({ canManageRegistry }: { canManageRegistry: boolean }) { // skipcq: JS-0067
  return (
    <section className="panel">
      <div className="panel-header">
        <div className="panel-title">
          <strong>Mapping Change Request</strong>
          <span>Restricted to registry admins and corporate finance approvers</span>
        </div>
        <Badge tone="amber">Audit required</Badge>
      </div>
      <div className="form-grid">
        <MappingSelectRow
          htmlFor="registryChannel"
          label="Channel"
          options={["Sports Extra", "Music Stage"]}
          disabled={!canManageRegistry}
        />
        <MappingSelectRow
          htmlFor="registryCompany"
          label="Company"
          options={["TV Sector", "Catalog Media"]}
          disabled={!canManageRegistry}
        />
        <MappingInputRow
          htmlFor="registryReason"
          label="Reason"
          defaultValue="March source evidence received"
          disabled={!canManageRegistry}
        />
        <MappingSelectRow
          htmlFor="registryEffective"
          label="Effective month"
          options={["Mar 2026", "Apr 2026"]}
          disabled={!canManageRegistry}
        />
      </div>
      <div className="action-row">
        {/* Phase 1: submit route not wired — disabled for all users. */}
        <button className="ghost-button" type="button" disabled>Save Draft</button>
        <button className="primary-button" type="button" disabled>Submit Approval</button>
      </div>
    </section>
  );
}

/** A labelled select inside the mapping-change form. */
function MappingSelectRow({ // skipcq: JS-0067
  htmlFor,
  label,
  options,
  disabled,
}: {
  htmlFor: string;
  label: string;
  options: string[];
  disabled: boolean;
}) {
  return (
    <div className="field-row">
      <label htmlFor={htmlFor}>{label}</label>
      <select id={htmlFor} disabled={disabled}>
        {options.map((option) => (
          <option key={option}>{option}</option>
        ))}
      </select>
    </div>
  );
}

/** A labelled text input inside the mapping-change form. */
function MappingInputRow({ // skipcq: JS-0067
  htmlFor,
  label,
  defaultValue,
  disabled,
}: {
  htmlFor: string;
  label: string;
  defaultValue: string;
  disabled: boolean;
}) {
  return (
    <div className="field-row">
      <label htmlFor={htmlFor}>{label}</label>
      <input id={htmlFor} defaultValue={defaultValue} disabled={disabled} />
    </div>
  );
}

/** The registry-controls panel listing the expected production behaviors. */
function RegistryControlsPanel() { // skipcq: JS-0067
  return (
    <section className="panel">
      <div className="panel-header">
        <div className="panel-title">
          <strong>Registry Controls</strong>
          <span>Active safeguards for registry operations</span>
        </div>
      </div>
      <div className="issue-list" role="list">
        {REGISTRY_CONTROLS.map((c) => (
          <ItemRow
            key={c.title}
            tone={c.tone}
            title={c.title}
            sub={c.sub}
            trailing={<Badge tone={c.badge.tone}>{c.badge.text}</Badge>}
          />
        ))}
      </div>
    </section>
  );
}
