import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { getAuthSession } from "@/lib/auth";
import { PVLogo } from "@/components/pv/Logo";

export const Route = createFileRoute("/change-password")({
  ssr: false,
  beforeLoad: async () => {
    const session = await getAuthSession();
    if (!session.authenticated) throw redirect({ to: "/login" });
    if (!session.password_change_required) throw redirect({ to: "/app" });
  },
  component: ChangePasswordPage,
});

function ChangePasswordPage() {
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    setSaving(true);
    setError("");
    const response = await fetch("/api/auth/change-password", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!response.ok) {
      setError("Your password could not be changed.");
      setSaving(false);
      return;
    }
    await navigate({ to: "/app" });
  }
  return (
    <div className="pv flex min-h-screen items-center justify-center px-6">
      <form onSubmit={submit} className="pv-panel w-full max-w-md space-y-4 p-6">
        <div className="flex items-center gap-3">
          <PVLogo size={40} />
          <div>
            <h1 className="pv-content-title text-2xl">Choose a new password</h1>
            <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
              A password change is required before entering Personal Vault.
            </p>
          </div>
        </div>
        <input
          className="pv-input"
          type="password"
          autoComplete="new-password"
          minLength={8}
          placeholder="New password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />
        <input
          className="pv-input"
          type="password"
          autoComplete="new-password"
          minLength={8}
          placeholder="Confirm new password"
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
          required
        />
        {error && (
          <p role="alert" className="text-sm" style={{ color: "#d98b8b" }}>
            {error}
          </p>
        )}
        <button className="pv-btn-primary w-full" disabled={saving}>
          {saving ? "Saving..." : "Set password"}
        </button>
      </form>
    </div>
  );
}
