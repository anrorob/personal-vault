import { useNavigate } from "@tanstack/react-router";
import { CircleCheck, CircleX, Clock3, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { VaultMasterActivityEntry, VaultMasterActivityListing } from "@/lib/incoming";

const ACTIVITY_LABELS: Record<string, string> = {
  file_analysed: "File analysed",
  file_inventoried: "Existing Vault file catalogued",
  scan_completed: "Scan completed",
  scan_failed: "Scan failed",
  proposal_approved: "Proposal approved",
  proposal_rejected: "Proposal rejected",
  move_queued: "Approved move queued",
  file_moved: "File moved safely",
  move_failed: "Move failed",
  duplicate_kept: "Arrival Hall duplicate kept",
  duplicate_removed: "Arrival Hall duplicate removed",
  duplicate_remove_failed: "Duplicate removal failed",
};

const HIDDEN_ACTIVITY_ACTIONS = new Set(["sidecars_reconciled"]);

export function VaultMasterActivity() {
  const navigate = useNavigate();
  const [events, setEvents] = useState<VaultMasterActivityEntry[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const loadActivity = useCallback(async () => {
    setRefreshing(true);
    setFailed(false);
    try {
      const response = await fetch("/api/vault-master/activity?limit=25", {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) throw new Error("Activity history request failed");
      const listing = (await response.json()) as VaultMasterActivityListing;
      setEvents(listing.events.filter((event) => !HIDDEN_ACTIVITY_ACTIONS.has(event.action)));
    } catch {
      setFailed(true);
    } finally {
      setRefreshing(false);
    }
  }, [navigate]);

  useEffect(() => {
    void loadActivity();
    const refresh = window.setInterval(() => void loadActivity(), 5000);
    return () => window.clearInterval(refresh);
  }, [loadActivity]);

  return (
    <section className="space-y-4 pt-4" aria-labelledby="vault-activity-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <div
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
            style={{ color: "var(--pv-gold)", border: "1px solid var(--pv-border)" }}
          >
            <Clock3 className="h-4 w-4" />
          </div>
          <div>
            <p className="text-[11px] uppercase tracking-[0.22em] text-[var(--pv-gold)]">
              Vault Master activity
            </p>
            <h2 id="vault-activity-heading" className="pv-content-title mt-1 text-xl">
              Recent actions
            </h2>
            <p className="mt-1 text-xs text-[var(--pv-text-dim)]">
              A reviewable record of scans, decisions, moves, duplicate handling, and failures.
            </p>
          </div>
        </div>
        <button
          type="button"
          className="pv-btn-secondary"
          disabled={refreshing}
          onClick={() => void loadActivity()}
        >
          <RefreshCw size={14} className={refreshing ? "animate-spin" : ""} />
          Refresh activity
        </button>
      </div>

      {failed ? (
        <p className="rounded-md border border-red-400/30 px-4 py-3 text-xs text-red-300">
          Vault Master activity is currently unavailable.
        </p>
      ) : events === null ? (
        <div className="pv-panel px-5 py-6 text-center text-xs text-[var(--pv-text-dim)]">
          Loading recent activity…
        </div>
      ) : events.length === 0 ? (
        <div className="pv-panel px-5 py-6 text-center text-xs text-[var(--pv-text-dim)]">
          No Vault Master activity has been recorded yet.
        </div>
      ) : (
        <div className="pv-panel overflow-hidden">
          <div className="divide-y divide-[var(--pv-border)]">
            {events.map((event) => (
              <ActivityEvent key={event.id} event={event} />
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function ActivityEvent({ event }: { event: VaultMasterActivityEntry }) {
  const source =
    event.source_kind === "incoming"
      ? "Arrival Hall"
      : event.source_kind === "inventory"
        ? "Vault inventory"
        : null;
  const timestamp = new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(event.created_at));
  const label = ACTIVITY_LABELS[event.action] ?? event.action.replaceAll("_", " ");

  return (
    <article className="flex items-start gap-3 px-5 py-4">
      <span
        className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full"
        style={{
          color: event.succeeded ? "var(--pv-gold)" : "#fca5a5",
          border: `1px solid ${event.succeeded ? "var(--pv-border)" : "rgba(248,113,113,0.3)"}`,
        }}
      >
        {event.succeeded ? <CircleCheck size={15} /> : <CircleX size={15} />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <p className="text-sm font-medium text-[var(--pv-silver)]">
            {label}
            {event.filename && (
              <span className="font-normal text-[var(--pv-silver-dim)]"> · {event.filename}</span>
            )}
          </p>
          <time className="text-[11px] text-[var(--pv-text-dim)]">{timestamp}</time>
        </div>
        {(source || event.username) && (
          <p className="mt-1 text-xs text-[var(--pv-text-dim)]">
            {[source, event.username ? `by ${event.username}` : null].filter(Boolean).join(" · ")}
          </p>
        )}
        {event.detail && (
          <p
            className="mt-2 text-xs"
            style={{ color: event.succeeded ? "var(--pv-silver-dim)" : "#fca5a5" }}
          >
            {event.detail}
          </p>
        )}
      </div>
    </article>
  );
}
