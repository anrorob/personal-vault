import { createFileRoute, Outlet, redirect } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { getAuthSession } from "@/lib/auth";
import { elevateVaultControl, passkeysSupported } from "@/lib/passkeys";

export const Route = createFileRoute("/app/vault-control")({
  beforeLoad: async () => {
    const session = await getAuthSession();
    if (!session.authenticated) throw redirect({ to: "/login" });
    if (session.role !== "administrator") throw redirect({ to: "/app" });
  },
  component: VaultControlGate,
});

function VaultControlGate() {
  const [elevated, setElevated] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      const response = await fetch("/api/auth/vault-control/elevation", { credentials: "include" });
      if (active)
        setElevated(
          response.ok && Boolean(((await response.json()) as { elevated?: boolean }).elevated),
        );
    };
    void refresh();
    const interval = window.setInterval(() => void refresh(), 15_000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, []);

  const confirmIdentity = async () => {
    setBusy(true);
    setError(null);
    try {
      await elevateVaultControl();
      setElevated(true);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Vault Control identity confirmation failed.",
      );
    } finally {
      setBusy(false);
    }
  };

  if (elevated) return <Outlet />;
  return (
    <main className="mx-auto max-w-xl px-6 py-16">
      <h1 className="pv-content-title text-3xl">Confirm your identity</h1>
      <p className="mt-4 text-muted-foreground">
        Vault Control requires a fresh passkey confirmation. Your normal Personal Vault session
        remains active.
      </p>
      <button
        className="mt-8 rounded bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
        onClick={() => void confirmIdentity()}
        disabled={busy || !passkeysSupported()}
      >
        {busy ? "Waiting for passkey…" : "Confirm with passkey"}
      </button>
      {!passkeysSupported() && (
        <p className="mt-3 text-sm text-destructive">This browser does not support passkeys.</p>
      )}
      {error && <p className="mt-3 text-sm text-destructive">{error}</p>}
    </main>
  );
}
