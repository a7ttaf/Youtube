import { useState } from "react";

import { ApiError, resolveUrl } from "@/lib/api/client";
import type {
  ExportJob,
  ExportRequestBody,
  ExportScopeType,
  ExportType,
} from "@/lib/api/types";
import { useExportActions } from "@/lib/api/useExportActions";
import { useExports } from "@/lib/api/useExports";
import { EXPORTS_GUARDRAILS } from "@/lib/mock/data";
import type { Severity } from "@/lib/mock/data";
import {
  Badge,
  DEFAULT_MONTH,
  Dot,
  formatTimestamp,
  ItemRow,
  MONTH_OPTIONS,
} from "../shared";
import { describeError } from "./CommandView";

// ============================================================================
// Purpose: The REAL-data Exports screen, extracted from AppShell. The operator
//   fills a request form (report type + scope + month + currency + reason),
//   "Generate" POSTs to /exports (creating a QUEUED job + audit event), and the
//   jobs table reloads from GET /exports. Each COMPLETED job exposes a DOWNLOAD
//   link — a plain browser anchor whose href is resolved against the same API
//   origin the JSON client uses (resolveUrl): relative (proxied) when no base is
//   configured so the Vite dev proxy injects the trusted-gateway + X-UMS-Tenant
//   headers, or the configured VITE_API_BASE_URL origin otherwise — because the
//   JSON-strict useApiClient cannot fetch binary. Loading / error / 403 states
//   mirror CommandView and TraceView. The Export Guardrails side panel stays as
//   static role context (it is descriptive, not API data).
// Database/ORM: None (frontend) — consumes GET /exports (list), POST /exports
//   (create, server-side insert + audit), and links to the binary download
//   routes; downloads are served by the backend, never fetched client-side.
// Standards: No client-side export authorization is invented — the backend gate
//   (EXPORT_REVENUE/ANALYTICS_REPORT + VIEW_* @scope, plus VIEW_FINALIZED_PAYMENTS
//   @finance_month for finance exports) is authoritative; a 403 surfaces as
//   no-permission copy. The browser never holds the gateway secret; downloads
//   ride the same proxied, header-injected path as every other call.
// Blast Radius: Export create (write path) + artifact download — both via the
//   backend's own guarded, audited routes only. No source-of-truth finance
//   number is computed or mutated client-side.
// Connections:
//   - File: frontend/src/lib/api/useExports.ts -> the list fetch hook.
//   - File: frontend/src/lib/api/useExportActions.ts -> the create POST hook.
//   - File: frontend/src/lib/api/types.ts -> ExportJob / ExportRequestBody.
//   - File: backend/ums_smart_revenue/api/exports.py:173 request_export / :287 list.
// ============================================================================

// FIX: USD only — the backend rejects every non-USD export currency
// (reports/exports.py::_normalize_currency raises "currency must be USD until
// exchange-rate support is implemented"), so offering EGP/AED produced a
// valid-looking form whose submission was guaranteed to 422. Re-add display
// currencies once backend exchange-rate support ships (Docs/18).
const CURRENCY_OPTIONS = ["USD"];

// Honest empty-state copy when the caller's permissions leave zero creatable
// export types for their current backend grants.
const NO_CREATABLE_TYPES_MESSAGE =
  "No export types are currently available for your role.";

type DownloadRoute = {
  readonly path: (id: string) => string;
  readonly format: string;
  readonly requiresRevenueVisibility?: boolean;
};

const DOWNLOAD_ROUTES: Partial<Record<ExportType, DownloadRoute>> = {
  FINANCE_EXCEL: {
    path: (id) => `/exports/${id}/finance-workbook.xlsx`,
    format: "XLSX",
  },
  EXECUTIVE_PDF: {
    path: (id) => `/exports/${id}/executive.pdf`,
    format: "PDF",
  },
  BRANDED_SLIDE_PACK: {
    path: (id) => `/exports/${id}/branded-slide-pack.pptx`,
    format: "PPTX",
  },
  ANALYTICS_SUMMARY_CSV: {
    path: (id) => `/exports/${id}/analytics-summary.csv`,
    format: "CSV",
    requiresRevenueVisibility: true,
  },
};

