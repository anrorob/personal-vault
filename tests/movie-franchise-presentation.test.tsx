import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import {
  FRANCHISE_POSTER_INTERVAL_MS as hour,
  franchisePosterAt,
  memberPosters,
  ungroupedMovies,
} from "../src/lib/movie-franchise-presentation";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { FranchisePoster } = await import("../src/components/pv/FranchisePoster");
let root: ReturnType<typeof createRoot> | undefined;
let container: HTMLDivElement;
const originalHref = window.location.href;
beforeEach(() => {
  window.location.href = "http://localhost/";
});
afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  root = undefined;
  container?.remove();
  window.location.href = originalHref;
});

test("normal representation hides any membership and restores movies when final membership disappears", () => {
  const movies = [{ id: "a" }, { id: "b" }, { id: "c" }];
  const one = { member_ids: ["a", "b"] },
    two = { member_ids: ["a"] };
  const before = JSON.stringify(movies);
  expect(ungroupedMovies(movies, [one, two])).toEqual([movies[2]]);
  expect(ungroupedMovies(movies, [{ member_ids: ["b"] }, two])).toEqual([movies[2]]);
  expect(ungroupedMovies(movies, [{ member_ids: ["b"] }])).toEqual([movies[0], movies[2]]);
  expect(ungroupedMovies(movies, [two])).toEqual([movies[1], movies[2]]);
  expect(ungroupedMovies(movies, [])).toEqual(movies);
  expect(JSON.stringify(movies)).toBe(before);
});

test("posters use authorized member artwork, stable ID order, and skip absent/unsupported/duplicate URLs", () => {
  const movies = [
    { id: "b", poster_url: "/example-b.jpg" },
    { id: "a", poster_url: "/example-a.jpg" },
    { id: "c", poster_url: null },
    { id: "d", poster_url: " " },
    { id: "e", poster_url: "javascript:example" },
    { id: "f", poster_url: "/example-a.jpg" },
    { id: "unrelated", poster_url: "/unrelated.jpg" },
  ];
  const ids = ["f", "e", "d", "c", "b", "a"];
  const before = JSON.stringify(movies);
  expect(memberPosters(movies, ids)).toEqual(["/example-a.jpg", "/example-b.jpg"]);
  expect(memberPosters([...movies].reverse(), ids)).toEqual(memberPosters(movies, ids));
  expect(memberPosters(movies, ["c"])).toEqual([]);
  expect(JSON.stringify(movies)).toBe(before);
});

test("hourly rotation is stable within a bucket, cycles predictably, and skips failed artwork", () => {
  const posters = ["/example-a.jpg", "/example-b.jpg", "/example-c.jpg"];
  const failed = new Set<string>();
  expect(franchisePosterAt(posters, 0, failed)).toBe(posters[0]);
  expect(franchisePosterAt(posters, hour - 1, failed)).toBe(posters[0]);
  expect(franchisePosterAt(posters, hour, failed)).toBe(posters[1]);
  expect(franchisePosterAt(posters, 2 * hour, failed)).toBe(posters[2]);
  expect(franchisePosterAt(posters, 3 * hour, failed)).toBe(posters[0]);
  expect(franchisePosterAt([posters[0]], 20 * hour, failed)).toBe(posters[0]);
  expect(franchisePosterAt(posters, 0, new Set([posters[0]]))).toBe(posters[1]);
  expect(franchisePosterAt(posters, 0, new Set(posters))).toBeNull();
  expect(franchisePosterAt([], 0, failed)).toBeNull();
});

async function mount(posters: string[]) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root!.render(<FranchisePoster name="Example Saga" posters={posters} />));
}

test("a franchise without member artwork uses the existing generic placeholder", async () => {
  await mount([]);
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("svg")).not.toBeNull();
});

test("single poster persists; broken images advance to another member and finally generic fallback", async () => {
  await mount(["/example-a.jpg"]);
  expect(container.querySelector("img")?.getAttribute("src")).toBe("/example-a.jpg");
  await act(async () =>
    root!.render(<FranchisePoster name="Example Saga" posters={["/example-a.jpg"]} />),
  );
  expect(container.querySelector("img")?.getAttribute("src")).toBe("/example-a.jpg");
  await act(async () => container.querySelector("img")!.dispatchEvent(new Event("error")));
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("svg")).not.toBeNull();
  await act(async () =>
    root!.render(
      <FranchisePoster name="Example Saga" posters={["/example-a.jpg", "/example-b.jpg"]} />,
    ),
  );
  expect(container.querySelector("img")?.getAttribute("src")).toBe("/example-b.jpg");
  await act(async () => container.querySelector("img")!.dispatchEvent(new Event("error")));
  expect(container.querySelector("img")).toBeNull();
  expect(container.firstElementChild?.className).toContain("aspect-[2/3]");
});

test("a mounted card advances at the next hour boundary and cancels its timer on unmount", async () => {
  let now = 5 * 60 * 1000;
  const clock = spyOn(Date, "now").mockImplementation(() => now);
  const originalTimeout = globalThis.setTimeout,
    originalClear = globalThis.clearTimeout;
  const timers = new Map<number, () => void>();
  let nextId = -1;
  globalThis.setTimeout = ((callback: () => void, delay: number, ...args: unknown[]) => {
    if (delay < 60 * 1000) return originalTimeout(callback, delay, ...args);
    expect(delay).toBeGreaterThan(0);
    const id = nextId--;
    timers.set(id, callback);
    return id;
  }) as typeof setTimeout;
  globalThis.clearTimeout = ((id: number) => {
    if (!timers.delete(id)) originalClear(id);
  }) as typeof clearTimeout;
  try {
    await mount(["/example-a.jpg", "/example-b.jpg"]);
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/example-a.jpg");
    await act(async () =>
      root!.render(
        <FranchisePoster
          name="Renamed Example Saga"
          posters={["/example-a.jpg", "/example-b.jpg"]}
        />,
      ),
    );
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/example-a.jpg");
    now = hour + 1;
    await act(async () => {
      const callbacks = [...timers.values()];
      timers.clear();
      callbacks.forEach((callback) => callback());
    });
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/example-b.jpg");
    await act(async () => root!.unmount());
    root = undefined;
    expect(timers.size).toBe(0);
  } finally {
    globalThis.setTimeout = originalTimeout;
    globalThis.clearTimeout = originalClear;
    clock.mockRestore();
  }
});
