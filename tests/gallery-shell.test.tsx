import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRoute, createRouter, createMemoryHistory, RouterProvider, Link } =
  await import("@tanstack/react-router");
const { AppShell } = await import("../src/components/pv/AppShell");
const { SectionHeading } = await import("../src/components/pv/SectionHeading");
const { Route: GalleryRoute } = await import("../src/routes/app.gallery.index");
const { Route: GalleryLayoutRoute } = await import("../src/routes/app.gallery");
const { clearGalleryPosition, rememberGalleryPosition, visitGalleryPath } =
  await import("../src/lib/gallery");
const originalFetch = globalThis.fetch;
const originalIntersectionObserver = globalThis.IntersectionObserver;
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
    if (url.includes("/api/gallery/pages?"))
      return Response.json({
        items: [
          {
            id: "synthetic",
            asset_id: "asset",
            name: "test.jpg",
            media_type: "image/jpeg",
            size: 10,
            captured_on: "2024-01-01",
            thumbnail_url: "/synthetic.jpg",
            can_edit: true,
          },
        ],
        next_cursor: null,
      });
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
  globalThis.IntersectionObserver = originalIntersectionObserver;
  clearGalleryPosition();
});

async function mountGallery(initialEntry = "/app/gallery/") {
  const parent = createRootRoute({ component: AppShell });
  const route = GalleryRoute.update({
    getParentRoute: () => parent,
    path: "/app/gallery/",
  } as never);
  const router = createRouter({
    routeTree: parent.addChildren([route]),
    history: createMemoryHistory({ initialEntries: [initialEntry] }),
  });
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
  await flush();
  return router;
}

test("cards display the API effective source date without requiring EXIF", async () => {
  await setup((url) =>
    url.includes("/api/gallery/pages?")
      ? Response.json({
          items: [
            {
              id: "synthetic",
              asset_id: "asset",
              name: "fixture.jpg",
              media_type: "image/jpeg",
              size: 10,
              captured_on: "2018-06-12",
              date_source: "source_file_created",
              thumbnail_url: "/synthetic.jpg",
            },
          ],
          next_cursor: null,
        })
      : undefined,
  );
  await mountGallery();
  expect(container.textContent).toContain("12 Jun 2018");
  expect(container.textContent).not.toContain("Date not recorded");
});

test("a deep detail return seeks the anchor directly and restores its viewport offset", async () => {
  const items = Array.from({ length: 60 }, (_, index) => ({
    id: `synthetic-return-${index}`,
    asset_id: `synthetic-asset-${index}`,
    name: `Synthetic-${index}.jpg`,
    media_type: "image/jpeg",
    size: 8,
    captured_on: "2018-03-01",
    thumbnail_url: "/synthetic.jpg",
  }));
  visitGalleryPath("/app/gallery/");
  rememberGalleryPosition({
    assetId: items[12].asset_id,
    photoId: items[12].id,
    query: "sort=newest&photo_type=camera",
    offset: 143,
    hidden: false,
  });
  visitGalleryPath("/app/gallery/synthetic-return-12");
  visitGalleryPath("/app/gallery/");
  const width = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
  const rect = HTMLElement.prototype.getBoundingClientRect;
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get: () => 1000,
  });
  HTMLElement.prototype.getBoundingClientRect = function () {
    const columns = window.innerWidth >= 1280 ? 4 : window.innerWidth >= 768 ? 3 : 2;
    const height = (1000 - (columns - 1) * 16) / columns + 46;
    const row = this.dataset.galleryIndex
      ? Math.floor(Number(this.dataset.galleryIndex) / columns)
      : 0;
    return new DOMRect(0, 200 + row * (height + 16) - window.scrollY, 1000, height);
  };
  try {
    await setup((url) =>
      url.startsWith("/api/gallery/pages?")
        ? Response.json({ items, next_cursor: null })
        : undefined,
    );
    await mountGallery("/app/gallery/?photo_type=camera");
    await flush();
    await flush();
    await flush();
    const requests = urls.filter((url) => url.startsWith("/api/gallery/pages?"));
    expect(requests.length).toBe(1);
    expect(requests[0]).toContain("anchor_asset_id=synthetic-asset-12");
    expect(requests[0]).toContain("photo_type=camera");
    const anchor = container.querySelector<HTMLElement>('[data-gallery-id="synthetic-return-12"]');
    expect(anchor).not.toBeNull();
    expect(Math.abs(anchor!.getBoundingClientRect().top - 143)).toBeLessThan(2);
    expect(container.querySelectorAll("[data-gallery-id]").length).toBeLessThanOrEqual(60);
  } finally {
    HTMLElement.prototype.getBoundingClientRect = rect;
    if (width) Object.defineProperty(HTMLElement.prototype, "clientWidth", width);
    else Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
  }
});

