import { useState } from "react";
import type { VaultMasterItem } from "@/lib/incoming";

const terminal = new Set(["moved", "arrival_removed", "duplicate_removed"]);

export function ArrivalMusicImports({
  items,
  onChanged,
}: {
  items: VaultMasterItem[];
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const groups = new Map<string, VaultMasterItem[]>();
  for (const item of items) {
    if (item.source_kind !== "incoming" || !item.album_group_id) continue;
    groups.set(item.album_group_id, [...(groups.get(item.album_group_id) ?? []), item]);
  }
  async function approve(id: string, members: VaultMasterItem[]) {
    if (
      !window.confirm(
        `Publish these ${members.length} received album tracks? Confirm that Supplier has finished this upload before continuing.`,
      )
    )
      return;
    setBusy(id);
    setError("");
    try {
      const response = await fetch(
        `/api/vault-master/music/imports/${encodeURIComponent(id)}/approve`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ item_ids: members.map((item) => item.id) }),
        },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(
          typeof body.detail === "string"
            ? body.detail
            : "The album could not be queued. Refresh and review its tracks.",
        );
      }
      await onChanged();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Album publication is unavailable.");
    } finally {
      setBusy(null);
    }
  }
  async function recover(id: string, members: VaultMasterItem[], action: "remove" | "retry") {
    if (
      action === "remove" &&
      !window.confirm(
        "Remove this unpublished album from Arrival Hall? This removes only staging; published or uncertain files cannot be removed.",
      )
    )
      return;
    setBusy(id);
    setError("");
    try {
      const response = await fetch("/api/vault-master/recovery", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action,
          item_ids: members.filter((item) => !terminal.has(item.state)).map((item) => item.id),
          ...(action === "remove" ? { confirmation: "REMOVE FROM ARRIVAL HALL" } : {}),
        }),
      });
      if (!response.ok)
        throw new Error("The album could not be recovered. Its files remain protected.");
      const result = (await response.json()) as {
        outcomes: { processed: boolean; message: string }[];
      };
      const failures = result.outcomes.filter((item) => !item.processed);
      if (failures.length)
        setError(`${failures.length} tracks need attention. ${failures[0].message}`);
      await onChanged();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Recovery is unavailable.");
    } finally {
      setBusy(null);
    }
  }
  return (
    <>
      {error && <p role="alert">{error}</p>}
      {[...groups]
        .filter(([, members]) => members.some((item) => !terminal.has(item.state)))
        .map(([id, members]) => {
          const context = members[0].metadata.source_context as
            | {
                source_label?: string;
                music_album?: { artist_name?: string; album_title?: string };
              }
            | undefined;
          const album = context?.music_album;
          const ready = members.every(
            (item) => item.state === "needs_review" && !item.duplicate_of_id,
          );
          return (
            <section
              key={id}
              className="pv-panel space-y-3 p-5 min-w-0"
              aria-label="Music album import"
            >
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="font-semibold break-words">
                    {album?.album_title || context?.source_label || "Album details needed"}
                  </h2>
                  {album?.artist_name && <p className="text-sm break-words">{album.artist_name}</p>}
                  <p className="text-xs">
                    {members.length} received tracks ·{" "}
                    {ready
                      ? "Awaiting album review"
                      : `${members.filter((item) => item.state === "moved").length} of ${members.length} published`}
                  </p>
                </div>
                <button
                  className="pv-btn-primary px-4 py-2"
                  disabled={!ready || busy !== null}
                  onClick={() => void approve(id, members)}
                >
                  {busy === id ? "Working…" : "Approve and publish album"}
                </button>
                <button
                  className="pv-btn-secondary px-4 py-2"
                  disabled={busy !== null}
                  onClick={() => void recover(id, members, "remove")}
                >
                  Remove unpublished staging
                </button>
                {members.some((item) =>
                  ["move_failed", "theatre_promotion_pending"].includes(item.state),
                ) && (
                  <button
                    className="pv-btn-secondary px-4 py-2"
                    disabled={busy !== null}
                    onClick={() => void recover(id, members, "retry")}
                  >
                    Retry safe publication
                  </button>
                )}
              </div>
              <p className="text-xs">
                Review the received tracks after Supplier finishes uploading. Track order uses
                recorded disc and track numbers; missing or ambiguous order remains unresolved.
              </p>
              <details>
                <summary className="cursor-pointer py-2">View {members.length} tracks</summary>
                <ul className="space-y-2">
                  {members.map((item) => (
                    <li key={item.id} className="flex flex-wrap justify-between gap-2 text-sm">
                      <span className="min-w-0 break-words">
                        {(
                          item.metadata.source_context as { original_filename?: string } | undefined
                        )?.original_filename || item.filename}
                      </span>
                      <span className="text-xs">{item.state.replaceAll("_", " ")}</span>
                    </li>
                  ))}
                </ul>
              </details>
            </section>
          );
        })}
    </>
  );
}
