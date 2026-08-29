import { createFileRoute, Link } from "@tanstack/react-router";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

export const Route = createFileRoute("/app/vault-control/overview")({
  component: OverviewPage,
});

type Issue = {
  severity: "warning" | "critical";
  message: string;
};

type Overview = {
  overall_health: "healthy" | "attention_required" | "critical";
  database: { status: "healthy" | "degraded" | "offline"; response_ms: number | null };
  capacity: { total_bytes: number | null; free_bytes: number | null; low_space: string[] };
  cpu: { load: number | null; temperature_c: number | null };
  gpu: { load: number | null; temperature_c: number | null };
  jobs: { running: number; queued: number; failed: number; unfinished: number } | null;
  issues: Issue[];
  attention: string[];
};

const HEALTH_LABELS: Record<Overview["overall_health"], string> = {
  healthy: "Healthy",
  attention_required: "Attention required",
  critical: "Critical",
};

function formatBytes(value: number | null) {
  if (value === null) return "Unavailable";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatMetric(value: number | null, unit: string) {
  return value === null ? "Unavailable" : `${value}${unit}`;
}

function OverviewPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadOverview = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch("/api/vault-control/overview", {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Overview is unavailable");
      setOverview((await response.json()) as Overview);
    } catch {
      setError("Current Vault status could not be retrieved.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  if (loading && !overview) {
    return <p style={{ color: "var(--pv-text-dim)" }}>Loading current Vault status…</p>;
  }

  if (!overview) {
    return (
      <section className="space-y-4">
        <p style={{ color: "var(--pv-text-dim)" }}>{error ?? "Unavailable"}</p>
        <button className="pv-btn-secondary" onClick={() => void loadOverview()}>
          <RefreshCw size={16} /> Refresh
        </button>
      </section>
    );
  }

  const databaseLabel =
    overview.database.status.charAt(0).toUpperCase() + overview.database.status.slice(1);

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center justify-end">
        <Link to="/app/vault-control/federation" className="pv-btn-ghost mr-2 px-3 py-2 text-xs">
          Incoming Vault Shares
        </Link>
        <button className="pv-btn-secondary" disabled={loading} onClick={() => void loadOverview()}>
          <RefreshCw size={16} className={loading ? "animate-spin" : undefined} />
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {overview.attention.length > 0 && (
        <section className="pv-panel border-[var(--pv-border-strong)] p-5 md:p-6">
          <div className="flex items-center gap-2">
            <AlertTriangle size={18} style={{ color: "var(--pv-gold)" }} />
            <h2 className="pv-content-title text-xl">Anything requiring attention</h2>
          </div>
          <ul className="mt-3 space-y-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            {overview.attention.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        </section>
      )}

      <section className="pv-panel p-5 md:p-6">
        <p className="text-xs uppercase tracking-widest" style={{ color: "var(--pv-text-dim)" }}>
          Overall Vault health
        </p>
        <p className="pv-content-title mt-2 text-2xl">{HEALTH_LABELS[overview.overall_health]}</p>
        <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
          {overview.overall_health === "healthy"
            ? "No administrator action is required."
            : "Review the active items above."}
        </p>
      </section>

      <section className="grid gap-4 md:grid-cols-2">
        <StatusPanel title="Database">
          <p className="text-lg font-semibold">{databaseLabel}</p>
          <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            {overview.database.response_ms === null
              ? "Unavailable"
              : `${overview.database.response_ms} ms response time`}
          </p>
        </StatusPanel>

        <StatusPanel title="Vault Capacity">
          {overview.capacity.free_bytes === null || overview.capacity.total_bytes === null ? (
            <p className="text-lg font-semibold">Unavailable</p>
          ) : (
            <>
              <p className="text-lg font-semibold">
                {formatBytes(overview.capacity.free_bytes)} free
              </p>
              <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
                {formatBytes(overview.capacity.total_bytes)} total
              </p>
            </>
          )}
          {overview.capacity.low_space.map((storage) => (
            <p key={storage} className="mt-3 text-sm" style={{ color: "var(--pv-gold)" }}>
              {storage} storage is low on space
            </p>
          ))}
        </StatusPanel>

        <StatusPanel title="CPU">
          <p className="text-lg font-semibold">{formatMetric(overview.cpu.load, " load")}</p>
          <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            {formatMetric(overview.cpu.temperature_c, "°C")}
          </p>
        </StatusPanel>

        <StatusPanel title="GPU">
          <p className="text-lg font-semibold">{formatMetric(overview.gpu.load, "%")}</p>
          <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            {formatMetric(overview.gpu.temperature_c, "°C")}
          </p>
        </StatusPanel>
      </section>

      <section className="pv-panel p-5 md:p-6">
        <h2 className="pv-content-title text-xl">Jobs</h2>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 text-sm">
          {(["running", "queued", "failed", "unfinished"] as const).map((status) => (
            <div key={status}>
              <p
                className="uppercase tracking-wider text-xs"
                style={{ color: "var(--pv-text-dim)" }}
              >
                {status}
              </p>
              <p className="mt-1 text-lg font-semibold">
                {overview.jobs?.[status] ?? "Unavailable"}
              </p>
            </div>
          ))}
        </div>
      </section>

      <section className="pv-panel p-5 md:p-6">
        <h2 className="pv-content-title text-xl">Errors &amp; Warnings</h2>
        {overview.issues.length === 0 ? (
          <p className="mt-3 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Errors &amp; Warnings: 0
          </p>
        ) : (
          <ul className="mt-4 space-y-3 text-sm">
            {overview.issues.map((issue) => (
              <li
                key={issue.message}
                className="border-l-2 pl-3"
                style={{
                  borderColor: issue.severity === "critical" ? "#b85c5c" : "var(--pv-gold)",
                  color: issue.severity === "critical" ? "#e8a0a0" : "var(--pv-text-dim)",
                }}
              >
                {issue.message}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function StatusPanel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="pv-panel p-5">
      <h2 className="pv-content-title text-xl">{title}</h2>
      <div className="mt-4">{children}</div>
    </section>
  );
}
