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
import { Badge, Dot, ItemRow } from "../shared";
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

// Default to a recent, demo-seedable month per the task brief (matches the
// other wired views).
const DEFAULT_MONTH = "2026-03";

// Months offered in the selector (most recent first). A simple dropdown by
// design — wiring real data is the priority, not month discovery.
const MONTH_OPTIONS = ["2026-03", "2026-02", "2026-01", "2025-12"];

const CURRENCY_OPTIONS = ["USD", "EGP", "AED"];

// The real accepted export_type enum values (ALLOWED_EXPORT_TYPES). The first
// three are finance exports; the CSV is analytics. The label is UI-only.
const REPORT_TYPE_OPTIONS: Array<{ value: ExportType; label: string }> = [
  { value: "FINANCE_EXCEL", label: "Finance workbook (XLSX)" },
  { value: "EXECUTIVE_PDF", label: "Executive summary (PDF)" },
  { value: "BRANDED_SLIDE_PACK", label: "Branded slide pack (PPTX)" },
  { value: "ANALYTICS_SUMMARY_CSV", label: "Analytics summary (CSV)" },
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
// Purpose: Map an export_type to its binary download route + a label. Only one
//   route is valid per type (the backend 422s a mismatched type); the analytics
//   CSV has no GET download endpoint yet, so it returns null and no link shows.
// Standards: The path is the proxied route — the Vite dev proxy injects the
//   trusted-gateway + X-UMS-Tenant headers, so a plain <a download> works
//   without the browser ever holding the gateway secret. Never fetched through
//   useApiClient (JSON-strict; cannot read binary).
// ============================================================================
function downloadFor(
  job: ExportJob,
): { href: string; label: string } | null {
  const id = encodeURIComponent(job.id);
  switch (job.export_type) {
    case "FINANCE_EXCEL":
      return { href: `/exports/${id}/finance-workbook.xlsx`, label: "Download XLSX" };
    case "EXECUTIVE_PDF":
      return { href: `/exports/${id}/executive.pdf`, label: "Download PDF" };
    case "BRANDED_SLIDE_PACK":
      return {
        href: `/exports/${id}/branded-slide-pack.pptx`,
        label: "Download PPTX",
      };
    default:
      return null;
  }
}

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

// A COMPLETED job with a persisted artifact is the only state the backend will
// serve cached bytes for; gate the visible download link on it.
function isDownloadable(job: ExportJob): boolean {
  return job.status.toUpperCase() === "COMPLETED" && job.file_url !== null;
}

function scopeLabel(job: ExportJob): string {
  if (job.scope_type === "global") return "Global";
  if (job.scope_id) return `${job.scope_type} · ${job.scope_id}`;
  return job.scope_type;
}

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

export default function ExportsView({
  canCreateExport,
}: {
  canCreateExport: boolean;
}) {
  const [exportType, setExportType] = useState<ExportType>("FINANCE_EXCEL");
  const [scopeType, setScopeType] = useState<ExportScopeType>("global");
  const [scopeId, setScopeId] = useState<string>("");
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  const [currency, setCurrency] = useState<string>("USD");
  const [reason, setReason] = useState<string>("");

  const { data, loading, error, reload } = useExports();
  const actions = useExportActions();

  const jobs = data?.items ?? [];
  const requiresScopeId = scopeType !== "global";
  const reasonProvided = reason.trim().length > 0;
  const scopeIdProvided = !requiresScopeId || scopeId.trim().length > 0;
  const canSubmit =
    canCreateExport && reasonProvided && scopeIdProvided && !actions.loading;

  const onGenerate = () => {
    if (!canSubmit) return;
    const body: ExportRequestBody = {
      export_type: exportType,
      scope_type: scopeType,
      // Global takes no scope_id; the backend coerces blank -> null anyway.
      scope_id: requiresScopeId ? scopeId.trim() : null,
      month,
      currency,
      reason: reason.trim(),
      include_confidence_notes: true,
      include_manual_override_notes: true,
    };
    // The hook captures its own error state; swallow the rejection here so an
    // un-actioned promise does not surface as an unhandled rejection. On
    // success refetch the list so the new QUEUED job appears.
    void actions
      .requestExport(body)
      .then(() => {
        setReason("");
        reload();
      })
      .catch(() => {});
  };

  return (
    <section className="view-page" aria-labelledby="exportsTitle">
      <div className="view-grid wide-side">
        <section className="panel">
          <div className="panel-header">
            <div className="panel-title">
              <strong id="exportsTitle">Export Center</strong>
              <span>
                Request a permission-controlled package, then download the
                generated, checksum-audited artifact
              </span>
            </div>
            <Badge tone="violet">Audited export</Badge>
          </div>

          <RequestExportForm
            exportType={exportType}
            onExportType={setExportType}
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

        <aside className="view-stack">
          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Export Guardrails</strong>
                <span>Every package records scope, filters, checksum, and actor</span>
              </div>
              <Badge tone="amber">Policy</Badge>
            </div>
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
      </div>
    </section>
  );
}

function RequestExportForm({
  exportType,
  onExportType,
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
        <select
          id="exportReportType"
          value={exportType}
          disabled={!canCreateExport}
          onChange={(e) => onExportType(e.target.value as ExportType)}
        >
          {REPORT_TYPE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
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
        <tbody>
          {jobs.map((job) => {
            const download = downloadFor(job);
            const downloadable = isDownloadable(job);
            return (
              <tr key={job.id}>
                <td>{job.export_type}</td>
                <td>{scopeLabel(job)}</td>
                <td>{job.month}</td>
                <td>
                  <Badge tone={statusTone(job.status)}>{job.status}</Badge>
                </td>
                <td>{formatTimestamp(job.created_at)}</td>
                <td>{formatTimestamp(job.completed_at)}</td>
                <td>
                  {downloadable && download ? (
                    // Plain anchor: the proxied path lets the browser stream the
                    // binary with the dev proxy's injected trusted-gateway +
                    // X-UMS-Tenant headers. NOT fetched via useApiClient (which
                    // is JSON-strict and cannot read binary).
                    <a
                      className="mini-button"
                      href={download.href}
                      download
                    >
                      {download.label}
                    </a>
                  ) : (
                    <span className="muted">
                      {job.status.toUpperCase() === "FAILED"
                        ? job.failure_reason ?? "Failed"
                        : "Not ready"}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
