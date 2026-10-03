import { useCallback, useEffect, useRef, useState } from "react";
import { ActionProgress } from "@/components/pv/ActionProgress";
import { validateKenRuns } from "@/lib/ken-binding";

import { KenTitles } from "@/components/pv/KenTitles";

type Engine = {
  display_name: string;
  model_revision: string;
  supported_input_modes: string[];
  status: string;
};
type Run = {
  id: string;
  asset_id: string;
  input_mode: string;
  status: string;
  created_at: string;
  processing_ms: number | null;
  configuration: {
    display_name: string;
    model_revision: string;
    runtime: string;
    correction_version?: string;
    draft_state?: string;
    automatic_title?: { status: string };
    correction_context?: {
      trusted_people?: { person_id: string; name: string }[];
      trusted_location?: { name: string; source: string } | null;
    };
    source?: { asset_id?: string };
    input_integrity?: { input_fingerprint: string; manifest: { asset_id: string } };
    result_binding_version?: string;
  };
  result: {
    description: string;
    warnings: string[];
    raw_response: string;
    asset_id?: string;
    run_id?: string;
    input_fingerprint?: string;
  } | null;
  error: string | null;
  failure?: {
    message: string;
    retryable: boolean;
    retry_allowed: boolean;
    failed_at: string | null;
  };
};
type Correction = {
  id: string;
  asset_id: string;
  sequence: number;
  text: string;
  created_at: string;
};
const trustedContext = (run: Run) => run.configuration.correction_context;
const active = new Set(["queued", "preparing_input", "analysing", "saving_result"]);
const labels: Record<string, string> = {
  queued: "Queued",
  preparing_input: "Preparing input",
  analysing: "Analysing (includes model startup)",
  saving_result: "Saving result",
  completed: "Completed",
  failed: "Failed",
};
const inputLabel = (mode: string) =>
  mode === "native_video" ? "Native video" : "Ordered sampled frames";
