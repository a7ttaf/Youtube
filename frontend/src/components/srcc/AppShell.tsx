import { useEffect, useRef, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type { TenantRead } from "@/lib/api/types";
import { useTenant } from "@/contexts/TenantContext";
import {
  AUDIT_EVENTS,
  AUDIT_SUMMARY,
  NAV_GROUPS,
  REGISTRY_CONTROLS,
  REGISTRY_ROWS,
  REGISTRY_SUMMARY,
  VIEW_COPY,
  WORKFLOW_STEPS,
} from "@/lib/mock/data";
import type { Role, ViewKey } from "@/lib/mock/data";
import { BrandIcon, NAV_ICONS, RefreshIcon } from "./icons";
import CloseView from "./views/CloseView";
import CommandView from "./views/CommandView";
import ConnectorsView from "./views/ConnectorsView";
import ExportsView from "./views/ExportsView";
import TraceView from "./views/TraceView";
import {
  Badge,
  Dot,
  ItemRow,
  RESTRICTED_FINANCE_VALUE,
  SummaryTile,
  workflowDotTone,
} from "./shared";

/* ------------------------------------------------------------------ shared */

const ROLE_LABELS: Record<Role, string> = {
  finance: "Finance Admin",
  assistant: "Assistant Analyst",
  company: "Company Manager",
};

type AuthenticatedSession = {
  role?: Role;
};

type AccessPermissions = {
  role: Role;
  canViewFinance: boolean;
  canManageRegistry: boolean;
  canCloseMonth: boolean;
  canCreateGlobalExports: boolean;
  canCreateScopedExports: boolean;
  canRequestRawExports: boolean;
  canExportFinanceReports: boolean;
  canExportAnalyticsReports: boolean;
  canRunConnectors: boolean;
  canViewAudit: boolean;
};

// FIX: The previous comment claimed this value "is hydrated from the
// server-authenticated session claim", but no hydration exists yet — the
// backend exposes no principal/role endpoint (only GET /tenants/me). It is an
// intentionally empty placeholder: a production build without
// VITE_ENABLE_ROLE_PREVIEW renders <AccessDeniedState/> (fail-closed) until a
// backend session endpoint ships. Tracked in Docs/15_DELIVERY_BACKLOG.md
// ("Production session role hydration").
const SERVER_AUTHENTICATED_SESSION: AuthenticatedSession = {};

const CAN_PREVIEW_ROLES =
  import.meta.env.DEV || import.meta.env.VITE_ENABLE_ROLE_PREVIEW === "true";
const DEFAULT_PREVIEW_ROLE: Role = "assistant";

/**
 * Resolve the boolean access permissions for a preview role so each view gates
 * finance visibility, registry editing, exports, connectors, and audit consistently.
 */
const permissionsForRole = (role: Role): AccessPermissions => {
  const finance = role === "finance";
  const company = role === "company";
  return {
    role,
    canViewFinance: finance,
    canManageRegistry: finance,
    canCloseMonth: finance,
    canCreateGlobalExports: finance,
    canCreateScopedExports: finance || company,
    canRequestRawExports: finance,
    canExportFinanceReports: finance,
    canExportAnalyticsReports: finance || company,
    // FIX: None of the frontend preview roles (finance/assistant/company) maps
    // to a backend role that holds RUN_CONNECTOR_JOBS — only super_owner,
    // revenue_operations_admin, system_integration_user, and connector_admin do
    // (backend auth/seed.py). The previous `finance` flag enabled connector
    // controls that always 403 at the backend, so it is fail-closed to false.
    canRunConnectors: false,
    canViewAudit: finance,
  };
};

/**
 * Report whether the viewer may create any export variant (global, scoped, or
 * raw), used to enable the header Create Export action.
 */
export function canCreateAnyExport(permissions: AccessPermissions) {
  return (
    permissions.canCreateGlobalExports ||
    permissions.canCreateScopedExports ||
    permissions.canRequestRawExports
  );
}

/** Render the fallback panel shown when no authenticated session role is present. */
export function AccessDeniedState() {
  return (
    <div className="app">
      <main className="main" aria-labelledby="accessDeniedTitle">
        <section className="panel">
          <AccessDeniedHeader />
        </section>
      </main>
    </div>
  );
}

/** Panel header for the access-denied state, kept flat to limit JSX nesting. */
const AccessDeniedHeader = () => {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="accessDeniedTitle">Access denied</strong>
        <span>Authenticated session role is required.</span>
      </div>
      <Badge tone="red">No session role</Badge>
    </div>
  );
};

