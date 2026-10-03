import type Hls from "hls.js";
import { Maximize, Minimize } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { PlayerMenu } from "./PlayerMenu";
import { PlayerTransport } from "./PlayerTransport";
import { attachPlaybackProgress } from "@/lib/playback-progress";
import { attachPlayerControlVisibility } from "@/lib/player-control-visibility";
import { attachPlayerFullscreen } from "@/lib/player-fullscreen";
import {
  measuredDeliveryThroughput,
  originalResolutionViolation,
  probeQualityTargets,
  qualityModes,
  type QualityInfo,
  type QualityMode,
} from "@/lib/theatre-quality";
import { theatreCapabilities, type TheatreSource } from "@/lib/theatre-capabilities";
import {
  attachTheatreBufferGate,
  theatreBufferPolicy,
  type BufferView,
} from "@/lib/theatre-buffering";

type MoviePlayerSubtitleTrack = {
  index: number;
  label: string;
};

export function MoviePlayer({
  source,
  sourceType = "hls",
  onPlaybackError,
  startSeconds = 0,
  onProgress,
  subtitleTracks = [],
  selectedSubtitleIndex = null,
  onSubtitleChange,
  playbackPlanUrl,
  bufferPlayback = false,
  initialQualityMode = "Auto",
  onQualityModeChange,
}: {
  source: string;
  sourceType?: "hls" | "file";
  onPlaybackError: () => void;
  startSeconds?: number;
  onProgress?: (positionSeconds: number, durationSeconds: number, completed: boolean) => void;
  subtitleTracks?: MoviePlayerSubtitleTrack[];
  selectedSubtitleIndex?: number | null;
  onSubtitleChange?: (subtitleIndex: number | null) => void;
  playbackPlanUrl?: string;
  bufferPlayback?: boolean;
  initialQualityMode?: QualityMode;
  onQualityModeChange?: (mode: QualityMode) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [mediaElement, setMediaElement] = useState<HTMLVideoElement | null>(null);
  const bindVideo = useCallback((video: HTMLVideoElement | null) => {
    videoRef.current = video;
    setMediaElement(video);
  }, []);
  const playerRef = useRef<HTMLDivElement>(null);
  const controlStripRef = useRef<HTMLDivElement>(null);
  const [controlsVisible, setControlsVisible] = useState(true);
  const [qualityMode, setQualityMode] = useState<QualityMode>(initialQualityMode);
  const [autoRevision, setAutoRevision] = useState(0);
  const [qualityError, setQualityError] = useState<string | null>(null);
  const qualitySwitch = useRef<{ position: number; wantsPlay: boolean } | null>(null);
  const hasPlayed = useRef(false);
  const selectedAudio = useRef<number | null>(null);
  const measuredBandwidth = useRef<number | null>(null);
  const lastAutoCheck = useRef(0);
  const playbackErrorRef = useRef(onPlaybackError);
  playbackErrorRef.current = onPlaybackError;
  const preservedPosition = useRef(startSeconds);
  const initialPositionReady = useRef(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [fullscreenAvailable, setFullscreenAvailable] = useState(false);
  const [fullscreenError, setFullscreenError] = useState<string | null>(null);
  const fullscreen = useRef<ReturnType<typeof attachPlayerFullscreen> | null>(null);
  const bufferingInfo = useRef<() => Record<string, unknown>>(() => ({}));
  const [bufferView, setBufferView] = useState<BufferView>({
    phase: "preparing",
    ahead: 0,
    target: 24,
  });

  const prepareQualitySwitch = useCallback(() => {
    const video = videoRef.current;
    if (!video || !hasPlayed.current) return;
    qualitySwitch.current = {
      position: video.readyState > 0 ? video.currentTime : preservedPosition.current,
      wantsPlay: Boolean(bufferingInfo.current().wants_playback ?? !video.paused),
    };
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) {
      return;
    }
    const resume = qualitySwitch.current;
    qualitySwitch.current = null;
    setQualityError(null);

    const startPosition = Math.max(0, resume?.position ?? preservedPosition.current);
    initialPositionReady.current = false;
    let sourceReady = false;
    let nativeHls = false;
    let positionApplied = false;
    const applyPosition = () => {
      if (
        !sourceReady ||
        positionApplied ||
        !Number.isFinite(video.duration) ||
        video.duration <= 0
      ) {
        return;
      }
      const target = Math.min(startPosition, Math.max(0, video.duration - 0.1));
      // Safari can publish HLS metadata before its timeline is seekable. A
      // one-shot seek there can be ignored/clamped to zero. Retry on readiness
      // events and start the existing gate only after the restored position is
      // available, rather than declaring success from a property assignment.
      if (target > 0) {
        if (Math.abs(video.currentTime - target) > 0.1) {
          // seekable governs whether to REQUEST a new native seek. It is not
          // required to acknowledge decoded media already at the saved time:
          // Safari can expose those range updates later than frame readiness.
          if (
            nativeHls &&
            !Array.from({ length: video.seekable.length }, (_, index) => index).some(
              (index) =>
                video.seekable.start(index) <= target && video.seekable.end(index) >= target,
            )
          )
            return;
          if (video.seeking) return;
          try {
            video.currentTime = target;
          } catch {
            return;
          }
        }
        if (video.seeking || video.readyState < 2 || Math.abs(video.currentTime - target) > 0.1)
          return;
      }
      positionApplied = true;
      initialPositionReady.current = true;
      if (gate) gate.start();
      else if (resume?.wantsPlay !== false) void video.play().catch(() => undefined);
    };
    const positionEvents = [
      "loadedmetadata",
      "durationchange",
      "loadeddata",
      "canplay",
      "progress",
      "seeked",
    ];
    positionEvents.forEach((event) => video.addEventListener(event, applyPosition));

    let cancelled = false;
    let hls: Hls | null = null;
    const controller = new AbortController();
    const memory = (navigator as Navigator & { deviceMemory?: number }).deviceMemory;
    let policy = theatreBufferPolicy(undefined, memory);
    let advertisedBitrate = 0;
    const gate = bufferPlayback
      ? attachTheatreBufferGate(video, {
          policy,
          onView: setBufferView,
          onFailure: () => hls?.stopLoad(),
          resume: Boolean(resume),
          wantsPlay: resume?.wantsPlay,
        })
      : null;
    const watchdog = gate
      ? window.setInterval(() => {
          // Native media readiness/range updates can settle after their queued
          // events. Reconcile the saved position before checking buffer release.
          applyPosition();
          gate.tick();
        }, 500)
      : undefined;
    bufferingInfo.current = () => gate?.snapshot() ?? {};
    const updatePolicy = (bitrate: number) => {
      if (!bufferPlayback) return;
      policy = theatreBufferPolicy(bitrate, memory);
      if (hls) Object.assign(hls.config, policy.hls);
      gate?.setPolicy(policy);
    };
    let activeQuality: QualityInfo | undefined;
    let evaluateAuto: (() => void) | undefined;
    let measuredFragments = 0;
    let deliveryThroughput: number | null = null;
    const qualityFailure = (reason: string) => {
      if (cancelled) return;
      gate?.dispose();
      video.pause();
      hls?.stopLoad();
      setQualityError(reason);
    };
    const checkResolution = () => {
      const violation = originalResolutionViolation(
        qualityMode,
        activeQuality?.target_resolution,
        video.videoWidth,
        video.videoHeight,
      );
      if (violation) qualityFailure(violation);
    };
    const onPlaying = () => {
      hasPlayed.current = true;
      checkResolution();
    };
    video.addEventListener("playing", onPlaying);
    video.addEventListener("loadeddata", checkResolution);
    video.addEventListener("resize", checkResolution);

    const start = async () => {
      let playbackSource = source;
      let playbackType = sourceType;
      if (playbackPlanUrl && sourceType === "hls") {
        const url = new URL(playbackPlanUrl, window.location.href);
        const subtitleIndex = url.searchParams.get("subtitle_index");
        url.search = "";
        const request = async (body: object) => {
          const response = await fetch(url, {
            method: "POST",
            credentials: "include",
            signal: controller.signal,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            if (error.detail?.quality) {
              throw new Error(error.detail.message || "Requested quality is unavailable");
            }
            throw new Error(`Playback negotiation failed (${response.status})`);
          }
          return response.json();
        };
        const { source: mediaSource } = (await request({})) as { source: TheatreSource };
        updatePolicy(mediaSource.bitrate || 20_000_000);
        const capabilities = await theatreCapabilities(mediaSource, video, selectedAudio.current);
        const decodeTargets = await probeQualityTargets(
          mediaSource,
          video,
          (capabilities.direct || capabilities.video_copy) && subtitleIndex === null,
        );
        if (cancelled) return;
        const requestBody = {
          capabilities,
          subtitle_index: subtitleIndex === null ? null : Number(subtitleIndex),
          audio_index: selectedAudio.current,
          quality_mode: qualityMode,
          decode_targets: decodeTargets,
        };
        const plan = await request({
          ...requestBody,
          bandwidth_bps: qualityMode === "Auto" ? measuredBandwidth.current : null,
        });
        if (cancelled) return;
        playbackSource = plan.url;
        playbackType = plan.source_type;
        activeQuality = plan.diagnostics?.quality;
        selectedAudio.current = plan.diagnostics?.playback?.selected_audio_index ?? null;
        lastAutoCheck.current = performance.now();
        let evaluating = false;
        evaluateAuto = () => {
          if (
            qualityMode !== "Auto" ||
            evaluating ||
            measuredFragments < 3 ||
            video.paused ||
            performance.now() - lastAutoCheck.current < 30_000
          )
            return;
          evaluating = true;
          lastAutoCheck.current = performance.now();
          void request({
            ...requestBody,
            bandwidth_bps: measuredBandwidth.current,
            evaluate_only: true,
          })
            .then((evaluation) => {
              if (cancelled) return;
              const next = evaluation.quality as QualityInfo;
              if (
                JSON.stringify(next.target_resolution) !==
                JSON.stringify(activeQuality?.target_resolution)
              ) {
                prepareQualitySwitch();
                setAutoRevision((revision) => revision + 1);
              } else {
                activeQuality = next;
              }
            })
            .catch(() => undefined) // A failed Auto evaluation retains the current stream.
            .finally(() => {
              evaluating = false;
            });
        };
      }
      if (cancelled) return;
      if (playbackType === "file" || video.canPlayType("application/vnd.apple.mpegurl")) {
        nativeHls = playbackType === "hls";
        sourceReady = true;
        video.src = playbackSource;
        return;
      }
      const { default: HlsPlayer } = await import("hls.js");
      if (cancelled) return;
      if (!HlsPlayer.isSupported()) throw new Error("HLS unavailable");
      hls = new HlsPlayer({
        ...(bufferPlayback ? policy.hls : {}),
        startPosition: startPosition > 0 || resume ? startPosition : -1,
      });
      sourceReady = true;
      hls.loadSource(playbackSource);
      hls.attachMedia(video);
      hls.on(HlsPlayer.Events.MANIFEST_PARSED, (_event, data) => {
        if (qualityMode !== "Auto" && activeQuality?.target_resolution) {
          const target = activeQuality.target_resolution;
          const matching = data.levels
            .map((level, index) => ({ level, index }))
            .filter(({ level }) => level.width <= target.width && level.height <= target.height)
            .sort((a, b) => b.level.width * b.level.height - a.level.width * a.level.height);
          const chosen = matching[0];
          if (
            qualityMode === "Original" &&
            (!chosen ||
              originalResolutionViolation(
                qualityMode,
                target,
                chosen.level.width,
                chosen.level.height,
              ))
          ) {
            qualityFailure(
              "Provider HLS variants do not contain the Original source resolution; playback stopped without downscaling.",
            );
            return;
          }
          if (chosen && hls) hls.currentLevel = chosen.index;
        }
        advertisedBitrate = Math.max(...data.levels.map((level) => level.bitrate), 0);
        if (advertisedBitrate > 0) updatePolicy(advertisedBitrate);
        applyPosition();
      });
      hls.on(HlsPlayer.Events.FRAG_BUFFERED, (_event, data) => {
        if (data.frag.type === "main" && typeof data.frag.sn === "number") {
          const elapsed = data.stats.parsing.end - data.stats.loading.start;
          if (!data.stats.aborted && data.stats.loaded > 0 && elapsed > 0) measuredFragments++;
          deliveryThroughput = measuredDeliveryThroughput(
            deliveryThroughput,
            data.stats.aborted ? 0 : data.stats.loaded,
            data.stats.loading.start,
            data.stats.parsing.end,
          );
          const estimate = hls?.bandwidthEstimate;
          if (
            measuredFragments >= 3 &&
            deliveryThroughput &&
            estimate &&
            Number.isFinite(estimate) &&
            estimate > 0
          ) {
            measuredBandwidth.current = Math.max(
              1,
              Math.min(2147483647, Math.round(Math.min(estimate, deliveryThroughput))),
            );
            evaluateAuto?.();
          }
        }
        if (data.frag.duration > 0) {
          const measuredRate = (data.stats.total * 8) / data.frag.duration;
          // Raise the estimate on larger segments; never reduce quality or rely
          // on a single unusually small segment to expand the memory budget.
          if (measuredRate > Math.max(advertisedBitrate, policy.bitrateBasis))
            updatePolicy(measuredRate * 1.1);
        }
        gate?.tick();
      });
      hls.on(HlsPlayer.Events.ERROR, (_event, data) => {
        if (data.fatal) playbackErrorRef.current();
      });
    };
    void start().catch((error: unknown) => {
      if (!cancelled) {
        if (playbackPlanUrl)
          qualityFailure(
            error instanceof Error ? error.message : "Requested quality is unavailable",
          );
        else playbackErrorRef.current();
      }
    });
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(watchdog);
      gate?.dispose();
      if (positionApplied) preservedPosition.current = video.currentTime;
      video.removeEventListener("playing", onPlaying);
      video.removeEventListener("loadeddata", checkResolution);
      video.removeEventListener("resize", checkResolution);
      positionEvents.forEach((event) => video.removeEventListener(event, applyPosition));
      hls?.destroy();
      video.removeAttribute("src");
      video.load();
    };
  }, [
    source,
    sourceType,
    playbackPlanUrl,
    bufferPlayback,
    qualityMode,
    autoRevision,
    prepareQualitySwitch,
  ]);

  const progressRef = useRef(onProgress);
  progressRef.current = onProgress;
  const progressEnabled = Boolean(onProgress);
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !progressEnabled) return;
    return attachPlaybackProgress(video, (position, duration, completed) => {
      // Loading/initial seeking can emit pause/timeupdate at zero. They are not
      // viewer progress and must not replace the saved Continue position.
      if (initialPositionReady.current) progressRef.current?.(position, duration, completed);
    });
  }, [progressEnabled]);

  useEffect(() => {
    const player = playerRef.current;
    const video = videoRef.current;
    if (!player || !video) return;
    const controller = attachPlayerFullscreen(player, setIsFullscreen, setFullscreenError);
    fullscreen.current = controller;
    setFullscreenAvailable(controller.available);
    return controller.dispose;
  }, []);

  useEffect(() => {
    const player = playerRef.current;
    const video = videoRef.current;
    const strip = controlStripRef.current;
    if (!player || !video || !strip) return;
    return attachPlayerControlVisibility(player, video, strip, setControlsVisible);
  }, []);

  return (
    <div ref={playerRef} className="pv-theatre-player">
      <video
        ref={bindVideo}
        className="pv-theatre-video"
        onError={sourceType === "file" ? onPlaybackError : undefined}
        autoPlay={!bufferPlayback}
        playsInline
        preload={bufferPlayback ? "auto" : "metadata"}
      >
        Your browser does not support video playback.
      </video>
      <div className="pv-player-status pointer-events-none" aria-live="polite">
        {fullscreenError && (
          <p role="alert" className="text-sm">
            {fullscreenError}
          </p>
        )}
        {qualityError && (
          <div role="alert" className="text-sm">
            {qualityError} Select another quality mode to retry.
          </div>
        )}
        {!qualityError && bufferPlayback && bufferView.phase !== "ready" && (
          <div role="status" className="text-xs">
            {bufferView.phase === "failed"
              ? "Playback could not build a playable buffer. Close the player and try again."
              : bufferView.phase === "blocked"
                ? "Ready to play. Press Play."
                : bufferView.phase === "preparing"
                  ? "Preparing playback…"
                  : `${bufferView.phase === "startup" ? "Preparing playback" : "Rebuilding playback buffer"}… ${bufferView.ahead} / ${bufferView.target} seconds`}
          </div>
        )}
      </div>
      <div
        ref={controlStripRef}
        aria-hidden={!controlsVisible}
        inert={!controlsVisible}
        data-visible={controlsVisible}
        className="pv-player-controls absolute inset-0 pointer-events-none text-xs text-white transition-[opacity,visibility] duration-200 motion-reduce:transition-none"
      >
        <div className="pv-player-transport">
          <PlayerTransport
            video={mediaElement}
            isPlaybackHeld={() =>
              ["preparing", "startup", "rebuffering", "seeking", "failed"].includes(
                String(bufferingInfo.current().phase),
              )
            }
          />
        </div>
        <div className="pv-player-toolbar">
          {playbackPlanUrl && sourceType === "hls" && (
            <PlayerMenu
              label="Quality"
              value={qualityMode}
              options={qualityModes.map((mode) => ({ value: mode, label: mode }))}
              onChange={(value) => {
                prepareQualitySwitch();
                setQualityMode(value as QualityMode);
                onQualityModeChange?.(value as QualityMode);
              }}
            />
          )}
          {subtitleTracks.length > 0 && onSubtitleChange ? (
            <PlayerMenu
              label="Subtitles"
              value={String(selectedSubtitleIndex ?? "off")}
              options={[
                { value: "off", label: "Off" },
                ...subtitleTracks.map((track) => ({
                  value: String(track.index),
                  label: track.label,
                })),
              ]}
              onChange={(value) => {
                onSubtitleChange(value === "off" ? null : Number(value));
              }}
            />
          ) : null}
          {fullscreenAvailable && (
            <button
              type="button"
              className="pv-player-fullscreen min-h-11 min-w-11 shrink-0 rounded p-1.5 hover:bg-white/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
              aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
              onClick={() => void fullscreen.current?.toggle()}
            >
              {isFullscreen ? <Minimize size={18} /> : <Maximize size={18} />}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