const hasDownloadRoute = (
  exportType: string,
): exportType is ExportType =>
  Object.prototype.hasOwnProperty.call(DOWNLOAD_ROUTES, exportType);

// ============================================================================
// Purpose: The real accepted export_type enum values (ALLOWED_EXPORT_TYPES), each
//   tagged with the per-type capability it needs and whether the create form may
//   currently OFFER it. The first three are finance exports; the CSV is analytics
//   and is downloadable through the dedicated analytics-summary route. The
//   backend remains authoritative for the additional finance.view_revenue gate
//   because the CSV carries revenue amounts.
// ============================================================================
type ReportTypeOption = {
  value: ExportType;
  label: string;
  capability: "finance" | "analytics";
  creatable: boolean;
  requiresRevenueVisibility?: boolean;
};

type ReportTypePermissions = {
  canExportFinance: boolean;
  canExportAnalytics: boolean;
  canViewRevenue: boolean;
};

const REPORT_TYPE_OPTIONS: ReportTypeOption[] = [
  { value: "FINANCE_EXCEL", label: "Finance workbook (XLSX)", capability: "finance", creatable: true },
  { value: "EXECUTIVE_PDF", label: "Executive summary (PDF)", capability: "finance", creatable: true },
  { value: "BRANDED_SLIDE_PACK", label: "Branded slide pack (PPTX)", capability: "finance", creatable: true },
  {
    value: "ANALYTICS_SUMMARY_CSV",
    label: "Analytics summary (CSV)",
    capability: "analytics",
    creatable: true,
    requiresRevenueVisibility: true,
  },
];

// The real accepted scope_type enum values (ALLOWED_EXPORT_SCOPE_TYPES). Global
// takes no scope_id; the others require one (the backend rejects a missing id).
const SCOPE_TYPE_OPTIONS: Array<{ value: ExportScopeType; label: string }> = [
  { value: "global", label: "Global (all channels)" },
  { value: "company", label: "Company" },
  { value: "sector", label: "Sector" },
  { value: "channel", label: "Channel" },
  { value: "group", label: "Group" },
];

const hasReportCapability = (
  option: ReportTypeOption,
  permissions: ReportTypePermissions,
): boolean => {
  return option.capability === "analytics"
    ? permissions.canExportAnalytics
    : permissions.canExportFinance;
};

const hasCreateRevenueVisibility = (
  option: ReportTypeOption,
  permissions: ReportTypePermissions,
): boolean => {
  return !option.requiresRevenueVisibility || permissions.canViewRevenue;
};

const canOfferReportType = (
  option: ReportTypeOption,
  permissions: ReportTypePermissions,
): boolean => {
  return (
    option.creatable &&
    hasReportCapability(option, permissions) &&
    hasCreateRevenueVisibility(option, permissions)
  );
};

// The default report-type selection: the first offered option, or FINANCE_EXCEL
// when none is offered (the form is disabled in that state, so the fallback is
// never submittable). Extracted from ExportsView so the component's cyclomatic
// complexity stays below the DeepSource medium-risk threshold.
const defaultReportType = (options: ReportTypeOption[]): ExportType =>
  options[0]?.value ?? "FINANCE_EXCEL";

// Keep the selection valid when the allowed set excludes the current choice
// (e.g. permissions change such that the selected type is no longer offered).
// Extracted from ExportsView so the component's cyclomatic complexity stays
// below the DeepSource medium-risk threshold.
const effectiveReportType = (
  options: ReportTypeOption[],
  selected: ExportType,
  fallback: ExportType,
): ExportType =>
  options.some((option) => option.value === selected) ? selected : fallback;

