import { useState } from "react";

import { ApiError } from "@/lib/api/client";
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
//   link — a plain browser anchor pointing at the proxied binary path (the Vite
//   dev proxy injects the trusted-gateway + X-UMS-Tenant headers) because the
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

const CURRENCY_OPTIONS = ["USD", "EGP", "AED"];

// Honest empty-state copy when the caller's permissions leave zero creatable
// export types (e.g. an analytics-only viewer, since the CSV is held back below).
const NO_CREATABLE_TYPES_MESSAGE =
  "No export types are currently available for your role.";

// ============================================================================
// Purpose: The real accepted export_type enum values (ALLOWED_EXPORT_TYPES), each
//   tagged with the per-type capability it needs and whether the create form may
//   currently OFFER it. The first three are finance exports; the CSV is analytics.
//   ANALYTICS_SUMMARY_CSV is `creatable: false` because the backend has NO GET
//   download route for it yet — offering it would let a user queue a CSV job that
//   shows "Not ready" forever (downloadFor() returns null for it). Existing CSV
//   jobs still render in the list; we only refuse to CREATE new ones. The
//   `capability` flag stays wired to canExportAnalytics so analytics gating
//   becomes meaningful again the moment a CSV download route ships and the type
//   is flipped back to `creatable: true`. The label is UI-only.
// ============================================================================
type ReportTypeOption = {
  value: ExportType;
  label: string;
  capability: "finance" | "analytics";
  creatable: boolean;
};

