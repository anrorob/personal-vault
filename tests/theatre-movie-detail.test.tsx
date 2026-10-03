import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { Route } = await import("../src/routes/app.movies.$movieId");
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

async function mount(state: "in_progress" | "watched") {
  window.location.href = "http://localhost/app/movies/example";
  HTMLVideoElement.prototype.canPlayType = () => "probably";
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/user-state/movies")
      return Response.json({
        example: {
          state,
          completed: state === "watched",
          position_seconds: 125,
          duration_seconds: 600,
        },
      });
    if (url.endsWith("/details"))
      return Response.json({
        id: "example",
        title: "Example Movie",
        year: null,
        community_rating: null,
        runtime_minutes: 10,
        genres: [],
        studios: [],
        people: [],
        extras: [],
        trailers: [],
        collections: [],
        subtitles: [],
        audio_codecs: [],
      });
    if (url.endsWith("/playback")) return Response.json({ subtitles: [] });
    if (url.endsWith("/playback-plan")) {
      const body = JSON.parse(String(init?.body));
      return Response.json(
        body.capabilities
          ? {
              url: "/synthetic-authorized-stream",
              source_type: "file",
            }
          : {
              source: {
                container: "mp4",
                video: { Codec: "h264", Width: 1920, Height: 1080 },
                audio_tracks: [],
              },
            },
      );
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;
  const parent = createRootRoute();
  const route = Route.update({
    getParentRoute: () => parent,
    path: "/app/movies/$movieId",
  } as never);
  const router = createRouter({
    routeTree: parent.addChildren([route]),
    history: createMemoryHistory({ initialEntries: ["/app/movies/example"] }),
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
}

test("movie detail uses Continue alone for in-progress presentation and resumes at the saved position", async () => {
  await mount("in_progress");
  expect(container.textContent).not.toContain("In Progress");
  expect(container.querySelector("progress")).toBeNull();
  const resume = [...container.querySelectorAll("button")].find((button) =>
    button.textContent?.includes("Continue at 0:02"),
  )!;
  expect(resume).toBeDefined();
  await act(async () => resume.click());
  const video = document.querySelector("video")!;
  expect(video).not.toBeNull();
  for (let attempt = 0; !video.getAttribute("src") && attempt < 20; attempt++) {
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  }
  expect(video.getAttribute("src")).toBe("/synthetic-authorized-stream");
  Object.defineProperty(video, "duration", { value: 600, configurable: true });
  Object.defineProperty(video, "readyState", { value: 3, configurable: true });
  await act(async () => video.dispatchEvent(new Event("loadedmetadata")));
  expect(video.currentTime).toBe(125);
  expect(document.querySelector('button[aria-label="Quality"]')?.textContent).toBe("Auto");
});

test("movie detail keeps its Watched indicator and manual action", async () => {
  await mount("watched");
  expect(container.textContent).not.toContain("Create franchise");
  expect(container.querySelector('[aria-label="Search movies"]')).toBeNull();
  expect(container.querySelector('[aria-label="Movies actions"]')).toBeNull();
  expect(container.textContent).toContain("Watched");
  expect(container.textContent).toContain("Mark as unwatched");
  expect(container.textContent).not.toContain("Continue at");
});
