import {
  Activity,
  BarChart3,
  Bot,
  ChevronLeft,
  Database,
  FlaskConical,
  Gauge,
  GitCompare,
  Menu,
  Network,
  PlayCircle,
  Settings2,
  X,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";
import "../styles.css";

type NavItem = { label: string; to: string; icon: typeof Gauge };

const navigation: NavItem[] = [
  { label: "Dashboard", to: "/", icon: Gauge },
  { label: "Models", to: "/models", icon: Settings2 },
  { label: "Agents", to: "/agents", icon: Bot },
  { label: "Benchmarks", to: "/benchmarks", icon: Database },
  { label: "New evaluation", to: "/evaluations/new", icon: PlayCircle },
  { label: "Runs", to: "/runs", icon: Activity },
  { label: "Analysis & review", to: "/analysis", icon: Network },
  { label: "Compare", to: "/compare", icon: GitCompare },
];

export function AppShell({ children }: { children?: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="app-shell">
      <button
        className="mobile-menu-button icon-button"
        aria-label="Open navigation"
        onClick={() => setOpen(true)}
      >
        <Menu size={18} />
      </button>
      <aside className={`sidebar ${open ? "sidebar-open" : ""}`}>
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true"><FlaskConical size={17} /></div>
          <div>
            <div className="brand-title">SecRL Lite</div>
            <div className="brand-subtitle">benchmark operations</div>
          </div>
          <button
            className="icon-button sidebar-close"
            aria-label="Close navigation"
            onClick={() => setOpen(false)}
          >
            <X size={17} />
          </button>
        </div>
        <nav aria-label="Primary navigation" className="primary-nav">
          <div className="nav-section-label">Workspace</div>
          {navigation.map(({ label, to, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) => `nav-item ${isActive ? "nav-item-active" : ""}`}
              onClick={() => setOpen(false)}
            >
              <Icon size={17} strokeWidth={1.8} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="system-note"><span className="status-dot" />Local platform</div>
          <button className="collapse-button" type="button" title="Sidebar is fixed on desktop">
            <ChevronLeft size={15} />
            <span>Secure workspace</span>
          </button>
        </div>
      </aside>
      {open && <button className="scrim" aria-label="Close navigation" onClick={() => setOpen(false)} />}
      <main className="main-content">
        {children ?? <Outlet />}
      </main>
    </div>
  );
}