const REPORT_TYPE_OPTIONS: ReportTypeOption[] = [
  { value: "FINANCE_EXCEL", label: "Finance workbook (XLSX)", capability: "finance", creatable: true },
  { value: "EXECUTIVE_PDF", label: "Executive summary (PDF)", capability: "finance", creatable: true },
  { value: "BRANDED_SLIDE_PACK", label: "Branded slide pack (PPTX)", capability: "finance", creatable: true },
  // Held back from the create form until a CSV GET download route exists.
  { value: "ANALYTICS_SUMMARY_CSV", label: "Analytics summary (CSV)", capability: "analytics", creatable: false },
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

// ============================================================================
// Purpose: Map an export_type to its binary download route + artifact format.
//   Only one route is valid per type (the backend 422s a mismatched type); the
//   analytics CSV has no GET download endpoint yet, so it returns null and no
//   link shows. The action verb (Download vs Generate) is derived from job
//   status by the caller; this returns the route and the format suffix only.
// Standards: The path is the proxied route — the Vite dev proxy injects the
//   trusted-gateway + X-UMS-Tenant headers, so a plain <a download> works
//   without the browser ever holding the gateway secret. Never fetched through
//   useApiClient (JSON-strict; cannot read binary).
// ============================================================================
/** Returns the binary download route and artifact format for a job, or null if the type has no GET route. */
function downloadFor(
  job: ExportJob,
): { href: string; format: string } | null {
  const id = encodeURIComponent(job.id);
  switch (job.export_type) {
    case "FINANCE_EXCEL":
      return { href: `/exports/${id}/finance-workbook.xlsx`, format: "XLSX" };
    case "EXECUTIVE_PDF":
      return { href: `/exports/${id}/executive.pdf`, format: "PDF" };
    case "BRANDED_SLIDE_PACK":
      return { href: `/exports/${id}/branded-slide-pack.pptx`, format: "PPTX" };
    default:
      return null;
  }
}

// ============================================================================
// Purpose: The action verb for a downloadable job's link. A QUEUED job triggers
//   server-side generation on first click; a COMPLETED job serves cached bytes.
// ============================================================================
/** Returns "Generate" for QUEUED jobs (generate-on-demand) and "Download" otherwise. */
function downloadVerb(job: ExportJob): string {
  return job.status.toUpperCase() === "QUEUED" ? "Generate" : "Download";
}

/** Maps an export-job status to its Severity badge tone. */
function statusTone(status: string): Severity {
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
}

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
function isDownloadable(job: ExportJob): boolean {
  const status = job.status.toUpperCase();
  return status === "QUEUED" || status === "COMPLETED";
}

/** Builds a human-readable scope label (e.g. "company · company-a"). */
function scopeLabel(job: ExportJob): string {
  if (job.scope_type === "global") return "Global";
  if (job.scope_id) return `${job.scope_type} · ${job.scope_id}`;
  return job.scope_type;
}

/**
 * The wired Exports screen: a permission-filtered request form plus the export
 * jobs table. Finance and analytics report types are offered only when the
 * caller's matching permission is granted; the default selection is the first
 * allowed report type.
 */
export default function ExportsView({
  canCreateExport,
  canExportFinance,
  canExportAnalytics,
}: {
  canCreateExport: boolean;
  canExportFinance: boolean;
  canExportAnalytics: boolean;
}) {
  // Offer a report type only when it is currently creatable AND the caller holds
  // its per-type capability. The capability check stays wired to
  // canExportFinance/canExportAnalytics so analytics gating is meaningful the
  // moment the CSV type flips back to creatable; the `creatable` check is what
  // holds the CSV out of the form today (no GET download route yet — see
  // REPORT_TYPE_OPTIONS). An analytics-only viewer therefore gets zero options.
  const reportTypeOptions = REPORT_TYPE_OPTIONS.filter((option) => {
    const hasCapability =
      option.capability === "analytics" ? canExportAnalytics : canExportFinance;
    return option.creatable && hasCapability;
  });
  const hasCreatableType = reportTypeOptions.length > 0;
  const defaultExportType = reportTypeOptions[0]?.value ?? "FINANCE_EXCEL";

  const [exportType, setExportType] = useState<ExportType>(defaultExportType);
  const [scopeType, setScopeType] = useState<ExportScopeType>("global");
  const [scopeId, setScopeId] = useState<string>("");
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  const [currency, setCurrency] = useState<string>("USD");
  const [reason, setReason] = useState<string>("");

  // Keep the selection valid when the allowed set excludes the current choice
  // (e.g. permissions change such that the selected type is no longer offered).
  const selectionAllowed = reportTypeOptions.some(
    (option) => option.value === exportType,
  );
  const effectiveExportType = selectionAllowed ? exportType : defaultExportType;

  const { data, loading, error, reload } = useExports();
  const actions = useExportActions();

  const jobs = data?.items ?? [];
  const requiresScopeId = scopeType !== "global";
  const reasonProvided = reason.trim().length > 0;
  const scopeIdProvided = !requiresScopeId || scopeId.trim().length > 0;
  const canSubmit =
    canCreateExport &&
    hasCreatableType &&
    reasonProvided &&
    scopeIdProvided &&
    !actions.loading;

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
    actions
      .requestExport(body)
      .then(() => {
        setReason("");
        reload();
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
          />
        </section>

        <ExportGuardrailsPanel />
      </div>
    </section>
  );
}

/** The Export Center panel header (title, description, and audited badge). */
function ExportCenterHeader() {
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
}

/** Static side panel listing the export guardrails (descriptive role context). */
function ExportGuardrailsPanel() {
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
}

/** Header for the export guardrails side panel (title, description, policy badge). */
function GuardrailsHeader() {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong>Export Guardrails</strong>
        <span>Every package records scope, filters, checksum, and actor</span>
      </div>
      <Badge tone="amber">Policy</Badge>
    </div>
  );
}

/**
 * The Report type field. Renders the creatable-type <select> when at least one
 * type is offered; otherwise it shows an honest disabled state (a single
 * non-selectable option carrying NO_CREATABLE_TYPES_MESSAGE) so a viewer with no
 * creatable types — e.g. analytics-only, since the CSV is held back — never sees
 * an enabled form with no options. The label is kept by the parent so the field
 * stays accessible by name in both states.
 */
function ReportTypeField({
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
}) {
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
}

/** The export request form: report type, scope, month, currency, and reason. */
function RequestExportForm({
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
}) {
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
}

/** Inline alert banner shown when an export request POST fails. */
function RequestError({ error }: { error: ApiError | Error }) {
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
}

/** Inline status banner confirming a newly requested export job. */
function RequestSuccess({ job }: { job: ExportJob }) {
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
}

/** The export jobs section: header with a refresh control plus the jobs body. */
function ExportJobsTable({
  jobs,
  loading,
  error,
  onRefresh,
}: {
  jobs: ExportJob[];
  loading: boolean;
  error: ApiError | Error | null;
  onRefresh: () => void;
}) {
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
      <ExportJobsTableBody jobs={jobs} loading={loading} error={error} />
    </>
  );
}

/** Renders the export jobs table, or the error / loading / empty placeholder. */
function ExportJobsTableBody({
  jobs,
  loading,
  error,
}: {
  jobs: ExportJob[];
  loading: boolean;
  error: ApiError | Error | null;
}) {
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
            <ExportJobRow key={job.id} job={job} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The export jobs table header row (column labels). */
function ExportJobsTableHead() {
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
}

/** A single export-job table row, including its status badge and download cell. */
function ExportJobRow({ job }: { job: ExportJob }) {
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
        <ExportDownloadCell job={job} />
      </td>
    </tr>
  );
}

/**
 * The download cell for a job: a link (Generate for QUEUED, Download for
 * COMPLETED) when the type has a route, otherwise a failure reason or
 * not-ready note.
 */
function ExportDownloadCell({ job }: { job: ExportJob }) {
  const download = downloadFor(job);
  if (isDownloadable(job) && download) {
    // Plain anchor: the proxied path lets the browser stream the binary with the
    // dev proxy's injected trusted-gateway + X-UMS-Tenant headers. NOT fetched
    // via useApiClient (which is JSON-strict and cannot read binary). A QUEUED
    // job triggers server-side generation on the first click.
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
}
