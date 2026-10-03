import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { AppShell } = await import("../src/components/pv/AppShell");

const { Route: VideosRoute } = await import("../src/routes/app.personal-videos");
const originalFetch = globalThis.fetch;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
let urls: string[];
async function setup(override?: (url: string, options?: RequestInit) => Response | undefined) {
  urls = [];
  globalThis.fetch = (async (input, options) => {
    const url = String(input);
    urls.push(url);
    const custom = override?.(url, options);
    if (custom) return custom;
    if (url === "/api/section-counts")
      return Response.json({ gallery: 1000, music: 500, home_videos: 20, movies: 200 });
    if (url === "/api/auth/session")
      return Response.json({
        authenticated: true,
        username: "synthetic",
        display_name: "Synthetic",
        role: "user",
      });
    if (url === "/api/user-state/gallery")
      return Response.json({ sort: "newest", anchor_id: null, anchor_offset: 0 });
    if (url.endsWith("/backfill/latest")) return Response.json({ run: null });
    if (url.includes("/api/personal-videos?"))
      return Response.json(
        ["one", "two"].map((id) => ({
          id,
          asset_id: id,
          name: id + ".mp4",
          display_title: id,
          size: 100,
          media_type: "video/mp4",
          can_edit: true,
        })),
      );
    if (url === "/api/personal-videos/ken/backfill")
      return Response.json({
        pending: 0,
        queued: 0,
        processing: 0,
        completed: 0,
        failed: 0,
        skipped: 0,
      });
    if (url.includes("filter-options"))
      return Response.json({
        people: [{ id: "person", display_name: "Synthetic person" }],
        private_tags: [{ id: "unused", display_name: "Unused" }],
        content_tags: [{ slug: "beach", display_name: "Beach" }],
        locations: ["Gdansk"],
      });
    if (url === "/api/auth/hidden-videos/authorization") return Response.json({ authorized: true });
    if (url.endsWith("shared-preference")) return Response.json({ include_shared_photos: false });
    return Response.json([]);
  }) as typeof fetch;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
}
async function flush() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 30));
  });
}
async function click(text: string) {
  const el = [...document.querySelectorAll<HTMLButtonElement>("button")].find(
    (b) => b.textContent?.trim() === text,
  );
  expect(el).toBeDefined();
  await act(async () => el!.click());
  await flush();
}
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
});

async function mountVideos() {
  const parent = createRootRoute({ component: AppShell });
  const route = VideosRoute.update({
    getParentRoute: () => parent,
    path: "/app/personal-videos",
  } as never);
  const router = createRouter({
    routeTree: parent.addChildren([route]),
    history: createMemoryHistory({ initialEntries: ["/app/personal-videos"] }),
  });
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
  await flush();
  return router;
}

