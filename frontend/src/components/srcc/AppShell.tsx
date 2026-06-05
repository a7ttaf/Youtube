import { useEffect, useRef, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type { SessionCapabilities, TenantRead } from "@/lib/api/types";
import { useSessionBootstrap } from "@/contexts/SessionContext";
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

// FIX: Session capabilities are now hydrated from GET /session/me (see
// SessionProvider / useSessionBootstrap) instead of guessed from a role string.
// The empty SERVER_AUTHENTICATED_SESSION placeholder is removed: production
// renders the dashboard gated by the backend-derived session.capabilities, the
// dev role selector only drives the displayed label, and a failed hydration
// (401/403/network) or session.disabled fails closed to <AccessDeniedState/>.

const DEFAULT_PREVIEW_ROLE: Role = "assistant";

// ============================================================================
// Purpose: Report whether the dev-only role preview selector should render. The
//          selector only changes the DISPLAYED role label; it never fabricates
//          capabilities (those are always authoritative from /session/me). This
//          is a function (not a module const) so the value is read at render
//          time and tests can flip import.meta.env via vi.stubEnv to exercise
//          the production (no-preview) path.
// Standards: Dev preview is presentation only; capabilities stay backend-derived.
// Blast Radius: Authorization (UI label only — never grants a capability).
// ============================================================================
function canPreviewRoles(): boolean { // skipcq: JS-0067
  return (
    import.meta.env.DEV || import.meta.env.VITE_ENABLE_ROLE_PREVIEW === "true"
  );
}

// ============================================================================
// Purpose: Map the backend-DERIVED session capabilities onto the UI gate shape
//          the wired views already consume. This is the single capabilities ->
//          UI-gates translation: every gate traces to an authoritative session
//          capability, so the UI never grants a surface the backend did not.
// Database/ORM: None (frontend).
// Standards: Capabilities are authoritative — no gate is invented. Finance
//            visibility maps to VIEW_REVENUE; close to LOCK_FINANCE_MONTH;
//            allocation editing to CHANGE_ALLOCATION_RULE; every export variant
//            to EXPORT_REVENUE_REPORT; audit to VIEW_AUDIT_LOG; connector job
//            controls to RUN_CONNECTOR_JOBS (NOT to finance, honoring that a
//            finance admin must not be able to trigger connector jobs). Registry
//            management is gated on canViewRevenue — the closest honest backend
//            capability today (no dedicated registry-write permission is exposed
//            on the session contract; this never grants a backend permission).
// Blast Radius: Authorization (UI gating). No graph projection impact detected.
// Connections:
//   - File: backend/ums_smart_revenue/api/session.py -> SessionCapabilities.
//   - File: frontend/src/components/srcc/views/ConnectorsView.tsx -> canRunConnectors.
// ============================================================================
function capabilitiesToPermissions( // skipcq: JS-0067
  role: Role,
  capabilities: SessionCapabilities,
): AccessPermissions {
  const canExport = capabilities.canExportRevenue;
  return {
    role,
    canViewFinance: capabilities.canViewRevenue,
    // No dedicated registry-write capability is exposed on the session contract;
    // gate registry management on the closest honest capability (revenue view).
    canManageRegistry: capabilities.canViewRevenue,
    canCloseMonth: capabilities.canCloseMonth,
    canCreateGlobalExports: canExport,
    canCreateScopedExports: canExport,
    canRequestRawExports: canExport,
    canExportFinanceReports: canExport,
    canExportAnalyticsReports: canExport,
    // Honest: connector job/sync controls require RUN_CONNECTOR_JOBS, which a
    // finance admin does not hold — so canViewRevenue must NOT enable them.
    canRunConnectors: capabilities.canRunConnectorJobs,
    canViewAudit: capabilities.canViewAudit,
  };
}

/**
 * Report whether the viewer may create any export variant (global, scoped, or
 * raw), used to enable the header Create Export action.
 */
function canCreateAnyExport(permissions: AccessPermissions) { // skipcq: JS-0067
  return (
    permissions.canCreateGlobalExports ||
    permissions.canCreateScopedExports ||
    permissions.canRequestRawExports
  );
}

/**
 * Render the fail-closed fallback panel shown when the session could not be
 * hydrated (401/403/network) or the principal is disabled. The detail copy
 * distinguishes a disabled principal from a failed/absent session.
 */
function AccessDeniedState({ disabled = false }: { disabled?: boolean }) { // skipcq: JS-0067
  return (
    <div className="app">
      <main className="main" aria-labelledby="accessDeniedTitle">
        <section className="panel">
          <AccessDeniedHeader disabled={disabled} />
        </section>
      </main>
    </div>
  );
}

/** Panel header for the access-denied state, kept flat to limit JSX nesting. */
function AccessDeniedHeader({ disabled }: { disabled: boolean }) { // skipcq: JS-0067
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="accessDeniedTitle">Access denied</strong>
        <span>
          {disabled
            ? "This account is disabled."
            : "An authenticated session is required."}
        </span>
      </div>
      <Badge tone="red">{disabled ? "Account disabled" : "No session"}</Badge>
    </div>
  );
}

