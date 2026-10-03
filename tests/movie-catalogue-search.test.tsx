import { afterEach, expect, test } from "bun:test";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { filterMovieCatalogue } from "../src/lib/movie-catalogue-search";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { Route } = await import("../src/routes/app.movies.index");
const movies = [
  {
    id: "a",
    title: "Example Adventure",
    original_title: "Exemple Original",
    year: 1999,
    poster_url: null as string | null,
    is_exclusive_movie: false,
  },
  {
    id: "b",
    title: "Example Sequel",
    original_title: null,
    year: 2003,
    poster_url: null as string | null,
    is_exclusive_movie: true,
  },
  {
    id: "c",
    title: "ExampleVeryLongUnbrokenMovieTitleForWrapping",
    year: null,
    poster_url: null as string | null,
    is_exclusive_movie: false,
  },
];
const franchises = [
  {
    id: "example",
    name: "Example Saga",
    member_ids: ["a", "b"],
    member_count: 2,
    description: "",
    poster_url: null,
    movies: movies.slice(0, 2),
    selected_order: null,
    effective_order: "release",
    timeline_available: false,
    chronology_source: null,
    chronology_url: null,
    chronology_version: null,
  },
];
const originalFetch = globalThis.fetch;
const originalHref = window.location.href;
let root: ReturnType<typeof createRoot> | undefined;
let container: HTMLDivElement;
let requests: { url: string; method: string }[];
let catalogueFranchises: typeof franchises;
afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  root = undefined;
  container?.remove();
  globalThis.fetch = originalFetch;
  window.location.href = originalHref;
});

test("search matches title, original title, year and manual franchises without IDs or mutation", () => {
  const before = JSON.stringify({ movies, franchises });
  for (const [query, ids] of [
    ["ADVENT", ["a"]],
    ["exemple", ["a"]],
    ["1999", ["a"]],
    ["sAgA", ["a", "b"]],
    ["no match", []],
    ["example", ["a", "b", "c"]],
  ] as const)
    expect(filterMovieCatalogue(movies, franchises, query).movies.map((m) => m.id)).toEqual(ids);
  expect(filterMovieCatalogue(movies, franchises, " saga ").franchises).toEqual(franchises);
  expect(
    filterMovieCatalogue(
      [{ id: "internal-only", title: "Example", year: null }],
      [],
      "internal-only",
    ).movies,
  ).toEqual([]);
  expect(filterMovieCatalogue(movies, franchises, "  ").movies).toBe(movies);
  expect(filterMovieCatalogue(movies, franchises, "").franchises).toBe(franchises);
  expect(JSON.stringify({ movies, franchises })).toBe(before);
});

