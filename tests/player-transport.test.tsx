import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { PlayerTransport } = await import("../src/components/pv/PlayerTransport");
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
});

async function mount(initialPaused = true, held = false, initialVolume = 1) {
  const video = document.createElement("video");
  video.volume = initialVolume;
  let paused = initialPaused;
  Object.defineProperty(video, "paused", { get: () => paused });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<PlayerTransport video={video} isPlaybackHeld={() => held} />));
  return {
    video,
    setPaused: (value: boolean) => {
      paused = value;
    },
  };
}

test("programmable volume starts full and Mute/unmute restore normal gain", async () => {
  const { video } = await mount(true, false, 0.5);
  expect(video.volume).toBe(1);
  const mute = () => container.querySelector<HTMLButtonElement>("button:last-of-type")!;
  await act(async () => mute().click());
  await emit(video, "volumechange");
  expect(video.muted).toBe(true);
  expect(mute().textContent).toBe("Unmute");
  video.volume = 0.25;
  await act(async () => mute().click());
  await emit(video, "volumechange");
  expect(video.muted).toBe(false);
  expect(video.volume).toBe(1);
  expect(mute().textContent).toBe("Mute");
});

async function scrubHarness(paused = true) {
  const { video } = await mount(paused);
  Object.defineProperty(video, "duration", { value: 300 });
  let actual = 40;
  let seeking = false;
  const seeks: number[] = [];
  Object.defineProperty(video, "currentTime", {
    get: () => actual,
    // Model an asynchronous engine: assignment queues a seek; the rendered
    // position remains old until the completion is explicitly delivered.
    set: (value: number) => {
      seeks.push(value);
      seeking = true;
      video.dispatchEvent(new Event("seeking"));
    },
  });
  Object.defineProperty(video, "seeking", { get: () => seeking });
  await emit(video, "durationchange");
  const input = container.querySelector<HTMLInputElement>("input")!;
  return {
    video,
    input,
    seeks,
    pointer: async (type: string, pointerType = "mouse", pointerId = 1) => {
      await act(async () =>
        input.dispatchEvent(
          new PointerEvent(type, {
            bubbles: true,
            pointerId,
            pointerType,
            button: 0,
          }),
        ),
      );
    },
    preview: async (value: number) => {
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(
          input,
          String(value),
        );
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    },
    complete: async (value: number) => {
      actual = value;
      seeking = false;
      await emit(video, "seeked");
    },
    tick: async (value: number) => {
      actual = value;
      await emit(video, "timeupdate");
    },
  };
}

for (const pointerType of ["mouse", "touch"]) {
  for (const paused of [true, false]) {
    test(`${pointerType} scrub previews immediately while ${paused ? "paused" : "playing"} and seeks once on release`, async () => {
      const h = await scrubHarness(paused);
      await h.pointer("pointerdown", pointerType);
      for (const value of [2, 280, 120]) {
        await h.preview(value);
        expect(Number(h.input.value)).toBe(value);
        await h.tick(41);
        expect(Number(h.input.value)).toBe(value);
        expect(h.seeks).toEqual([]);
      }
      await h.pointer("pointerup", pointerType);
      expect(h.seeks).toEqual([120]);
      await h.tick(42);
      expect(Number(h.input.value)).toBe(120);
      await act(async () => h.input.dispatchEvent(new Event("change", { bubbles: true })));
      expect(h.seeks).toEqual([120]);
      await h.complete(120);
      await h.tick(121);
      expect(Number(h.input.value)).toBe(121);
      expect(h.video.paused).toBe(paused);
    });
  }
}

test("tap release, repeated seeks and stale completions preserve the latest target", async () => {
  const h = await scrubHarness();
  for (const value of [0, 300, 10, 250]) {
    await h.pointer("pointerdown", "touch");
    await h.preview(value);
    await h.pointer("pointerup", "touch");
    expect(Number(h.input.value)).toBe(value);
  }
  expect(h.seeks).toEqual([0, 300, 10, 250]);
  await h.complete(10); // Completion from an older request.
  await h.tick(11);
  expect(Number(h.input.value)).toBe(250);
  await h.complete(250);
  await h.tick(251);
  expect(Number(h.input.value)).toBe(251);
});

