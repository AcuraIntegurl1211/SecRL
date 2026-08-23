import { FormEvent, useState } from "react";
import { apiFetch, ApiClientError } from "../api/client";
import { EmptyState, ErrorState, LoadingState, PageTitle } from "../components/PageStates";

type Analysis = { id: string; revision: number; taxonomy_version: string; output_manifest_sha256: string };
type Attribution = { id: string; case_id: string; label: string; taxonomy: string; confidence: number; evidence: string[] };
type Review = { id: string; revision: number; primary: string; confidence: string; prior_review_id: string | null; notes: string };

export function AnalysisReviewPage() {
  const [runId, setRunId] = useState(() => new URLSearchParams(window.location.search).get("run_id") ?? "");
  const [analyses, setAnalyses] = useState<Analysis[] | null>(null);
  const [attributions, setAttributions] = useState<Attribution[] | null>(null);
  const [attributionId, setAttributionId] = useState("");
  const [reviews, setReviews] = useState<Review[]>([]);
  const [primary, setPrimary] = useState("");
  const [confidence, setConfidence] = useState("medium");
  const [notes, setNotes] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function loadAnalysis(event?: FormEvent) {
    event?.preventDefault();
    setLoading(true); setError(null); setMessage(null);
    try {
      const [history, automatic] = await Promise.all([
        apiFetch<Analysis[]>(`/api/v1/runs/${encodeURIComponent(runId)}/analysis`),
        apiFetch<Attribution[]>(`/api/v1/runs/${encodeURIComponent(runId)}/attributions`),
      ]);
      setAnalyses(history); setAttributions(automatic);
    } catch (reason) {
      setError(errorMessage(reason, "Unable to load analysis"));
    } finally {
      setLoading(false);
    }
  }

  async function selectAttribution(item: Attribution) {
    setAttributionId(item.id); setPrimary(""); setMessage(null); setError(null);
    try {
      setReviews(await apiFetch<Review[]>(`/api/v1/attributions/${item.id}/reviews`));
    } catch (reason) {
      setError(errorMessage(reason, "Unable to load review history"));
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault(); setMessage(null); setError(null);
    try {
      await apiFetch(`/api/v1/attributions/${attributionId}/reviews`, { method: "POST", json: { primary, secondary: [], confidence, evidence: [], notes } });
      setMessage("Review revision appended to the audit history."); setPrimary(""); setNotes("");
      setReviews(await apiFetch<Review[]>(`/api/v1/attributions/${attributionId}/reviews`));
    } catch (reason) {
      setError(errorMessage(reason, "Unable to append review"));
    }
  }

  return <section className="page-frame">
    <PageTitle eyebrow="Failure analysis" title="Analysis & review" detail="Automatic attribution stays read-only; each human decision is an append-only revision." />
    <form className="compare-form" onSubmit={loadAnalysis}><label>Run ID<input value={runId} onChange={(event) => setRunId(event.target.value)} required /></label><button className="button button-primary" disabled={loading}>{loading ? "Loading…" : "Load analysis"}</button></form>
    {error && <ErrorState message={error} />}
    {loading && <LoadingState label="Loading analysis" />}
    <div className="review-layout">
      <section className="section-block"><h2>Automatic attribution</h2>{analyses === null ? <EmptyState title="No analysis selected" detail="Enter a completed Run ID to inspect versioned analysis." /> : <div className="result-stack">{analyses.map((item) => <article className="resource-row" key={item.id}><div className="resource-main"><strong>{item.taxonomy_version}</strong><span>Analysis revision {item.revision}</span><code>{item.output_manifest_sha256}</code></div></article>)}{attributions?.map((item) => <article className="resource-row" key={item.id}><div className="resource-main"><strong>{item.label}</strong><span>{item.case_id} · confidence {item.confidence}</span><code>{item.taxonomy} · {item.evidence.join(", ")}</code></div><button className="button button-quiet" onClick={() => void selectAttribution(item)}>Review {item.label} for {item.case_id}</button></article>)}{attributions?.length === 0 && <EmptyState title="No attribution candidates" detail="Automatic analysis did not produce a candidate." />}</div>}</section>
      <form className="form-panel" onSubmit={submit}><h2>Append HumanReview</h2><p className="muted">Review inputs are persisted with reviewer identity, prior revision and audit event.</p><label>Attribution ID<input value={attributionId} readOnly required /></label><label>Primary label<input value={primary} onChange={(event) => setPrimary(event.target.value)} required /></label><label>Confidence<select value={confidence} onChange={(event) => setConfidence(event.target.value)}><option>low</option><option>medium</option><option>high</option></select></label><label>Notes<textarea value={notes} onChange={(event) => setNotes(event.target.value)} rows={5} maxLength={4096} /></label>{reviews.length > 0 && <div className="result-stack"><strong>Revision history</strong>{reviews.map((review) => <span className="muted" key={review.id}>r{review.revision} · {review.primary} · {review.confidence}</span>)}</div>}{message && <div className="form-success">{message}</div>}<button className="button button-primary" disabled={!attributionId}>Append review revision</button></form>
    </div>
  </section>;
}

function errorMessage(reason: unknown, fallback: string) {
  return reason instanceof ApiClientError ? reason.message : fallback;
}