async function mount(initialFranchises = franchises, catalogueMovies = movies) {
  requests = [];
  window.location.href = "http://localhost/app/movies/";
  catalogueFranchises = structuredClone(initialFranchises);
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    requests.push({ url, method: init?.method ?? "GET" });
    if (url.startsWith("/api/movies?"))
      return Response.json(url.endsWith("exclusive") ? [catalogueMovies[1]] : catalogueMovies);
    if (url === "/api/movie-franchises") {
      if (init?.method === "POST")
        return Response.json({
          ...franchises[0],
          id: "new-example",
          ...JSON.parse(String(init.body)),
        });
      // List responses intentionally omit detail movies; posters come from the catalogue.
      return Response.json(catalogueFranchises.map((item) => ({ ...item, movies: [] })));
    }
    if (url.startsWith("/api/movie-franchises/")) {
      const [, id, member] =
        url.match(/\/api\/movie-franchises\/([^/]+)(?:\/members\/([^/]+))?/) ?? [];
      const entry = catalogueFranchises.find((item) => item.id === id)!;
      if (init?.method === "DELETE") {
        if (!member) {
          catalogueFranchises = catalogueFranchises.filter((item) => item.id !== id);
          return new Response(null, { status: 204 });
        }
        entry.member_ids = entry.member_ids.filter((item) => item !== member);
        entry.movies = entry.movies.filter((item) => item.id !== member);
        entry.member_count = entry.member_ids.length;
      }
      return Response.json(entry);
    }
    if (url === "/api/user-state/movies")
      return Response.json({
        a: { state: "watched", completed: true, position_seconds: 100, duration_seconds: 100 },
        b: { state: "in_progress", completed: false, position_seconds: 40, duration_seconds: 100 },
      });
    throw new Error(`Unexpected request ${url}`);
  }) as typeof fetch;
  const parent = createRootRoute();
  const route = Route.update({ getParentRoute: () => parent, path: "/app/movies/" } as never);
  const detail = createRoute({
    getParentRoute: () => parent,
    path: "/app/movies/$movieId",
    component: () => <p>Example movie details</p>,
  });
  const router = createRouter({
    routeTree: parent.addChildren([route, detail]),
    history: createMemoryHistory({ initialEntries: ["/app/movies/"] }),
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    await router.load();
    root!.render(<RouterProvider router={router} />);
  });
  return router;
}
async function click(label: string) {
  const button = [...container.querySelectorAll("button")].find(
    (b) => b.textContent?.trim() === label || b.getAttribute("aria-label") === label,
  );
  expect(button).toBeDefined();
  await act(async () => button!.click());
}
async function search(query: string) {
  const input = container.querySelector<HTMLInputElement>('input[type="search"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, query);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function openOverflow() {
  const trigger = container.querySelector<HTMLButtonElement>(
    'button[aria-label="Movies actions"]',
  )!;
  await act(async () => trigger.click());
}
async function selectAction(label: string) {
  const item = [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')].find(
    (item) => item.textContent === label,
  )!;
  expect(item).toBeDefined();
  await act(async () => item.click());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
  });
}
const movieLinks = () => [...container.querySelectorAll('a[aria-label^="View details"]')];

test("mobile overflow has only the two secondary actions, opens existing search and dismisses accessibly", async () => {
  await mount();
  const trigger = container.querySelector<HTMLButtonElement>(
    'button[aria-label="Movies actions"]',
  )!;
  expect(trigger.className).not.toContain("md:hidden");
  for (const label of ["Search", "Create franchise"]) {
    const button = [...container.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === label,
    )!;
    expect(button).toBeUndefined();
  }
  const count = requests.length;
  await openOverflow();
  // The portal is outside .pv; it must carry the owned portal palette.
  const menu = document.querySelector('[role="menu"]')!;
  expect(container.contains(menu)).toBe(false);
  expect(menu.classList.contains("pv-dialog")).toBe(true);
  expect(
    [...document.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent),
  ).toEqual(["Search", "Create franchise"]);
  await selectAction("Search");
  const input = container.querySelector<HTMLInputElement>('input[type="search"]')!;
  expect(document.querySelector('[role="menu"]')).toBeNull();
  expect(document.activeElement === input).toBe(true);
  await search("saga");
  expect(movieLinks()).toHaveLength(2);
  await click("Clear movie search");
  expect(movieLinks()).toHaveLength(1);
  await openOverflow();
  await act(async () =>
    document
      .querySelector('[role="menu"]')!
      .dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
  );
  expect(document.querySelector('[role="menu"]')).toBeNull();
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
  });
  expect(document.activeElement === trigger).toBe(true);
  await openOverflow();
  await act(async () =>
    document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })),
  );
  expect(document.querySelector('[role="menu"]')).toBeNull();
  expect(requests).toHaveLength(count);
});

test("overflow Create franchise opens the existing editor after the menu closes", async () => {
  await mount();
  await openOverflow();
  await selectAction("Create franchise");
  expect(document.querySelector('[role="menu"]')).toBeNull();
  const dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.textContent).toContain("Create franchise");
  expect(dialog.querySelector("input") === document.activeElement).toBe(true);
  expect(requests.every((request) => request.method === "GET")).toBe(true);
  await act(async () =>
    dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
  );
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  await openOverflow();
  await selectAction("Create franchise");
  const reopened = document.querySelector('[role="dialog"]')!;
  const name = reopened.querySelector<HTMLInputElement>("input")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(
      name,
      "Example New Franchise",
    );
    name.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () =>
    reopened
      .querySelector("form")!
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
  );
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(
    container.querySelector('a[aria-label="Open franchise Example New Franchise"]'),
  ).not.toBeNull();
  expect(requests.filter((request) => request.method === "POST")).toEqual([
    { url: "/api/movie-franchises", method: "POST" },
  ]);
});

