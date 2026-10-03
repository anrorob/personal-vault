import { afterEach, beforeEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act, useState } = await import("react");
const { createRoot } = await import("react-dom/client");
const { MoviePlayer } = await import("../src/components/pv/MoviePlayer");
const originalFetch = globalThis.fetch;
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
const fullscreenDescriptor = Object.getOwnPropertyDescriptor(document, "fullscreenElement");
const originalExitFullscreen = document.exitFullscreen;
const originalRequestFullscreen = HTMLElement.prototype.requestFullscreen;
const originalShowPopover = HTMLElement.prototype.showPopover;
const originalHidePopover = HTMLElement.prototype.hidePopover;
const enabledDescriptor = Object.getOwnPropertyDescriptor(document, "fullscreenEnabled");
const setFullscreen = (element: Element | null) => {
  Object.defineProperty(document, "fullscreenElement", { value: element, configurable: true });
  document.dispatchEvent(new Event("fullscreenchange"));
};
const idleTimers = new Map<number, () => void>();
let nextTimer = -1;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;

beforeEach(() => {
  Object.defineProperty(document, "fullscreenEnabled", { value: true, configurable: true });
  HTMLElement.prototype.requestFullscreen = async function () {
    setFullscreen(this);
  };
  document.exitFullscreen = async () => setFullscreen(null);
  globalThis.fetch = (async () => new Response(null, { status: 404 })) as typeof fetch;
  globalThis.setTimeout = ((callback: () => void, delay: number, ...args: unknown[]) => {
    if (delay !== 3000) return originalSetTimeout(callback, delay, ...args);
    const id = nextTimer--;
    idleTimers.set(id, callback);
    return id;
  }) as typeof setTimeout;
  globalThis.clearTimeout = ((id: number) => {
    if (!idleTimers.delete(id)) originalClearTimeout(id);
  }) as typeof clearTimeout;
});

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  expect(idleTimers.size).toBe(0);
  container?.remove();
  globalThis.fetch = originalFetch;
  globalThis.setTimeout = originalSetTimeout;
  globalThis.clearTimeout = originalClearTimeout;
  if (fullscreenDescriptor)
    Object.defineProperty(document, "fullscreenElement", fullscreenDescriptor);
  else Reflect.deleteProperty(document, "fullscreenElement");
  document.exitFullscreen = originalExitFullscreen;
  HTMLElement.prototype.requestFullscreen = originalRequestFullscreen;
  HTMLElement.prototype.showPopover = originalShowPopover;
  HTMLElement.prototype.hidePopover = originalHidePopover;
  if (enabledDescriptor) Object.defineProperty(document, "fullscreenEnabled", enabledDescriptor);
  else Reflect.deleteProperty(document, "fullscreenEnabled");
});

async function idle() {
  await act(async () => {
    const callbacks = [...idleTimers.values()];
    idleTimers.clear();
    callbacks.forEach((callback) => callback());
  });
}

async function emit(target: EventTarget, event: string) {
  await act(async () => target.dispatchEvent(new Event(event, { bubbles: true })));
}

async function mount() {
  const selected: (number | null)[] = [];
  function Harness() {
    const [subtitle, setSubtitle] = useState<number | null>(null);
    return (
      <MoviePlayer
        source="/test-authorized-file"
        sourceType="file"
        onPlaybackError={() => {}}
        subtitleTracks={[{ index: 2, label: "Example subtitle" }]}
        selectedSubtitleIndex={subtitle}
        onSubtitleChange={(value) => {
          selected.push(value);
          setSubtitle(value);
        }}
      />
    );
  }
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<Harness />));
  const video = container.querySelector("video")!;
  const player = video.parentElement!;
  const picker = container.querySelector<HTMLButtonElement>('[aria-label="Subtitles"]')!;
  const strip = player.querySelector<HTMLDivElement>(".pv-player-controls")!;
  let paused = true;
  Object.defineProperty(video, "paused", { get: () => paused });
  return {
    video,
    player,
    picker,
    strip,
    selected,
    play: async () => {
      paused = false;
      await emit(video, "playing");
    },
  };
}

async function click(button: HTMLButtonElement) {
  await act(async () => button.click());
}
const button = (label: string) =>
  container.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!;

test("transport and accessories fade and return on mouse, touch and keyboard interaction", async () => {
  const { video, strip, play } = await mount();
  expect(video.controls).toBe(false);
  expect(strip.className).toContain("absolute");
  expect(strip.className).toContain("pointer-events-none");
  expect(video.parentElement?.contains(strip)).toBe(true);
  expect(button("Play")).not.toBeNull();
  await play();
  for (const event of ["pointermove", "pointerdown", "touchstart", "keydown"]) {
    await idle();
    expect(strip.hasAttribute("inert")).toBe(true);
    expect(strip.dataset.visible).toBe("false");
    video.addEventListener(event, (e) => e.stopPropagation());
    await emit(video, event);
    expect(strip.hasAttribute("inert")).toBe(false);
    expect(strip.dataset.visible).toBe("true");
  }
});

test("menu remains usable while open, closes after selection and fades after touch", async () => {
  const { video, picker, strip, selected, play } = await mount();
  await play();
  await idle();
  await emit(video, "touchstart");
  await click(picker);
  await idle();
  expect(strip.hasAttribute("inert")).toBe(false);
  await click(container.querySelector<HTMLButtonElement>('[role="menuitemradio"]:last-child')!);
  expect(selected).toEqual([2]);
  expect(picker.getAttribute("aria-expanded")).toBe("false");
  await idle();
  expect(strip.hasAttribute("inert")).toBe(true);
  await emit(video, "touchstart");
  await click(picker);
  await click(container.querySelector<HTMLButtonElement>('[role="menuitemradio"]')!);
  expect(selected).toEqual([2, null]);
});

