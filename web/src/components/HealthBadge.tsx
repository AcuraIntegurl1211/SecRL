const TASK_STATUS_TONES: Record<string, string> = {
  succeeded: "healthy",
  running: "degraded",
  queued: "degraded",
  failed: "offline",
  budget_exhausted: "offline",
  canceled: "offline",
  paused: "offline",
  pause_requested: "offline",
  interrupted: "offline",
};

export function HealthBadge({ status }: { status: "healthy" | "degraded" | "offline" | string }) {
  const normalized = status.toLowerCase();
  const tone = TASK_STATUS_TONES[normalized]
    ?? (normalized === "healthy" || normalized === "ok" ? "healthy" : normalized === "degraded" ? "degraded" : "offline");
  return <span className={`health-badge health-${tone}`}><span className="status-dot" />{status}</span>;
}
