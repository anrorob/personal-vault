import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { ArrivalMusicImports } = await import("../src/components/ArrivalMusicImports");
import type { VaultMasterItem } from "../src/lib/incoming";
const originalFetch = globalThis.fetch;
const originalConfirm = window.confirm;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
  window.confirm = originalConfirm;
});
function members() {
  return Array.from(
    { length: 13 },
    (_, index) =>
      ({
        id: `track-${index}`,
        album_group_id: "album-one",
        source_kind: "incoming",
        state: "needs_review",
        filename: `synthetic-${index}.wma`,
        metadata: {
          source_context: {
            original_filename: `synthetic-${index}.wma`,
            music_album: { artist_name: "Synthetic artist", album_title: "Synthetic album" },
          },
        },
      }) as VaultMasterItem,
  );
}
async function mount(items: VaultMasterItem[], onChanged = async () => {}) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<ArrivalMusicImports items={items} onChanged={onChanged} />));
}
test("one album review publishes all 13 received members with one confirmation", async () => {
  const requests: { item_ids: string[] }[] = [];
  let refreshed = false;
  globalThis.fetch = (async (url, options) => {
    expect(String(url)).toBe("/api/vault-master/music/imports/album-one/approve");
    requests.push(JSON.parse(String(options?.body)));
    return Response.json({ items: [] });
  }) as typeof fetch;
  window.confirm = () => true;
  await mount(members(), async () => {
    refreshed = true;
  });
  expect(container.querySelectorAll('section[aria-label="Music album import"]').length).toBe(1);
  expect(container.querySelectorAll("li").length).toBe(13);
  await act(async () => container.querySelector("button")!.click());
  expect(requests).toEqual([{ item_ids: members().map((item) => item.id) }]);
  expect(refreshed).toBe(true);
});
test("ambiguous metadata preserves group; standalone files and completed groups are not album review", async () => {
  const items = members().map((item) => ({
    ...item,
    metadata: {
      source_context: {
        source_label: "Unsplit folder",
        music_album: { artist_name: "", album_title: "" },
      },
    },
  }));
  await mount([...items, { ...items[0], id: "song", album_group_id: null }]);
  expect(container.textContent).toContain("Unsplit folder");
  expect(container.querySelectorAll("li").length).toBe(13);
  await act(async () =>
    root.render(
      <ArrivalMusicImports
        items={items.map((item) => ({ ...item, state: "moved" }))}
        onChanged={async () => {}}
      />,
    ),
  );
  expect(container.querySelector("section")).toBeNull();
});
test("a failed approval keeps the complete album visible", async () => {
  globalThis.fetch = (async () =>
    Response.json({ detail: "Received tracks changed" }, { status: 409 })) as typeof fetch;
  window.confirm = () => true;
  await mount(members());
  await act(async () => container.querySelector("button")!.click());
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(
    "Received tracks changed",
  );
  expect(container.querySelectorAll("li").length).toBe(13);
});

test("group removal reuses the exact staged-only recovery confirmation and member IDs", async () => {
  let posted: Record<string, unknown> | undefined;
  globalThis.fetch = (async (url, options) => {
    expect(String(url)).toBe("/api/vault-master/recovery");
    posted = JSON.parse(String(options?.body));
    return Response.json({ outcomes: [] });
  }) as typeof fetch;
  window.confirm = () => false;
  await mount(members());
  const remove = [...container.querySelectorAll("button")].find(
    (button) => button.textContent === "Remove unpublished staging",
  )!;
  await act(async () => remove.click());
  expect(posted).toBeUndefined();
  window.confirm = () => true;
  await act(async () => remove.click());
  expect(posted).toEqual({
    action: "remove",
    confirmation: "REMOVE FROM ARRIVAL HALL",
    item_ids: members().map((item) => item.id),
  });
});

test("partial album shows 5 of 13 and retries only eight missing members", async () => {
  const items = members().map((item, index) => ({
    ...item,
    state: index < 5 ? "moved" : "theatre_promotion_pending",
  }));
  let posted: Record<string, unknown> | undefined;
  globalThis.fetch = (async (url, options) => {
    expect(String(url)).toBe("/api/vault-master/recovery");
    posted = JSON.parse(String(options?.body));
    return Response.json({ outcomes: [] });
  }) as typeof fetch;
  await mount(items);
  expect(container.textContent).toContain("5 of 13 published");
  expect(container.querySelectorAll("section")).toHaveLength(1);
  const retry = [...container.querySelectorAll("button")].find(
    (button) => button.textContent === "Retry safe publication",
  )!;
  await act(async () => retry.click());
  expect(posted).toEqual({ action: "retry", item_ids: items.slice(5).map((item) => item.id) });
});
