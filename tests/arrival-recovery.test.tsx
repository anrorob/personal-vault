import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { ArrivalRecovery } = await import("../src/components/ArrivalRecovery");
const { isActiveArrivalState } = await import("../src/lib/incoming");
const originalFetch = globalThis.fetch;
const originalConfirm = window.confirm;
test("completed and reconciled intake disappears from active staging while unfinished records remain", () => {
  for (const state of ["moved", "arrival_removed", "duplicate_removed"])
    expect(isActiveArrivalState(state)).toBe(false);
  for (const state of [
    undefined,
    "needs_review",
    "theatre_promotion_pending",
    "moving",
    "move_failed",
  ])
    expect(isActiveArrivalState(state)).toBe(true);
});
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
let posts: { action: string; item_ids: string[] }[];
async function mount() {
  posts = [];
  globalThis.fetch = (async (_url, options) => {
    if (options?.method === "POST") {
      posts.push(JSON.parse(String(options.body)));
      return Response.json({
        outcomes: [
          { item_id: "safe", processed: true, message: "Removed" },
          { item_id: "uncertain", processed: false, message: "Publication cannot be verified" },
        ],
      });
    }
    return Response.json({
      items: [
        {
          item_id: "safe",
          filename: "synthetic.wma",
          state: "theatre_promotion_pending",
          status: "unpublished",
          message: "Unpublished",
          can_remove: true,
          can_retry: true,
        },
        {
          item_id: "uncertain",
          filename: "missing.wma",
          state: "moving",
          status: "needs_recovery",
          message: "Publication cannot be verified",
          can_remove: false,
          can_retry: false,
        },
      ],
    });
  }) as typeof fetch;
  window.confirm = () => true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root.render(<ArrivalRecovery onChanged={async () => {}} />);
  });
}
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
  window.confirm = originalConfirm;
});
function button(text: string) {
  return [...container.querySelectorAll("button")].find((item) => item.textContent === text)!;
}
test("persisted missing-source intake stays visible with Recheck; only safe staging offers removal", async () => {
  await mount();
  expect(container.textContent).toContain("missing.wma");
  expect(container.textContent).toContain("Needs recovery");
  expect(
    [...container.querySelectorAll("button")].filter(
      (item) => item.textContent === "Remove from Arrival Hall",
    ),
  ).toHaveLength(1);
  expect(
    [...container.querySelectorAll("button")].filter(
      (item) => item.textContent === "Recheck / reconcile",
    ),
  ).toHaveLength(2);
  await act(async () => button("Retry safe move").click());
  expect(posts[0]).toMatchObject({ action: "retry", item_ids: ["safe"] });
});
test("mixed selection sends explicit removal and reports each skipped unsafe item", async () => {
  await mount();
  await act(async () => {
    container
      .querySelectorAll<HTMLInputElement>('input[type="checkbox"]')
      .forEach((input) => input.click());
  });
  await act(async () => button("Remove selected staging").click());
  expect(posts[0]).toMatchObject({
    action: "remove",
    item_ids: ["safe", "uncertain"],
    confirmation: "REMOVE FROM ARRIVAL HALL",
  });
  expect(container.querySelector('[role="status"]')?.textContent).toContain(
    "1 processed; 1 need attention",
  );
  expect(container.textContent).toContain("missing.wma: Publication cannot be verified");
});
test("cancelled confirmation sends no destructive request and does not move page scroll", async () => {
  await mount();
  window.confirm = () => false;
  document.documentElement.scrollTop = 320;
  await act(async () => button("Remove from Arrival Hall").click());
  expect(posts).toHaveLength(0);
  expect(document.documentElement.scrollTop).toBe(320);
});
