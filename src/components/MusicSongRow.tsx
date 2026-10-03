import { useState } from "react";
import { Music2, Play, Pause } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "./ui/dialog";
import type { LibraryTrack } from "../lib/music-album-order";

export function MusicSongRow({
  track,
  play,
  identify,
  active,
  playing,
  onSaved,
}: {
  track: LibraryTrack;
  play: () => void;
  identify: () => void;
  active: boolean;
  playing: boolean;
  onSaved?: () => Promise<void>;
}) {
  const [failedArtwork, setFailedArtwork] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [artist, setArtist] = useState("");
  const [year, setYear] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = track.title?.trim() || "Untitled song";
  const visibleArtist = track.artist && track.artist !== "Unknown artist" ? track.artist : "";
  const duration = track.duration_seconds;
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    let saved = false;
    try {
      const response = await fetch(
        `/api/vault-master/assets/${encodeURIComponent(track.asset_id)}/metadata`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            display_title: title.trim() || null,
            artist: artist.trim() || null,
            release_year: year ? Number(year) : null,
          }),
        },
      );
      if (!response.ok) throw new Error("Correction failed");
      saved = true;
      await onSaved?.();
      setEditing(false);
    } catch {
      setError(
        saved
          ? "Your correction was saved, but Music could not be refreshed. Reopen Music to see it."
          : "Song information could not be saved. Your changes are still here.",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <article
      className="flex min-w-0 flex-wrap items-center gap-x-3 px-3 py-2"
      aria-current={active ? "true" : undefined}
    >
      <button
        type="button"
        aria-label={`${active && playing ? "Pause" : "Play"} ${label}`}
        onClick={play}
        className="flex min-h-14 min-w-0 flex-1 items-center gap-3 rounded-md text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--pv-gold)]"
      >
        <span className="relative flex h-12 w-12 shrink-0 items-center justify-center overflow-hidden rounded bg-white/5">
          {track.artwork_url && failedArtwork !== track.artwork_url ? (
            <img
              src={track.artwork_url}
              alt=""
              className="h-full w-full object-cover"
              onError={() => setFailedArtwork(track.artwork_url)}
            />
          ) : (
            <Music2 aria-label="No song artwork" />
          )}
          {active && playing ? (
            <Pause
              size={14}
              aria-hidden="true"
              className="absolute bottom-0 right-0 rounded bg-black/75"
            />
          ) : (
            <Play
              size={14}
              aria-hidden="true"
              className="absolute bottom-0 right-0 rounded bg-black/75"
            />
          )}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-[var(--pv-silver)]">{label}</span>
          {visibleArtist && (
            <span className="block truncate text-xs text-[var(--pv-text-dim)]">
              {visibleArtist}
            </span>
          )}
          {track.release_year != null && (
            <span className="block text-xs text-[var(--pv-text-dim)]">{track.release_year}</span>
          )}
          {active && (
            <span className="block text-xs text-[var(--pv-gold)]">
              {playing ? "Playing" : "Paused"}
            </span>
          )}
        </span>
        {duration != null && Number.isFinite(duration) && duration >= 0 && (
          <span className="shrink-0 text-xs tabular-nums text-[var(--pv-text-dim)]">
            {Math.floor(duration / 60)}:{String(Math.floor(duration % 60)).padStart(2, "0")}
          </span>
        )}
      </button>
      {track.can_edit === true && onSaved && (
        <Dialog
          open={editing}
          onOpenChange={(open) => {
            setEditing(open);
            if (open) {
              setTitle(track.title ?? "");
              setArtist(visibleArtist);
              setYear(track.release_year?.toString() ?? "");
              setError(null);
            }
          }}
        >
          <DialogTrigger asChild>
            <button
              type="button"
              className="pv-btn-ghost min-h-11 text-xs"
              aria-label={`Edit metadata for ${label}`}
            >
              Edit metadata
            </button>
          </DialogTrigger>
          <DialogContent className="pv-dialog max-h-[90dvh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Edit song metadata</DialogTitle>
              <DialogDescription>
                Correct the title, artist or release year. Leaving a field blank removes its manual
                correction. Files and album membership stay the same.
              </DialogDescription>
            </DialogHeader>
            <form onSubmit={(event) => void save(event)} className="space-y-4">
              <label className="block">
                Title
                <input
                  className="pv-input mt-2 w-full"
                  maxLength={240}
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                />
              </label>
              <label className="block">
                Artist
                <input
                  className="pv-input mt-2 w-full"
                  maxLength={240}
                  value={artist}
                  onChange={(e) => setArtist(e.target.value)}
                />
              </label>
              <label className="block">
                Release year
                <input
                  type="number"
                  min={1000}
                  max={9999}
                  step={1}
                  className="pv-input mt-2 w-full"
                  value={year}
                  onChange={(e) => setYear(e.target.value)}
                />
              </label>
              {error && <p role="alert">{error}</p>}
              <div className="flex flex-wrap gap-2">
                <button type="submit" className="pv-btn-primary min-h-11" disabled={busy}>
                  {busy ? "Saving..." : "Save correction"}
                </button>
                <button
                  type="button"
                  className="pv-btn-ghost min-h-11"
                  onClick={() => setEditing(false)}
                >
                  Cancel
                </button>
              </div>
            </form>
          </DialogContent>
        </Dialog>
      )}
      {track.enrichment_status === "needs_review" && (
        <button type="button" onClick={identify} className="pv-btn-ghost min-h-11 text-xs">
          Identify album
        </button>
      )}
    </article>
  );
}
