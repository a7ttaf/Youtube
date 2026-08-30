import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConfidenceBadge, MoneyCell } from "@ums/design-system";

describe("design-system package boundary", () => {
  it("resolves through the workspace export and applies shared badge classes", () => {
    render(
      <>
        <MoneyCell value="1,234.00" currency="USD" />
        <ConfidenceBadge label="High" tone="green" />
      </>,
    );

    expect(screen.getByTestId("money-cell")).toHaveAttribute("data-currency", "USD");
    expect(screen.getByTestId("confidence-badge")).toHaveClass("badge", "green");
  });
});
