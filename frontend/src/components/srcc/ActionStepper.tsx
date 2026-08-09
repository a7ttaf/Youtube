import type { ReactNode } from "react";

import type { Severity } from "@/lib/mock/data";
import { Dot } from "./shared";

// ============================================================================
// Purpose: Dumb, reusable step-indicator shell. Renders a labelled sequence of
//   steps (one marked active by index), an arbitrary body (children), and a
//   Cancel action. The Groups view's CMS-sync flow renders this with steps
//   ["Reason", "Preview", "Applied"]; PR-B's import stepper reuses it as-is
//   with different step labels. The calling flow owns ALL state (which step is
//   active, what the body renders) — this component holds none of its own.
// Database/ORM: None (presentation only).
// Standards: Zero internal state, zero data fetching, zero extra props beyond
//   the contract. Reuses the existing .steps/.step/.is-current/.is-done
//   step-indicator classes and the Dot primitive (see AppShell's WorkflowRail)
//   for the step row, and .action-row/.ghost-button for the Cancel action — no
//   new visual language is introduced. Cancel is disable-able by the caller
//   (cancelDisabledReason) so a flow with a write in flight can refuse to
//   unmount itself mid-request; the reason doubles as the button title, so a
//   disabled Cancel always states why. .steps hardcodes a 6-column grid (sized
//   for WorkflowRail's fixed 6-item rail); an inline gridTemplateColumns
//   override sizes it to the caller's actual step count so a 3-step (or any
//   N-step) flow lays out evenly instead of leaving empty trailing columns.
// Blast Radius: Presentation only; the sole side effect is the caller-supplied
//   onCancel.
// ============================================================================

export type ActionStepperProps = {
  /**
   * Ordered step labels, e.g. ["Reason", "Preview", "Applied"]. Labels MUST be
   * unique: each label is its step pill's React key, so a duplicate label would
   * collide with its twin among its siblings.
   */
  steps: string[];
  /** Index of the currently active step (0-based). */
  activeIndex: number;
  /** Fired when the Cancel control is activated. */
  onCancel: () => void;
  /**
   * When set, Cancel is DISABLED and this string is its title — the operator
   * reason it cannot be used right now. One prop rather than a separate
   * boolean + title so a disabled Cancel can never render without saying why.
   * Omitted (the default) leaves Cancel enabled, the pre-existing behaviour.
   */
  cancelDisabledReason?: string;
  /** The active step's body content, rendered below the step row. */
  children: ReactNode;
};

/**
 * Tone for a step's leading Dot: green once passed, the default (primary)
 * dot for the active step, amber while still upcoming — the same
 * done/current/pending coloring AppShell's WorkflowRail already uses for its
 * own step Dots (see workflowDotTone in shared.tsx).
 */
const stepDotTone = (index: number, activeIndex: number): Severity | undefined => {
  if (index < activeIndex) {
    return "green";
  }
  if (index === activeIndex) {
    return undefined;
  }
  return "amber";
};

/** Render a single step pill: leading Dot + label, active/done state classes. */
const StepPill = ({ label, index, activeIndex }: {
  label: string;
  index: number;
  activeIndex: number;
}) => {
  const isActive = index === activeIndex;
  const isDone = index < activeIndex;
  return (
    <span
      className={`step${isActive ? " is-current" : isDone ? " is-done" : ""}`}
      data-active={isActive ? "true" : undefined}
      role="listitem"
    >
      <Dot tone={stepDotTone(index, activeIndex)} />
      {label}
    </span>
  );
};

/**
 * Render the step row (active step flagged via data-active="true"), the
 * caller's body below it, and a Cancel action. Purely presentational — no
 * state, no data fetching; the calling flow decides activeIndex and children.
 */
export const ActionStepper = ({
  steps,
  activeIndex,
  onCancel,
  cancelDisabledReason,
  children,
}: ActionStepperProps) => {
  return (
    <div>
      <div
        className="steps"
        role="list"
        style={{ gridTemplateColumns: `repeat(${steps.length}, minmax(100px, 1fr))` }}
      >
        {steps.map((label, index) => (
          <StepPill key={label} label={label} index={index} activeIndex={activeIndex} />
        ))}
      </div>
      {children}
      <div className="action-row">
        <button
          type="button"
          className="ghost-button"
          disabled={cancelDisabledReason !== undefined}
          title={cancelDisabledReason}
          onClick={onCancel}
        >
          Cancel
        </button>
      </div>
    </div>
  );
};
