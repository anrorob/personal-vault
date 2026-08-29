import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { registerPasskey, passkeysSupported } from "@/lib/passkeys";
import {
  describeAuthenticationMethod,
  describeSessionClient,
  relativeActivity,
  relativeExpiry,
} from "@/lib/session-presentation";

type Credential = {
  id: string;
  label: string | null;
  created_at: string;
  last_used_at: string | null;
  authenticator_attachment: string | null;
};
type Session = { password_login_enabled?: boolean };
type ActiveSession = {
  id: string;
  created_at: string;
  last_seen_at: string | null;
  expires_at: string;
  authentication_method: string | null;
  client_ip: string | null;
  user_agent: string | null;
  vault_control_elevated: boolean;
  current: boolean;
};
type SecurityEvent = {
  id: string;
  event_type: string;
  occurred_at: string;
  authentication_method: string | null;
  client_ip: string | null;
  user_agent: string | null;
};

const dateTime = (value: string) =>
  new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
const eventLabel = (event: SecurityEvent) =>
  ({
    sign_in_succeeded: `Signed in${event.authentication_method ? ` with ${event.authentication_method}` : ""}`,
    sign_in_failed: "Failed sign-in",
    signed_out: "Signed out",
    session_revoked: "Session revoked",
    other_sessions_revoked: "Other sessions signed out",
    passkey_added: "Passkey added",
    passkey_removed: "Passkey removed",
    password_changed: "Password changed",
  })[event.event_type] ?? "Security activity";

export const Route = createFileRoute("/app/security")({ component: SecurityPage });