test("changed query and fresh Gallery navigation ignore stale detail anchors", async () => {
  visitGalleryPath("/app/gallery/");
  rememberGalleryPosition({
    assetId: "synthetic",
    photoId: "synthetic",
    query: "sort=oldest",
    offset: 143,
    hidden: false,
  });
  visitGalleryPath("/app/gallery/synthetic");
  visitGalleryPath("/app/gallery/");
  await setup();
  await mountGallery();
  expect(
    urls
      .filter((url) => url.startsWith("/api/gallery/pages?"))
      .every((url) => !url.includes("anchor_asset_id")),
  ).toBe(true);
  clearGalleryPosition();
  rememberGalleryPosition({
    assetId: "synthetic",
    photoId: "synthetic",
    query: "sort=newest",
    offset: 143,
    hidden: false,
  });
  visitGalleryPath("/app/gallery/");
  expect((await import("../src/lib/gallery")).galleryReturnPosition()).toBeNull();
});

test("the real Gallery layout distinguishes the detail Gallery link and browser Back from fresh entry", async () => {
  await setup();
  const parent = createRootRoute({ component: AppShell });
  const layout = GalleryLayoutRoute.update({
    getParentRoute: () => parent,
    path: "/app/gallery",
  } as never);
  const index = GalleryRoute.update({ getParentRoute: () => layout, path: "/" } as never);
  const detail = createRoute({
    getParentRoute: () => layout,
    path: "$photoId",
    component: () => (
      <Link to="/app/gallery/" resetScroll={false}>
        Synthetic Back to Gallery
      </Link>
    ),
  });
  const router = createRouter({
    routeTree: parent.addChildren([layout.addChildren([index, detail])]),
    history: createMemoryHistory({ initialEntries: ["/app/gallery/"] }),
  });
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
  await flush();
  expect(urls.filter((url) => url.startsWith("/api/gallery/pages?")).length).toBe(1);
  await act(async () => container.querySelector<HTMLElement>("[data-gallery-id]")!.click());
  await flush();
  expect(container.textContent).toContain("Synthetic Back to Gallery");
  await act(
    async () =>
      container.querySelector<HTMLAnchorElement>('a[href="/app/gallery"]')?.click() ??
      [...container.querySelectorAll<HTMLAnchorElement>("a")]
        .find((a) => a.textContent === "Synthetic Back to Gallery")!
        .click(),
  );
  await flush();
  expect(urls.some((url) => url.includes("anchor_asset_id=asset"))).toBe(true);
  await act(async () => container.querySelector<HTMLElement>("[data-gallery-id]")!.click());
  await flush();
  await act(async () => router.history.back());
  await flush();
  expect(urls.filter((url) => url.includes("anchor_asset_id=asset")).length).toBe(2);
});

test("Sort expands years into touch-sized months and seeks without walking pages", async () => {
  await setup((url) => {
    return url.startsWith("/api/gallery/chronology?")
      ? Response.json([
          { year: 2024, month: 6, count: 800, start: "new-year" },
          { year: 2018, month: 9, count: 30, start: "old-year" },
          { year: 2018, month: 3, count: 40, start: "march" },
        ])
      : undefined;
  });
  await mountGallery();
  expect(document.querySelector('[aria-label="Gallery dates"]')).toBeNull();
  await click("Newest first");
  const rail = document.querySelector('[aria-label="Gallery dates"]')!;
  expect(container.querySelector('[aria-label="Gallery dates"]')).toBeNull();
  expect(rail.textContent).toContain("2018");
  expect(rail.closest('[aria-label="Gallery sort and dates"]')?.className).toContain("pv-dialog");
  expect(rail.closest('[aria-label="Gallery sort and dates"]')?.className).toContain(
    "overflow-y-auto",
  );
  expect(document.querySelector('[aria-label="Months in 2018"]')).toBeNull();
  await click("2018");
  expect(urls.filter((url) => url.startsWith("/api/gallery/pages?")).length).toBe(1);
  expect(rail.querySelector('[aria-expanded="true"]')?.textContent).toContain("2018");
  const month = [...rail.querySelectorAll<HTMLButtonElement>("button")].find(
    (button) => button.textContent === "March",
  )!;
  expect(month.className).toContain("min-h-11");
  await act(async () => month.click());
  await flush();
  expect(urls.some((url) => url.includes("start=march"))).toBe(true);
  expect(document.querySelector('[aria-label="Gallery dates"]')).toBeNull();
  expect(urls.filter((url) => url.startsWith("/api/gallery/pages?")).length).toBe(2);
  expect(urls.some((url) => url.includes("cursor="))).toBe(false);
});