// ============================================================================
// Purpose: Map an export_type to its binary download route + artifact format.
//   Only one route is valid per type (the backend 422s a mismatched type). The
//   action verb (Download vs Generate) is derived from job status by the caller;
//   this returns the route and the format suffix only.
// Standards: The href is resolved through the SAME base-URL logic the JSON
//   client uses (resolveUrl), so when VITE_API_BASE_URL points at a separate API
//   origin the download anchor targets that origin instead of the frontend's.
//   With no base configured the href stays relative (byte-identical to before),
//   so the Vite dev proxy still injects the trusted-gateway + X-UMS-Tenant
//   headers and a plain <a download> works without the browser ever holding the
//   gateway secret. Never fetched through useApiClient (JSON-strict; cannot read
//   binary).
// ============================================================================
/**
 * Returns the binary download route (resolved against the configured API origin)
 * and artifact format for a job, or null if the type has no GET route.
 */
const downloadFor = (
  job: ExportJob,
  canViewRevenue: boolean,
): { href: string; format: string } | null => {
  const route = hasDownloadRoute(job.export_type)
    ? DOWNLOAD_ROUTES[job.export_type]
    : null;
  if (!route || (route.requiresRevenueVisibility && !canViewRevenue)) {
    // FIX: Analytics CSV artifacts include revenue amounts, so the UI must not
    // expose their direct GET route when finance.view_revenue is absent.
    return null;
  }
  const id = encodeURIComponent(job.id);
  return { href: resolveUrl(route.path(id)), format: route.format };
};

// ============================================================================
// Purpose: The action verb for a downloadable job's link. A QUEUED job triggers
//   server-side generation on first click; a COMPLETED job serves cached bytes.
// ============================================================================
/** Returns "Generate" for QUEUED jobs (generate-on-demand) and "Download" otherwise. */
const downloadVerb = (job: ExportJob): string => {
  return job.status.toUpperCase() === "QUEUED" ? "Generate" : "Download";
};

/** Maps an export-job status to its Severity badge tone. */
const statusTone = (status: string): Severity => {
  switch (status.toUpperCase()) {
    case "COMPLETED":
      return "green";
    case "QUEUED":
      return "amber";
    case "FAILED":
      return "red";
    case "CANCELLED":
      return "blue";
    default:
      return "blue";
  }
};

// FIX: The backend download routes generate-on-demand: for a QUEUED job with no
// persisted artifact they build + persist + stream the bytes (exports.py
// download_*; the `served is None` branch). So both QUEUED and COMPLETED jobs
// are downloadable — QUEUED triggers server-side generation on first click,
// COMPLETED serves cached bytes. Only FAILED (shows failure_reason) and
// CANCELLED (no artifact) are not downloadable. Gating on COMPLETED + file_url
// previously hid the link for a freshly QUEUED job that the backend would serve.
/**
 * True when the backend can serve or generate this job's artifact: QUEUED
 * (generate-on-demand) and COMPLETED (cached) are downloadable; FAILED and
 * CANCELLED are not.
 */
const isDownloadable = (job: ExportJob): boolean => {
  const status = job.status.toUpperCase();
  return status === "QUEUED" || status === "COMPLETED";
};

/** Builds a human-readable scope label (e.g. "company · company-a"). */
const scopeLabel = (job: ExportJob): string => {
  if (job.scope_type === "global") return "Global";
  if (job.scope_id) return `${job.scope_type} · ${job.scope_id}`;
  return job.scope_type;
};

// Whether the Generate button may fire: every gate must hold — creation
// permission, at least one offered report type, the audit reason present, the
// scope id present when required, and no request already in flight. Extracted
// from ExportsView so the component's cyclomatic complexity stays below the
// DeepSource medium-risk threshold.
const canSubmitExportRequest = (form: {
  canCreateExport: boolean;
  hasCreatableType: boolean;
  reasonProvided: boolean;
  scopeIdProvided: boolean;
  submitting: boolean;
}): boolean =>
  form.canCreateExport &&
  form.hasCreatableType &&
  form.reasonProvided &&
  form.scopeIdProvided &&
  !form.submitting;

