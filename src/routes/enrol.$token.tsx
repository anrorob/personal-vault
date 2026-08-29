import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { registerEnrolmentPasskey, passkeysSupported } from "@/lib/passkeys";

export const Route = createFileRoute("/enrol/$token")({ component: EnrolmentPage });

type InviteStatus = { display_name: string; expires_at: string; status: string };

function EnrolmentPage() {
  const { token } = Route.useParams();
  const [status, setStatus] = useState<InviteStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [complete, setComplete] = useState(false);

  useEffect(() => {
    void (async () => {
      const response = await fetch("/api/auth/enrolment/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (!response.ok) {
        setError("This enrolment link is invalid, expired, replaced, or has already been used.");
        return;
      }
      setStatus((await response.json()) as InviteStatus);
    })();
  }, [token]);

  async function createPasskey() {
    setError("");
    setBusy(true);
    try {
      await registerEnrolmentPasskey(token);
      setComplete(true);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Passkey enrolment could not be completed.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="pv flex min-h-screen items-center justify-center px-6 py-12">
      <section className="pv-panel w-full max-w-lg space-y-5 p-7">
        <h1 className="pv-content-title text-3xl">Set up your passkey</h1>
        {error && (
          <p role="alert" className="text-sm" style={{ color: "#d98b8b" }}>
            {error}
          </p>
        )}
        {complete ? (
          <p>
            Your passkey is ready. You can now return to Personal Vault and sign in with your
            passkey.
          </p>
        ) : status ? (
          <>
            <p>This link is for {status.display_name}&apos;s first Personal Vault passkey.</p>
            <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
              This link expires at {new Date(status.expires_at).toLocaleString()}.
            </p>
            <button
              className="pv-btn-primary"
              disabled={busy || !passkeysSupported()}
              onClick={() => void createPasskey()}
            >
              {busy ? "Creating passkey…" : "Create passkey"}
            </button>
            {!passkeysSupported() && (
              <p className="text-sm">This browser does not support passkeys.</p>
            )}
          </>
        ) : !error ? (
          <p>Checking your enrolment link…</p>
        ) : null}
      </section>
    </main>
  );
}
