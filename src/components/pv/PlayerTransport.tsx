import { useEffect, useRef, useState } from "react";

// Presentation for the existing media element. The playback/buffer controller
// continues to handle play intent, seeking and progress on that same element.
export function PlayerTransport({
  video,
  isPlaybackHeld = () => false,
}: {
  video: HTMLVideoElement | null;
  isPlaybackHeld?: () => boolean;
}) {
  const [state, setState] = useState({ paused: true, time: 0, duration: 0, muted: false });
  const [playError, setPlayError] = useState<string | null>(null);
  const playAttempt = useRef(0);
  const transport = useRef<HTMLDivElement>(null);
  const timeline = useRef<HTMLInputElement>(null);
  const drag = useRef<{ pointerId: number; target: number } | null>(null);
  const pendingSeek = useRef<number | null>(null);
  const [preview, setPreview] = useState<number | null>(null);
  const scrubbing = (active: boolean) => {
    if (!transport.current) return;
    transport.current.dataset.scrubbing = String(active);
    transport.current.dispatchEvent(new Event("pvcontrolscrubchange", { bubbles: true }));
  };
  const commit = (target: number) => {
    if (!video || pendingSeek.current === target) return;
    // A native range can emit change after pointerup. Acknowledge that same
    // target without a second seek, including a click on the current position.
    if (!video.seeking && Math.abs(video.currentTime - target) < 0.001) {
      pendingSeek.current = null;
      setPreview(null);
      return;
    }
    pendingSeek.current = target;
    setPreview(target);
    try {
      video.currentTime = target;
    } catch {
      pendingSeek.current = null;
      setPreview(null);
    }
  };
  const finishDrag = useRef<(event: PointerEvent) => void>(() => {});
  finishDrag.current = (event) => {
    if (!drag.current || event.pointerId !== drag.current.pointerId) return;
    const target = Number(timeline.current?.value ?? drag.current.target);
    drag.current = null;
    if (event.type === "pointercancel") setPreview(pendingSeek.current);
    else commit(target);
    scrubbing(false);
  };
  useEffect(() => {
    if (!video) return;
    // PV has Mute, but no volume slider. Keep normal full media gain;
    // iOS can ignore this property and retain hardware volume authority.
    video.volume = 1;
    const update = () =>
      setState({
        paused: video.paused,
        time: video.currentTime,
        duration: Number.isFinite(video.duration) ? video.duration : 0,
        muted: video.muted,
      });
    const events = [
      "play",
      "playing",
      "pause",
      "ended",
      "timeupdate",
      "seeked",
      "durationchange",
      "volumechange",
      "emptied",
      "loadedmetadata",
      "canplay",
    ];
    const invalidate = () => {
      playAttempt.current++;
    };
    const reset = () => {
      invalidate();
      setPlayError(null);
    };
    const resetPosition = () => {
      drag.current = null;
      pendingSeek.current = null;
      setPreview(null);
      scrubbing(false);
    };
    const acknowledgeSeek = () => {
      // A queued completion from a superseded seek must not clear a newer
      // preview. Read the live element, not the event's former position.
      if (
        pendingSeek.current !== null &&
        !video.seeking &&
        Math.abs(video.currentTime - pendingSeek.current) < 0.5
      ) {
        pendingSeek.current = null;
        if (!drag.current) setPreview(null);
      }
    };
    const finish = (event: PointerEvent) => finishDrag.current(event);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    events.forEach((event) => video.addEventListener(event, update));
    video.addEventListener("seeked", acknowledgeSeek);
    video.addEventListener("emptied", resetPosition);
    video.addEventListener("emptied", reset);
    video.addEventListener("playing", reset);
    update();
    return () => {
      invalidate();
      resetPosition();
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      events.forEach((event) => video.removeEventListener(event, update));
      video.removeEventListener("seeked", acknowledgeSeek);
      video.removeEventListener("emptied", resetPosition);
      video.removeEventListener("emptied", reset);
      video.removeEventListener("playing", reset);
    };
  }, [video]);
  return (
    <div ref={transport} className="flex w-full min-w-0 flex-wrap items-center gap-2">
      <button
        type="button"
        className="min-h-11 min-w-11 shrink-0 rounded px-2 hover:bg-white/10 focus-visible:outline-white"
        aria-label={state.paused ? "Play" : "Pause"}
        disabled={!video}
        onClick={() => {
          if (!video) return;
          setPlayError(null);
          const attempt = ++playAttempt.current;
          const failed = (error: unknown) => {
            if (attempt !== playAttempt.current) return;
            const name = error && typeof error === "object" && "name" in error ? error.name : "";
            // The existing buffer gate and source switches intentionally pause
            // pending playback. Keep their interruption/release policy intact.
            if (name === "AbortError" && isPlaybackHeld()) return;
            setState((previous) => ({ ...previous, paused: video.paused }));
            setPlayError(
              name === "NotAllowedError"
                ? "Playback was blocked by the browser. Tap Play to retry."
                : "Playback could not start. Tap Play to retry.",
            );
          };
          // Read the media element at the time of interaction. Media events are
          // queued; React's last rendered paused state can already be stale.
          if (video.paused || video.ended) {
            try {
              // Call synchronously while Safari still has the user's gesture.
              void video.play().catch(failed);
            } catch (error) {
              failed(error);
            }
          } else video.pause();
        }}
      >
        {state.paused ? "Play" : "Pause"}
      </button>
      <input
        ref={timeline}
        aria-label="Playback position"
        type="range"
        min={0}
        max={state.duration || 0}
        step="any"
        value={Math.min(preview ?? state.time, state.duration)}
        disabled={!state.duration}
        className="min-h-11 min-w-0 flex-1 touch-none accent-white"
        onPointerDown={(event) => {
          if (event.button !== 0 || drag.current) return;
          const target = Number(event.currentTarget.value);
          drag.current = { pointerId: event.pointerId, target };
          setPreview(target);
          scrubbing(true);
          // Keep the range's browser-owned drag tracking. Explicit pointer
          // capture on the input disrupts WebKit's native thumb interaction;
          // window release/cancel listeners also handle release outside it.
        }}
        onChange={(event) => {
          const target = Number(event.target.value);
          if (drag.current) {
            drag.current.target = target;
            setPreview(target);
          } else commit(target); // Native keyboard/accessibility adjustment.
        }}
      />
      <button
        type="button"
        className="min-h-11 min-w-11 shrink-0 rounded px-2 hover:bg-white/10 focus-visible:outline-white"
        aria-label={state.muted ? "Unmute" : "Mute"}
        onClick={() => {
          if (video) {
            video.volume = 1;
            video.muted = !video.muted;
          }
        }}
      >
        {state.muted ? "Unmute" : "Mute"}
      </button>
      {playError && (
        <p role="alert" className="pointer-events-none absolute bottom-full left-0 right-0 text-sm">
          {playError}
        </p>
      )}
    </div>
  );
}
