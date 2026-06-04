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
  canRunConnectors: boolean;
  canViewAudit: boolean;
};

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
          <TraceView canViewFinance={canViewFinance} role={displayedRole} />
        )}
        {view === "exports" && (
          <ExportsView canCreateExport={canCreateAnyExport(permissions)} />
        )}
        {view === "connectors" && (
          <ConnectorsView canRunConnectors={permissions.canRunConnectors} />
        )}
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
