import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "@/lib/api/client";
import type { ChannelRegistryEntry, OrgUnit } from "@/lib/api/types";
import { useChannelMappingAction } from "@/lib/api/useChannelMapping";
import { useChannels } from "@/lib/api/useChannels";
import { useProposeAccountLinkAction } from "@/lib/api/useChannelAccountLinks";
import { useOrgUnits } from "@/lib/api/useOrgUnits";
import type { Severity } from "@/lib/mock/data";
import { REGISTRY_CONTROLS, REGISTRY_SUMMARY } from "@/lib/mock/data";
import {
  Badge,
  Dot,
  ItemRow,
  RESTRICTED_FINANCE_VALUE,
  SummaryTile,
} from "../shared";
import { RegistryImportFlow } from "./RegistryImportFlow";
import { useUnsettledImport } from "@/contexts/UnsettledImportContext";

// ============================================================================
// Purpose: The REAL-data Channel Registry screen (Phase 2). The table is wired
//   to GET /channels; company/sector columns resolve display names from the new
//   GET /org-units endpoint with an honest raw-id fallback (a missing or
//   deactivated unit shows its raw id, never an invented name). Row actions are
//   live: "Map" opens the mapping-change panel (PATCH /channels/{id}/mapping),
//   "Assign" opens the account-link proposal panel (POST
//   /revenue/channel-account-links, proposes an UNVERIFIED link), "Review"
//   navigates to the Trace view preselected on the channel. The header's
//   Import CSV action (PR-B) opens RegistryImportFlow, which swaps the main
//   panel's content (band + table) for the three-step import stepper; cancel
//   restores the table untouched, done restores it and reloads the channels.
// Database/ORM: None (frontend) — consumes GET /channels + GET /org-units
//   (VIEW_ANALYTICS gates), PATCH /channels/{id}/mapping (dual
//   MANAGE_ORG_MAPPING), POST /revenue/channel-account-links
//   (MANAGE_ORG_MAPPING@global); POST /channels/import stays inside
//   RegistryImportFlow.
// Standards: No client-side authorization is invented. canManageRegistry gates
//   the write affordances and trace-key visibility (fail-closed); the backend
//   permission checks remain the authority and scoped 403s surface inline.
//   canImportChannels (a backend-derived both-permission render hint) gates
//   the Import CSV control — hidden, not disabled, per the fail-closed house
//   rule. Mutations use the synchronous in-flight ref latch (one request per
//   click burst -> one audit event) and reload the channel list on success.
// Blast Radius: Registry reads + two audited registry write paths, plus the
//   audited bulk-import write path behind RegistryImportFlow. No finance
//   number computation; no source-of-truth mutation outside the wired routes.
// Connections:
//   - File: frontend/src/lib/api/useOrgUnits.ts -> org-unit name directory.
//   - File: frontend/src/lib/api/useChannelMapping.ts -> PATCH mapping action.
//   - File: frontend/src/lib/api/useChannelAccountLinks.ts -> propose action.
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx -> the
//     import stepper the main panel swaps in behind canImportChannels.
//   - File: backend/ums_smart_revenue/api/org_units.py -> GET /org-units.
//   - File: Docs/superpowers/specs/2026-06-07-registry-phase2-design.md -> spec.
// ============================================================================

// ---- Client-side derivation helpers ----------------------------------------

/** Compute up to 2-char initials from a channel display name. */
const avatarFromName = (name: string): string =>  name
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
 * Action label derived from state. Phase 2: Map and Assign are LIVE write
 * affordances (gated by canManageRegistry; the backend stays the authority);
 * Review is read-only navigation to the Trace view.
 *
 * Any channel without a primary_company_id (including PERFORMANCE_ONLY and
 * other non-Export-block shapes) needs Map first; a company assignment is
 * the prerequisite for all downstream actions. Mapped channels with a missing
 * revenue source need Review, not a remap that would audit an unrelated change.
 */
const deriveAction = (
  state: { text: string },
  ch: ChannelRegistryEntry,
): string => {
  if (!ch.primary_company_id) return "Map";
  if (state.text === "Evidence due") return "Assign";
  return "Review";
};

/**
 * Trace key: "channel:{youtube_channel_id}" when the channel has an org mapping,
 * "pending" when unmapped (no primary_company_id).
 */
const traceKey = (ch: ChannelRegistryEntry): string =>
  ch.primary_company_id ? `channel:${ch.youtube_channel_id}` : "pending";

/**
 * Company display name with honest fallback: org-unit name when the unit is in
 * the active directory, otherwise the raw id (never an invented name), "—" when
 * the channel is unmapped.
 */
const companyLabel = (
  ch: ChannelRegistryEntry,
  unitsById: Map<string, OrgUnit>,
): string => {
  if (!ch.primary_company_id) return "—";
  return unitsById.get(ch.primary_company_id)?.name ?? ch.primary_company_id;
};

