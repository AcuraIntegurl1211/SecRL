import { render, screen } from "@testing-library/react";
import { HealthBadge } from "./HealthBadge";

describe("HealthBadge task status tones", () => {
  it.each([
    ["SUCCEEDED", "health-healthy"],
    ["RUNNING", "health-degraded"],
    ["QUEUED", "health-degraded"],
    ["FAILED", "health-offline"],
    ["BUDGET_EXHAUSTED", "health-offline"],
    ["PAUSED", "health-offline"],
    ["INTERRUPTED", "health-offline"],
    ["CANCELED", "health-offline"],
  ])("renders %s with the expected tone", (status, tone) => {
    render(<HealthBadge status={status} />);
    const badge = screen.getByText(status);
    expect(badge.className).toContain(tone);
  });

  it("keeps infrastructure tones intact", () => {
    render(<HealthBadge status="healthy" />);
    expect(screen.getByText("healthy").className).toContain("health-healthy");
    render(<HealthBadge status="degraded" />);
    expect(screen.getByText("degraded").className).toContain("health-degraded");
    render(<HealthBadge status="offline" />);
    expect(screen.getByText("offline").className).toContain("health-offline");
  });
});
