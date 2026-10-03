import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { Route: MusicRoute } = await import("../src/routes/app.music_.songs.index");
const song = (id: string, extra = {}) => ({
  id,
  asset_id: id,
  title: id,
  artist: "Test artist",
  album: "Unknown album",
  album_artist: null,
  album_folder: ".",
  album_group_id: null,
  can_edit: true,
  duration_seconds: 65,
  release_year: 2003,
  artwork_url: null,
  lyrics_available: false,
  enrichment_status: "identified",
  playback_url: `/synthetic-stream/${id}`,
  ...extra,
});
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
const originalFetch = globalThis.fetch;
const originalCreate = URL.createObjectURL;
const originalRevoke = URL.revokeObjectURL;
let streams: string[];
let patches: unknown[];
let failSave: boolean;
async function mount(
  tracks = [
    song("solo"),
    song("second"),
    song("member", {
      album_group_id: "album",
      album: "Real album",
      album_position: 1,
      album_member_count: 1,
      album_order_state: "ready",
    }),
  ],
) {
  streams = [];
  patches = [];
  failSave = false;
  URL.createObjectURL = () => "blob:synthetic";
  URL.revokeObjectURL = () => {};
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    if (url === "/api/music-videos") return Response.json([]);
    if (url === "/api/music") return Response.json(tracks);
    if (init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      patches.push(body);
      if (failSave) return Response.json({}, { status: 404 });
      tracks = tracks.map((track) =>
        url.includes(`/${track.asset_id}/`)
          ? {
              ...track,
              title: body.display_title,
              artist: body.artist,
              release_year: body.release_year,
            }
          : track,
      );
      return Response.json({});
    }
    streams.push(url);
    return new Response(new Blob(["synthetic"]));
  }) as typeof fetch;
  const parent = createRootRoute();
  const music = MusicRoute.update({ getParentRoute: () => parent, path: "/app/music" } as never);
  const router = createRouter({
    routeTree: parent.addChildren([music]),
    history: createMemoryHistory({ initialEntries: ["/app/music"] }),
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
}
const button = (label: string) =>
  [...document.querySelectorAll<HTMLButtonElement>("button")].find(
    (b) => b.textContent === label || b.getAttribute("aria-label") === label,
  )!;
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
  URL.createObjectURL = originalCreate;
  URL.revokeObjectURL = originalRevoke;
});

test("Songs subsection contains only explicit non-members and compact metadata", async () => {
  await mount();
  const songs = container.querySelector("#music-songs")!;
  expect(songs.querySelectorAll("article")).toHaveLength(2);
  expect(songs.textContent).not.toContain("member");
  expect(songs.textContent).toContain("2003");
  expect(songs.textContent).toContain("1:05");
  expect(container.textContent).not.toContain("Unknown album");
  expect(container.querySelector("#music-albums")).toBeNull();
  expect(songs.querySelector("a")).toBeNull();
  expect(button("Play solo").className).toContain("min-h-14");
});

test("standalone playback reuses the shared player, shows state and stops after one song", async () => {
  await mount();
  await act(async () => button("Play second").click());
  expect(streams).toEqual(["/synthetic-stream/second"]);
  const audio = container.querySelector("audio")!;
  expect(container.querySelectorAll("audio")).toHaveLength(1);
  expect(container.querySelector('[aria-label="Now playing"]')).not.toBeNull();
  await act(async () => audio.dispatchEvent(new Event("play")));
  expect(button("Pause second")).toBeDefined();
  expect(
    container.querySelector('#music-songs article[aria-current="true"]')?.textContent,
  ).toContain("Playing");
  await act(async () => audio.dispatchEvent(new Event("pause")));
  expect(button("Play second")).toBeDefined();
  await act(async () => {
    audio.dispatchEvent(new Event("ended"));
    await new Promise((resolve) => setTimeout(resolve, 2050));
  });
  expect(streams).toHaveLength(1);
});

test("missing metadata and broken artwork degrade without internal identifiers", async () => {
  await mount([
    song("internal-id", {
      title: "",
      artist: "Unknown artist",
      duration_seconds: null,
      release_year: null,
      artwork_url: "https://synthetic.invalid/artwork",
      can_edit: false,
    }),
  ]);
  const image = container.querySelector("#music-songs img")!;
  await act(async () => image.dispatchEvent(new Event("error")));
  expect(container.querySelector('[aria-label="No song artwork"]')).not.toBeNull();
  expect(button("Play Untitled song")).toBeDefined();
  expect(container.textContent).not.toContain("internal-id");
  expect(container.textContent).not.toContain("Unknown artist");
  expect(container.textContent).not.toContain("NaN");
  expect(button("Edit metadata for Untitled song")).toBeUndefined();
});

test("owner correction uses existing asset API, refreshes values, and has no album assignment", async () => {
  await mount();
  const trigger = button("Edit metadata for solo");
  trigger.focus();
  await act(async () => trigger.click());
  const dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.querySelectorAll("input")).toHaveLength(3);
  expect(dialog.querySelector("select")).toBeNull();
  await act(async () => button("Cancel").click());
  await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
  expect(document.activeElement === trigger).toBe(true);
  expect(patches).toEqual([]);
  await act(async () => trigger.click());
  const title = document.querySelector<HTMLInputElement>("form input")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(
      title,
      "Corrected song",
    );
    title.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () =>
    document
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(patches).toEqual([
    { display_title: "Corrected song", artist: "Test artist", release_year: 2003 },
  ]);
  expect(button("Play Corrected song")).toBeDefined();
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(container.querySelector("#music-albums")).toBeNull();
});

test("failed owner correction retains the draft; non-owner has no edit entry", async () => {
  await mount([song("owner"), song("other", { can_edit: false })]);
  expect(button("Edit metadata for other")).toBeUndefined();
  await act(async () => button("Edit metadata for owner").click());
  failSave = true;
  await act(async () =>
    document
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(document.querySelector('[role="dialog"] [role="alert"]')?.textContent).toContain(
    "could not be saved",
  );
  expect(document.querySelector<HTMLInputElement>("form input")?.value).toBe("owner");
});
