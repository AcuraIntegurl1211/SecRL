import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiFetch, ApiClientError } from "../api/client";
import { EmptyState, ErrorState, LoadingState, PageTitle } from "../components/PageStates";
import { HealthBadge } from "../components/HealthBadge";
import { MetricValue } from "../components/MetricValue";

type RunFailure = { code: string; retryable?: boolean; usage_may_have_occurred?: boolean; response_shape?: string };
type Run = { id: string; task_id: string; status: string; checkpoint: number; run_spec_sha256: string; failure?: RunFailure };
type RunProgress = { run_id: string; task_id: string; task_status: string; frozen_case_count: number; completed: number; failed: number; correct: number; reward_sum: number | null; average_reward: number | null; tokens: { agent: number; evaluator: number; total: number }; estimated_cost: string; budget: { max_tokens: number | null; max_cost: string | null; max_cases: number | null }; elapsed_seconds: number | null; current_case_index: number };
type Artifact = { id: string; kind: string; sha256: string; size_bytes: number; download_url: string };
type CaseAttempt = { case_id: string; attempt_id: string; status: string; metrics: Record<string, unknown>; trajectory_artifact: Artifact | null };
type TrajectoryStep = { step: number; total_steps: number; artifact_sha256: string; exchange: Record<string, unknown> };
type Analysis = { id: string; revision: number; taxonomy_version: string; output_manifest_sha256: string };
type Attribution = { id: string; case_id: string; label: string; taxonomy: string; confidence: number; explanation?: string; evidence: string[] };
type AuditEvent = { id: string; created_at: string; action: string; entity_type: string; payload: Record<string, unknown> };

const tabs = ["Overview", "Cases", "Trajectory", "Analysis", "Artifacts", "Audit"] as const;
type Tab = typeof tabs[number];

