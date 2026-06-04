import { useEffect, useRef, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type { TenantRead } from "@/lib/api/types";
import { useTenant } from "@/contexts/TenantContext";
import {
  AUDIT_EVENTS,
  AUDIT_SUMMARY,
  CLOSE_CHECKPOINTS,
  CLOSE_DETAILS,
  CLOSE_SUMMARY,
  CONNECTORS_SUMMARY,
  CONNECTOR_HEALTH,
  CONNECTOR_JOBS,
  CREDENTIAL_CONTROLS,
  EXPORTS_GUARDRAILS,
  EXPORTS_ROWS,
  EXPORTS_SUMMARY,
  EXPORT_META,
  NAV_GROUPS,
  RECON_NOTES,
  REGISTRY_CONTROLS,
  REGISTRY_ROWS,
  REGISTRY_SUMMARY,
  TRACE_EVENTS,
  TRACE_LINES_LARGE,
  TRACE_NODES_LARGE,
  TRACE_SUMMARY,
  VIEW_COPY,
  WORKFLOW_STEPS,
} from "@/lib/mock/data";
import type { Role, ViewKey } from "@/lib/mock/data";
import { BrandIcon, NAV_ICONS, RefreshIcon } from "./icons";
import CommandView from "./views/CommandView";
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
  canRunConnectors: boolean;
  canViewAudit: boolean;
};

type ExportScope = (typeof EXPORTS_ROWS)[number]["scope"];

// In production this value is hydrated from the server-authenticated session claim.
const SERVER_AUTHENTICATED_SESSION: AuthenticatedSession = {};

const CAN_PREVIEW_ROLES =
  import.meta.env.DEV || import.meta.env.VITE_ENABLE_ROLE_PREVIEW === "true";
const DEFAULT_PREVIEW_ROLE: Role = "assistant";

function permissionsForRole(role: Role): AccessPermissions {
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
    canRunConnectors: finance,
    canViewAudit: finance,
  };
}

function canCreateAnyExport(permissions: AccessPermissions) {
  return (
    permissions.canCreateGlobalExports ||
    permissions.canCreateScopedExports ||
    permissions.canRequestRawExports
  );
}

function canCreateExportScope(permissions: AccessPermissions, scope: ExportScope) {
  if (scope === "global") return permissions.canCreateGlobalExports;
  if (scope === "scoped") return permissions.canCreateScopedExports;
  return permissions.canRequestRawExports;
}

function AccessDeniedState() {
  return (
    <div className="app">
      <main className="main" aria-labelledby="accessDeniedTitle">
        <section className="panel">
          <div className="panel-header">
            <div className="panel-title">
              <strong id="accessDeniedTitle">Access denied</strong>
              <span>Authenticated session role is required.</span>
            </div>
            <Badge tone="red">No session role</Badge>
          </div>
        </section>
      </main>
    </div>
  );
}

/* ------------------------------------------------------------------ shell */

