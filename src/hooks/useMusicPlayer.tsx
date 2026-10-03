import { useEffect, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { FileText, Pause, Play } from "lucide-react";
import { MUSIC_INTER_TRACK_DELAY_MS, nextAlbumTrack } from "../lib/music-playback-queue";
import type { MusicTrack } from "../lib/music-types";

function formatDuration(seconds: number | null) {
  if (seconds === null) return "";
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

export function useMusicPlayer(showLyrics?: (track: MusicTrack) => void, customControls = false) {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const playbackRequest = useRef<AbortController | null>(null);
  const playbackObjectUrl = useRef<string | null>(null);
  const nextTrackTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const albumQueue = useRef<MusicTrack[]>([]);
  const activeTrackId = useRef<string | null>(null);
  const [active, setActive] = useState<MusicTrack | null>(null);
  const [playing, setPlaying] = useState(false);
  const [playbackUrl, setPlaybackUrl] = useState<string | null>(null);
  const [playbackLoading, setPlaybackLoading] = useState(false);
  const [playbackPosition, setPlaybackPosition] = useState(0);
  const [playbackDuration, setPlaybackDuration] = useState(0);
  const [waitingForNext, setWaitingForNext] = useState(false);
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
        headers: { Accept: "audio/flac, audio/mp4, audio/mpeg, audio/wav, audio/aac" },
        signal: controller.signal,
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const message =
          response.status === 404
            ? "This track is unavailable or you no longer have access."
            : response.status === 503
              ? "This track could not be prepared for playback. Please try again."
              : "This track could not be loaded for playback.";
        if (!controller.signal.aborted) setError(message);
        return;
      }

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

  const startAlbum = (ordered: MusicTrack[], start = ordered[0]) => {
    if (!start) return;
    albumQueue.current = ordered;
    void loadTrack(start);
  };
  return {
    active,
    playing,
    play,
    startAlbum,
    playbackPosition,
    playbackDuration,
    playbackLoading,
    waitingForNext,
    canSeek: !!playbackUrl,
    seek,
    stop: () => {
      audio.current?.pause();
      playbackRequest.current?.abort();
      if (nextTrackTimer.current) clearTimeout(nextTrackTimer.current);
      nextTrackTimer.current = null;
      albumQueue.current = [];
      activeTrackId.current = null;
      setActive(null);
      setPlaying(false);
      setPlaybackUrl(null);
      if (playbackObjectUrl.current) URL.revokeObjectURL(playbackObjectUrl.current);
      playbackObjectUrl.current = null;
    },
    controls: (
      <>
        {error && (
          <p role="alert" className="pv-panel p-4 text-red-300">
            {error}
          </p>
        )}
        {active && !customControls && (
          <section
            aria-label="Now playing"
            className="sticky bottom-3 z-30 flex flex-wrap items-center gap-3 rounded-xl border border-[var(--pv-border)] bg-[var(--pv-bg-elev)] p-4 shadow-xl"
          >
            <button
              type="button"
              onClick={() => void play(active)}
              disabled={playbackLoading || waitingForNext}
              className="pv-btn-ghost min-h-11 min-w-11"
              aria-label={playing ? "Pause" : "Play"}
            >
              {playing ? <Pause size={18} /> : <Play size={18} />}
            </button>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm">{active.title}</p>
              <p className="truncate text-xs text-[var(--pv-text-dim)]">{active.artist}</p>
            </div>
            <span className="text-xs tabular-nums">
              {formatDuration(playbackPosition)} /{" "}
              {formatDuration(playbackDuration || active.duration_seconds)}
            </span>
            <input
              type="range"
              min={0}
              max={Math.max(playbackDuration || active.duration_seconds || 0, 1)}
              step={0.1}
              value={playbackPosition}
              onChange={(event) => seek(Number(event.target.value))}
              disabled={!playbackUrl}
              aria-label="Playback position"
              className="w-full accent-[var(--pv-gold)]"
            />
            {active.lyrics_available && showLyrics && (
              <button
                type="button"
                onClick={() => void showLyrics(active)}
                className="pv-btn-ghost"
              >
                <FileText size={14} />
                Lyrics
              </button>
            )}
          </section>
        )}

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
      </>
    ),
  };
}