/** Sector display name via the company unit's parent; "—" when unresolvable. */
const sectorLabel = (
  ch: ChannelRegistryEntry,
  unitsById: Map<string, OrgUnit>,
): string => {
  const company = ch.primary_company_id
    ? unitsById.get(ch.primary_company_id)
    : undefined;
  const parentId = company?.parent_id;
  return (parentId ? unitsById.get(parentId)?.name : undefined) ?? "—";
};

/** Map a mutation failure to operator-facing copy (typed ApiError aware). */
const describeMutationError = (err: unknown): string => {
  if (err instanceof ApiError) {
    const body = err.body as { detail?: unknown } | null;
    const detail = typeof body?.detail === "string" ? body.detail : null;
    return detail ?? (err.status === 403
      ? "No permission for this action."
      : `Request failed (${err.status}).`);
  }
  return "Request failed. Please retry.";
};

// ---- Summary tile counts (derived from fetched channels) -------------------

// ============================================================================
// Purpose: Derive display values for all four summary tiles from the live
//   channel response. Returns neutral values (loading: "…", error: "—") when
//   channels is null so mock numbers are never shown during load or on error.
//   Finance tiles (Unmapped revenue, Scoped changes) have no live source —
//   they show "—" after a successful fetch rather than static mock numbers.
//   Outside-CMS count includes ONLY channels with cms_status === "OUTSIDE_CMS".
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

type ChannelAsyncState = ReturnType<typeof useChannels>;

type RowActions = {
  hasTraceNav: boolean;
  onMap: (ch: ChannelRegistryEntry) => void;
  onAssign: (ch: ChannelRegistryEntry) => void;
  onReview: (ch: ChannelRegistryEntry) => void;
};

/**
 * Registry panel header: title/subtitle and, for import-capable operators, the
 * live Import CSV action (PR-B — POST /channels/import behind the flow). Per
 * the fail-closed house rule the button renders only when canImportChannels is
 * held (hidden, not disabled); it disables only while the stepper is already
 * open, mirroring the Groups header's sync control.
 */
const RegistryPanelHeader = ({
  canImportChannels,
  importOpen,
  onStartImport,
}: {
  canImportChannels: boolean;
  importOpen: boolean;
  onStartImport: () => void;
}) => {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="registryTitle">Channel Registry</strong>
        <span>Ownership, CMS status, revenue scope, and SQL lineage identity</span>
      </div>
      {canImportChannels ? (
        <div className="view-actions">
          <button
            className="primary-button"
            type="button"
            disabled={importOpen}
            onClick={onStartImport}
          >
            Import CSV
          </button>
        </div>
      ) : null}
    </div>
  );
};

/** Finance-visible mapping band; the scope badge reflects registry-edit access. */
const RegistryMappingBand = ({ canManageRegistry }: { canManageRegistry: boolean }) => {
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
};

/** Column header row. Extracted to keep nesting shallow. */
const RegistryTableHead = () => {
  return (
    <thead>
      <tr>
        <th>Channel</th><th>Company</th><th>Sector</th><th>CMS</th>
        <th>Revenue Source</th><th>Trace Key</th><th>State</th><th>Action</th>
      </tr>
    </thead>
  );
};

/** Single-row message cell inside the registry table shell (loading, error, empty). */
const RegistryTableMessageRow = ({
  title,
  sub,
}: {
  title: string;
  sub: string;
}) => {
  return (
    <tr>
      <td colSpan={8}>
        <span className="item-title">{title}</span>
        <span className="item-sub">{sub}</span>
      </td>
    </tr>
  );
};

/** Error message row for the registry table; extracted to keep RegistryTable CC low. */
const registryErrorRow = (error: unknown) => {
  const is403 = error instanceof ApiError && error.status === 403;
  return (
    <RegistryTableMessageRow
      title={is403 ? "No permission" : "Failed to load channels"}
      sub={
        is403
          ? "Your role cannot view the channel registry."
          : "An error occurred loading the channel registry. Please try again."
      }
    />
  );
};

/** Channel cell: avatar + name + channel ID stacked. */
const RegistryChannelCell = ({
  name,
  code,
  avatar,
}: {
  name: string;
  code: string;
  avatar: string;
}) => {
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
};

