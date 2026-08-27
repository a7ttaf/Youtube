import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type {
  ScopedFinanceViewHint,
  SessionCapabilities,
  SessionMe,
  TenantRead,
} from "@/lib/api/types";
import {
  useSessionBootstrap,
  type SessionBootstrap,
} from "@/contexts/SessionContext";
import { useTenant } from "@/contexts/TenantContext";
import {
  WriteInFlightProvider,
  useWriteInFlightLatch,
} from "@/contexts/WriteInFlightContext";
import {
  NAV_GROUPS,
  VIEW_COPY,
  WORKFLOW_STEPS,
} from "@/lib/mock/data";
import type { Role, ViewKey } from "@/lib/mock/data";
import ErrorBoundary from "./ErrorBoundary";
import { BrandIcon, NAV_ICONS, RefreshIcon } from "./icons";
import AuditView from "./views/AuditView";
import CloseView from "./views/CloseView";
import CommandView from "./views/CommandView";
import { ConnectorsView } from "./views/ConnectorsView";
import ExportsView from "./views/ExportsView";
import { GroupsView } from "./views/GroupsView";
import RegistryView from "./views/RegistryView";
import { importScopeFor } from "@/contexts/UnsettledImportContext";
import TraceView from "./views/TraceView";
import {
  Badge,
  Dot,
  RESTRICTED_FINANCE_VALUE,
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
  canViewRevenue: boolean;
  // Global-scope-only revenue gate for surfaces whose backend boundary is
  // VIEW_REVENUE @ global (the composed gap-explanation read).
  canViewRevenueGlobal: boolean;
  canViewConfidence: boolean;
  canViewPayments: boolean;
  canViewBankReconciliation: boolean;
  // Month-resolution grant hints for the two finance views above.
  paymentsViewScopes: ScopedFinanceViewHint;
  bankReconciliationViewScopes: ScopedFinanceViewHint;
  canManageRegistry: boolean;
  canManageGroups: boolean;
  canImportChannels: boolean;
  canManageConnectors: boolean;
  canCloseMonth: boolean;
  canUnlockMonth: boolean;
  canCreateGlobalExports: boolean;
  canCreateScopedExports: boolean;
  canRequestRawExports: boolean;
  canExportFinanceReports: boolean;
  canExportAnalyticsReports: boolean;
  canRunConnectors: boolean;
  canViewAudit: boolean;
  canViewConnectorHealth: boolean;
  canViewAnalytics: boolean;
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
//          is a function (not an eagerly-computed value) so the env is read at
//          render time and tests can flip import.meta.env via vi.stubEnv to
//          exercise the production (no-preview) path.
// Standards: Dev preview is presentation only; capabilities stay backend-derived.
// Blast Radius: Authorization (UI label only — never grants a capability).
// ============================================================================
const canPreviewRoles = (): boolean => {
  return (
    import.meta.env.DEV || import.meta.env.VITE_ENABLE_ROLE_PREVIEW === "true"
  );
};

// ============================================================================
// Purpose: Map the backend-DERIVED session capabilities onto the UI gate shape
//          the wired views already consume. This is the single capabilities ->
//          UI-gates translation: every gate traces to an authoritative session
//          capability, so the UI never grants a surface the backend did not.
// Database/ORM: None (frontend).
// Standards: Capabilities are authoritative — no gate is invented. Finance
//            visibility maps to VIEW_REVENUE (global or scoped org data grant);
//            close to LOCK_FINANCE_MONTH; allocation editing to
//            CHANGE_ALLOCATION_RULE; finance export variants (global/scoped/raw/
//            report) to EXPORT_REVENUE_REPORT; analytics CSV exports to
//            EXPORT_ANALYTICS_REPORT plus VIEW_REVENUE because the CSV includes
//            source-row revenue amounts;
//            audit to VIEW_AUDIT_LOG; connector management UI to MANAGE_CONNECTORS;
//            connector job controls to RUN_CONNECTOR_JOBS (NOT to finance,
//            honoring that a finance admin must not trigger connector jobs).
//            Registry management uses canManageRegistry derived from
//            MANAGE_CHANNELS (corporate_admin and revenue_operations_admin
//            hold registry permissions without VIEW_REVENUE; finance_admin
//            holds VIEW_REVENUE without registry permissions — the two are
//            disjoint).
// Blast Radius: Authorization (UI gating). No graph projection impact detected.
// Connections:
//   - File: backend/ums_smart_revenue/api/session.py -> SessionCapabilities.
//   - File: frontend/src/components/srcc/views/ConnectorsView.tsx -> canRunConnectors,
//     canManageConnectors.
// ============================================================================
const capabilitiesToPermissions = (
  role: Role,
  capabilities: SessionCapabilities,
): AccessPermissions => {
  const canExport = capabilities.canExportRevenue;
  return {
    role,
    canViewFinance: capabilities.canViewRevenue,
    canViewRevenue: capabilities.canViewRevenue,
    canViewRevenueGlobal: capabilities.canViewRevenueGlobal,
    canViewConfidence: capabilities.canViewConfidence,
    canViewPayments: capabilities.canViewPayments,
    canViewBankReconciliation: capabilities.canViewBankReconciliation,
    paymentsViewScopes: capabilities.paymentsViewScopes,
    bankReconciliationViewScopes: capabilities.bankReconciliationViewScopes,
    canManageRegistry: capabilities.canManageRegistry,
    canManageGroups: capabilities.canManageGroups,
    // Import CSV render hint: the backend derives this as MANAGE_CHANNELS AND
    // MANAGE_GROUPS (never either-of) — the import route always needs the
    // former and additionally the latter for Group_ID-bearing rosters, so a
    // channels-only principal never sees a control that would 403 mid-flow.
    canImportChannels: capabilities.canImportChannels,
    canManageConnectors: capabilities.canManageConnectors,
    canCloseMonth: capabilities.canCloseMonth,
    canUnlockMonth: capabilities.canUnlockMonth,
    canCreateGlobalExports: canExport,
    canCreateScopedExports: canExport,
    canRequestRawExports: canExport,
    canExportFinanceReports: canExport,
    canExportAnalyticsReports: capabilities.canExportAnalyticsReports,
    // Honest: connector job/sync controls require RUN_CONNECTOR_JOBS, which a
    // finance admin does not hold — so canViewRevenue must NOT enable them.
    canRunConnectors: capabilities.canRunConnectorJobs,
    canViewAudit: capabilities.canViewAudit,
    // The run-history panel gates on the backend's read permission exactly:
    // /session/me now exposes canViewConnectorHealth (VIEW_CONNECTOR_HEALTH),
    // mirroring the GET /connectors/runs route gate. Narrower principals see the
    // restricted placeholder and fire no fetch; the route 403 stays authoritative.
    canViewConnectorHealth: capabilities.canViewConnectorHealth,
    // The outside-CMS / channel-issues monitor gates on the backend's analytics
    // read exactly: /session/me exposes canViewAnalytics (scope-aware — true for
    // ANY active VIEW_ANALYTICS grant), mirroring the GET /channels/outside-cms +
    // /channels/issues route gates. A narrower principal sees the restricted
    // placeholder and fires no fetch; the route 403 stays authoritative.
    canViewAnalytics: capabilities.canViewAnalytics,
  };
};

/**
 * Report whether the viewer may create any export variant (global, scoped, or
 * raw), used to enable the header Create Export action.
 */
const canCreateAnyExport = (permissions: AccessPermissions) => {
  return (
    permissions.canCreateGlobalExports ||
    permissions.canCreateScopedExports ||
    permissions.canRequestRawExports ||
    // FIX: Analytics summary CSV creation is controlled by the analytics export
    // capability plus revenue visibility, not by legacy finance export flags.
    (permissions.canExportAnalyticsReports && permissions.canViewRevenue)
  );
};

/** Panel header for the access-denied state, kept flat to limit JSX nesting. */
const AccessDeniedHeader = ({ disabled }: { disabled: boolean }) => {
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
};

/**
 * Render the fail-closed fallback panel shown when the session could not be
 * hydrated (401/403/network) or the principal is disabled. The detail copy
 * distinguishes a disabled principal from a failed/absent session.
 */
const AccessDeniedState = ({ disabled = false }: { disabled?: boolean }) => {
  return (
    <div className="app">
      <main className="main" aria-labelledby="accessDeniedTitle">
        <section className="panel">
          <AccessDeniedHeader disabled={disabled} />
        </section>
      </main>
    </div>
  );
};

/** Panel body shown while the one-shot /session/me bootstrap is in flight. */
const SessionLoadingPanelContent = () => {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="sessionLoadingTitle">Loading session…</strong>
        <span>Resolving your authenticated capabilities.</span>
      </div>
      <Badge tone="blue">Loading</Badge>
    </div>
  );
};

