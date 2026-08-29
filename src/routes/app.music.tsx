import { createFileRoute, useNavigate } from "@tanstack/react-router";
import {
  ChevronDown,
  Disc3,
  FileText,
  Music2,
  Pause,
  Play,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { MUSIC_INTER_TRACK_DELAY_MS, nextAlbumTrack } from "../lib/music-playback-queue";

export const Route = createFileRoute("/app/music")({ component: MusicPage });

type MusicTrack = {
  id: string;
  asset_id: string;
  title: string;
  artist: string;
  album: string;
  album_artist: string | null;
  album_folder: string;
  genre: string | null;
  genres: string[];
  track_number: number | null;
  disc_number: number | null;
  release_year: number | null;
  overview: string | null;
  duration_seconds: number | null;
  artwork_url: string | null;
  lyrics_available: boolean;
  enrichment_status: "identified" | "needs_review";
  playback_url: string;
};

type AlbumCandidate = {
  release_id: string;
  title: string;
  artist: string;
  date: string | null;
  country: string | null;
  track_count: number;
  score: number;
  cover_art_available: boolean;
};

type AlbumPreview = {
  folder: string;
  release_id: string;
  title: string;
  artist: string;
  date: string | null;
  country: string | null;
  genres: string[];
  cover_art_available: boolean;
  local_track_count: number;
  matched_track_count: number;
  unmatched_local_files: string[];
  tracks: Array<{
    asset_id: string | null;
    filename: string | null;
    disc_number: number;
    track_number: number;
    title: string;
    artist: string;
    matched: boolean;
  }>;
};

function formatDuration(seconds: number | null) {
  if (seconds === null) return "";
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function MusicPage() {
  const navigate = useNavigate();
  const audio = useRef<HTMLAudioElement>(null);
  const playbackRequest = useRef<AbortController | null>(null);
  const playbackObjectUrl = useRef<string | null>(null);
  const nextTrackTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const albumQueue = useRef<MusicTrack[]>([]);
  const activeTrackId = useRef<string | null>(null);
  const [tracks, setTracks] = useState<MusicTrack[] | null>(null);
  const [active, setActive] = useState<MusicTrack | null>(null);
  const [playing, setPlaying] = useState(false);
  const [playbackUrl, setPlaybackUrl] = useState<string | null>(null);
  const [playbackLoading, setPlaybackLoading] = useState(false);
  const [playbackPosition, setPlaybackPosition] = useState(0);
  const [playbackDuration, setPlaybackDuration] = useState(0);
  const [waitingForNext, setWaitingForNext] = useState(false);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lyrics, setLyrics] = useState<{ trackId: string; text: string } | null>(null);
  const [reviewFolder, setReviewFolder] = useState<string | null>(null);
  const [identityArtist, setIdentityArtist] = useState("");
  const [identityAlbum, setIdentityAlbum] = useState("");
  const [candidates, setCandidates] = useState<AlbumCandidate[]>([]);
  const [albumPreview, setAlbumPreview] = useState<AlbumPreview | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [expandedAlbums, setExpandedAlbums] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/music", {
      credentials: "include",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 401) {
          await navigate({ to: "/login" });
          return null;
        }
        if (!response.ok) throw new Error("Music request failed");
        return (await response.json()) as MusicTrack[];
      })
      .then((items) => items && setTracks(items))
      .catch((requestError) => {
        if (!(requestError instanceof DOMException && requestError.name === "AbortError")) {
          setError("Music is currently unavailable.");
        }
      });
    return () => controller.abort();
  }, [navigate]);

  useEffect(
    () => () => {
      playbackRequest.current?.abort();
      if (nextTrackTimer.current) clearTimeout(nextTrackTimer.current);
      if (playbackObjectUrl.current) URL.revokeObjectURL(playbackObjectUrl.current);
    },
    [],
  );

  useEffect(() => {
    if (!playbackUrl || !audio.current) return;
    const player = audio.current;
    const startPlayback = () => {
      void player.play().catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setPlaying(false);
        setError("This track could not be started by the browser.");
      });
    };
    if (player.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) startPlayback();
    else player.addEventListener("canplay", startPlayback, { once: true });
    return () => player.removeEventListener("canplay", startPlayback);
  }, [playbackUrl]);

  const albums = useMemo(() => {
    const grouped = new Map<string, MusicTrack[]>();
    const normalisedQuery = query.trim().toLowerCase();
    for (const track of tracks ?? []) {
      const key =
        track.enrichment_status === "identified"
          ? `${track.album_artist ?? track.artist}\u0000${track.album}`
          : `folder\u0000${track.album_folder}`;
      grouped.set(key, [...(grouped.get(key) ?? []), track]);
    }
    return [...grouped.entries()]
      .filter(
        ([, albumTracks]) =>
          !normalisedQuery ||
          albumTracks.some((track) =>
            `${track.title} ${track.artist} ${track.album} ${track.album_artist ?? ""} ${track.genre ?? ""}`
              .toLowerCase()
              .includes(normalisedQuery),
          ),
      )
      .map(([key, albumTracks]) => ({ key, tracks: albumTracks }));
  }, [query, tracks]);

  const toggleAlbum = (key: string) => {
    setExpandedAlbums((current) => {
      const updated = new Set(current);
      if (updated.has(key)) updated.delete(key);
      else updated.add(key);
      return updated;
    });
  };

  const loadTrack = async (track: MusicTrack) => {
    if (nextTrackTimer.current) {
      clearTimeout(nextTrackTimer.current);
      nextTrackTimer.current = null;
    }
    audio.current?.pause();
    playbackRequest.current?.abort();
    if (playbackObjectUrl.current) {
      URL.revokeObjectURL(playbackObjectUrl.current);
      playbackObjectUrl.current = null;
    }

    const controller = new AbortController();
    playbackRequest.current = controller;
    activeTrackId.current = track.id;
    setActive(track);
    setPlaying(false);
    setPlaybackUrl(null);
    setPlaybackLoading(true);
    setPlaybackPosition(0);
    setPlaybackDuration(track.duration_seconds ?? 0);
    setWaitingForNext(false);
    setError(null);

    try {
      const response = await fetch(track.playback_url, {
        credentials: "include",
        headers: { Accept: "audio/mpeg" },
        signal: controller.signal,
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) throw new Error("Music playback request failed");

      const objectUrl = URL.createObjectURL(await response.blob());
      if (controller.signal.aborted) {
        URL.revokeObjectURL(objectUrl);
        return;
      }
      playbackObjectUrl.current = objectUrl;
      setPlaybackUrl(objectUrl);
    } catch (requestError) {
      if (!(requestError instanceof DOMException && requestError.name === "AbortError")) {
        setError("This track could not be played through the playback service.");
      }
    } finally {
      if (playbackRequest.current === controller) {
        playbackRequest.current = null;
        setPlaybackLoading(false);
      }
    }
  };

  const play = async (track: MusicTrack, albumTracks?: MusicTrack[]) => {
    setError(null);
    if (active?.id !== track.id) {
      albumQueue.current = albumTracks ?? [track];
      await loadTrack(track);
      return;
    }
    if (!playbackUrl) {
      if (!playbackLoading) await loadTrack(track);
      return;
    }
    if (audio.current?.paused) await audio.current.play();
    else audio.current?.pause();
  };

  const playNextTrack = () => {
    if (!active || activeTrackId.current !== active.id) return;
    if (nextTrackTimer.current) return;
    const nextTrack = nextAlbumTrack(albumQueue.current, active.id);
    setPlaying(false);
    setPlaybackPosition(playbackDuration);
    if (!nextTrack) {
      setWaitingForNext(false);
      return;
    }
    setWaitingForNext(true);
    nextTrackTimer.current = setTimeout(() => {
      nextTrackTimer.current = null;
      if (activeTrackId.current !== active.id) return;
      void loadTrack(nextTrack);
    }, MUSIC_INTER_TRACK_DELAY_MS);
  };

  const seek = (seconds: number) => {
    if (!audio.current) return;
    audio.current.currentTime = seconds;
    setPlaybackPosition(seconds);
  };

  const refreshMetadata = async () => {
    setRefreshing(true);
    setError(null);
    try {
      const response = await fetch("/api/music/refresh", {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) throw new Error("Music refresh failed");
      const updated = await fetch("/api/music", {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!updated.ok) throw new Error("Music reload failed");
      setTracks((await updated.json()) as MusicTrack[]);
    } catch {
      setError("Music information could not be refreshed from the playback service.");
    } finally {
      setRefreshing(false);
    }
  };

  const showLyrics = async (track: MusicTrack) => {
    setError(null);
    try {
      const response = await fetch(`/api/music/${track.id}/lyrics`, {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Lyrics request failed");
      const result = (await response.json()) as { text: string };
      setLyrics({ trackId: track.id, text: result.text });
    } catch {
      setError("Lyrics are not available for this track.");
    }
  };

  const startAlbumReview = (track: MusicTrack) => {
    setReviewFolder(track.album_folder);
    setIdentityArtist(track.artist.toLowerCase().startsWith("unknown") ? "" : track.artist);
    setIdentityAlbum(
      track.album.toLowerCase().startsWith("unknown")
        ? (track.album_folder.split("/").at(-1)?.split(" - ").slice(1).join(" - ") ?? "")
        : track.album,
    );
    setCandidates([]);
    setAlbumPreview(null);
    setReviewError(null);
  };

  const closeAlbumReview = () => {
    setReviewFolder(null);
    setCandidates([]);
    setAlbumPreview(null);
    setReviewError(null);
  };

  const searchAlbum = async () => {
    if (!reviewFolder || !identityArtist.trim() || !identityAlbum.trim()) return;
    setReviewBusy(true);
    setReviewError(null);
    setAlbumPreview(null);
    try {
      const response = await fetch("/api/vault-master/music/albums/search", {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          folder: reviewFolder,
          artist: identityArtist.trim(),
          album: identityAlbum.trim(),
        }),
      });
      if (!response.ok) throw new Error("Album search failed");
      const result = (await response.json()) as { candidates: AlbumCandidate[] };
      setCandidates(result.candidates);
      if (!result.candidates.length) {
        setReviewError("No matching releases were found. Check the artist and album names.");
      }
    } catch {
      setReviewError("The online music catalogue could not be searched.");
    } finally {
      setReviewBusy(false);
    }
  };

  const previewAlbum = async (releaseId: string) => {
    if (!reviewFolder) return;
    setReviewBusy(true);
    setReviewError(null);
    try {
      const response = await fetch("/api/vault-master/music/albums/preview", {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ folder: reviewFolder, release_id: releaseId }),
      });
      if (!response.ok) throw new Error("Album preview failed");
      setAlbumPreview((await response.json()) as AlbumPreview);
    } catch {
      setReviewError("The selected release could not be reviewed.");
    } finally {
      setReviewBusy(false);
    }
  };

  const approveAlbum = async () => {
    if (!reviewFolder || !albumPreview) return;
    setReviewBusy(true);
    setReviewError(null);
    try {
      const response = await fetch("/api/vault-master/music/albums/approve", {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ folder: reviewFolder, release_id: albumPreview.release_id }),
      });
      if (!response.ok) throw new Error("Album approval failed");
      const updated = await fetch("/api/music", {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!updated.ok) throw new Error("Music reload failed");
      setTracks((await updated.json()) as MusicTrack[]);
      closeAlbumReview();
    } catch {
      setReviewError("The reviewed album information could not be retained.");
    } finally {
      setReviewBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-7xl space-y-7 pb-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="pv-content-title text-xl">Music</h2>
          <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
            {tracks === null
              ? "Opening your music library..."
              : `${albums.length} albums · ${tracks.length} tracks`}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refreshMetadata()}
          disabled={refreshing}
          className="pv-btn-ghost flex items-center gap-2"
        >
          <RefreshCw size={15} className={refreshing ? "animate-spin" : ""} />
          {refreshing ? "Refreshing" : "Refresh music information"}
        </button>
      </div>

      {(tracks?.length ?? 0) > 0 && (
        <label className="pv-panel flex items-center gap-3 px-4 py-3">
          <Search size={16} style={{ color: "var(--pv-text-dim)" }} />
          <span className="sr-only">Search Music</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search tracks, artists, albums or genres"
            className="w-full bg-transparent text-sm outline-none"
            style={{ color: "var(--pv-silver)" }}
          />
        </label>
      )}

      {error && <div className="pv-panel p-6 text-center text-sm text-red-300">{error}</div>}
      {!error && tracks?.length === 0 && (
        <div className="pv-panel p-12 text-center">
          <Music2 className="mx-auto" size={28} style={{ color: "var(--pv-gold)" }} />
          <h3 className="mt-4 text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
            Music is ready
          </h3>
          <p className="mt-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Approved tracks will appear after Vault Master catalogues the Music library.
          </p>
        </div>
      )}

      {albums.map(({ key, tracks: albumTracks }) => {
        const first = albumTracks[0];
        const isExpanded = query.trim().length > 0 || expandedAlbums.has(key);
        const trackListId = `album-tracks-${encodeURIComponent(key).replaceAll("%", "")}`;
        return (
          <section key={key} className="pv-panel overflow-hidden">
            <div
              className={`flex items-stretch gap-2 p-3 sm:gap-4 sm:p-5 ${isExpanded ? "border-b" : ""}`}
              style={{ borderColor: "var(--pv-border)" }}
            >
              <button
                type="button"
                onClick={() => toggleAlbum(key)}
                aria-expanded={isExpanded}
                aria-controls={trackListId}
                className="flex min-w-0 flex-1 items-center gap-4 rounded-md text-left outline-none transition-colors hover:bg-white/[0.025] focus-visible:ring-2 focus-visible:ring-[var(--pv-gold)] sm:gap-5"
              >
                <span className="flex h-20 w-20 shrink-0 items-center justify-center overflow-hidden rounded-md bg-black/40 sm:h-24 sm:w-24">
                  {first.artwork_url ? (
                    <img src={first.artwork_url} alt="" className="h-full w-full object-cover" />
                  ) : (
                    <Disc3 size={30} style={{ color: "var(--pv-gold-dim)" }} />
                  )}
                </span>
                <span className="min-w-0 flex-1">
                  <span
                    className="block text-xs uppercase tracking-widest"
                    style={{ color: "var(--pv-gold)" }}
                  >
                    Album
                  </span>
                  <span
                    className="block truncate text-lg font-semibold"
                    style={{ color: "var(--pv-silver)" }}
                  >
                    {first.album}
                  </span>
                  <span className="block truncate text-sm" style={{ color: "var(--pv-text-dim)" }}>
                    {first.album_artist ?? first.artist}
                    {first.release_year ? ` · ${first.release_year}` : ""}
                  </span>
                  <span className="mt-2 block text-xs" style={{ color: "var(--pv-text-dim)" }}>
                    {albumTracks.length} {albumTracks.length === 1 ? "track" : "tracks"} ·{" "}
                    {first.enrichment_status === "identified"
                      ? "identified · retained locally"
                      : "needs metadata review"}
                  </span>
                </span>
                <ChevronDown
                  size={20}
                  aria-hidden="true"
                  className={`mr-1 shrink-0 transition-transform ${isExpanded ? "rotate-180" : ""}`}
                  style={{ color: "var(--pv-text-dim)" }}
                />
              </button>
              {first.enrichment_status === "needs_review" && (
                <button
                  type="button"
                  onClick={() => startAlbumReview(first)}
                  className="pv-btn-gold hidden self-center sm:block"
                >
                  Identify album
                </button>
              )}
            </div>
            {first.enrichment_status === "needs_review" && isExpanded && (
              <div
                className="border-b px-5 py-3 sm:hidden"
                style={{ borderColor: "var(--pv-border)" }}
              >
                <button
                  type="button"
                  onClick={() => startAlbumReview(first)}
                  className="pv-btn-gold w-full"
                >
                  Identify album
                </button>
              </div>
            )}
            <div
              id={trackListId}
              hidden={!isExpanded}
              className="divide-y"
              style={{ borderColor: "var(--pv-border)" }}
            >
              {albumTracks.map((track) => {
                const isActive = active?.id === track.id;
                const duration = playbackDuration || track.duration_seconds || 0;
                return (
                  <div
                    key={track.id}
                    className={`px-5 py-3 transition-colors hover:bg-white/[0.025] ${
                      isActive ? "ring-1 ring-inset ring-[var(--pv-gold)]" : ""
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => void play(track, albumTracks)}
                      className="flex w-full items-center gap-4 text-left"
                      aria-label={`${isActive && playing ? "Pause" : "Play"} ${track.title}`}
                    >
                      <span
                        className="flex w-7 justify-center text-xs"
                        style={{ color: isActive ? "var(--pv-gold)" : "var(--pv-text-dim)" }}
                      >
                        {isActive && playing ? (
                          <Pause size={14} />
                        ) : isActive && playbackLoading ? (
                          <RefreshCw size={13} className="animate-spin" />
                        ) : (
                          (track.track_number ?? <Play size={13} />)
                        )}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span
                          className="block truncate text-sm"
                          style={{ color: "var(--pv-silver)" }}
                        >
                          {track.title}
                        </span>
                        <span
                          className="block truncate text-xs"
                          style={{ color: "var(--pv-text-dim)" }}
                        >
                          {track.artist}
                          {track.genres.length ? ` · ${track.genres.join(", ")}` : ""}
                        </span>
                      </span>
                      <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                        {formatDuration(track.duration_seconds)}
                      </span>
                    </button>
                    {isActive && (
                      <div className="mt-3 flex flex-wrap items-center gap-3 pl-11">
                        <button
                          type="button"
                          onClick={() => void play(track)}
                          disabled={playbackLoading || waitingForNext}
                          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border transition-colors hover:bg-white/5"
                          style={{ borderColor: "var(--pv-gold)", color: "var(--pv-gold)" }}
                          aria-label={playing ? "Pause" : "Play"}
                        >
                          {playbackLoading ? (
                            <RefreshCw size={14} className="animate-spin" />
                          ) : playing ? (
                            <Pause size={14} />
                          ) : (
                            <Play size={14} />
                          )}
                        </button>
                        <span
                          className="w-10 text-right text-[11px] tabular-nums"
                          style={{ color: "var(--pv-text-dim)" }}
                        >
                          {formatDuration(playbackPosition)}
                        </span>
                        <input
                          type="range"
                          min={0}
                          max={Math.max(duration, 1)}
                          step={0.1}
                          value={Math.min(playbackPosition, Math.max(duration, 1))}
                          onChange={(event) => seek(Number(event.target.value))}
                          disabled={!playbackUrl}
                          aria-label={`Playback position for ${track.title}`}
                          className="h-1 min-w-0 flex-1 cursor-pointer accent-[var(--pv-gold)]"
                        />
                        <span
                          className="w-10 text-[11px] tabular-nums"
                          style={{ color: "var(--pv-text-dim)" }}
                        >
                          {formatDuration(duration)}
                        </span>
                        {track.lyrics_available && (
                          <button
                            type="button"
                            onClick={() => void showLyrics(track)}
                            className="pv-btn-ghost flex shrink-0 items-center gap-2 !px-2 !py-1.5"
                          >
                            <FileText size={13} /> <span className="hidden sm:inline">Lyrics</span>
                          </button>
                        )}
                        {waitingForNext && (
                          <span
                            className="shrink-0 text-[11px]"
                            style={{ color: "var(--pv-gold)" }}
                          >
                            Next track in 2 seconds…
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        );
      })}

      {active && (
        <audio
          ref={audio}
          key={`${active.id}:${playbackUrl ?? "loading"}`}
          src={playbackUrl ?? undefined}
          autoPlay={false}
          onLoadedMetadata={(event) => setPlaybackDuration(event.currentTarget.duration)}
          onTimeUpdate={(event) => setPlaybackPosition(event.currentTarget.currentTime)}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={playNextTrack}
          onError={() => {
            setPlaying(false);
            setError("This track could not be played through the playback service.");
          }}
        />
      )}

      {reviewFolder && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4">
          <section
            className="max-h-[90vh] w-full max-w-3xl overflow-auto rounded-lg border p-6 shadow-2xl"
            style={{ background: "var(--pv-bg-elev)", borderColor: "var(--pv-border)" }}
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <p
                  className="text-xs uppercase tracking-widest"
                  style={{ color: "var(--pv-gold)" }}
                >
                  Vault Master album review
                </p>
                <h3 className="mt-2 text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
                  Identify {reviewFolder}
                </h3>
                <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  Search results are proposals. Nothing is published until you approve an exact
                  release and track match.
                </p>
              </div>
              <button
                type="button"
                onClick={closeAlbumReview}
                className="pv-btn-ghost !p-2"
                aria-label="Close album review"
              >
                <X size={16} />
              </button>
            </div>

            <div className="mt-6 grid gap-4 sm:grid-cols-2">
              <label className="space-y-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                Artist or band
                <input
                  value={identityArtist}
                  onChange={(event) => setIdentityArtist(event.target.value)}
                  className="pv-input w-full"
                  placeholder="Imagine Dragons"
                />
              </label>
              <label className="space-y-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                Album
                <input
                  value={identityAlbum}
                  onChange={(event) => setIdentityAlbum(event.target.value)}
                  className="pv-input w-full"
                  placeholder="Mercury – Act 1"
                />
              </label>
            </div>
            <div className="mt-4 flex justify-end">
              <button
                type="button"
                onClick={() => void searchAlbum()}
                disabled={reviewBusy || !identityArtist.trim() || !identityAlbum.trim()}
                className="pv-btn-gold flex items-center gap-2"
              >
                <Search size={15} /> {reviewBusy ? "Searching" : "Find releases"}
              </button>
            </div>

            {reviewError && <p className="mt-4 text-sm text-red-300">{reviewError}</p>}

            {!albumPreview && candidates.length > 0 && (
              <div className="mt-6 space-y-3">
                <h4 className="text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
                  Possible releases
                </h4>
                {candidates.map((candidate) => (
                  <div
                    key={candidate.release_id}
                    className="flex items-center justify-between gap-4 rounded-md border p-4"
                    style={{ borderColor: "var(--pv-border)" }}
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm" style={{ color: "var(--pv-silver)" }}>
                        {candidate.title}
                      </p>
                      <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                        {candidate.artist}
                        {candidate.date ? ` · ${candidate.date}` : ""}
                        {candidate.country ? ` · ${candidate.country}` : ""} ·{" "}
                        {candidate.track_count} tracks
                        {candidate.cover_art_available ? " · cover available" : ""}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => void previewAlbum(candidate.release_id)}
                      disabled={reviewBusy}
                      className="pv-btn-ghost shrink-0"
                    >
                      Review match
                    </button>
                  </div>
                ))}
              </div>
            )}

            {albumPreview && (
              <div className="mt-6 space-y-4">
                <div>
                  <h4 className="text-base font-semibold" style={{ color: "var(--pv-silver)" }}>
                    {albumPreview.artist} — {albumPreview.title}
                  </h4>
                  <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                    {albumPreview.matched_track_count} of {albumPreview.local_track_count}
                    tracks matched by disc and track number
                    {albumPreview.cover_art_available ? " · front cover available" : ""}
                  </p>
                </div>
                <div
                  className="max-h-72 divide-y overflow-auto rounded-md border"
                  style={{ borderColor: "var(--pv-border)" }}
                >
                  {albumPreview.tracks.map((track) => (
                    <div
                      key={`${track.disc_number}:${track.track_number}`}
                      className="flex items-center gap-3 px-4 py-3 text-sm"
                    >
                      <span className="w-10 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                        {track.disc_number}.{track.track_number}
                      </span>
                      <span
                        className="min-w-0 flex-1 truncate"
                        style={{ color: "var(--pv-silver)" }}
                      >
                        {track.title}
                      </span>
                      <span className={track.matched ? "text-emerald-300" : "text-amber-300"}>
                        {track.matched ? track.filename : "No matching track"}
                      </span>
                    </div>
                  ))}
                </div>
                {albumPreview.unmatched_local_files.length > 0 && (
                  <div className="rounded-md border border-amber-400/30 bg-amber-400/5 p-4">
                    <p className="text-xs font-semibold text-amber-200">Files without a match</p>
                    <p className="mt-2 text-xs text-amber-100/80">
                      {albumPreview.unmatched_local_files.join(", ")}
                    </p>
                  </div>
                )}
                <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  Approval stores the selected titles, artist, album identity and available cover
                  inside Vault Master. Original audio files are never renamed or retagged.
                </p>
                <div className="flex justify-between gap-3">
                  <button
                    type="button"
                    onClick={() => setAlbumPreview(null)}
                    disabled={reviewBusy}
                    className="pv-btn-ghost"
                  >
                    Back to results
                  </button>
                  <button
                    type="button"
                    onClick={() => void approveAlbum()}
                    disabled={reviewBusy || albumPreview.matched_track_count === 0}
                    className="pv-btn-gold"
                  >
                    {reviewBusy ? "Retaining" : "Approve album information"}
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>
      )}

      {lyrics && (
        <aside
          className="fixed bottom-20 right-5 z-40 max-h-[60vh] w-[min(28rem,calc(100vw-2.5rem))] overflow-auto rounded-lg border p-5 shadow-2xl"
          style={{ background: "var(--pv-bg-elev)", borderColor: "var(--pv-border)" }}
        >
          <div className="mb-4 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
              Lyrics
            </h3>
            <button
              type="button"
              onClick={() => setLyrics(null)}
              className="pv-btn-ghost !p-2"
              aria-label="Close lyrics"
            >
              <X size={15} />
            </button>
          </div>
          <p className="whitespace-pre-line text-sm leading-7" style={{ color: "var(--pv-text)" }}>
            {lyrics.text}
          </p>
        </aside>
      )}
    </div>
  );
}