test("release outside commits, unrelated pointers do not and cancellation restores playback display", async () => {
  const h = await scrubHarness();
  await h.pointer("pointerdown");
  await h.preview(100);
  await h.pointer("pointerup", "touch", 2);
  expect(h.seeks).toEqual([]);
  await act(async () => window.dispatchEvent(new PointerEvent("pointerup", { pointerId: 1 })));
  expect(h.seeks).toEqual([100]);
  await h.complete(100);
  await h.pointer("pointerdown");
  await h.preview(200);
  await h.pointer("pointercancel");
  expect(h.seeks).toEqual([100]);
  expect(Number(h.input.value)).toBe(100);
});

test("keyboard adjustments commit and source replacement clears an unfinished preview", async () => {
  const h = await scrubHarness();
  await h.preview(50);
  expect(h.seeks).toEqual([50]);
  await h.pointer("pointerdown");
  await h.preview(200);
  await emit(h.video, "emptied");
  await h.tick(0);
  expect(Number(h.input.value)).toBe(0);
  await h.pointer("pointerup");
  expect(h.seeks).toEqual([50]);
});
const toggle = () => container.querySelector<HTMLButtonElement>("button")!;
async function emit(video: HTMLVideoElement, event: string) {
  await act(async () => video.dispatchEvent(new Event(event)));
}

test("Play uses the live paused state before a queued pause event is delivered", async () => {
  const { video, setPaused } = await mount(false);
  let plays = 0,
    pauses = 0;
  video.play = async () => {
    plays++;
    setPaused(false);
  };
  video.pause = () => {
    pauses++;
    setPaused(true);
  };
  // A gate/browser pause changes the property before its queued event runs.
  setPaused(true);
  await act(async () => toggle().click());
  expect(plays).toBe(1);
  expect(pauses).toBe(0);
});

test("Pause uses live playback even before a queued play event updates React", async () => {
  const { video, setPaused } = await mount();
  let plays = 0,
    pauses = 0;
  video.play = async () => {
    plays++;
  };
  video.pause = () => {
    pauses++;
    setPaused(true);
  };
  setPaused(false);
  await act(async () => toggle().click());
  expect(plays).toBe(0);
  expect(pauses).toBe(1);
});

test("a browser-rejected Play is visible and a successful explicit retry clears it", async () => {
  const { video, setPaused } = await mount();
  video.play = () => Promise.reject(new DOMException("Synthetic denial", "NotAllowedError"));
  await act(async () => toggle().click());
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("blocked");
  expect(toggle().getAttribute("aria-label")).toBe("Play");
  let inClick = false,
    direct = false;
  video.play = async () => {
    direct = inClick;
    setPaused(false);
    video.dispatchEvent(new Event("playing"));
  };
  await act(async () => {
    inClick = true;
    toggle().click();
    inClick = false;
  });
  expect(direct).toBe(true);
  expect(toggle().getAttribute("aria-label")).toBe("Pause");
  expect(container.querySelector('[role="alert"]')).toBeNull();
  video.pause = () => {
    setPaused(true);
    video.dispatchEvent(new Event("pause"));
  };
  await act(async () => toggle().click());
  expect(video.paused).toBe(true);
  expect(toggle().getAttribute("aria-label")).toBe("Play");
});

test("expected buffering interruption is not reported as a playback failure", async () => {
  const { video } = await mount(true, true);
  video.play = () => Promise.reject(new DOMException("Synthetic gate pause", "AbortError"));
  await act(async () => toggle().click());
  expect(container.querySelector('[role="alert"]')).toBeNull();
});

test("an unexpected abort outside a buffer hold does not silently swallow Play", async () => {
  const { video } = await mount();
  video.play = () =>
    Promise.reject(new DOMException("Synthetic unexpected interruption", "AbortError"));
  await act(async () => toggle().click());
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("could not start");
});

test("a rejection from a replaced source cannot appear against the new source", async () => {
  const { video } = await mount();
  let reject!: (error: Error) => void;
  video.play = () =>
    new Promise<void>((_resolve, failure) => {
      reject = failure;
    });
  await act(async () => toggle().click());
  await emit(video, "emptied");
  await act(async () => reject(new Error("Synthetic old-source failure")));
  expect(container.querySelector('[role="alert"]')).toBeNull();
});