/** A single channel registry row; derives all display fields from the API shape. */
const RegistryRow = ({
  channel: ch,
  canManageRegistry,
  unitsById,
  hasTraceNav,
  onMap,
  onAssign,
  onReview,
}: {
  channel: ChannelRegistryEntry;
  canManageRegistry: boolean;
  unitsById: Map<string, OrgUnit>;
} & RowActions) => {
  const avatar = avatarFromName(ch.channel_name);
  const cms = cmsBadge(ch.cms_status);
  const state = deriveState(ch);
  const action = deriveAction(state, ch);
  const node = traceKey(ch);

  // Map/Assign are write affordances (capability-gated; backend authorizes);
  // Review is read-only navigation and stays enabled whenever nav is wired.
  const isWriteAction = action !== "Review";
  const disabled = isWriteAction ? !canManageRegistry : !hasTraceNav;
  /** Route the row click to the Map, Assign, or Review handler for this channel. */
  const onClick = () => {
    if (action === "Map") {
      onMap(ch);
    } else if (action === "Assign") {
      onAssign(ch);
    } else {
      onReview(ch);
    }
  };

  return (
    <tr>
      <RegistryChannelCell
        name={ch.channel_name}
        code={ch.youtube_channel_id}
        avatar={avatar}
      />
      {/* Company/Sector: org-unit display names with honest raw-id fallback. */}
      <td>{companyLabel(ch, unitsById)}</td>
      <td>{sectorLabel(ch, unitsById)}</td>
      <td><Badge tone={cms.tone}>{cms.text}</Badge></td>
      <td>{sourceLabel(ch.revenue_source_status)}</td>
      <td>
        <span className="code-chip">
          {canManageRegistry ? node : RESTRICTED_FINANCE_VALUE}
        </span>
      </td>
      <td><Badge tone={state.tone}>{state.text}</Badge></td>
      <td>
        <button
          className="mini-button"
          type="button"
          disabled={disabled}
          onClick={onClick}
        >
          {action}
        </button>
      </td>
    </tr>
  );
};

/**
 * Channel registry data table. Receives the shared channelState and org-unit
 * directory (both hoisted from RegistryView). Handles loading, error (403 →
 * no-permission row), empty, and loaded states. Trace keys stay withheld from
 * non-registry viewers (fail-closed: RESTRICTED_FINANCE_VALUE).
 */