export default function AppShell() {
  const [view, setView] = useState<ViewKey>("command");
  const authenticatedRole = SERVER_AUTHENTICATED_SESSION.role;
  const [previewRole, setPreviewRole] = useState<Role>(
    authenticatedRole ?? DEFAULT_PREVIEW_ROLE,
  );
  const [traceTab, setTraceTab] = useState<"Revenue Flow" | "Issues" | "Ownership">(
    "Revenue Flow",
  );

  // ============================================================================
  // Purpose: Fire /tenants/me once on mount; hydrate TenantContext on success or
  //          render the typed ApiError message in the dev-only proof tag on failure.
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
  const tenant = useTenant();
  const client = useApiClient();
  const hasRequestedTenantRef = useRef(false);
  const [tenantError, setTenantError] = useState<ApiError | Error | null>(null);
  const displayedRole = CAN_PREVIEW_ROLES ? previewRole : authenticatedRole;

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
        // this, the proof tag stayed pinned to the failure branch (Line
        // 364) even after the user retried and /tenants/me returned 200.
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

  const tenantErrorDetail =
    tenantError instanceof ApiError &&
    typeof tenantError.body === "object" &&
    tenantError.body !== null &&
    typeof (tenantError.body as { detail?: unknown }).detail === "string" &&
    (tenantError.body as { detail: string }).detail.trim().length > 0
      ? (tenantError.body as { detail: string }).detail
      : null;

  // Pre-hydration the slug is intentionally empty — show a sentinel rather
  // than a stray space so the dev proof tag stays readable.
  const displaySlug = tenant.tenantSlug || "(resolving…)";
  const tenantProofLabel = tenantError
    ? `Tenant: ${displaySlug}; /tenants/me failed: ${tenantError.message}${
        tenantErrorDetail ? ` — ${tenantErrorDetail}` : ""
      }`
    : tenant.id
      ? `Tenant: ${tenant.displayName} (${tenant.tenantSlug}) — id ${tenant.id}`
      : `Tenant: ${displaySlug} (loading…)`;

  if (!displayedRole) {
    return <AccessDeniedState />;
  }

  const permissions = permissionsForRole(displayedRole);
  const canViewFinance = permissions.canViewFinance;
  const copy = VIEW_COPY[view];

  return (
    <div className="app">
      {import.meta.env.DEV && (
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
          {tenantProofLabel}
        </small>
      )}
      {/* ============================================================ sidebar */}
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
          <nav key={group.label} className="nav-section" aria-label={group.label}>
            <div className="nav-title">{group.label}</div>
            {group.items.map((item) => {
              const active = item.key === view;
              return (
                <button
                  key={item.key}
                  type="button"
                  className={`nav-item${active ? " is-active" : ""}`}
                  aria-current={active ? "page" : undefined}
                  onClick={() => setView(item.key)}
                >
                  {NAV_ICONS[item.icon]}
                  <span>{item.label}</span>
                  <span className="nav-count">{item.count}</span>
                </button>
              );
            })}
          </nav>
        ))}

        <div className="role-card">
          <label htmlFor="roleSelect">Current role</label>
          {CAN_PREVIEW_ROLES ? (
            <select
              id="roleSelect"
              value={previewRole}
              onChange={(e) => setPreviewRole(e.target.value as Role)}
            >
              {(Object.entries(ROLE_LABELS) as [Role, string][]).map(
                ([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ),
              )}
            </select>
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
      </aside>

      {/* ============================================================ main */}
      <main className="main">
        {/* top bar */}
        <header className="topbar">
          <div className="page-title">
            <div className="title-row">
              <h1>{copy.title}</h1>
            </div>
            <p>{copy.subtitle}</p>
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
            <button className="primary-button" disabled={!canCreateAnyExport(permissions)}>
              Create Export
            </button>
          </div>
        </header>

        {view === "command" && <CommandView canViewFinance={canViewFinance} />}
        {view === "registry" && <RegistryView permissions={permissions} />}
        {view === "close" && <CloseView permissions={permissions} />}
        {view === "trace" && (
          <TraceView
            traceTab={traceTab}
            setTraceTab={setTraceTab}
            canViewFinance={canViewFinance}
            role={displayedRole}
          />
        )}
        {view === "exports" && <ExportsView permissions={permissions} />}
        {view === "connectors" && <ConnectorsView permissions={permissions} />}
        {view === "audit" && <AuditView permissions={permissions} />}

        {view === "command" && <WorkflowRail />}
      </main>
    </div>
  );
}

/* ------------------------------------------------------------------ command */

// CommandView is the first REAL-data view; it lives in ./views/CommandView.tsx
// and is wired to GET /revenue/months/{month}/net-revenue via useNetRevenue.

function WorkflowRail() {
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

function RegistryView({ permissions }: { permissions: AccessPermissions }) {
  const { canManageRegistry, canViewFinance } = permissions;
  return (
    <section className="view-page" aria-labelledby="registryTitle">
      <div className="view-summary" aria-label="Registry summary">
        {REGISTRY_SUMMARY.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>

      <div className="view-grid wide-side">
        <section className="panel">
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
          <div className="table-wrap">
            <table aria-label="Channel registry">
              <thead>
                <tr>
                  <th>Channel</th><th>Company</th><th>Sector</th><th>CMS</th>
                  <th>Revenue Source</th><th>Trace Key</th><th>State</th><th>Action</th>
                </tr>
              </thead>
              <tbody>
                {REGISTRY_ROWS.map((r) => (
                  <tr key={r.code}>
                    <td>
                      <span className="channel-cell">
                        <span className="avatar">{r.avatar}</span>
                        <span>
                          <span className="channel-name">{r.name}</span>
                          <span className="channel-id">{r.code}</span>
                        </span>
                      </span>
                    </td>
                    <td>{r.company}</td>
                    <td>{r.sector}</td>
                    <td><Badge tone={r.cms.tone}>{r.cms.text}</Badge></td>
                    <td>{r.source}</td>
                    <td>
                      <span className="code-chip">
                        {canManageRegistry ? r.node : RESTRICTED_FINANCE_VALUE}
                      </span>
                    </td>
                    <td><Badge tone={r.state.tone}>{r.state.text}</Badge></td>
                    <td>
                      <button className="mini-button" type="button" disabled={!canManageRegistry}>
                        {r.action}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <aside className="view-stack" aria-label="Registry side panels">
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
                <select id="registryChannel" disabled={!canManageRegistry}><option>Sports Extra</option><option>Music Stage</option></select>
              </div>
              <div className="field-row">
                <label htmlFor="registryCompany">Company</label>
                <select id="registryCompany" disabled={!canManageRegistry}><option>TV Sector</option><option>Catalog Media</option></select>
              </div>
              <div className="field-row">
                <label htmlFor="registryReason">Reason</label>
                <input id="registryReason" defaultValue="March source evidence received" disabled={!canManageRegistry} />
              </div>
              <div className="field-row">
                <label htmlFor="registryEffective">Effective month</label>
                <select id="registryEffective" disabled={!canManageRegistry}><option>Mar 2026</option><option>Apr 2026</option></select>
              </div>
            </div>
            <div className="action-row">
              <button className="ghost-button" type="button" disabled={!canManageRegistry}>Save Draft</button>
              <button className="primary-button" type="button" disabled={!canManageRegistry}>Submit Approval</button>
            </div>
          </section>

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
        </aside>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ close */

function CloseView({ permissions }: { permissions: AccessPermissions }) {
  const { canCloseMonth, canViewFinance } = permissions;
  return (
    <section className="view-page" aria-labelledby="closeViewTitle">
      <div className="view-summary" aria-label="Month close summary">
        {CLOSE_SUMMARY.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>

      <div className="view-grid">
        <section className="panel">
          <div className="panel-header">
            <div className="panel-title">
              <strong id="closeViewTitle">Month Close Workbench</strong>
              <span>Deterministic workflow from report ingestion to locked exports</span>
            </div>
            <Badge tone={canCloseMonth ? "amber" : "red"}>
              {canCloseMonth ? "Finance Admin" : "Restricted"}
            </Badge>
          </div>
          <div className="detail-grid">
            {CLOSE_DETAILS.map((d) => (
              <div key={d.label} className="detail-cell">
                <span>{d.label}</span>
                <strong>{d.value}</strong>
              </div>
            ))}
          </div>
          <div className="table-wrap">
            <table aria-label="Month close checkpoints">
              <thead>
                <tr><th>Checkpoint</th><th>Owner</th><th>Evidence</th><th>Sensitive Action</th><th>State</th><th>Next</th></tr>
              </thead>
              <tbody>
                {CLOSE_CHECKPOINTS.map((c) => (
                  <tr key={c.name}>
                    <td>{c.name}</td>
                    <td>{c.owner}</td>
                    <td><span className="code-chip">{c.evidence}</span></td>
                    <td>{c.action}</td>
                    <td><Badge tone={c.state.tone}>{c.state.text}</Badge></td>
                    <td>
                      <button className="mini-button" type="button" disabled={!canCloseMonth}>
                        {c.next}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <aside className="view-stack">
          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Lock Controls</strong>
                <span>Actions stay disabled until blockers are cleared</span>
              </div>
              <Badge tone={canCloseMonth ? "amber" : "red"}>
                {canCloseMonth ? "Finance Admin" : "Restricted"}
              </Badge>
            </div>
            <div className="form-grid">
              <div className="field-row">
                <label htmlFor="closeMonth">Month</label>
                <select id="closeMonth" disabled={!canCloseMonth}><option>Mar 2026</option><option>Feb 2026</option></select>
              </div>
              <div className="field-row">
                <label htmlFor="closeReason">Reason</label>
                <input id="closeReason" defaultValue="Export blockers remain open" disabled={!canCloseMonth} />
              </div>
              <div className="field-row">
                <label htmlFor="closeApprover">Approver</label>
                <select id="closeApprover" disabled={!canCloseMonth}><option>Finance Admin required</option><option>Corporate Admin co-approval</option></select>
              </div>
            </div>
            <div className="action-row">
              <button className="danger-button" type="button" disabled={!canCloseMonth}>Request Unlock</button>
              <button className="primary-button" type="button" disabled={!canCloseMonth}>Lock Month</button>
            </div>
          </section>

          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Reconciliation Equation</strong>
                <span>Displayed as explanation, not editable revenue logic</span>
              </div>
            </div>
            <div className="formula" style={{ margin: 13 }}>
              gross_reported - official_tax - payment_fees - allocation_gap + approved_overrides = locked_net
            </div>
            <div className="issue-list" role="list">
              {RECON_NOTES.map((n) => (
                <ItemRow key={n.title} tone={n.tone} title={n.title} sub={n.sub}
                  trailing={<Badge tone={n.badge.tone}>{n.badge.text}</Badge>} />
              ))}
            </div>
          </section>
        </aside>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ trace */

function TraceView({
  traceTab,
  setTraceTab,
  canViewFinance,
  role,
}: {
  traceTab: "Revenue Flow" | "Issues" | "Ownership";
  setTraceTab: (t: "Revenue Flow" | "Issues" | "Ownership") => void;
  canViewFinance: boolean;
  role: Role;
}) {
  const permissionDetails =
    role === "finance"
      ? [
          { label: "Role", value: "Finance Admin" },
          { label: "Scope", value: "Global finance month" },
          { label: "Companies", value: "All allowed" },
          { label: "Raw files", value: "Hidden unless explicitly granted" },
        ]
      : role === "assistant"
        ? [
            { label: "Role", value: "Assistant Analyst" },
            { label: "Scope", value: "Assigned non-finance work" },
            { label: "Companies", value: "Visible without money cells" },
            { label: "Raw files", value: "Hidden unless explicitly granted" },
          ]
        : [
            { label: "Role", value: "Company Manager" },
            { label: "Scope", value: "Assigned company only" },
            { label: "Companies", value: "Company-scoped" },
            { label: "Raw files", value: "Hidden unless explicitly granted" },
          ];

  return (
    <section className="view-page" aria-labelledby="traceViewTitle">
      <div className="view-summary" aria-label="Trace summary">
        {TRACE_SUMMARY.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>

      <div className="view-grid wide-side">
        <section className="panel">
          <div className="panel-header">
            <div className="panel-title">
              <strong id="traceViewTitle">Trace Explorer</strong>
              <span>Ownership, source, issue, and payment relationships filtered by application permissions</span>
            </div>
            <div className="segmented" role="tablist" aria-label="Trace mode">
              {(["Revenue Flow", "Issues", "Ownership"] as const).map((t) => (
                <button
                  key={t}
                  type="button"
                  role="tab"
                  aria-selected={traceTab === t}
                  className={traceTab === t ? "is-active" : undefined}
                  onClick={() => setTraceTab(t)}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>
          <div style={{ padding: 13 }}>
            <div className="trace-canvas large" aria-label="SQL-scoped lineage mockup">
              {TRACE_LINES_LARGE.map((l) => (
                <span key={l.id} className="trace-line"
                  style={{ left: l.left, top: l.top, width: l.width, transform: `rotate(${l.rotate}deg)` }} />
              ))}
              {TRACE_NODES_LARGE.map((n) => (
                <span key={n.id}
                  className={`trace-node${n.finance ? " finance" : ""}`}
                  style={{ left: n.x, top: n.y }}>
                  {n.text}
                </span>
              ))}
              <span className="trace-note">
                Dashboard users read SQL-backed lineage only. The app resolves scopes first,
                then applies allowed company, channel, month, and revenue permissions before
                trace data is returned.
              </span>
            </div>
          </div>
        </section>

        <aside className="view-stack">
          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Permission Filter</strong>
                <span>Applied before SQL trace query construction</span>
              </div>
              <Badge tone="violet">Scoped trace</Badge>
            </div>
            <div className="detail-grid">
              {permissionDetails.map((d) => (
                <div key={d.label} className="detail-cell">
                  <span>{d.label}</span><strong>{d.value}</strong>
                </div>
              ))}
            </div>
          </section>

          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Trace Selection</strong>
                <span>Issue path selected from SQL-backed reconciliation state</span>
              </div>
            </div>
            <div className="issue-list" role="list">
              {TRACE_EVENTS.map((t) => (
                <ItemRow key={t.title} tone={t.tone} title={t.title} sub={t.sub}
                  trailing={<Badge tone={t.badge.tone}>{t.badge.text}</Badge>} />
              ))}
            </div>
          </section>
        </aside>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ exports */

function ExportsView({ permissions }: { permissions: AccessPermissions }) {
  const { canViewFinance } = permissions;
  const canCreateExport = canCreateAnyExport(permissions);
  return (
    <section className="view-page" aria-labelledby="exportsTitle">
      <div className="view-summary" aria-label="Export summary">
        {EXPORTS_SUMMARY.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>

      <div className="view-grid">
        <section className="panel">
          <div className="panel-header">
            <div className="panel-title">
              <strong id="exportsTitle">Export Center</strong>
              <span>Permission-controlled packages with locked month and audit requirements</span>
            </div>
            <button className="primary-button" type="button" disabled={!canCreateExport}>
              Create Export Job
            </button>
          </div>
          <div className="table-wrap">
            <table aria-label="Exports">
              <thead>
                <tr><th>Package</th><th>Audience</th><th>Contains Money</th><th>Requires</th><th>Status</th><th>Action</th></tr>
              </thead>
              <tbody>
                {EXPORTS_ROWS.map((r) => {
                  const canCreatePackage = canCreateExportScope(permissions, r.scope);
                  return (
                    <tr key={r.name}>
                      <td>{r.name}</td>
                      <td>{r.audience}</td>
                      <td><Badge tone={r.money.tone}>{r.money.text}</Badge></td>
                      <td>{r.requires}</td>
                      <td><Badge tone={r.status.tone}>{r.status.text}</Badge></td>
                      <td>
                        <button className="mini-button" type="button" disabled={!canCreatePackage}>
                          {r.action}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <aside className="view-stack">
          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Export Guardrails</strong>
                <span>Every package records scope, filters, checksum, and actor</span>
              </div>
            </div>
            <div className="issue-list" role="list">
              {EXPORTS_GUARDRAILS.map((g) => (
                <ItemRow key={g.title} tone={g.tone} title={g.title} sub={g.sub}
                  trailing={<Badge tone={g.badge.tone}>{g.badge.text}</Badge>} />
              ))}
            </div>
          </section>

          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Export Metadata</strong>
                <span>UI-facing fields that must come from backend jobs</span>
              </div>
            </div>
            <div className="detail-grid">
              {EXPORT_META.map((m) => (
                <div key={m.label} className="detail-cell">
                  <span>{m.label}</span>
                  <strong>
                    {m.chip ? (
                      <span className="code-chip">
                        {canCreateExport ? m.chip : RESTRICTED_FINANCE_VALUE}
                      </span>
                    ) : (
                      m.value
                    )}
                  </strong>
                </div>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ connectors */

function ConnectorsView({ permissions }: { permissions: AccessPermissions }) {
  const { canRunConnectors, canViewFinance } = permissions;
  return (
    <section className="view-page" aria-labelledby="connectorsTitle">
      <div className="view-summary" aria-label="Connector summary">
        {CONNECTORS_SUMMARY.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>

      <div className="view-grid">
        <section className="panel">
          <div className="panel-header">
            <div className="panel-title">
              <strong id="connectorsTitle">Connector Operations</strong>
              <span>OAuth credentials, API jobs, raw files, and ingestion windows</span>
            </div>
            <button className="primary-button" type="button" disabled={!canRunConnectors}>
              Run Approved Job
            </button>
          </div>
          <div className="connector-health" aria-label="Connector health">
            {CONNECTOR_HEALTH.map((h) => (
              <div key={h.name} className="health-block">
                <strong>{h.name}</strong>
                <span>{h.note}</span>
                <Badge tone={h.badge.tone}>{h.badge.text}</Badge>
              </div>
            ))}
          </div>
          <div className="table-wrap">
            <table aria-label="Connector jobs">
              <thead>
                <tr><th>Job</th><th>Credential</th><th>Last Run</th><th>Raw File</th><th>Sensitive Action</th><th>State</th></tr>
              </thead>
              <tbody>
                {CONNECTOR_JOBS.map((j) => (
                  <tr key={j.job}>
                    <td>{j.job}</td>
                    <td>
                      <span className="code-chip">
                        {canRunConnectors ? j.credential : RESTRICTED_FINANCE_VALUE}
                      </span>
                    </td>
                    <td>{j.lastRun}</td>
                    <td>
                      <span className="code-chip">
                        {canRunConnectors ? j.rawFile : RESTRICTED_FINANCE_VALUE}
                      </span>
                    </td>
                    <td>{canRunConnectors ? j.action : RESTRICTED_FINANCE_VALUE}</td>
                    <td><Badge tone={j.state.tone}>{j.state.text}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <aside className="view-stack">
          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Credential Controls</strong>
                <span>No Google passwords stored; OAuth grants only</span>
              </div>
              <Badge tone={canRunConnectors ? "amber" : "red"}>
                {canRunConnectors ? "Admin" : "Restricted"}
              </Badge>
            </div>
            <div className="issue-list" role="list">
              {CREDENTIAL_CONTROLS.map((c) => (
                <ItemRow key={c.title} tone={c.tone} title={c.title} sub={c.sub}
                  trailing={<Badge tone={c.badge.tone}>{c.badge.text}</Badge>} />
              ))}
            </div>
          </section>
        </aside>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ audit */

function AuditView({ permissions }: { permissions: AccessPermissions }) {
  const { canViewAudit, canViewFinance } = permissions;
  return (
    <section className="view-page" aria-labelledby="auditTitle">
      <div className="view-summary" aria-label="Audit summary">
        {AUDIT_SUMMARY.map((s) => (
          <SummaryTile key={s.label} {...s} canViewFinance={canViewFinance} />
        ))}
      </div>

      <div className="view-grid">
        <section className="panel">
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
          <div className="timeline" role="list">
            {canViewAudit ? (
              AUDIT_EVENTS.map((e) => (
                <div key={e.id} className="timeline-item" role="listitem">
                  <span className="timeline-time">{e.time}</span>
                  <Dot tone={e.tone} />
                  <span>
                    <span className="item-title">{e.title}</span>
                    <span className="item-sub">{e.sub}</span>
                  </span>
                  <Badge tone={e.badge.tone}>{e.badge.text}</Badge>
                </div>
              ))
            ) : (
              <div className="timeline-item" role="listitem">
                <span className="timeline-time">--:--</span>
                <Dot tone="red" />
                <span>
                  <span className="item-title">Audit view restricted</span>
                  <span className="item-sub">Sensitive audit events require Finance Admin access</span>
                </span>
                <Badge tone="red">Restricted</Badge>
              </div>
            )}
          </div>
        </section>

        <aside className="view-stack">
          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Audit Coverage</strong>
                <span>Required to be present for every sensitive surface</span>
              </div>
            </div>
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
      </div>
    </section>
  );
}