const elapsed = (ms: number) => `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;

export function KenAnalysis({
  assetId,
  onTitleChange,
  onMetadataChange,
}: {
  assetId: string;
  onTitleChange?: (title: string) => void;
  onMetadataChange?: () => Promise<void>;
}) {
  return (
    <AssetKenAnalysis
      key={assetId}
      assetId={assetId}
      onTitleChange={onTitleChange}
      onMetadataChange={onMetadataChange}
    />
  );
}

function AssetKenAnalysis({
  assetId,
  onTitleChange,
  onMetadataChange,
}: {
  assetId: string;
  onTitleChange?: (title: string) => void;
  onMetadataChange?: () => Promise<void>;
}) {
  const requestSequence = useRef(0);
  const lifetime = useRef(new AbortController());
  const [engine, setEngine] = useState<Engine | null>(null);
  const mode = "native_video";
  const [runs, setRuns] = useState<Run[]>([]);
  const [corrections, setCorrections] = useState<Correction[]>([]);
  const [adjustment, setAdjustment] = useState("");
  const [hidden, setHidden] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(
    async (signal?: AbortSignal) => {
      const sequence = ++requestSequence.current;
      signal ??= lifetime.current.signal;
      const response = await fetch(`/api/personal-videos/ken/assets/${assetId}/runs`, {
        credentials: "include",
        cache: "no-store",
        signal,
      });
      if (!response.ok) throw new Error("KEN results could not be loaded.");
      const items = (await response.json()) as Run[];
      const correctionResponse = await fetch(
        `/api/personal-videos/ken/assets/${assetId}/corrections`,
        {
          credentials: "include",
          cache: "no-store",
          signal,
        },
      );
      if (!correctionResponse.ok) throw new Error("KEN corrections could not be loaded.");
      const history = (await correctionResponse.json()) as Correction[];
      if (signal.aborted || lifetime.current.signal.aborted || sequence !== requestSequence.current)
        return;
      try {
        if (history.some((item) => item.asset_id !== assetId))
          throw new Error("Correction identity mismatch");
        setRuns(
          validateKenRuns(assetId, items).filter(
            (run) => run.configuration.correction_version === "ken-corrections-v1",
          ),
        );
        setCorrections(history);
      } catch (cause) {
        setRuns([]);
        setCorrections([]);
        throw cause;
      }
    },
    [assetId],
  );
  useEffect(() => {
    lifetime.current = new AbortController();
    const controller = new AbortController();
    void fetch("/api/personal-videos/ken/engine", {
      credentials: "include",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 404) {
          setHidden(true);
          return;
        }
        if (!response.ok) throw new Error("KEN is unavailable.");
        const currentEngine = (await response.json()) as Engine;
        if (controller.signal.aborted) return;
        setEngine(currentEngine);
        await load(controller.signal);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted)
          setError(cause instanceof Error ? cause.message : "KEN is unavailable.");
      });
    return () => {
      controller.abort();
      lifetime.current.abort();
    };
  }, [load]);
  const running = runs.some((run) => active.has(run.status));
  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setInterval(
      () => {
        void load(controller.signal).catch(() => {
          if (!controller.signal.aborted)
            setError("Progress could not be refreshed. Refresh results to reconnect.");
        });
      },
      running ? 2000 : 10000,
    );
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [running, load]);
  const selectedRun = runs[0];
  const latestDraft = runs.find(
    (run) =>
      run.configuration.correction_version === "ken-corrections-v1" &&
      run.status === "completed" &&
      run.result?.description,
  );
  const completedVersion = latestDraft
    ? `${latestDraft.id}:${latestDraft.configuration.automatic_title?.status ?? ""}`
    : null;
  useEffect(() => {
    if (completedVersion) void onMetadataChange?.();
  }, [completedVersion, onMetadataChange]);
  const regenerate = async () => {
    if (!latestDraft || !adjustment.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/personal-videos/ken/assets/${assetId}/corrections`, {
        method: "POST",
        credentials: "include",
        signal: lifetime.current.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parent_run_id: latestDraft.id, text: adjustment }),
      });
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "KEN could not regenerate the draft.");
      }
      await load();
    } catch (cause) {
      if (!lifetime.current.signal.aborted)
        setError(cause instanceof Error ? cause.message : "Adjustment failed.");
    } finally {
      setBusy(false);
    }
  };
  const queue = async () => {
    setBusy(true);
    setError(null);
    try {
      const endpoint =
        `/api/personal-videos/ken/assets/${assetId}/runs` +
        (selectedRun?.status === "failed" ? `/${selectedRun.id}/retry` : "");
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        signal: lifetime.current.signal,
        body: JSON.stringify({}),
      });
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Analysis could not be queued.");
      }
      const accepted = validateKenRuns(assetId, [(await response.json()) as Run])[0];
      if (lifetime.current.signal.aborted) return;
      setRuns((previous) => [accepted, ...previous.filter((run) => run.id !== accepted.id)]);
      await load();
    } catch (cause) {
      if (!lifetime.current.signal.aborted)
        setError(cause instanceof Error ? cause.message : "Analysis could not be queued.");
    } finally {
      setBusy(false);
    }
  };
  const refresh = async () => {
    try {
      const response = await fetch("/api/personal-videos/ken/engine", { credentials: "include" });
      if (!response.ok) throw new Error("KEN could not be refreshed.");
      const currentEngine = (await response.json()) as Engine;
      setEngine(currentEngine);
      await load();
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "KEN results could not be loaded.");
    }
  };
  if (hidden) return null;
  return (
    <section className="pv-panel p-5 space-y-4" aria-label="KEN">
      <div>
        <h3 className="text-sm font-semibold">KEN</h3>
        <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
          Engine: Qwen3-VL 8B
        </p>
      </div>
      {engine === null && !error && <ActionProgress state="running" label="Loading KEN" />}
      {error && (
        <p role="alert" className="text-xs text-red-300">
          {error}
        </p>
      )}
      {engine && (
        <div className="flex flex-wrap items-end gap-3 text-xs">
          <p aria-label="Active KEN engine" className="py-2">
            Engine: {engine?.display_name ?? "Qwen3-VL 8B"} · {engine?.status ?? "loading"}
          </p>
          <button
            type="button"
            className="pv-btn-primary px-3 py-2 disabled:opacity-40"
            onClick={() => void queue()}
            disabled={
              busy ||
              !engine ||
              !engine.supported_input_modes.includes(mode) ||
              !["available", "busy"].includes(engine.status) ||
              (selectedRun?.status === "failed" && !selectedRun.failure?.retry_allowed) ||
              runs.some((run) => run.input_mode === mode && active.has(run.status))
            }
          >
            {busy
              ? "Queuing…"
              : selectedRun?.status === "failed"
                ? "Retry analysis once"
                : "Analyse"}
          </button>
          <button type="button" className="underline py-2" onClick={() => void refresh()}>
            Refresh results and availability
          </button>
        </div>
      )}
      {selectedRun && (
        <div aria-label="KEN analysis status" className="space-y-1">
          <ActionProgress
            state={
              selectedRun.status === "queued"
                ? "queued"
                : active.has(selectedRun.status)
                  ? "running"
                  : selectedRun.status === "completed"
                    ? "completed"
                    : "failed"
            }
            label={`${engine?.display_name ?? "KEN"}: ${labels[selectedRun.status] ?? selectedRun.status}`}
            detail={
              active.has(selectedRun.status)
                ? "Progress refreshes automatically. A completion percentage is not available."
                : undefined
            }
          />
          {selectedRun.error && (
            <p role="alert" className="text-xs text-red-300">
              {selectedRun.failure?.message ?? selectedRun.error}
            </p>
          )}
          {selectedRun.failure && (
            <p className="text-xs">
              {selectedRun.failure.failed_at
                ? `Failed ${new Date(selectedRun.failure.failed_at).toLocaleString()}. `
                : ""}
              {selectedRun.failure.retry_allowed
                ? "One controlled retry is available; the prior failure will be kept."
                : "No further retry is available here. Technical review is required."}
            </p>
          )}
        </div>
      )}
      {engine && runs.length === 0 && (
        <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Not analysed. Press Analyse to create a description and title.
        </p>
      )}
      <div className="space-y-4">
        {runs
          .filter(
            (run, index) => index === 0 || run.id === latestDraft?.id || run.status === "failed",
          )
          .map((run) => (
            <article key={run.id} className="rounded border border-zinc-800 p-3 space-y-2">
              <div className="text-xs font-medium">
                {run.configuration.display_name} · {new Date(run.created_at).toLocaleString()}
              </div>
              <ActionProgress
                state={
                  run.status === "queued"
                    ? "queued"
                    : active.has(run.status)
                      ? "running"
                      : run.status === "completed"
                        ? "completed"
                        : "failed"
                }
                label={labels[run.status] ?? run.status}
              />
              {run.error && (
                <p className="text-xs text-red-300">{run.failure?.message ?? run.error}</p>
              )}
              {run.result?.description && (
                <p className="text-xs">
                  {run.configuration.draft_state === "user_adjusted"
                    ? "User-adjusted KEN draft"
                    : "Generated draft"}
                </p>
              )}
              {run.result?.description && (
                <details className="text-xs">
                  <summary>Generated version</summary>
                  <p className="text-sm whitespace-pre-wrap">{run.result.description}</p>
                </details>
              )}
              <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                {inputLabel(run.input_mode)}
                {run.processing_ms !== null
                  ? ` · Processing time: ${elapsed(run.processing_ms)}`
                  : ""}
              </p>
              {run.result?.warnings?.map((warning, index) => (
                <p className="text-xs" key={index} style={{ color: "var(--pv-gold)" }}>
                  {warning}
                </p>
              ))}
              {(!!trustedContext(run)?.trusted_people?.length ||
                !!trustedContext(run)?.trusted_location) && (
                <details className="text-xs">
                  <summary>Trusted context supplied to KEN</summary>
                  <ul>
                    {trustedContext(run)?.trusted_people?.map((p) => (
                      <li key={p.person_id}>People: {p.name}</li>
                    ))}
                  </ul>
                  {trustedContext(run)?.trusted_location && (
                    <p>Location: {trustedContext(run)?.trusted_location?.name}</p>
                  )}
                  <p>
                    Accepted associations identify people in this video, not who performs each
                    action. Explicit identity corrections remain authoritative.
                  </p>
                </details>
              )}
              <details className="text-xs">
                <summary className="cursor-pointer">Analysis details</summary>
                <pre className="whitespace-pre-wrap break-all mt-2 max-h-72 overflow-auto">
                  {JSON.stringify(
                    {
                      run_id: run.id,
                      model_revision: run.configuration.model_revision,
                      runtime: run.configuration.runtime,
                      input_fingerprint: run.configuration.input_integrity?.input_fingerprint,
                    },
                    null,
                    2,
                  )}
                </pre>
              </details>
            </article>
          ))}
      </div>
      {latestDraft && (
        <div className="space-y-2">
          <label htmlFor={`ken-adjust-${assetId}`} className="text-sm font-semibold">
            Adjust with KEN
          </label>
          <textarea
            id={`ken-adjust-${assetId}`}
            className="block w-full rounded border border-zinc-700 bg-zinc-900 p-2 text-sm"
            rows={3}
            maxLength={2000}
            value={adjustment}
            onChange={(event) => setAdjustment(event.target.value)}
            placeholder="Tell KEN what is wrong or what should change."
          />
          <button
            type="button"
            className="pv-btn-primary px-3 py-2 disabled:opacity-40"
            disabled={busy || running || !adjustment.trim()}
            onClick={() => void regenerate()}
          >
            Regenerate
          </button>
        </div>
      )}
      {latestDraft && (
        <KenTitles
          key={completedVersion}
          assetId={assetId}
          runId={latestDraft.id}
          disabled={busy || running}
          onTitleChange={onTitleChange}
        />
      )}
      {corrections.length > 0 && (
        <details className="text-xs">
          <summary>Corrections ({corrections.length})</summary>
          <ol className="space-y-2 mt-2">
            {corrections.map((item) => (
              <li key={item.id}>
                <span>{item.sequence}. </span>
                {item.text}
              </li>
            ))}
          </ol>
        </details>
      )}
    </section>
  );
}
