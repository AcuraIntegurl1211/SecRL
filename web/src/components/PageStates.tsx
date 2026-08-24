import { AlertTriangle, LoaderCircle, SearchX } from "lucide-react";

export function LoadingState({ label = "Loading" }: { label?: string }) {
  return <div className="page-state"><LoaderCircle className="spin" size={18} /><span>{label}</span></div>;
}

export function ErrorState({ message = "Something went wrong", onRetry }: { message?: string; onRetry?: () => void }) {
  return <div className="page-state page-state-error"><AlertTriangle size={18} /><span>{message}</span>{onRetry && <button className="button button-quiet" onClick={onRetry}>Retry</button>}</div>;
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="page-state page-state-empty"><SearchX size={18} /><div><strong>{title}</strong><span>{detail}</span></div></div>;
}

export function PageTitle({ eyebrow, title, detail, action }: { eyebrow: string; title: string; detail: string; action?: React.ReactNode }) {
  return <header className="page-header"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p className="lede">{detail}</p></div>{action}</header>;
}