/** Render the loading panel shown while the one-shot /session/me bootstrap runs. */
function SessionLoadingState() { // skipcq: JS-0067
  return (
    <div className="app">
      <main className="main" aria-labelledby="sessionLoadingTitle" aria-busy="true">
        <section className="panel">
          <div className="panel-header">
            <div className="panel-title">
              <strong id="sessionLoadingTitle">Loading session…</strong>
              <span>Resolving your authenticated capabilities.</span>
            </div>
            <Badge tone="blue">Loading</Badge>
          </div>
        </section>
      </main>
    </div>
  );
}

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
//            Effect is gated on `enabled` (the session is ready) so a shell that
//            renders the loading or fail-closed <AccessDeniedState/> never issues
//            a tenant bootstrap fetch. The guard is reset in the failure path so
//            a subsequent dep change (provider rebuild) can retry — a transient
//            5xx must not permanently pin tenantSlug to the bootstrap value.
// Blast Radius: None detected (read-only; does not mutate financial state).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET helper.
//   - File: frontend/src/contexts/TenantContext.tsx -> hydrate() stores id/displayName.
// ============================================================================
/**
 * Bootstrap the active tenant from /tenants/me, hydrating TenantContext and
 * exposing the dev-only proof label that reflects success or the typed error.
 * Gated on `enabled` so it only fires once the authenticated session is ready;
 * `retryToken` is an opaque dependency (the dev preview role) that, when it
 * changes after a failure, re-fires the bootstrap so a transient 5xx is not
 * permanent.
 */
function useTenantBootstrap( // skipcq: JS-0067
  enabled: boolean,
  retryToken: string,
): TenantBootstrap {
  const tenant = useTenant();
  const client = useApiClient();
  const hasRequestedTenantRef = useRef(false);
  const [tenantError, setTenantError] = useState<ApiError | Error | null>(null);

  useEffect(() => {
    if (!enabled) return;
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
        // change (retryToken/role switch, client rebuild after slug change) can
        // retry the bootstrap fetch. Without this, a transient 5xx on first
        // load permanently disabled re-hydration and pinned X-UMS-Tenant.
        hasRequestedTenantRef.current = false;
        setTenantError(error as ApiError | Error);
      });
  }, [client, tenant.id, tenant.hydrate, enabled, retryToken]);

  return { proofLabel: tenantProofLabel(tenant, tenantError) };
}

/**
 * Extract the trimmed `detail` string from a typed ApiError JSON body, or null
 * when the body has no usable detail message.
 */
function apiErrorDetail(error: ApiError | Error | null): string | null { // skipcq: JS-0067, JS-R1005
  if (
    error instanceof ApiError &&
    typeof error.body === "object" &&
    error.body !== null &&
    typeof (error.body as { detail?: unknown }).detail === "string" &&
    (error.body as { detail: string }).detail.trim().length > 0
  ) {
    return (error.body as { detail: string }).detail;
  }
  return null;
}

/**
 * Build the dev-only tenant proof label from the hydrated tenant context and
 * any bootstrap error, covering the loading, success, and failure states.
 */