/** Render the loading panel shown while the one-shot /session/me bootstrap runs. */
const SessionLoadingState = () => {
  return (
    <div className="app">
      <main className="main" aria-labelledby="sessionLoadingTitle" aria-busy="true">
        <section className="panel">
          <SessionLoadingPanelContent />
        </section>
      </main>
    </div>
  );
};

/* ------------------------------------------------------------------ tenant bootstrap */

type TenantBootstrap = {
  /** The label rendered in the dev-only tenant proof tag. */
  proofLabel: string;
  /** Whether the tenant id has stopped moving — see the return statement. */
  tenantSettled: boolean;
};

/** A parsed error body that carries an operator-facing `detail` message. */
type ApiErrorDetailBody = { detail: string };

/**
 * Predicate for "an object body carrying a non-blank string detail". Written as
 * an `x is` type guard so TypeScript still narrows the body at the call site
 * after the check moved out of apiErrorDetail's own condition.
 */
const hasDetailMessage = (body: unknown): body is ApiErrorDetailBody =>
  typeof body === "object" &&
  body !== null &&
  typeof (body as { detail?: unknown }).detail === "string" &&
  (body as ApiErrorDetailBody).detail.trim().length > 0;

/**
 * Extract the trimmed `detail` string from a typed ApiError JSON body, or null
 * when the body has no usable detail message.
 */
