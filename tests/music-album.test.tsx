import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { Route: AlbumRoute } = await import("../src/routes/app.music_.albums.$albumId");

const track = (id: string, position: number | null) => ({
  id,
  asset_id: id,
  title: id,
  artist: "Test artist",
  album: "Test album",
  album_artist: "Test artist",
  album_group_id: "synthetic-album",
  album_position: position,
  album_order_state: position ? "ready" : "unresolved",
  album_member_count: 2,
  track_number: position,
  disc_number: position,
  duration_seconds: null,
  playback_url: `/synthetic-stream/${id}`,
  artwork_url: null,
  release_year: null,
  lyrics_available: false,
});
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
const originalFetch = globalThis.fetch;
const originalCreate = URL.createObjectURL;
const originalRevoke = URL.revokeObjectURL;
let requests: string[];
let patches: unknown[];
async function mount(
  tracks = [track("Alphabetical first", 2), track("Zebra", 1)],
  extra: Record<string, unknown> = {},
) {
  requests = [];
  patches = [];
  URL.createObjectURL = () => "blob:synthetic";
  URL.revokeObjectURL = () => {};
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    requests.push(url);
    if (init?.method === "PATCH") {
      const body = JSON.parse(String(init.body));
      patches.push(body);
      extra = { ...extra, title: body.album_title, artist: body.artist_name };
      tracks = tracks.map((track) => ({
        ...track,
        album: body.album_title,
        artist: body.artist_name,
        album_artist: body.artist_name,
      }));
      return Response.json(body);
    }
    if (url.startsWith("/synthetic-stream/")) return new Response(new Blob(["synthetic"]));
    return Response.json({
      id: "synthetic-album",
      title: "Test album",
      artist: "Test artist",
      order_state: "ready",
      tracks,
      can_edit: true,
      artwork_url: null,
      release_year: null,
      ...extra,
    });
  }) as typeof fetch;
  const parent = createRootRoute();
  const album = AlbumRoute.update({
    getParentRoute: () => parent,
    path: "/app/music/albums/$albumId",
  } as never);
  const library = createRoute({
    getParentRoute: () => parent,
    path: "/app/music",
    component: () => <p>Music</p>,
  });
  const router = createRouter({
    routeTree: parent.addChildren([album, library]),
    history: createMemoryHistory({ initialEntries: ["/app/music/albums/synthetic-album"] }),
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

test("album identity, responsive header and authoritative multi-disc rows have no title fallback", async () => {
  await mount();
  expect(requests[0]).toBe("/api/music/albums/synthetic-album");
  expect(container.querySelector("h2")?.textContent).toBe("Test album");
  expect(container.querySelector('[aria-label="No album artwork"]')).not.toBeNull();
  expect(container.querySelector("header")?.className).toContain("sm:flex-row");
  expect(
    [...container.querySelectorAll('section[aria-label="Album tracks"] button')].map((b) =>
      b.getAttribute("aria-label"),
    ),
  ).toEqual(["Play Zebra", "Play Alphabetical first"]);
  expect(container.textContent).toContain("Disc 1");
  expect(container.textContent).toContain("Disc 2");
  expect(button("Play Zebra").className).toContain("pv-console-row");
  expect(button("Play Zebra").closest("a")).toBeNull();
  expect(container.textContent).not.toContain("synthetic-album");
  expect(container.textContent).not.toContain("NaN");
});

test("album Play starts first stored member and advances in the same queue", async () => {
  await mount();
  await act(async () => button("Play album").click());
  expect(requests.at(-1)).toBe("/synthetic-stream/Zebra");
  const audio = container.querySelector("audio")!;
  await act(async () => audio.dispatchEvent(new Event("play")));
  expect(button("Pause Zebra")).toBeDefined();
  await act(async () => {
    audio.dispatchEvent(new Event("ended"));
    await new Promise((resolve) => setTimeout(resolve, 2050));
  });
  expect(requests.at(-1)).toBe("/synthetic-stream/Alphabetical first");
});

test("per-track Play targets selected track and current state stays accessible", async () => {
  await mount();
  await act(async () => button("Play Alphabetical first").click());
  expect(requests.at(-1)).toBe("/synthetic-stream/Alphabetical first");
  expect(container.querySelector('li[aria-current="true"]')?.textContent).toContain(
    "Alphabetical first",
  );
  expect(container.querySelector('section[aria-label="Now playing"]')).not.toBeNull();
});

test("unresolved membership cannot play an invented album order but can play one track", async () => {
  await mount([track("Second", null), track("First", null)], { order_state: "unresolved" });
  expect(button("Play album").disabled).toBe(true);
  expect(container.textContent).toContain("Album order has not yet been resolved");
  await act(async () => button("Play First").click());
  expect(requests.at(-1)).toBe("/synthetic-stream/First");
  await act(async () => {
    container.querySelector("audio")!.dispatchEvent(new Event("ended"));
    await new Promise((resolve) => setTimeout(resolve, 2050));
  });
  expect(requests.filter((r) => r.startsWith("/synthetic-stream/"))).toHaveLength(1);
});

test("empty album and unavailable metadata remain usable without fabrication", async () => {
  await mount([], { artist: "", can_edit: false, order_state: "unresolved" });
  expect(container.textContent).toContain("This album has no available tracks yet.");
  expect(button("Play album").disabled).toBe(true);
  expect(button("Edit metadata")).toBeUndefined();
  expect(container.querySelector("a")?.textContent).toBe("Back to Albums");
});

test("existing metadata correction is a labelled viewport dialog with cancel and focus return", async () => {
  await mount();
  const trigger = button("Edit metadata");
  trigger.focus();
  await act(async () => trigger.click());
  const dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.textContent).toContain("Edit album metadata");
  expect(dialog.className).toContain("overflow-y-auto");
  expect(dialog.querySelectorAll("label input")).toHaveLength(2);
  await act(async () => button("Cancel").click());
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  expect(document.activeElement === trigger).toBe(true);
  expect(patches).toEqual([]);
  await act(async () => trigger.click());
  await act(async () =>
    document
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(patches).toEqual([{ album_title: "Test album", artist_name: "Test artist" }]);
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});

test("available artwork and year are shown, failed artwork falls back", async () => {
  await mount(undefined, { artwork_url: "https://synthetic.invalid/artwork", release_year: 2001 });
  expect(container.querySelector("header")?.textContent).toContain("2001");
  const image = container.querySelector("img")!;
  expect(image.alt).toBe("Test album artwork");
  await act(async () => image.dispatchEvent(new Event("error")));
  expect(container.querySelector('[aria-label="No album artwork"]')).not.toBeNull();
});

test("saved identity reloads authoritative track metadata without stale artist labels", async () => {
  await mount();
  await act(async () => button("Edit metadata").click());
  const artist = document.querySelectorAll<HTMLInputElement>("form input")[1];
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(
      artist,
      "Corrected artist",
    );
    artist.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () =>
    document
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(patches).toEqual([{ album_title: "Test album", artist_name: "Corrected artist" }]);
  expect(container.querySelector("header")?.textContent).toContain("Corrected artist");
  expect(container.textContent).not.toContain("Test artist");
  expect(requests.filter((url) => url === "/api/music/albums/synthetic-album")).toHaveLength(2);
});

test("13-member album plays every durable position and stops after the final member", async () => {
  const members = Array.from({ length: 13 }, (_, index) => ({
    ...track(`Track ${13 - index}`, index + 1),
    album_member_count: 13,
  }));
  await mount([...members].reverse());
  await act(async () => button("Play album").click());
  for (let index = 0; index < members.length; index++) {
    expect(requests.filter((r) => r.startsWith("/synthetic-stream/"))).toEqual(
      members.slice(0, index + 1).map((item) => item.playback_url),
    );
    expect(container.querySelector('[aria-label="Now playing"]')?.textContent).toContain(
      members[index].title,
    );
    await act(async () => {
      container.querySelector("audio")!.dispatchEvent(new Event("ended"));
      await new Promise((resolve) => setTimeout(resolve, 2050));
    });
  }
  expect(requests.filter((r) => r.startsWith("/synthetic-stream/"))).toHaveLength(13);
  expect(button("Play").disabled).toBe(false);
}, 35000);

test("choosing album member five continues with member six", async () => {
  const members = Array.from({ length: 13 }, (_, index) => ({
    ...track(`Member ${index + 1}`, index + 1),
    album_member_count: 13,
  }));
  await mount([...members].reverse());
  await act(async () => button("Play Member 5").click());
  expect(requests.at(-1)).toBe(members[4].playback_url);
  await act(async () => {
    container.querySelector("audio")!.dispatchEvent(new Event("ended"));
    await new Promise((resolve) => setTimeout(resolve, 2050));
  });
  expect(requests.at(-1)).toBe(members[5].playback_url);
});

test("integrated console retains card and uses one player with transport, timing and seeking", async () => {
  await mount([track("Second", 2), { ...track("First", 1), duration_seconds: 224 }]);
  const panel = container.querySelector<HTMLElement>('[aria-label="Now playing"]')!;
  expect(panel.previousElementSibling?.tagName).toBe("HEADER");
  expect(panel.querySelector('[aria-label="Album tracks"]')).not.toBeNull();
  expect(container.querySelectorAll('[aria-label="Album tracks"]')).toHaveLength(1);
  expect(panel.nextElementSibling).toBeNull();
  expect(panel.textContent).not.toContain("Choose a track");
  expect(panel.querySelector('[role="status"]')?.textContent).toBe("Ready");
  expect(button("Edit metadata")).toBeDefined();
  expect(button("Play album")).toBeDefined();
  expect(panel.className).toBe("pv-music-console");
  expect(panel.querySelector("input")?.parentElement?.className).toBe("pv-console-seek");
  expect(button("Previous track").disabled).toBe(true);
  expect(button("Next track").disabled).toBe(true);
  await act(async () => button("Play album").click());
  expect(panel.textContent).toContain("First");
  expect(panel.textContent).toContain("Test artist");
  expect(panel.textContent).toContain("Test album");
  expect(panel.textContent).toContain("01 / 02");
  expect(container.querySelectorAll('[aria-label="Now playing"]')).toHaveLength(1);
  expect(container.querySelectorAll("audio")).toHaveLength(1);
  const audio = container.querySelector("audio")!;
  Object.defineProperty(audio, "duration", { configurable: true, value: 224 });
  let paused = true;
  Object.defineProperty(audio, "paused", { configurable: true, get: () => paused });
  audio.play = async () => {
    paused = false;
    audio.dispatchEvent(new Event("play"));
  };
  audio.pause = () => {
    paused = true;
    audio.dispatchEvent(new Event("pause"));
  };
  await act(async () => button("Play").click());
  expect(button("Pause")).toBeDefined();
  expect(panel.querySelector(".pv-console-activity")?.getAttribute("data-playing")).toBe("true");
  await act(async () => button("Pause").click());
  expect(panel.querySelector(".pv-console-activity")?.getAttribute("data-playing")).toBe("false");
  expect(button("Play")).toBeDefined();
  await act(async () => {
    audio.dispatchEvent(new Event("loadedmetadata"));
    audio.currentTime = 42;
    audio.dispatchEvent(new Event("timeupdate"));
  });
  expect(panel.querySelector('[aria-label="Playback time"]')?.textContent).toBe("00:42/ 03:44");
  const slider = panel.querySelector<HTMLInputElement>('input[type="range"]')!;
  expect(slider.value).toBe("42");
  expect(slider.disabled).toBe(false);
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(slider, "80");
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    slider.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(audio.currentTime).toBe(80);
  expect(panel.textContent).toContain("01:20");
  await act(async () => button("Next track").click());
  expect(requests.at(-1)).toBe("/synthetic-stream/Second");
  expect(panel.textContent).toContain("02 / 02");
  expect(button("Next track").disabled).toBe(true);
  expect(container.querySelector('li[aria-current="true"] button')?.textContent).toContain(
    "Second",
  );
  await act(async () => button("Previous track").click());
  expect(requests.at(-1)).toBe("/synthetic-stream/First");
  expect(panel.textContent).toContain("01 / 02");
  expect(button("Previous track").disabled).toBe(true);
  await act(async () => button("Stop").click());
  expect(container.querySelector("audio")).toBeNull();
  expect(panel.querySelector('[role="status"]')?.textContent).toBe("Stopped");
  expect(panel.querySelector(".pv-console-activity")?.getAttribute("data-playing")).toBe("false");
  expect(container.querySelector('li[aria-current="true"]')).toBeNull();
});

test("console responsive and activity styles preserve touch targets and reduced motion", async () => {
  const styles = await Bun.file("src/components/MusicAlbumPlayer.css").text();
  expect(styles).toContain("grid-template-columns: minmax(104px, 0.8fr) minmax(0, 2fr)");
  expect(styles).toContain("grid-template-columns: 174px minmax(0, 1fr) auto");
  expect(styles).toContain("min-height: 44px");
  expect(styles).toContain("(pointer: fine)");
  expect(styles).toContain("prefers-reduced-motion: reduce");
  expect(styles).toContain("animation: none");
  expect(styles).toContain('.pv-console-activity[data-playing="true"] span');
  expect(styles).not.toContain('.pv-console-activity[data-playing="false"] span');
  await mount();
  const panel = container.querySelector('[aria-label="Now playing"]')!;
  expect(panel.querySelector(".pv-console-activity")?.getAttribute("data-playing")).toBe("false");
  expect(panel.textContent).not.toContain("kbps");
  expect(panel.textContent).not.toContain("kHz");
  const row = button("Play Zebra");
  expect(row.closest('[aria-label="Now playing"]')).toBe(panel);
  expect(row.querySelector("button")).toBeNull();
  await act(async () =>
    row
      .querySelector(".pv-console-row-title")!
      .dispatchEvent(new MouseEvent("click", { bubbles: true })),
  );
  expect(requests.at(-1)).toBe("/synthetic-stream/Zebra");
  expect(panel.querySelector(".pv-console-track")?.textContent).toBe("Zebra");
});

test("Stop cancels pending album continuation without leaving active feedback", async () => {
  await mount();
  await act(async () => button("Play album").click());
  await act(async () => container.querySelector("audio")!.dispatchEvent(new Event("ended")));
  await act(async () => button("Stop").click());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 2050));
  });
  expect(requests.filter((r) => r.startsWith("/synthetic-stream/"))).toHaveLength(1);
  expect(container.querySelector('[role="status"]')?.textContent).toBe("Stopped");
  expect(button("Play").disabled).toBe(false);
  expect(button("Previous track").disabled).toBe(true);
  expect(button("Next track").disabled).toBe(true);
});
