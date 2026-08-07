// frontend/src/components/srcc/__tests__/OutcomeTable.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { OutcomeTable } from "@/components/srcc/OutcomeTable";

describe("OutcomeTable", () => {
  it("renders columns as table headers and cells in order", () => {
    const { container } = render(
      <OutcomeTable
        columns={["Channel", "Outcome"]}
        rows={[
          { key: "row-1", cells: ["UC-1", "CREATE"] },
          { key: "row-2", cells: ["UC-2", "UNCHANGED"] },
        ]}
        emptyLabel="No differences"
      />,
    );

    // Semantic <table> with real <thead>/<tbody> — not a div-based grid.
    expect(container.querySelector("table")).toBeTruthy();

    const headerCells = Array.from(container.querySelectorAll("thead th")).map(
      (el) => el.textContent,
    );
    expect(headerCells).toEqual(["Channel", "Outcome"]);

    const bodyRows = container.querySelectorAll("tbody tr");
    expect(bodyRows).toHaveLength(2);
    expect(Array.from(bodyRows[0].querySelectorAll("td")).map((el) => el.textContent)).toEqual([
      "UC-1",
      "CREATE",
    ]);
    expect(Array.from(bodyRows[1].querySelectorAll("td")).map((el) => el.textContent)).toEqual([
      "UC-2",
      "UNCHANGED",
    ]);
  });

  it("carries data-tone on a toned row and omits it on a toneless row", () => {
    const { container } = render(
      <OutcomeTable
        columns={["Channel", "Outcome"]}
        rows={[
          { key: "row-warn", tone: "warn", cells: ["UC-1", "CONFLICT"] },
          { key: "row-plain", cells: ["UC-2", "UNCHANGED"] },
        ]}
        emptyLabel="No differences"
      />,
    );

    const bodyRows = container.querySelectorAll("tbody tr");
    expect(bodyRows[0]).toHaveAttribute("data-tone", "warn");
    expect(bodyRows[1]).not.toHaveAttribute("data-tone");
  });

  it("renders the emptyLabel once inside the table body when rows is empty", () => {
    const { container } = render(
      <OutcomeTable columns={["Channel", "Outcome"]} rows={[]} emptyLabel="No differences found" />,
    );

    // One full-width message row — never an empty <tbody>.
    const bodyRows = container.querySelectorAll("tbody tr");
    expect(bodyRows).toHaveLength(1);
    expect(screen.getAllByText("No differences found")).toHaveLength(1);

    const cell = bodyRows[0].querySelector("td");
    expect(cell).toHaveAttribute("colspan", "2");
    expect(cell).toHaveTextContent("No differences found");
  });
});
