import { expect, test } from "bun:test";

import { normalizeTvResolverBatches, tvCounts } from "../src/lib/tv-resolver";

const track = (id, classification, season = 3) => ({
  id,
  original_filename: `Westworld Season ${season} Disc 1_t${id}.mkv`,
  runtime_seconds: classification === "likely_extra" ? 240 : 3600,
  disc_number: 1,
  track_number: Number(id),
  classification,
  proposed_season_number: classification === "likely_episode" ? season : null,
  proposed_episode_number: classification === "likely_episode" ? Number(id) : null,
  canonical_destination:
    classification === "likely_episode"
      ? `/vault/Theatre/TV Shows/Westworld/Season 03/Westworld - S03E${id}.mkv`
      : null,
  confidence: classification === "likely_episode" ? "high" : "low",
  evidence: [],
  publication_state: "proposed",
  failure_detail: null,
});

test("normalizes two production-shaped Westworld batches with 99 tracks", () => {
  const ambiguous = [
    ...Array.from({ length: 14 }, (_, index) => track(String(index + 1), "likely_episode")),
    ...Array.from({ length: 10 }, (_, index) => track(String(index + 15), "unresolved")),
  ];
  const second = [
    ...Array.from({ length: 24 }, (_, index) => track(String(index + 25), "likely_episode", 2)),
    ...Array.from({ length: 51 }, (_, index) => track(String(index + 49), "likely_extra", 1)),
  ];
  const result = normalizeTvResolverBatches({
    batches: [
      {
        id: "ambiguous",
        status: "needs_review",
        proposed_show_title: "Westworld",
        confidence: "medium",
        seasons: [
          { season_number: 3, episode_candidate_count: 14, extra_count: 0, unresolved_count: 10 },
        ],
        tracks: ambiguous,
      },
      {
        id: "high",
        status: "proposed",
        proposed_show_title: "Westworld",
        confidence: "high",
        seasons: [
          { season_number: 1, episode_candidate_count: 10, extra_count: 13, unresolved_count: 0 },
          { season_number: 2, episode_candidate_count: 10, extra_count: 27, unresolved_count: 0 },
          { season_number: 3, episode_candidate_count: 4, extra_count: 11, unresolved_count: 0 },
        ],
        tracks: second,
      },
    ],
  });
  expect(result.batches).toHaveLength(2);
  expect(result.batches.flatMap((batch) => batch.tracks)).toHaveLength(99);
  expect(tvCounts(result.batches[0])).toEqual({
    episodes: 14,
    extras: 0,
    extrasRemaining: 0,
    unresolved: 10,
    failed: 0,
    published: 0,
  });
});

test("keeps ordinary Arrival Hall safe for an empty or malformed resolver payload", () => {
  expect(normalizeTvResolverBatches({ batches: [] })).toEqual({ batches: [], dropped: 0 });
  expect(
    normalizeTvResolverBatches({ batches: [{ id: "valid", tracks: null, seasons: null }] }),
  ).toEqual({
    batches: [
      {
        id: "valid",
        status: "needs_review",
        proposed_show_title: null,
        confidence: null,
        tracks: [],
        seasons: [],
      },
    ],
    dropped: 0,
  });
  expect(normalizeTvResolverBatches({ batches: [{ tracks: [] }, { id: "valid" }] })).toEqual({
    batches: [
      {
        id: "valid",
        status: "needs_review",
        proposed_show_title: null,
        confidence: null,
        tracks: [],
        seasons: [],
      },
    ],
    dropped: 1,
  });
  expect(() => normalizeTvResolverBatches({ unavailable: true })).toThrow(
    "TV Resolver response has no batch list",
  );
});
