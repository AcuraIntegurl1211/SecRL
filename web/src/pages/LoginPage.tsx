import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiClientError, apiFetch, setCsrfToken } from "../api/client";

export function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setError(null); setPending(true);
    try { const result = await apiFetch<{ csrf_token: string; password_change_required: boolean }>("/api/v1/auth/login", { method: "POST", json: { username, password } }); setCsrfToken(result.csrf_token); setPassword(""); navigate(result.password_change_required ? "/change-password" : "/"); }
    catch (reason) { setError(reason instanceof ApiClientError ? reason.message : "Unable to sign in"); }
    finally { setPending(false); }
  }
  return <main className="login-page"><div className="login-panel"><div className="eyebrow">Local access</div><h1>Sign in to SecRL Lite</h1><p className="lede">The platform is intentionally local-first. Credentials stay in this browser session.</p><form onSubmit={submit} className="form-stack"><label>Username<input aria-label="Username" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required /></label><label>Password<input aria-label="Password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label>{error && <div role="alert" className="form-error">{error}</div>}<button className="button button-primary" disabled={pending}>{pending ? "Signing in…" : "Sign in"}</button></form></div></main>;
}
