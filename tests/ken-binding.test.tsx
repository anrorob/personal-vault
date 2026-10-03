import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { act } from "react";
import { readFileSync } from "node:fs";
import { useHomeVideoDetails } from "../src/lib/use-home-video-details";
import { KenAnalysis } from "../src/components/pv/KenAnalysis";

if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { createRoot } = await import("react-dom/client");
const originalFetch = globalThis.fetch;
let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLDivElement;
type Pending = { url: string; resolve: (response: Response) => void };
let pending: Pending[];

afterEach(async () => {
  await act(async () => root?.unmount());
  root = null;
  container?.remove();
  globalThis.fetch = originalFetch;
});

function setup(modes = ["native_video"]) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  pending = [];
  globalThis.fetch = ((url: string) => {
    if (url.endsWith("/corrections")) return Promise.resolve(Response.json([]));
    if (url === "/api/personal-videos/ken/engine")
      return Promise.resolve(
        Response.json({
          display_name: "Synthetic KEN",
          status: "available",
          supported_input_modes: modes,
        }),
      );
    // Deliberately ignore AbortSignal: guards must work even after a response has arrived.
    return new Promise<Response>((resolve) => pending.push({ url, resolve }));
  }) as typeof fetch;
}

async function resolve(url: string, value: unknown) {
  const index = pending.findIndex((request) => request.url === url);
  expect(index).toBeGreaterThanOrEqual(0);
  const [request] = pending.splice(index, 1);
  await act(async () => request.resolve(Response.json(value)));
}

function fixture(asset: string) {
  return [
    {
      id: `run-${asset}`,
      asset_id: asset,
      input_mode: "native_video",
      status: "completed",
      created_at: "2026-09-05T00:00:00Z",
      processing_ms: 1,
      configuration: {
        display_name: "Synthetic KEN",
        runtime: "fake",
        correction_version: "ken-corrections-v1",
        correction_fingerprint: "synthetic-corrections",
        model_revision: "fake",
        result_binding_version: "v1",
        input_integrity: { input_fingerprint: asset, manifest: { asset_id: asset } },
      },
      result: {
        asset_id: asset,
        run_id: `run-${asset}`,
        input_fingerprint: asset,
        correction_fingerprint: "synthetic-corrections",
        description: `SYNTHETIC_RESULT_${asset}`,
        warnings: [],
        raw_response: "",
      },
    },
  ];
}

function Harness({ fileId }: { fileId: string | null }) {
  const { details } = useHomeVideoDetails<{ file_id: string; asset_id: string }>(fileId);
  return <div data-selected={fileId}>{details && <KenAnalysis assetId={details.asset_id} />}</div>;
}

test("failed KEN details show a bounded reason and use the one-retry endpoint", async () => {
  setup();
  const failed = {
    ...fixture("A")[0],
    status: "failed",
    result: null,
    error: "Interrupted by worker restart",
    failure: {
      message: "Analysis was interrupted by a worker restart.",
      retryable: true,
      retry_allowed: true,
      failed_at: "2026-09-13T00:00:00Z",
    },
  };
  await act(async () => root!.render(<KenAnalysis assetId="A" />));
  await resolve("/api/personal-videos/ken/assets/A/runs", [failed]);
  expect(container.textContent).toContain("One controlled retry is available");
  expect(container.textContent).toContain(failed.failure.message);
  const retry = [...container.querySelectorAll("button")].find(
    (b) => b.textContent === "Retry analysis once",
  )!;
  expect(retry.disabled).toBe(false);
  await act(async () => retry.click());
  expect(pending.some((p) => p.url === "/api/personal-videos/ken/assets/A/runs/run-A/retry")).toBe(
    true,
  );
});

test("nonretryable KEN failure does not offer another analysis", async () => {
  setup();
  await act(async () => root!.render(<KenAnalysis assetId="A" />));
  await resolve("/api/personal-videos/ken/assets/A/runs", [
    {
      ...fixture("A")[0],
      status: "failed",
      result: null,
      error: "Synthetic error",
      failure: {
        message: "Video input preparation failed.",
        retryable: false,
        retry_allowed: false,
        failed_at: null,
      },
    },
  ]);
  expect(container.textContent).toContain("Technical review is required");
  const retry = [...container.querySelectorAll("button")].find(
    (b) => b.textContent === "Retry analysis once",
  )!;
  expect(retry.disabled).toBe(true);
});

test("the native-video engine initially selects native video", async () => {
  setup(["native_video"]);
  await act(async () => root!.render(<KenAnalysis assetId="A" />));
  await resolve("/api/personal-videos/ken/assets/A/runs", []);
  expect(container.querySelectorAll("select").length).toBe(0);
  const analyse = [...container.querySelectorAll("button")].find(
    (button) => button.textContent === "Analyse",
  );
  expect(analyse?.disabled).toBe(false);
  expect(container.textContent).not.toContain("Experimental");
  expect(container.textContent).not.toContain("Select a candidate");
  expect(container.querySelector('[aria-label="Active KEN engine"]')).not.toBeNull();
});

