import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { createRootRoute, createRouter, createMemoryHistory, RouterProvider } =
  await import("@tanstack/react-router");
const { PhotoDetails, Route: PhotoRoute } = await import("../src/routes/app.gallery.$photoId");
import type { GalleryImageDetails } from "../src/lib/gallery";

const photo = {
  id: "synthetic",
  asset_id: "asset",
  name: "receipt.jpg",
  size: 3200000,
  mime_type: "image/jpeg",
  sha256: "synthetic-hash",
  vault_path: "/vault/Gallery/receipt.jpg",
  display_title: "Receipt",
  can_edit: true,
  captured_on: "2024-02-03",
  captured_at: "source-date",
  location: "Test place",
  metadata_provenance: {
    display_title: "filename",
    captured_on: "embedded",
    location: "user_override",
  },
  intelligence: [],
  custom_tags: [],
  people: [],
  faces: [],
} as unknown as GalleryImageDetails;
const originalFetch = globalThis.fetch;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
let calls: Array<[string, RequestInit | undefined]>;
let updates: GalleryImageDetails[];
const sharing = {
  mode: "private",
  owner_username: "Synthetic owner",
  recipients: [],
  eligible_users: [],
  share_mode: "quick",
};
async function mount(displayedPhoto = photo, viewer = false) {
  calls = [];
  updates = [];
  globalThis.fetch = (async (input, options) => {
    const url = String(input);
    calls.push([url, options]);
    if (url.startsWith("/api/gallery/synthetic?")) return Response.json(displayedPhoto);
    if (url.endsWith("/sharing")) return Response.json(sharing);
    if (url.endsWith("/metadata"))
      return Response.json({ ...photo, metadata_provenance: photo.metadata_provenance });
    if (url.endsWith("/ai"))
      return Response.json({
        jobs: [],
        suggestions: [],
        visual_description: { caption: "Synthetic description", status: "completed" },
      });
    if (url.endsWith("/section-move/destinations"))
      return Response.json({ destinations: ["Documents", "Archives"] });
    if (url.endsWith("/preflight")) return Response.json({ ready: true });
    if (url.endsWith("/section-move"))
      return Response.json({ status: "completed", operation_id: "operation" });
    if (url.endsWith("/status")) return Response.json({ job: null });
    if (url.endsWith("/people/settings")) return Response.json({});
    return Response.json([]);
  }) as typeof fetch;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const route = createRootRoute({
    component: () => (
      <PhotoDetails
        photo={displayedPhoto}
        onUpdated={(value) => updates.push(value)}
        sort="newest"
        faceIdentificationMode={false}
        setFaceIdentificationMode={() => {}}
        selectedFaceId={null}
        setSelectedFaceId={() => {}}
      />
    ),
  });
  const parent = createRootRoute();
  const viewerRoute = viewer
    ? PhotoRoute.update({ getParentRoute: () => parent, path: "/app/gallery/$photoId" } as never)
    : null;
  const router = createRouter({
    routeTree: viewerRoute ? parent.addChildren([viewerRoute]) : route,
    history: createMemoryHistory({ initialEntries: [viewer ? "/app/gallery/synthetic" : "/"] }),
  });
  await act(async () => {
    await router.load();
    root.render(<RouterProvider router={router} />);
  });
}
afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
});
const button = (name: string, scope: ParentNode = document) =>
  [...scope.querySelectorAll("button")].find((node) => node.textContent?.trim() === name)!;

test("details display the API effective source date without requiring EXIF", async () => {
  await mount(
    { ...photo, captured_on: "2018-06-12", captured_at: null, date_source: "source_file_created" },
    true,
  );
  expect(container.textContent).toContain("12 Jun 2018");
  expect(container.textContent).not.toContain("Date not recorded");
});
async function click(name: string, scope?: ParentNode) {
  const node = button(name, scope);
  expect(node).toBeDefined();
  await act(async () => {
    node.focus();
    node.click();
  });
}

