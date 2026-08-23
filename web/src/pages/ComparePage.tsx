import { FormEvent, useState } from "react";
import { ArrowRight, GitCompare } from "lucide-react";
import { apiFetch, ApiClientError } from "../api/client";
import { ErrorState, PageTitle } from "../components/PageStates";

type Metrics = {
  case_count: number;
  success_count: number;
  success_rate: number | null;
  average_reward: number | null;
  average_steps: number | null;
  tokens: number | null;
  estimated_cost: string | null;
  token_cost_available: boolean;
  duration_seconds: number | null;
};

type Side = {
  id: string;
  status: string;
  benchmark_revision_id: string;
  dataset_version_id: string;
  metrics: Metrics;
};

type Comparison = {
  revision: { benchmark_revision_id: string; dataset_version_id: string };
  left: Side;
  right: Side;
};

export function ComparePage() {
  const [left, setLeft] = useState("");
  const [right, setRight] = useState("");
  const [result, setResult] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function compare(event: FormEvent) {
    event.preventDefault(); setError(null); setResult(null); setLoading(true);
    try {
      setResult(await apiFetch<Comparison>(`/api/v1/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`));
    } catch (reason) {
      setError(reason instanceof ApiClientError ? reason.message : "Unable to compare runs");
    } finally { setLoading(false); }
  }

  return <section className="page-frame">
    <PageTitle eyebrow="Results" title="Compare" detail="Reward metrics are available only for completed tasks with identical Benchmark and Dataset revisions." action={<GitCompare size={20} />} />
    <form className="compare-form" onSubmit={compare}>
      <label>Left task<input value={left} onChange={(event) => setLeft(event.target.value)} required placeholder="Task ID" /></label>
      <ArrowRight size={17} />
      <label>Right task<input value={right} onChange={(event) => setRight(event.target.value)} required placeholder="Task ID" /></label>
      <button className="button button-primary" disabled={loading}>{loading ? "Comparing…" : "Compare"}</button>
    </form>
    {error && <ErrorState message={error} />}
    {result && <ComparisonTable result={result} />}
  </section>;
}

function ComparisonTable({ result }: { result: Comparison }) {
  const rows: Array<[string, string, (side: Side) => string]> = [
    ["Status", "Terminal task state", (side) => side.status],
    ["Cases", "Final attempts", (side) => String(side.metrics.case_count)],
    ["Success", "Correct final attempts", (side) => `${side.metrics.success_count} · ${percent(side.metrics.success_rate)}`],
    ["Average reward", "Benchmark reward", (side) => number(side.metrics.average_reward, 3)],
    ["Average steps", "Executed Action count", (side) => number(side.metrics.average_steps, 2)],
    ["Tokens", "Agent + evaluator", (side) => side.metrics.token_cost_available ? String(side.metrics.tokens ?? 0) : "Not recorded"],
    ["Estimated cost", "Frozen pricing", (side) => side.metrics.token_cost_available ? `$${side.metrics.estimated_cost ?? "0"}` : "Not recorded"],
    ["Duration", "Wall clock", (side) => side.metrics.duration_seconds === null ? "Not recorded" : `${side.metrics.duration_seconds.toFixed(2)}s`],
  ];
  return <div className="comparison-result">
    <h2>Comparable revisions</h2>
    <p className="muted hash-line">Benchmark {result.revision.benchmark_revision_id} · Dataset {result.revision.dataset_version_id}</p>
    <div className="table-scroll"><table className="comparison-table">
      <thead><tr><th>Metric</th><th>{result.left.id}</th><th>{result.right.id}</th></tr></thead>
      <tbody>{rows.map(([label, detail, value]) => <tr key={label}><th scope="row">{label}<span>{detail}</span></th><td>{value(result.left)}</td><td>{value(result.right)}</td></tr>)}</tbody>
    </table></div>
  </div>;
}

function percent(value: number | null) { return value === null ? "Not recorded" : `${(value * 100).toFixed(1)}%`; }
function number(value: number | null, digits: number) { return value === null ? "Not recorded" : value.toFixed(digits); }
