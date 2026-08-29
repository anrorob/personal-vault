import type Hls from "hls.js";
import { Maximize, Minimize } from "lucide-react";
import { useEffect, useRef, useState } from "react";

type MoviePlayerSubtitleTrack = {
  index: number;
  label: string;
};

export function MoviePlayer({
  source,
  onPlaybackError,
  startSeconds = 0,
  onProgress,
  subtitleTracks = [],
  selectedSubtitleIndex = null,
  onSubtitleChange,
}: {
  source: string;
  onPlaybackError: () => void;
  startSeconds?: number;
  onProgress?: (positionSeconds: number, durationSeconds: number, completed: boolean) => void;
  subtitleTracks?: MoviePlayerSubtitleTrack[];
  selectedSubtitleIndex?: number | null;
  onSubtitleChange?: (subtitleIndex: number | null) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const playerRef = useRef<HTMLDivElement>(null);
  const lastReport = useRef(0);
  const preservedPosition = useRef(startSeconds);
  const [isFullscreen, setIsFullscreen] = useState(false);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) {
      return;
    }

    let positionApplied = false;
    const applyPosition = () => {
      if (positionApplied || !Number.isFinite(video.duration) || video.duration <= 0) {
        return;
      }
      if (preservedPosition.current > 0 && preservedPosition.current < video.duration - 10) {
        video.currentTime = preservedPosition.current;
      }
      positionApplied = true;
      void video.play().catch(() => undefined);
    };
    video.addEventListener("loadedmetadata", applyPosition);

    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = source;
      return () => {
        preservedPosition.current = video.currentTime;
        video.removeEventListener("loadedmetadata", applyPosition);
        video.removeAttribute("src");
        video.load();
      };
    }

    let cancelled = false;
    let hls: Hls | null = null;

    void import("hls.js")
      .then(({ default: HlsPlayer }) => {
        if (cancelled) {
          return;
        }

        if (!HlsPlayer.isSupported()) {
          onPlaybackError();
          return;
        }

        hls = new HlsPlayer();
        hls.loadSource(source);
        hls.attachMedia(video);
        hls.on(HlsPlayer.Events.MANIFEST_PARSED, () => {
          applyPosition();
        });
        hls.on(HlsPlayer.Events.ERROR, (_event, data) => {
          if (data.fatal) {
            onPlaybackError();
          }
        });
      })
      .catch(() => {
        onPlaybackError();
      });

    return () => {
      cancelled = true;
      preservedPosition.current = video.currentTime;
      video.removeEventListener("loadedmetadata", applyPosition);
      hls?.destroy();
    };
  }, [onPlaybackError, source]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !onProgress) return;
    const report = (completed = false) => {
      if (!Number.isFinite(video.duration) || video.duration <= 0) return;
      onProgress(video.currentTime, video.duration, completed);
    };
    const handleTime = () => {
      if (video.currentTime - lastReport.current >= 10) {
        lastReport.current = video.currentTime;
        report();
      }
    };
    const handlePause = () => report();
    const handleEnded = () => report(true);
    video.addEventListener("timeupdate", handleTime);
    video.addEventListener("pause", handlePause);
    video.addEventListener("ended", handleEnded);
    return () => {
      report();
      video.removeEventListener("timeupdate", handleTime);
      video.removeEventListener("pause", handlePause);
      video.removeEventListener("ended", handleEnded);
    };
  }, [onProgress]);

  useEffect(() => {
    const handleFullscreenChange = () => {
      setIsFullscreen(document.fullscreenElement === playerRef.current);
    };
    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

  const toggleFullscreen = async () => {
    if (document.fullscreenElement === playerRef.current) {
      await document.exitFullscreen();
      return;
    }
    await playerRef.current?.requestFullscreen();
  };

  return (
    <div ref={playerRef} className="group relative h-full w-full bg-black">
      <video
        ref={videoRef}
        className="h-full w-full bg-black"
        controls
        controlsList="nofullscreen"
        autoPlay
        playsInline
        preload="metadata"
      >
        Your browser does not support video playback.
      </video>
      <div className="absolute bottom-12 right-3 flex items-center gap-2 rounded-md bg-black/80 p-2 text-sm text-white shadow-lg">
        {subtitleTracks.length > 0 && onSubtitleChange ? (
          <label className="flex items-center gap-2">
            <span>Subtitles</span>
            <select
              aria-label="Subtitles"
              className="max-w-56 rounded border border-white/30 bg-black px-2 py-1 text-white"
              value={selectedSubtitleIndex ?? "off"}
              onChange={(event) => {
                onSubtitleChange(event.target.value === "off" ? null : Number(event.target.value));
              }}
            >
              <option value="off">Off</option>
              {subtitleTracks.map((track) => (
                <option key={track.index} value={track.index}>
                  {track.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <button
          type="button"
          className="rounded p-1.5 hover:bg-white/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
          aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
          onClick={() => void toggleFullscreen().catch(() => undefined)}
        >
          {isFullscreen ? <Minimize size={18} /> : <Maximize size={18} />}
        </button>
      </div>
    </div>
  );
}