function SecurityPage() {
  const navigate = useNavigate();
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [passwordLoginEnabled, setPasswordLoginEnabled] = useState<boolean | null>(null);
  const [sessions, setSessions] = useState<ActiveSession[]>([]);
  const [events, setEvents] = useState<SecurityEvent[]>([]);

  const load = useCallback(async () => {
    const response = await fetch("/api/auth/passkeys", { credentials: "include" });
    if (response.status === 401) return navigate({ to: "/login" });
    if (response.ok) {
      setCredentials((await response.json()) as Credential[]);
      const session = await fetch("/api/auth/session", { credentials: "include" });
      if (session.ok)
        setPasswordLoginEnabled(((await session.json()) as Session).password_login_enabled ?? null);
      const [activeSessions, securityEvents] = await Promise.all([
        fetch("/api/auth/sessions", { credentials: "include" }),
        fetch("/api/auth/security-events", { credentials: "include" }),
      ]);
      if (activeSessions.ok) setSessions((await activeSessions.json()) as ActiveSession[]);
      if (securityEvents.ok) setEvents((await securityEvents.json()) as SecurityEvent[]);
    }
  }, [navigate]);

  useEffect(() => {
    void load();
  }, [load]);

  const add = async () => {
    setBusy(true);
    setMessage("");
    try {
      await registerPasskey();
      setMessage("Passkey added. Password sign-in remains available.");
      await load();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Passkey registration failed.");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (session: ActiveSession) => {
    if (!window.confirm("Sign out this other session? It will need to sign in again.")) return;
    const response = await fetch(`/api/auth/sessions/${session.id}`, {
      method: "DELETE",
      credentials: "include",
    });
    if (!response.ok) return setMessage("That session could not be revoked.");
    setMessage("Other session signed out.");
    await load();
  };
  const signOutOthers = async () => {
    if (!window.confirm("Sign out all other sessions? This session will remain active.")) return;
    const response = await fetch("/api/auth/sessions/sign-out-others", {
      method: "POST",
      credentials: "include",
    });
    if (!response.ok) return setMessage("Other sessions could not be signed out.");
    setMessage("Other sessions signed out.");
    await load();
  };

  const remove = async (id: string) => {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`/api/auth/passkeys/${id}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(body?.detail ?? "Passkey removal failed.");
      }
      setMessage("Passkey removed.");
      await load();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Passkey removal failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="mx-auto max-w-3xl space-y-6">
      <div>
        <h2 className="pv-content-title text-2xl">Passkeys</h2>
        <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
          Add a passkey for quick, secure sign-in on this device.
        </p>
        {passwordLoginEnabled !== null && (
          <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Password sign-in: {passwordLoginEnabled ? "Allowed" : "Disabled"}
          </p>
        )}
        {passwordLoginEnabled === false && (
          <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Keep at least one passkey registered. If all passkeys are lost, an administrator can
            assist with recovery.
          </p>
        )}
      </div>
      <div className="pv-panel space-y-4 p-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-lg font-semibold">Active sessions</h3>
            <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
              Review devices signed in to your account.
            </p>
          </div>
          <button
            className="pv-btn-secondary"
            disabled={busy || sessions.length < 2}
            onClick={() => void signOutOthers()}
          >
            Sign out other sessions
          </button>
        </div>
        {sessions.length ? (
          sessions.map((session) => (
            <div
              key={session.id}
              className="flex items-start justify-between gap-4 border-t pt-3"
              style={{ borderColor: "var(--pv-border)" }}
            >
              <div>
                <p className="font-medium">
                  {describeSessionClient(session.user_agent)}
                  {session.current ? " · This session" : ""}
                </p>
                <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  {describeAuthenticationMethod(session.authentication_method)}
                </p>
                <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  {relativeActivity(session.last_seen_at, session.current)} ·{" "}
                  {relativeExpiry(session.expires_at)}
                  {session.vault_control_elevated ? " · Vault Control access active" : ""}
                </p>
                <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  IP: {session.client_ip || "IP unavailable"} · Expires{" "}
                  {dateTime(session.expires_at)}
                </p>
              </div>
              {!session.current && (
                <button className="pv-btn-secondary" onClick={() => void revoke(session)}>
                  Sign out
                </button>
              )}
            </div>
          ))
        ) : (
          <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
            No active sessions found.
          </p>
        )}
      </div>
      <div className="pv-panel space-y-3 p-6">
        <div>
          <h3 className="text-lg font-semibold">Recent security activity</h3>
          <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Latest 50 account security events. IP and browser details are informational.
          </p>
        </div>
        {events.length ? (
          events.map((event) => (
            <div
              key={event.id}
              className="border-t pt-3 text-sm"
              style={{ borderColor: "var(--pv-border)" }}
            >
              <p className="font-medium">{eventLabel(event)}</p>
              <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                {dateTime(event.occurred_at)}
                {event.client_ip ? ` · ${event.client_ip}` : ""}
                {event.user_agent ? ` · ${event.user_agent}` : ""}
              </p>
            </div>
          ))
        ) : (
          <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
            No recent security activity.
          </p>
        )}
      </div>
      <div className="pv-panel p-6 space-y-4">
        {passkeysSupported() ? (
          <button className="pv-btn-primary" disabled={busy} onClick={() => void add()}>
            {busy ? "Working..." : "Add passkey"}
          </button>
        ) : (
          <p className="text-sm">
            This browser does not support passkeys. Password sign-in remains available.
          </p>
        )}
        {message ? (
          <p role="status" className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
            {message}
          </p>
        ) : null}
        <div className="space-y-3">
          {credentials.length ? (
            credentials.map((credential) => (
              <div
                key={credential.id}
                className="flex items-center justify-between gap-4 border-t pt-3"
                style={{ borderColor: "var(--pv-border)" }}
              >
                <div>
                  <p className="font-medium">{credential.label || "Passkey"}</p>
                  <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                    Added {new Date(credential.created_at).toLocaleDateString()}
                    {credential.last_used_at
                      ? ` · last used ${new Date(credential.last_used_at).toLocaleDateString()}`
                      : ""}
                  </p>
                </div>
                <button
                  className="pv-btn-secondary"
                  disabled={busy}
                  onClick={() => void remove(credential.id)}
                >
                  Remove
                </button>
              </div>
            ))
          ) : (
            <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
              No passkeys have been added yet.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
