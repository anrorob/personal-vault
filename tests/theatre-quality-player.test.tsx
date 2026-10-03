import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { MoviePlayer } = await import("../src/components/pv/MoviePlayer");
const originalFetch = globalThis.fetch;
const originalCanPlay = HTMLVideoElement.prototype.canPlayType;
const originalRequestFullscreen = HTMLElement.prototype.requestFullscreen;
const fullscreenDescriptor = Object.getOwnPropertyDescriptor(document, "fullscreenElement");
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
  HTMLVideoElement.prototype.canPlayType = originalCanPlay;
  HTMLElement.prototype.requestFullscreen = originalRequestFullscreen;
  if (fullscreenDescriptor)
    Object.defineProperty(document, "fullscreenElement", fullscreenDescriptor);
  else Reflect.deleteProperty(document, "fullscreenElement");
});

for (const section of ["movies", "tv/episodes"]) {
  test(`${section}: mode switch preserves position, tracks and six-second resume`, async () => {
    const requests: Record<string, unknown>[] = [];
    HTMLVideoElement.prototype.canPlayType = () => "probably";
    HTMLElement.prototype.requestFullscreen = async function () {
      Object.defineProperty(document, "fullscreenElement", { value: this, configurable: true });
      document.dispatchEvent(new Event("fullscreenchange"));
    };
    globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
      if (!init?.body) return new Response(null, { status: 404 });
      const body = JSON.parse(String(init.body));
      requests.push(body);
      if (!body.capabilities)
        return Response.json({
          source: {
            container: "mp4",
            video: { Codec: "h264", Width: 1920, Height: 1080, BitRate: 20000000 },
            audio_tracks: [{ Index: 3, Codec: "aac", IsDefault: true }],
          },
        });
      return Response.json({
        url: "/api/example/private-resource",
        source_type: "hls",
        diagnostics: {
          quality: {
            selected_mode: body.quality_mode,
            target_resolution: { width: 1920, height: 1080 },
          },
          playback: { selected_audio_index: 3 },
        },
      });
    }) as typeof fetch;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () =>
      root.render(
        <MoviePlayer
          source="/api/example/master"
          playbackPlanUrl={`http://localhost/api/${section}/example/playback-plan?subtitle_index=2`}
          bufferPlayback
          subtitleTracks={[{ index: 2, label: "Example subtitle" }]}
          selectedSubtitleIndex={2}
          onSubtitleChange={() => {}}
          onPlaybackError={() => {
            throw new Error("Unexpected error");
          }}
        />,
      ),
    );
    const select = container.querySelector<HTMLButtonElement>('button[aria-label="Quality"]')!;
    expect(select.textContent).toBe("Auto");
    expect(select.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector("select")).toBeNull();
    await act(async () => select.click());
    expect(
      [...container.querySelectorAll('[role="menuitemradio"]')].map((o) => o.textContent),
    ).toEqual(["Original", "Auto", "FHD", "720p"]);
    await act(async () => select.click());
    expect(container.textContent).not.toContain("Playback Info");
    expect(container.querySelector("pre")).toBeNull();
    expect(container.querySelector("details")).toBeNull();
    expect(container.querySelector('button[aria-label="Enter fullscreen"]')).not.toBeNull();
    const strip = container.querySelector<HTMLDivElement>(".pv-player-controls")!;
    expect(strip.hasAttribute("aria-hidden")).toBe(true);
    expect(strip.querySelector('button[aria-label="Subtitles"]')).not.toBeNull();
    const video = container.querySelector("video")!;
    let end = 24,
      plays = 0;
    Object.defineProperty(video, "duration", { value: 300, configurable: true });
    Object.defineProperty(video, "readyState", { value: 3, configurable: true });
    Object.defineProperty(video, "seekable", {
      get: () => ({ length: 1, start: () => 0, end: () => 300 }),
    });
    Object.defineProperty(video, "buffered", {
      get: () => ({ length: 1, start: () => 0, end: () => end }),
    });
    video.load = () => {
      video.currentTime = 0;
    };
    video.play = async () => {
      plays++;
      video.dispatchEvent(new Event("playing"));
    };
    await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
    expect(plays).toBe(1);
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[aria-label="Enter fullscreen"]')!.click(),
    );
    expect(document.fullscreenElement?.contains(select)).toBe(true);
    for (const [mode, position] of [
      ["Original", 100],
      ["FHD", 0],
      ["720p", 298],
    ] as const) {
      video.currentTime = position;
      end = position;
      await act(async () => {
        select.click();
      });
      await act(async () => {
        [...container.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')]
          .find((item) => item.textContent === mode)!
          .click();
      });
      const request = requests.filter((r) => r.capabilities).at(-1)!;
      expect(request.quality_mode).toBe(mode);
      expect(request.subtitle_index).toBe(2);
      expect(request.audio_index).toBe(3);

      const before = plays;
      await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
      expect(video.currentTime).toBe(position);
      expect(plays).toBe(before);
      end = Math.min(300, position + 6);
      await act(async () => video.dispatchEvent(new Event("progress")));
      expect(plays).toBe(before + 1);
      expect(video.currentTime).toBe(position);
    }
  });
}
