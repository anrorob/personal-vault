import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  describeAuthenticationMethod,
  describeSessionClient,
  relativeActivity,
  relativeExpiry,
} from "@/lib/session-presentation";

export const Route = createFileRoute("/app/vault-control/users")({ component: UsersPage });

type User = {
  user_id: string;
  username: string;
  display_name: string;
  email: string | null;
  role: "administrator" | "user";
  active: boolean;
  created_at: string;
  last_sign_in_at: string | null;
  password_change_required: boolean;
  password_login_enabled: boolean;
  passkeys_available: boolean;
  passkeys_active_count: number;
  recovery_pending: boolean;
  recovery_expires_at: string | null;
  storage_used_bytes: number | null;
};
type InviteResponse = { enrolment_url: string; enrolment_expires_at: string };
type RecoveryResponse = { recovery_url: string; recovery_expires_at: string };
type CopyLink = { url: string; expires_at: string; label: string };
type UserSession = {
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
const empty = { display_name: "", email: "", role: "user", passkey_first: true };
const vaultControlMenuItemClass =
  "text-[#ebe8e1] focus:bg-[#2a261c] focus:text-[#f0d58a] data-[disabled]:text-[#99978f] data-[disabled]:opacity-100";
const vaultControlDestructiveMenuItemClass =
  "text-[#f0c2bd] focus:bg-[#3a2220] focus:text-[#ffd8d2] data-[disabled]:text-[#99978f] data-[disabled]:opacity-100";
const vaultControlMenuSeparatorClass = "bg-[#5a4d30]";
function storage(value: number | null) {
  return value === null
    ? "Unavailable"
    : `${(value / 1024 ** 3).toFixed(value >= 1024 ** 3 ? 1 : 0)} GB`;
}
function date(value: string | null) {
  return value
    ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
        new Date(value),
      )
    : "Never";
}

