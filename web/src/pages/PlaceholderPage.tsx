import { useLocation } from "react-router-dom";

export function PlaceholderPage() {
  const location = useLocation();
  const title = location.pathname === "/evaluations/new" ? "New evaluation" : location.pathname.slice(1).replaceAll("/", " ") || "Workspace";
  return <section className="page-frame"><header className="page-header"><div><div className="eyebrow">Workspace</div><h1>{title}</h1><p className="lede">This operational view is being prepared.</p></div></header><div className="empty-state large"><p>Loading workspace data</p><span>Use the navigation to move between benchmark operations.</span></div></section>;
}
