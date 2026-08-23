import { FormEvent, useState } from "react";
import { ArrowRight, GitCompare } from "lucide-react";
import { apiFetch, ApiClientError } from "../api/client";
import { ErrorState, PageTitle } from "../components/PageStates";

export function ComparePage() {
  const [left, setLeft] = useState(""); const [right, setRight] = useState(""); const [result, setResult] = useState<Record<string, unknown> | null>(null); const [error, setError] = useState<string | null>(null);
  async function compare(event: FormEvent) { event.preventDefault(); setError(null); setResult(null); try { setResult(await apiFetch<Record<string, unknown>>(`/api/v1/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`)); } catch (reason) { setError(reason instanceof ApiClientError ? reason.message : "Unable to compare runs"); } }
  return <section className="page-frame"><PageTitle eyebrow="Results" title="Compare" detail="Reward charts are only available for identical Benchmark and Dataset revisions." action={<GitCompare size={20} />} /><form className="compare-form" onSubmit={compare}><label>Left task<input value={left} onChange={(event) => setLeft(event.target.value)} required placeholder="Task ID" /></label><ArrowRight size={17} /><label>Right task<input value={right} onChange={(event) => setRight(event.target.value)} required placeholder="Task ID" /></label><button className="button button-primary">Compare</button></form>{error && <ErrorState message={error} />}{result && <div className="comparison-result"><h2>Comparable revisions</h2><pre className="code-block">{JSON.stringify(result, null, 2)}</pre></div>}</section>;
}
