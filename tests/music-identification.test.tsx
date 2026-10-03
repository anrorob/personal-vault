import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { Route: AlbumsRoute } = await import("../src/routes/app.music_.albums.index");
const { Route: SongsRoute } = await import("../src/routes/app.music_.songs.index");
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
const originalFetch = globalThis.fetch;
let requests: { url: string; body?: unknown }[];
const candidate = {
  release_id: "12345678-1234-4123-8123-123456789abc",
  title: "Example Release",
  artist: "Example Artist",
  date: "2002-03-04",
  country: "US",
  track_count: 12,
  score: 100,
  cover_art_available: false,
};
async function mount(
  search: () => Promise<Response> | Response,
  groupIds: string[] = [],
  example_album = false,
  unorderedApproval = false,
  previewOverride?: Record<string, unknown>,
) {
  requests = [];
  globalThis.fetch = (async (input, options) => {
    const url = String(input);
    requests.push({ url, body: options?.body ? JSON.parse(String(options.body)) : undefined });
    if (url === "/api/music")
      return Response.json(
        (groupIds.length ? groupIds : [null]).map((groupId, index) => ({
          id: groupId ? `synthetic-${index}` : "synthetic",
          album_group_id: groupId,
          asset_id: "asset",
          can_edit: true,
          album_order_state: "unresolved",
          album_position: null,
          album_member_count: 1,
          title: "Synthetic track",
          artist: example_album ? "EXAMPLE ARTIST" : "Example Artist",
          album: example_album ? "Example Album Act 1" : "Example Release",
          album_artist: null,
          album_folder: example_album ? `Albums/${groupId}` : ".",
          genre: null,
          genres: [],
          track_number: 1,
          disc_number: 1,
          release_year: 2017,
          overview: null,
          duration_seconds: 100,
          artwork_url: null,
          lyrics_available: false,
          enrichment_status: unorderedApproval ? "identified" : "needs_review",
          playback_url: "/synthetic/playback",
        })),
      );
    if (url.endsWith("/search")) return search();
    if (url.endsWith("/preview") && previewOverride) return Response.json(previewOverride);
    if (url.endsWith("/approve") && previewOverride)
      return Response.json({ sidecars_exported: true });
    if (url.endsWith("/preview") && unorderedApproval)
      return Response.json({
        ...candidate,
        folder: ".",
        genres: [],
        local_track_count: 1,
        matched_track_count: 0,
        tracks: [],
        unmatched_local_files: ["Example.wav"],
      });
    if (url.endsWith("/approve") && unorderedApproval)
      return Response.json({ updated_track_count: 1, artwork_retained: false });
    if (url.endsWith("/preview"))
      return Response.json(
        {
          detail:
            "These tracks are stored directly in Music. Album grouping is required before reviewing or approving a match.",
        },
        { status: 422 },
      );
    throw new Error("Unexpected mutation or playback: " + url);
  }) as typeof fetch;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const parent = createRootRoute();
  const route = (groupIds.length ? AlbumsRoute : SongsRoute).update({
    getParentRoute: () => parent,
    path: "/app/music",
  } as never);
  const router = createRouter({
    routeTree: parent.addChildren([route]),
    history: createMemoryHistory({ initialEntries: ["/app/music"] }),
  });
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
  await flush();
  await click("Identify album");
}
async function flush() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 20));
  });
}
async function click(text: string) {
  const el = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
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

test("unordered identified group remains searchable and permits explicit album-only approval", async () => {
  const group = "00000000-0000-4000-8000-000000000202";
  await mount(() => Response.json({ candidates: [candidate] }), [group], false, true);
  expect(
    container.querySelector<HTMLButtonElement>('button[aria-label="Play Example Release"]')!
      .disabled,
  ).toBe(true);
  await click("Find releases");
  await click("Review match");
  expect(container.textContent).toContain("Playback order changes only when explicitly selected");
  expect(requests.some((request) => request.url.endsWith("/approve"))).toBe(false);
  await click("Approve album information");
  expect(requests.find((request) => request.url.endsWith("/approve"))?.body).toEqual({
    folder: ".",
    album_group_id: group,
    release_id: candidate.release_id,
  });
  expect(
    container.querySelector<HTMLButtonElement>('button[aria-label="Play Example Release"]')!
      .disabled,
  ).toBe(true);
});
test("root Music search displays candidates, keeps explicit review and blocks unsafe root approval", async () => {
  await mount(() => Response.json({ candidates: [candidate] }));
  await click("Find releases");
  expect(requests.find((r) => r.url.endsWith("/search"))?.body).toEqual({
    folder: ".",
    artist: "Example Artist",
    album: "Example Release",
  });
  expect(container.textContent).toContain("Possible releases");
  expect(container.textContent).toContain("12 tracks");
  await click("Review match");
  expect(container.textContent).toContain("Album grouping is required");
  expect(requests.some((r) => r.url.endsWith("/approve"))).toBe(false);
});
test("Music search distinguishes no matches, validation failure and provider failure; clears stale candidates", async () => {
  let response = Response.json({ candidates: [candidate] });
  await mount(() => response);
  await click("Find releases");
  expect(container.textContent).toContain("Possible releases");
  response = Response.json({ candidates: [] });
  await click("Find releases");
  expect(container.textContent).toContain("No matching releases were found.");
  expect(container.textContent).not.toContain("Possible releases");
  response = Response.json({ detail: "Music album folder is invalid" }, { status: 422 });
  await click("Find releases");
  expect(container.textContent).toContain("Music album folder is invalid");
  expect(container.textContent).not.toContain("The online music catalogue could not be searched.");
  response = Response.json({ detail: "Provider unavailable" }, { status: 503 });
  await click("Find releases");
  expect(container.textContent).toContain("The online music catalogue is temporarily unavailable.");
});
test("Music search shows searching while provider response is pending", async () => {
  let finish!: (r: Response) => void;
  await mount(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  await click("Find releases");
  expect(container.textContent).toContain("Searching");
  const searching = [...container.querySelectorAll("button")].find(
    (b) => b.textContent?.trim() === "Searching",
  );
  expect(searching?.disabled).toBe(true);
  await act(async () => finish(Response.json({ candidates: [] })));
  await flush();
  expect(container.textContent).toContain("Find releases");
});

test("explicit album UUIDs keep identical display names separate and scope identification", async () => {
  const groups = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"];
  await mount(() => Response.json({ candidates: [candidate] }), groups);
  expect(
    [...container.querySelectorAll("button")].filter(
      (b) => b.textContent?.trim() === "Identify album",
    ),
  ).toHaveLength(2);
  await click("Find releases");
  expect(requests.find((r) => r.url.endsWith("/search"))?.body).toEqual({
    folder: ".",
    album_group_id: groups[0],
    artist: "Example Artist",
    album: "Example Release",
  });
});

test("Example Album album dialog emits the canonical grouped request and distinguishes failure classes", async () => {
  const group = "00000000-0000-4000-8000-000000000201";
  let response = Response.json({
    candidates: [{ ...candidate, title: "Example Album â€“ Act 1", track_count: 13 }],
  });
  await mount(() => response, [group], true);
  await click("Find releases");
  expect(requests.find((r) => r.url.endsWith("/search"))?.body).toEqual({
    folder: `Albums/${group}`,
    album_group_id: group,
    artist: "EXAMPLE ARTIST",
    album: "Example Album Act 1",
  });
  expect(container.textContent).toContain("13 tracks");
  for (const [status, text] of [
    [401, "Your session has expired"],
    [403, "You do not have permission"],
    [500, "because of a server error"],
    [502, "catalogue returned a technical error"],
    [503, "catalogue is temporarily unavailable"],
  ] as const) {
    response = Response.json({ detail: "internal provider detail" }, { status });
    await click("Find releases");
    expect(container.textContent).toContain(text);
    expect(container.textContent).not.toContain("Possible releases");
    expect(container.textContent).not.toContain("internal provider detail");
  }
});

test("release candidates expose edition, format and catalogue details without approving", async () => {
  await mount(
    () =>
      Response.json({
        candidates: [
          {
            ...candidate,
            track_count: 14,
            disambiguation: "international deluxe",
            formats: ["CD"],
            barcode: "0000000000000",
            catalog_numbers: ["EXAMPLE-001"],
          },
          {
            ...candidate,
            release_id: "00000000-0000-4000-8000-000000000203",
            title: "Example Album (Special Edition)",
            track_count: 15,
          },
        ],
      }),
    ["00000000-0000-4000-8000-000000000202"],
  );
  await click("Find releases");
  expect(container.textContent).toContain("Example Release (international deluxe)");
  expect(container.textContent).toContain("14 tracks · CD");
  expect(container.textContent).toContain("Barcode: 0000000000000");
  expect(container.textContent).toContain("Catalog: EXAMPLE-001");
  expect(container.textContent).toContain("Example Album (Special Edition)");
  expect(container.textContent).toContain("15 tracks");
  expect(requests.some((request) => request.url.endsWith("/approve"))).toBe(false);
});

test("owner reviews proposals, can leave unmatched and override without duplicate assignments", async () => {
  const group = "00000000-0000-4000-8000-000000000303";
  const preview = {
    ...candidate,
    review_revision: "synthetic-review",
    order_ready: false,
    folder: ".",
    genres: [],
    local_track_count: 2,
    matched_track_count: 1,
    unmatched_local_files: ["Deep Sea.wav"],
    tracks: [
      { disc_number: 1, track_number: 1, title: "Bright Sky", matched: true },
      { disc_number: 2, track_number: 1, title: "Deep Sea", matched: false },
    ],
    local_matches: [
      {
        asset_id: "local-a",
        filename: "Bright Sky.wav",
        proposed_track: "1:1",
        status: "confident",
        score: 101,
        reason: "Exact normalized filename title",
        local_duration: 180,
        provider_duration: 180.5,
      },
      {
        asset_id: "local-b",
        filename: "Deep Sea.wav",
        proposed_track: null,
        status: "review",
        score: 98,
        reason: "Exact normalized filename title",
        warning: "Large duration difference; owner review required",
        local_duration: 350,
        provider_duration: 180,
      },
    ],
  };
  await mount(() => Response.json({ candidates: [candidate] }), [group], false, false, preview);
  await click("Find releases");
  await click("Review match");
  expect(requests.some((r) => r.url.endsWith("/approve"))).toBe(false);
  expect(container.textContent).toContain("Large duration difference");
  expect(container.textContent).toContain("180.5 s");
  const first = container.querySelector<HTMLSelectElement>(
    'select[aria-label="Match Bright Sky.wav"]',
  )!;
  const second = container.querySelector<HTMLSelectElement>(
    'select[aria-label="Match Deep Sea.wav"]',
  )!;
  expect(second.querySelector<HTMLOptionElement>('option[value="1:1"]')!.disabled).toBe(true);
  await click("Leave all unmatched");
  expect(first.value).toBe("");
  await click("Use confident proposals");
  expect(first.value).toBe("1:1");
  await act(async () => {
    second.value = "2:1";
    second.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await click("Approve release and mapping");
  expect(requests.find((r) => r.url.endsWith("/approve"))?.body).toEqual({
    folder: ".",
    album_group_id: group,
    release_id: candidate.release_id,
    review_revision: "synthetic-review",
    apply_order: true,
    mapping: [
      { asset_id: "local-a", provider_track: "1:1" },
      { asset_id: "local-b", provider_track: "2:1" },
    ],
  });
});
