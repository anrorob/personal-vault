import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { MoviePlayer } = await import("../src/components/pv/MoviePlayer");
const originalFetch = globalThis.fetch;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
});

async function mount(bufferPlayback: boolean, startSeconds = 0) {
  globalThis.fetch = (async () => new Response(null, { status: 404 })) as typeof fetch;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MoviePlayer
        source="/test-authorized-file"
        sourceType="file"
        bufferPlayback={bufferPlayback}
        startSeconds={startSeconds}
        onPlaybackError={() => {
          throw new Error("Unexpected playback error");
        }}
      />,
    ),
  );
  const video = container.querySelector("video")!;
  Object.defineProperty(video, "duration", { value: 300, configurable: true });
  Object.defineProperty(video, "readyState", { value: 3 });
  let end = 0;
  Object.defineProperty(video, "buffered", {
    get: () => ({ length: end > 0 ? 1 : 0, start: () => 0, end: () => end }),
  });
  let plays = 0;
  video.play = async () => {
    plays++;
  };
  return {
    video,
    plays: () => plays,
    bufferTo: (value: number) => {
      end = value;
    },
  };
}

test("the PV Play button remains gated, then starts and can pause real media state", async () => {
  const { video, bufferTo } = await mount(true);
  let paused = true;
  Object.defineProperty(video, "paused", { get: () => paused });
  video.pause = () => {
    paused = true;
    video.dispatchEvent(new Event("pause"));
  };
  video.play = () => {
    paused = false;
    video.dispatchEvent(new Event("play"));
    if (paused) return Promise.reject(new DOMException("Synthetic gate pause", "AbortError"));
    video.dispatchEvent(new Event("playing"));
    return Promise.resolve();
  };
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Play"]')!.click());
  expect(video.paused).toBe(true);
  expect(container.querySelector('[role="status"]')?.textContent).toContain("Preparing playback");
  expect(container.querySelector('[role="alert"]')).toBeNull();
  bufferTo(25);
  await act(async () => video.dispatchEvent(new Event("progress")));
  expect(video.paused).toBe(false);
  expect(container.querySelector('[role="status"]')).toBeNull();
  await act(async () =>
    container.querySelector<HTMLButtonElement>('[aria-label="Pause"]')!.click(),
  );
  await act(async () => video.dispatchEvent(new Event("progress")));
  expect(video.paused).toBe(true);
  expect(container.querySelector('[aria-label="Play"]')).not.toBeNull();
});

test("the PV Play button recovers blocked autoplay directly inside the user's click", async () => {
  const { video, bufferTo } = await mount(true);
  let paused = true,
    inGesture = false,
    attempts = 0;
  Object.defineProperty(video, "paused", { get: () => paused });
  video.play = () => {
    attempts++;
    if (!inGesture)
      return Promise.reject(new DOMException("Synthetic autoplay denial", "NotAllowedError"));
    paused = false;
    video.dispatchEvent(new Event("play"));
    video.dispatchEvent(new Event("playing"));
    return Promise.resolve();
  };
  bufferTo(25);
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  expect(container.querySelector('[role="status"]')?.textContent).toContain("Press Play");
  await act(async () => {
    inGesture = true;
    container.querySelector<HTMLButtonElement>('[aria-label="Play"]')!.click();
    inGesture = false;
  });
  expect(attempts).toBe(2);
  expect(video.paused).toBe(false);
  expect(container.querySelector('[aria-label="Pause"]')).not.toBeNull();
  expect(container.querySelector('[role="status"]')).toBeNull();
});

test("Theatre opt-in replaces autoplay with a visible measured-media gate", async () => {
  const { video, plays, bufferTo } = await mount(true);
  expect(video.hasAttribute("autoplay")).toBe(false);
  expect(video.preload).toBe("auto");
  expect(container.textContent).toContain("Preparing playback");
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  expect(plays()).toBe(0);
  bufferTo(25);
  await act(async () => video.dispatchEvent(new Event("progress")));
  expect(plays()).toBe(1);
  expect(container.querySelector('[role="status"]')).toBeNull();
});

