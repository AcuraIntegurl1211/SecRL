import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Activity, ArrowUpRight, Clock3, ShieldCheck } from "lucide-react";
import { apiFetch, ApiClientError, type TaskSummary } from "../api/client";
import { ErrorState } from "../components/PageStates";
import { HealthBadge } from "../components/HealthBadge";
import { MetricValue } from "../components/MetricValue";

export function DashboardPage() {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [apiHealth, setApiHealth] = useState<"ok" | "error" | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let mounted = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const [next, health] = await Promise.all([
          apiFetch<TaskSummary[]>("/api/v1/tasks"),
          apiFetch<{ status: string }>("/api/v1/health"),
        ]);
        if (!mounted) return;
        setTasks(next);
        setApiHealth(health.status === "ok" ? "ok" : "error");
        if (next.some((task) => ["QUEUED", "RUNNING", "PAUSED", "PAUSE_REQUESTED", "INTERRUPTED"].includes(task.status))) {
          timer = setTimeout(() => void load(), 5000);
        }
      } catch (reason) {
        if (mounted) { setApiHealth("error"); setError(reason instanceof ApiClientError ? reason.message : "Unable to load dashboard"); }
      }
    };
    void load();
    return () => { mounted = false; if (timer) clearTimeout(timer); };
  }, []);
  const active = (tasks ?? []).filter((task) => ["QUEUED", "RUNNING", "PAUSED", "PAUSE_REQUESTED", "INTERRUPTED"].includes(task.status));
  const completed = (tasks ?? []).filter((task) => task.status === "SUCCEEDED").length;
  const metric = (value: number) => tasks === null ? "…" : value;
  return <section className="page-frame">
    <header className="page-header"><div><div className="eyebrow">Overview</div><h1>Dashboard</h1><p className="lede">A quiet view of the local benchmark queue and recent outcomes.</p></div><HealthBadge status={apiHealth === null ? "Checking" : apiHealth === "ok" ? "healthy" : "degraded"} /></header>
    <div className="metric-grid">
      <MetricValue value={metric(active.length)} label="Active tasks" detail="Updates while a task is running" />
      <MetricValue value={metric(completed)} label="Completed runs" detail="Last 24 hours" />
      <MetricValue value="—" label="Average reward" detail="Frozen benchmark revisions" />
      <MetricValue value="—" label="Artifact integrity" detail="SHA-256 verified" />
    </div>
    {error && <ErrorState message={error} />}
    <div className="split-grid">
      <section className="section-block"><div className="section-heading"><h2>Queue</h2><Activity size={17} /></div>{tasks === null ? <div className="empty-state"><Clock3 size={20} /><p>Loading queue</p><span>Checking the local runner.</span></div> : active.length === 0 ? <div className="empty-state"><Clock3 size={20} /><p>No active tasks</p><span>New evaluations appear here once queued.</span></div> : <div className="resource-list">{active.slice(0, 5).map((task) => <Link className="resource-row resource-link" key={task.id} to={`/runs/${task.run_id ?? task.id}`}><div className="resource-main"><strong>{task.name}</strong><span>{task.id}</span></div><HealthBadge status={task.status} /></Link>)}</div>}</section>
      <section className="section-block"><div className="section-heading"><h2>Integrity</h2><ShieldCheck size={17} /></div><div className="signal-list"><div><span>API and migrated schema</span><HealthBadge status={apiHealth === "ok" ? "healthy" : apiHealth === null ? "Checking" : "degraded"} /></div><div><span>Artifact store</span><span className="muted">SHA-256 on access</span></div><div><span>Runner activity</span><span className="muted">{active.length ? `${active.length} active` : "Idle"}</span></div></div><a className="text-link" href="/benchmarks">Review benchmark revisions <ArrowUpRight size={14} /></a></section>
    </div>
  </section>;
}