test("real details hierarchy places Florence before Intelligence and People, without diagnostics", async () => {
  await mount();
  const header = container.querySelector('header[aria-label="Photo actions"]')!;
  expect(header.textContent).toContain("receipt.jpg \u00b7");
  expect(header.textContent).toContain("MB");
  expect([...header.querySelectorAll("button")].map((node) => node.textContent?.trim())).toEqual([
    "Edit metadata",
    "Manage sharing",
    "Options",
  ]);
  for (const diagnostic of [
    "/vault/",
    "image/jpeg",
    "synthetic-hash",
    "Metadata provenance",
    "Use the arrow keys",
  ])
    expect(container.textContent).not.toContain(diagnostic);
  const headings = [...container.querySelectorAll("h2")].map((node) => node.textContent);
  expect(headings[1]).toBe("Florence visual description");
  expect(container.textContent).toContain("Synthetic description");
  expect(headings[2]).toBe("Gallery Intelligence");
  expect(headings).toContain("People");
  const intelligence = [...container.querySelectorAll("section")].find(
    (node) => node.querySelector("h2")?.textContent === "Gallery Intelligence",
  );
  expect(intelligence?.textContent).toContain("My private tags");
  expect(intelligence?.textContent).toContain("Visible only to you.");
  expect(document.querySelector('[role="dialog"]')).toBeNull();
});

test("all top actions use the viewport dialog and restore trigger focus without scrolling", async () => {
  await mount();
  window.scrollTo(0, 480);
  expect(window.scrollY).toBe(480);
  for (const name of ["Edit metadata", "Manage sharing", "Options"]) {
    const before = window.scrollY;
    await click(name);
    const dialog = document.querySelector('[role="dialog"]')!;
    expect(dialog).not.toBeNull();
    expect(container.contains(dialog)).toBe(false);
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(dialog.className).toContain("overflow-y-auto");
    const outside = document.createElement("button");
    document.body.append(outside);
    await act(async () => outside.focus());
    expect(dialog.contains(document.activeElement)).toBe(true);
    outside.remove();
    await act(async () =>
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
    );
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(document.activeElement === button(name)).toBe(true);
    expect(window.scrollY).toBe(before);
  }
});

test("Hide retains the existing lifecycle action", async () => {
  await mount();
  await click("Options");
  await click("Hide", document.querySelector('[role="dialog"]')!);
  expect(
    calls.some(([url, options]) => url.endsWith("/lifecycle/hide") && options?.method === "POST"),
  ).toBe(true);
});

test("metadata and sharing keep their real save and cancel endpoints", async () => {
  await mount();
  await click("Edit metadata");
  let dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.textContent).toContain("Display title");
  expect(dialog.textContent).toContain("Capture date");
  expect(dialog.textContent).toContain("Location");
  await click("Cancel", dialog);
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  await click("Edit metadata");
  const titleInput = document.querySelector('[role="dialog"] input') as HTMLInputElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(
      titleInput,
      "Corrected title",
    );
    titleInput.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await click("Save correction", document.querySelector('[role="dialog"]')!);
  expect(
    calls.some(([url, options]) => url.endsWith("/metadata") && options?.method === "PATCH"),
  ).toBe(true);
  expect(updates.length).toBe(1);
  await click("Manage sharing");
  dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.textContent).toContain("Share in my Vault — Everyone");
  expect(dialog.textContent).toContain("Share in my Vault — Specific people");
  await click("Cancel", dialog);
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  await click("Manage sharing");
  await click("Save sharing", document.querySelector('[role="dialog"]')!);
  expect(
    calls.some(([url, options]) => url.endsWith("/sharing") && options?.method === "PUT"),
  ).toBe(true);
});

test("Options exposes diagnostics and logical Move, with no per-photo analysis or raw paths", async () => {
  await mount();
  await click("Options");
  let dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.textContent).toContain("Hide");
  expect(dialog.textContent).not.toContain("Analyse");
  await click("Technical details", dialog);
  dialog = document.querySelector('[role="dialog"]')!;
  for (const value of [
    photo.vault_path,
    photo.mime_type,
    photo.sha256,
    "Metadata provenance",
    "filename",
    "embedded",
  ])
    expect(dialog.textContent).toContain(value);
  await click("Close", dialog);
  await click("Options");
  await click("Move");
  dialog = document.querySelector('[role="dialog"]')!;
  expect([...dialog.querySelectorAll("option")].map((node) => node.textContent)).toEqual([
    "Documents",
    "Archives",
  ]);
  expect(dialog.querySelector("input")).toBeNull();
  expect(dialog.textContent).not.toContain("/vault/");
  await click("Continue", dialog);
  await click("Confirm move to Documents", dialog);
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("Moved to Documents");
  const move = calls.find(
    ([url, options]) => url.endsWith("/section-move") && options?.method === "POST",
  );
  expect(JSON.parse(String(move?.[1]?.body))).toEqual({ destination: "Documents", confirm: true });
});
