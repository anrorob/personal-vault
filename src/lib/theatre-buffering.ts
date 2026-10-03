// Experimental playback policy, not a user setting or a quality constraint.
export function theatreBufferPolicy(bitrate = 20_000_000, deviceMemory?: number) {
  const bytes = (deviceMemory && deviceMemory <= 4 ? 64 : 160) * 1024 * 1024;
  const rate = Number.isFinite(bitrate) && bitrate > 0 ? bitrate : 20_000_000;
  // Leave room for one in-flight segment. hls.js maxBufferSize is NOT a hard
  // byte cap: its time target also needs bounding (getMaxBufferLength).
  const forward = Math.max(3, Math.min(90, (bytes * 8) / rate - 3));
  return {
    startupSeconds: Math.min(24, forward * 0.75),
    recoverySeconds: Math.min(12, forward * 0.5),
    // About two current three-second HLS segments; only the seek release gate.
    seekSeconds: Math.min(6, forward * 0.5),
    bitrateBasis: rate,
    hls: {
      maxBufferLength: forward,
      maxMaxBufferLength: forward,
      frontBufferFlushThreshold: forward,
      maxBufferSize: bytes,
      backBufferLength: 10,
      maxBufferHole: 0.1,
      highBufferWatchdogPeriod: 2,
      autoStartLoad: true,
    },
  };
}

export type TheatreBufferPolicy = ReturnType<typeof theatreBufferPolicy>;
type Phase = "preparing" | "startup" | "rebuffering" | "seeking" | "ready" | "blocked" | "failed";
export type BufferView = { phase: Phase; ahead: number; target: number };

export function bufferRanges(ranges: TimeRanges): [number, number][] {
  return Array.from({ length: ranges.length }, (_, i) => [ranges.start(i), ranges.end(i)]);
}

export function playableAhead(ranges: TimeRanges, position: number): number {
  // Never count a later disconnected range as playable at the current position.
  for (const [start, end] of bufferRanges(ranges)) {
    if (start <= position + 0.05 && end > position) return end - position;
  }
  return 0;
}

/** Event-driven playback gate. tick's clock is only a bounded failure watchdog;
 * elapsed time alone can never make an empty buffer playable. */