test("a late A details response cannot mount A's KEN under selected video B", async () => {
  setup();
  await act(async () => root!.render(<Harness fileId="file-A" />));
  await act(async () => root!.render(<Harness fileId="file-B" />));
  await resolve("/api/personal-videos/file-B/details", { file_id: "file-B", asset_id: "B" });
  await resolve("/api/personal-videos/ken/assets/B/runs", fixture("B"));
  await resolve("/api/personal-videos/file-A/details", { file_id: "file-A", asset_id: "A" });
  expect(container.textContent).toContain("SYNTHETIC_RESULT_B");
  expect(container.textContent).not.toContain("SYNTHETIC_RESULT_A");
  expect(pending.some((request) => request.url.includes("/assets/A/"))).toBe(false);
});

test("switching videos clears displayed runs immediately and ignores A's late refresh", async () => {
  setup();
  await act(async () => root!.render(<KenAnalysis assetId="A" />));
  await resolve("/api/personal-videos/ken/assets/A/runs", fixture("A"));
  expect(container.textContent).toContain("SYNTHETIC_RESULT_A");
  const refresh = [...container.querySelectorAll("button")].find((button) =>
    button.textContent?.includes("Refresh"),
  )!;
  await act(async () => refresh.click());
  await act(async () => root!.render(<KenAnalysis assetId="B" />));
  expect(container.textContent).not.toContain("SYNTHETIC_RESULT_A");
  await resolve("/api/personal-videos/ken/assets/B/runs", fixture("B"));
  await resolve("/api/personal-videos/ken/assets/A/runs", fixture("A"));
  expect(container.textContent).toContain("SYNTHETIC_RESULT_B");
  expect(container.textContent).not.toContain("SYNTHETIC_RESULT_A");
});

test("the selected file rejects details cached for another file", async () => {
  setup();
  await act(async () => root!.render(<Harness fileId="file-B" />));
  await resolve("/api/personal-videos/file-B/details", { file_id: "file-A", asset_id: "A" });
  expect(container.querySelector('[aria-label="KEN"]')).toBeNull();
});

test.each(["asset", "run", "input", "row", "correction"])(
  "KEN rejects mismatched %s binding without displaying the result",
  async (field) => {
    setup();
    await act(async () => root!.render(<KenAnalysis assetId="B" />));
    const rows = fixture("B");
    if (field === "asset") rows[0].result.asset_id = "A";
    if (field === "run") rows[0].result.run_id = "run-A";
    if (field === "input") rows[0].result.input_fingerprint = "A";
    if (field === "row") rows[0].asset_id = "A";
    if (field === "correction") rows[0].result.correction_fingerprint = "wrong-correction-set";
    await resolve("/api/personal-videos/ken/assets/B/runs", rows);
    expect(container.textContent).not.toContain("SYNTHETIC_RESULT_");
    expect(container.textContent).toContain("identity mismatch");
  },
);

test("production Home Videos page uses the guarded selection hook", () => {
  const source = readFileSync(
    new URL("../src/routes/app.personal-videos.tsx", import.meta.url),
    "utf8",
  );
  expect(source).toContain("useHomeVideoDetails<VideoDetails>(selected?.id ?? null)");
  expect(source).not.toContain("setDetails(");
});

test("old phase results and reset responses cannot restore a stale draft", async () => {
  setup();
  await act(async () => root!.render(<KenAnalysis assetId="A" />));
  const old = fixture("A");
  old[0].configuration.correction_version = "old-candidate-phase";
  await resolve("/api/personal-videos/ken/assets/A/runs", old);
  expect(container.textContent).toContain("Not analysed");
  expect(container.textContent).not.toContain("SYNTHETIC_RESULT_A");
  const refresh = [...container.querySelectorAll("button")].find((button) =>
    button.textContent?.includes("Refresh"),
  )!;
  await act(async () => refresh.click());
  await resolve("/api/personal-videos/ken/assets/A/runs", fixture("A"));
  expect(container.textContent).toContain("Adjust with KEN");
  await act(async () => refresh.click());
  await resolve("/api/personal-videos/ken/assets/A/runs", []);
  expect(container.textContent).toContain("Not analysed");
  expect(container.textContent).not.toContain("SYNTHETIC_RESULT_A");
  expect(container.querySelector("textarea")).toBeNull();
});

