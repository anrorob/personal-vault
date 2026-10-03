import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { MusicLibrary } = await import("../src/components/MusicLibrary");
const { Route: AlbumRoute } = await import("../src/routes/app.music_.albums.$albumId");
import type { LibraryTrack } from "../src/components/MusicLibrary";

const track = (id: string, group: string | null, position: number | null): LibraryTrack => ({
  id,
  asset_id: id,
  title: id,
  artist: "Test artist",
  album: group ? "Test album" : "Unknown album",
  album_artist: null,
  album_group_id: group,
  album_position: position,
  album_order_state: position == null ? "unresolved" : "ready",
  album_member_count: 2,
  release_year: null,
  artwork_url: null,
  duration_seconds: 65,
  enrichment_status: "identified",
  can_edit: true,
});
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
let albumPlays: string[][];
let songPlays: string[];
const originalFetch = globalThis.fetch;
async function mount(tracks: LibraryTrack[], query = "") {
  albumPlays = [];
  songPlays = [];
  const parent = createRootRoute();
  const music = createRoute({
    getParentRoute: () => parent,
    path: "/app/music",
    component: () => (
      <MusicLibrary
        section="albums"
        tracks={tracks}
        query={query}
        playAlbum={(members) => albumPlays.push(members.map((t) => t.asset_id))}
        playSong={(t) => songPlays.push(t.asset_id)}
        identify={() => {}}
      />
    ),
  });
  const album = AlbumRoute.update({
    getParentRoute: () => parent,
    path: "/app/music/albums/$albumId",
  } as never);
  const router = createRouter({
    routeTree: parent.addChildren([music, album]),
    history: createMemoryHistory({ initialEntries: ["/app/music"] }),
  });
  globalThis.fetch = (async (input) =>
    String(input) === "/api/music-videos"
      ? Response.json([])
      : Response.json({
          id: "g",
          title: "Test album",
          artist: "Test artist",
          order_state: "ready",
          tracks,
          can_edit: false,
          artwork_url: null,
          release_year: null,
        })) as typeof fetch;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
  return router;
}
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
});

test("empty Albums subsection has a bounded empty state", async () => {
  await mount([]);
  expect(container.textContent).toContain("No albums yet.");
  expect(container.querySelector("#music-songs")).toBeNull();
});

test("explicit groups stay separate and standalone songs never become Unknown album", async () => {
  await mount([track("a", "group-one", 1), track("b", "group-two", 1), track("solo", null, null)]);
  expect(container.querySelectorAll("#music-albums article")).toHaveLength(2);
  expect(container.querySelector("#music-songs")).toBeNull();
  expect(container.textContent).not.toContain("Unknown album");
});

test("album Play consumes complete durable order despite search and never navigates", async () => {
  const tracks = [track("Alphabetical first", "g", 2), track("Z last alphabetically", "g", 1)];
  const router = await mount(tracks, "Alphabetical first");
  const play = container.querySelector<HTMLButtonElement>('button[aria-label="Play Test album"]')!;
  expect(play.closest("a")).toBeNull();
  expect(play.className).toContain("h-11");
  expect(play.className).not.toContain("opacity-0");
  await act(async () => play.click());
  expect(albumPlays).toEqual([["Z last alphabetically", "Alphabetical first"]]);
  expect(router.state.location.pathname).toBe("/app/music");
  expect(container.querySelector("#music-albums .pv-card-grid")?.className).toContain(
    "pv-card-grid",
  );
});

test("unresolved or incomplete album cannot start a guessed queue", async () => {
  await mount([track("missing", "g", null), track("known", "g", 2)]);
  const play = container.querySelector<HTMLButtonElement>('button[aria-label="Play Test album"]')!;
  expect(play.disabled).toBe(true);
  expect(play.getAttribute("aria-describedby")).toBe("order-g");
  await act(async () => play.click());
  expect(albumPlays).toEqual([]);
  expect(container.textContent).toContain("Identify album");
});

test("ordered identified album remains identifiable but recipient cannot edit", async () => {
  await mount([track("one", "g", 1), track("two", "g", 2)]);
  expect(container.textContent).toContain("Identify album");
  expect(
    container.querySelector<HTMLButtonElement>('button[aria-label="Play Test album"]')!.disabled,
  ).toBe(false);
});

test("non-owner sees no album identification action", async () => {
  await mount([{ ...track("one", "g", null), can_edit: false }]);
  expect(container.textContent).not.toContain("Identify album");
});

test("album card opens UUID detail route without starting playback", async () => {
  const router = await mount([track("first", "g", 1), track("second", "g", 2)]);
  await act(async () => {
    container.querySelector<HTMLAnchorElement>('a[aria-label="Open Test album"]')!.click();
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
  expect(router.state.location.pathname).toBe("/app/music/albums/g");
  expect(container.querySelector('section[aria-label="Album tracks"]')).not.toBeNull();
  expect(container.querySelector("audio")).toBeNull();
  expect(albumPlays).toEqual([]);
});

test("album-card Play hands all thirteen members to the player in stored order", async () => {
  const members = Array.from({ length: 13 }, (_, i) => ({
    ...track(`Reverse title ${13 - i}`, "g", i + 1),
    album_member_count: 13,
  }));
  const router = await mount([...members].reverse(), "Reverse title 9");
  await act(async () =>
    container.querySelector<HTMLButtonElement>('button[aria-label="Play Test album"]')!.click(),
  );
  expect(albumPlays).toEqual([members.map((member) => member.asset_id)]);
  expect(router.state.location.pathname).toBe("/app/music");
});
