import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowRight, Disc3, ListMusic, Music2, Video } from "lucide-react";
import { useEffect, useState } from "react";
import type { MusicTrack } from "../lib/music-types";

export const Route = createFileRoute("/app/music")({ component: MusicDashboard });
type Preview = { id: string; url: string | null };
type VideoPreview = { asset_id: string; thumbnail_url?: string | null };
const PREVIEW_LIMIT = 4;

function DashboardCard({
  title,
  to,
  status,
  previews,
  icon: Icon,
}: {
  title: string;
  to: "/app/music/albums" | "/app/music/songs" | "/app/music/videos" | "/app/music/playlists";
  status: string;
  previews: Preview[];
  icon: typeof Disc3;
}) {
  return (
    <Link
      to={to}
      aria-label={`Open ${title}`}
      data-music-dashboard-card
      className="pv-panel pv-panel-hover group flex h-32 sm:h-36 w-full min-w-0 items-center gap-3 sm:gap-6 overflow-hidden px-4 sm:px-6 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--pv-gold)]"
    >
      <Icon aria-hidden="true" className="pv-card-icon shrink-0" size={26} />
      <div className="min-w-0 flex-1">
        <h2 className="truncate text-lg sm:text-xl font-semibold text-[var(--pv-silver)]">
          {title}
        </h2>
        <p className="truncate text-sm text-[var(--pv-text-dim)]">{status}</p>
      </div>
      <div aria-hidden="true" className="flex shrink-0 gap-2 overflow-hidden max-w-[45%]">
        {previews.slice(0, PREVIEW_LIMIT).map((preview, index) => (
          <span
            key={preview.id}
            data-music-preview
            className={`${index > 0 ? "hidden sm:flex" : "flex"} ${index > 1 ? "!hidden lg:!flex" : ""} h-16 w-16 sm:h-20 sm:w-20 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-[var(--pv-border)] bg-black/20`}
          >
            {preview.url ? (
              <img
                src={preview.url}
                alt=""
                loading="lazy"
                className="h-full w-full object-cover"
                onError={(event) => {
                  event.currentTarget.style.display = "none";
                }}
              />
            ) : (
              <Icon size={24} className="text-[var(--pv-gold-dim)]" />
            )}
          </span>
        ))}
      </div>
      <ArrowRight aria-hidden="true" size={20} className="shrink-0 text-[var(--pv-gold)]" />
    </Link>
  );
}

function MusicDashboard() {
  const [tracks, setTracks] = useState<MusicTrack[] | null>(null);
  const [videos, setVideos] = useState<VideoPreview[] | null>(null);
  const [musicError, setMusicError] = useState(false);
  const [videoError, setVideoError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    async function load<T>(url: string, set: (items: T[]) => void, failed: () => void) {
      try {
        const response = await fetch(url, { credentials: "include", signal: controller.signal });
        if (!response.ok) throw new Error("Library unavailable");
        const items = await response.json();
        if (!Array.isArray(items)) throw new Error("Invalid library response");
        if (!controller.signal.aborted) set(items);
      } catch {
        if (!controller.signal.aborted) failed();
      }
    }
    void load<MusicTrack>("/api/music", setTracks, () => setMusicError(true));
    void load<VideoPreview>("/api/music-videos", setVideos, () => setVideoError(true));
    return () => controller.abort();
  }, []);
  const albums = new Map<string, Preview>();
  for (const track of tracks ?? []) {
    if (
      track.album_group_id &&
      (!albums.has(track.album_group_id) || !albums.get(track.album_group_id)?.url)
    ) {
      albums.set(track.album_group_id, { id: track.album_group_id, url: track.artwork_url });
    }
  }
  const songs = (tracks ?? []).filter((track) => !track.album_group_id);
  const stable = (items: Preview[]) =>
    items.sort((a, b) => a.id.localeCompare(b.id)).slice(0, PREVIEW_LIMIT);
  const status = (count: number, noun: string, loaded: boolean, error: boolean) =>
    error ? "Currently unavailable" : loaded ? `${count} ${noun}` : "Loading…";
  return (
    <div className="pv-layout-container mx-auto w-full max-w-7xl min-w-0 space-y-5 pb-8">
      <h1 className="pv-content-title text-2xl">Music</h1>
      <nav aria-label="Music subsections" className="grid grid-cols-1 gap-4">
        <DashboardCard
          title="Albums"
          to="/app/music/albums"
          icon={Disc3}
          status={status(albums.size, "albums", tracks !== null, musicError)}
          previews={stable([...albums.values()])}
        />
        <DashboardCard
          title="Songs"
          to="/app/music/songs"
          icon={Music2}
          status={status(songs.length, "songs", tracks !== null, musicError)}
          previews={stable(songs.map((track) => ({ id: track.asset_id, url: track.artwork_url })))}
        />
        <DashboardCard
          title="Music Videos"
          to="/app/music/videos"
          icon={Video}
          status={status(videos?.length ?? 0, "videos", videos !== null, videoError)}
          previews={stable(
            (videos ?? []).map((video) => ({
              id: video.asset_id,
              url: video.thumbnail_url ?? null,
            })),
          )}
        />
        <DashboardCard
          title="Playlists"
          to="/app/music/playlists"
          icon={ListMusic}
          status="Coming later"
          previews={[]}
        />
      </nav>
    </div>
  );
}