test("correction history is asset-scoped and clears when switching videos", async () => {
  setup();
  const fallback = globalThis.fetch;
  globalThis.fetch = ((url: string, options?: RequestInit) => {
    if (url === "/api/personal-videos/ken/assets/A/corrections")
      return Promise.resolve(
        Response.json([{ id: "c1", asset_id: "A", sequence: 1, text: "SYNTHETIC_CORRECTION_A" }]),
      );
    return fallback(url, options);
  }) as typeof fetch;
  await act(async () => root!.render(<KenAnalysis assetId="A" />));
  await resolve("/api/personal-videos/ken/assets/A/runs", fixture("A"));
  expect(container.textContent).toContain("SYNTHETIC_CORRECTION_A");
  await act(async () => root!.render(<KenAnalysis assetId="B" />));
  await resolve("/api/personal-videos/ken/assets/B/runs", fixture("B"));
  expect(container.textContent).not.toContain("SYNTHETIC_CORRECTION_A");
});

test("correction response for another asset fails closed", async () => {
  setup();
  const fallback = globalThis.fetch;
  globalThis.fetch = ((url: string, options?: RequestInit) =>
    url.endsWith("/corrections")
      ? Promise.resolve(
          Response.json([{ id: "c1", asset_id: "B", sequence: 1, text: "WRONG_CORRECTION" }]),
        )
      : fallback(url, options)) as typeof fetch;
  await act(async () => root!.render(<KenAnalysis assetId="A" />));
  await resolve("/api/personal-videos/ken/assets/A/runs", fixture("A"));
  expect(container.textContent).not.toContain("WRONG_CORRECTION");
  expect(container.textContent).not.toContain("SYNTHETIC_RESULT_A");
  expect(container.textContent).toContain("Correction identity mismatch");
});

test("Regenerate submits the current draft and preserves the adjustment with revised history", async () => {
  setup(["native_video"]);
  const fallback = globalThis.fetch;
  let submitted: { parent_run_id: string; text: string } | undefined;
  globalThis.fetch = ((url: string, options?: RequestInit) => {
    if (url.endsWith("/corrections") && options?.method === "POST") {
      submitted = JSON.parse(String(options.body));
      return Promise.resolve(Response.json({ id: "revision-A" }, { status: 202 }));
    }
    if (url.endsWith("/corrections") && submitted)
      return Promise.resolve(
        Response.json([{ id: "c1", asset_id: "A", sequence: 1, text: submitted.text }]),
      );
    return fallback(url, options);
  }) as typeof fetch;
  await act(async () => root!.render(<KenAnalysis assetId="A" />));
  await resolve("/api/personal-videos/ken/assets/A/runs", fixture("A"));
  const area = container.querySelector("textarea")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(
      area,
      "The camera moves.",
    );
    area.dispatchEvent(new Event("input", { bubbles: true }));
    area.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const button = [...container.querySelectorAll("button")].find(
    (b) => b.textContent === "Regenerate",
  )!;
  expect(button.disabled).toBe(false);
  await act(async () => button.click());
  expect(submitted).toEqual({ parent_run_id: "run-A", text: "The camera moves." });
  const revised = fixture("A");
  revised[0].id = revised[0].result.run_id = "revision-A";
  revised[0].result.description = "SYNTHETIC_REVISED_DRAFT";
  await resolve("/api/personal-videos/ken/assets/A/runs", revised);
  expect(container.textContent).toContain("SYNTHETIC_REVISED_DRAFT");
  expect(container.textContent).toContain("Corrections (1)");
  expect(area.value).toBe("The camera moves.");
});

import { KenTitles } from "../src/components/pv/KenTitles";

test("title generation is explicit, Use title updates the parent, and manual titles are protected", async () => {
  setup();
  const calls: string[] = [];
  let changed = "";
  globalThis.fetch = (async (url: string, options?: RequestInit) => {
    calls.push(`${options?.method ?? "GET"} ${url}`);
    if (!options?.method)
      return Response.json({
        asset_id: "A",
        display_title: "Filename",
        manual_title: false,
        suggestions: [],
      });
    if (url.endsWith("/titles")) {
      expect(JSON.parse(options.body as string).run_id).toBe("run-A");
      return Response.json({
        id: "title-A",
        asset_id: "A",
        run_id: "run-A",
        title: "Synthetic Pool Challenge",
      });
    }
    return Response.json({ asset_id: "A", display_title: "Synthetic Pool Challenge" });
  }) as typeof fetch;
  await act(async () =>
    root!.render(
      <KenTitles
        assetId="A"
        runId="run-A"
        disabled={false}
        onTitleChange={(title) => {
          changed = title;
        }}
      />,
    ),
  );
  expect(calls.length).toBe(1);
  await act(async () =>
    Array.from(container.querySelectorAll("button"))
      .find((b) => b.textContent === "Generate title from this draft")!
      .click(),
  );
  expect(container.textContent).toContain("Synthetic Pool Challenge");
  expect(changed).toBe("");
  await act(async () =>
    Array.from(container.querySelectorAll("button"))
      .find((b) => b.textContent === "Use title")!
      .click(),
  );
  expect(changed).toBe("Synthetic Pool Challenge");
  await act(async () =>
    Array.from(container.querySelectorAll("button"))
      .find((b) => b.textContent === "Save manual title")!
      .click(),
  );
  expect(
    Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Use title")!
      .disabled,
  ).toBe(true);
});

