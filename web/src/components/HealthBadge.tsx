export function HealthBadge({ status }: { status: "healthy" | "degraded" | "offline" | string }) {
  const normalized = status.toLowerCase();
  const tone = normalized === "healthy" || normalized === "ok" ? "healthy" : normalized === "degraded" ? "degraded" : "offline";
  return <span className={`health-badge health-${tone}`}><span className="status-dot" />{status}</span>;
}