/* ------------------------------------------------------------------ tenant bootstrap */

type TenantBootstrap = {
  /** The label rendered in the dev-only tenant proof tag. */
  proofLabel: string;
};

// ============================================================================
// Purpose: Fire /tenants/me once on mount; hydrate TenantContext on success or
//          surface the typed ApiError message in the dev-only proof tag on
//          failure, returning the proof label for the shell to render.
// Database/ORM: None (frontend API call only).
// Standards: useRef re-entry guard keeps fetch count at 1 under React StrictMode.
//            Effect is gated on displayedRole so sessions that immediately
//            render <AccessDeniedState/> never issue a bootstrap fetch.
//            The guard is reset in the failure path so a subsequent dep
//            change (role switch, provider rebuild) can retry — a transient
//            5xx must not permanently pin tenantSlug to the bootstrap value.
// Blast Radius: None detected (read-only; does not mutate financial state).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET helper.
//   - File: frontend/src/contexts/TenantContext.tsx -> hydrate() stores id/displayName.
// ============================================================================
/**
 * Bootstrap the active tenant from /tenants/me, hydrating TenantContext and
 * exposing the dev-only proof label that reflects success or the typed error.
 */
export function useTenantBootstrap(displayedRole: Role | undefined): TenantBootstrap {
  const tenant = useTenant();
  const client = useApiClient();
  const hasRequestedTenantRef = useRef(false);
  const [tenantError, setTenantError] = useState<ApiError | Error | null>(null);

  useEffect(() => {
    if (!displayedRole) return;
    if (hasRequestedTenantRef.current || tenant.id) return;
    hasRequestedTenantRef.current = true;
    client
      .get<TenantRead>("/tenants/me")
      .then((payload) => {
        tenant.hydrate(payload);
        // FIX: Clear any stale tenantError from a prior failed attempt so a
        // successful retry re-renders the hydrated success state. Without
        // this, the proof tag stayed pinned to the failure branch even after
        // the user retried and /tenants/me returned 200.
        setTenantError(null);
      })
      .catch((error: unknown) => {
        // FIX: Clear the one-shot guard on failure so a future dependency
        // change (role switch, client rebuild after slug change) can retry
        // the bootstrap fetch. Without this, a transient 5xx on first load
        // permanently disabled re-hydration and pinned X-UMS-Tenant to "ums".
        hasRequestedTenantRef.current = false;
        setTenantError(error as ApiError | Error);
      });
  }, [client, tenant.id, tenant.hydrate, displayedRole]);

  return { proofLabel: tenantProofLabel(tenant, tenantError) };
}

/**
 * Extract the trimmed `detail` string from a typed ApiError JSON body, or null
 * when the body has no usable detail message.
 */
const apiErrorDetail = (error: ApiError | Error | null): string | null => {
  if (!(error instanceof ApiError)) {
    return null;
  }
  const body = error.body;
  if (typeof body !== "object" || body === null) {
    return null;
  }
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail !== "string") {
    return null;
  }
  const trimmed = detail.trim();
  if (trimmed.length === 0) {
    return null;
  }
  return trimmed;
};

/**
 * Build the dev-only tenant proof label from the hydrated tenant context and
 * any bootstrap error, covering the loading, success, and failure states.
 */
const tenantProofLabel = (
  tenant: ReturnType<typeof useTenant>,
  tenantError: ApiError | Error | null,
): string => {
  // Pre-hydration the slug is intentionally empty — show a sentinel rather
  // than a stray space so the dev proof tag stays readable.
  const displaySlug = tenant.tenantSlug || "(resolving…)";
  if (tenantError) {
    const detail = apiErrorDetail(tenantError);
    if (detail) {
      return detail;
    }
    return `(error)`;
  } else if (!tenant.tenantSlug) {
    return displaySlug;
  }
  return displaySlug;
};
    return `Tenant: ${displaySlug}; /tenants/me failed: ${tenantError.message}${
      detail ? ` — ${detail}` : ""
    }`;
  }
  if (tenant.id) {
    return `Tenant: ${tenant.displayName} (${tenant.tenantSlug}) — id ${tenant.id}`;
  }
  return `Tenant: ${displaySlug} (loading…)`;
};

