import { expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { TvResolverActions } from "../src/components/TvResolverActions";
import { normalizeTvResolverBatches, tvCounts } from "../src/lib/tv-resolver";

function batch(status = "published") {
  return normalizeTvResolverBatches({
    batches: [
      {
        id: "synthetic-batch",
        status,
        proposed_show_title: "Synthetic Show",
        seasons: [],
        tracks: Array.from({ length: 3 }, (_, index) => ({
          id: String(index),
          original_filename: `track-${index}.mkv`,
          classification: index < 2 ? "likely_episode" : "likely_extra",
          publication_state: index < 2 ? "published" : "needs_review",
        })),
      },
    ],
  }).batches[0];
}

test("published episodes expose only the explicit extras phase", () => {
  const value = batch();
  expect(tvCounts(value).published).toBe(2);
  expect(tvCounts(value).extrasRemaining).toBe(1);
  const html = renderToStaticMarkup(
    <TvResolverActions batch={value} busy={false} onAction={() => {}} />,
  );
  expect(html).not.toContain("Approve batch");
  expect(html).toContain("Publish extras");
});

test("failed extras retain successful episode progress and expose failed-only retry", () => {
  const value = batch("failed");
  value.tracks[2].publication_state = "failed";
  const html = renderToStaticMarkup(
    <TvResolverActions batch={value} busy={false} onAction={() => {}} />,
  );
  expect(tvCounts(value).published).toBe(2);
  expect(html).toContain("Retry failed");
  expect(html).not.toContain("Publish extras");
});

test("complete batches are absent from the active view model", () => {
  expect(
    normalizeTvResolverBatches({ batches: [{ ...batch(), status: "complete" }] }).batches,
  ).toEqual([]);
});