test("late title response cannot cross video boundaries", async () => {
  setup();
  await act(async () =>
    root!.render(<KenTitles key="A" assetId="A" runId="run-A" disabled={false} />),
  );
  await act(async () =>
    root!.render(<KenTitles key="B" assetId="B" runId="run-B" disabled={false} />),
  );
  await resolve("/api/personal-videos/ken/assets/B/titles", {
    asset_id: "B",
    display_title: "B",
    manual_title: false,
    suggestions: [],
  });
  await resolve("/api/personal-videos/ken/assets/A/titles", {
    asset_id: "A",
    display_title: "STALE_TITLE_A",
    manual_title: false,
    suggestions: [],
  });
  expect(container.textContent).not.toContain("STALE_TITLE_A");
  expect(container.querySelector("input")!.value).toBe("B");
});

test("KEN trusted context disclosure includes location without leaking across assets", async () => {
  setup();
  await act(async () => root!.render(<KenAnalysis assetId="A" />));
  const runs = fixture("A");
  Object.assign(runs[0].configuration, {
    correction_context: {
      trusted_people: [{ person_id: "p1", name: "Synthetic Ada" }],
      trusted_location: { name: "Greece", source: "embedded" },
    },
  });
  await resolve("/api/personal-videos/ken/assets/A/runs", runs);
  expect(container.textContent).toContain("Trusted context supplied to KEN");
  expect(container.textContent).toContain("Location: Greece");
  expect(container.textContent).toContain("People: Synthetic Ada");
  await act(async () => root!.render(<KenAnalysis assetId="B" />));
  expect(container.textContent).not.toContain("Greece");
  expect(container.textContent).not.toContain("Synthetic Ada");
});

test("accepted run status is visible beside controls before history refresh completes", async () => {
  setup(["native_video"]);
  await act(async () => root!.render(<KenAnalysis assetId="A" />));
  await resolve("/api/personal-videos/ken/assets/A/runs", []);
  const analyse = [...container.querySelectorAll("button")].find(
    (b) => b.textContent === "Analyse",
  )!;
  await act(async () => analyse.click());
  const queued = { ...fixture("A")[0], status: "queued", result: null };
  await resolve("/api/personal-videos/ken/assets/A/runs", queued);
  expect(container.querySelector('[aria-label="KEN analysis status"]')?.textContent).toContain(
    "Queued",
  );
  await resolve("/api/personal-videos/ken/assets/A/runs", [{ ...queued, status: "analysing" }]);
  expect(container.querySelector('[aria-label="KEN analysis status"]')?.textContent).toContain(
    "Analysing",
  );
  const refresh = [...container.querySelectorAll("button")].find((b) =>
    b.textContent?.startsWith("Refresh results"),
  )!;
  await act(async () => refresh.click());
  await resolve("/api/personal-videos/ken/assets/A/runs", [
    { ...queued, status: "failed", error: "Synthetic input failure" },
  ]);
  expect(container.querySelector('[aria-label="KEN analysis status"]')?.textContent).toContain(
    "Failed",
  );
  expect(container.querySelector('[aria-label="KEN analysis status"]')?.textContent).toContain(
    "Synthetic input failure",
  );
});

test("completed KEN revisions refresh normal metadata once per completion/title state", async () => {
  setup(["native_video"]);
  let reloads = 0;
  const reload = async () => {
    reloads++;
  };
  await act(async () => root!.render(<KenAnalysis assetId="A" onMetadataChange={reload} />));
  await resolve("/api/personal-videos/ken/assets/A/runs", fixture("A"));
  expect(reloads).toBe(1);
  const refresh = [...container.querySelectorAll("button")].find((b) =>
    b.textContent?.startsWith("Refresh"),
  )!;
  await act(async () => refresh.click());
  await resolve("/api/personal-videos/ken/assets/A/runs", fixture("A"));
  expect(reloads).toBe(1);
  await act(async () => refresh.click());
  const titled = fixture("A");
  Object.assign(titled[0].configuration, { automatic_title: { status: "completed" } });
  await resolve("/api/personal-videos/ken/assets/A/runs", titled);
  expect(reloads).toBe(2);
});
