import { Check } from "lucide-react";
import type { PlaybackState } from "@/lib/theatre-progress";

export function PlaybackStateIndicator({ progress }: { progress?: PlaybackState }) {
  if (!progress || progress.state === "unwatched") return null;
  if (progress.state === "watched")
    return (
      <span className="inline-flex items-center gap-1 text-xs text-emerald-400">
        <Check size={14} aria-hidden="true" />
        Watched
      </span>
    );
  const percent =
    progress.duration_seconds > 0
      ? Math.min(100, (progress.position_seconds / progress.duration_seconds) * 100)
      : 0;
  return (
    <span className="mt-1 block text-xs text-muted-foreground">
      In Progress
      <progress
        className="mt-1 block h-1 w-full accent-amber-400"
        aria-label="Playback progress"
        value={percent}
        max={100}
      />
    </span>
  );
}

export function WatchedAction({
  progress,
  pending,
  onChange,
}: {
  progress?: PlaybackState;
  pending: boolean;
  onChange: (watched: boolean) => void;
}) {
  const watched = progress?.completed ?? false;
  return (
    <button
      type="button"
      className="pv-btn-ghost text-xs"
      disabled={pending}
      onClick={() => onChange(!watched)}
    >
      {watched ? "Mark as unwatched" : "Mark as watched"}
    </button>
  );
}