const RegistryTable = ({
  canManageRegistry,
  channelState,
  unitsById,
  ...rowActions
}: {
  canManageRegistry: boolean;
  channelState: ChannelAsyncState;
  unitsById: Map<string, OrgUnit>;
} & RowActions) => {
  const { data: channels, error } = channelState;

  if (error) {
    return (
      <div className="table-wrap" role="alert">
        <table aria-label="Channel registry">
          <RegistryTableHead />
          <tbody>{registryErrorRow(error)}</tbody>
        </table>
      </div>
    );
  }

  if (!channels) {
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

  if (channels.length === 0) {
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
          {channels.map((ch) => (
            <RegistryRow
              key={ch.youtube_channel_id}
              channel={ch}
              canManageRegistry={canManageRegistry}
              unitsById={unitsById}
              {...rowActions}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
};

// ============================================================================
// Purpose: Carry an UNSETTLED import out of the stepper and keep saying so.
//   When an apply's response never arrives, the flow's exit still reloads the
//   registry — but that GET races the original POST and can return PRE-write
//   rows, so the table below it may be stale while looking authoritative.
// Database/ORM: None (frontend) — the reload button re-issues the existing
//   GET /channels via the shared channel state; nothing here writes.
// Standards: The uncertainty leaves WITH the operator instead of trapping them
//   in the stepper. Blocking both of the flow's exits until reconciliation
//   settles is not an option: a roster whose rows carry group keys can never
//   auto-settle, because `outcome` is computed from channel inventory and the
//   planner never loads memberships — that operator would have no way out at
//   all. So the exit is left open and the hazard is made loud and PERSISTENT:
//   it survives reloads, other views, and the document, and only an explicit
//   acknowledgement clears it.
//   "Import CSV" deliberately stays LIVE. The duplicate it once guarded is
//   already unreachable a layer down — Apply is disabled and the dispatch is
//   refused while any apply is unaccounted for — so disabling the opener
//   bought no safety and cost the operator the only reconciliation surface
//   they have: re-opening the importer runs the READ-ONLY dry run against the
//   same roster, which is what tells them whether the registry matches it.
//   That matters most for the operators who cannot read the audit trail at
//   all: MANAGE_CHANNELS + MANAGE_GROUPS does not imply VIEW_AUDIT_LOG, so
//   the seeded revenue_operations_admin and data_steward roles can import but
//   cannot open AuditView. Telling THEM to "check the audit trail" is advice
//   they cannot follow, so the notice reads their capability and says
//   something they can act on (review #184, codex P2).
// Blast Radius: Whether an operator can start a duplicate audited bulk import
//   after an import of unknown outcome. No requests beyond the registry GET,
//   no authorization meaning.
// Connections:
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       exits with ImportExitOutcome "unknown", which raises this notice.
//   - File: Docs/12_BACKEND_API_SPEC.md -> the dry run is the reconciliation
//       tool for an apply whose response was lost.
// ============================================================================
const UnsettledImportNotice = ({
  canViewAudit,
  onReload,
  onAcknowledge,
}: {
  canViewAudit: boolean;
  onReload: () => void;
  onAcknowledge: () => void;
}) => {
  return (
    <div className="callout warning" role="status">
      <strong>An import may still be committing.</strong>{" "}
      <span>
        Its response never arrived, so the rows below may predate it. Reload
        before judging the registry, and do not re-import until you know what
        happened — a second import would record the same roster twice.
      </span>{" "}
      <span>
        {canViewAudit
          ? "The audit trail settles it: look for a CHANNEL_IMPORTED entry for " +
            "this roster. You can also re-open Import CSV and preview the same " +
            "file — Apply stays blocked until this is accounted for."
          : "Your role cannot open the audit trail, which is the only place " +
            "that says which import wrote what. Re-open Import CSV and preview " +
            "the same file to see whether the registry already matches it — " +
            "Apply stays blocked until this is accounted for — and ask someone " +
            "with audit access to confirm the CHANNEL_IMPORTED entry before " +
            "importing it again."}
      </span>
      <div className="view-actions">
        <button className="ghost-button" type="button" onClick={onReload}>
          Reload registry
        </button>
        <button className="ghost-button" type="button" onClick={onAcknowledge}>
          {canViewAudit
            ? "I have checked the audit trail"
            : "This import is accounted for"}
        </button>
      </div>
    </div>
  );
};

/**
 * The registry main panel: header + either the import stepper (importOpen) or
 * the steady-state mapping band + registry table. importOpen can only ever
 * become true for an import-capable operator — the header renders the opening
 * control only behind canImportChannels.
 */
const RegistryMainPanel = ({
  canManageRegistry,
  canImportChannels,
  channelState,
  unitsById,
  importOpen,
  importUnsettled,
  canViewAudit,
  importScope,
  importScopeSettled,
  onStartImport,
  onCancelImport,
  onImportDone,
  onAcknowledgeUnsettled,
  ...rowActions
}: {
  canManageRegistry: boolean;
  canImportChannels: boolean;
  channelState: ChannelAsyncState;
  unitsById: Map<string, OrgUnit>;
  importOpen: boolean;
  importUnsettled: boolean;
  canViewAudit: boolean;
  importScope: string | undefined;
  /** False while that namespace can still change under a write. */
  importScopeSettled: boolean;
  onStartImport: () => void;
  onCancelImport: () => void;
  onImportDone: () => void;
  onAcknowledgeUnsettled: () => void;
} & RowActions) => {
  return (
    <section className="panel">
      <RegistryPanelHeader
        canImportChannels={canImportChannels}
        importOpen={importOpen}
        onStartImport={onStartImport}
      />
      {importOpen ? (
        <RegistryImportFlow
          importScope={importScope}
          importScopeSettled={importScopeSettled}
          onCancel={onCancelImport}
          onDone={onImportDone}
        />
      ) : (
        <>
          {importUnsettled ? (
            <UnsettledImportNotice
              canViewAudit={canViewAudit}
              onReload={channelState.reload}
              onAcknowledge={onAcknowledgeUnsettled}
            />
          ) : null}
          <RegistryMappingBand canManageRegistry={canManageRegistry} />
          <RegistryTable
            canManageRegistry={canManageRegistry}
            channelState={channelState}
            unitsById={unitsById}
            {...rowActions}
          />
        </>
      )}
    </section>
  );
};

// ============================================================================
// Purpose: Guard + field check for the channel-mapping submission — the
//   operator holds the registry capability, no submit is in flight, and the
//   channel id, company id, and audited reason are all non-blank. Extracted to
//   keep MappingChangeRequestPanel's cyclomatic complexity below the DeepSource
//   medium-risk threshold.
// Database/ORM: None (frontend) — a pure predicate over resolved form state.
// Standards: No client-side authorization is invented. `canManageRegistry` is a
//   backend-derived capability, so this gates the affordance only and the
//   backend's dual MANAGE_ORG_MAPPING check stays authoritative. The reason
//   arrives already trimmed, so whitespace cannot pass as a supplied value.
//   Side-effect free and total.
// Blast Radius: Registry write + audit — a submit PATCHes a channel's org
//   mapping, which moves the channel between company/sector rollups and so
//   changes which principals can see its revenue downstream. This predicate is
//   what keeps a blank audited reason or an unset channel/company from reaching
//   PATCH /channels/{id}/mapping. It is a usability gate, not the authorization
//   boundary — and not the double-submit guard either: `busy` is React state, so
//   both clicks of a same-tick double-click read it as false off the same
//   render. The synchronous inFlightRef latch in the panel's submit is what
//   actually drops the second PATCH; `busy` only keeps the control disabled
//   across the renders that follow.
// Connections: channels.py update_channel_mapping (authoritative gate + audited
//   write), MappingChangeRequestPanel (owns `busy`, the reason, and the latch).
//   - File: backend/ums_smart_revenue/api/channels.py:1123
//     update_channel_mapping -> the authoritative permission check and the
//     audited mapping write.
//   - File: frontend/src/lib/api/useChannelMapping.ts:27
//     useChannelMappingAction -> owns the PATCH this predicate gates.
//   - File: frontend/src/components/srcc/views/RegistryView.tsx ->
//     MappingChangeRequestPanel's inFlightRef (declared beside its useState
//     block, latched in submit) is the real same-tick double-submit guard.
//     `busy` is that panel's own useState, not something the hook owns. Named
//     by symbol, not line: this pointer is into its own file, so any edit
//     above it would silently invalidate a line number.
// ============================================================================
const isMappingSubmittable = (
  canManageRegistry: boolean,
  busy: boolean,
  channelId: string,
  companyId: string,
  trimmedReason: string,
): boolean => {
  return (
    canManageRegistry && !busy &&
    channelId !== "" && companyId !== "" && trimmedReason !== ""
  );
};

/** True when the preset channelId is not present in the loaded channel list. */
const isMissingSelectedChannel = (
  channelId: string,
  channels: ChannelRegistryEntry[],
): boolean => {
  return channelId !== "" && !channels.some((ch) => ch.youtube_channel_id === channelId);
};

// ============================================================================
// Purpose: LIVE mapping-change form — submits PATCH /channels/{id}/mapping
//   with {primary_company_id, reason}. Channel options come from the live
//   channel list; company options from active COMPANY org-units (labels are
//   display names, submitted values are org-unit UUIDs). A row "Map" click
//   presets the channel. The mock "Save Draft" and "Effective month" controls
//   are gone — the backend has no draft or effective-month concept on this
//   route, and a dead affordance would be dishonest UI.
// Standards: reason required + trimmed; synchronous in-flight ref latch (one
//   PATCH per click burst -> one CHANNEL_UPDATED audit event); success reloads
//   the channel list; typed ApiError surfaces inline (403 incl. the
//   unmapped-channel dead-zone, 404, 409, 422) and form state survives for
//   retry. canManageRegistry disables inputs; the backend dual
//   MANAGE_ORG_MAPPING check stays the authority.
// ============================================================================
const MappingChangeRequestPanel = ({
  canManageRegistry,
  channels,
  companies,
  preset,
  onMapped,
}: {
  canManageRegistry: boolean;
  channels: ChannelRegistryEntry[];
  companies: OrgUnit[];
  preset: { channelId: string } | null;
  onMapped: () => void;
}) => {
  const [channelId, setChannelId] = useState("");
  const [companyId, setCompanyId] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<string | null>(null);
  const inFlightRef = useRef(false);
  const submitMapping = useChannelMappingAction();

  // A row "Map" click presets the channel selection (operator can still change
  // it) AND clears any stale form values from a previous action — the
  // preset is a fresh object per click, so even re-clicking the same row resets.
  // FIX: also clear companyId and reason so a stale company from a prior row
  // cannot silently carry over to the newly targeted channel.
  useEffect(() => {
    if (preset) {
      setChannelId(preset.channelId);
      setCompanyId("");
      setReason("");
      setConfirmation(null);
      setError(null);
    }
  }, [preset]);

  const trimmedReason = reason.trim();
  const canSubmit = isMappingSubmittable(canManageRegistry, busy, channelId, companyId, trimmedReason);

  /** Submit the mapping-change PATCH; latched against concurrent double-clicks. */
  const submit = async () => {
    if (!canSubmit) return;
    // Synchronous latch: both clicks of a same-tick double-click run off the
    // same render, so state alone cannot drop the second PATCH.
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy(true);
    setError(null);
    setConfirmation(null);
    try {
      await submitMapping(channelId, companyId, trimmedReason);
      setBusy(false);
      setCompanyId("");
      setReason("");
      setConfirmation("Mapping updated — audited as CHANNEL_UPDATED.");
      onMapped();
    } catch (caught) {
      setBusy(false);
      setError(describeMutationError(caught));
      // Form state stays intact so the operator can correct and retry.
    } finally {
      inFlightRef.current = false;
    }
  };
  const selectedChannelMissing = isMissingSelectedChannel(channelId, channels);

  // Hoist option lists out of the return to keep JSX nesting ≤ 4 levels deep.
  const channelOpts = [
    <option key="_ph" value="">Select channel…</option>,
    ...(selectedChannelMissing
      ? [<option key={channelId} value={channelId}>{channelId}</option>]
      : []),
    ...channels.map((ch) => (
      <option key={ch.youtube_channel_id} value={ch.youtube_channel_id}>
        {ch.channel_name} ({ch.youtube_channel_id})
      </option>
    )),
  ];
  const companyOpts = [
    <option key="_ph" value="">Select company…</option>,
    ...companies.map((unit) => (
      <option key={unit.id} value={unit.id}>{unit.name}</option>
    )),
  ];

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
        <div className="field-row">
          <label htmlFor="registryChannel">Channel</label>
          <select
            id="registryChannel"
            disabled={!canManageRegistry}
            value={channelId}
            onChange={(e) => setChannelId(e.target.value)}
          >
            {channelOpts}
          </select>
        </div>
        <div className="field-row">
          <label htmlFor="registryCompany">Company</label>
          <select
            id="registryCompany"
            disabled={!canManageRegistry}
            value={companyId}
            onChange={(e) => setCompanyId(e.target.value)}
          >
            {companyOpts}
          </select>
        </div>
        <div className="field-row">
          <label htmlFor="registryReason">Reason (required, audited)</label>
          <input
            id="registryReason"
            disabled={!canManageRegistry}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why this mapping changes"
          />
        </div>
      </div>
      {error ? (
        <div className="form-error" role="alert">
          <strong>Mapping change failed</strong>
          <span>{error}</span>
        </div>
      ) : null}
      {confirmation ? <div className="form-note">{confirmation}</div> : null}
      <div className="action-row">
        <button
          className="primary-button"
          type="button"
          disabled={!canSubmit}
          onClick={submit}
        >
          {busy ? "Submitting…" : "Submit mapping change"}
        </button>
      </div>
    </section>
  );
};

/** Current month as YYYY-MM for the proposal default (operator can change it). */
const currentMonth = (): string => {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
};

/** May the operator act on the proposal form at all (permission held, no submit in flight)? */
const canActOnProposal = (canManageRegistry: boolean, busy: boolean): boolean =>
  canManageRegistry && !busy;

// ============================================================================
// Purpose: Guard + field check for the account-link proposal submission — the
//   operator holds the registry capability, no submit is in flight, and the
//   AdSense account id, content-owner id, effective month, and audited reason
//   are all non-blank. Extracted to keep AccountLinkProposalPanel's cyclomatic
//   complexity below the DeepSource medium-risk threshold.
// Database/ORM: None (frontend) — a pure predicate over resolved form state.
// Standards: No client-side authorization is invented. `canManageRegistry` is a
//   backend-derived capability, so this gates the affordance only and the
//   backend's MANAGE_ORG_MAPPING@global check stays authoritative. Ids and
//   reason are checked after trimming, so whitespace never passes as supplied.
//   Side-effect free and total.
// Blast Radius: Finance map write + audit — a proposal inserts an UNVERIFIED
//   AdSense-account to content-owner link. This predicate is what keeps a blank
//   audited reason or a whitespace-only identifier from reaching
//   POST /revenue/channel-account-links, and the busy half of canActOnProposal
//   is what stops a double-click filing a duplicate proposal. It cannot create
//   a VERIFIED link — verification is a separate dual-gated admin flow.
// Connections: channel_account_links.py propose_channel_account_link
//   (authoritative gate + audited insert), canActOnProposal (local half).
//   - File: backend/ums_smart_revenue/api/channel_account_links.py:275
//     propose_channel_account_link -> the authoritative permission gate and the
//     audited insert.
//   - File: frontend/src/components/srcc/views/RegistryView.tsx ->
//     canActOnProposal supplies the capability + in-flight half.
// ============================================================================
const isProposalSubmittable = (
  canManageRegistry: boolean,
  busy: boolean,
  adsenseAccountId: string,
  contentOwnerId: string,
  effectiveMonthStart: string,
  reason: string,
): boolean => {
  return (
    canActOnProposal(canManageRegistry, busy) && adsenseAccountId.trim() !== "" &&
    contentOwnerId.trim() !== "" && effectiveMonthStart !== "" &&
    reason.trim() !== ""
  );
};

// ============================================================================
// Purpose: LIVE account-link proposal form — submits POST
//   /revenue/channel-account-links proposing an UNVERIFIED AdSense-account ↔
//   content-owner link (provenance_kind fixed OPERATOR_ASSERTED, empty
//   payload). Verification stays a separate dual-gated admin flow — this panel
//   never claims a link is verified. A row "Assign" click sets the channel
//   context line; the link itself is account↔owner grain, not channel grain.
// Standards: same mutation discipline as the mapping panel (required trimmed
//   reason, in-flight ref latch, inline typed errors, reload on success).
//   Backend gate: MANAGE_ORG_MAPPING@global.
// ============================================================================
const AccountLinkProposalPanel = ({
  canManageRegistry,
  context,
  onProposed,
}: {
  canManageRegistry: boolean;
  context: { channel: ChannelRegistryEntry } | null;
  onProposed: () => void;
}) => {
  const [adsenseAccountId, setAdsenseAccountId] = useState("");
  const [contentOwnerId, setContentOwnerId] = useState("");
  const [effectiveMonthStart, setEffectiveMonthStart] = useState(currentMonth);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<string | null>(null);
  const inFlightRef = useRef(false);
  const propose = useProposeAccountLinkAction();

  // A row "Assign" click resets the full form so that stale values from a prior
  // channel's proposal never carry over to the newly targeted channel.
  // FIX: also clear account IDs and reset the effective month so the operator
  // always starts from a blank form when switching Assign targets.
  useEffect(() => {
    if (context) {
      setAdsenseAccountId("");
      setContentOwnerId("");
      setEffectiveMonthStart(currentMonth());
      setReason("");
      setConfirmation(null);
      setError(null);
    }
  }, [context]);

  const canSubmit = isProposalSubmittable(
    canManageRegistry, busy, adsenseAccountId, contentOwnerId,
    effectiveMonthStart, reason,
  );

  /** Submit the account-link proposal POST; latched against concurrent double-clicks. */
  const submit = async () => {
    if (!canSubmit) return;
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy(true);
    setError(null);
    setConfirmation(null);
    try {
      await propose({
        adsenseAccountId: adsenseAccountId.trim(),
        contentOwnerId: contentOwnerId.trim(),
        effectiveMonthStart,
        reason: reason.trim(),
      });
      setBusy(false);
      setAdsenseAccountId("");
      setContentOwnerId("");
      setEffectiveMonthStart(currentMonth());
      setReason("");
      setConfirmation(
        "Link proposed (UNVERIFIED) — verification is a separate admin step.",
      );
      onProposed();
    } catch (caught) {
      setBusy(false);
      setError(describeMutationError(caught));
    } finally {
      inFlightRef.current = false;
    }
  };

  return (
    <section className="panel">
      <div className="panel-header">
        <div className="panel-title">
          <strong>Account Link Proposal</strong>
          <span>Proposes an UNVERIFIED AdSense account ↔ content owner link</span>
        </div>
        <Badge tone="amber">Audit required</Badge>
      </div>
      {context ? (
        <div className="form-note">
          Context: {context.channel.channel_name} ({context.channel.youtube_channel_id})
        </div>
      ) : null}
      <div className="form-grid">
        <div className="field-row">
          <label htmlFor="assignAccount">AdSense account ID</label>
          <input
            id="assignAccount"
            disabled={!canManageRegistry}
            value={adsenseAccountId}
            onChange={(e) => setAdsenseAccountId(e.target.value)}
            placeholder="pub-…"
          />
        </div>
        <div className="field-row">
          <label htmlFor="assignOwner">Content owner ID</label>
          <input
            id="assignOwner"
            disabled={!canManageRegistry}
            value={contentOwnerId}
            onChange={(e) => setContentOwnerId(e.target.value)}
            placeholder="content owner"
          />
        </div>
        <div className="field-row">
          <label htmlFor="assignMonth">Effective month start</label>
          <input
            id="assignMonth"
            type="month"
            disabled={!canManageRegistry}
            value={effectiveMonthStart}
            onChange={(e) => setEffectiveMonthStart(e.target.value)}
          />
        </div>
        <div className="field-row">
          <label htmlFor="assignReason">Proposal reason (required, audited)</label>
          <input
            id="assignReason"
            disabled={!canManageRegistry}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Evidence for this link"
          />
        </div>
      </div>
      {error ? (
        <div className="form-error" role="alert">
          <strong>Link proposal failed</strong>
          <span>{error}</span>
        </div>
      ) : null}
      {confirmation ? <div className="form-note">{confirmation}</div> : null}
      <div className="action-row">
        <button
          className="primary-button"
          type="button"
          disabled={!canSubmit}
          onClick={submit}
        >
          {busy ? "Proposing…" : "Propose link"}
        </button>
      </div>
    </section>
  );
};

/** The registry-controls panel listing the expected production behaviors. */
const RegistryControlsPanel = () => {
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
};

/** Registry side panels: the live mapping-change form, the account-link proposal form, and registry controls. */
const RegistrySidePanels = ({
  canManageRegistry,
  channels,
  companies,
  mapPreset,
  assignContext,
  onMutated,
}: {
  canManageRegistry: boolean;
  channels: ChannelRegistryEntry[];
  companies: OrgUnit[];
  mapPreset: { channelId: string } | null;
  assignContext: { channel: ChannelRegistryEntry } | null;
  onMutated: () => void;
}) => {
  return (
    <aside className="view-stack" aria-label="Registry side panels">
      <MappingChangeRequestPanel
        canManageRegistry={canManageRegistry}
        channels={channels}
        companies={companies}
        preset={mapPreset}
        onMapped={onMutated}
      />
      <AccountLinkProposalPanel
        canManageRegistry={canManageRegistry}
        context={assignContext}
        onProposed={onMutated}
      />
      <RegistryControlsPanel />
    </aside>
  );
};

// ---- Root component ---------------------------------------------------------

/**
 * The real-data channel registry view. useChannels() and useOrgUnits() are each
 * called ONCE here and threaded down — a second hook call in a child would
 * duplicate the GET. Row actions set the side-panel targets (Map/Assign) or
 * navigate to Trace (Review). The importing flag swaps the main panel's
 * content for RegistryImportFlow: cancel restores the table untouched, done
 * restores it and reloads the channel list.
 */
const RegistryView = ({
  canManageRegistry,
  canImportChannels,
  canViewFinance,
  canViewAudit = false,
  importScope,
  importScopeSettled = true,
  onOpenTrace,
}: {
  canManageRegistry: boolean;
  canImportChannels: boolean;
  canViewFinance: boolean;
  /** Namespaces the unsettled-import records to one tenant + principal. Omitted
   * only in standalone renders, which fall back to the unscoped bucket. */
  importScope?: string;
  /** False while that namespace can still change under a write. Defaults to
   * true: a standalone render supplies no scope and has nothing to wait on. */
  importScopeSettled?: boolean;
  /** Whether AuditView is reachable at all. Defaults to the SAFE assumption:
   * without it the notice must not send the operator somewhere they will be
   * refused. */
  canViewAudit?: boolean;
  onOpenTrace?: (channelId: string) => void;
}) => {
  const channelState = useChannels();
  const orgUnitState = useOrgUnits();
  // Fresh object per click (not a bare id): the panels' effects key on object
  // identity, so re-clicking the SAME row still re-presets the form and clears
  // any stale success/error note from a previous action.
  const [mapPreset, setMapPreset] = useState<{ channelId: string } | null>(null);
  const [assignContext, setAssignContext] =
    useState<{ channel: ChannelRegistryEntry } | null>(null);
  const [importing, setImporting] = useState(false);
  // Raised when the stepper exits with an apply whose outcome it never learned.
  // Held in the SHELL, not here: this view unmounts the moment the operator
  // follows the notice's own advice and opens the Audit trail, and the flag
  // has to survive that (review #184).
  const unsettledImport = useUnsettledImport(importScope);
  // The applies the CURRENT warning represents, captured when it goes up. The
  // operator's acknowledgement is about those, and re-reading the scope at
  // click time would also retire an apply another tab admitted after the
  // warning rendered — a live request the warning never mentioned (review
  // #184, codex P2). Re-captured only on a false -> true transition, so ids
  // added while the warning stands are deliberately excluded and keep it up.
  const warnedApplyIdsRef = useRef<readonly string[]>([]);
  const { unsettled, snapshotPendingIds } = unsettledImport;
  useEffect(() => {
    if (unsettled) {
      warnedApplyIdsRef.current = snapshotPendingIds();
    }
  }, [unsettled, snapshotPendingIds]);

  const unitsById = useMemo(
    () => new Map((orgUnitState.data ?? []).map((unit) => [unit.id, unit])),
    [orgUnitState.data],
  );
  const companies = useMemo(
    () =>
      (orgUnitState.data ?? []).filter((unit) => unit.type === "COMPANY"),
    [orgUnitState.data],
  );

  const summaryTiles = buildSummaryTiles(
    channelState.data,
    channelState.loading,
    REGISTRY_SUMMARY,
  );

  const onMap = useCallback(
    (ch: ChannelRegistryEntry) =>
      setMapPreset({ channelId: ch.youtube_channel_id }),
    [],
  );
  const onAssign = useCallback(
    (ch: ChannelRegistryEntry) => setAssignContext({ channel: ch }),
    [],
  );
  const onReview = useCallback(
    (ch: ChannelRegistryEntry) => onOpenTrace?.(ch.youtube_channel_id),
    [onOpenTrace],
  );

  return (
    <section className="view-page" aria-labelledby="registryTitle">
      <div className="view-summary" aria-label="Registry summary">
        {summaryTiles.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>

      <div className="view-grid wide-side">
        <RegistryMainPanel
          canManageRegistry={canManageRegistry}
          canImportChannels={canImportChannels}
          channelState={channelState}
          unitsById={unitsById}
          importOpen={importing}
          importUnsettled={unsettledImport.unsettled}
          canViewAudit={canViewAudit}
          importScope={importScope}
          importScopeSettled={importScopeSettled}
          onStartImport={() => setImporting(true)}
          onCancelImport={() => setImporting(false)}
          onImportDone={() => {
            setImporting(false);
            channelState.reload();
            // Nothing to raise here: the flow raises the unsettled flag BEFORE
            // it dispatches the apply and clears it only once the response
            // establishes an outcome. Raising on this callback instead left
            // the case codex found — an operator who leaves by the sidebar
            // never reaches it, and neither does a closed tab.
          }}
          onAcknowledgeUnsettled={() => {
            unsettledImport.acknowledge(warnedApplyIdsRef.current);
            // RE-CAPTURE what remains. The effect only fires on a false -> true
            // transition, so if one apply settles while another is still
            // pending the flag never drops and the ref would stay pinned to
            // the settled id — every further acknowledgement replaying a list
            // that retires nothing, leaving the operator blocked until a
            // reload. Refreshing here keeps the exclusion (the later apply
            // survives THIS click) while letting the next click acknowledge
            // the warning that is still on screen (review #184, codex P2).
                      warnedApplyIdsRef.current = unsettledImport.snapshotPendingIds();
          }}
          hasTraceNav={Boolean(onOpenTrace)}
          onMap={onMap}
          onAssign={onAssign}
          onReview={onReview}
        />
        <RegistrySidePanels
          canManageRegistry={canManageRegistry}
          channels={channelState.data ?? []}
          companies={companies}
          mapPreset={mapPreset}
          assignContext={assignContext}
          onMutated={channelState.reload}
        />
      </div>
    </section>
  );
};

export default RegistryView;
