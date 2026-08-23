import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AppShell } from "./AppShell";

describe("AppShell navigation", () => {
  it("exposes all operational destinations", () => {
    render(
      <MemoryRouter>
        <AppShell />
      </MemoryRouter>,
    );

    for (const label of [
      "Dashboard",
      "Models",
      "Agents",
      "Benchmarks",
      "New evaluation",
      "Runs",
      "Analysis & review",
      "Compare",
    ]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
  });
});