/* ------------------------------------------------------------------ shell */

/**
 * Top-level SRCC shell: resolves the displayed role, bootstraps the tenant, and
 * composes the sidebar, top bar, and active view for the control center.
 */
export default function AppShell() {
  const [view, setView] = useState<ViewKey>("command");
  const authenticatedRole = SERVER_AUTHENTICATED_SESSION.role;
  const [previewRole, setPreviewRole] = useState<Role>(
    authenticatedRole ?? DEFAULT_PREVIEW_ROLE,
  );

  const displayedRole = CAN_PREVIEW_ROLES ? previewRole : authenticatedRole;
  const { proofLabel } = useTenantBootstrap(displayedRole);

  if (!displayedRole) {
    return <AccessDeniedState />;
  }

  const permissions = permissionsForRole(displayedRole);
  const canViewFinance = permissions.canViewFinance;
  const copy = VIEW_COPY[view];

  const viewComponents: Record<ViewKey, JSX.Element | null> = {
    command: <WorkflowRail />,
  };

  return (
    <div className="app">
      {import.meta.env.DEV && <TenantProofTag label={proofLabel} />}
      <Sidebar
        view={view}
        onSelectView={setView}
        previewRole={previewRole}
        onSelectPreviewRole={setPreviewRole}
        displayedRole={displayedRole}
        canViewFinance={canViewFinance}
      />
      <main className="main">
        <Topbar
          title={copy.title}
          subtitle={copy.subtitle}
          canViewFinance={canViewFinance}
          canCreateExport={canCreateAnyExport(permissions)}
        />
        <ViewRouter
          view={view}
          permissions={permissions}
          canViewFinance={canViewFinance}
          displayedRole={displayedRole}
        />
        {viewComponents[view]}
      </main>
    </div>
  );
}

/** Dev-only fixed-position tag that proves which tenant the shell resolved. */
export function TenantProofTag({ label }: { label: string }) {
  return (
    <small
      data-testid="tenant-proof"
      style={{
        position: "fixed",
        bottom: 8,
        right: 8,
        fontSize: 11,
        opacity: 0.6,
        padding: "2px 6px",
        borderRadius: 4,
        background: "rgba(0,0,0,0.4)",
        color: "#fff",
        zIndex: 9999,
        pointerEvents: "none",
      }}
    >
      {label}
    </small>
  );
}

/* ------------------------------------------------------------------ sidebar */

/** Primary navigation sidebar: brand mark, nav groups, and the role card. */
export function Sidebar({
  view,
  onSelectView,
  previewRole,
  onSelectPreviewRole,
  displayedRole,
  canViewFinance,
}: {
  view: ViewKey;
  onSelectView: (key: ViewKey) => void;
  previewRole: Role;
  onSelectPreviewRole: (role: Role) => void;
  displayedRole: Role;
  canViewFinance: boolean;
}) {
  return (
    <aside className="sidebar" aria-label="Primary navigation">
      <div className="brand">
        <div className="brand-mark">
          <BrandIcon />
        </div>
        <div>
          <strong>UMS Revenue</strong>
          <span>Control Center</span>
        </div>
      </div>

      {NAV_GROUPS.map((group) => (
        <NavSection
          key={group.label}
          group={group}
          view={view}
          onSelectView={onSelectView}
        />
      ))}

      <RoleCard
        previewRole={previewRole}
        onSelectPreviewRole={onSelectPreviewRole}
        displayedRole={displayedRole}
        canViewFinance={canViewFinance}
      />
    </aside>
  );
}

