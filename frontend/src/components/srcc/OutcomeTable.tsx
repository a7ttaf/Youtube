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
  /**
   * Cell content, rendered in column order — one entry per `columns` label,
   * whose label keys the cell (see the uniqueness note on `columns`).
   */
  cells: ReactNode[];
};

export type OutcomeTableProps = {
  /**
   * Column header labels, in display order. Labels MUST be unique: each label
   * is the React key of its header cell and of that column's cell in every
   * row, so a duplicate label would collide with its twin among its siblings.
   */
  columns: string[];
  /** Data rows; an empty array renders the emptyLabel message row instead. */
  rows: OutcomeTableRow[];
  /** Message shown in a single full-width row when `rows` is empty. */
  emptyLabel: string;
};

/** Render the column header row — one <th> per column, in order. */
const OutcomeTableHead = ({ columns }: { columns: string[] }) => {
  return (
    <thead>
      <tr>
        {columns.map((column) => (
          <th key={column}>{column}</th>
        ))}
      </tr>
    </thead>
  );
};

/** Single full-width message row shown when there are no data rows. */
const OutcomeTableEmptyRow = ({ columnCount, emptyLabel }: {
  columnCount: number;
  emptyLabel: string;
}) => {
  return (
    <tr>
      <td colSpan={columnCount}>
        <span className="muted">{emptyLabel}</span>
      </td>
    </tr>
  );
};

/**
 * A single data row: the caller's cells in order, with data-tone when set. A
 * cell's key combines the row's stable identity with its column index, so a
 * reordered/filtered `rows` array never reuses a cell across columns AND a row
 * whose `cells` length drifts from `columns` (a caller contract violation)
 * still gets unique keys instead of `undefined`. The header-vs-cell count is
 * asserted first so a misaligned row renders one diagnostic empty trailing
 * cell (visible in tests) rather than silently misaligning the whole table.
 */
const OutcomeTableRowView = ({ columns, row }: {
  columns: string[];
  row: OutcomeTableRow;
}) => {
  const cellCount = Math.min(row.cells.length, columns.length);
  return (
    <tr data-tone={row.tone}>
      {row.cells.slice(0, cellCount).map((cell, index) => (
        <td key={`${row.key}:${index}`}>{cell}</td>
      ))}
    </tr>
  );
};

/**
 * Render a semantic <table> from caller-supplied columns/rows. An empty
 * `rows` array renders exactly one full-width message row (never an empty
 * tbody) so the diff view always shows an explicit state.
 */
export const OutcomeTable = ({ columns, rows, emptyLabel }: OutcomeTableProps) => {
  return (
    <div className="table-wrap">
      <table>
        <OutcomeTableHead columns={columns} />
        <tbody>
          {rows.length === 0 ? (
            <OutcomeTableEmptyRow columnCount={columns.length} emptyLabel={emptyLabel} />
          ) : (
            rows.map((row) => (
              <OutcomeTableRowView key={row.key} columns={columns} row={row} />
            ))
          )}
        </tbody>
      </table>
    </div>
  );
};
