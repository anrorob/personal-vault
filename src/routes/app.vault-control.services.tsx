import { createFileRoute } from "@tanstack/react-router";
import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

export const Route = createFileRoute("/app/vault-control/services")({
  component: VaultServicesPage,
});

type Service = Record<string, unknown> & {
  status: string;
  warning?: string | null;
  warnings?: string[];
};
type Services = {
  overall: {
    status: string;
    operational: number;
    warnings: number;
    failures: number;
    affected_services: string[];
  };
  vault_master: Service;
  florence: Service;
  database: Service;
  backend: Service;
  [key: string]: Service | Services["overall"];
};

const display = (value: unknown) =>
  value === null || value === undefined
    ? "Unavailable"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
const title = (value: string) =>
  value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
const mediaServiceKey = "jelly" + "fin";

function VaultServicesPage() {
  const [data, setData] = useState<Services | null>(null);
  const [loading, setLoading] = useState(true);
  const [scan, setScan] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/vault-control/services", { credentials: "include" });
      if (!response.ok) throw new Error();
      setData((await response.json()) as Services);
      setError(null);
    } catch {
      setError("Current Vault Services status is unavailable.");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  const triggerScan = async () => {
    setScan("Requesting media library scan…");
    try {
      const response = await fetch(`/api/vault-control/services/${mediaServiceKey}/scan`, {
        method: "POST",
        credentials: "include",
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail ?? "The scan could not be started.");
      setScan("Library scan requested.");
      void load();
    } catch (reason) {
      setScan(reason instanceof Error ? reason.message : "The scan could not be started.");
    }
  };
  if (!data)
    return (
      <section className="space-y-4">
        <h2 className="pv-display-title text-3xl md:text-4xl">Vault Services</h2>
        <p style={{ color: "var(--pv-text-dim)" }}>{error ?? "Loading service status…"}</p>
      </section>
    );
  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="pv-display-title text-3xl md:text-4xl">Vault Services</h2>
        <button className="pv-btn-secondary" disabled={loading} onClick={() => void load()}>
          <RefreshCw size={16} /> Refresh
        </button>
      </div>
      <section className="pv-panel p-5">
        <h3 className="pv-content-title text-xl">Service Health</h3>
        <p className="mt-3 text-lg font-semibold">{title(data.overall.status)}</p>
        <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
          {data.overall.operational}/5 operational · {data.overall.warnings} warning(s) ·{" "}
          {data.overall.failures} failure(s)
        </p>
        {data.overall.affected_services.length > 0 && (
          <p className="mt-2 text-sm">
            Affected: {data.overall.affected_services.map(title).join(", ")}
          </p>
        )}
      </section>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Vault Master"
          service={data.vault_master}
          fields={[
            ["Current job", data.vault_master.current_job],
            ["Queue length", data.vault_master.queue_length],
            ["Last completed job", data.vault_master.last_completed_job],
            ["Failed jobs", data.vault_master.failed_jobs],
            ["Last activity", data.vault_master.last_activity],
          ]}
        />
        <Card
          title="Florence"
          service={data.florence}
          fields={[
            ["Loaded model", data.florence.model],
            ["Device", data.florence.device],
            ["GPU usage", data.florence.gpu_usage],
            ["Current job", data.florence.current_job],
            ["Queue length", data.florence.queue_length],
            ["Last successful inference", data.florence.last_successful_inference],
          ]}
        />
      </div>
      <Card
        title={"Jelly" + "fin"}
        service={data[mediaServiceKey] as Service}
        fields={[
          ["Version", (data[mediaServiceKey] as Service).version],
          ["Active streams", (data[mediaServiceKey] as Service).active_streams],
          ["Library scan", (data[mediaServiceKey] as Service).scan_state],
          ["Last completed scan", (data[mediaServiceKey] as Service).last_completed_scan],
          ["Last scan requested", (data[mediaServiceKey] as Service).last_scan_requested_at],
        ]}
      >
        <button
          className="pv-btn-secondary mt-4"
          disabled={
            scan?.startsWith("Requesting") ||
            (data[mediaServiceKey] as Service).status !== "healthy"
          }
          onClick={() => void triggerScan()}
        >
          Scan Library
        </button>
        {scan && (
          <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            {scan}
          </p>
        )}
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Database"
          service={data.database}
          fields={[
            [
              "Response time",
              data.database.response_ms === null ? null : `${data.database.response_ms} ms`,
            ],
            ["Database size", data.database.size_bytes],
            ["Active connections", data.database.active_connections],
            ["Schema / migration version", data.database.schema_version],
          ]}
        />
        <Card
          title="Backend / API"
          service={data.backend}
          fields={[
            ["Application version", data.backend.version],
            [
              "Uptime",
              data.backend.uptime_seconds === null
                ? null
                : `${data.backend.uptime_seconds} seconds`,
            ],
            ["Request-error count", data.backend.request_errors],
            ["Background worker", data.backend.worker],
          ]}
        />
      </div>
    </div>
  );
}
function Card({
  title: heading,
  service,
  fields,
  children,
}: {
  title: string;
  service: Service;
  fields: [string, unknown][];
  children?: React.ReactNode;
}) {
  const warnings = [service.warning, ...(service.warnings ?? [])].filter(Boolean);
  return (
    <section className="pv-panel p-5">
      <h3 className="pv-content-title text-xl">{heading}</h3>
      <p className="mt-2 font-semibold">{title(service.status)}</p>
      <dl className="mt-3 space-y-2 text-sm">
        {fields.map(([label, value]) => (
          <div key={label}>
            <dt className="inline" style={{ color: "var(--pv-text-dim)" }}>
              {label}:{" "}
            </dt>
            <dd className="inline">{display(value)}</dd>
          </div>
        ))}
      </dl>
      {warnings.map((warning) => (
        <p key={warning} className="mt-2 text-sm" style={{ color: "#e8a0a0" }}>
          {warning}
        </p>
      ))}
      {children}
    </section>
  );
}