const apiErrorDetail = (error: ApiError | Error | null): string | null => {
  if (!(error instanceof ApiError)) return null;
  // Bound to a local so the type guard's narrowing applies to the value read.
  const body: unknown = error.body;
  return hasDetailMessage(body) ? body.detail : null;
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
    return `Tenant: ${displaySlug}; /tenants/me failed: ${tenantError.message}${
      detail ? ` — ${detail}` : ""
    }`;
  }
  if (tenant.id) {
    return `Tenant: ${tenant.displayName} (${tenant.tenantSlug}) — id ${tenant.id}`;
  }
  return `Tenant: ${displaySlug} (loading…)`;
};

// ============================================================================
// Purpose: Fire /tenants/me once on mount; hydrate TenantContext on success or
//          surface the typed ApiError message in the dev-only proof tag on
//          failure. Returns the proof label AND `tenantSettled` — whether the
//          tenant id is KNOWN — which is no longer presentational: it decides
//          whether an audited bulk import may be dispatched at all.
// Database/ORM: None (frontend API call only).
// Standards: useRef re-entry guard keeps fetch count at 1 under React StrictMode.
//            Effect is gated on `enabled` (the session is ready) so a shell that
//            renders the loading or fail-closed <AccessDeniedState/> never issues
//            a tenant bootstrap fetch. The guard is reset in the failure path so
//            a subsequent dep change (provider rebuild) can retry — a transient
//            5xx must not permanently pin tenantSlug to the bootstrap value.
//            `tenantSettled` is `tenant.id !== null` and NOTHING else. A failure
//            does not settle it: two tabs disagreeing about the tenant build
//            different key prefixes and different Web Lock names, so neither can
//            see the other's pending import and both dispatch (review #184).
//            Because the failure path RETRIES, "failed" is not terminal — which
//            is exactly why it must not be read as settled.
// Blast Radius: Beyond the dev proof tag: `tenantSettled` gates dispatch of an
//            audited bulk import and decides whether cross-tab duplicate
//            protection is namespaced by a stable tenant. A wrong value here
//            shows as a refused import or a duplicated one plus a duplicated
//            CHANNEL_IMPORTED audit event — never as an unpermitted write, since
//            no authorization is derived from it. Still no financial mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET helper.
//   - File: frontend/src/contexts/TenantContext.tsx -> hydrate() stores id/displayName.
//   - File: frontend/src/components/srcc/AppShell.tsx -> isImportScopeSettled,
//       the admission gate that consumes tenantSettled (contract just below).
// ============================================================================
/**
 * Bootstrap the active tenant from /tenants/me, hydrating TenantContext and
 * exposing the dev-only proof label that reflects success or the typed error.
 * Gated on `enabled` so it only fires once the authenticated session is ready;
 * `retryToken` is an opaque dependency (the dev preview role) that, when it
 * changes after a failure, re-fires the bootstrap so a transient 5xx is not
 * permanent.
 */