export function attachTheatreBufferGate(
  video: HTMLVideoElement,
  options: {
    policy: TheatreBufferPolicy;
    onView: (view: BufferView) => void;
    onFailure: () => void;
    now?: () => number;
    resume?: boolean;
    wantsPlay?: boolean;
  },
) {
  const now = options.now ?? (() => performance.now());
  const createdAt = now();
  let policy = options.policy;
  let phase: Phase = "preparing";
  let gateAt = createdAt;
  let wantsPlay = options.wantsPlay ?? true;
  let internalPauses = 0;
  let disposed = false;
  let firstPlaying: number | null = null;
  let firstFrame: number | null = null;
  let frameRequest: number | undefined;
  let bufferedAtStart: number | null = null;
  let rebufferCount = 0;
  let waitingEvents = 0;
  let stalledEvents = 0;
  let fallbackReason: string | null = null;
  let lastView = "";
  const samples: Record<string, number> = {};
  const isGated = () => ["startup", "rebuffering", "seeking"].includes(phase);
  const ahead = () => playableAhead(video.buffered, video.currentTime);
  const target = () =>
    Math.min(
      phase === "startup"
        ? policy.startupSeconds
        : phase === "seeking"
          ? policy.seekSeconds
          : policy.recoverySeconds,
      Number.isFinite(video.duration) ? Math.max(0, video.duration - video.currentTime) : Infinity,
    );
  const publish = () => {
    const view = { phase, ahead: Math.floor(ahead()), target: Math.ceil(target()) };
    const key = JSON.stringify(view);
    if (key !== lastView && !disposed) {
      lastView = key;
      options.onView(view);
    }
  };
  const pauseForBuffer = () => {
    if (!video.paused) {
      internalPauses++;
      video.pause();
    }
  };
  const play = () => {
    if (!wantsPlay || disposed || video.ended) return;
    void video.play().catch((error: unknown) => {
      if (disposed || isGated()) return;
      if (error instanceof Error && error.name === "NotAllowedError") {
        phase = "blocked";
        wantsPlay = false;
        publish();
      } else if (!(error instanceof Error && error.name === "AbortError")) {
        phase = "failed";
        wantsPlay = false;
        options.onFailure();
        publish();
      }
    });
  };
  const hold = (next: Phase) => {
    phase = next;
    gateAt = now();
    pauseForBuffer();
    publish();
  };
  const tick = () => {
    if (disposed) return;
    if (phase === "preparing" && now() - createdAt >= 90_000) {
      phase = "failed";
      wantsPlay = false;
      pauseForBuffer();
      options.onFailure();
    }
    const seconds = ahead();
    if (firstPlaying !== null) {
      for (const elapsed of [30, 120]) {
        if (now() - firstPlaying >= elapsed * 1000 && samples[String(elapsed)] === undefined) {
          samples[String(elapsed)] = seconds;
        }
      }
    }
    if (isGated() && !video.seeking) {
      const remaining = target();
      const ready = seconds > 0 && video.readyState >= 3;
      const elapsed = now() - gateAt;
      const full = ready && seconds + 0.1 >= remaining;
      const limited = ready && elapsed >= 45_000 && seconds >= Math.min(6, remaining);
      if (full || limited) {
        if (limited && !full)
          fallbackReason =
            "Buffer target not reached; resumed with measured playable media after 45s";
        phase = "ready";
        publish();
        play();
      } else if (elapsed >= 90_000) {
        phase = "failed";
        wantsPlay = false;
        pauseForBuffer();
        options.onFailure();
      }
    }
    publish();
  };
  const onPlay = () => {
    wantsPlay = true;
    if (isGated() || phase === "preparing" || phase === "failed") pauseForBuffer();
    else if (phase === "blocked") {
      phase = "ready";
      publish();
    }
  };
  const onPause = () => {
    if (internalPauses) internalPauses--;
    else wantsPlay = false;
  };
  const onPlaying = () => {
    if (isGated() || phase === "preparing") {
      pauseForBuffer();
      return;
    }
    if (firstPlaying === null) {
      firstPlaying = now();
      bufferedAtStart = ahead();
      if (video.requestVideoFrameCallback) {
        frameRequest = video.requestVideoFrameCallback(() => {
          if (!disposed) firstFrame = now();
        });
      }
    }
  };
  const onWaiting = () => {
    waitingEvents++;
    if (
      phase === "ready" &&
      wantsPlay &&
      !video.seeking &&
      !video.ended &&
      ahead() < policy.recoverySeconds
    ) {
      rebufferCount++;
      hold("rebuffering");
      tick();
    }
  };
  const onStalled = () => {
    stalledEvents++;
    if (video.readyState < 3 && ahead() < 1) onWaiting();
  };
  const onSeeking = () => {
    if (phase !== "preparing" && phase !== "failed") {
      hold(firstPlaying === null && !options.resume ? "startup" : "seeking");
    }
  };
  const events: [string, EventListener][] = [
    ["play", onPlay],
    ["pause", onPause],
    ["playing", onPlaying],
    ["waiting", onWaiting],
    ["stalled", onStalled],
    ["seeking", onSeeking],
    ["seeked", tick],
    ["progress", tick],
    ["canplay", tick],
    ["timeupdate", tick],
  ];
  events.forEach(([event, handler]) => video.addEventListener(event, handler));
  return {
    start() {
      if (phase === "preparing") hold(options.resume ? "seeking" : "startup");
      tick();
    },
    tick,
    setPolicy(next: TheatreBufferPolicy) {
      policy = next;
      tick();
    },
    snapshot() {
      return {
        phase,
        wants_playback: wantsPlay,
        buffered_seconds_ahead: ahead(),
        buffer_ranges: bufferRanges(video.buffered),
        startup_buffer_target: policy.startupSeconds,
        recovery_buffer_target: policy.recoverySeconds,
        seek_buffer_target: policy.seekSeconds,
        required_resume_buffer_seconds: isGated() ? target() : null,
        rebuffer_count: rebufferCount,
        waiting_events: waitingEvents,
        stalled_events: stalledEvents,
        startup_seconds: firstFrame === null ? null : (firstFrame - createdAt) / 1000,
        playing_event_seconds: firstPlaying === null ? null : (firstPlaying - createdAt) / 1000,
        startup_basis: "Player mount to first presented frame; playing event reported separately",
        buffered_seconds_at_start: bufferedAtStart,
        buffer_samples_after_start: { ...samples },
        fallback_reason: fallbackReason,
      };
    },
    dispose() {
      disposed = true;
      events.forEach(([event, handler]) => video.removeEventListener(event, handler));
      if (frameRequest !== undefined) video.cancelVideoFrameCallback(frameRequest);
    },
  };
}
