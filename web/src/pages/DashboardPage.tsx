import { Activity, ArrowUpRight, Clock3, ShieldCheck } from "lucide-react";
import { HealthBadge } from "../components/HealthBadge";
import { MetricValue } from "../components/MetricValue";

export function DashboardPage() {
  return <section className="page-frame">
    <header className="page-header"><div><div className="eyebrow">Overview</div><h1>Dashboard</h1><p className="lede">A quiet view of the local benchmark queue and recent outcomes.</p></div><HealthBadge status="healthy" /></header>
    <div className="metric-grid">
      <MetricValue value="—" label="Active tasks" detail="Updates while a task is running" />
      <MetricValue value="—" label="Completed runs" detail="Last 24 hours" />
      <MetricValue value="—" label="Average reward" detail="Frozen benchmark revisions" />
      <MetricValue value="—" label="Artifact integrity" detail="SHA-256 verified" />
    </div>
    <div className="split-grid">
      <section className="section-block"><div className="section-heading"><h2>Queue</h2><Activity size={17} /></div><div className="empty-state"><Clock3 size={20} /><p>No active tasks</p><span>New evaluations appear here once queued.</span></div></section>
      <section className="section-block"><div className="section-heading"><h2>Integrity</h2><ShieldCheck size={17} /></div><div className="signal-list"><div><span>Database schema</span><HealthBadge status="healthy" /></div><div><span>Artifact store</span><HealthBadge status="healthy" /></div><div><span>Runner lease</span><span className="muted">Idle</span></div></div><a className="text-link" href="/benchmarks">Review benchmark revisions <ArrowUpRight size={14} /></a></section>
    </div>
  </section>;
}