const useTenantBootstrap = (
  enabled: boolean,
  retryToken: string,
): TenantBootstrap => {
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

  return {
    proofLabel: tenantProofLabel(tenant, tenantError),
    // SETTLED means the tenant is KNOWN, so anything namespaced by it lands
    // where every other document for this operator will look. Nothing else
    // counts — not a failure, and not a failure that will never be retried.
    //
    // This deliberately REVERSES an earlier reading of mine, which treated a
    // failed resolution as settled on the grounds that a tenant which cannot
    // resolve would otherwise withhold imports forever. That reasoning missed
    // the case that matters: with /tenants/me failing in one tab and
    // succeeding in another for the SAME operator, the two tabs build
    // different scopes — different key prefixes AND different Web Lock names —
    // so neither can see the other's pending apply and both dispatch the same
    // roster, duplicating an audited write and its CHANNEL_IMPORTED event.
    // Scope adoption cannot undo that: the request is already gone (review
    // #184, codex P2).
    //
    // The lockout it trades against is far milder than I first judged: a
    // reload re-runs the bootstrap, so the recovery path is a page refresh,
    // and IMPORT_SCOPE_UNSETTLED_NOTE says so. Blocking an import until the
    // workspace is known is the fail-closed direction; admitting one into a
    // namespace nobody else shares is not.
    tenantSettled: tenant.id !== null,
  };
};

/* ------------------------------------------------------------------ sidebar */

/**
 * Render a single labelled navigation group with its selectable items.
 *
 * `blockedReason` is non-null while a flow below the shell holds an
 * unabortable write in flight. Navigating then would unmount that flow
 * without stopping its request: the write still commits, but its completion
 * handler can no longer reload anything or tell the operator it landed. The
 * reason doubles as each button's title, so a disabled nav always says why.
 */
const NavSection = ({
  group,
  view,
  onSelectView,
  blockedReason,
}: {
  group: (typeof NAV_GROUPS)[number];
  view: ViewKey;
  onSelectView: (key: ViewKey) => void;
  blockedReason: string | null;
}) => {
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
            disabled={blockedReason !== null}
            title={blockedReason ?? undefined}
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
};

/**
 * Presentation-only disclaimer rendered beneath the dev role switcher. The
 * switcher only changes the UI's permission MODELLING; every API request still
 * carries the fixed dev-proxy identity (VITE_DEV_GATEWAY_ROLE, injected
 * server-side at proxy start), so backend authorization does not change when the
 * preview role does. The hint makes that explicit so a demo viewer does not read
 * the switcher as a real privilege change.
 */
const RolePreviewHint = () => {
  return (
    <small className="role-preview-hint" data-testid="role-preview-hint">
      Presentation preview only — API permissions come from the dev gateway role.
    </small>
  );
};

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
};

/** Primary navigation sidebar: brand mark, nav groups, and the role card. */
const Sidebar = ({
  view,
  onSelectView,
  previewRole,
  onSelectPreviewRole,
  displayedRole,
  canViewFinance,
  blockedReason,
}: {
  view: ViewKey;
  onSelectView: (key: ViewKey) => void;
  previewRole: Role;
  onSelectPreviewRole: (role: Role) => void;
  displayedRole: Role;
  canViewFinance: boolean;
  blockedReason: string | null;
}) => {
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
          blockedReason={blockedReason}
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
};

/* ------------------------------------------------------------------ topbar */