export function RunDetailPage() {
  const { id = "" } = useParams();
  const [run, setRun] = useState<Run | null>(null);
  const [tab, setTab] = useState<Tab>("Overview");
  const [cases, setCases] = useState<CaseAttempt[] | null>(null);
  const [trajectoryCase, setTrajectoryCase] = useState("");
  const [trajectory, setTrajectory] = useState<TrajectoryStep | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[] | null>(null);
  const [analyses, setAnalyses] = useState<Analysis[] | null>(null);
  const [attributions, setAttributions] = useState<Attribution[] | null>(null);
  const [audit, setAudit] = useState<AuditEvent[] | null>(null);
  const [loadingTab, setLoadingTab] = useState(false);
  const [progress, setProgress] = useState<RunProgress | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    void apiFetch<Run>(`/api/v1/runs/${id}`)
      .then(setRun)
      .catch((reason) => setError(message(reason, "Unable to load run")));
  }, [id]);

  useEffect(() => {
    if (!id) return undefined;
    let mounted = true;
    const poll = () => {
      void apiFetch<RunProgress>(`/api/v1/runs/${id}/progress`)
        .then((next) => { if (mounted) setProgress(next); })
        .catch(() => { if (mounted) setProgress(null); });
    };
    poll();
    const timer = setInterval(poll, 5000);
    return () => { mounted = false; clearInterval(timer); };
  }, [id]);

  async function loadCases(): Promise<CaseAttempt[]> {
    if (cases !== null) return cases;
    const loaded = await apiFetch<CaseAttempt[]>(`/api/v1/runs/${id}/cases`);
    setCases(loaded);
    return loaded;
  }

  async function loadTrajectory(caseId: string, step: number) {
    setLoadingTab(true);
    try {
      const loaded = await apiFetch<TrajectoryStep>(
        `/api/v1/runs/${id}/cases/${encodeURIComponent(caseId)}/trajectory?step=${step}`,
      );
      setTrajectoryCase(caseId);
      setTrajectory(loaded);
    } catch (reason) {
      setError(message(reason, "Unable to load trajectory step"));
    } finally {
      setLoadingTab(false);
    }
  }

  async function selectTab(next: Tab) {
    setTab(next);
    setError(null);
    setLoadingTab(true);
    try {
      if (next === "Cases") await loadCases();
      if (next === "Trajectory" && trajectory === null) {
        const loadedCases = await loadCases();
        const first = loadedCases.find((item) => item.trajectory_artifact !== null);
        if (first) await loadTrajectory(first.case_id, 0);
      }
      if (next === "Artifacts" && artifacts === null) {
        setArtifacts(await apiFetch<Artifact[]>(`/api/v1/runs/${id}/artifacts`));
      }
      if (next === "Analysis" && analyses === null) {
        const [history, automatic] = await Promise.all([
          apiFetch<Analysis[]>(`/api/v1/runs/${id}/analysis`),
          apiFetch<Attribution[]>(`/api/v1/runs/${id}/attributions`),
        ]);
        setAnalyses(history);
        setAttributions(automatic);
      }
      if (next === "Audit" && audit === null) {
        setAudit(await apiFetch<AuditEvent[]>(`/api/v1/runs/${id}/audit`));
      }
    } catch (reason) {
      setError(message(reason, `Unable to load ${next.toLowerCase()}`));
    } finally {
      setLoadingTab(false);
    }
  }

  async function transition(action: "pause" | "resume" | "cancel") {
    if (action === "cancel" && !window.confirm("Cancel this run? Completed cases will be preserved.")) return;
    try {
      const next = await apiFetch<{ status: string }>(`/api/v1/runs/${id}:${action}`, { method: "POST" });
      setRun((value) => value ? { ...value, status: next.status } : value);
    } catch (reason) {
      setError(message(reason, "Run state transition failed"));
    }
  }

  if (error && !run) return <section className="page-frame"><ErrorState message={error} /></section>;
  if (!run) return <section className="page-frame"><LoadingState label="Loading run" /></section>;
  const trajectoryCases = (cases ?? []).filter((item) => item.trajectory_artifact !== null);

  return <section className="page-frame">
    <PageTitle eyebrow="Run detail" title={run.id.slice(0, 12)} detail={`Checkpoint ${run.checkpoint} · RunSpec ${run.run_spec_sha256.slice(0, 16)}…`} action={<HealthBadge status={run.status} />} />
    <div className="toolbar">
      <button className="button button-quiet" onClick={() => void transition("pause")} disabled={!['QUEUED', 'RUNNING'].includes(run.status)}>Pause</button>
      <button className="button button-quiet" onClick={() => void transition("resume")} disabled={run.status !== 'PAUSED'}>Resume</button>
      <button className="button button-danger" onClick={() => void transition("cancel")} disabled={['SUCCEEDED', 'FAILED', 'CANCELED'].includes(run.status)}>Cancel</button>
    </div>
    {progress && progress.budget && typeof progress.frozen_case_count === "number" && <div className="metric-grid" aria-label="Live run progress">
      <MetricValue value={`${progress.completed}/${progress.frozen_case_count}`} label="Cases completed" detail={progress.failed > 0 ? `${progress.failed} failed` : "Updates every 5 s"} />
      <MetricValue value={progress.correct} label="Correct answers" detail={`${progress.frozen_case_count} frozen in scope`} />
      <MetricValue value={progress.average_reward ?? "—"} label="Average reward" detail={progress.reward_sum !== null ? `sum ${Number(progress.reward_sum.toFixed(3))}` : "no scored cases yet"} />
      <MetricValue value={(progress.tokens?.total ?? 0).toLocaleString()} label="Tokens" detail={`agent ${(progress.tokens?.agent ?? 0).toLocaleString()} · evaluator ${(progress.tokens?.evaluator ?? 0).toLocaleString()}`} />
      <MetricValue value={`$${Number(progress.estimated_cost ?? 0).toFixed(4)}`} label="Estimated cost" detail={progress.budget.max_cost !== null && progress.budget.max_cost !== undefined ? `cap $${progress.budget.max_cost}` : "no cost cap"} />
      <MetricValue value={typeof progress.elapsed_seconds === "number" ? formatDuration(progress.elapsed_seconds) : "—"} label="Elapsed" detail={`checkpoint ${run.checkpoint}`} />
    </div>}
    <div className="tabs" role="tablist">{tabs.map((name) => <button key={name} role="tab" aria-selected={tab === name} className={tab === name ? "tab-active" : ""} onClick={() => void selectTab(name)}>{name}</button>)}</div>
    <div className="tab-panel">
      {loadingTab && <LoadingState label={`Loading ${tab.toLowerCase()}`} />}
      {!loadingTab && tab === "Overview" && <dl className="summary-list"><div><dt>Status</dt><dd>{run.status}</dd></div><div><dt>Task</dt><dd>{run.task_id}</dd></div>{run.failure && <div><dt>Failure code</dt><dd>{run.failure.code}{run.failure.retryable === true ? " · safe to retry" : run.failure.usage_may_have_occurred ? " · provider usage may have occurred; manual retry required" : ""}</dd></div>}</dl>}
      {!loadingTab && tab === "Cases" && (cases?.length ? <pre className="code-block">{JSON.stringify(cases, null, 2)}</pre> : <EmptyState title="No case attempts" detail="The Runner has not committed a case attempt." />)}
      {!loadingTab && tab === "Trajectory" && (trajectory ? <div className="result-stack"><label>Case<select value={trajectoryCase} onChange={(event) => void loadTrajectory(event.target.value, 0)}>{trajectoryCases.map((item) => <option key={item.attempt_id} value={item.case_id}>{item.case_id}</option>)}</select></label><div className="toolbar"><button className="button button-quiet" disabled={trajectory.step === 0} onClick={() => void loadTrajectory(trajectoryCase, trajectory.step - 1)}>Previous step</button><span className="muted">Step {trajectory.step + 1} of {trajectory.total_steps}</span><button className="button button-quiet" disabled={trajectory.step + 1 >= trajectory.total_steps} onClick={() => void loadTrajectory(trajectoryCase, trajectory.step + 1)}>Next step</button></div><pre className="code-block">{JSON.stringify(trajectory.exchange, null, 2)}</pre><code className="hash-line">SHA-256 {trajectory.artifact_sha256}</code></div> : <EmptyState title="No trajectory" detail="No final case attempt has a public trajectory artifact." />)}
      {!loadingTab && tab === "Analysis" && <div className="result-stack">{analyses?.length ? analyses.map((item) => <article className="resource-row" key={item.id}><div className="resource-main"><strong>{item.taxonomy_version}</strong><span>Revision {item.revision}</span><code>{item.output_manifest_sha256}</code></div></article>) : <EmptyState title="No analysis run" detail="Run failure analysis to produce versioned attribution." />}{attributions?.map((item) => <article className="resource-row" key={item.id}><div className="resource-main"><strong>{item.label}</strong><span>{item.case_id} · confidence {item.confidence}</span>{item.explanation && <span className="muted">{item.explanation}</span>}<code>{item.taxonomy}</code></div></article>)}<Link className="button button-primary" to={`/analysis?run_id=${encodeURIComponent(id)}`}>Open review workspace</Link></div>}
      {!loadingTab && tab === "Artifacts" && (artifacts?.length ? <div className="resource-list">{artifacts.map((artifact) => <a className="resource-row resource-link" href={artifact.download_url} key={artifact.id}><div className="resource-main"><strong>{artifact.kind}</strong><span>{artifact.size_bytes} bytes</span><code>{artifact.sha256}</code></div></a>)}</div> : <EmptyState title="No public artifacts" detail="Restricted evaluator artifacts are never exposed here." />)}
      {!loadingTab && tab === "Audit" && (audit?.length ? <pre className="code-block">{JSON.stringify(audit, null, 2)}</pre> : <EmptyState title="No audit events" detail="Human review revisions will appear here." />)}
    </div>
    {error && <ErrorState message={error} />}
  </section>;
}

function message(reason: unknown, fallback: string) {
  return reason instanceof ApiClientError ? reason.message : fallback;
}

function formatDuration(seconds: number) {
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}:${String(s).padStart(2, "0")}`;
}