function tenantProofLabel( // skipcq: JS-0067
  tenant: ReturnType<typeof useTenant>,
  tenantError: ApiError | Error | null,
): string {
  // Pre-hydration the slug is intentionally empty — show a sentinel rather
  // than a stray space so the dev proof tag stays readable.
  const displaySlug = tenant.tenantSlug || "(resolving…)";
  if (tenantError) {
    const detail = apiErrorDetail(tenantError);
    return `Tenant: ${displaySlug}; /tenants/me failed: ${tenantError.message}${
      detail ? ` — ${detail}` : ""
    }`;
  }
  if (tenant.id) {
    return `Tenant: ${tenant.displayName} (${tenant.tenantSlug}) — id ${tenant.id}`;
  }
  return `Tenant: ${displaySlug} (loading…)`;
}

/* ------------------------------------------------------------------ shell */

// ============================================================================
// Purpose: Top-level SRCC shell. Hydrates the authenticated session from
//          /session/me, then renders: a loading state while the bootstrap runs;
//          the fail-closed <AccessDeniedState/> when hydration failed (401/403/
//          network) OR the principal is disabled; otherwise the dashboard gated
//          by the backend-DERIVED session.capabilities. The dev-only role
//          selector drives the DISPLAYED label only — every capability gate
//          comes from the session, never from the role string, so dev preview
//          can never fabricate a capability the backend did not grant.
// Database/ORM: None (frontend).
// Standards: Hooks are called unconditionally before any early return. Fail
//            closed: loading -> loading, error/disabled -> access denied.
// Blast Radius: Authorization (UI gating). No graph projection impact detected.
// Connections:
//   - File: frontend/src/contexts/SessionContext.tsx -> useSessionBootstrap.
//   - File: backend/ums_smart_revenue/api/session.py -> GET /session/me.
// ============================================================================
export default function AppShell() { // skipcq: JS-0067, JS-R1005
  const [view, setView] = useState<ViewKey>("command");
  const [previewRole, setPreviewRole] = useState<Role>(DEFAULT_PREVIEW_ROLE);

  const sessionBootstrap = useSessionBootstrap();
  // The tenant bootstrap only runs once the authenticated session is ready, so
  // a loading or access-denied shell never issues a /tenants/me fetch.
  const sessionReady = sessionBootstrap.status === "ready";
  // previewRole is passed as the retry token: a dev role switch after a failed
  // tenant bootstrap re-fires it (dev-only; it does not affect capabilities).
  const { proofLabel } = useTenantBootstrap(sessionReady, previewRole);

  if (sessionBootstrap.status === "loading") {
    return <SessionLoadingState />;
  }

  // Fail closed: a failed hydration (401/403/network) OR a disabled principal
  // renders the access-denied screen instead of any gated dashboard.
  if (
    sessionBootstrap.status === "error" ||
    sessionBootstrap.session === null ||
    sessionBootstrap.session.disabled
  ) {
    return <AccessDeniedState disabled={sessionBootstrap.session?.disabled ?? false} />;
  }

  // Capabilities are AUTHORITATIVE from the session. The dev role selector only
  // changes the displayed label; permissions are derived from capabilities only.
  const displayedRole = previewRole;
  const permissions = capabilitiesToPermissions(
    displayedRole,
    sessionBootstrap.session.capabilities,
  );
  const canViewFinance = permissions.canViewFinance;
  const copy = VIEW_COPY[view];

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
        {view === "command" && <WorkflowRail />}
      </main>
    </div>
  );
}