/**
 * Operational status cues for the top bar (source, bank gap, export blockers,
 * trace). Extracted so the Topbar JSX tree stays shallow; the bank-gap value is
 * gated behind canViewFinance so non-finance roles see the restricted sentinel.
 */
const OperationalCues = ({ canViewFinance }: { canViewFinance: boolean }) => {
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
};

/** Page header: title, operational cues, and the report filter / export controls. */
const Topbar = ({
  title,
  subtitle,
  canViewFinance,
  canCreateExport,
}: {
  title: string;
  subtitle: string;
  canViewFinance: boolean;
  canCreateExport: boolean;
}) => {
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
};

/* ------------------------------------------------------------------ view router */

type ViewRouterProps = {
  view: ViewKey;
  permissions: AccessPermissions;
  canViewFinance: boolean;
  displayedRole: Role;
  traceChannelId: string | null;
  /** Namespaces the unsettled-import records to one tenant + principal. */
  importScope: string;
  /** False while that namespace can still change under a write. */
  importScopeSettled: boolean;
  onOpenTrace: (channelId: string) => void;
};

/**
 * Route the active view key to its wired or mock view with the right props. A
 * ViewKey-keyed render map replaces the per-view conditional chain: the Record
 * is exhaustive by type (adding a ViewKey without a renderer is a type error),
 * and only the active key's renderer is invoked — one mounted view, exactly as
 * the chain produced.
 */
const ViewRouter = ({
  view,
  permissions,
  canViewFinance,
  displayedRole,
  traceChannelId,
  importScope,
  importScopeSettled,
  onOpenTrace,
}: ViewRouterProps) => {
  const renderView: Record<ViewKey, () => ReactNode> = {
    command: () => (
      <CommandView
        canViewFinance={canViewFinance}
        canViewAnalytics={permissions.canViewAnalytics}
        canViewPayments={permissions.canViewPayments}
        canViewBankReconciliation={permissions.canViewBankReconciliation}
        canViewRevenueGlobal={permissions.canViewRevenueGlobal}
        canViewConfidence={permissions.canViewConfidence}
        paymentsViewScopes={permissions.paymentsViewScopes}
        bankReconciliationViewScopes={permissions.bankReconciliationViewScopes}
      />
    ),
    registry: () => (
      <RegistryView
        canManageRegistry={permissions.canManageRegistry}
        canImportChannels={permissions.canImportChannels}
        canViewFinance={permissions.canViewFinance}
        canViewAudit={permissions.canViewAudit}
        importScope={importScope}
        importScopeSettled={importScopeSettled}
        onOpenTrace={onOpenTrace}
      />
    ),
    groups: () => <GroupsView canManageGroups={permissions.canManageGroups} />,
    close: () => <CloseView permissions={permissions} />,
    trace: () => (
      <TraceView
        canViewFinance={canViewFinance}
        role={displayedRole}
        presetChannelId={traceChannelId ?? undefined}
      />
    ),
    exports: () => (
      <ExportsView
        canCreateExport={canCreateAnyExport(permissions)}
        canExportFinance={permissions.canExportFinanceReports}
        canExportAnalytics={permissions.canExportAnalyticsReports}
        canViewRevenue={permissions.canViewRevenue}
      />
    ),
    connectors: () => (
      <ConnectorsView
        canRunConnectors={permissions.canRunConnectors}
        canManageConnectors={permissions.canManageConnectors}
        canViewFinance={permissions.canViewFinance}
        canViewConnectorHealth={permissions.canViewConnectorHealth}
      />
    ),
    audit: () => (
      <AuditView
        canViewAudit={permissions.canViewAudit}
        canViewFinance={permissions.canViewFinance}
      />
    ),
  };

  return <>{renderView[view]()}</>;
};

/* ------------------------------------------------------------------ command */

// CommandView is the first REAL-data view; it lives in ./views/CommandView.tsx
// and is wired to GET /revenue/months/{month}/net-revenue via useNetRevenue.

/** Month-close workflow rail shown beneath the Command view. */
const WorkflowRail = () => {
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
};

/* ------------------------------------------------------------------ registry */

