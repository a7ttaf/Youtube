import type { ReactNode } from "react";

// ============================================================================
// Purpose: Dumb, reusable outcome/diff table. Renders caller-supplied
//   columns/rows with an optional per-row tone (surfaced as a `data-tone`
//   attribute for the caller's CSS/tests to key on) and a single full-width
//   message row when there are no rows. The Groups view's CMS-sync dry-run
//   diff renders this with outcome Badge chips inside `cells` and
//   tone="warn" on CONFLICT rows; PR-B's import stepper reuses it as-is with
//   different columns. Row content — including any Badge chips — is supplied
//   by the caller as `cells: ReactNode[]` and rendered verbatim; this
//   component owns no state and knows nothing about outcome/Badge semantics.
// Database/ORM: None (presentation only).
// Standards: Zero internal state, zero data fetching, zero extra props beyond
//   the contract. Reuses the exact table-wrap/table/thead/tbody markup
//   RegistryView's RegistryTable already establishes (RegistryTableHead,
//   RegistryTableMessageRow) — same full-width colSpan message-row idiom for
//   the empty case, same .muted text treatment, no new table visual language.
// Blast Radius: Presentation only; no side effects.
// ============================================================================

export type OutcomeTableRow = {
  /** Stable row identity, used as the React key. */
  key: string;
  /** Optional tone surfaced as the row's data-tone attribute. */
  tone?: "warn" | "error";
  /** Cell content, rendered in column order. */
  cells: ReactNode[];
};

export type OutcomeTableProps = {
  /** Column header labels, in display order. */
  columns: string[];
  /** Data rows; an empty array renders the emptyLabel message row instead. */
  rows: OutcomeTableRow[];
  /** Message shown in a single full-width row when `rows` is empty. */
  emptyLabel: string;
};

/** Render the column header row — one <th> per column, in order. */
function OutcomeTableHead({ columns }: { columns: string[] }) {
  return (
    <thead>
      <tr>
        {columns.map((column, index) => (
          <th key={index}>{column}</th>
        ))}
      </tr>
    </thead>
  );
}

/** Single full-width message row shown when there are no data rows. */
function OutcomeTableEmptyRow({ columnCount, emptyLabel }: {
  columnCount: number;
  emptyLabel: string;
}) {
  return (
    <tr>
      <td colSpan={columnCount}>
        <span className="muted">{emptyLabel}</span>
      </td>
    </tr>
  );
}

/** A single data row: the caller's cells in order, with data-tone when set. */
function OutcomeTableRowView({ row }: { row: OutcomeTableRow }) {
  return (
    <tr data-tone={row.tone}>
      {row.cells.map((cell, index) => (
        <td key={index}>{cell}</td>
      ))}
    </tr>
  );
}

/**
 * Render a semantic <table> from caller-supplied columns/rows. An empty
 * `rows` array renders exactly one full-width message row (never an empty
 * tbody) so the diff view always shows an explicit state.
 */
export function OutcomeTable({ columns, rows, emptyLabel }: OutcomeTableProps) {
  return (
    <div className="table-wrap">
      <table>
        <OutcomeTableHead columns={columns} />
        <tbody>
          {rows.length === 0 ? (
            <OutcomeTableEmptyRow columnCount={columns.length} emptyLabel={emptyLabel} />
          ) : (
            rows.map((row) => <OutcomeTableRowView key={row.key} row={row} />)
          )}
        </tbody>
      </table>
    </div>
  );
}