test("compact search filters in place, clears to existing order, and never requests per keystroke", async () => {
  await mount();
  expect(container.querySelector('input[type="search"]')).toBeNull();
  const links = movieLinks().map((a) => a.getAttribute("href"));
  await openOverflow();
  await selectAction("Search");
  const requestCount = requests.length;
  for (const query of ["advent", "exemple", "1999"]) {
    await search(query);
    expect(movieLinks().map((a) => a.textContent)).toEqual([
      expect.stringContaining("Example Adventure"),
    ]);
  }
  await search("SAGA");
  expect(movieLinks()).toHaveLength(2);
  expect(container.querySelector('a[aria-label="Open franchise Example Saga"]')).not.toBeNull();
  await search("no match");
  expect(movieLinks()).toHaveLength(0);
  expect(container.querySelector('[role="status"]')?.textContent).toContain(
    "No movies or franchises match",
  );
  await click("Clear movie search");
  expect(movieLinks().map((a) => a.getAttribute("href"))).toEqual(links);
  expect(container.querySelector('[role="status"]')).toBeNull();
  expect(requests).toHaveLength(requestCount);
  expect(requests.every((request) => request.method === "GET")).toBe(true);
  await click("Exclusive Movies");
  await search("saga");
  expect(movieLinks()).toHaveLength(1);
  await click("Clear movie search");
  expect(movieLinks()).toHaveLength(1);
  expect(container.querySelector('button[aria-pressed="true"]')?.textContent).toBe(
    "Exclusive Movies",
  );
});

test("both grids retain posters, wrapping, watched/progress and normal navigation", async () => {
  const router = await mount();
  const grids = [...container.querySelectorAll(".pv-card-grid")];
  expect(grids).toHaveLength(2);
  for (const grid of grids) {
    expect(grid.classList.contains("pv-card-grid")).toBe(true);
    for (const card of grid.children) {
      expect(card.classList.contains("min-w-0")).toBe(true);
      expect(card.className).not.toContain("col-span");
      expect(card.querySelector("h3")?.className).toContain("[overflow-wrap:anywhere]");
      expect(card.querySelector('[class*="aspect-[2/3]"]')).not.toBeNull();
    }
  }
  expect(movieLinks().map((link) => link.getAttribute("href"))).toEqual(["/app/movies/c"]);
  await openOverflow();
  await selectAction("Search");
  await search("example");
  expect(movieLinks()[0].textContent).toContain("Watched");
  expect(movieLinks()[0].textContent).toContain("1999");
  expect(movieLinks()[1].querySelector("progress")?.value).toBe(40);
  await act(async () =>
    (container.querySelector('a[aria-label="Open franchise Example Saga"]') as HTMLElement).click(),
  );
  expect(router.state.location.search).toEqual({ franchise: "example" });
  expect(container.textContent).toContain("Example Saga");
  expect(container.querySelector('[aria-label="Movies actions"]')).toBeNull();
  expect(container.querySelector('[aria-label="Search movies"]')).toBeNull();
  expect(container.textContent).not.toContain("Create franchise");
  expect(container.textContent).toContain("Add movies");
  expect(container.textContent).toContain("Edit franchise");
  await act(async () => router.navigate({ to: "/app/movies", search: {} }));
  await act(async () => (movieLinks()[0] as HTMLElement).click());
  expect(container.textContent).toContain("Example movie details");
});

test("removing all memberships and deleting a franchise restore standalone cards on return", async () => {
  const router = await mount();
  expect(movieLinks()).toHaveLength(1);
  await act(async () => router.navigate({ to: "/app/movies", search: { franchise: "example" } }));
  expect(movieLinks()).toHaveLength(2);
  await click("Remove from franchise");
  await act(async () => router.navigate({ to: "/app/movies", search: {} }));
  expect(movieLinks().map((link) => link.getAttribute("href"))).toEqual([
    "/app/movies/a",
    "/app/movies/c",
  ]);
  await act(async () => router.navigate({ to: "/app/movies", search: { franchise: "example" } }));
  await click("Delete franchise");
  const confirm = [...document.querySelector('[role="dialog"]')!.querySelectorAll("button")].find(
    (button) => button.textContent === "Delete franchise",
  )!;
  await act(async () => confirm.click());
  expect(movieLinks()).toHaveLength(3);
  expect(container.querySelector('a[aria-label^="Open franchise"]')).toBeNull();
});