function UsersPage() {
  const navigate = useNavigate();
  const [users, setUsers] = useState<User[]>([]);
  const [form, setForm] = useState(empty);
  const [showAdd, setShowAdd] = useState(false);
  const [detailUsername, setDetailUsername] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [invite, setInvite] = useState<CopyLink | null>(null);
  const [userSessions, setUserSessions] = useState<Record<string, UserSession[]>>({});
  const load = async () => {
    const response = await fetch("/api/vault-control/users", { credentials: "include" });
    if (response.status === 401) return navigate({ to: "/login" });
    if (response.status === 403) return navigate({ to: "/app" });
    if (!response.ok) {
      setError("Users could not be loaded.");
      return;
    }
    setUsers(((await response.json()) as { users: User[] }).users);
  };
  useEffect(() => {
    void load();
  }, []);
  async function add(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    const response = await fetch("/api/vault-control/users", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { detail?: string } | null;
      setError(body?.detail ?? "User could not be created.");
      return;
    }
    const created = (await response.json()) as Partial<InviteResponse>;
    setInvite(
      created.enrolment_url && created.enrolment_expires_at
        ? {
            url: created.enrolment_url,
            expires_at: created.enrolment_expires_at,
            label: "First-passkey enrolment link",
          }
        : null,
    );
    setForm(empty);
    setShowAdd(false);
    await load();
  }
  async function regenerateInvite(user: User) {
    setError("");
    const response = await fetch(
      `/api/vault-control/users/${encodeURIComponent(user.user_id)}/enrolment-invite`,
      {
        method: "POST",
        credentials: "include",
      },
    );
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { detail?: string } | null;
      setError(body?.detail ?? "Enrolment link could not be regenerated.");
      return;
    }
    const created = (await response.json()) as InviteResponse;
    setInvite({
      url: created.enrolment_url,
      expires_at: created.enrolment_expires_at,
      label: "First-passkey enrolment link",
    });
  }
  async function recover(user: User) {
    if (
      !window.confirm(
        `Recover access for ${user.display_name}? This immediately revokes that user's active passkeys and normal sessions before creating a 60-minute recovery link.`,
      )
    )
      return;
    setError("");
    const response = await fetch(
      `/api/vault-control/users/${encodeURIComponent(user.user_id)}/recovery`,
      { method: "POST", credentials: "include" },
    );
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { detail?: string } | null;
      setError(body?.detail ?? "Recovery link could not be created.");
      return;
    }
    const created = (await response.json()) as RecoveryResponse;
    setInvite({
      url: created.recovery_url,
      expires_at: created.recovery_expires_at,
      label: "Recovery link",
    });
    await load();
  }
  async function copyInvite() {
    if (!invite) return;
    try {
      await navigator.clipboard.writeText(invite.url);
    } catch {
      setError("Copy the enrolment link manually from the address shown below.");
    }
  }
  async function change(user: User, patch: Partial<User>) {
    const response = await fetch(`/api/vault-control/users/${encodeURIComponent(user.username)}`, {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        display_name: user.display_name,
        email: user.email ?? "",
        role: patch.role ?? user.role,
        active: patch.active ?? user.active,
      }),
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { detail?: string } | null;
      setError(body?.detail ?? "User could not be changed.");
      return;
    }
    await load();
  }
  async function reset(user: User) {
    const temporary_password = window.prompt(
      `Set a temporary password for ${user.display_name} (minimum 8 characters):`,
    );
    if (!temporary_password) return;
    const response = await fetch(
      `/api/vault-control/users/${encodeURIComponent(user.username)}/reset-password`,
      {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ temporary_password }),
      },
    );
    if (!response.ok) {
      setError("Password could not be reset.");
      return;
    }
    await load();
  }
  async function changePasswordPolicy(user: User) {
    const response = await fetch(
      `/api/vault-control/users/${encodeURIComponent(user.user_id)}/authentication-policy`,
      {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password_login_enabled: !user.password_login_enabled }),
      },
    );
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as { detail?: string } | null;
      setError(body?.detail ?? "Password sign-in policy could not be changed.");
      return;
    }
    await load();
  }
  async function edit(user: User) {
    const display_name = window.prompt("Display name:", user.display_name);
    if (!display_name) return;
    const email = window.prompt("Email address:", user.email ?? "");
    if (!email) return;
    await change(user, { display_name, email });
  }
  async function loadSessions(user: User) {
    const response = await fetch(
      `/api/vault-control/users/${encodeURIComponent(user.user_id)}/sessions`,
      { credentials: "include" },
    );
    if (!response.ok) return setError("Sessions could not be loaded.");
    const body = (await response.json()) as { sessions: UserSession[] };
    setUserSessions((current) => ({
      ...current,
      [user.user_id]: body.sessions,
    }));
  }
  async function revokeAllSessions(user: User) {
    const includesCurrent = userSessions[user.user_id]?.some((session) => session.current);
    if (
      !window.confirm(
        includesCurrent
          ? `Revoke all active sessions for ${user.display_name}? This includes your current session and will return you to sign-in.`
          : `Revoke all active sessions for ${user.display_name}?`,
      )
    )
      return;
    const response = await fetch(
      `/api/vault-control/users/${encodeURIComponent(user.user_id)}/sessions/revoke-all`,
      { method: "POST", credentials: "include" },
    );
    if (!response.ok) return setError("Sessions could not be revoked.");
    setUserSessions((current) => ({ ...current, [user.user_id]: [] }));
  }
  async function revokeSession(user: User, session: UserSession) {
    if (!window.confirm("Revoke this session?")) return;
    const response = await fetch(
      `/api/vault-control/users/${encodeURIComponent(user.user_id)}/sessions/${encodeURIComponent(session.id)}`,
      { method: "DELETE", credentials: "include" },
    );
    if (!response.ok) return setError("Session could not be revoked.");
    await loadSessions(user);
  }
  return (
    <section className="mx-auto max-w-6xl space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="pv-display-title text-3xl md:text-4xl">Users</h2>
          <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Manage Personal Vault accounts. Storage is attributed only to file ownership.
          </p>
        </div>
        <button className="pv-btn-primary" onClick={() => setShowAdd(!showAdd)}>
          + Add User
        </button>
      </div>
      {error && (
        <p role="alert" className="text-sm" style={{ color: "#d98b8b" }}>
          {error}
        </p>
      )}
      {showAdd && (
        <form className="pv-panel grid gap-3 p-5 md:grid-cols-2" onSubmit={add}>
          <input
            className="pv-input"
            placeholder="Display name"
            required
            value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
          />
          <input
            className="pv-input"
            placeholder="Email address"
            type="email"
            required
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
          />
          <select
            className="pv-input"
            value={form.role}
            onChange={(e) => setForm({ ...form, role: e.target.value })}
          >
            <option value="user">User</option>
            <option value="administrator">Administrator</option>
          </select>
          <p className="self-center text-sm" style={{ color: "var(--pv-text-dim)" }}>
            The user will create their first passkey. No password is created.
          </p>
          <div className="md:col-span-2">
            <button className="pv-btn-primary">Create user</button>
          </div>
        </form>
      )}
      {invite && (
        <aside className="pv-panel space-y-3 p-5" aria-live="polite">
          <p className="font-medium">{invite.label}</p>
          <p className="break-all text-sm" style={{ color: "var(--pv-text-dim)" }}>
            {invite.url}
          </p>
          <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Expires: {date(invite.expires_at)} (60 minutes)
          </p>
          <button className="pv-btn-secondary" onClick={() => void copyInvite()}>
            Copy {invite.label.toLowerCase()}
          </button>
        </aside>
      )}
      <div className="space-y-3">
        {users.map((user) => (
          <article key={user.username} className="pv-panel p-5">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <h3 className="text-lg font-semibold">{user.display_name}</h3>
                <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
                  {user.role === "administrator" ? "Administrator" : "User"} ·{" "}
                  {user.active ? "Active" : "Disabled"}
                </p>
                <p className="mt-2 text-sm">
                  {user.email ?? "Email unavailable for migrated account"}
                </p>
                <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
                  Storage used: {storage(user.storage_used_bytes)} · Last sign-in:{" "}
                  {date(user.last_sign_in_at)}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="pv-btn-ghost inline-flex h-10 w-10 items-center justify-center p-0"
                  onClick={() => {
                    const open = detailUsername !== user.username;
                    setDetailUsername(open ? user.username : null);
                    if (open) void loadSessions(user);
                  }}
                  aria-expanded={detailUsername === user.username}
                  aria-controls={`user-details-${user.user_id}`}
                  aria-label={
                    detailUsername === user.username
                      ? `Hide details for ${user.display_name}`
                      : `Show details for ${user.display_name}`
                  }
                  title={detailUsername === user.username ? "Hide details" : "Show details"}
                >
                  <ChevronDown
                    size={18}
                    aria-hidden="true"
                    className={detailUsername === user.username ? "rotate-180" : undefined}
                  />
                </button>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button type="button" className="pv-btn-ghost inline-flex items-center gap-1.5">
                      Options <ChevronDown size={15} aria-hidden="true" />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent
                    align="end"
                    className="min-w-56 border-[#2d2b27] bg-[#17171a] text-[#ebe8e1]"
                  >
                    <DropdownMenuItem
                      className={vaultControlMenuItemClass}
                      onSelect={() => void edit(user)}
                    >
                      Edit
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      className={vaultControlMenuItemClass}
                      onSelect={() =>
                        void change(user, {
                          role: user.role === "administrator" ? "user" : "administrator",
                        })
                      }
                    >
                      Make {user.role === "administrator" ? "User" : "Administrator"}
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      className={
                        user.active
                          ? vaultControlDestructiveMenuItemClass
                          : vaultControlMenuItemClass
                      }
                      onSelect={() => void change(user, { active: !user.active })}
                    >
                      {user.active ? "Disable" : "Re-enable"}
                    </DropdownMenuItem>
                    <DropdownMenuSeparator className={vaultControlMenuSeparatorClass} />
                    <DropdownMenuItem
                      className={vaultControlMenuItemClass}
                      onSelect={() => void reset(user)}
                    >
                      Reset password
                    </DropdownMenuItem>
                    {!user.passkeys_available && !user.password_login_enabled && user.active && (
                      <DropdownMenuItem
                        className={vaultControlMenuItemClass}
                        onSelect={() => void regenerateInvite(user)}
                      >
                        Regenerate enrolment link
                      </DropdownMenuItem>
                    )}
                    {user.role === "user" && user.active && (
                      <DropdownMenuItem
                        className={vaultControlDestructiveMenuItemClass}
                        onSelect={() => void recover(user)}
                      >
                        Recover access
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuItem
                      className={vaultControlMenuItemClass}
                      onSelect={() => void changePasswordPolicy(user)}
                    >
                      {user.password_login_enabled
                        ? "Disable password sign-in"
                        : "Allow password sign-in"}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            </div>
            {detailUsername === user.username && (
              <dl
                id={`user-details-${user.user_id}`}
                className="mt-4 grid gap-x-6 gap-y-2 border-t pt-4 text-sm sm:grid-cols-2"
                style={{ borderColor: "var(--pv-border)", color: "var(--pv-text-dim)" }}
              >
                <div>
                  <dt className="inline">Sign-in methods: </dt>
                  <dd className="inline">
                    Passkeys: {user.passkeys_available ? "Available" : "None registered"} · Password
                    sign-in: {user.password_login_enabled ? "Allowed" : "Disabled"}
                  </dd>
                </div>
                <div className="sm:col-span-2">
                  <dt className="inline">Active sessions: </dt>
                  <dd className="inline">{userSessions[user.user_id]?.length ?? "Loading..."}</dd>
                  <button
                    className="pv-btn-secondary ml-3 px-2 py-1 text-xs"
                    onClick={() => void loadSessions(user)}
                  >
                    Refresh
                  </button>
                  {!!userSessions[user.user_id]?.length && (
                    <button
                      className="pv-btn-secondary ml-2 px-2 py-1 text-xs"
                      onClick={() => void revokeAllSessions(user)}
                    >
                      Revoke all sessions
                    </button>
                  )}
                  {userSessions[user.user_id]?.map((session) => (
                    <div
                      key={session.id}
                      className="mt-3 border-t pt-2"
                      style={{ borderColor: "var(--pv-border)" }}
                    >
                      <p className="font-medium">
                        {describeSessionClient(session.user_agent)}
                        {session.current ? " · This session" : ""}
                      </p>
                      <p>{describeAuthenticationMethod(session.authentication_method)}</p>
                      <p>
                        {relativeActivity(session.last_seen_at, session.current)} ·{" "}
                        {relativeExpiry(session.expires_at)}
                      </p>
                      <p>
                        IP: {session.client_ip || "IP unavailable"}
                        {session.vault_control_elevated ? " · Vault Control access active" : ""}
                      </p>
                      <p>Expires: {date(session.expires_at)}</p>
                      <button
                        className="pv-btn-secondary mt-2 px-2 py-1 text-xs"
                        onClick={() => void revokeSession(user, session)}
                      >
                        Revoke session
                      </button>
                    </div>
                  ))}
                </div>
                <div>
                  <dt className="inline">Recovery: </dt>
                  <dd className="inline">
                    {user.recovery_pending
                      ? `Pending until ${date(user.recovery_expires_at)}`
                      : "Not pending"}
                  </dd>
                </div>
                <div>
                  <dt className="inline">Created: </dt>
                  <dd className="inline">{date(user.created_at)}</dd>
                </div>
                <div>
                  <dt className="inline">Password change required: </dt>
                  <dd className="inline">{user.password_change_required ? "Yes" : "No"}</dd>
                </div>
                <div>
                  <dt className="inline">Storage used: </dt>
                  <dd className="inline">{storage(user.storage_used_bytes)}</dd>
                </div>
              </dl>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