test("non-Theatre consumers retain autoplay and metadata preload", async () => {
  const { video, plays } = await mount(false);
  expect(video.hasAttribute("autoplay")).toBe(true);
  expect(video.preload).toBe("metadata");
  expect(container.querySelector('[role="status"]')).toBeNull();
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  expect(plays()).toBe(1);
});

test("late finite duration activates the buffer gate after early metadata", async () => {
  const { video, plays, bufferTo } = await mount(true);
  Object.defineProperty(video, "duration", { value: Infinity, configurable: true });
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  bufferTo(25);
  expect(plays()).toBe(0);
  Object.defineProperty(video, "duration", { value: 300, configurable: true });
  await act(async () => video.dispatchEvent(new Event("durationchange")));
  expect(plays()).toBe(1);
});

test("Theatre retains the existing initial resume-position rule", async () => {
  const { video, plays, bufferTo } = await mount(true, 100);
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  expect(video.currentTime).toBe(100);
  bufferTo(110);
  await act(async () => video.dispatchEvent(new Event("progress")));
  expect(plays()).toBe(0);
  bufferTo(125);
  await act(async () => video.dispatchEvent(new Event("progress")));
  expect(plays()).toBe(1);
});

test("shared Theatre player releases a scene jump at six seconds without replacing the source", async () => {
  const { video, plays, bufferTo } = await mount(true);
  const source = video.src;
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  bufferTo(24);
  await act(async () => video.dispatchEvent(new Event("progress")));
  await act(async () => video.dispatchEvent(new Event("playing")));
  expect(plays()).toBe(1);
  video.currentTime = 100;
  await act(async () => video.dispatchEvent(new Event("seeking")));
  bufferTo(103);
  await act(async () => video.dispatchEvent(new Event("seeked")));
  expect(plays()).toBe(1);
  expect(container.querySelector('[role="status"]')).not.toBeNull();
  bufferTo(106);
  await act(async () => video.dispatchEvent(new Event("progress")));
  expect(plays()).toBe(2);
  expect(container.querySelector('[role="status"]')).toBeNull();
  expect(video.src).toBe(source);
  expect(video.currentTime).toBe(100);
  expect(video.preload).toBe("auto");
});

test("explicit resume after clearing Watched retains a near-end saved position", async () => {
  const { video, plays, bufferTo } = await mount(true, 298);
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  expect(video.currentTime).toBe(298);
  expect(plays()).toBe(0);
  bufferTo(300);
  await act(async () => video.dispatchEvent(new Event("progress")));
  expect(plays()).toBe(1);
});

for (const paused of [false, true]) {
  test(`scrub release retains the shared Movie/TV seek gate and ${paused ? "paused" : "playing"} intent`, async () => {
    const { video, plays, bufferTo } = await mount(true);
    await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
    bufferTo(24);
    await act(async () => video.dispatchEvent(new Event("progress")));
    await act(async () => video.dispatchEvent(new Event("playing")));
    expect(plays()).toBe(1);
    if (paused) await act(async () => video.dispatchEvent(new Event("pause")));
    const source = video.src;
    const input = container.querySelector<HTMLInputElement>("input")!;
    await act(async () =>
      input.dispatchEvent(
        new PointerEvent("pointerdown", {
          bubbles: true,
          pointerId: 1,
          pointerType: "touch",
          button: 0,
        }),
      ),
    );
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "100");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      video.dispatchEvent(new Event("timeupdate"));
    });
    expect(input.value).toBe("100");
    expect(video.currentTime).toBe(0);
    expect(container.querySelector('[role="status"]')).toBeNull();
    await act(async () => window.dispatchEvent(new PointerEvent("pointerup", { pointerId: 1 })));
    expect(video.currentTime).toBe(100);
    await act(async () => video.dispatchEvent(new Event("seeking")));
    bufferTo(105);
    await act(async () => video.dispatchEvent(new Event("seeked")));
    expect(plays()).toBe(1);
    expect(container.querySelector('[role="status"]')).not.toBeNull();
    bufferTo(106);
    await act(async () => video.dispatchEvent(new Event("progress")));
    expect(plays()).toBe(paused ? 1 : 2);
    expect(container.querySelector('[role="status"]')).toBeNull();
    expect(video.src).toBe(source);
  });
}