test("outside interaction and Escape close the PV menu", async () => {
  const { video, picker } = await mount();
  await click(picker);
  await emit(video, "pointerdown");
  expect(container.querySelector('[role="menu"]')).toBeNull();
  await click(picker);
  await act(async () =>
    picker.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
  );
  expect(container.querySelector('[role="menu"]')).toBeNull();
  expect(document.activeElement).toBe(picker);
});

test("only PV fullscreen is exposed; it retains controls through idle and exit", async () => {
  const { video, player, strip, picker, play } = await mount();
  const source = video.src;
  expect(video.controls).toBe(false);
  expect(container.querySelectorAll('[aria-label="Enter fullscreen"]').length).toBe(1);
  await play();
  await click(button("Enter fullscreen"));
  expect(document.fullscreenElement).toBe(player);
  expect(player.contains(picker)).toBe(true);
  await idle();
  expect(strip.hasAttribute("inert")).toBe(true);
  await emit(video, "touchstart");
  await click(picker);
  expect(container.querySelector('[role="menu"]')).not.toBeNull();
  await emit(video, "pointerdown");
  await click(button("Exit fullscreen"));
  expect(document.fullscreenElement).toBeNull();
  expect(video.src).toBe(source);
  expect(container.querySelector("video")).toBe(video);
});

test("rejected container fullscreen uses viewport mode and Escape preserves playback", async () => {
  HTMLElement.prototype.requestFullscreen = async () => {
    throw new Error("Unavailable");
  };
  let shown = 0,
    hidden = 0;
  HTMLElement.prototype.showPopover = () => {
    shown++;
  };
  HTMLElement.prototype.hidePopover = () => {
    hidden++;
  };
  const { video, player, strip, play } = await mount();
  video.currentTime = 125;
  await play();
  await click(button("Enter fullscreen"));
  expect(shown).toBe(1);
  expect(player.dataset.playerExpanded).toBe("true");
  await idle();
  expect(strip.hasAttribute("inert")).toBe(true);
  await emit(video, "touchstart");
  expect(strip.hasAttribute("inert")).toBe(false);
  let dialogEscapes = 0;
  const dialogEscape = () => dialogEscapes++;
  document.addEventListener("keydown", dialogEscape, true);
  await act(async () =>
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
    ),
  );
  document.removeEventListener("keydown", dialogEscape, true);
  expect(dialogEscapes).toBe(0);
  expect(hidden).toBe(1);
  expect(player.hasAttribute("popover")).toBe(false);
  expect(video.currentTime).toBe(125);
});

test("unsupported fullscreen offers no dead or native video-only action", async () => {
  Object.defineProperty(document, "fullscreenEnabled", { value: false, configurable: true });
  HTMLElement.prototype.showPopover = undefined!;
  const { video } = await mount();
  expect(button("Enter fullscreen")).toBeNull();
  expect(video.controls).toBe(false);
});

test("fullscreen failure is reported without disturbing playback", async () => {
  HTMLElement.prototype.requestFullscreen = async () => {
    throw new Error("Denied");
  };
  HTMLElement.prototype.showPopover = undefined!;
  await mount();
  await click(button("Enter fullscreen"));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(
    "Fullscreen could not open",
  );
});

test("stationary mouse does not pin controls; keyboard focus does", async () => {
  const { player, strip, play } = await mount();
  await play();
  await emit(strip, "pointerenter");
  await idle();
  expect(strip.hasAttribute("inert")).toBe(true);
  await emit(player, "keydown");
  await act(async () => button("Pause").focus());
  await idle();
  expect(strip.hasAttribute("inert")).toBe(false);
});

test("Play, Pause, mute and timeline operate the existing media element", async () => {
  const { video } = await mount();
  let plays = 0,
    pauses = 0;
  video.play = async () => {
    plays++;
    await emit(video, "play");
  };
  video.pause = () => {
    pauses++;
  };
  await click(button("Play"));
  expect(plays).toBe(1);
  await click(button("Mute"));
  expect(video.muted).toBe(true);
  Object.defineProperty(video, "duration", { value: 300 });
  await emit(video, "durationchange");
  const seek = container.querySelector<HTMLInputElement>('[aria-label="Playback position"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(seek, "120");
    seek.dispatchEvent(new Event("input", { bubbles: true }));
    seek.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(video.currentTime).toBe(120);
  expect(pauses).toBe(0);
});

test("a stationary scrub remains usable through idle and fades again after release", async () => {
  const { video, strip, play } = await mount();
  Object.defineProperty(video, "duration", { value: 300 });
  await emit(video, "durationchange");
  await play();
  const seek = container.querySelector<HTMLInputElement>("input")!;
  await act(async () =>
    seek.dispatchEvent(
      new PointerEvent("pointerdown", {
        pointerId: 1,
        button: 0,
        bubbles: true,
      }),
    ),
  );
  await idle();
  expect(strip.hasAttribute("inert")).toBe(false);
  await act(async () => window.dispatchEvent(new PointerEvent("pointerup", { pointerId: 1 })));
  await idle();
  expect(strip.hasAttribute("inert")).toBe(true);
});
