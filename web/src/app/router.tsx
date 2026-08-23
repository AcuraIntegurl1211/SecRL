import { createBrowserRouter } from "react-router-dom";
import { AppShell } from "../components/AppShell";
import { DashboardPage } from "../pages/DashboardPage";
import { AgentsPage } from "../pages/AgentsPage";
import { AnalysisReviewPage } from "../pages/AnalysisReviewPage";
import { BenchmarksPage } from "../pages/BenchmarksPage";
import { ComparePage } from "../pages/ComparePage";
import { LoginPage } from "../pages/LoginPage";
import { ModelsPage } from "../pages/ModelsPage";
import { NewEvaluationPage } from "../pages/NewEvaluationPage";
import { RunDetailPage } from "../pages/RunDetailPage";
import { RunsPage } from "../pages/RunsPage";

export const router = createBrowserRouter([
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
      { path: "login", element: <LoginPage /> },
    ],
  },
]);
