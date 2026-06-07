// ============================================================================
// Purpose: Shared, display-only confidence resolver. Turns a backend confidence
//   CODE (e.g. "B_RECONCILED") and/or an API-provided human label (HIGH/MEDIUM/
//   LOW) into a presentable { label, tone } pair so the Command and Trace views
//   render the same human copy and never drift. Pure presentation — it invents no
//   finance number and changes no source-of-truth value.
// Database/ORM: None (frontend).
// Standards: Prefers any API-provided human label; the prefix map is a FALLBACK
//   only. Tone vocabulary matches the shared Badge `Severity` type (there is no
//   "slate" tone; the neutral tone is "blue", consistent with TraceView's
//   confidenceTone default).
// Blast Radius: Display only — no finance math, no authorization, no audit.
// Connections:
//   - File: frontend/src/lib/mock/data.ts -> Severity tone union.
//   - File: frontend/src/components/srcc/views/TraceView.tsx -> shared HIGH/
//     MEDIUM/LOW tone mapping (confidenceTone).
//   - File: frontend/src/components/srcc/views/CommandView.tsx -> confidence
//     badge consumer.
// ============================================================================

export type ConfidenceTone = "green" | "amber" | "red" | "blue";

export type ConfidenceDisplay = {
  label: string;
  tone: ConfidenceTone;
};

const API_LABEL_TO_TONE: Record<string, ConfidenceTone> = {
  HIGH: "green",
  MEDIUM: "amber",
  LOW: "red",
};

const PREFIX_TO_DISPLAY: Record<string, ConfidenceDisplay> = {
  A: { label: "Official", tone: "green" },
  B: { label: "Reconciled", tone: "green" },
  C: { label: "Allocated", tone: "amber" },
  D: { label: "Estimated", tone: "amber" },
  E: { label: "Missing", tone: "red" },
};

/** Map an API human label (HIGH/MEDIUM/LOW) to a tone — matches TraceView. */
const toneFromApiLabel = (label: string): ConfidenceTone =>
  API_LABEL_TO_TONE[label.toUpperCase()] ?? "blue";

/**
 * Resolve a confidence code (+ optional API label) into a human label and tone.
 * Prefers a non-empty API label; otherwise falls back by code prefix:
 *   A -> Official (green), B -> Reconciled (green), C -> Allocated (amber),
 *   D -> Estimated (amber), E -> Missing (red); unknown -> the raw code with a
 *   neutral tone.
 */
export const confidenceDisplay = (
  code: string,
  apiLabel?: string | null,
): ConfidenceDisplay => {
  const trimmedLabel = apiLabel?.trim();
  if (trimmedLabel) {
    return { label: trimmedLabel, tone: toneFromApiLabel(trimmedLabel) };
  }
  const prefix = (code ?? "").trim().charAt(0).toUpperCase();
  return PREFIX_TO_DISPLAY[prefix] ?? { label: code || "—", tone: "blue" };
};