// RegistryView is the wired Channel Registry screen; it lives in
// ./views/RegistryView.tsx and reads GET /channels via useChannels. Client-side
// derivation maps the API fields to avatar initials, CMS badge tone, source
// label, state (Option A: from existing fields), trace key, and action label.
// Company/sector columns show primary_company_id and "—" until GET /org-units
// is added. Summary tiles derive active-channel + outside-CMS counts from the
// response; finance tiles stay static placeholders.



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
// sources) + GET /connectors/runs (run history) + GET /adsense/payments
// (synced payments) and POSTs /connectors/jobs (request sync) +
// /adsense/sync-payments via the useConnectors / useAdsense hooks.

/* ------------------------------------------------------------------ audit */

// AuditView is the wired Audit Log screen; it lives in ./views/AuditView.tsx and
// reads GET /audit/events via useAuditEvents (cursor-paginated, server-driven
// redaction). The timeline gate is canViewAudit (restricted -> no fetch); the
// summary tiles + coverage panel stay static context (no aggregate-count route).

/* ------------------------------------------------------------------ shell */

/**
 * A bootstrap whose session hydrated to a usable, NON-disabled principal. The
 * intersection is what keeps `session` non-null for the gated dashboard below.
 */
type ActiveSessionBootstrap = SessionBootstrap & { session: SessionMe };

// ============================================================================
// Purpose: Fail-closed session gate, evaluated in the SAME left-to-right order
//   the shell body used inline: a failed hydration (401/403/network) is
//   rejected first, then an absent session, then a disabled principal. Written
//   as an `x is` type guard so the narrowed non-null session survives the
//   early return into the gated dashboard.
// Database/ORM: None (frontend).
// Standards: Read-only authorization predicate; no permission is granted here —
//   the underlying routes re-check every capability server-side. Fail-closed:
//   the "error" and null-session branches both narrow to never (no dashboard).
// Blast Radius: Authorization render surface only; no mutation, no network.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx (AppShell body) -> the
//     single consumer; returns AccessDeniedState when this predicate is false.
// ============================================================================
const hasActiveSession = (
  bootstrap: SessionBootstrap,
): bootstrap is ActiveSessionBootstrap =>
  bootstrap.status !== "error" &&
  bootstrap.session !== null &&
  !bootstrap.session.disabled;

// ============================================================================
// Purpose: Whether the denied panel reads "account disabled" rather than "no
//   session": true only when a session hydrated AND carries disabled=true. Kept
//   distinct from hasActiveSession so the denied copy is chosen without
//   re-reading the session fields.
// Database/ORM: None (frontend).
// Standards: Read-only predicate over the already-hydrated session; no authz
//   decision of its own.
// Blast Radius: UI copy selection only.
// Connections:
//   - File: frontend/src/components/srcc/AppShell.tsx (AppShell body) -> passes
//     its result to AccessDeniedState's `disabled` prop.
// ============================================================================
const isDisabledPrincipal = (bootstrap: SessionBootstrap): boolean =>
  bootstrap.session?.disabled ?? false;

/** Dev-only fixed-position tag that proves which tenant the shell resolved. */
const TenantProofTag = ({ label }: { label: string }) => {
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
};

// ============================================================================
// Purpose: Choose the tenant identity that NAMES this operator's
//   unsettled-import namespace — the session's own tenant when it carries one,
//   otherwise the tenant resolved asynchronously from /tenants/me.
// Database/ORM: None (frontend) — a pure choice between two identity sources.
//   What it namespaces is localStorage, and through it which pending audited
//   import the duplicate-write guard can see.
// Standards: The tenant **ID**, never the slug. A slug is routing metadata and
//   can be renamed, which would mint a fresh scope while an apply is still
//   unsettled — hiding the warning and handing admission an empty bucket for a
//   request whose outcome nobody knows. `SessionMe.tenant` is nullable, so the
//   resolved tenant is the fallback rather than an equal alternative: without
//   it every tenant for one operator collapses into a single bucket and one
//   acknowledgement retires them all (review #184). Preferring the session
//   value keeps the scope STABLE across the bootstrap, which is what
//   isImportScopeSettled below depends on — the two are one decision split in
//   half, and changing the preference here silently changes what that gate
//   admits.
// Blast Radius: Cross-tenant isolation of the duplicate-import guard. Picking
//   the wrong source can hide a pending import from the operator who owns it,
//   or let one operator acknowledge another namespace's record away — and an
//   acknowledgement is what re-enables dispatch. No authorization meaning: the
//   backend's permission and tenant scoping are unaffected either way.
// Connections:
//   - File: frontend/src/contexts/UnsettledImportContext.tsx -> importScopeFor
//     encodes this pair into the storage key prefix.
//   - File: frontend/src/contexts/TenantContext.tsx -> supplies the resolved
//     tenant used when the session body carries none.
// ============================================================================
// Extracted from AppShell so the shell stays under the analyzer's complexity
// threshold (DeepSource JS-R1005) — conformed, not suppressed.
const resolveImportScope = (
  session: SessionMe,
  resolvedTenant: { id: string | null },
): string => {
  return importScopeFor(session.tenant?.id ?? resolvedTenant.id, session.user_id);
};

