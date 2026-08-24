import { FormEvent, useEffect, useMemo, useState } from "react";
import { Check, ChevronLeft, ChevronRight, LockKeyhole, Play } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { apiFetch, AgentSummary, ApiClientError, BenchmarkSummary, ModelSummary, PreflightResponse } from "../api/client";
import { EmptyState, ErrorState, LoadingState, PageTitle } from "../components/PageStates";

const steps = ["Scope", "Runtime", "Reliability", "Budget / review"];

export function NewEvaluationPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [benchmarks, setBenchmarks] = useState<BenchmarkSummary[]>([]);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [models, setModels] = useState<ModelSummary[]>([]);
  const [benchmark, setBenchmark] = useState("");
  const [agent, setAgent] = useState("");
  const [model, setModel] = useState("");
  const [cases, setCases] = useState("smoke-001");
  const [incidentIds, setIncidentIds] = useState<string[]>([]);
  const [allCases, setAllCases] = useState(false);
  const [maxSteps, setMaxSteps] = useState(32);
  const [maxStrLen, setMaxStrLen] = useState(100000);
  const [maxEntryReturn, setMaxEntryReturn] = useState(15);
  const [maxCases, setMaxCases] = useState("");
  const [maxTokens, setMaxTokens] = useState("");
  const [maxCost, setMaxCost] = useState("");
  const [agentParameterValues, setAgentParameterValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    let mounted = true;
    void Promise.all([
      apiFetch<BenchmarkSummary[]>("/api/v1/benchmarks"),
      apiFetch<AgentSummary[]>("/api/v1/agents"),
      apiFetch<ModelSummary[]>("/api/v1/models"),
    ]).then(([benchmarkRows, agentRows, modelRows]) => {
      if (!mounted) return;
      setBenchmarks(benchmarkRows); setAgents(agentRows); setModels(modelRows);
      const firstBenchmark = String(benchmarkRows[0]?.manifest.benchmark_id ?? "");
      setBenchmark(firstBenchmark);
      setCases(firstBenchmark === "secrl" ? "" : "smoke-001");
      setAgent(agentRows[0]?.id ?? "");
    }).catch((reason) => {
      if (mounted) setError(reason instanceof ApiClientError ? reason.message : "Unable to load evaluation configuration");
    }).finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, []);

  const caseIds = useMemo(
    () => cases.split(",").map((item) => item.trim()).filter(Boolean),
    [cases],
  );
  const selectedBenchmark = benchmarks.find((item) => String(item.manifest.benchmark_id) === benchmark);
  const isSecRL = benchmark === "secrl";
  const incidentCounts = selectedBenchmark?.dataset.incidents ?? {};
  const hasScope = allCases || caseIds.length > 0 || incidentIds.length > 0;
  const budget = useMemo(() => ({
    ...(maxCases ? { max_cases: Number(maxCases) } : {}),
    ...(maxTokens ? { max_tokens: Number(maxTokens) } : {}),
    ...(maxCost ? { max_cost: maxCost } : {}),
  }), [maxCases, maxCost, maxTokens]);
  const selectedAgent = agents.find((item) => item.id === agent);
  const parameterProperties = (
    selectedAgent?.manifest.parameter_schema as { properties?: Record<string, { type?: string }> } | undefined
  )?.properties ?? {};
  const agentParameters = useMemo(() => Object.fromEntries(
    Object.entries(agentParameterValues)
      .filter(([, value]) => value !== "")
      .map(([name, value]) => {
        const type = parameterProperties[name]?.type;
        if (type === "boolean") return [name, value === "true"];
        if (type === "integer" || type === "number") return [name, Number(value)];
        return [name, value];
      }),
  ), [agentParameterValues, parameterProperties]);
  const summary = useMemo(() => ({
    benchmark,
    agent,
    model: model || "No model (deterministic)",
    cases: caseIds,
    incidents: incidentIds.map((id) => `${id} (${incidentCounts[id] ?? 0} cases)`),
    allCases,
    maxSteps,
    maxStrLen,
    maxEntryReturn,
    maxCases: maxCases || "Unlimited",
    maxTokens: maxTokens || "Unlimited",
    maxCost: maxCost || "Unlimited",
    agentParameters,
  }), [agent, agentParameters, allCases, benchmark, caseIds, incidentCounts, incidentIds, maxCases, maxCost, maxEntryReturn, maxSteps, maxStrLen, maxTokens, model]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (step < steps.length - 1) { setStep((value) => value + 1); return; }
    setPending(true); setError(null);
    try {
      const preflightQuery = new URLSearchParams({ benchmark_id: benchmark, agent_revision_id: agent });
      if (model) preflightQuery.set("model_config_revision_id", model);
      const preflight = await apiFetch<PreflightResponse>(`/api/v1/preflight?${preflightQuery.toString()}`);
      const blocked = preflight.checks.find((check) => check.status === "missing");
      if (blocked) {
        setError(blocked.message);
        return;
      }
      const result = await apiFetch<{ run_id: string }>("/api/v1/tasks", { method: "POST", json: {
        name: `${benchmark} evaluation`, benchmark_id: benchmark,
        agent_revision_id: agent, model_config_revision_id: model || null,
        case_ids: caseIds, incident_ids: incidentIds, all_cases: allCases, budget, max_steps: maxSteps,
        max_str_len: maxStrLen, max_entry_return: maxEntryReturn,
        agent_parameters: agentParameters,
      } });
      navigate(`/runs/${result.run_id}`);
    } catch (reason) {
      setError(reason instanceof ApiClientError ? reason.message : "Unable to create evaluation");
    } finally { setPending(false); }
  }

  return <section className="page-frame"><PageTitle eyebrow="Queue a run" title="New evaluation" detail="Create a frozen TaskSpec with explicit scope, runtime, limits and budget." />
    {error && <ErrorState message={error} />}
    {loading ? <LoadingState label="Loading frozen configuration" /> : <>
      <div className="stepper" aria-label="Evaluation setup steps">{steps.map((label, index) => <div className={`step ${index <= step ? "step-active" : ""}`} key={label}><span>{index < step ? <Check size={13} /> : index + 1}</span>{label}</div>)}</div>
      <form className="form-panel evaluation-form" onSubmit={submit}>
        {step === 0 && <div className="form-grid"><label>Benchmark revision<select aria-label="Benchmark revision" value={benchmark} onChange={(event) => { setBenchmark(event.target.value); setIncidentIds([]); setAllCases(false); setCases(event.target.value === "secrl" ? "" : "smoke-001"); }} required>{benchmarks.map((item) => { const manifest = item.manifest as Record<string, unknown>; return <option value={String(manifest.benchmark_id)} key={String(manifest.benchmark_id)}>{String(manifest.name ?? manifest.benchmark_id)}</option>; })}</select><span className="field-hint">Dataset revisions are frozen when the task is queued.</span></label>{isSecRL && <><label className="span-2">Incident selection<select aria-label="Incident selection" multiple size={Math.min(Math.max(Object.keys(incidentCounts).length, 3), 8)} value={incidentIds} onChange={(event) => setIncidentIds(Array.from(event.target.selectedOptions, (option) => option.value))}>{Object.entries(incidentCounts).map(([id, count]) => <option value={id} key={id}>{id} · {count} cases</option>)}</select><span className="field-hint">Choose one or more complete Incidents; the backend freezes their Case IDs and Dataset hash.</span></label><label className="checkbox-field"><input type="checkbox" checked={allCases} onChange={(event) => { setAllCases(event.target.checked); if (event.target.checked) { setIncidentIds([]); setCases(""); } }} />Run the full Benchmark ({selectedBenchmark?.dataset.case_count ?? 0} cases)</label></>}{!allCases && <label>Case IDs<input value={cases} onChange={(event) => setCases(event.target.value)} placeholder="smoke-001, smoke-002" required={!isSecRL || incidentIds.length === 0} /><span className="field-hint">{isSecRL ? "Optional when Incidents are selected; enter one or more frozen Case IDs otherwise." : "Comma-separated IDs from the frozen DatasetVersion."}</span></label>}</div>}
        {step === 1 && (agents.length === 0 ? <EmptyState title="No registered Agent" detail="Register an allowlisted Agent before creating an evaluation." /> : <div className="form-grid"><label>Agent revision<select aria-label="Agent revision" value={agent} onChange={(event) => { setAgent(event.target.value); setAgentParameterValues({}); }} required>{agents.map((item) => <option value={item.id} key={item.id}>{item.name} · {item.kind}</option>)}</select><span className="field-hint">Uses the immutable database revision ID returned by the API.</span></label><label>Model revision (optional)<select aria-label="Model revision (optional)" value={model} onChange={(event) => setModel(event.target.value)}><option value="">No model (deterministic only)</option>{models.map((item) => <option value={item.id} key={item.id}>{item.name} · {item.model}</option>)}</select></label>{Object.entries(parameterProperties).map(([name, schema]) => <label key={name}>{name}{schema.type === "boolean" ? <select aria-label={name} value={agentParameterValues[name] ?? ""} onChange={(event) => setAgentParameterValues((current) => ({ ...current, [name]: event.target.value }))}><option value="">Use default</option><option value="true">true</option><option value="false">false</option></select> : <input aria-label={name} type={schema.type === "integer" || schema.type === "number" ? "number" : "text"} step={schema.type === "number" ? "any" : undefined} value={agentParameterValues[name] ?? ""} onChange={(event) => setAgentParameterValues((current) => ({ ...current, [name]: event.target.value }))} />}</label>)}</div>)}
        {step === 2 && <div className="form-grid"><label>Max steps<input type="number" min="1" value={maxSteps} onChange={(event) => setMaxSteps(Number(event.target.value))} /></label><label>Max observation string length<input type="number" min="1" value={maxStrLen} onChange={(event) => setMaxStrLen(Number(event.target.value))} /></label><label>Max entry return<input type="number" min="1" value={maxEntryReturn} onChange={(event) => setMaxEntryReturn(Number(event.target.value))} /></label></div>}
        {step === 3 && <div className="result-stack"><div className="form-grid"><label>Max cases (optional)<input type="number" min="1" value={maxCases} onChange={(event) => setMaxCases(event.target.value)} /></label><label>Max tokens (optional)<input type="number" min="1" value={maxTokens} onChange={(event) => setMaxTokens(event.target.value)} /></label><label>Max cost (optional)<input type="number" min="0" step="0.000001" value={maxCost} onChange={(event) => setMaxCost(event.target.value)} /></label></div><div className="review-summary"><LockKeyhole size={17} /><div><strong>Immutable run summary</strong><span>Limits are frozen into the RunSpec and cannot be changed by the agent.</span></div></div><dl className="summary-list">{Object.entries(summary).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{Array.isArray(value) ? value.join(", ") : String(value)}</dd></div>)}</dl></div>}
        <div className="form-actions">{step > 0 && <button type="button" className="button button-quiet" onClick={() => setStep((value) => value - 1)}><ChevronLeft size={15} />Back</button>}<button className="button button-primary" disabled={pending || !benchmark || !agent || !hasScope}>{step < steps.length - 1 ? <>Continue <ChevronRight size={15} /></> : <><Play size={15} />{pending ? "Queueing…" : "Queue evaluation"}</>}</button></div>
      </form>
    </>}
  </section>;
}
