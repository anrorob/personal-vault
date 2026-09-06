import { expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { TvResolverActions } from "../src/components/TvResolverActions";
import { normalizeTvResolverBatches, tvCounts } from "../src/lib/tv-resolver";

const batch = () =>
  normalizeTvResolverBatches({
    batches: [
      {
        id: "batch",
        status: "published",
        proposed_show_title: "Westworld",
        seasons: [],
        tracks: Array.from({ length: 99 }, (_, n) => ({
          id: String(n),
          original_filename: `track-${n}.mkv`,
          classification: n < 28 ? "likely_episode" : "likely_extra",
          publication_state: n < 28 ? "published" : "needs_review",
        })),
      },
    ],
  }).batches[0];

test("published episodes have no approval button and 71 extras await the explicit action", () => {
  const value = batch();
  expect(tvCounts(value).published).toBe(28);
  expect(tvCounts(value).extrasRemaining).toBe(71);
  const html = renderToStaticMarkup(
    <TvResolverActions batch={value} busy={false} onAction={() => {}} />,
  );
  expect(html).not.toContain("Approve batch");
  expect(html).toContain("Publish extras");
  expect(html).not.toContain("Retry failed");
});

test("a failed extra exposes retry and preserves successful counts", () => {
  const value = batch();
  value.status = "failed";
  value.tracks.forEach((track) => {
    track.publication_state = "published";
  });
  value.tracks[98].publication_state = "failed";
  expect(tvCounts(value).extrasRemaining).toBe(1);
  expect(tvCounts(value).failed).toBe(1);
  const html = renderToStaticMarkup(
    <TvResolverActions batch={value} busy={false} onAction={() => {}} />,
  );
  expect(html).toContain("Retry failed");
  expect(html).not.toContain("Publish extras");
  expect(html).not.toContain("Approve batch");
});

test("complete batches are absent from the active resolver view", () => {
  const value = batch();
  value.status = "complete";
  expect(normalizeTvResolverBatches({ batches: [value] }).batches).toEqual([]);
  expect(
    renderToStaticMarkup(<TvResolverActions batch={value} busy={false} onAction={() => {}} />),
  ).toBe("");
});
