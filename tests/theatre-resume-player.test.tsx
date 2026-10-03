import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import Hls from "hls.js";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { MoviePlayer } = await import("../src/components/pv/MoviePlayer");
const originalCanPlay = HTMLVideoElement.prototype.canPlayType;
const originalSupported = Hls.isSupported;
const originalLoad = Hls.prototype.loadSource;
const originalAttach = Hls.prototype.attachMedia;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  HTMLVideoElement.prototype.canPlayType = originalCanPlay;
  Hls.isSupported = originalSupported;
  Hls.prototype.loadSource = originalLoad;
  Hls.prototype.attachMedia = originalAttach;
});
async function mount(native: boolean, saved = 125) {
  HTMLVideoElement.prototype.canPlayType = () => (native ? "probably" : "");
  const reports: number[] = [];
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MoviePlayer
        source="/synthetic-master.m3u8"
        startSeconds={saved}
        bufferPlayback
        onPlaybackError={() => {
          throw new Error("Unexpected playback error");
        }}
        onProgress={(position) => reports.push(position)}
      />,
    ),
  );
  const video = container.querySelector("video")!;
  Object.defineProperty(video, "duration", { value: 600, configurable: true });
  return { video, reports };
}
async function emit(video: HTMLVideoElement, event: string) {
  await act(async () => video.dispatchEvent(new Event(event)));
}
test("native HLS Continue waits for a seekable timeline and confirmed position before playback/progress", async () => {
  const { video, reports } = await mount(true);
  let seekable = false,
    ready = 1,
    seeking = false,
    position = 0,
    end = 0;
  let plays = 0;
  const seeks: number[] = [];
  Object.defineProperty(video, "seekable", {
    get: () => ({ length: seekable ? 1 : 0, start: () => 0, end: () => 600 }),
  });
  Object.defineProperty(video, "currentTime", {
    get: () => position,
    set: (value: number) => {
      seeks.push(value);
      if (seekable) {
        position = value;
        seeking = true;
      }
    },
  });
  Object.defineProperty(video, "readyState", { get: () => ready });
  Object.defineProperty(video, "seeking", { get: () => seeking });
  Object.defineProperty(video, "buffered", {
    get: () => ({ length: end > 0 ? 1 : 0, start: () => 125, end: () => end }),
  });
  video.play = async () => {
    plays++;
  };
  await emit(video, "loadedmetadata");
  await emit(video, "timeupdate");
  expect(seeks).toEqual([]);
  expect(reports).toEqual([]);
  seekable = true;
  await emit(video, "progress");
  expect(position).toBe(125);
  expect(plays).toBe(0);
  // A rejected/clamped initial seek must not latch success at metadata time.
  seeking = false;
  position = 0;
  ready = 3;
  await emit(video, "canplay");
  expect(seeks).toEqual([125, 125]);
  expect(plays).toBe(0);
  seeking = false;
  end = 150;
  await emit(video, "seeked");
  expect(plays).toBe(1);
  expect(position).toBe(125);
  expect(reports).toEqual([125]);
  // Initial restoration must not snap later viewer seeks back to Continue.
  position = 200;
  await emit(video, "seeked");
  await emit(video, "canplay");
  expect(position).toBe(200);
  expect(seeks).toEqual([125, 125]);
});
test("hls.js receives the saved initial position before loading any fragments", async () => {
  let start: number | undefined;
  Hls.isSupported = () => true;
  Hls.prototype.loadSource = function () {
    start = this.config.startPosition;
  };
  Hls.prototype.attachMedia = () => {};
  await mount(false);
  // Dynamic module import completes asynchronously in the source effect.
  for (let attempt = 0; start === undefined && attempt < 10; attempt++) {
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  }
  expect(start).toBe(125);
});

test("a confirmed native Continue position can start while seekable ranges lag behind decoded media", async () => {
  const { video, reports } = await mount(true);
  let paused = true;
  let plays = 0;
  Object.defineProperties(video, {
    currentTime: { value: 125, writable: true },
    readyState: { value: 3 },
    seeking: { value: false },
    paused: { get: () => paused },
    seekable: { get: () => ({ length: 0 }) },
    buffered: { get: () => ({ length: 1, start: () => 125, end: () => 150 }) },
  });
  video.play = () => {
    plays++;
    paused = false;
    video.dispatchEvent(new Event("play"));
    if (!paused) video.dispatchEvent(new Event("playing"));
    return Promise.resolve();
  };
  video.pause = () => {
    paused = true;
    video.dispatchEvent(new Event("pause"));
  };
  await emit(video, "loadeddata");
  await emit(video, "canplay");
  await emit(video, "progress");
  // Before the fix this stayed preparing, and even explicit Play was paused
  // by that gate despite playable media at the target.
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Play"]')?.click());
  expect(plays).toBeGreaterThan(0);
  expect(paused).toBe(false);
  expect(video.currentTime).toBe(125);
  expect(container.querySelector('[role="status"]')).toBeNull();
  await emit(video, "timeupdate");
  expect(reports).toEqual([125]);
});

test("readiness settling between native media events is reconciled by the existing watchdog", async () => {
  const originalInterval = window.setInterval;
  let tick: (() => void) | undefined;
  window.setInterval = ((callback: () => void, delay: number) => {
    if (delay === 500) tick = callback;
    return originalInterval(callback, delay);
  }) as typeof window.setInterval;
  try {
    const { video } = await mount(true);
    let ready = 1;
    let position = 0;
    let seekable = false;
    let plays = 0;
    Object.defineProperties(video, {
      currentTime: {
        get: () => position,
        set: (value) => {
          position = value;
        },
      },
      readyState: { get: () => ready },
      seeking: { value: false },
      seekable: { get: () => ({ length: seekable ? 1 : 0, start: () => 0, end: () => 600 }) },
      buffered: { get: () => ({ length: ready === 3 ? 1 : 0, start: () => 125, end: () => 150 }) },
    });
    video.play = async () => {
      plays++;
    };
    await emit(video, "loadedmetadata");
    expect(position).toBe(0);
    expect(plays).toBe(0);
    seekable = true;
    ready = 3;
    await act(async () => tick!());
    expect(position).toBe(125);
    expect(plays).toBe(1);
    expect(container.querySelector('[role="status"]')).toBeNull();
  } finally {
    window.setInterval = originalInterval;
  }
});

test("native startup does not restart an in-flight saved-position seek", async () => {
  const { video } = await mount(true);
  let position = 0,
    seeking = false,
    plays = 0;
  const seeks: number[] = [];
  Object.defineProperties(video, {
    currentTime: {
      get: () => position,
      set: (value) => {
        seeks.push(value);
        seeking = true;
      },
    },
    readyState: { value: 3 },
    seeking: { get: () => seeking },
    seekable: { get: () => ({ length: 1, start: () => 0, end: () => 600 }) },
    buffered: { get: () => ({ length: 1, start: () => 125, end: () => 150 }) },
  });
  video.play = async () => {
    plays++;
  };
  await emit(video, "loadedmetadata");
  for (const event of ["progress", "canplay", "durationchange", "loadeddata"])
    await emit(video, event);
  expect(seeks).toEqual([125]);
  expect(plays).toBe(0);
  position = 125;
  seeking = false;
  await emit(video, "seeked");
  expect(plays).toBe(1);
});