/** Dev-only fixed-position tag that proves which tenant the shell resolved. */
function TenantProofTag({ label }: { label: string }) { // skipcq: JS-0067
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
function Sidebar({ // skipcq: JS-0067
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
function NavSection({ // skipcq: JS-0067
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
function RoleCard({ // skipcq: JS-0067
  previewRole,
  onSelectPreviewRole,
  displayedRole,
  canViewFinance,
}: {
  previewRole: Role;
  onSelectPreviewRole: (role: Role) => void;
  displayedRole: Role;
  canViewFinance: boolean;
}) {
  return (
    <div className="role-card">
      <label htmlFor="roleSelect">Current role</label>
      {canPreviewRoles() ? (
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
}

/**
 * Presentation-only disclaimer rendered beneath the dev role switcher. The
 * switcher only changes the UI's permission MODELLING; every API request still
 * carries the fixed dev-proxy identity (VITE_DEV_GATEWAY_ROLE, injected
 * server-side at proxy start), so backend authorization does not change when the
 * preview role does. The hint makes that explicit so a demo viewer does not read
 * the switcher as a real privilege change.
 */
function RolePreviewHint() { // skipcq: JS-0067
  return (
    <small className="role-preview-hint" data-testid="role-preview-hint">
      Presentation preview only — API permissions come from the dev gateway role.
    </small>
  );
}

/* ------------------------------------------------------------------ topbar */

/** Page header: title, operational cues, and the report filter / export controls. */
function Topbar({ // skipcq: JS-0067
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
function OperationalCues({ canViewFinance }: { canViewFinance: boolean }) { // skipcq: JS-0067
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
function ViewRouter({ // skipcq: JS-0067, JS-R1005
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
  return (
    <>
      {view === "command" && <CommandView canViewFinance={canViewFinance} />}
      {view === "registry" && <RegistryView permissions={permissions} />}
      {view === "close" && <CloseView permissions={permissions} />}
      {view === "trace" && (
        <TraceView canViewFinance={canViewFinance} role={displayedRole} />
      )}
      {view === "exports" && (
        <ExportsView
          canCreateExport={canCreateAnyExport(permissions)}
          canExportFinance={permissions.canExportFinanceReports}
          canExportAnalytics={permissions.canExportAnalyticsReports}
        />
      )}
      {view === "connectors" && (
        <ConnectorsView
          canRunConnectors={permissions.canRunConnectors}
          canViewFinance={permissions.canViewFinance}
        />
      )}
      {view === "audit" && <AuditView permissions={permissions} />}
    </>
  );
}

/* ------------------------------------------------------------------ command */

// CommandView is the first REAL-data view; it lives in ./views/CommandView.tsx
// and is wired to GET /revenue/months/{month}/net-revenue via useNetRevenue.

/** Month-close workflow rail shown beneath the Command view. */
function WorkflowRail() { // skipcq: JS-0067
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
function RegistryView({ permissions }: { permissions: AccessPermissions }) { // skipcq: JS-0067
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
function RegistryMainPanel({ canManageRegistry }: { canManageRegistry: boolean }) { // skipcq: JS-0067
  return (
    <section className="panel">
      <RegistryPanelHeader canManageRegistry={canManageRegistry} />
      <RegistryMappingBand canManageRegistry={canManageRegistry} />
      <RegistryTable canManageRegistry={canManageRegistry} />
    </section>
  );
}

/** Registry panel header: title/subtitle and the bulk-import / mapping-change actions. */
function RegistryPanelHeader({ canManageRegistry }: { canManageRegistry: boolean }) { // skipcq: JS-0067
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

/** The channel registry table column header row. Extracted to keep nesting shallow. */
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

/** Channel registry data table; trace keys are withheld from non-registry roles. */
function RegistryTable({ canManageRegistry }: { canManageRegistry: boolean }) { // skipcq: JS-0067
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
function RegistryRow({ // skipcq: JS-0067
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
 * The mapping-change request panel: an audited form (channel, company, reason,
 * effective month) plus save/submit actions. Extracted with a shallow form so
 * the registry side-panel JSX tree stays within the nesting limit.
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
        <button className="ghost-button" type="button" disabled={!canManageRegistry}>Save Draft</button>
        <button className="primary-button" type="button" disabled={!canManageRegistry}>Submit Approval</button>
      </div>
    </section>
  );
}

/** A labelled mapping-form select row that owns its own options. Keeps the form tree shallow. */
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

/** A labelled mapping-form text input row. Keeps the form tree shallow. */
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
function AuditView({ permissions }: { permissions: AccessPermissions }) { // skipcq: JS-0067
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
function AuditLogPanel({ canViewAudit }: { canViewAudit: boolean }) { // skipcq: JS-0067
  return (
    <section className="panel">
      <AuditLogPanelHeader canViewAudit={canViewAudit} />
      <AuditTimeline canViewAudit={canViewAudit} />
    </section>
  );
}

/** Audit-log panel header: title/subtitle and the severity filter + download actions. */
function AuditLogPanelHeader({ canViewAudit }: { canViewAudit: boolean }) { // skipcq: JS-0067
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
}

/** Audit event timeline; non-audit roles see a single restricted placeholder row. */
function AuditTimeline({ canViewAudit }: { canViewAudit: boolean }) { // skipcq: JS-0067
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
function AuditTimelineItem({ event }: { event: (typeof AUDIT_EVENTS)[number] }) { // skipcq: JS-0067
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
