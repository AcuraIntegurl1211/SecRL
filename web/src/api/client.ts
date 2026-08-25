export type ApiErrorEnvelope = {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
    request_id: string;
  };
};

const CSRF_STORAGE_KEY = "secrl-lite.csrf";

export function setCsrfToken(token: string): void {
  sessionStorage.setItem(CSRF_STORAGE_KEY, token);
}

export function clearCsrfToken(): void {
  sessionStorage.removeItem(CSRF_STORAGE_KEY);
}

export function getCsrfToken(): string | null {
  return sessionStorage.getItem(CSRF_STORAGE_KEY);
}

export class ApiClientError extends Error {
  constructor(
    public readonly status: number,
    public readonly envelope: ApiErrorEnvelope,
  ) {
    super(envelope.error.message);
    this.name = "ApiClientError";
  }
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const { json, headers: initHeaders, ...requestInit } = init;
  const headers = new Headers(initHeaders);
  headers.set("Accept", "application/json");
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    requestInit.body = JSON.stringify(json);
  }
  const method = (requestInit.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = getCsrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  const response = await fetch(path, {
    ...requestInit,
    headers,
    credentials: "include",
  });
  if (response.status === 204) return undefined as T;
  const payload: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    const envelope = payload as ApiErrorEnvelope;
    throw new ApiClientError(response.status, envelope);
  }
  return payload as T;
}

export type ModelSummary = {
  id: string;
  name: string;
  provider: string;
  model: string;
  credential_configured: boolean;
  pricing_configured: boolean;
  sha256: string;
};

export type AgentSummary = {
  id: string;
  name: string;
  kind: "BUILT_IN" | "SERVICE";
  sha256: string;
  manifest: Record<string, unknown>;
  endpoint?: string;
};

export type BenchmarkSummary = {
  manifest: Record<string, unknown>;
  dataset: Record<string, unknown> & {
    case_count?: number;
    incidents?: Record<string, number>;
  };
};

export type PreflightCheck = {
  name: string;
  status: "ready" | "missing" | "not_applicable";
  message: string;
  code?: string;
  secret_status?: "configured" | "missing";
  unavailable_incidents?: string[];
  start_command?: string;
};

export type PreflightResponse = {
  ready: boolean;
  benchmark_id: string;
  checks: PreflightCheck[];
  scope?: {
    mode: "CASES" | "INCIDENTS" | "ALL_BENCHMARK";
    case_count: number;
    incident_count: number;
    incident_ids?: string[];
  } | null;
};

export type TaskSummary = {
  id: string;
  run_id: string;
  name: string;
  status: string;
  task_spec: Record<string, unknown>;
  task_spec_sha256: string;
};
