import { createFileRoute } from "@tanstack/react-router";
import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ActionProgress } from "@/components/pv/ActionProgress";

export const Route = createFileRoute("/app/vault-control/intake")({ component: IntakePage });

type Receipt = {
  id: string;
  filename: string;
  status: string;
  rejection_reason: string | null;
  created_at: string;
};
type Intake = {
  gate: { state: "open" | "pausing" | "paused" | "resuming" | "error"; active_transfers: number };
  sources: { id: string; name: string; status: string; updated_at: string }[];
  arrival_hall: { waiting: number; processing: number; needs_review: number | null };
  processing: Receipt[];
  failed: Receipt[];
  receipts: Receipt[];
  audits: { name: string; status: string; findings: number }[];
};

function IntakePage() {
  const [data, setData] = useState<Intake | null>(null);
  const [limit, setLimit] = useState(20);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch(`/api/vault-control/intake?limit=${limit}`, {
        credentials: "include",
      });
      if (!response.ok) throw new Error();
      setData((await response.json()) as Intake);
      setError(null);
    } catch {
      setError("Current Intake status is unavailable.");
    } finally {
      setLoading(false);
    }
  }, [limit]);
  useEffect(() => {
    void load();
  }, [load]);
  const changeGate = async () => {
    if (!data) return;
    const action = data.gate.state === "open" ? "pause" : "resume";
    const response = await fetch(`/api/vault-control/intake/gate/${action}`, {
      method: "POST",
      credentials: "include",
    });
    if (response.ok) void load();
    else setError("The Global Intake Gate could not be updated.");
  };
  if (!data)
    return (
      <section className="space-y-4">
        <h2 className="pv-display-title text-3xl md:text-4xl">Intake</h2>
        <p style={{ color: "var(--pv-text-dim)" }}>{error ?? "Loading Intake status…"}</p>
      </section>
    );
  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="pv-display-title text-3xl md:text-4xl">Intake</h2>
        <button className="pv-btn-secondary" disabled={loading} onClick={() => void load()}>
          <RefreshCw size={16} /> Refresh
        </button>
      </div>
      {data.failed.length > 0 && (
        <section className="pv-panel border-[var(--pv-border-strong)] p-5">
          <h3 className="pv-content-title text-xl">Failed / Unfinished Imports</h3>
          {data.failed.map((item) => (
            <p key={item.id} className="mt-3 text-sm" style={{ color: "#e8a0a0" }}>
              {item.filename} · {item.status} · {item.rejection_reason ?? "Reason unavailable"} ·
              Retry unavailable
            </p>
          ))}
        </section>
      )}
      <section className="pv-panel p-5">
        <h3 className="pv-content-title text-xl">Global Intake Gate</h3>
        <div className="mt-3">
          <ActionProgress
            state={
              data.gate.state === "pausing" || data.gate.state === "resuming" ? "running" : "idle"
            }
            label={
              data.gate.state === "pausing"
                ? "Pausing Intake"
                : data.gate.state === "resuming"
                  ? "Resuming Intake"
                  : data.gate.state[0].toUpperCase() + data.gate.state.slice(1)
            }
          />
        </div>
        <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
          {data.gate.state === "open"
            ? "New Intake files may start."
            : "New Intake files are suspended; an in-flight file may finish safely."}{" "}
          Active transfers: {data.gate.active_transfers}.
        </p>
        <button className="pv-btn-secondary mt-4" onClick={() => void changeGate()}>
          {data.gate.state === "open" ? "Pause Intake" : "Resume Intake"}
        </button>
      </section>
      <section className="grid gap-4 md:grid-cols-2">
        <Block title="Automated Sources">
          {data.sources.length ? (
            data.sources.map((source) => (
              <p key={source.id} className="text-sm">
                {source.name} · {source.status}
              </p>
            ))
          ) : (
            <Quiet>No configured automated sources.</Quiet>
          )}
        </Block>
        <Block title="Arrival Hall">
          <p className="text-sm">
            Waiting: {data.arrival_hall.waiting} · Processing: {data.arrival_hall.processing} ·
            Needs review: {data.arrival_hall.needs_review ?? "Unavailable"}
          </p>
        </Block>
      </section>
      <Block title="Processing Status">
        {data.processing.length ? (
          data.processing.map((item) => (
            <div key={item.id} className="flex items-center justify-between gap-3 text-sm">
              <span>{item.filename}</span>
              <ActionProgress
                state="running"
                label={item.status === "scanning" ? "Scanning" : "Processing"}
              />
            </div>
          ))
        ) : (
          <Quiet>No active Intake jobs.</Quiet>
        )}
      </Block>
      <Block title="Intake Receipts">
        {data.receipts.map((item) => (
          <p key={item.id} className="text-sm">
            {item.filename} · {item.status}
          </p>
        ))}
        {limit === 20 && (
          <button className="pv-btn-secondary mt-4" onClick={() => setLimit(50)}>
            View more
          </button>
        )}
      </Block>
      <Block title="Audits">
        {data.audits.map((audit) => (
          <p key={audit.name} className="text-sm">
            {audit.name} · {audit.status} · {audit.findings} finding(s)
          </p>
        ))}
      </Block>
    </div>
  );
}
function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="pv-panel p-5">
      <h3 className="pv-content-title text-xl">{title}</h3>
      <div className="mt-3 space-y-2">{children}</div>
    </section>
  );
}
function Quiet({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
      {children}
    </p>
  );
}
