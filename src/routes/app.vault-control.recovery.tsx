import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

export const Route = createFileRoute("/app/vault-control/recovery")({
  component: CatalogueRecoveryPage,
});

type Candidate = {
  sidecar_name: string;
  status: string;
  detail: string;
  asset_id: string | null;
  display_title: string | null;
  vault_path: string | null;
  filename: string | null;
};

type Assessment = {
  current: number;
  hidden: number;
  recoverable: number;
  intentionally_deleted: number;
  media_missing: number;
  conflicting: number;
  path_conflicts: number;
  invalid: number;
  unsupported: number;
  candidates: Candidate[];
};

function CatalogueRecoveryPage() {
  const navigate = useNavigate();
  const [assessment, setAssessment] = useState<Assessment | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setMessage(null);
    try {
      const response = await fetch("/api/vault-master/sidecars/recovery/assessment", {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401) return navigate({ to: "/login" });
      if (!response.ok) throw new Error();
      const result = (await response.json()) as Assessment;
      setAssessment(result);
      setSelected(
        (current) =>
          new Set(
            [...current].filter((id) =>
              result.candidates.some(
                (candidate) => candidate.status === "recoverable" && candidate.asset_id === id,
              ),
            ),
          ),
      );
    } catch {
      setMessage("Catalogue recovery assessment is currently unavailable.");
    } finally {
      setLoading(false);
    }
  }, [navigate]);

  useEffect(() => {
    void load();
  }, [load]);

  const restore = async () => {
    setRestoring(true);
    const results: string[] = [];
    for (const assetId of selected) {
      try {
        const response = await fetch(`/api/vault-master/sidecars/recovery/${assetId}/restore`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: true }),
        });
        if (!response.ok)
          throw new Error((await response.json().catch(() => ({}))).detail ?? "refused");
        results.push("restored");
      } catch (error) {
        results.push(error instanceof Error ? error.message : "refused");
      }
    }
    setMessage(
      results.every((result) => result === "restored")
        ? "Selected catalogue records were restored."
        : results.join(" · "),
    );
    setConfirming(false);
    setSelected(new Set());
    setRestoring(false);
    await load();
  };

  const recoverable =
    assessment?.candidates.filter((candidate) => candidate.status === "recoverable") ?? [];
  const attention =
    assessment?.candidates.filter((candidate) =>
      ["conflict", "path_conflict", "invalid", "unsupported"].includes(candidate.status),
    ) ?? [];

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h2 className="pv-display-title text-3xl md:text-4xl">Catalogue Recovery</h2>
          <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Administrative disaster repair for verified canonical files. Recovery never recreates or
            moves media.
          </p>
        </div>
        <button className="pv-btn-secondary" disabled={loading} onClick={() => void load()}>
          <RefreshCw size={16} /> Refresh
        </button>
      </div>

      {assessment && (
        <section className="pv-panel space-y-4 p-5">
          <div
            className="flex flex-wrap gap-x-3 gap-y-1 text-xs"
            style={{ color: "var(--pv-text-dim)" }}
          >
            <span>{assessment.current} current</span>
            <span>·</span>
            <span>{assessment.hidden} hidden</span>
            <span>·</span>
            <span>{assessment.recoverable} recoverable</span>
            <span>·</span>
            <span>{assessment.media_missing} media missing</span>
            <span>·</span>
            <span>{assessment.conflicting + assessment.path_conflicts} conflicts</span>
            <span>·</span>
            <span>{assessment.invalid + assessment.unsupported} unreadable</span>
            {assessment.intentionally_deleted > 0 && (
              <>
                <span>·</span>
                <span>{assessment.intentionally_deleted} intentionally deleted</span>
              </>
            )}
          </div>

          {recoverable.length === 0 ? (
            <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
              No verified catalogue records are available to restore.
            </p>
          ) : (
            recoverable.map((candidate) => (
              <label
                key={candidate.sidecar_name}
                className="flex cursor-pointer items-start gap-3 rounded-md p-3"
                style={{ border: "1px solid var(--pv-border)" }}
              >
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={Boolean(candidate.asset_id && selected.has(candidate.asset_id))}
                  onChange={(event) => {
                    if (!candidate.asset_id) return;
                    setSelected((current) => {
                      const next = new Set(current);
                      if (event.target.checked) next.add(candidate.asset_id!);
                      else next.delete(candidate.asset_id!);
                      return next;
                    });
                    setConfirming(false);
                  }}
                />
                <span>
                  <span className="block text-sm" style={{ color: "var(--pv-silver)" }}>
                    {candidate.display_title ?? candidate.filename ?? candidate.sidecar_name}
                  </span>
                  <span className="mt-1 block text-xs" style={{ color: "var(--pv-text-dim)" }}>
                    {candidate.vault_path} · {candidate.detail}
                  </span>
                </span>
              </label>
            ))
          )}

          {attention.length > 0 && (
            <details>
              <summary className="cursor-pointer text-xs" style={{ color: "var(--pv-gold)" }}>
                Review {attention.length} item(s) requiring attention
              </summary>
              <div className="mt-2 space-y-2">
                {attention.map((candidate) => (
                  <div
                    key={candidate.sidecar_name}
                    className="rounded-md p-3 text-xs"
                    style={{ border: "1px solid var(--pv-border)", color: "var(--pv-text-dim)" }}
                  >
                    <strong style={{ color: "var(--pv-silver)" }}>
                      {candidate.display_title ?? candidate.sidecar_name}
                    </strong>
                    <span className="ml-2 uppercase">{candidate.status.replace("_", " ")}</span>
                    <p className="mt-1">{candidate.detail}</p>
                  </div>
                ))}
              </div>
            </details>
          )}

          {selected.size > 0 && !confirming && (
            <button
              className="pv-btn-primary px-4 py-2 text-xs"
              onClick={() => setConfirming(true)}
            >
              Review restore ({selected.size})
            </button>
          )}
          {confirming && (
            <div
              className="rounded-md p-3 text-xs"
              style={{ border: "1px solid var(--pv-gold)", color: "var(--pv-text-dim)" }}
            >
              <p>
                Restore {selected.size} verified catalogue record(s)? Each canonical file will be
                verified by size and SHA-256.
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  className="pv-btn-secondary px-3 py-2"
                  disabled={restoring}
                  onClick={() => setConfirming(false)}
                >
                  Cancel
                </button>
                <button
                  className="pv-btn-primary px-3 py-2"
                  disabled={restoring}
                  onClick={() => void restore()}
                >
                  {restoring ? "Restoring…" : "Confirm restore"}
                </button>
              </div>
            </div>
          )}
        </section>
      )}
      {message && (
        <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          {message}
        </p>
      )}
    </div>
  );
}
