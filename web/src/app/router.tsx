import { useEffect, useState } from "react";
import { createBrowserRouter, Navigate, Outlet, useLocation } from "react-router-dom";
import { apiFetch } from "../api/client";
import { AppShell } from "../components/AppShell";
import { LoadingState } from "../components/PageStates";
import { DashboardPage } from "../pages/DashboardPage";
import { AgentsPage } from "../pages/AgentsPage";
import { AnalysisReviewPage } from "../pages/AnalysisReviewPage";
import { BenchmarksPage } from "../pages/BenchmarksPage";
import { ComparePage } from "../pages/ComparePage";
import { ChangePasswordPage } from "../pages/ChangePasswordPage";
import { LoginPage } from "../pages/LoginPage";
import { ModelsPage } from "../pages/ModelsPage";
import { NewEvaluationPage } from "../pages/NewEvaluationPage";
import { RunDetailPage } from "../pages/RunDetailPage";
import { RunsPage } from "../pages/RunsPage";

export function AuthGate() {
  const location = useLocation();
  const [state, setState] = useState<"loading" | "ready" | "missing">("loading");
  useEffect(() => {
    let mounted = true;
    void apiFetch("/api/v1/tasks")
      .then(() => { if (mounted) setState("ready"); })
      .catch(() => { if (mounted) setState("missing"); });
    return () => { mounted = false; };
  }, []);
  if (state === "loading") return <LoadingState label="Checking local session" />;
  if (state === "missing") return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Outlet />;
}

export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  { path: "/change-password", element: <ChangePasswordPage /> },
  {
    element: <AuthGate />,
    children: [
      {
        path: "/",
        element: <AppShell />,
        children: [
          { index: true, element: <DashboardPage /> },
          { path: "models", element: <ModelsPage /> },
          { path: "agents", element: <AgentsPage /> },
          { path: "benchmarks", element: <BenchmarksPage /> },
          { path: "evaluations/new", element: <NewEvaluationPage /> },
          { path: "runs", element: <RunsPage /> },
          { path: "runs/:id", element: <RunDetailPage /> },
          { path: "analysis", element: <AnalysisReviewPage /> },
          { path: "compare", element: <ComparePage /> },
        ],
      },
    ],
  },
]);