test("Gallery appends a continuation page without duplicate cards", async () => {
  let observe: IntersectionObserverCallback | null = null;
  globalThis.IntersectionObserver = class {
    constructor(callback: IntersectionObserverCallback) {
      observe = callback;
    }
    observe() {}
    disconnect() {}
    unobserve() {}
    takeRecords() {
      return [];
    }
    root = null;
    rootMargin = "";
    thresholds = [];
  } as typeof IntersectionObserver;
  const photo = (id: string) => ({
    id,
    asset_id: `asset-${id}`,
    name: `Synthetic-${id}.jpg`,
    media_type: "image/jpeg",
    size: 100,
    captured_on: "2024-01-01",
    thumbnail_url: `/synthetic-${id}.jpg`,
  });
  await setup((url) => {
    if (!url.startsWith("/api/gallery/pages?")) return undefined;
    const query = new URL(url, "https://synthetic.invalid").searchParams;
    if (query.get("cursor") === "page-two")
      return Response.json({ items: [photo("second"), photo("first")], next_cursor: null });
    return Response.json({ items: [photo("first")], next_cursor: "page-two" });
  });
  await mountGallery();
  expect(container.querySelectorAll("[data-gallery-id]").length).toBe(1);
  await act(async () =>
    observe?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver),
  );
  await flush();
  expect(
    [...container.querySelectorAll<HTMLElement>("[data-gallery-id]")].map(
      (node) => node.dataset.galleryId,
    ),
  ).toEqual(["first", "second"]);
  expect(urls.filter((url) => url.includes("cursor=page-two")).length).toBe(1);
});

test("Gallery keeps mounted cards bounded with a 3,000-photo synthetic catalogue", async () => {
  const items = Array.from({ length: 3000 }, (_, index) => ({
    id: `synthetic-${index}`,
    asset_id: `asset-${index}`,
    name: `Synthetic-Example-${index}.jpg`,
    media_type: "image/jpeg",
    size: 100,
    captured_on: "2024-01-01",
    thumbnail_url: `/synthetic-${index}.jpg`,
  }));
  await setup((url) =>
    url.startsWith("/api/gallery/pages?") ? Response.json({ items, next_cursor: null }) : undefined,
  );
  await mountGallery();
  expect(container.querySelectorAll("[data-gallery-id]").length).toBeLessThanOrEqual(60);
});

test("shared section headings use authoritative totals and approved units", async () => {
  await setup();
  for (const [title, total] of [
    ["Gallery", "1,000 photos"],

    ["Home Videos", "20 videos"],
    ["Theatre", "200 movies"],
  ]) {
    await act(async () =>
      root.render(<SectionHeading title={title} section="Library" pathname={title} />),
    );
    await flush();
    expect(container.textContent).toContain(total);
    expect(container.textContent).not.toContain("Library");
  }
  expect(urls.every((u) => u === "/api/section-counts")).toBe(true);
});

test("real shell and Gallery keep distinct navigation, contextual actions, Sort and sticky selection", async () => {
  await setup();
  const router = await mountGallery();
  expect(container.querySelector("h1")?.textContent).toBe("Gallery");
  expect([...container.querySelectorAll("h2")].some((el) => el.textContent === "Gallery")).toBe(
    false,
  );
  expect(container.querySelector('[aria-label="Toggle navigation"]')).not.toBeNull();
  expect(container.querySelector("[data-gallery-controls]")?.className).toContain("sticky top-16");
  await act(async () =>
    container.querySelector<HTMLButtonElement>('[aria-label="Toggle navigation"]')!.click(),
  );
  expect(container.querySelector("[data-mobile-navigation-overlay]")).not.toBeNull();
  await act(async () =>
    container.querySelector<HTMLButtonElement>('[aria-label="Toggle navigation"]')!.click(),
  );
  await click("Gallery actions");
  const menu = container.querySelector("#gallery-context-actions")!;
  expect(menu.className).toContain("md:static md:flex");
  expect(menu.className).toContain("overflow-y-auto");
  expect(container.querySelector("[data-gallery-sort]")?.parentElement?.className).toContain(
    "flex-wrap",
  );
  for (const text of [
    "Filter",
    "Shared photos",
    "Select",
    "View Hidden",
    "Analyse existing photos",
  ])
    expect(menu.textContent).toContain(text);
  expect(menu.querySelector('select[aria-label="Sort Gallery photos"]')).toBeNull();
  expect(
    container.querySelector('[data-gallery-sort] button[aria-label="Sort Gallery photos"]'),
  ).not.toBeNull();
  window.scrollTo(0, 1600);
  await click("Select");
  expect(window.scrollY).toBe(1600);
  expect(
    container.querySelector('[aria-label="Gallery selection controls"]')?.textContent,
  ).toContain("0 selected");
  const tile = container.querySelector<HTMLElement>("[data-gallery-id]")!;
  await act(async () => tile.click());
  await flush();
  expect(
    container.querySelector('[aria-label="Gallery selection controls"]')?.textContent,
  ).toContain("1 selected");
  await click("Clear");
  expect(window.scrollY).toBe(1600);
  await click("Done");
  expect(window.scrollY).toBe(1600);
  await act(async () =>
    router.navigate({
      to: "/app/gallery/",
      search: { photo_type: ["document"] } as never,
      resetScroll: false,
    }),
  );
  await flush();
  expect(container.querySelector('[aria-label="Gallery total"]')?.textContent).toContain(
    "1,000 photos",
  );
  expect(urls.filter((u) => u === "/api/section-counts").length).toBe(1);
});