/** The Export Center panel header (title, description, and audited badge). */
const ExportCenterHeader = () => {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="exportsTitle">Export Center</strong>
        <span>
          Request a permission-controlled package, then download the generated,
          checksum-audited artifact
        </span>
      </div>
      <Badge tone="violet">Audited export</Badge>
    </div>
  );
};

/** Header for the export guardrails side panel (title, description, policy badge). */
const GuardrailsHeader = () => {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong>Export Guardrails</strong>
        <span>Every package records scope, filters, checksum, and actor</span>
      </div>
      <Badge tone="amber">Policy</Badge>
    </div>
  );
};

/** Static side panel listing the export guardrails (descriptive role context). */
const ExportGuardrailsPanel = () => {
  return (
    <aside className="view-stack">
      <section className="panel">
        <GuardrailsHeader />
        <div className="issue-list" role="list">
          {EXPORTS_GUARDRAILS.map((g) => (
            <ItemRow
              key={g.title}
              tone={g.tone}
              title={g.title}
              sub={g.sub}
              trailing={<Badge tone={g.badge.tone}>{g.badge.text}</Badge>}
            />
          ))}
        </div>
      </section>
    </aside>
  );
};

/**
 * The Report type field. Renders the creatable-type <select> when at least one
 * type is offered; otherwise it shows an honest disabled state (a single
 * non-selectable option carrying NO_CREATABLE_TYPES_MESSAGE) so a viewer with no
 * creatable types — e.g. analytics-only, since the CSV is held back — never sees
 * an enabled form with no options. The label is kept by the parent so the field
 * stays accessible by name in both states.
 */
