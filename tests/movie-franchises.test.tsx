import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { franchiseRequest, type MovieFranchise } from "../src/lib/movie-franchises";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { MovieFranchisePanel, MovieFranchiseMembership } =
  await import("../src/components/pv/MovieFranchises");
const originalFetch = globalThis.fetch;
let root: ReturnType<typeof createRoot> | undefined;
let container: HTMLDivElement;
afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  root = undefined;
  container?.remove();
  globalThis.fetch = originalFetch;
});
const movie = (id: string) => ({ id, title: `Example Movie ${id}`, year: 2000, poster_url: null });
const example = (): MovieFranchise => ({
  id: "example",
  name: "Example Franchise",
  description: "",
  member_count: 2,
  member_ids: ["a", "c"],
  poster_url: null,
  movies: [movie("a"), movie("c")],
  selected_order: null,
  effective_order: "timeline",
  timeline_available: true,
  chronology_source: "Example Publisher",
  chronology_url: "https://example.invalid/chronology",
  chronology_version: "example-v1",
});
async function mount(component: React.ReactNode) {
  const parent = createRootRoute({ component: () => component });
  const router = createRouter({
    routeTree: parent,
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    await router.load();
    root!.render(<RouterProvider router={router} />);
  });
}
async function click(text: string) {
  const button = [...document.querySelectorAll("button")].find((item) => item.textContent === text);
  expect(button).toBeDefined();
  await act(async () => button!.click());
}

test("franchise membership is added and removed only by explicit movie actions", async () => {
  let entry = example();
  entry.member_ids = ["c"];
  const requests: { url: string; method: string; body?: unknown }[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input),
      method = init?.method ?? "GET";
    requests.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (method === "POST") entry = { ...entry, member_ids: ["c", "a"] };
    if (method === "DELETE") entry = { ...entry, member_ids: ["c"] };
    return Response.json(method === "GET" ? [entry] : entry);
  }) as typeof fetch;
  await mount(<MovieFranchiseMembership movieId="a" />);
  expect(container.textContent).not.toContain("Create franchise");
  expect(container.querySelector('input[type="search"]')).toBeNull();
  expect(requests.every((request) => request.method === "GET")).toBe(true);
  const select = container.querySelector("select")!;
  await act(async () => {
    select.value = "example";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await click("Add to franchise");
  expect(requests.find((request) => request.method === "POST")?.body).toEqual({ asset_ids: ["a"] });
  expect(container.textContent).toContain("Remove from Example Franchise");
  await click("Remove from Example Franchise");
  expect(requests.find((request) => request.method === "DELETE")?.url).toBe(
    "/api/movie-franchises/example/members/a",
  );
  expect(container.textContent).not.toContain("Remove from Example Franchise");
});

test("franchise orders are selected without numbering, batch addition preserves server order", async () => {
  let entry = example();
  const mutations: { url: string; body: unknown }[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input),
      method = init?.method ?? "GET";
    if (url === "/api/user-state/movies") return Response.json({});
    if (url === "/api/movies") return Response.json([movie("a"), movie("b"), movie("c")]);
    if (method !== "GET") {
      const body = JSON.parse(String(init?.body));
      mutations.push({ url, body });
      if (url.endsWith("/order"))
        entry = { ...entry, effective_order: "release", movies: [movie("c"), movie("a")] };
      else
        entry = {
          ...entry,
          member_ids: ["c", "b", "a"],
          movies: [movie("c"), movie("b"), movie("a")],
        };
    }
    return Response.json(entry);
  }) as typeof fetch;
  await mount(<MovieFranchisePanel id="example" onDeleted={() => {}} />);
  expect(container.textContent).not.toContain("Create franchise");
  expect(container.querySelector('input[type="search"]')).toBeNull();
  expect(container.textContent).toContain("Edit franchise");
  expect(container.textContent).toContain("Remove from franchise");
  const select = container.querySelector<HTMLSelectElement>(
    'select[aria-label="Franchise order"]',
  )!;
  await act(async () => {
    select.value = "release";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(mutations[0]).toEqual({
    url: "/api/movie-franchises/example/order",
    body: { order: "release" },
  });
  expect([...container.querySelectorAll("h4")].map((item) => item.textContent)).toEqual([
    "Example Movie c",
    "Example Movie a",
  ]);
  await click("Add movies");
  const checkbox = document.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  await act(async () => checkbox.click());
  await click("Add selected movies (1)");
  expect(mutations[1].body).toEqual({ asset_ids: ["b"] });
  expect([...container.querySelectorAll("h4")].map((item) => item.textContent)).toEqual([
    "Example Movie c",
    "Example Movie b",
    "Example Movie a",
  ]);
  expect(container.textContent).not.toContain("order number");
});

test("unresolved timeline is unavailable and Release is honestly labelled", async () => {
  const entry = {
    ...example(),
    timeline_available: false,
    effective_order: "release",
    selected_order: "timeline",
  };
  globalThis.fetch = (async (input) =>
    Response.json(String(input).includes("user-state") ? {} : entry)) as typeof fetch;
  await mount(<MovieFranchisePanel id="example" onDeleted={() => {}} />);
  const select = container.querySelector<HTMLSelectElement>(
    'select[aria-label="Franchise order"]',
  )!;
  expect(select.value).toBe("release");
  expect(select.querySelector<HTMLOptionElement>('option[value="timeline"]')!.disabled).toBe(true);
  expect(container.textContent).toContain(
    "Timeline is unavailable for this collection. Showing Release order.",
  );
});

test("failed franchise mutation is surfaced and authenticated JSON contract is preserved", async () => {
  let submitted: RequestInit | undefined;
  globalThis.fetch = (async (_input, init) => {
    submitted = init;
    return new Response(null, { status: 403 });
  }) as typeof fetch;
  await expect(franchiseRequest("/example/members", "POST", { asset_ids: ["a"] })).rejects.toThrow(
    "could not be updated",
  );
  expect(submitted?.credentials).toBe("include");
  expect(submitted?.headers).toEqual({
    Accept: "application/json",
    "Content-Type": "application/json",
  });
});