/** Render a single labelled navigation group with its selectable items. */
function NavSection({
  group,
  view,
  onSelectView,
}: {
  group: (typeof NAV_GROUPS)[number];
  view: ViewKey;
  onSelectView: (key: ViewKey) => void;
}) {
  return (
    <nav className="nav-section" aria-label={group.label}>
      <div className="nav-title">{group.label}</div>
      {group.items.map((item) => {
        const active = item.key === view;
        return (
          <button
            key={item.key}
            type="button"
            className={`nav-item${active ? " is-active" : ""}`}
            aria-current={active ? "page" : undefined}
            onClick={() => onSelectView(item.key)}
          >
            {NAV_ICONS[item.icon]}
            <span>{item.label}</span>
            <span className="nav-count">{item.count}</span>
          </button>
        );
      })}
    </nav>
  );
}

/** Role selector (preview) plus the finance-visibility permission indicators. */
const RoleCard = ({
  previewRole,
  onSelectPreviewRole,
  displayedRole,
  canViewFinance,
}: {
  previewRole: Role;
  onSelectPreviewRole: (role: Role) => void;
  displayedRole: Role;
  canViewFinance: boolean;
}) => {
  return (
    <div className="role-card">
      <label htmlFor="roleSelect">Current role</label>
      {CAN_PREVIEW_ROLES ? (
        <>
          <select
            id="roleSelect"
            value={previewRole}
            onChange={(e) => onSelectPreviewRole(e.target.value as Role)}
          >
            {(Object.entries(ROLE_LABELS) as [Role, string][]).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <RolePreviewHint />
        </>
      ) : (
        <output id="roleSelect">{ROLE_LABELS[displayedRole]}</output>
      )}
      <div className="role-meta" aria-label="Role permission state">
        <span className={`role-state${canViewFinance ? "" : " is-restricted"}`}>
          <Dot tone={canViewFinance ? "green" : "red"} />
          <span>{canViewFinance ? "Money visible" : "Money withheld"}</span>
        </span>
        <span className="role-state">
          <Dot tone="amber" />
          <span>Raw files gated</span>
        </span>
      </div>
    </div>
  );
};

/**
 * Presentation-only disclaimer rendered beneath the dev role switcher. The
 * switcher only changes the UI's permission MODELLING; every API request still
 * carries the fixed dev-proxy identity (VITE_DEV_GATEWAY_ROLE, injected
 * server-side at proxy start), so backend authorization does not change when the
 * preview role does. The hint makes that explicit so a demo viewer does not read
 * the switcher as a real privilege change.
 */
export function RolePreviewHint() {
  return (
    <small className="role-preview-hint" data-testid="role-preview-hint">
      Presentation preview only — API permissions come from the dev gateway role.
    </small>
  );
}

/* ------------------------------------------------------------------ topbar */

/** Page header: title, operational cues, and the report filter / export controls. */
export function Topbar({
  title,
  subtitle,
  canViewFinance,
  canCreateExport,
}: {
  title: string;
  subtitle: string;
  canViewFinance: boolean;
  canCreateExport: boolean;
}) {
  return (
    <header className="topbar">
      <div className="page-title">
        <div className="title-row">
          <h1>{title}</h1>
        </div>
        <p>{subtitle}</p>
        <OperationalCues canViewFinance={canViewFinance} />
      </div>
      <div className="control-row" aria-label="Report filters">
        <select className="control" aria-label="Month" defaultValue="Mar 2026">
          <option>Mar 2026</option>
          <option>Feb 2026</option>
          <option>Jan 2026</option>
        </select>
        <select className="control" aria-label="Scope" defaultValue="UMS Holding">
          <option>UMS Holding</option>
          <option>TV Sector</option>
          <option>News Sector</option>
          <option>Company A</option>
        </select>
        <select className="control" aria-label="Currency" defaultValue="USD">
          <option>USD</option>
          <option>EGP</option>
          <option>AED</option>
        </select>
        <button className="icon-button" aria-label="Refresh reports" title="Refresh reports">
          <RefreshIcon />
        </button>
        <button className="primary-button" disabled={!canCreateExport}>
          Create Export
        </button>
      </div>
    </header>
  );
}

/**
 * Operational status cues for the top bar (source, bank gap, export blockers,
 * trace). Extracted so the Topbar JSX tree stays shallow; the bank-gap value is
 * gated behind canViewFinance so non-finance roles see the restricted sentinel.
 */
export function OperationalCues({ canViewFinance }: { canViewFinance: boolean }) {
  return (
    <div className="operational-cues" aria-label="Operational status">
      <span className="cue green">
        Source <strong>A Official</strong>
      </span>
      <span className="cue amber">
        Bank gap <strong>{canViewFinance ? "$31.4K" : RESTRICTED_FINANCE_VALUE}</strong>
      </span>
      <span className="cue red">
        Export blockers <strong>2</strong>
      </span>
      <span className="cue violet">
        Trace <strong>SQL scoped</strong>
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------ view router */

/** Route the active view key to its wired or mock view with the right props. */
export function ViewRouter({
  view,
  permissions,
  canViewFinance,
  displayedRole,
}: {
  view: ViewKey;
  permissions: AccessPermissions;
  canViewFinance: boolean;
  displayedRole: Role;
}) {
  const viewMap: Record<ViewKey, JSX.Element> = {
    command: <CommandView canViewFinance={canViewFinance} />,
    registry: <RegistryView permissions={permissions} />,
    close: <CloseView permissions={permissions} />,
    trace: <TraceView canViewFinance={canViewFinance} role={displayedRole} />,
    exports: (
      <ExportsView
        canCreateExport={canCreateAnyExport(permissions)}
        canExportFinance={permissions.canExportFinanceReports}
        canExportAnalytics={permissions.canExportAnalyticsReports}
      />
    ),
    connectors: (
      <ConnectorsView
        canRunConnectors={permissions.canRunConnectors}
        canViewFinance={permissions.canViewFinance}
      />
    ),
    audit: <AuditView permissions={permissions} />,
  };

  return <>{viewMap[view] || null}</>;
}

/* ------------------------------------------------------------------ command */

// CommandView is the first REAL-data view; it lives in ./views/CommandView.tsx
// and is wired to GET /revenue/months/{month}/net-revenue via useNetRevenue.

/** Month-close workflow rail shown beneath the Command view. */
export function WorkflowRail() {
  return (
    <footer className="workflow" aria-label="Month close workflow">
      <div className="workflow-label">
        Monthly close<span>2 blockers before export</span>
      </div>
      <div className="steps" role="list">
        {WORKFLOW_STEPS.map((s) => (
          <span key={s.label} className={`step ${s.state}`} role="listitem">
            <Dot tone={workflowDotTone(s.tone)} />
            {s.label}
          </span>
        ))}
      </div>
      <button className="primary-button">Open Close</button>
    </footer>
  );
}

/* ------------------------------------------------------------------ registry */

/** Mock Channel Registry view: summary tiles, registry table, and side panels. */
export function RegistryView({ permissions }: { permissions: AccessPermissions }) {
  const { canManageRegistry, canViewFinance } = permissions;
  return (
    <section className="view-page" aria-labelledby="registryTitle">
      <div className="view-summary" aria-label="Registry summary">
        {REGISTRY_SUMMARY.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>

      <div className="view-grid wide-side">
        <RegistryMainPanel canManageRegistry={canManageRegistry} />

        <RegistrySidePanels canManageRegistry={canManageRegistry} />
      </div>
    </section>
  );
}

/**
 * The registry main panel: header (title + bulk/mapping actions), the
 * finance-visible mapping band, and the registry table. Extracted so the
 * RegistryView JSX tree stays shallow (JSX nesting).
 */
export function RegistryMainPanel({ canManageRegistry }: { canManageRegistry: boolean }) {
  return (
    <section className="panel">
      <RegistryPanelHeader canManageRegistry={canManageRegistry} />
      <RegistryMappingBand canManageRegistry={canManageRegistry} />
      <RegistryTable canManageRegistry={canManageRegistry} />
    </section>
  );
}

/** Registry panel header: title/subtitle and the bulk-import / mapping-change actions. */
export function RegistryPanelHeader({ canManageRegistry }: { canManageRegistry: boolean }) {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="registryTitle">Channel Registry</strong>
        <span>Ownership, CMS status, revenue scope, and SQL lineage identity</span>
      </div>
      <div className="view-actions">
        <button className="ghost-button" type="button" disabled={!canManageRegistry}>
          Bulk Import
        </button>
        <button className="primary-button" type="button" disabled={!canManageRegistry}>
          Request Mapping Change
        </button>
      </div>
    </div>
  );
}

/** Finance-visible mapping band; the scope badge reflects registry-edit access. */
export function RegistryMappingBand({ canManageRegistry }: { canManageRegistry: boolean }) {
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

/** The channel registry table column header row. Extracted to keep nesting shallow. */
export function RegistryTableHead() {
  return (
    <thead>
      <tr>
        <th>Channel</th><th>Company</th><th>Sector</th><th>CMS</th>
        <th>Revenue Source</th><th>Trace Key</th><th>State</th><th>Action</th>
      </tr>
    </thead>
  );
}

/** Channel registry data table; trace keys are withheld from non-registry roles. */
export function RegistryTable({ canManageRegistry }: { canManageRegistry: boolean }) {
  return (
    <div className="table-wrap">
      <table aria-label="Channel registry">
        <RegistryTableHead />
        <tbody>
          {REGISTRY_ROWS.map((r) => (
            <RegistryRow key={r.code} row={r} canManageRegistry={canManageRegistry} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A single channel registry row; the action button is gated by registry access. */
function RegistryRow({
  row,
  canManageRegistry,
}: {
  row: (typeof REGISTRY_ROWS)[number];
  canManageRegistry: boolean;
}) {
  return (
    <tr>
      <RegistryChannelCell name={row.name} code={row.code} avatar={row.avatar} />
      <td>{row.company}</td>
      <td>{row.sector}</td>
      <td><Badge tone={row.cms.tone}>{row.cms.text}</Badge></td>
      <td>{row.source}</td>
      <td>
        <span className="code-chip">
          {canManageRegistry ? row.node : RESTRICTED_FINANCE_VALUE}
        </span>
      </td>
      <td><Badge tone={row.state.tone}>{row.state.text}</Badge></td>
      <td>
        <button className="mini-button" type="button" disabled={!canManageRegistry}>
          {row.action}
        </button>
      </td>
    </tr>
  );
}

/** The avatar + name/id identity cell for one registry row. Extracted to keep nesting shallow. */
export function RegistryChannelCell({
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
export function RegistrySidePanels({ canManageRegistry }: { canManageRegistry: boolean }) {
  return (
    <aside className="view-stack" aria-label="Registry side panels">
      <MappingChangeRequestPanel canManageRegistry={canManageRegistry} />
      <RegistryControlsPanel />
    </aside>
  );
}

/**
 * The mapping-change request panel: an audited form (channel, company, reason,
 * effective month) plus save/submit actions. Extracted with a shallow form so
 * the registry side-panel JSX tree stays within the nesting limit.
 */
export function MappingChangeRequestPanel({ canManageRegistry }: { canManageRegistry: boolean }) {
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
        <button className="ghost-button" type="button" disabled={!canManageRegistry}>Save Draft</button>
        <button className="primary-button" type="button" disabled={!canManageRegistry}>Submit Approval</button>
      </div>
    </section>
  );
}

/** A labelled mapping-form select row that owns its own options. Keeps the form tree shallow. */
export function MappingSelectRow({
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

/** A labelled mapping-form text input row. Keeps the form tree shallow. */
export function MappingInputRow({
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
export function RegistryControlsPanel() {
  return (
    <section className="panel">
      <div className="panel-header">
        <div className="panel-title">
          <strong>Registry Controls</strong>
          <span>Production behavior expected from the backend foundation</span>
        </div>
      </div>
      <div className="issue-list" role="list">
        {REGISTRY_CONTROLS.map((c) => (
          <ItemRow key={c.title} tone={c.tone} title={c.title} sub={c.sub}
            trailing={<Badge tone={c.badge.tone}>{c.badge.text}</Badge>} />
        ))}
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ close */

// CloseView is the wired Month-Close view; it lives in ./views/CloseView.tsx
// and reads GET /finance-close/{month} (+ /readiness) and posts lock/unlock
// via the useMonthClose hooks.

/* ------------------------------------------------------------------ trace */

// TraceView is the wired Explain-Number view; it lives in ./views/TraceView.tsx
// and POSTs /revenue/channels/{channel}/months/{month}/explain?metric={metric}
// via the useExplanation hook (reusing useNetRevenue for the channel dropdown).

/* ------------------------------------------------------------------ exports */

// ExportsView is the wired Exports screen; it lives in ./views/ExportsView.tsx
// and reads GET /exports (job list) + POSTs /exports (request a job) via the
// useExports / useExportActions hooks, with plain-anchor binary downloads of
// the generated artifacts for COMPLETED jobs.

/* ------------------------------------------------------------------ connectors */

// ConnectorsView is the wired Connectors / data-source screen; it lives in
// ./views/ConnectorsView.tsx and reads GET /connectors/credentials (data
// sources) + GET /adsense/payments (synced payments) and POSTs /connectors/jobs
// (request sync) + /adsense/sync-payments via the useConnectors / useAdsense
// hooks. It states the run-history gap honestly (no connector-runs read route).

/* ------------------------------------------------------------------ audit */

/** Mock Audit Log view: summary tiles, the audit timeline, and coverage panel. */
export function AuditView({ permissions }: { permissions: AccessPermissions }) {
  const { canViewAudit, canViewFinance } = permissions;
  return (
    <section className="view-page" aria-labelledby="auditTitle">
      <div className="view-summary" aria-label="Audit summary">
        {AUDIT_SUMMARY.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>

      <div className="view-grid">
        <AuditLogPanel canViewAudit={canViewAudit} />
        <AuditCoveragePanel />
      </div>
    </section>
  );
}

/**
 * The audit-log main panel: header (title + severity filter / download actions)
 * and the audit timeline. Extracted so the AuditView JSX tree stays shallow.
 */
export function AuditLogPanel({ canViewAudit }: { canViewAudit: boolean }) {
  return (
    <section className="panel">
      <AuditLogPanelHeader canViewAudit={canViewAudit} />
      <AuditTimeline canViewAudit={canViewAudit} />
    </section>
  );
}

/** Audit-log panel header: title/subtitle and the severity filter + download actions. */
const AuditLogPanelHeader = ({ canViewAudit }: { canViewAudit: boolean }) => {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="auditTitle">Audit Log</strong>
        <span>Every sensitive action records actor, permission, scope, target, and result</span>
      </div>
      <div className="view-actions">
        <select className="control" aria-label="Audit severity" disabled={!canViewAudit}>
          <option>All sensitive</option><option>Denied</option><option>Exports</option>
        </select>
        <button className="ghost-button" type="button" disabled={!canViewAudit}>Download Audit View</button>
      </div>
    </div>
  );
};

/** Audit event timeline; non-audit roles see a single restricted placeholder row. */
export function AuditTimeline({ canViewAudit }: { canViewAudit: boolean }) {
  if (!canViewAudit) {
    return (
      <div className="timeline" role="list">
        <div className="timeline-item" role="listitem">
          <span className="timeline-time">--:--</span>
          <Dot tone="red" />
          <span>
            <span className="item-title">Audit view restricted</span>
            <span className="item-sub">Sensitive audit events require Finance Admin access</span>
          </span>
          <Badge tone="red">Restricted</Badge>
        </div>
      </div>
    );
  }
  return (
    <div className="timeline" role="list">
      {AUDIT_EVENTS.map((e) => (
        <AuditTimelineItem key={e.id} event={e} />
      ))}
    </div>
  );
}

/** A single audit timeline entry: timestamp, tone dot, title/subtitle, and badge. */
const AuditTimelineItem = ({ event }: { event: (typeof AUDIT_EVENTS)[number] }) => {
  return (
    <div className="timeline-item" role="listitem">
      <span className="timeline-time">{event.time}</span>
      <Dot tone={event.tone} />
      <span>
        <span className="item-title">{event.title}</span>
        <span className="item-sub">{event.sub}</span>
      </span>
      <Badge tone={event.badge.tone}>{event.badge.text}</Badge>
    </div>
  );
};

/** Static audit coverage panel listing the always-audited sensitive surfaces. */
export function AuditCoveragePanel() {
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
export function AuditCoverageHeader() {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong>Audit Coverage</strong>
        <span>Required to be present for every sensitive surface</span>
      </div>
    </div>
  );
}
