import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, Database, FileCheck2, ListTree } from "lucide-react";
import { apiFetch, ApiClientError, type BenchmarkSummary } from "../api/client";
import { EmptyState, ErrorState, LoadingState, PageTitle } from "../components/PageStates";

type PublicCase = {
  id: string;
  scenario_id: string;
  ordinal: number;
  public_input_sha256: string;
  public_input: { question?: string; context?: string; [key: string]: unknown };
};

type CasePage = {
  benchmark_id: string;
  dataset_sha256: string;
  total: number;
  offset: number;
  limit: number;
  items: PublicCase[];
};

export function BenchmarksPage() {
  const [items, setItems] = useState<BenchmarkSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState("");
  const [cases, setCases] = useState<CasePage | null>(null);
  const [casesLoading, setCasesLoading] = useState(false);
  const [casesError, setCasesError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true); setError(null);
    try { setItems(await apiFetch<BenchmarkSummary[]>("/api/v1/benchmarks")); }
    catch (reason) { setError(message(reason, "Unable to load benchmarks")); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);

  async function loadCases(benchmarkId: string, offset = 0) {
    setSelected(benchmarkId); setCasesLoading(true); setCasesError(null);
    try {
      setCases(await apiFetch<CasePage>(`/api/v1/benchmarks/${encodeURIComponent(benchmarkId)}/cases?offset=${offset}&limit=25`));
    } catch (reason) { setCasesError(message(reason, "Unable to load public questions")); }
    finally { setCasesLoading(false); }
  }

  return <section className="page-frame">
    <PageTitle eyebrow="Frozen inputs" title="Benchmarks" detail="Benchmark revisions, DatasetVersions, Incident scope and public question integrity." />
    {error && <ErrorState message={error} onRetry={() => void load()} />}
    {loading ? <LoadingState label="Loading benchmark revisions" /> : items.length === 0 ? <EmptyState title="No benchmark revisions" detail="Platform adapters will appear after database initialization." /> : <div className="resource-list">{items.map((item) => {
      const manifest = item.manifest as Record<string, unknown>;
      const dataset = item.dataset as Record<string, unknown>;
      const benchmarkId = String(manifest.benchmark_id);
      const name = String(manifest.name ?? benchmarkId);
      return <article className="resource-row" key={benchmarkId}>
        <div className="resource-main"><strong><Database size={15} /> {name}</strong><span>Revision {String(manifest.version ?? "—")} · Dataset {String(dataset.version ?? "—")} · split {String(dataset.split ?? "—")}</span><code>{String(dataset.sha256 ?? "")}</code></div>
        <div className="resource-meta"><span className="secure-state"><FileCheck2 size={14} />{String(dataset.case_count ?? "—")} cases</span><button className="button button-quiet" aria-label={`Browse ${name} questions`} onClick={() => void loadCases(benchmarkId)}><ListTree size={14} />Browse</button></div>
      </article>;
    })}</div>}
    {selected && <section className="section-block benchmark-browser" aria-live="polite">
      <div className="section-heading"><div><h2>Public questions</h2><p className="muted">{selected} · gold and evaluator-private fields are excluded</p></div>{cases && <span className="muted">{cases.total} total</span>}</div>
      {casesError && <ErrorState message={casesError} onRetry={() => void loadCases(selected, cases?.offset ?? 0)} />}
      {casesLoading ? <LoadingState label="Loading public questions" /> : cases?.items.length === 0 ? <EmptyState title="No questions in this scope" detail="Choose another Benchmark or Incident scope." /> : <div className="result-stack">{cases?.items.map((item) => <article className="question-row" key={item.id}><div><strong>{String(item.public_input.question ?? "Untitled question")}</strong><span>{item.scenario_id} · question {item.ordinal + 1}</span></div>{item.public_input.context && <p>{String(item.public_input.context)}</p>}<code>{item.public_input_sha256}</code></article>)}</div>}
      {cases && <div className="form-actions"><button className="button button-quiet" disabled={casesLoading || cases.offset === 0} onClick={() => void loadCases(selected, Math.max(0, cases.offset - cases.limit))}><ChevronLeft size={14} />Previous</button><span className="muted">{cases.total === 0 ? 0 : cases.offset + 1}–{Math.min(cases.total, cases.offset + cases.items.length)} of {cases.total}</span><button className="button button-quiet" disabled={casesLoading || cases.offset + cases.limit >= cases.total} onClick={() => void loadCases(selected, cases.offset + cases.limit)}>Next<ChevronRight size={14} /></button></div>}
    </section>}
  </section>;
}

function message(reason: unknown, fallback: string) {
  return reason instanceof ApiClientError ? reason.message : fallback;
}
