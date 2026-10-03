import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { act } from "react";
import { readFileSync } from "node:fs";
import { GalleryFilters } from "../src/routes/app.gallery.index";
import { GalleryIntelligenceMetadata } from "../src/routes/app.gallery.$photoId";
import type { GalleryImageDetails, GalleryIntelligenceTerm } from "../src/lib/gallery";

if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { createRoot } = await import("react-dom/client");
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
const originalFetch = globalThis.fetch;
afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
});
function mount() {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
}
// Read the backend-owned definition, so the UI test does not create another product taxonomy.
const taxonomySource = readFileSync(
  new URL("../backend/app/gallery_taxonomy.py", import.meta.url),
  "utf8",
);
const terms = [
  ...taxonomySource.matchAll(/\("(photo_type|content_tag)", "([^"]+)", "([^"]+)"\)/g),
].map((match) => ({
  namespace: match[1],
  slug: match[2],
  display_name: match[3],
})) as GalleryIntelligenceTerm[];

test("Gallery Filter renders canonical groups separately from every private tag, including unused", async () => {
  mount();
  let selected: unknown;
  await act(async () =>
    root.render(
      <GalleryFilters
        terms={terms}
        photoTypes={[]}
        contentTags={[]}
        people={[]}
        selectedPeople={[]}
        privateTags={[
          { id: "private-bikes", slug: "bikes", display_name: "Bikes" },
          { id: "private-unused", slug: "unused", display_name: "Unused" },
        ]}
        selectedPrivateTags={[]}
        onChange={async (...args) => {
          selected = args;
        }}
      />,
    ),
  );
  const groups = [...container.querySelectorAll("fieldset")];
  expect(groups.map((group) => group.querySelector("legend")?.textContent)).toEqual([
    "Photo type",
    "Tags",
    "My private tags",
  ]);
  expect(groups[0].querySelectorAll("input").length).toBe(10);
  expect(groups[1].querySelectorAll("input").length).toBe(7);
  expect(groups[0].textContent).not.toContain("Bikes");
  expect(groups[1].textContent).not.toContain("Unused");
  expect(groups[2].textContent).toContain("Visible only to you.");
  await act(async () => (groups[2].querySelectorAll("input")[1] as HTMLInputElement).click());
  expect(selected).toEqual([[], [], [], ["private-unused"]]);
});

test("private tags are a compact subsection inside the actual Gallery Intelligence component", async () => {
  mount();
  globalThis.fetch = (async () => Response.json(terms)) as typeof fetch;
  const photo = {
    id: "synthetic",
    can_edit: true,
    intelligence: [],
    custom_tags: [{ id: "private", slug: "bikes", display_name: "Bikes" }],
  } as unknown as GalleryImageDetails;
  await act(async () =>
    root.render(
      <GalleryIntelligenceMetadata
        photo={photo}
        onUpdated={() => {}}
        job={null}
        queueing={false}
        analysisError={null}
        onRetry={() => {}}
      />,
    ),
  );
  expect(container.querySelectorAll("section").length).toBe(1);
  const section = container.querySelector("section")!;
  expect(section.querySelector("h2")?.textContent).toBe("Gallery Intelligence");
  expect([...section.querySelectorAll("h3")].map((node) => node.textContent)).toEqual([
    "Photo type",
    "Content tags",
    "My private tags",
  ]);
  expect(section.textContent).toContain("Visible only to you.");
  expect(section.textContent).toContain("Bikes");
  expect(section.querySelector('input[placeholder="Add private tag"]')).not.toBeNull();
  const selects = section.querySelectorAll("select");
  expect(selects[0].options.length).toBe(11);
  expect(selects[1].options.length).toBe(8);
});
