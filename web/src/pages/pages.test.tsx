import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AgentsPage } from "./AgentsPage";
import { AnalysisReviewPage } from "./AnalysisReviewPage";
import { BenchmarksPage } from "./BenchmarksPage";
import { ComparePage } from "./ComparePage";
import { LoginPage } from "./LoginPage";
import { ModelsPage } from "./ModelsPage";
import { NewEvaluationPage } from "./NewEvaluationPage";
import { RunDetailPage } from "./RunDetailPage";
import { RunsPage } from "./RunsPage";

function renderPage(element: React.ReactNode) {
  return render(<MemoryRouter>{element}</MemoryRouter>);
}

describe("core operational pages", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
  });

  it("supports local admin login without echoing a password", async () => {
    const user = userEvent.setup();
    renderPage(<LoginPage />);
    await user.type(screen.getByLabelText("Username"), "admin");
    await user.type(screen.getByLabelText("Password"), "secret");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it.each([
    [ModelsPage, "Models"],
    [AgentsPage, "Agents"],
    [BenchmarksPage, "Benchmarks"],
    [NewEvaluationPage, "New evaluation"],
    [RunsPage, "Runs"],
    [AnalysisReviewPage, "Analysis & review"],
    [ComparePage, "Compare"],
  ])("renders %s with an operational heading", (Page, heading) => {
    renderPage(<Page />);
    expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
  });

  it("renders a run detail route without loading the whole trajectory", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ id: "run-1", task_id: "task-1", status: "QUEUED", checkpoint: 0, run_spec_sha256: "a".repeat(64) }), { status: 200 })));
    render(<MemoryRouter initialEntries={["/runs/run-1"]}><Routes><Route path="/runs/:id" element={<RunDetailPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "run-1" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Trajectory" })).toBeInTheDocument();
  });
});
