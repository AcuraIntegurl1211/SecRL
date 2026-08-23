export function MetricValue({ value, label, detail }: { value: string | number; label: string; detail?: string }) {
  return <div className="metric-value"><div className="metric-number">{value}</div><div className="metric-label">{label}</div>{detail && <div className="metric-detail">{detail}</div>}</div>;
}