// ============================================================================
// Purpose: The ADMISSION PRECONDITION for an audited bulk import — whether the
//   namespace that isolates pending-write records is FINAL. The session's own
//   tenant is authoritative and needs no bootstrap, so a session carrying one
//   is settled immediately; a session WITHOUT one waits for /tenants/me to
//   SUCCEED.
// Database/ORM: None (frontend) — a pure predicate over two identity sources.
//   It issues no request and decides no permission; what it gates is whether
//   RegistryImportFlow may reach POST /channels/import at all, and therefore
//   whether a CHANNEL_IMPORTED audit event can be appended.
// Standards: A FAILURE does not settle it, and that is the load-bearing part.
//   Treating a failure as settled — which this did until review #184 — lets
//   two tabs for the same operator disagree about the tenant: one builds the
//   missing-tenant scope, the other the resolved one. Different key prefixes
//   AND different Web Lock names, so neither can see the other's pending apply
//   and both dispatch the same roster, duplicating an audited write. Scope
//   adoption cannot undo that, because the request has already left.
//   Fails CLOSED, and recoverably: a reload re-runs the bootstrap, so
//   withholding is a retry rather than a wedge, and
//   IMPORT_SCOPE_UNSETTLED_NOTE tells the operator exactly that.
//   Kept beside resolveImportScope on purpose — the two read the same two
//   sources and must agree about which one supplied the tenant, or the guard
//   would be settled against one scope and the record written under another.
//   Not an authorization input: the backend's permission checks, its own
//   tenant scoping, and the plan fingerprint remain the authority regardless
//   of what this returns.
// Blast Radius: Whether a duplicate audited bulk import can be dispatched
//   while tenant identity is unresolved. No revenue math; a mistake here shows
//   as a refused import or a duplicated one, never as an unpermitted one.
// Connections:
//   - File: frontend/src/contexts/UnsettledImportContext.tsx -> importScopeFor
//       builds the namespace this decides the finality of, and admit() is what
//       it gates.
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       receives this as importScopeSettled and refuses Apply while false.
//   - File: frontend/src/contexts/TenantContext.tsx -> the hydration that
//       flips it, via useTenantBootstrap's tenantSettled just above.
// ============================================================================
export const isImportScopeSettled = (session: SessionMe, tenantSettled: boolean): boolean => {
  return session.tenant?.id != null || tenantSettled;
};

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
const AppShell = () => {
  const [view, setView] = useState<ViewKey>("command");
  const [previewRole, setPreviewRole] = useState<Role>(DEFAULT_PREVIEW_ROLE);
  // Registry "Review" navigation target: seeds TraceView's initial channel
  // selection. Navigation-only state — carries no authorization meaning.
  const [traceChannelId, setTraceChannelId] = useState<string | null>(null);

  const sessionBootstrap = useSessionBootstrap();
  // The RESOLVED tenant, for the import scope below. SessionMe.tenant is
  // nullable, and /tenants/me may have resolved the tenant even when the
  // session body carries none — falling back to it keeps two tenants for the
  // same operator in separate buckets instead of collapsing them (review
  // #184).
  const resolvedTenant = useTenant();
  // The tenant bootstrap only runs once the authenticated session is ready, so
  // a loading or access-denied shell never issues a /tenants/me fetch.
  const sessionReady = sessionBootstrap.status === "ready";
  // previewRole is passed as the retry token: a dev role switch after a failed
  // tenant bootstrap re-fires it (dev-only; it does not affect capabilities).
  const { proofLabel, tenantSettled } = useTenantBootstrap(sessionReady, previewRole);

  // FIX: Clear the Registry→Trace navigation seed when leaving the trace view
  // so that a later manual click on the Trace nav item opens a blank view
  // instead of pre-selecting the last "Review" channel.
  // Navigating away from a flow holding an unabortable write in flight would
  // unmount it without stopping the request: the write still commits, but its
  // completion handler can no longer reload the view or tell the operator it
  // landed (review #184). NavSection disables its buttons off the same latch;
  // this guard is the second half, so a keyboard or programmatic caller cannot
  // route around the disabled control.
  const writeInFlight = useWriteInFlightLatch();
  const navBlockedReason = writeInFlight.reason;

  const handleViewChange = useCallback(
    (next: ViewKey) => {
      if (navBlockedReason !== null) return;
      if (next !== "trace") setTraceChannelId(null);
      setView(next);
    },
    [navBlockedReason, setView, setTraceChannelId],
  );

  if (sessionBootstrap.status === "loading") {
    return <SessionLoadingState />;
  }

  // Fail closed: a failed hydration (401/403/network) OR an absent session OR a
  // disabled principal renders the access-denied screen instead of any gated
  // dashboard. hasActiveSession holds exactly that order (defined just above);
  // isDisabledPrincipal picks the denied copy without re-reading the session.
  if (!hasActiveSession(sessionBootstrap)) {
    return <AccessDeniedState disabled={isDisabledPrincipal(sessionBootstrap)} />;
  }

  // Capabilities are AUTHORITATIVE from the session. The dev role selector only
  // changes the displayed label; permissions are derived from capabilities only.
  const displayedRole = previewRole;
  const permissions = capabilitiesToPermissions(
    displayedRole,
    sessionBootstrap.session.capabilities,
  );
  const canViewFinance = permissions.canViewFinance;
  // Namespaces the unsettled-import records. localStorage is origin-wide and
  // outlives sign-out, so without this a pending import follows a shared
  // browser into the next operator's or the next tenant's session.
  const importScope = resolveImportScope(sessionBootstrap.session, resolvedTenant);
  // Withhold import ADMISSION until that namespace is final. Without this the
  // shell renders while /tenants/me is still in flight, and an apply admitted
  // in that window files its pending record under the missing-tenant scope
  // that the very next render replaces (review #184).
  const importScopeSettled = isImportScopeSettled(sessionBootstrap.session, tenantSettled);
  const copy = VIEW_COPY[view];


  return (
    <WriteInFlightProvider value={writeInFlight}>
    <div className="app">
      {import.meta.env.DEV && <TenantProofTag label={proofLabel} />}
      <Sidebar
        view={view}
        onSelectView={handleViewChange}
        previewRole={previewRole}
        onSelectPreviewRole={setPreviewRole}
        displayedRole={displayedRole}
        canViewFinance={canViewFinance}
        blockedReason={navBlockedReason}
      />
      <main className="main">
        <Topbar
          title={copy.title}
          subtitle={copy.subtitle}
          canViewFinance={canViewFinance}
          canCreateExport={canCreateAnyExport(permissions)}
        />
        {/* Keyed by the active view so navigating away clears a caught error. */}
        <ErrorBoundary key={view}>
        <ViewRouter
          view={view}
          permissions={permissions}
          canViewFinance={canViewFinance}
          displayedRole={displayedRole}
          traceChannelId={traceChannelId}
          importScope={importScope}
          importScopeSettled={importScopeSettled}
          onOpenTrace={(channelId) => {
            setTraceChannelId(channelId);
            setView("trace");
          }}
        />
        </ErrorBoundary>
        {view === "command" && <WorkflowRail />}
      </main>
    </div>
    </WriteInFlightProvider>
  );
};

export default AppShell;