const tiles = () => [...container.querySelectorAll<HTMLButtonElement>("button[aria-pressed]")];
test("Home Videos keeps real shell, sticky controls, separate mobile actions and Sort", async () => {
  await setup();
  await mountVideos();
  expect(container.querySelector('[aria-label="Toggle navigation"]')).not.toBeNull();
  expect(container.querySelector("[data-gallery-controls]")?.className).toContain("sticky top-16");
  await click("Home Videos actions");
  const menu = container.querySelector("#home-videos-context-actions")!;
  for (const name of [
    "Filter",
    "Shared videos",
    "Select",
    "View Hidden",
    "Analyse existing videos",
  ])
    expect(menu.textContent).toContain(name);
  expect(menu.querySelector("select")).toBeNull();
  expect(container.querySelector("[data-gallery-sort] select")).not.toBeNull();
  expect(container.querySelector(".pv-card-grid")?.className).toBe(
    "pv-card-grid pv-card-grid--wide",
  );
});
test("whole cards toggle multi-selection without playback or scroll reset; successful hide exits", async () => {
  await setup((url) => (url.endsWith("/lifecycle/hide") ? Response.json({}) : undefined));
  await mountVideos();
  window.scrollTo(0, 1500);
  await click("Select");
  expect(window.scrollY).toBe(1500);
  await act(async () => tiles()[0].click());
  await act(async () => tiles()[1].click());
  expect(container.textContent).toContain("2 selected");
  await act(async () => tiles()[0].click());
  expect(container.textContent).toContain("1 selected");
  expect(urls.some((u) => u.endsWith("/playback") || u.endsWith("/details"))).toBe(false);
  await click("Hide selected");
  expect(tiles()).toHaveLength(0);
  expect(container.querySelectorAll(".pv-panel-hover")).toHaveLength(1);
  expect(window.scrollY).toBe(1500);
});
test("failed hide retains cards and selection; partial success removes only successful cards", async () => {
  await setup((url) =>
    url.endsWith("/one/lifecycle/hide")
      ? Response.json({})
      : url.endsWith("/two/lifecycle/hide")
        ? Response.json(
            { detail: "This video is shared. Unshare it before hiding." },
            { status: 409 },
          )
        : undefined,
  );
  await mountVideos();
  await click("Select");
  await act(async () => {
    tiles()[0].click();
    tiles()[1].click();
  });
  await click("Hide selected");
  expect(tiles()).toHaveLength(1);
  expect(container.textContent).toContain("1 selected");
  expect(container.textContent).toContain("This video is shared. Unshare it before hiding.");
});
test("Filter uses viewport dialog and private options without photo taxonomy", async () => {
  await setup();
  await mountVideos();
  window.scrollTo(0, 900);
  await click("Filter");
  const dialog = document.querySelector('[role="dialog"]')!;
  for (const label of ["People", "My private tags", "Unused", "Location"])
    expect(dialog.textContent).toContain(label);
  expect(dialog.textContent).not.toContain("Photo type");
  expect(dialog.textContent).not.toContain("Content tags");
  expect(dialog.className).toContain("overflow-y-auto");
  const tag = [...dialog.querySelectorAll("select")].find((s) =>
    s.textContent?.includes("Unused"),
  )!;
  await act(async () => {
    tag.value = "unused";
    tag.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await click("Apply filters");
  expect(urls.some((u) => u.includes("private_tag=unused"))).toBe(true);
  expect(window.scrollY).toBe(900);
});
test("hidden scope exits and re-enters through session authorization; bulk KEN has no per-video prompt", async () => {
  await setup((url) =>
    url.includes("/api/personal-videos?") && url.includes("include_hidden=true")
      ? Response.json([])
      : undefined,
  );
  await mountVideos();
  await click("View Hidden");
  expect(container.querySelectorAll(".pv-panel-hover")).toHaveLength(0);
  await click("Leave Hidden Videos");
  expect(container.querySelectorAll(".pv-panel-hover")).toHaveLength(2);
  await click("View Hidden");
  expect(urls.filter((u) => u === "/api/auth/hidden-videos/authorization")).toHaveLength(2);
  expect(urls.some((u) => u.endsWith("/authorization/options"))).toBe(false);
  await click("Analyse existing videos");
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});

test("KEN summary separates pending, processing, completed and failed with bulk outcomes", async () => {
  await setup((url, options) =>
    url === "/api/personal-videos/ken/backfill"
      ? Response.json({
          pending: 2,
          queued: 3,
          processing: 1,
          completed: 7,
          failed: 1,
          skipped: 0,
          ...(options?.method === "POST"
            ? { queued_new: 2, skipped_active: 4, already_current: 7 }
            : {}),
        })
      : undefined,
  );
  await mountVideos();
  expect(container.textContent).toContain("5 pending · 1 processing · 7 completed · 1 failed");
  await click("Analyse existing videos");
  expect(container.textContent).toContain("2 newly queued");
  expect(container.textContent).toContain("4 already active");
  expect(container.textContent).toContain("7 already current");
  expect(container.textContent).toContain("KEN details for the failure reason");
});

for (const criteria of [
  {
    person: "person",
    content_tag: "beach",
    private_tag: "unused",
    date_from: "2024-01-01",
    date_to: "2025-01-01",
    location: "Gdansk",
  },
  { person: "person" },
  {},
]) {
  test(`Clear filters resets form and applied results while preserving sort: ${Object.keys(criteria).length} criteria`, async () => {
    const keys = ["person", "content_tag", "private_tag", "date_from", "date_to", "location"];
    await setup((url) => {
      if (!url.startsWith("/api/personal-videos?")) return;
      const query = new URL(url, "http://synthetic.test").searchParams;
      return keys.some((key) => query.has(key)) ? Response.json([]) : undefined;
    });
    await mountVideos();
    const sort = container.querySelector<HTMLSelectElement>("[data-gallery-sort] select")!;
    await act(async () => {
      sort.value = "oldest";
      sort.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();
    await click("Filter");
    const fields = () => [
      ...document.querySelectorAll<HTMLInputElement | HTMLSelectElement>(
        '[role="dialog"] select, [role="dialog"] input',
      ),
    ];
    for (const [key, value] of Object.entries(criteria)) {
      const field = fields()[keys.indexOf(key)];
      await act(async () => {
        // Use the native setter so React observes input changes as browser events.
        const prototype =
          field instanceof HTMLSelectElement
            ? HTMLSelectElement.prototype
            : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(field, value);
        field.dispatchEvent(
          new Event(field instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }),
        );
        field.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }
    await click("Apply filters");
    const lastQuery = () =>
      new URL(
        urls.filter((u) => u.startsWith("/api/personal-videos?")).at(-1)!,
        "http://synthetic.test",
      ).searchParams;
    for (const [key, value] of Object.entries(criteria)) expect(lastQuery().get(key)).toBe(value);
    expect(container.querySelectorAll(".pv-panel-hover")).toHaveLength(
      Object.keys(criteria).length ? 0 : 2,
    );
    await click("Filter");
    await click("Clear filters");
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(fields().map((field) => field.value)).toEqual(["", "", "", "", "", ""]);
    for (const key of keys) expect(lastQuery().has(key)).toBe(false);
    expect(lastQuery().get("sort")).toBe("oldest");
    expect(lastQuery().get("include_hidden")).toBe("false");
    expect(lastQuery().get("include_shared")).toBe("true");
    expect(sort.value).toBe("oldest");
    expect(container.querySelectorAll(".pv-panel-hover")).toHaveLength(2);
    await click("Clear filters");
    expect(fields().map((field) => field.value)).toEqual(["", "", "", "", "", ""]);
    expect(container.querySelectorAll(".pv-panel-hover")).toHaveLength(2);
  });
}

test("real Home Video details keep date editing local, propagate save, and separate private tags", async () => {
  let date: string | null = null;
  let privateTags: { id: string; display_name: string }[] = [];
  const writes: { url: string; body: unknown }[] = [];
  await setup((url, options) => {
    if (url.endsWith("/ken/engine"))
      return Response.json({
        display_name: "Synthetic KEN",
        supported_input_modes: [],
        status: "ready",
      });
    if (url.endsWith("/metadata") && options?.method === "PATCH") {
      const body = JSON.parse(String(options.body));
      writes.push({ url, body });
      date = body.captured_on;
      return Response.json({ captured_on: date });
    }
    if (url.endsWith("/details"))
      return Response.json({
        file_id: "one",
        asset_id: "one",
        name: "one.mp4",
        display_title: "one",
        analysis: null,
        narrative: "Synthetic description",
        narrative_source: "none",
        people: [],
        content_tags: [],
        warnings: [],
        captured_on: date,
      });
    if (url.endsWith("/playback"))
      return Response.json({ status: "direct", playback_url: "/synthetic.mp4" });
    if (url === "/api/gallery/custom-tags") return Response.json(privateTags);
    if (url.endsWith("/private-tags")) {
      if (options?.method === "POST") {
        const body = JSON.parse(String(options.body));
        writes.push({ url, body });
        privateTags = [{ id: "private-a", display_name: body.display_name }];
        return Response.json(privateTags[0]);
      }
      return Response.json(privateTags);
    }
    if (url.endsWith("/intelligence/terms"))
      return Response.json([{ namespace: "content_tag", slug: "beach", display_name: "Beach" }]);
    if (url.startsWith("/api/personal-videos?"))
      return Response.json([
        {
          id: "one",
          asset_id: "one",
          name: "one.mp4",
          display_title: "one",
          size: 100,
          can_edit: true,
          captured_on: date,
        },
      ]);
    return undefined;
  });
  await mountVideos();
  await click("one");
  const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
  dialog.scrollTop = 400;
  const capture = () => dialog.querySelector<HTMLElement>('[aria-label="Capture date"]')!;
  expect(capture().textContent).toContain("Date not recorded");
  await click("Add date");
  expect(capture().querySelector('input[type="date"]')).not.toBeNull();
  const setDate = async (value: string) => {
    const field = capture().querySelector<HTMLInputElement>('input[type="date"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };
  await setDate("2020-04-05");
  await click("Cancel");
  expect(writes).toHaveLength(0);
  expect(capture().textContent).toContain("Date not recorded");
  expect(dialog.scrollTop).toBe(400);
  await click("Add date");
  await setDate("2020-04-05");
  await click("Save date");
  expect(writes[0]).toEqual({
    url: "/api/vault-master/assets/one/metadata",
    body: { captured_on: "2020-04-05" },
  });
  expect(capture().textContent).toContain("2020-04-05");
  expect(capture().querySelector("input")).toBeNull();
  expect(container.querySelector(".pv-panel-hover time")).toBeNull();
  expect(container.querySelector(".pv-panel-hover")?.textContent).toBe("one");
  expect(container.querySelector(".pv-panel-hover")?.textContent).not.toContain("2020-04-05");
  expect(dialog.scrollTop).toBe(400);
  expect(document.activeElement?.textContent).toBe("Edit date");
  await click("Edit date");
  await act(async () =>
    capture()
      .querySelector("input")!
      .dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
      ),
  );
  expect(capture().querySelector("input")).toBeNull();
  expect(document.querySelector('[role="dialog"]')).toBe(dialog);
  expect(dialog.scrollTop).toBe(400);
  expect(urls.filter((url) => url.includes("/playback"))).toHaveLength(1);
  await click("Add tag");
  expect(dialog.textContent).not.toContain("Create tag");
  expect(dialog.querySelector('[aria-label="Add content tag"]')?.textContent).toContain("Beach");
  const input = dialog.querySelector<HTMLInputElement>('[aria-label="New private tag"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(
      input,
      "Private trip",
    );
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await click("Create private tag");
  expect(writes[1]).toEqual({
    url: "/api/personal-videos/assets/one/private-tags",
    body: { display_name: "Private trip" },
  });
  expect(dialog.querySelector('[aria-label="My private tags"]')?.textContent).toContain(
    "Private trip",
  );
  expect(dialog.querySelector('[aria-label="Add content tag"]')?.textContent).not.toContain(
    "Private trip",
  );
});

test("shared video allows only recipient private annotations, with no owner date or KEN editor", async () => {
  await setup((url) => {
    if (url.startsWith("/api/personal-videos?"))
      return Response.json([
        {
          id: "shared",
          asset_id: "shared",
          name: "shared.mp4",
          display_title: "Shared recording",
          size: 100,
          can_edit: false,
        },
      ]);
    if (url.endsWith("/playback"))
      return Response.json({ status: "direct", playback_url: "/synthetic.mp4" });
    if (url.endsWith("/details")) return Response.json({ detail: "Not found" }, { status: 404 });
    return undefined;
  });
  await mountVideos();
  await click("Shared recording");
  const dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.querySelector('[aria-label="My private tags"]')).not.toBeNull();
  expect(dialog.querySelector('[aria-label="Capture date"]')).toBeNull();
  expect(dialog.textContent).not.toContain("Edit description");
  expect(urls.some((url) => url.includes("/ken/assets/shared"))).toBe(false);
});
