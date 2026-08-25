import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiFetch, ApiClientError, type TaskSummary } from "../api/client";
import { EmptyState, ErrorState, LoadingState, PageTitle } from "../components/PageStates";
import { HealthBadge } from "../components/HealthBadge";

export function RunsPage() {
  const [tasks, setTasks] = useState<TaskSummary[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState<string | null>(null); const load = async () => { setLoading(true); try { setTasks(await apiFetch<TaskSummary[]>("/api/v1/tasks")); } catch (reason) { setError(reason instanceof ApiClientError ? reason.message : "Unable to load runs"); } finally { setLoading(false); } }; useEffect(() => { void load(); }, []);
  return <section className="page-frame"><PageTitle eyebrow="Queue and history" title="Runs" detail="Watch task state, checkpoints, and immutable run specification hashes." action={<Link className="button button-primary" to="/evaluations/new">New evaluation</Link>} />{error && <ErrorState message={error} onRetry={() => void load()} />}{loading ? <LoadingState label="Loading runs" /> : tasks.length === 0 ? <EmptyState title="No runs yet" detail="Create a Protocol-Smoke evaluation to populate the queue." /> : <div className="resource-list">{tasks.map((task) => <Link className="resource-row resource-link" key={task.id} to={`/runs/${task.run_id ?? task.id}`}><div className="resource-main"><strong>{task.name}</strong><span>{task.id}</span><code>{task.task_spec_sha256.slice(0, 16)}…</code>{task.scope && <span>{formatScope(task.scope)}</span>}</div><div className="resource-meta"><HealthBadge status={task.status} /></div></Link>)}</div>}</section>;
}

function formatScope(scope: NonNullable<TaskSummary["scope"]>): string {
  const cases = `${scope.case_count} ${scope.case_count === 1 ? "Case" : "Cases"}`;
  const incidents = `${scope.incident_count} ${scope.incident_count === 1 ? "Incident" : "Incidents"}`;
  return `${cases} across ${incidents}${scope.legacy ? " · legacy scope inferred" : ""}`;
}
