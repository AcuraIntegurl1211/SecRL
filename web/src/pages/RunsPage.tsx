import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiFetch, ApiClientError, type TaskSummary } from "../api/client";
import { EmptyState, ErrorState, LoadingState, PageTitle } from "../components/PageStates";
import { HealthBadge } from "../components/HealthBadge";

const ACTIVE_STATUSES = ["QUEUED", "RUNNING", "PAUSED", "PAUSE_REQUESTED", "INTERRUPTED"];

type LiveProgress = { completed: number; frozen_case_count: number; average_reward: number | null; estimated_cost: string };

export function RunsPage() {
  const [tasks, setTasks] = useState<TaskSummary[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState<string | null>(null); const [live, setLive] = useState<Record<string, LiveProgress>>({}); const load = async () => { setLoading(true); try { setTasks(await apiFetch<TaskSummary[]>("/api/v1/tasks")); } catch (reason) { setError(reason instanceof ApiClientError ? reason.message : "Unable to load runs"); } finally { setLoading(false); } }; useEffect(() => { void load(); }, []);
  useEffect(() => {
    const active = tasks.filter((task) => ACTIVE_STATUSES.includes(task.status) && task.run_id);
    if (active.length === 0) return undefined;
    let mounted = true;
    const poll = () => {
      void Promise.all(active.map(async (task) => {
        try { return [task.id, await apiFetch<LiveProgress>(`/api/v1/runs/${task.run_id}/progress`)] as const; }
        catch { return [task.id, null] as const; }
      })).then((entries) => {
        if (!mounted) return;
        const next: Record<string, LiveProgress> = {};
        for (const [taskId, payload] of entries) { if (payload !== null) next[taskId] = payload; }
        setLive(next);
      });
    };
    poll();
    const timer = setInterval(poll, 5000);
    return () => { mounted = false; clearInterval(timer); };
  }, [tasks]);
  return <section className="page-frame"><PageTitle eyebrow="Queue and history" title="Runs" detail="Watch task state, checkpoints, and immutable run specification hashes." action={<Link className="button button-primary" to="/evaluations/new">New evaluation</Link>} />{error && <ErrorState message={error} onRetry={() => void load()} />}{loading ? <LoadingState label="Loading runs" /> : tasks.length === 0 ? <EmptyState title="No runs yet" detail="Create a Protocol-Smoke evaluation to populate the queue." /> : <div className="resource-list">{tasks.map((task) => { const progress = live[task.id]; return <Link className="resource-row resource-link" key={task.id} to={`/runs/${task.run_id ?? task.id}`}><div className="resource-main"><strong>{task.name}</strong><span>{task.id}</span><code>{task.task_spec_sha256.slice(0, 16)}…</code>{task.scope && <span>{formatScope(task.scope)}</span>}</div><div className="resource-meta">{progress && <span className="muted">{progress.completed}/{progress.frozen_case_count} · reward {progress.average_reward ?? "—"} · ${Number(progress.estimated_cost).toFixed(4)}</span>}<HealthBadge status={task.status} /></div></Link>; })}</div>}</section>;
}

function formatScope(scope: NonNullable<TaskSummary["scope"]>): string {
  const cases = `${scope.case_count} ${scope.case_count === 1 ? "Case" : "Cases"}`;
  const incidents = `${scope.incident_count} ${scope.incident_count === 1 ? "Incident" : "Incidents"}`;
  return `${cases} across ${incidents}${scope.legacy ? " · legacy scope inferred" : ""}`;
}
