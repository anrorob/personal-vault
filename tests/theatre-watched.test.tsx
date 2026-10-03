import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { PlaybackStateIndicator, WatchedAction } =
  await import("../src/components/pv/PlaybackState");
const { useTheatreProgress, resumePosition } = await import("../src/lib/theatre-progress");
const originalFetch = globalThis.fetch;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  globalThis.fetch = originalFetch;
});
for (const collection of ["movies", "tv-episodes"] as const) {
  test(`${collection} shows progress and performs reversible viewer watched actions`, async () => {
    let record = {
      position_seconds: 125,
      duration_seconds: 600,
      completed: false,
      state: "in_progress" as "in_progress" | "watched",
    };
    const urls: string[] = [];
    globalThis.fetch = (async (url: unknown, init?: RequestInit) => {
      urls.push(String(url));
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        record = {
          ...record,
          completed: body.watched,
          state: body.watched ? "watched" : "in_progress",
        };
        return Response.json(record);
      }
      return Response.json({ example: record });
    }) as typeof fetch;
    function Harness() {
      const { progress, pending, mark } = useTheatreProgress(collection);
      return (
        <>
          <PlaybackStateIndicator progress={progress.example} />
          <WatchedAction
            progress={progress.example}
            pending={pending}
            onChange={(watched) => void mark("example", watched)}
          />
          <span data-resume={resumePosition(progress.example)} />
        </>
      );
    }
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root.render(<Harness />));
    expect(container.textContent).toContain("In Progress");
    expect(container.querySelector("progress")).not.toBeNull();
    expect(container.querySelector("[data-resume]")?.getAttribute("data-resume")).toBe("125");
    await act(async () => container.querySelector("button")!.click());
    expect(container.textContent).toContain("Watched");
    expect(container.textContent).toContain("Mark as unwatched");
    expect(container.querySelector("progress")).toBeNull();
    expect(container.querySelector("[data-resume]")?.getAttribute("data-resume")).toBe("0");
    await act(async () => container.querySelector("button")!.click());
    expect(container.textContent).toContain("In Progress");
    expect(record.position_seconds).toBe(125);
    expect(urls.at(-1)).toBe(`/api/user-state/${collection}/example/watched`);
  });
}
test("Unwatched has no watched indicator or Continue eligibility", async () => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const progress = {
    position_seconds: 0,
    duration_seconds: 600,
    completed: false,
    state: "unwatched" as const,
  };
  await act(async () => root.render(<PlaybackStateIndicator progress={progress} />));
  expect(container.textContent).toBe("");
  expect(resumePosition(progress)).toBe(0);
});