test("membership in another franchise keeps a movie hidden after removal from one", async () => {
  const other = {
    ...franchises[0],
    id: "other-example",
    name: "Example Other Saga",
    member_ids: ["a"],
    member_count: 1,
    movies: [movies[0]],
  };
  const router = await mount([...franchises, other]);
  expect(movieLinks()).toHaveLength(1);
  expect(container.querySelectorAll('a[aria-label^="Open franchise"]')).toHaveLength(2);
  await act(async () => router.navigate({ to: "/app/movies", search: { franchise: "example" } }));
  await click("Remove from franchise");
  await act(async () => router.navigate({ to: "/app/movies", search: {} }));
  expect(movieLinks().map((link) => link.getAttribute("href"))).toEqual(["/app/movies/c"]);
  await act(async () =>
    router.navigate({ to: "/app/movies", search: { franchise: "other-example" } }),
  );
  expect(movieLinks()[0].getAttribute("href")).toBe("/app/movies/a");
  await act(async () => (movieLinks()[0] as HTMLElement).click());
  expect(container.textContent).toContain("Example movie details");
});

test("franchise card takes a poster from member catalogue metadata even when list detail movies are omitted", async () => {
  await mount(
    franchises,
    movies.map((movie) => ({
      ...movie,
      poster_url: movie.id === "a" ? "/example-member-poster.jpg" : null,
    })),
  );
  const card = container.querySelector('a[aria-label="Open franchise Example Saga"]')!;
  expect(card.querySelector("img")?.getAttribute("src")).toBe("/example-member-poster.jpg");
  expect(card.textContent).toContain("Franchise · 2 movies");
  expect(movieLinks().map((link) => link.getAttribute("href"))).toEqual(["/app/movies/c"]);
});

test("compiled catalogue CSS includes the shared width-driven grid", async () => {
  const { compile } = await import("@tailwindcss/node");
  const source = await readFile(resolve("src/styles.css"), "utf8");
  const compiler = await compile(source, { base: resolve("src"), onDependency: () => {} });
  const css = compiler.build([
    "min-w-0",
    "[overflow-wrap:anywhere]",
    "aspect-[2/3]",
    "min-h-11",
    "min-w-11",
    "max-w-[calc(100vw-1rem)]",
  ]);
  expect(css).toContain(".pv-card-grid");
  expect(css).toContain("@container (min-width: 36rem)");
  expect(css).toContain("grid-template-columns: repeat(3, minmax(0, 1fr))");
  expect(css).toContain("overflow-wrap: anywhere");
  expect(css).toContain("aspect-ratio: 2/3");
  expect(css).toContain("max-width: calc(100vw - 1rem)");
});

test("touch gesture opens once, outside tap closes, and keyboard activation remains available", async () => {
  await mount();
  const trigger = container.querySelector<HTMLButtonElement>(
    'button[aria-label="Movies actions"]',
  )!;
  await act(async () => {
    trigger.dispatchEvent(
      new PointerEvent("pointerdown", {
        pointerType: "touch",
        button: 0,
        bubbles: true,
        cancelable: true,
      }),
    );
    trigger.dispatchEvent(
      new PointerEvent("pointerup", { pointerType: "touch", button: 0, bubbles: true }),
    );
    trigger.click();
  });
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(document.querySelector('[role="menu"]')).not.toBeNull();
  expect(document.body.style.pointerEvents).not.toBe("none");
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
  });
  await act(async () => {
    document.body.dispatchEvent(
      new PointerEvent("pointerdown", { pointerType: "touch", bubbles: true }),
    );
    document.body.click();
  });
  expect(document.querySelector('[role="menu"]')).toBeNull();
  await act(async () =>
    trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })),
  );
  expect(document.querySelector('[role="menu"]')).not.toBeNull();
});