const cleanupPhotos = ["first", "second"].map((id) => ({
  id: `view-${id}`,
  asset_id: id,
  name: `${id}.jpg`,
  media_type: "image/jpeg",
  size: 10,
  thumbnail_url: "/synthetic.jpg",
  captured_on: "2024-01-01",
  can_edit: true,
}));
async function selectTiles() {
  await click("Select");
  for (const tile of container.querySelectorAll<HTMLElement>("[data-gallery-id]"))
    await act(async () => tile.click());
}

test("Hide reconciles partial success and retains failed photos/selection until retry", async () => {
  let fail = true;
  await setup((url) => {
    if (url.startsWith("/api/gallery/pages?"))
      return Response.json({ items: cleanupPhotos, next_cursor: null });
    if (url.endsWith("/lifecycle/hide"))
      return url.includes("second") && fail
        ? Response.json(
            { detail: "This photo is shared. Unshare it before hiding." },
            { status: 409 },
          )
        : Response.json({});
  });
  await mountGallery();
  await selectTiles();
  await click("Hide selected");
  expect(container.querySelectorAll("[data-gallery-id]").length).toBe(1);
  expect(container.querySelector("[data-gallery-id]")?.getAttribute("data-gallery-id")).toBe(
    "view-second",
  );
  expect(container.textContent).toContain("This photo is shared. Unshare it before hiding.");
  expect(
    container.querySelector('[aria-label="Gallery selection controls"]')?.textContent,
  ).toContain("1 selected");
  fail = false;
  await click("Hide selected");
  expect(container.querySelectorAll("[data-gallery-id]").length).toBe(0);
  expect(container.querySelector('[aria-label="Gallery selection controls"]')).toBeNull();
  expect(container.textContent).toContain("Gallery is empty");
});

test("Restore removes successes from Hidden Photos and reuses current session authorization", async () => {
  await setup((url) => {
    if (url === "/api/auth/hidden-photos/authorization") return Response.json({ authorized: true });
    if (url.startsWith("/api/gallery/pages?"))
      return Response.json({ items: cleanupPhotos, next_cursor: null });
    if (url.endsWith("/lifecycle/unhide")) return Response.json({});
  });
  await mountGallery();
  await click("View Hidden");
  await selectTiles();
  await click("Restore selected");
  expect(container.querySelectorAll("[data-gallery-id]").length).toBe(0);
  expect(container.textContent).toContain("No hidden photos");
  expect(urls.some((url) => url.endsWith("/authorization/options"))).toBe(false);
});

test("preference refresh errors retain cards, and a successful empty filter clears stale errors", async () => {
  let failing = false;
  let empty = false;
  await setup((url) => {
    if (url.startsWith("/api/gallery/pages?"))
      return failing
        ? Response.json({}, { status: 503 })
        : Response.json({ items: empty ? [] : cleanupPhotos, next_cursor: null });
  });
  const router = await mountGallery();
  failing = true;
  const toggle = container.querySelector<HTMLInputElement>('details input[type="checkbox"]')!;
  await act(async () => toggle.click());
  await flush();
  expect(container.querySelectorAll("[data-gallery-id]").length).toBe(2);
  expect(container.textContent).toContain("The Gallery is currently unavailable.");
  failing = false;
  empty = true;
  await act(async () =>
    router.navigate({
      to: "/app/gallery/",
      search: { private_tag: ["unused"] } as never,
      resetScroll: false,
    }),
  );
  await flush();
  expect(container.textContent).not.toContain("The Gallery is currently unavailable.");
  expect(container.textContent).toContain("No photos match these filters");
});
