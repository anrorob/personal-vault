import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router";
import { getAuthSession } from "@/lib/auth";
import { useEffect, useState } from "react";
import { PVLogo } from "@/components/pv/Logo";
import { authenticateWithPasskey, passkeysSupported } from "@/lib/passkeys";

const LOCKOUT_STORAGE_KEY = "pv-login-lockout-until";

function getStoredLockoutUntil(): number {
  try {
    const storedValue = Number.parseInt(sessionStorage.getItem(LOCKOUT_STORAGE_KEY) ?? "", 10);

    if (Number.isFinite(storedValue) && storedValue > Date.now()) {
      return storedValue;
    }

    sessionStorage.removeItem(LOCKOUT_STORAGE_KEY);
  } catch {
    // The countdown still works when browser storage is unavailable.
  }

  return 0;
}

function formatCountdown(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

export const Route = createFileRoute("/login")({
  ssr: false,
  beforeLoad: async () => {
    const session = await getAuthSession();

    if (session.authenticated) {
      throw redirect({
        to: session.password_change_required ? "/change-password" : "/app",
      });
    }
  },
  head: () => ({
    meta: [
      { title: "Sign in — Personal Vault" },
      { name: "description", content: "Secure access to your Personal Vault." },
      { name: "robots", content: "noindex" },
    ],
  }),
  component: LoginPage,
});

function LoginPage() {
  const navigate = useNavigate();
  const [user, setUser] = useState("");
  const [pass, setPass] = useState("");
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isPasskeySubmitting, setIsPasskeySubmitting] = useState(false);
  const [lockoutUntil, setLockoutUntil] = useState(getStoredLockoutUntil);
  const [now, setNow] = useState(Date.now);
  const lockoutSeconds = Math.max(0, Math.ceil((lockoutUntil - now) / 1000));
  const isLockedOut = lockoutSeconds > 0;
  const supportsPasskeys = passkeysSupported();

  useEffect(() => {
    if (!lockoutUntil) {
      return;
    }

    const timer = window.setInterval(() => {
      const currentTime = Date.now();
      setNow(currentTime);

      if (currentTime >= lockoutUntil) {
        window.clearInterval(timer);

        try {
          sessionStorage.removeItem(LOCKOUT_STORAGE_KEY);
        } catch {
          // Browser storage is optional.
        }
      }
    }, 1000);

    return () => window.clearInterval(timer);
  }, [lockoutUntil]);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (isLockedOut) {
      return;
    }

    setError("");
    setIsSubmitting(true);

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
        body: JSON.stringify({
          username: user,
          password: pass,
        }),
      });

      if (response.status === 429) {
        const retryAfter = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);

        if (Number.isFinite(retryAfter) && retryAfter > 0) {
          const nextLockoutUntil = Date.now() + retryAfter * 1000;
          setLockoutUntil(nextLockoutUntil);
          setNow(Date.now());

          try {
            sessionStorage.setItem(LOCKOUT_STORAGE_KEY, String(nextLockoutUntil));
          } catch {
            // The countdown still works when browser storage is unavailable.
          }
        } else {
          setError("Too many failed sign-in attempts. Try again later.");
        }

        return;
      }

      if (!response.ok) {
        setError("Invalid username or password.");
        return;
      }

      const result = (await response.json()) as { password_change_required?: boolean };
      await navigate({ to: result.password_change_required ? "/change-password" : "/app" });
    } catch {
      setError("The Vault could not be reached.");
    } finally {
      setIsSubmitting(false);
    }
  };

  const onPasskey = async () => {
    setError("");
    setIsPasskeySubmitting(true);
    try {
      const result = await authenticateWithPasskey();
      await navigate({ to: result.password_change_required ? "/change-password" : "/app" });
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Passkey sign-in failed. You can still use your password.",
      );
    } finally {
      setIsPasskeySubmitting(false);
    }
  };

  return (
    <div className="pv flex min-h-screen flex-col items-center justify-center px-6 py-12">
      <div className="pv-fade-in w-full max-w-md">
        <div className="flex flex-col items-center text-center">
          <PVLogo size={72} />
          <h1 className="pv-content-title mt-6 text-3xl tracking-tight">Personal Vault</h1>
          <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Secure access to your digital world
          </p>
        </div>

        <form onSubmit={onSubmit} className="pv-panel mt-8 space-y-5 p-6">
          {supportsPasskeys ? (
            <button
              type="button"
              className="pv-btn-primary w-full"
              disabled={isPasskeySubmitting}
              onClick={() => void onPasskey()}
            >
              {isPasskeySubmitting ? "Waiting for passkey..." : "Sign in with passkey"}
            </button>
          ) : (
            <p className="text-center text-sm" style={{ color: "var(--pv-text-dim)" }}>
              This browser does not support passkeys. Use your password to sign in.
            </p>
          )}

          <div className="flex items-center gap-3" aria-hidden="true">
            <span className="h-px flex-1" style={{ backgroundColor: "var(--pv-border)" }} />
            <span
              className="text-center text-xs uppercase tracking-wider"
              style={{ color: "var(--pv-silver-dim)" }}
            >
              Or sign in with username and password
            </span>
            <span className="h-px flex-1" style={{ backgroundColor: "var(--pv-border)" }} />
          </div>
          <div className="space-y-1.5">
            <label
              className="text-xs uppercase tracking-wider"
              style={{ color: "var(--pv-silver-dim)" }}
            >
              Username or Email
            </label>
            <input
              type="text"
              autoComplete="username"
              className="pv-input"
              value={user}
              onChange={(e) => setUser(e.target.value)}
              placeholder="you@vault"
            />
          </div>
          <div className="space-y-1.5">
            <label
              className="text-xs uppercase tracking-wider"
              style={{ color: "var(--pv-silver-dim)" }}
            >
              Password
            </label>
            <input
              type="password"
              autoComplete="current-password"
              className="pv-input"
              value={pass}
              onChange={(e) => setPass(e.target.value)}
              placeholder="••••••••"
            />
          </div>
          {isLockedOut ? (
            <p className="text-sm" style={{ color: "#d98b8b" }} role="alert">
              Too many failed sign-in attempts. Try again in {formatCountdown(lockoutSeconds)}.
            </p>
          ) : error ? (
            <p className="text-sm" style={{ color: "#d98b8b" }} role="alert">
              {error}
            </p>
          ) : null}
          <button
            type="submit"
            className="pv-btn-ghost w-full"
            disabled={isSubmitting || isLockedOut}
          >
            {isSubmitting
              ? "Signing In..."
              : isLockedOut
                ? `Try again in ${formatCountdown(lockoutSeconds)}`
                : "Sign in with password"}
          </button>
          <p className="text-center text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Password sign-in remains available.
          </p>
        </form>

        <p
          className="mt-8 text-center text-xs tracking-wide"
          style={{ color: "var(--pv-text-dim)" }}
        >
          Self-hosted Personal Vault
        </p>
      </div>
    </div>
  );
}
