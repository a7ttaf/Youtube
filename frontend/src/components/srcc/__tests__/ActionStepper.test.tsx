// frontend/src/components/srcc/__tests__/ActionStepper.test.tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ActionStepper } from "@/components/srcc/ActionStepper";

describe("ActionStepper", () => {
  it("renders step labels with the active one marked and fires onCancel", async () => {
    const onCancel = vi.fn();
    render(
      <ActionStepper steps={["Upload", "Preview", "Applied"]} activeIndex={1} onCancel={onCancel}>
        <p>step body</p>
      </ActionStepper>,
    );
    expect(screen.getByText("Preview").closest('[data-active="true"]')).toBeTruthy();
    expect(screen.getByText("Upload").closest('[data-active="true"]')).toBeNull();
    expect(screen.getByText("step body")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
