import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { episodeNearCompletion, type TvShow } from "../src/lib/theatre-episodes";
import type { PlaybackState } from "../src/lib/theatre-progress";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { Route } = await import("../src/routes/app.movies.tv-shows.$showId");
const originalFetch = globalThis.fetch;
const originalCanPlay = HTMLVideoElement.prototype.canPlayType;
const originalHref = window.location.href;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
  HTMLVideoElement.prototype.canPlayType = originalCanPlay;
  window.location.href = originalHref;
});
const exampleEpisode = (id: string, number: number) => ({
  id,
  episode_number: number,
  title: `Example ${id}`,
  runtime_minutes: 10,
  artwork_url: null,
});
const show: TvShow = {
  id: "example",
  title: "Example Series",
  poster_url: null,
  seasons: [
    {
      id: "season-one",
      season_number: 1,
      poster_url: null,
      episodes: [exampleEpisode("a", 1), exampleEpisode("b", 3)],
    },
    { id: "season-two", season_number: 2, poster_url: null, episodes: [exampleEpisode("c", 2)] },
  ],
};
const state = (value: PlaybackState["state"], position = 125): PlaybackState => ({
  state: value,
  completed: value === "watched",
  position_seconds: position,
  duration_seconds: 600,
});
function button(text: string) {
  return [...container.querySelectorAll<HTMLButtonElement>("button")].find((item) =>
    item.textContent?.includes(text),
  );
}
async function click(text: string) {
  const item = button(text);
  expect(item).toBeDefined();
  await act(async () => item!.click());
}
async function mount(
  records: Record<string, PlaybackState> = {},
  options: { failProgress?: boolean; failPlayback?: string; deferPlayback?: string } = {},
) {
  const requests: {
    url: string;
    body?: Record<string, unknown>;
    credentials?: RequestCredentials;
  }[] = [];
  let release: ((value: Response) => void) | undefined;
  window.location.href = "http://localhost/app/movies/tv-shows/example";
  HTMLVideoElement.prototype.canPlayType = () => "probably";
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ url, body, credentials: init?.credentials });
    if (url === "/api/tv-shows/example") return Response.json(show);
    if (url === "/api/user-state/tv-episodes")
      return options.failProgress ? new Response(null, { status: 500 }) : Response.json(records);
    if (url.startsWith("/api/user-state/tv-episodes/")) {
      const id = url.split("/").at(-1)!;
      if (init?.method === "PUT") {
        const completed =
          records[id]?.completed ||
          episodeNearCompletion(body.position_seconds, body.duration_seconds, body.completed);
        records[id] = {
          ...body,
          completed,
          state: completed ? "watched" : body.position_seconds >= 30 ? "in_progress" : "unwatched",
        };
      }
      return Response.json(records[id] ?? null);
    }
    if (url.endsWith("/playback")) {
      const id = url.split("/").at(-2);
      if (id === options.failPlayback) return new Response(null, { status: 503 });
      if (id === options.deferPlayback)
        return new Promise<Response>((resolve) => {
          release = resolve;
        });
      return Response.json({
        subtitles: [{ index: id === "a" ? 2 : 7, label: "Example English" }],
      });
    }
    if (url.endsWith("/playback-plan"))
      return Response.json(
        body.capabilities
          ? { url: `/synthetic-stream/${url.split("/").at(-2)}`, source_type: "file" }
          : {
              source: {
                container: "mp4",
                video: { Codec: "h264", Width: 1920, Height: 1080 },
                audio_tracks: [],
              },
            },
      );
    return new Response(null, { status: 404 });
  }) as typeof fetch;
  const parent = createRootRoute();
  const route = Route.update({
    getParentRoute: () => parent,
    path: "/app/movies/tv-shows/$showId",
  } as never);
  const router = createRouter({
    routeTree: parent.addChildren([route]),
    history: createMemoryHistory({ initialEntries: ["/app/movies/tv-shows/example"] }),
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
  return { records, requests, release: () => release!(Response.json({ subtitles: [] })) };
}
async function videoReady() {
  const video = container.querySelector("video")!;
  expect(video).not.toBeNull();
  for (let attempt = 0; !video.getAttribute("src") && attempt < 20; attempt++)
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  Object.defineProperty(video, "duration", { value: 600, configurable: true });
  Object.defineProperty(video, "readyState", { value: 3, configurable: true });
  Object.defineProperty(video, "buffered", {
    get: () => ({ length: 1, start: () => 0, end: () => 600 }),
    configurable: true,
  });
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  return video;
}
async function report(video: HTMLVideoElement, position: number, event = "seeked") {
  await act(async () => {
    video.currentTime = position;
    video.dispatchEvent(new Event(event));
  });
}

test("Continue resumes this viewer, shows exact identity and current selection, and never auto-advances", async () => {
  const { records, requests } = await mount({ a: state("in_progress") });
  await click("Continue — Season 1 · Episode 1");
  const identity = container.querySelector('[aria-label="Current episode"]')!;
  expect(identity.textContent).toContain("Example Series");
  expect(identity.textContent).toContain("Season 1 · Episode 1 — Example a");
  expect(container.querySelector('[aria-current="true"]')?.textContent).toContain(
    "Current episode",
  );
  const video = await videoReady();
  expect(video.currentTime).toBe(125);
  expect(button("Play Next Episode")).toBeUndefined();
  await report(video, 569);
  expect(button("Play Next Episode")).toBeUndefined();
  await report(video, 570);
  expect(container.querySelector('[aria-label="Up next"]')?.textContent).toContain(
    "Season 1 · Episode 3 — Example b",
  );
  expect(button("Play Next Episode")?.className).toContain("min-h-11");
  expect(records.a.state).toBe("watched");
  await report(video, 600, "ended");
  expect(container.querySelector("video")).toBe(video);
  expect(requests.filter((r) => r.url.endsWith("/b/playback"))).toHaveLength(0);
  await click("Play Next Episode");
  expect(container.querySelector('[aria-label="Current episode"]')?.textContent).toContain(
    "Season 1 · Episode 3 — Example b",
  );
  expect(container.querySelector('[aria-pressed="true"]')?.textContent).toContain("Example b");
  expect(button("Play Next Episode")).toBeUndefined();
  const second = await videoReady();
  expect(second.currentTime).toBe(0);
  await report(second, 580);
  await click("Play Next Episode");
  expect(container.querySelector('[aria-label="Current episode"]')?.textContent).toContain(
    "Season 2 · Episode 2 — Example c",
  );
  expect(container.querySelector('[aria-current="true"]')?.textContent).toContain("Example c");
  const final = await videoReady();
  await report(final, 600, "ended");
  expect(button("Play Next Episode")).toBeUndefined();
  expect(button("Continue —")).toBeUndefined();
  expect(requests.filter((r) => r.url.includes("/watched"))).toHaveLength(0);
  expect(requests.every((r) => r.credentials === "include")).toBe(true);
});

test("sticky Watched replay does not offer Next until this playback reaches completion", async () => {
  await mount({ a: state("watched") });
  expect(button("Continue — Season 1 · Episode 3")).toBeDefined();
  await click("1. Example a");
  const video = await videoReady();
  expect(video.currentTime).toBe(0);
  await report(video, 100);
  expect(button("Play Next Episode")).toBeUndefined();
  await report(video, 570);
  expect(button("Play Next Episode")).toBeDefined();
  await report(video, 100);
  expect(button("Play Next Episode")).toBeUndefined();
});

test("separate viewer sessions receive separate Continue targets without copying viewer state", async () => {
  const first = await mount({ a: state("watched"), b: state("in_progress", 200) });
  expect(button("Continue — Season 1 · Episode 3")).toBeDefined();
  await act(async () => root.unmount());
  container.remove();
  const second = await mount({ c: state("in_progress", 300) });
  await click("Continue — Season 2 · Episode 2");
  const video = await videoReady();
  expect(video.currentTime).toBe(300);
  await report(video, 400);
  expect(second.records.c.position_seconds).toBe(400);
  expect(first.records.b.position_seconds).toBe(200);
  expect(first.requests.filter((request) => request.body)).toHaveLength(0);
});

test("quality and matching subtitle preference survive an explicit Next across episode-local track indices", async () => {
  const { requests } = await mount();
  await click("Continue — Season 1 · Episode 1");
  let video = await videoReady();
  await act(async () =>
    container.querySelector<HTMLButtonElement>('[aria-label="Quality"]')!.click(),
  );
  await click("FHD");
  await act(async () =>
    container.querySelector<HTMLButtonElement>('[aria-label="Subtitles"]')!.click(),
  );
  await click("Example English");
  // The existing quality/subtitle switch waits for readiness before reporting progress.
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  await report(video, 580);
  await click("Play Next Episode");
  video = await videoReady();
  expect(container.querySelector('[aria-label="Quality"]')?.textContent).toBe("FHD");
  expect(container.querySelector('[aria-label="Subtitles"]')?.textContent).toContain(
    "Example English",
  );
  const plan = requests
    .filter((r) => r.url.endsWith("/b/playback-plan") && r.body?.capabilities)
    .at(-1)!;
  expect(plan.body?.quality_mode).toBe("FHD");
  expect(plan.body?.subtitle_index).toBe(7);
  expect(video.currentTime).toBe(0);
});

test("failed episode selection remains identifiable and retains the previous player", async () => {
  await mount({}, { failPlayback: "b" });
  await click("Continue — Season 1 · Episode 1");
  const video = await videoReady();
  await click("3. Example b");
  expect(container.querySelector("video")).toBe(video);
  expect(container.querySelector('[aria-pressed="true"]')?.textContent).toContain("Selected");
  expect(container.querySelector('[aria-current="true"]')?.textContent).toContain("Example a");
  expect(container.textContent).toContain("Episode playback is not available.");
});

test("late playback responses cannot replace a newer explicit selection", async () => {
  const { release } = await mount({}, { deferPlayback: "a" });
  await click("1. Example a");
  expect(container.querySelector('[aria-pressed="true"]')?.textContent).toContain(
    "Loading episode",
  );
  await click("3. Example b");
  await act(async () => release());
  expect(container.querySelector('[aria-label="Current episode"]')?.textContent).toContain(
    "Season 1 · Episode 3 — Example b",
  );
});

test("all watched has no Continue; a failed progress load cannot guess a continuation", async () => {
  await mount({ a: state("watched"), b: state("watched"), c: state("watched") });
  expect(button("Continue —")).toBeUndefined();
});
test("failed progress load leaves episode playback accessible without an invented Continue target", async () => {
  await mount({}, { failProgress: true });
  expect(button("Continue —")).toBeUndefined();
  expect(container.textContent).toContain("Playback state could not be loaded.");
  await click("1. Example a");
  expect(container.querySelector('[aria-label="Current episode"]')).not.toBeNull();
});