const ReportTypeField = ({
  exportType,
  onExportType,
  reportTypeOptions,
  hasCreatableType,
  canCreateExport,
}: {
  exportType: ExportType;
  onExportType: (value: ExportType) => void;
  reportTypeOptions: ReportTypeOption[];
  hasCreatableType: boolean;
  canCreateExport: boolean;
}) => {
  if (!hasCreatableType) {
    return (
      <select id="exportReportType" disabled>
        <option>{NO_CREATABLE_TYPES_MESSAGE}</option>
      </select>
    );
  }
  return (
    <select
      id="exportReportType"
      value={exportType}
      disabled={!canCreateExport}
      onChange={(e) => onExportType(e.target.value as ExportType)}
    >
      {reportTypeOptions.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
};

/** The export request form: report type, scope, month, currency, and reason. */
const RequestExportForm = ({
  exportType,
  onExportType,
  reportTypeOptions,
  hasCreatableType,
  scopeType,
  onScopeType,
  scopeId,
  onScopeId,
  requiresScopeId,
  month,
  onMonth,
  currency,
  onCurrency,
  reason,
  onReason,
  canCreateExport,
  canSubmit,
  submitting,
  onGenerate,
}: {
  exportType: ExportType;
  onExportType: (value: ExportType) => void;
  reportTypeOptions: ReportTypeOption[];
  hasCreatableType: boolean;
  scopeType: ExportScopeType;
  onScopeType: (value: ExportScopeType) => void;
  scopeId: string;
  onScopeId: (value: string) => void;
  requiresScopeId: boolean;
  month: string;
  onMonth: (value: string) => void;
  currency: string;
  onCurrency: (value: string) => void;
  reason: string;
  onReason: (value: string) => void;
  canCreateExport: boolean;
  canSubmit: boolean;
  submitting: boolean;
  onGenerate: () => void;
}) => {
  return (
    <div className="form-grid" aria-label="Request export" style={{ margin: 13 }}>
      <div className="field-row">
        <label htmlFor="exportReportType">Report type</label>
        <ReportTypeField
          exportType={exportType}
          onExportType={onExportType}
          reportTypeOptions={reportTypeOptions}
          hasCreatableType={hasCreatableType}
          canCreateExport={canCreateExport}
        />
      </div>
      <div className="field-row">
        <label htmlFor="exportScopeType">Scope type</label>
        <select
          id="exportScopeType"
          value={scopeType}
          disabled={!canCreateExport}
          onChange={(e) => onScopeType(e.target.value as ExportScopeType)}
        >
          {SCOPE_TYPE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <div className="field-row">
        <label htmlFor="exportScopeId">Scope id</label>
        <input
          id="exportScopeId"
          value={scopeId}
          disabled={!canCreateExport || !requiresScopeId}
          placeholder={requiresScopeId ? "e.g. company-a" : "Not used for global"}
          onChange={(e) => onScopeId(e.target.value)}
        />
      </div>
      <div className="field-row">
        <label htmlFor="exportMonth">Month</label>
        <select
          id="exportMonth"
          value={month}
          disabled={!canCreateExport}
          onChange={(e) => onMonth(e.target.value)}
        >
          {MONTH_OPTIONS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </div>
      <div className="field-row">
        <label htmlFor="exportCurrency">Currency</label>
        <select
          id="exportCurrency"
          value={currency}
          disabled={!canCreateExport}
          onChange={(e) => onCurrency(e.target.value)}
        >
          {CURRENCY_OPTIONS.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>
      <div className="field-row">
        <label htmlFor="exportReason">Reason</label>
        <input
          id="exportReason"
          value={reason}
          disabled={!canCreateExport}
          placeholder="Recorded on the audit event"
          onChange={(e) => onReason(e.target.value)}
        />
      </div>
      <div className="action-row">
        <button
          className="primary-button"
          type="button"
          disabled={!canSubmit}
          onClick={onGenerate}
        >
          {submitting ? "Generating…" : "Generate"}
        </button>
      </div>
    </div>
  );
};

/** Inline alert banner shown when an export request POST fails. */
const RequestError = ({ error }: { error: ApiError | Error }) => {
  const { title, detail } = describeError(error);
  return (
    <div className="permission-band" role="alert" style={{ margin: 13 }}>
      <Dot tone="red" />
      <span>
        <strong>{title}</strong>
        <span>{`Export request failed — ${detail}`}</span>
      </span>
      <Badge tone="red">Not created</Badge>
    </div>
  );
};

/** Inline status banner confirming a newly requested export job. */
const RequestSuccess = ({ job }: { job: ExportJob }) => {
  return (
    <div className="permission-band" role="status" style={{ margin: 13 }}>
      <Dot tone="green" />
      <span>
        <strong>Export requested</strong>
        <span>{`${job.export_type} · ${scopeLabel(job)} · ${job.month}`}</span>
      </span>
      <Badge tone={statusTone(job.status)}>{job.status}</Badge>
    </div>
  );
};

/** The export jobs table header row (column labels). */
const ExportJobsTableHead = () => {
  return (
    <thead>
      <tr>
        <th scope="col">Type</th>
        <th scope="col">Scope</th>
        <th scope="col">Month</th>
        <th scope="col">Status</th>
        <th scope="col">Created</th>
        <th scope="col">Completed</th>
        <th scope="col">Download</th>
      </tr>
    </thead>
  );
};

/**
 * The download cell for a job: a link (Generate for QUEUED, Download for
 * COMPLETED) when the type has a route, otherwise a failure reason or
 * not-ready note.
 */
const ExportDownloadCell = ({
  job,
  canViewRevenue,
}: {
  job: ExportJob;
  canViewRevenue: boolean;
}) => {
  const download = downloadFor(job, canViewRevenue);
  if (isDownloadable(job) && download) {
    // Plain anchor: the href is resolved against the configured API origin
    // (resolveUrl). When no base is set it stays relative so the dev proxy
    // injects the trusted-gateway + X-UMS-Tenant headers; with VITE_API_BASE_URL
    // it targets that API origin. NOT fetched via useApiClient (which is
    // JSON-strict and cannot read binary). A QUEUED job triggers server-side
    // generation on the first click.
    return (
      <a className="mini-button" href={download.href} download>
        {`${downloadVerb(job)} ${download.format}`}
      </a>
    );
  }
  return (
    <span className="muted">
      {job.status.toUpperCase() === "FAILED"
        ? job.failure_reason ?? "Failed"
        : "Not ready"}
    </span>
  );
};

/** A single export-job table row, including its status badge and download cell. */
const ExportJobRow = ({
  job,
  canViewRevenue,
}: {
  job: ExportJob;
  canViewRevenue: boolean;
}) => {
  return (
    <tr>
      <td>{job.export_type}</td>
      <td>{scopeLabel(job)}</td>
      <td>{job.month}</td>
      <td>
        <Badge tone={statusTone(job.status)}>{job.status}</Badge>
      </td>
      <td>{formatTimestamp(job.created_at)}</td>
      <td>{formatTimestamp(job.completed_at)}</td>
      <td>
        <ExportDownloadCell job={job} canViewRevenue={canViewRevenue} />
      </td>
    </tr>
  );
};

/** Renders the export jobs table, or the error / loading / empty placeholder. */
const ExportJobsTableBody = ({
  jobs,
  loading,
  error,
  canViewRevenue,
}: {
  jobs: ExportJob[];
  loading: boolean;
  error: ApiError | Error | null;
  canViewRevenue: boolean;
}) => {
  if (error) {
    const { title, detail } = describeError(error);
    return (
      <div className="table-wrap" role="alert">
        <div style={{ padding: 16 }}>
          <strong>{title}</strong>
          <p className="item-sub">{detail}</p>
        </div>
      </div>
    );
  }

  if (loading && jobs.length === 0) {
    return (
      <div className="table-wrap" aria-busy="true">
        <div style={{ padding: 16 }} className="item-sub">
          Loading export jobs…
        </div>
      </div>
    );
  }

  if (jobs.length === 0) {
    return (
      <div className="table-wrap">
        <div style={{ padding: 16 }} className="item-sub">
          No export jobs yet. Request one above to generate a package.
        </div>
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table aria-label="Export jobs">
        <ExportJobsTableHead />
        <tbody>
          {jobs.map((job) => (
            <ExportJobRow
              key={job.id}
              job={job}
              canViewRevenue={canViewRevenue}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
};

/** The export jobs section: header with a refresh control plus the jobs body. */
const ExportJobsTable = ({
  jobs,
  loading,
  error,
  onRefresh,
  canViewRevenue,
}: {
  jobs: ExportJob[];
  loading: boolean;
  error: ApiError | Error | null;
  onRefresh: () => void;
  canViewRevenue: boolean;
}) => {
  return (
    <>
      <div className="panel-header" style={{ marginTop: 13 }}>
        <div className="panel-title">
          <strong>Export Jobs</strong>
          <span>Your requested packages and their generated artifacts</span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="Refresh export jobs"
          title="Refresh export jobs"
          onClick={onRefresh}
        >
          ↻
        </button>
      </div>
      <ExportJobsTableBody
        jobs={jobs}
        loading={loading}
        error={error}
        canViewRevenue={canViewRevenue}
      />
    </>
  );
};

/**
 * The wired Exports screen: a permission-filtered request form plus the export
 * jobs table. Finance and analytics report types are offered only when the
 * caller's matching permission is granted; the default selection is the first
 * allowed report type.
 */
const ExportsView = ({
  canCreateExport,
  canExportFinance,
  canExportAnalytics,
  canViewRevenue,
}: {
  canCreateExport: boolean;
  canExportFinance: boolean;
  canExportAnalytics: boolean;
  canViewRevenue: boolean;
}) => {
  // Offer a report type only when it is currently creatable AND the caller holds
  // its per-type capability. Revenue-valued CSVs also require the scoped/global
  // revenue hint that /session/me derives from the backend grants.
  const reportTypePermissions = {
    canExportFinance,
    canExportAnalytics,
    canViewRevenue,
  };
  const reportTypeOptions = REPORT_TYPE_OPTIONS.filter((option) =>
    canOfferReportType(option, reportTypePermissions),
  );
  const hasCreatableType = reportTypeOptions.length > 0;
  const defaultExportType = defaultReportType(reportTypeOptions);

  const [exportType, setExportType] = useState<ExportType>(defaultExportType);
  const [scopeType, setScopeType] = useState<ExportScopeType>("global");
  const [scopeId, setScopeId] = useState<string>("");
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  const [currency, setCurrency] = useState<string>("USD");
  const [reason, setReason] = useState<string>("");

  const effectiveExportType = effectiveReportType(
    reportTypeOptions,
    exportType,
    defaultExportType,
  );

  const { data, loading, error, reload } = useExports();
  const actions = useExportActions();

  const jobs = data?.items ?? [];
  const requiresScopeId = scopeType !== "global";
  const canSubmit = canSubmitExportRequest({
    canCreateExport,
    hasCreatableType,
    reasonProvided: reason.trim().length > 0,
    scopeIdProvided: !requiresScopeId || scopeId.trim().length > 0,
    submitting: actions.loading,
  });

  /**
   * Validates and POSTs the request form, then refetches the list on success.
   * The hook owns the error state, so the rejection is swallowed locally to
   * avoid an unhandled promise rejection.
   */
  const onGenerate = () => {
    if (!canSubmit) return;
    const body: ExportRequestBody = {
      export_type: effectiveExportType,
      scope_type: scopeType,
      // Global takes no scope_id; the backend coerces blank -> null anyway.
      scope_id: requiresScopeId ? scopeId.trim() : null,
      month,
      currency,
      reason: reason.trim(),
      include_confidence_notes: true,
      include_manual_override_notes: true,
    };
    // On success refetch the list so the new QUEUED job appears.
    // FIX: gate side effects on a non-null result — requestExport() resolves
    // with null when a same-tick duplicate is dropped by the in-flight guard
    // (no POST fired); clearing the reason before the real in-flight POST
    // settles would discard the operator's audit reason if that request fails.
    actions
      .requestExport(body)
      .then((created) => {
        if (created !== null) {
          setReason("");
          reload();
        }
      })
      .catch(() => {
        // The hook captures its own error state (actions.error), which the form
        // renders; swallow the rejection here so it does not surface as an
        // unhandled promise rejection.
      });
  };

  return (
    <section className="view-page" aria-labelledby="exportsTitle">
      <div className="view-grid wide-side">
        <section className="panel">
          <ExportCenterHeader />

          <RequestExportForm
            exportType={effectiveExportType}
            onExportType={setExportType}
            reportTypeOptions={reportTypeOptions}
            hasCreatableType={hasCreatableType}
            scopeType={scopeType}
            onScopeType={setScopeType}
            scopeId={scopeId}
            onScopeId={setScopeId}
            requiresScopeId={requiresScopeId}
            month={month}
            onMonth={setMonth}
            currency={currency}
            onCurrency={setCurrency}
            reason={reason}
            onReason={setReason}
            canCreateExport={canCreateExport}
            canSubmit={canSubmit}
            submitting={actions.loading}
            onGenerate={onGenerate}
          />

          {actions.error ? <RequestError error={actions.error} /> : null}
          {actions.data ? <RequestSuccess job={actions.data} /> : null}

          <ExportJobsTable
            jobs={jobs}
            loading={loading}
            error={error}
            onRefresh={reload}
            canViewRevenue={canViewRevenue}
          />
        </section>

        <ExportGuardrailsPanel />
      </div>
    </section>
  );
};

export default ExportsView;
