import { useEffect, useRef, useState } from "react";

type Suggestion = {
  id: string;
  asset_id: string;
  run_id: string;
  title: string;
  accepted_at: string | null;
};
type State = {
  asset_id: string;
  display_title: string;
  manual_title: boolean;
  suggestions: Suggestion[];
};

export function KenTitles({
  assetId,
  runId,
  disabled,
  onTitleChange,
}: {
  assetId: string;
  runId: string;
  disabled: boolean;
  onTitleChange?: (title: string) => void;
}) {
  const lifetime = useRef(new AbortController());
  const [state, setState] = useState<State | null>(null);
  const [manual, setManual] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const base = `/api/personal-videos/ken/assets/${assetId}`;
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    void fetch(`${base}/titles`, {
      credentials: "include",
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (r) => {
        if (!r.ok) throw new Error("Title suggestions could not be loaded.");
        const value = (await r.json()) as State;
        if (controller.signal.aborted) return;
        if (value.asset_id !== assetId || value.suggestions.some((s) => s.asset_id !== assetId))
          throw new Error("Title identity mismatch");
        setState(value);
        setManual(value.display_title);
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted)
          setError(e instanceof Error ? e.message : "Title unavailable");
      });
    return () => controller.abort();
  }, [base, assetId]);
  const suggestion = state?.suggestions.find((s) => s.run_id === runId);
  const act = async (kind: "generate" | "accept" | "manual") => {
    setBusy(true);
    setError(null);
    try {
      const path =
        kind === "generate"
          ? "/titles"
          : kind === "accept"
            ? `/titles/${suggestion?.id}/accept`
            : "/title";
      const response = await fetch(base + path, {
        method: kind === "manual" ? "PATCH" : "POST",
        credentials: "include",
        signal: lifetime.current.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          kind === "generate" ? { run_id: runId } : kind === "manual" ? { title: manual } : {},
        ),
      });
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail ?? "KEN title action failed.");
      if (lifetime.current.signal.aborted) return;
      if (value.asset_id !== assetId || (kind === "generate" && value.run_id !== runId))
        throw new Error("Title identity mismatch");
      if (kind === "generate") {
        setState((s) => (s ? { ...s, suggestions: [value as Suggestion, ...s.suggestions] } : s));
      } else {
        setState((s) =>
          s ? { ...s, display_title: value.display_title, manual_title: kind === "manual" } : s,
        );
        setManual(value.display_title);
        onTitleChange?.(value.display_title);
      }
    } catch (e) {
      if (!lifetime.current.signal.aborted)
        setError(e instanceof Error ? e.message : "Title action failed.");
    } finally {
      if (!lifetime.current.signal.aborted) setBusy(false);
    }
  };
  return (
    <div className="space-y-2 border-t border-zinc-800 pt-3 text-sm">
      <h4 className="font-semibold">Title</h4>
      <p className="text-xs">
        KEN generates and applies a title automatically after analysis. You can also request another
        suggestion. Manual titles remain authoritative.
      </p>
      {error && (
        <p role="alert" className="text-xs text-red-300">
          {error}
        </p>
      )}
      {suggestion && <p>{suggestion.title}</p>}
      <div className="flex gap-3">
        <button
          type="button"
          className="pv-btn-primary px-3 py-2 disabled:opacity-40"
          disabled={busy || disabled || !state}
          onClick={() => void act("generate")}
        >
          {busy ? "Working…" : suggestion ? "Regenerate title" : "Generate title from this draft"}
        </button>
        {suggestion && (
          <button
            type="button"
            className="pv-btn-primary px-3 py-2 disabled:opacity-40"
            disabled={busy || disabled || state?.manual_title || !!suggestion.accepted_at}
            onClick={() => void act("accept")}
          >
            {suggestion.accepted_at ? "Title applied" : "Use title"}
          </button>
        )}
      </div>
      {state?.manual_title && (
        <p className="text-xs">Your manual title is protected. Edit it below to change it.</p>
      )}
      <details>
        <summary className="cursor-pointer text-xs">Edit video title</summary>
        <label className="block mt-2">
          Manual title
          <input
            className="block w-full bg-zinc-900 border border-zinc-700 rounded p-2"
            value={manual}
            maxLength={160}
            onChange={(e) => setManual(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="pv-btn-primary px-3 py-2 mt-2 disabled:opacity-40"
          disabled={busy || !manual.trim() || !state}
          onClick={() => void act("manual")}
        >
          Save manual title
        </button>
      </details>
    </div>
  );
}
