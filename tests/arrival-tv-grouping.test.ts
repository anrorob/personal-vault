import { expect, test } from "bun:test";
import { normalizeTvResolverBatches, tvGroupedEpisodeIds } from "../src/lib/tv-resolver";

function listing() {
  return normalizeTvResolverBatches({
    batches: [
      {
        id: "example-season-review",
        status: "proposed",
        proposed_show_title: "Example Show",
        seasons: [{ season_number: 1, episode_candidate_count: 10 }],
        tracks: Array.from({ length: 10 }, (_, index) => ({
          id: `resolver-track-${index}`,
          arrival_item_id: `arrival-episode-${index}`,
          original_filename: `Example Show S01E${String(index + 1).padStart(2, "0")}.mp4`,
          classification: "likely_episode",
          publication_state: "proposed",
        })),
      },
    ],
  }).batches;
}

test("ten staged episodes appear through one season review while movies and another upload remain individual", () => {
  const batches = listing();
  const grouped = tvGroupedEpisodeIds(batches);
  const receivedIds = [
    ...Array.from({ length: 10 }, (_, index) => `arrival-episode-${index}`),
    "arrival-movie",
    "arrival-other-show",
  ];
  expect(batches).toHaveLength(1);
  expect(batches[0].tracks).toHaveLength(10);
  expect(receivedIds.filter((id) => !grouped.has(id))).toEqual([
    "arrival-movie",
    "arrival-other-show",
  ]);
  expect(grouped.has("resolver-track-0")).toBe(false);
});

test("unresolved, cancelled and missing-relation tracks retain individual review", () => {
  const batches = listing();
  batches[0].tracks[0].classification = "unresolved";
  batches[0].tracks[1].publication_state = "cancelled";
  batches[0].tracks[2].arrival_item_id = null;
  const grouped = tvGroupedEpisodeIds(batches);
  expect(grouped.has("arrival-episode-0")).toBe(false);
  expect(grouped.has("arrival-episode-1")).toBe(false);
  expect(grouped.has("arrival-episode-2")).toBe(false);
  expect(grouped.size).toBe(7);
});

test("retired group state cannot hide staged files", () => {
  for (const status of ["complete", "superseded"]) {
    const batches = listing();
    batches[0].status = status;
    expect(tvGroupedEpisodeIds(batches).size).toBe(0);
  }
});
