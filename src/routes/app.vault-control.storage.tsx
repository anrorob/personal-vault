import { createFileRoute } from "@tanstack/react-router";
import { Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export const Route = createFileRoute("/app/vault-control/storage")({ component: StoragePage });

type Disk = {
  id: string;
  model: string | null;
  serial: string | null;
  wwn: string | null;
  device_path: string | null;
  capacity_bytes: number | null;
  filesystem_capacity_bytes?: number | null;
  filesystem_uuid?: string | null;
  physical_mount?: string | null;
  used_bytes: number | null;
  free_bytes: number | null;
  reserved_bytes?: number | null;
  state: string;
  status_detail?: string;
  integration_mode?: "legacy_direct" | "slot_managed";
  lifecycle_eligible?: boolean;
  production_lifecycle_eligible?: boolean;
  mounted: boolean;
  areas: string[];
  temperature_c: number | null;
  smart_status: string | null;
  replaces: string | null;
  replaced_by: string | null;
};
type Device = {
  hardware_id: string | null;
  model: string | null;
  serial: string | null;
  wwn: string | null;
  device_path: string;
  capacity_bytes: number | null;
  has_existing_filesystem?: boolean;
  safety?: "ready" | "blocked";
  reason?: string | null;
};
type AddDriveContext = {
  status: "available" | "unavailable";
  operations_enabled: boolean;
  eligible_areas: string[];
  candidates: Device[];
  active_operation: { state?: string } | null;
};
type RetirePreflight = {
  slot_id: string;
  state: "eligible" | "blocked";
  areas: string[];
  canonical_file_count: number;
  canonical_bytes: number;
};
type SwapContext = {
  slot_id: string;
  swap_enabled: boolean;
  candidates: Device[];
  active_operation: StorageOperation | null;
};
type StorageOperation = {
  operation_id: string;
  operation: string;
  source_disk_id?: string | null;
  state: string;
  error?: string | null;
  resume_error?: string | null;
  cutover_failure?: {
    stage?: string | null;
    predicate?: string | null;
    mountpoint?: string | null;
  } | null;
  lifecycle?: Record<string, "pending" | "active" | "complete">;
  safe_to_disconnect?: boolean;
};
type Inventory = {
  status: "available" | "unavailable";
  operations_enabled?: boolean;
  generated_at: string | null;
  summary: {
    total_bytes: number;
    used_bytes: number;
    free_bytes: number;
    health: string;
    active_disk_count: number;
    unassigned_device_count: number;
  } | null;
  disks: Disk[];
  unassigned_devices: Device[];
  verification: { status: string; results: string[] } | null;
};

const bytes = (value: number | null) => {
  if (value === null) return "Unavailable";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(index ? 1 : 0)} ${units[index]}`;
};

function StoragePage() {
  const [data, setData] = useState<Inventory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [wizard, setWizard] = useState<
    { kind: "add" } | { kind: "swap"; disk: Disk } | { kind: "retire"; disk: Disk } | null
  >(null);
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch("/api/vault-control/storage", { credentials: "include" });
      if (!response.ok) throw new Error();
      setData((await response.json()) as Inventory);
    } catch {
      setError("Storage information is unavailable.");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  if (!data || data.status === "unavailable")
    return (
      <section className="space-y-4">
        <h2 className="pv-display-title text-3xl md:text-4xl">Storage</h2>
        <p style={{ color: "var(--pv-text-dim)" }}>
          {error ?? "Host storage telemetry is unavailable."}
        </p>
        <button className="pv-btn-ghost inline-flex items-center gap-2" onClick={() => void load()}>
          <RefreshCw size={16} /> Refresh
        </button>
      </section>
    );
  const summary = data.summary;
  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center justify-between gap-4">
        <h2 className="pv-display-title text-3xl md:text-4xl">Storage</h2>
      </div>
      <section className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="pv-content-title text-xl">Commissioned Drives</h3>
            {summary && (
              <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
                {summary.active_disk_count} active slots · {summary.health}
              </p>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              className="pv-btn-ghost inline-flex items-center gap-2 whitespace-nowrap px-3 py-2 text-sm"
              disabled={loading}
              onClick={() => void load()}
            >
              <RefreshCw size={16} className={loading ? "animate-spin" : undefined} />
              {loading ? "Refreshing…" : "Refresh"}
            </button>
            <button
              className="pv-btn-primary inline-flex items-center gap-2 whitespace-nowrap px-3 py-2 text-sm"
              onClick={() => setWizard({ kind: "add" })}
            >
              <Plus size={16} /> Add drive
            </button>
          </div>
        </div>
        {data.disks.map((disk) => (
          <article key={disk.id} className="pv-panel p-4 md:p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h4 className="pv-content-title text-lg">{disk.id}</h4>
                <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
                  Current drive · {disk.model ?? "Unavailable"}
                </p>
              </div>
              <p className="text-sm font-semibold">{disk.status_detail ?? disk.state}</p>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-5 text-sm">
              <Metric label="Capacity" value={bytes(disk.capacity_bytes)} />
              <Metric label="Used" value={bytes(disk.used_bytes)} />
              <Metric label="Available" value={bytes(disk.free_bytes)} />
              <Metric label="Health" value={disk.smart_status ?? "Unavailable"} />
              <Metric
                label="Temperature"
                value={disk.temperature_c === null ? "Unavailable" : `${disk.temperature_c} °C`}
              />
            </div>
            <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--pv-border)" }}>
              <div className="min-w-0">
                <p
                  className="text-xs uppercase tracking-wider"
                  style={{ color: "var(--pv-text-dim)" }}
                >
                  Vault Areas
                </p>
                <p className="mt-1 text-sm font-semibold">
                  {disk.areas.length ? disk.areas.join(" · ") : "None"}
                </p>
              </div>
              <div className="mt-3 grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
                <details className="min-w-0 text-sm" style={{ color: "var(--pv-text-dim)" }}>
                  <summary className="cursor-pointer">Technical details</summary>
                  <div className="mt-2 grid gap-2 sm:grid-cols-2">
                    <span>Device: {disk.device_path ?? "Unavailable"}</span>
                    <span>Serial: {disk.serial ?? "Unavailable"}</span>
                    <span>WWN: {disk.wwn ?? "Unavailable"}</span>
                    <span>Filesystem UUID: {disk.filesystem_uuid ?? "Unavailable"}</span>
                    <span>Physical mount: {disk.physical_mount ?? "Unavailable"}</span>
                    <span>Mount status: {disk.mounted ? "Mounted" : "Unmounted"}</span>
                    <span>
                      Integration:{" "}
                      {disk.integration_mode === "legacy_direct"
                        ? "Legacy direct topology"
                        : "Managed slot"}
                    </span>
                    <span>Filesystem reserved: {bytes(disk.reserved_bytes ?? null)}</span>
                  </div>
                </details>
                <div className="flex flex-wrap gap-2 sm:shrink-0 sm:justify-end">
                  {disk.state === "Active" && disk.lifecycle_eligible && (
                    <button
                      className="pv-btn-ghost inline-flex shrink-0 items-center gap-2 whitespace-nowrap px-3 py-2 text-sm"
                      onClick={() => setWizard({ kind: "swap", disk })}
                    >
                      Swap drive
                    </button>
                  )}
                  {disk.state === "Active" && disk.production_lifecycle_eligible && (
                    <button
                      className="pv-btn-ghost inline-flex shrink-0 items-center gap-2 whitespace-nowrap px-3 py-2 text-sm text-red-200"
                      onClick={() => setWizard({ kind: "retire", disk })}
                    >
                      Retire drive
                    </button>
                  )}
                </div>
              </div>
            </div>
            {disk.replaces && (
              <p className="mt-4 text-sm" style={{ color: "var(--pv-text-dim)" }}>
                Replaces {disk.replaces}
              </p>
            )}
            {disk.replaced_by && (
              <p className="mt-4 text-sm" style={{ color: "var(--pv-text-dim)" }}>
                Replaced by {disk.replaced_by}
              </p>
            )}
          </article>
        ))}
      </section>
      <StorageWizard
        wizard={wizard}
        onClose={() => setWizard(null)}
        onCompleted={() => void load()}
      />
    </div>
  );
}
function StorageWizard({
  wizard,
  onClose,
  onCompleted,
}: {
  wizard: { kind: "add" } | { kind: "swap"; disk: Disk } | { kind: "retire"; disk: Disk } | null;
  onClose: () => void;
  onCompleted: () => void;
}) {
  const swapping = wizard?.kind === "swap";
  const retiring = wizard?.kind === "retire";
  const disk = swapping || retiring ? wizard.disk : null;
  const [context, setContext] = useState<AddDriveContext | null>(null);
  const [selectedHardwareId, setSelectedHardwareId] = useState("");
  const [area, setArea] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retirement, setRetirement] = useState<RetirePreflight | null>(null);
  const [retireOperation, setRetireOperation] = useState<StorageOperation | null>(null);
  const [swapContext, setSwapContext] = useState<SwapContext | null>(null);
  const [swapOperation, setSwapOperation] = useState<StorageOperation | null>(null);
  useEffect(() => {
    if (!wizard || swapping || retiring) return;
    setContext(null);
    setSelectedHardwareId("");
    setArea("");
    setConfirmation("");
    setError(null);
    void fetch("/api/vault-control/storage/add-drive", { credentials: "include" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Drive discovery is unavailable.");
        return (await response.json()) as AddDriveContext;
      })
      .then((nextContext) => {
        const candidates = nextContext.candidates.filter(
          (candidate) => candidate.safety === "ready" && Boolean(candidate.hardware_id),
        );
        setContext(nextContext);
        setSelectedHardwareId(candidates.length === 1 ? (candidates[0].hardware_id ?? "") : "");
      })
      .catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : "Drive discovery is unavailable."),
      );
  }, [retiring, swapping, wizard]);
  useEffect(() => {
    if (!swapping || !disk) return;
    setSwapContext(null);
    setSwapOperation(null);
    setConfirmation("");
    setError(null);
    void fetch(`/api/vault-control/storage/swap/${encodeURIComponent(disk.id)}`, {
      credentials: "include",
    })
      .then(async (response) => {
        const body = (await response.json()) as SwapContext & { detail?: string };
        if (!response.ok)
          throw new Error(body.detail ?? "Replacement drive discovery is unavailable.");
        return body;
      })
      .then((nextContext) => {
        setSwapContext(nextContext);
        setSelectedHardwareId(
          nextContext.candidates.length === 1 ? (nextContext.candidates[0].hardware_id ?? "") : "",
        );
      })
      .catch((reason: unknown) =>
        setError(
          reason instanceof Error ? reason.message : "Replacement drive discovery is unavailable.",
        ),
      );
  }, [disk, swapping]);
  useEffect(() => {
    if (!retiring || !disk) return;
    setRetirement(null);
    setRetireOperation(null);
    setConfirmation("");
    setError(null);
    void fetch(`/api/vault-control/storage/retire/${encodeURIComponent(disk.id)}`, {
      credentials: "include",
    })
      .then(async (response) => {
        const body = (await response.json()) as RetirePreflight & { detail?: string };
        if (!response.ok) throw new Error(body.detail ?? "Retirement preflight is unavailable.");
        setRetirement(body);
      })
      .catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : "Retirement preflight is unavailable."),
      );
    void fetch("/api/vault-control/storage/operations", { credentials: "include" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Retirement status is unavailable.");
        return (await response.json()) as StorageOperation[];
      })
      .then((operations) => {
        const existing = operations.find(
          (operation) =>
            operation.operation === "retire_slot" && operation.source_disk_id === disk.id,
        );
        setRetireOperation(existing ?? null);
      })
      .catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : "Retirement status is unavailable."),
      );
  }, [disk, retiring]);
  useEffect(() => {
    if (
      !retiring ||
      !retireOperation ||
      ["completed", "failed", "cancelled"].includes(retireOperation.state)
    )
      return;
    const timer = window.setInterval(() => {
      void fetch("/api/vault-control/storage/operations", { credentials: "include" })
        .then(async (response) => (await response.json()) as StorageOperation[])
        .then((operations) =>
          setRetireOperation(
            operations.find(
              (operation) => operation.operation_id === retireOperation.operation_id,
            ) ?? retireOperation,
          ),
        )
        .catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [retireOperation, retiring]);
  useEffect(() => {
    if (
      !swapping ||
      !swapOperation ||
      ["completed", "failed", "cancelled"].includes(swapOperation.state)
    )
      return;
    const timer = window.setInterval(() => {
      void fetch("/api/vault-control/storage/operations", { credentials: "include" })
        .then(async (response) => (await response.json()) as StorageOperation[])
        .then((operations) =>
          setSwapOperation(
            operations.find((operation) => operation.operation_id === swapOperation.operation_id) ??
              swapOperation,
          ),
        )
        .catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [swapOperation, swapping]);
  // Frontend filtering is defence in depth only. The backend supplies this
  // context and the privileged executor independently revalidates it.
  const candidates = (context?.candidates ?? []).filter(
    (candidate) => candidate.safety === "ready" && Boolean(candidate.hardware_id),
  );
  const selected =
    candidates.find((candidate) => candidate.hardware_id === selectedHardwareId) ?? null;
  const prepare = async () => {
    if (!selected?.hardware_id || !area || confirmation !== "PREPARE DRIVE") return;
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/vault-control/storage/operations", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          operation: "commission_add",
          target_hardware_id: selected.hardware_id,
          vault_area: area,
          confirmation,
        }),
      });
      const body = (await response.json()) as { detail?: string };
      if (!response.ok) throw new Error(body.detail ?? "The drive could not be prepared.");
      onCompleted();
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The drive could not be prepared.");
    } finally {
      setSubmitting(false);
    }
  };
  const retire = async () => {
    if (!disk || retirement?.state !== "eligible" || confirmation !== "RETIRE DRIVE") return;
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/vault-control/storage/operations", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation: "retire_slot", source_disk_id: disk.id, confirmation }),
      });
      const body = (await response.json()) as { detail?: string; request_id?: string };
      if (!response.ok) throw new Error(body.detail ?? "The slot could not be retired.");
      if (!body.request_id)
        throw new Error("The retirement operation did not return durable status.");
      setRetireOperation({
        operation_id: body.request_id,
        operation: "retire_slot",
        source_disk_id: disk.id,
        state: "queued",
      });
      onCompleted();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The slot could not be retired.");
    } finally {
      setSubmitting(false);
    }
  };
  const startSwap = async () => {
    if (!disk || !swapContext?.swap_enabled || !selectedHardwareId || confirmation !== disk.id)
      return;
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/vault-control/storage/operations", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          operation: "start_replacement",
          source_disk_id: disk.id,
          target_hardware_id: selectedHardwareId,
          confirmation,
        }),
      });
      const body = (await response.json()) as { detail?: string; request_id?: string };
      if (!response.ok || !body.request_id)
        throw new Error(body.detail ?? "The Swap Drive request could not be created.");
      setSwapOperation({
        operation_id: body.request_id,
        operation: "start_replacement",
        source_disk_id: disk.id,
        state: "queued",
      });
      onCompleted();
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "The Swap Drive request could not be created.",
      );
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <Dialog open={wizard !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        className="pv-dialog"
        style={{ background: "#08080a", borderColor: "var(--pv-border)" }}
      >
        <DialogHeader>
          <DialogTitle>
            {swapping ? `Swap ${disk?.id}` : retiring ? `Retire ${disk?.id}` : "Add a drive"}
          </DialogTitle>
          <DialogDescription>
            {swapping ? disk?.areas.join(" · ") : "Commission a new persistent storage slot"}
          </DialogDescription>
        </DialogHeader>
        {retiring ? (
          <div className="space-y-4 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            {error && (
              <p role="alert" className="font-semibold text-red-300">
                {error}
              </p>
            )}
            {!retirement && !error && <p>Checking {disk?.id}…</p>}
            {retireOperation ? (
              <RetireProgress operation={retireOperation} diskId={disk?.id ?? "this drive"} />
            ) : (
              retirement && (
                <>
                  <p>Vault Areas: {retirement.areas.join(" · ") || "None"}</p>
                  <p>Canonical files: {retirement.canonical_file_count}</p>
                  <p>Stored canonical data: {bytes(retirement.canonical_bytes)}</p>
                  {retirement.state === "blocked" ? (
                    <p>
                      This drive cannot be retired yet because it contains canonical Vault content.
                      Migrate the files explicitly first.
                    </p>
                  ) : (
                    <>
                      <p>
                        This slot can be retired without migrating content. Its historical identity
                        remains, and the physical drive will not be erased.
                      </p>
                      <label className="block space-y-1">
                        <span>
                          Type <strong>RETIRE DRIVE</strong> to confirm
                        </span>
                        <input
                          className="pv-input w-full"
                          value={confirmation}
                          onChange={(event) => setConfirmation(event.target.value)}
                        />
                      </label>
                      <button
                        className="pv-btn-ghost inline-flex items-center text-red-200"
                        disabled={confirmation !== "RETIRE DRIVE" || submitting}
                        onClick={() => void retire()}
                      >
                        {submitting ? "Retiring…" : "Retire this drive"}
                      </button>
                    </>
                  )}
                </>
              )
            )}
          </div>
        ) : swapping ? (
          <div className="space-y-3 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            {error && (
              <p role="alert" className="font-semibold text-red-300">
                {error}
              </p>
            )}
            {swapOperation ? (
              <SwapProgress operation={swapOperation} diskId={disk?.id ?? "this drive"} />
            ) : (
              <>
                <p>
                  Keep the current drive connected and unchanged until Vault Control confirms it is
                  safe to disconnect.
                </p>
                {!swapContext && !error && <p>Checking replacement hardware…</p>}
                {swapContext && swapContext.active_operation && (
                  <p>A storage operation is already active.</p>
                )}
                {swapContext && !swapContext.active_operation && !swapContext.swap_enabled && (
                  <p
                    role="status"
                    className="rounded border p-3"
                    style={{ borderColor: "var(--pv-border)" }}
                  >
                    Swap Drive is currently unavailable. Replacement hardware can be reviewed, but
                    no Swap request can be started until the root storage executor is enabled.
                  </p>
                )}
                {swapContext &&
                  !swapContext.active_operation &&
                  swapContext.swap_enabled &&
                  swapContext.candidates.length === 0 && (
                    <p>Connect an eligible unassigned replacement drive to the Vault host.</p>
                  )}
                {swapContext &&
                  !swapContext.active_operation &&
                  swapContext.swap_enabled &&
                  swapContext.candidates.length > 0 && (
                    <>
                      <label className="block space-y-1">
                        <span>Replacement drive</span>
                        <select
                          className="pv-input w-full"
                          value={selectedHardwareId}
                          onChange={(event) => setSelectedHardwareId(event.target.value)}
                        >
                          <option value="">Choose a drive</option>
                          {swapContext.candidates.map((candidate) => (
                            <option key={candidate.device_path} value={candidate.hardware_id ?? ""}>
                              {candidate.model ?? candidate.device_path} ·{" "}
                              {bytes(candidate.capacity_bytes)}
                            </option>
                          ))}
                        </select>
                      </label>
                      <p>
                        The replacement will receive a new filesystem. Canonical files are copied
                        and checksum-verified before any controlled cutover.
                      </p>
                      <label className="block space-y-1">
                        <span>
                          Type <strong>{disk?.id}</strong> to confirm
                        </span>
                        <input
                          className="pv-input w-full"
                          value={confirmation}
                          onChange={(event) => setConfirmation(event.target.value)}
                        />
                      </label>
                      <button
                        className="pv-btn-primary inline-flex items-center"
                        disabled={!selectedHardwareId || confirmation !== disk?.id || submitting}
                        onClick={() => void startSwap()}
                      >
                        {submitting ? "Starting Swap…" : "Start Swap Drive"}
                      </button>
                    </>
                  )}
              </>
            )}
          </div>
        ) : (
          <div className="space-y-4 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            <p>Connect a new drive to the Vault host.</p>
            {!context && !error && (
              <p className="font-semibold" style={{ color: "var(--pv-silver)" }}>
                Waiting for an unassigned drive…
              </p>
            )}
            {error && (
              <p role="alert" className="font-semibold text-red-300">
                {error}
              </p>
            )}
            {context?.active_operation && (
              <p role="alert">
                A storage operation is already active. Wait for it to complete before adding a
                drive.
              </p>
            )}
            {context && !context.active_operation && candidates.length === 0 && (
              <p className="font-semibold" style={{ color: "var(--pv-silver)" }}>
                Waiting for an unassigned drive…
              </p>
            )}
            {context && !context.active_operation && candidates.length > 0 && (
              <>
                {candidates.length > 1 && (
                  <label className="block space-y-1">
                    <span>Eligible drive</span>
                    <select
                      className="pv-input w-full"
                      value={selectedHardwareId}
                      onChange={(event) => setSelectedHardwareId(event.target.value)}
                    >
                      <option value="">Choose a drive</option>
                      {candidates.map((candidate) => (
                        <option key={candidate.device_path} value={candidate.hardware_id ?? ""}>
                          {candidate.model ?? candidate.device_path} ·{" "}
                          {bytes(candidate.capacity_bytes)}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                {selected && (
                  <div className="rounded border p-3" style={{ borderColor: "var(--pv-border)" }}>
                    <p className="font-semibold" style={{ color: "var(--pv-silver)" }}>
                      {candidates.length === 1 ? "Eligible drive" : "Drive ready"}
                    </p>
                    <p className="mt-1">
                      {selected.model ?? "Unknown model"} · {bytes(selected.capacity_bytes)}
                    </p>
                    <p>Serial: {selected.serial ?? "Unavailable"}</p>
                    {selected.has_existing_filesystem && (
                      <p className="mt-2">
                        Existing filesystem signatures were found. Preparing this drive will erase
                        them.
                      </p>
                    )}
                  </div>
                )}
                {!context.operations_enabled && (
                  <p
                    role="status"
                    className="rounded border p-3"
                    style={{ borderColor: "var(--pv-border)" }}
                  >
                    Add Drive commissioning is currently unavailable. You can review eligible
                    hardware, but preparation is disabled until this Vault’s storage authority is
                    enabled.
                  </p>
                )}
                <label className="block space-y-1">
                  <span>Where should this drive add capacity?</span>
                  <select
                    className="pv-input w-full"
                    value={area}
                    onChange={(event) => setArea(event.target.value)}
                    disabled={!selected}
                  >
                    <option value="">Choose a Vault Area</option>
                    {context.eligible_areas.map((item) => (
                      <option key={item}>{item}</option>
                    ))}
                  </select>
                </label>
                {selected && area && context.operations_enabled && (
                  <>
                    <p>
                      This will create the next persistent slot and permanently erase existing data
                      on this physical drive.
                    </p>
                    <label className="block space-y-1">
                      <span>
                        Type <strong>PREPARE DRIVE</strong> to confirm
                      </span>
                      <input
                        className="pv-input w-full"
                        value={confirmation}
                        onChange={(event) => setConfirmation(event.target.value)}
                      />
                    </label>
                    <button
                      className="pv-btn-primary inline-flex items-center"
                      disabled={confirmation !== "PREPARE DRIVE" || submitting}
                      onClick={() => void prepare()}
                    >
                      {submitting ? "Preparing…" : "Prepare this drive"}
                    </button>
                  </>
                )}
              </>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wider" style={{ color: "var(--pv-text-dim)" }}>
        {label}
      </p>
      <p className="mt-1 font-semibold">{value}</p>
    </div>
  );
}

function RetireProgress({ operation, diskId }: { operation: StorageOperation; diskId: string }) {
  const steps = [
    "Checking slot",
    "Removing from placement",
    "Updating Jellyfin",
    "Updating backup",
    "Releasing drive",
    "Verifying",
    "Complete",
  ];
  if (operation.state === "failed") {
    return (
      <p role="alert" className="font-semibold text-red-300">
        Retirement failed safely:{" "}
        {operation.error ?? "The executor did not complete the operation."}
      </p>
    );
  }
  if (operation.state === "completed") {
    return (
      <div className="space-y-2">
        <p className="font-semibold" style={{ color: "var(--pv-silver)" }}>
          {diskId} retired
        </p>
        <p>The physical drive can now be safely disconnected.</p>
      </div>
    );
  }
  return (
    <div className="space-y-2" role="status">
      <p className="font-semibold" style={{ color: "var(--pv-silver)" }}>
        Retirement in progress
      </p>
      {steps.map((step) => (
        <p key={step}>
          {operation.lifecycle?.[step] === "complete"
            ? "✓"
            : operation.lifecycle?.[step] === "active"
              ? "•"
              : "○"}{" "}
          {step}
        </p>
      ))}
    </div>
  );
}

function SwapProgress({ operation, diskId }: { operation: StorageOperation; diskId: string }) {
  const steps = [
    "Checking replacement",
    "Preparing replacement",
    "Copying canonical files",
    "Verifying checksums",
    "Switching the logical mount",
    "Verifying after restart",
    "Verifying integrations",
    "Complete",
  ];
  if (operation.state === "failed")
    return (
      <p role="alert" className="font-semibold text-red-300">
        Swap Drive stopped safely:{" "}
        {operation.cutover_failure?.predicate ??
          operation.error ??
          "The existing drive remains intact."}
      </p>
    );
  if (operation.safe_to_disconnect)
    return (
      <div className="space-y-2">
        <p className="font-semibold" style={{ color: "var(--pv-silver)" }}>
          {diskId} replacement complete
        </p>
        <p>Old drive is safe to disconnect.</p>
      </div>
    );
  return (
    <div className="space-y-2" role="status">
      <p className="font-semibold" style={{ color: "var(--pv-silver)" }}>
        Swap Drive in progress
      </p>
      {operation.resume_error && (
        <p role="alert" className="font-semibold text-amber-200">
          Swap Drive paused safely: {operation.resume_error}
        </p>
      )}
      {operation.state === "awaiting_finalisation" && (
        <p>Preparing automatic cutover from the verified replacement.</p>
      )}
      {operation.state === "awaiting_reboot_verification" && (
        <p>Restarting server for verification. Keep both drives connected and unchanged.</p>
      )}
      {steps.map((step) => (
        <p key={step}>
          {operation.lifecycle?.[step] === "complete"
            ? "✓"
            : operation.lifecycle?.[step] === "active"
              ? "•"
              : "○"}{" "}
          {step}
        </p>
      ))}
      <p>Keep the old drive connected and unchanged.</p>
    </div>
  );
}
