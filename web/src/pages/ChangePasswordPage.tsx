import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiClientError, apiFetch } from "../api/client";

export function ChangePasswordPage() {
  const navigate = useNavigate();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault(); setError(null);
    if (newPassword !== confirmPassword) { setError("New passwords do not match"); return; }
    setPending(true);
    try {
      await apiFetch("/api/v1/auth/password", { method: "POST", json: { current_password: currentPassword, new_password: newPassword } });
      setCurrentPassword(""); setNewPassword(""); setConfirmPassword(""); navigate("/");
    } catch (reason) {
      setError(reason instanceof ApiClientError ? reason.message : "Unable to change password");
    } finally { setPending(false); }
  }

  return <main className="login-page"><div className="login-panel"><div className="eyebrow">Required security step</div><h1>Change initial password</h1><p className="lede">Choose a new local administrator password before using the platform.</p><form onSubmit={submit} className="form-stack"><label>Current password<input aria-label="Current password" type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoComplete="current-password" required /></label><label>New password<input aria-label="New password" type="password" minLength={12} value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" required /></label><label>Confirm new password<input aria-label="Confirm new password" type="password" minLength={12} value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} autoComplete="new-password" required /></label>{error && <div role="alert" className="form-error">{error}</div>}<button className="button button-primary" disabled={pending}>{pending ? "Changing…" : "Change password"}</button></form></div></main>;
}
