import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { Route: Dashboard } = await import("../src/routes/app.music");
const { Route: Albums } = await import("../src/routes/app.music_.albums.index");
const { Route: AlbumDetail } = await import("../src/routes/app.music_.albums.$albumId");
const { Route: Songs } = await import("../src/routes/app.music_.songs.index");
const { Route: Videos } = await import("../src/routes/app.music_.videos");
const { Route: Playlists } = await import("../src/routes/app.music_.playlists");
const { SectionHeading } = await import("../src/components/pv/SectionHeading");
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
const originalFetch = globalThis.fetch;
async function mount(count: number, failMusic = false) {
  const tracks = Array.from({ length: count }, (_, i) => ({
    id: `album-track-${i}`,
    asset_id: `album-track-${i}`,
    album_group_id: `group-${i}`,
    title: `Album track ${i}`,
    album: `Album ${i}`,
    artist: "Synthetic artist",
    album_artist: "Synthetic artist",
    album_folder: `Albums/group-${i}`,
    artwork_url: `/synthetic/album-${i}.jpg`,
    album_position: 1,
    album_member_count: 1,
    album_order_state: "ready",
    enrichment_status: "identified",
    playback_url: `/synthetic/${i}`,
  }));
  const songs = tracks.map((t, i) => ({
    ...t,
    id: `song-${i}`,
    asset_id: `song-${i}`,
    album_group_id: null,
    title: `Solo ${i}`,
  }));
  globalThis.fetch = (async (input) => {
    if (String(input) === "/api/music")
      return failMusic ? Response.json({}, { status: 503 }) : Response.json([...tracks, ...songs]);
    if (String(input).startsWith("/api/music/albums/"))
      return Response.json({
        id: "group-0",
        title: "Album 0",
        artist: "Synthetic artist",
        order_state: "ready",
        tracks: tracks.slice(0, 1),
        can_edit: false,
      });
    if (String(input) === "/api/music-videos")
      return Response.json(
        tracks.map((t, i) => ({
          asset_id: `video-${i}`,
          title: `Video ${i}`,
          artist: "Video artist",
          thumbnail_url: t.artwork_url,
          playback_url: `/synthetic/video-${i}`,
          can_edit: false,
        })),
      );
    throw new Error(`Unexpected request ${input}`);
  }) as typeof fetch;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const parent = createRootRoute();
  const routes = [
    [Dashboard, "/app/music"],
    [Albums, "/app/music/albums"],
    [AlbumDetail, "/app/music/albums/$albumId"],
    [Songs, "/app/music/songs"],
    [Videos, "/app/music/videos"],
    [Playlists, "/app/music/playlists"],
  ] as const;
  const router = createRouter({
    routeTree: parent.addChildren(
      routes.map(([route, path]) => route.update({ getParentRoute: () => parent, path } as never)),
    ),
    history: createMemoryHistory({ initialEntries: ["/app/music"] }),
  });
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
  return router;
}
async function click(label: string) {
  await act(async () => {
    container.querySelector<HTMLAnchorElement>(`a[aria-label="${label}"]`)!.click();
    await new Promise((r) => setTimeout(r, 35));
  });
}
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
});

test("dashboard remains exactly four fixed cards from empty to 1000 albums and songs; previews stay capped", async () => {
  await mount(0);
  const small = [...container.querySelectorAll("[data-music-dashboard-card]")].map(
    (card) => card.className,
  );
  expect(small).toHaveLength(4);
  expect(container.textContent).toContain("0 albums");
  expect(container.textContent).toContain("0 songs");
  expect(container.textContent).toContain("0 videos");
  await act(async () => root.unmount());
  container.remove();
  await mount(1000);
  const cards = [...container.querySelectorAll("[data-music-dashboard-card]")];
  expect(cards.map((card) => card.className)).toEqual(small);
  for (const card of cards) {
    expect(card.className).toContain("h-32");
    expect(card.className).toContain("sm:h-36");
    expect(card.className).toContain("overflow-hidden");
    expect(card.querySelectorAll("[data-music-preview]").length).toBeLessThanOrEqual(4);
  }
  expect(container.querySelector('nav[aria-label="Music subsections"]')?.className).toContain(
    "gap-4",
  );
  expect(container.querySelectorAll("[data-music-preview]")).toHaveLength(12);
  expect(container.querySelector("article, input, select, button, audio, video")).toBeNull();
  expect(container.textContent).toContain("1000 albums");
  expect(container.textContent).toContain("1000 songs");
  expect(container.textContent).toContain("1000 videos");
});

