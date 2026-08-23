import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AgentsPage } from "./AgentsPage";
import { AnalysisReviewPage } from "./AnalysisReviewPage";
import { BenchmarksPage } from "./BenchmarksPage";
import { ComparePage } from "./ComparePage";
import { DashboardPage } from "./DashboardPage";
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

  it("shows queue metrics from the task endpoint", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([
      { id: "task-1", name: "Smoke", status: "RUNNING", task_spec: {}, task_spec_sha256: "a".repeat(64) },
      { id: "task-2", name: "Done", status: "SUCCEEDED", task_spec: {}, task_spec_sha256: "b".repeat(64) },
    ]), { status: 200 })));
    renderPage(<DashboardPage />);
    expect((await screen.findAllByText("1")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Active tasks")).toBeInTheDocument();
    expect(screen.getByText("Completed runs")).toBeInTheDocument();
  });

  it("renders a run detail route without loading the whole trajectory", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ id: "run-1", task_id: "task-1", status: "QUEUED", checkpoint: 0, run_spec_sha256: "a".repeat(64) }), { status: 200 })));
    render(<MemoryRouter initialEntries={["/runs/run-1"]}><Routes><Route path="/runs/:id" element={<RunDetailPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "run-1" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Trajectory" })).toBeInTheDocument();
  });

  it("lazy-loads one trajectory step and public artifacts from the API", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith("/runs/run-1")) {
        return new Response(JSON.stringify({ id: "run-1", task_id: "task-1", status: "SUCCEEDED", checkpoint: 1, run_spec_sha256: "a".repeat(64) }), { status: 200 });
      }
      if (path.endsWith("/runs/run-1/cases")) {
        return new Response(JSON.stringify([{ case_id: "smoke-001", attempt_id: "attempt-1", status: "SUCCEEDED", trajectory_artifact: { id: "artifact-1", sha256: "b".repeat(64) } }]), { status: 200 });
      }
      if (path.includes("/cases/smoke-001/trajectory?step=0")) {
        return new Response(JSON.stringify({ step: 0, total_steps: 2, artifact_sha256: "b".repeat(64), exchange: { action: { type: "tool_call", tool: "echo" }, observation: { ok: true } } }), { status: 200 });
      }
      if (path.endsWith("/runs/run-1/artifacts")) {
        return new Response(JSON.stringify([{ id: "artifact-1", kind: "trajectory", sha256: "b".repeat(64), size_bytes: 128, download_url: "/api/v1/artifacts/artifact-1" }]), { status: 200 });
      }
      throw new Error(`unexpected request ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/runs/run-1"]}><Routes><Route path="/runs/:id" element={<RunDetailPage />} /></Routes></MemoryRouter>);
    await screen.findByRole("heading", { name: "run-1" });

    await user.click(screen.getByRole("tab", { name: "Trajectory" }));

    expect(await screen.findByText("Step 1 of 2")).toBeInTheDocument();
    expect(screen.getByText(/tool_call/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining("step=1"), expect.anything());
    await user.click(screen.getByRole("tab", { name: "Artifacts" }));
    expect(await screen.findByRole("link", { name: /trajectory/ })).toHaveAttribute("href", "/api/v1/artifacts/artifact-1");
  });

  it("loads automatic attribution and appends a HumanReview revision", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/runs/run-1/analysis")) {
        return new Response(JSON.stringify([{ id: "analysis-1", revision: 1, taxonomy_version: "taxonomy_v1", output_manifest_sha256: "c".repeat(64) }]), { status: 200 });
      }
      if (path.endsWith("/runs/run-1/attributions")) {
        return new Response(JSON.stringify([{ id: "attribution-1", case_id: "smoke-001", label: "ANSWER", taxonomy: "taxonomy_v1", confidence: 0.75, evidence: ["trajectory:step:0"] }]), { status: 200 });
      }
      if (path.endsWith("/attributions/attribution-1/reviews") && init?.method === "POST") {
        return new Response(JSON.stringify({ id: "review-1", revision: 1 }), { status: 201 });
      }
      if (path.endsWith("/attributions/attribution-1/reviews")) {
        return new Response(JSON.stringify([]), { status: 200 });
      }
      throw new Error(`unexpected request ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPage(<AnalysisReviewPage />);
    await user.type(screen.getByLabelText("Run ID"), "run-1");
    await user.click(screen.getByRole("button", { name: "Load analysis" }));
    expect(await screen.findByText("ANSWER")).toBeInTheDocument();
    expect(screen.getByText("taxonomy_v1")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Review ANSWER/ }));
    await user.type(screen.getByLabelText("Primary label"), "ANSWER");
    await user.click(screen.getByRole("button", { name: "Append review revision" }));
    expect(await screen.findByText("Review revision appended to the audit history.")).toBeInTheDocument();
  });

  it("creates an evaluation with registered runtime IDs and no accidental one-case exhaustion", async () => {
    let taskBody: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/agents")) return new Response(JSON.stringify([{
        id: "agent-db-1",
        name: "Deterministic smoke",
        kind: "BUILT_IN",
        sha256: "a".repeat(64),
        manifest: {
          agent_id: "builtin-deterministic-smoke-v1",
          parameter_schema: { properties: { retry_num: { type: "integer" } } },
        },
      }]), { status: 200 });
      if (path.endsWith("/models")) return new Response(JSON.stringify([]), { status: 200 });
      if (path.endsWith("/benchmarks")) return new Response(JSON.stringify([{ manifest: { benchmark_id: "protocol-smoke", name: "Protocol Smoke" }, dataset: { case_count: 1 } }]), { status: 200 });
      if (path.endsWith("/tasks") && init?.method === "POST") {
        taskBody = JSON.parse(String(init.body));
        return new Response(JSON.stringify({ run_id: "run-created" }), { status: 201 });
      }
      throw new Error(`unexpected request ${path}`);
    }));
    const user = userEvent.setup();
    renderPage(<NewEvaluationPage />);
    expect(await screen.findByLabelText("Benchmark revision")).toHaveValue("protocol-smoke");
    await user.click(screen.getByRole("button", { name: /Continue/ }));
    expect(screen.getByLabelText("Agent revision")).toHaveValue("agent-db-1");
    await user.type(screen.getByLabelText("retry_num"), "2");
    await user.click(screen.getByRole("button", { name: /Continue/ }));
    await user.click(screen.getByRole("button", { name: /Continue/ }));
    await user.click(screen.getByRole("button", { name: /Queue evaluation/ }));
    expect(taskBody).toMatchObject({ agent_revision_id: "agent-db-1", agent_parameters: { retry_num: 2 }, budget: {} });
  });

  it("does not claim an Agent Service is healthy before a real manifest check", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/agents") && !init?.method) return new Response(JSON.stringify([{ id: "service-1", name: "Reference", kind: "SERVICE", endpoint: "http://agent-service-reference:8081", sha256: "a".repeat(64), manifest: {} }]), { status: 200 });
      if (path.endsWith("/agents/service-1:check") && init?.method === "POST") return new Response(JSON.stringify({ status: "valid" }), { status: 200 });
      throw new Error(`unexpected request ${path}`);
    }));
    const user = userEvent.setup();
    renderPage(<AgentsPage />);
    expect(await screen.findByText("Unchecked")).toBeInTheDocument();
    expect(screen.queryByText("healthy")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Check Reference" }));
    expect(await screen.findByText("valid")).toBeInTheDocument();
  });
});
