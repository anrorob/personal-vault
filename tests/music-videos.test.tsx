import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { MusicVideos } = await import("../src/components/MusicVideos");
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
const originalFetch = globalThis.fetch;
let requests: string[];
let corrections: unknown[];
let stopped: number;
const fixture = (id: string, artist: string | null, can_edit = true) => ({
  asset_id: id,
  title: id,
  artist,
  can_edit,
  playback_url: `/api/music-videos/${id}/playback`,
});
async function mount(
  items = [
    fixture("First", "Band A"),
    fixture("Second", "Band B", false),
    fixture("Missing", null),
  ],
) {
  requests = [];
  corrections = [];
  stopped = 0;
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    requests.push(url);
    if (url === "/api/music-videos") return Response.json(items);
    if (url.endsWith("/favorite")) {
      const favorite = JSON.parse(String(init?.body)).favorite;
      items = items.map((item) =>
        url.includes(`/${item.asset_id}/`) ? { ...item, favorite } : item,
      );
      return Response.json({ favorite });
    }
    if (url.endsWith("/metadata")) {
      corrections.push(JSON.parse(String(init?.body)));
      return Response.json({});
    }
    if (url.endsWith("/playback"))
      return Response.json({ status: "direct", playback_url: "/synthetic-video-content" });
    return new Response(null, { status: 404 });
  }) as typeof fetch;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<MusicVideos beforePlay={() => stopped++} />));
}
const button = (name: string) =>
  [...document.querySelectorAll<HTMLButtonElement>('button, [role="menuitem"]')].find(
    (element) => element.textContent === name || element.getAttribute("aria-label") === name,
  )!;
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
});

test("individual cards show only title and artist, filter uses effective values and All resets", async () => {
  await mount();
  expect(container.querySelectorAll("article")).toHaveLength(3);
  expect(container.textContent).not.toMatch(/KEN|Album|Genre|Description|Lyrics/);
  const filter = container.querySelector("select")!;
  await act(async () => {
    filter.value = "artist:Band A";
    filter.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(container.querySelectorAll("article")).toHaveLength(1);
  expect(container.querySelector("article")?.textContent).toContain("First");
  await act(async () => {
    filter.value = "missing";
    filter.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(container.querySelector("article")?.textContent).toContain("Missing");
  await act(async () => {
    filter.value = "all";
    filter.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(container.querySelectorAll("article")).toHaveLength(3);
});

test("card opens viewport player directly through authorised source and stops audio", async () => {
  await mount();
  const trigger = button("Play First");
  await act(async () => trigger.click());
  expect(stopped).toBe(1);
  expect(requests).toContain("/api/music-videos/First/playback");
  expect(document.querySelector('[role="dialog"] video')?.getAttribute("src")).toBe(
    "/synthetic-video-content",
  );
  expect(document.querySelector('[role="dialog"] video')?.hasAttribute("autoplay")).toBe(true);
  expect(container.querySelectorAll('a[href*="detail"]')).toHaveLength(0);
});

test("owner editor is a two-field viewport dialog; cancel and save use existing authority", async () => {
  await mount();
  expect(container.querySelectorAll("article:nth-child(2) button")).toHaveLength(2);
  await openMenu();
  const trigger = button("Edit metadata");
  await act(async () => trigger.focus());
  await act(async () => trigger.click());
  expect(document.querySelectorAll('[role="dialog"] input')).toHaveLength(2);
  await act(async () => button("Cancel").click());
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(corrections).toHaveLength(0);
  await openMenu();
  await act(async () => button("Edit metadata").click());
  await act(async () =>
    document
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(corrections).toEqual([{ artist: "Band A", title: "First" }]);
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});

test("empty library has filter and clean empty state", async () => {
  await mount([]);
  expect(container.textContent).toContain("No music videos yet.");
  expect(container.querySelector("select")).not.toBeNull();
  expect(container.textContent).not.toContain("coming later");
});

async function openMenu() {
  await act(async () =>
    button("Options for First").dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
    ),
  );
}

test("restrained menu does not play; Favorites is first and updates immediately", async () => {
  await mount();
  expect([...container.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
    "Favorites",
    "All",
    "Not set",
    "Band A",
    "Band B",
  ]);
  expect(button("Edit metadata")).toBeUndefined();
  expect(button("Options for First").closest('button[aria-label="Play First"]')).toBeNull();
  await openMenu();
  expect(requests.some((url) => url.endsWith("/playback"))).toBe(false);
  expect(document.querySelectorAll('[role="menuitem"]')).toHaveLength(2);
  await act(async () => button("Add to Favorites").click());
  const filter = container.querySelector("select")!;
  await act(async () => {
    filter.value = "favorites";
    filter.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(container.querySelectorAll("article")).toHaveLength(1);
  await openMenu();
  await act(async () => button("Remove from Favorites").click());
  expect(container.querySelectorAll("article")).toHaveLength(0);
  expect(container.textContent).toContain("No favorite music videos yet.");
  expect(stopped).toBe(0);
});

test("artists are dynamic, deduplicated and alphabetically follow fixed options", async () => {
  const items = [fixture("First", "Zulu"), fixture("Second", "Alpha"), fixture("Third", "Zulu")];
  await mount(items);
  const values = () =>
    [...container.querySelectorAll("option")].map((option) => option.textContent);
  expect(values()).toEqual(["Favorites", "All", "Not set", "Alpha", "Zulu"]);
  // Simulate the authoritative list changing after metadata correction/reload.
  items[0].artist = "Beta";
  await openMenu();
  await act(async () => button("Edit metadata").click());
  await act(async () =>
    document
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(values()).toEqual(["Favorites", "All", "Not set", "Alpha", "Beta", "Zulu"]);
});