for (const [name, path] of [
  ["Albums", "albums"],
  ["Songs", "songs"],
  ["Music Videos", "videos"],
  ["Playlists", "playlists"],
] as const) {
  test(`${name} launcher opens its real subsection and returns to Music`, async () => {
    const router = await mount(8);
    await click(`Open ${name}`);
    expect(router.state.location.pathname).toBe(`/app/music/${path}`);
    expect(container.querySelector("[data-music-dashboard-card]")).toBeNull();
    const nav = container.querySelector('nav[aria-label="Music navigation"]')!;
    expect(nav.className).toContain("min-h-11");
    expect(nav.className).toContain("flex");
    expect(nav.parentElement?.className).toContain("space-y-5");
    expect(nav.parentElement?.className).toContain("min-w-0");
    expect(nav.parentElement?.firstElementChild).toBe(nav);
    const titleArea = nav.nextElementSibling!;
    const title = titleArea.matches("h1") ? titleArea : titleArea.querySelector("h1,h2");
    expect(title?.textContent).toBe(name);
    expect(nav.querySelector("h1,h2,p")).toBeNull();
    if (path === "albums" || path === "songs") {
      expect(titleArea.tagName).toBe("HEADER");
      expect(titleArea.querySelector("p")?.textContent).toBe(`8 ${path}`);
      expect(container.querySelectorAll(`#music-${path}-heading`)).toHaveLength(1);
      expect(container.querySelector("input")?.placeholder).toBe(
        path === "albums" ? "Search albums or artists" : "Search songs or artists",
      );
      expect(titleArea.querySelector("input,button,select")).toBeNull();
    }
    if (path === "albums") {
      expect(container.querySelectorAll("#music-albums article")).toHaveLength(8);
      expect(container.querySelector("#music-songs")).toBeNull();
    }
    if (path === "songs") {
      expect(container.querySelectorAll("#music-songs article")).toHaveLength(8);
      expect(container.textContent).not.toContain("Album track");
    }
    if (path === "videos") {
      expect(nav.nextElementSibling?.querySelector("h2")?.textContent).toBe("Music Videos");
      expect(container.textContent).toContain("Artist / Band");
      expect(container.querySelector("select")).not.toBeNull();
    }
    if (path === "playlists") {
      expect(container.textContent).toContain("Playlists are coming later.");
      expect(container.querySelector("button,input,select")).toBeNull();
    }
    const back = [...container.querySelectorAll("a")].find(
      (a) => a.textContent === "Back to Music",
    )!;
    await act(async () => {
      back.click();
      await new Promise((r) => setTimeout(r, 35));
    });
    expect(router.state.location.pathname).toBe("/app/music");
    expect(container.querySelectorAll("[data-music-dashboard-card]")).toHaveLength(4);
  });
}

test("failed audio count does not remove launchers or hide available video count", async () => {
  await mount(3, true);
  expect(container.querySelectorAll("[data-music-dashboard-card]")).toHaveLength(4);
  expect(container.textContent).toContain("Currently unavailable");
  expect(container.textContent).toContain("3 videos");
});

test("shared Music header never mislabels the dashboard with song count or LIBRARY", async () => {
  await mount(0);
  await act(async () =>
    root.render(<SectionHeading title="Music" section="Library" pathname="/app/music" />),
  );
  expect(container.textContent).toBe("Music");
  expect(container.querySelector("h1")?.className).not.toContain("hidden");
});

test("Albums card reaches unchanged detail page and Back to Albums returns to the full grid", async () => {
  const router = await mount(8);
  await click("Open Albums");
  await click("Open Album 0");
  expect(router.state.location.pathname).toBe("/app/music/albums/group-0");
  expect(container.querySelector('section[aria-label="Album tracks"]')).not.toBeNull();
  const back = [...container.querySelectorAll("a")].find(
    (a) => a.textContent === "Back to Albums",
  )!;
  await act(async () => {
    back.click();
    await new Promise((r) => setTimeout(r, 35));
  });
  expect(router.state.location.pathname).toBe("/app/music/albums");
  expect(container.querySelectorAll("#music-albums article")).toHaveLength(8);
});
