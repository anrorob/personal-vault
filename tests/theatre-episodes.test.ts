import { expect, test } from "bun:test";
import {
  continueEpisode,
  episodeIdentity,
  episodeNearCompletion,
  nextEpisode,
  orderedEpisodes,
  type TvShow,
} from "../src/lib/theatre-episodes";
import type { PlaybackState } from "../src/lib/theatre-progress";

const episode = (id: string, number: number) => ({
  id,
  episode_number: number,
  title: `Example ${id}`,
  runtime_minutes: null,
  artwork_url: null,
});
const show: TvShow = {
  id: "example-series",
  title: "Example Series",
  poster_url: null,
  seasons: [
    { id: "second", season_number: 2, poster_url: null, episodes: [episode("c", 2)] },
    {
      id: "first",
      season_number: 1,
      poster_url: null,
      episodes: [episode("b", 5), episode("a", 1)],
    },
    { id: "empty", season_number: 3, poster_url: null, episodes: [] },
  ],
};
const episodes = orderedEpisodes(show);
const state = (value: PlaybackState["state"]): PlaybackState => ({
  state: value,
  completed: value === "watched",
  position_seconds: 100,
  duration_seconds: 600,
});

test("PV catalogue order crosses seasons, skips gaps/empty seasons, and stops at the last episode", () => {
  expect(episodes.map((e) => e.id)).toEqual(["a", "b", "c"]);
  expect(show.seasons[1].episodes[0].id).toBe("b"); // input remains unchanged
  expect(nextEpisode(episodes, "a")?.id).toBe("b");
  expect(nextEpisode(episodes, "b")?.id).toBe("c");
  expect(nextEpisode(episodes, "c")).toBeUndefined();
  expect(nextEpisode(episodes, "unknown")).toBeUndefined();
  expect(episodeIdentity(episodes[2])).toBe("Season 2 · Episode 2 — Example c");
});

test("Continue prioritizes In Progress, using catalogue order for multiple unfinished episodes", () => {
  expect(continueEpisode(episodes, { a: state("in_progress"), b: state("watched") })?.id).toBe("a");
  expect(continueEpisode(episodes, { a: state("in_progress"), c: state("in_progress") })?.id).toBe(
    "a",
  );
});

test("Continue follows the latest watched catalogue position rather than highest episode number", () => {
  expect(continueEpisode(episodes, {})?.id).toBe("a");
  expect(continueEpisode(episodes, { a: state("watched") })?.id).toBe("b");
  expect(continueEpisode(episodes, { b: state("watched") })?.id).toBe("c");
  expect(
    continueEpisode(episodes, { a: state("watched"), b: state("watched"), c: state("watched") }),
  ).toBeUndefined();
  expect(continueEpisode(episodes, { c: state("watched") })).toBeUndefined();
  expect(continueEpisode([], {})).toBeUndefined();
});

test("Continue joins only this viewer's states to visible catalogue episodes", () => {
  const firstViewer = {
    a: state("watched"),
    b: state("in_progress"),
    hidden: state("in_progress"),
  };
  const secondViewer = { c: state("in_progress") };
  expect(continueEpisode(episodes, firstViewer)?.id).toBe("b");
  expect(continueEpisode(episodes, secondViewer)?.id).toBe("c");
  expect(continueEpisode(episodes, { hidden: state("in_progress") })?.id).toBe("a");
});

test("Next availability shares the established watched threshold and handles ended/short media", () => {
  expect(episodeNearCompletion(569, 600, false)).toBe(false);
  expect(episodeNearCompletion(570, 600, false)).toBe(true);
  expect(episodeNearCompletion(94, 100, false)).toBe(false);
  expect(episodeNearCompletion(95, 100, false)).toBe(true);
  expect(episodeNearCompletion(0, 0, false)).toBe(false);
  expect(episodeNearCompletion(100, 100, true)).toBe(true);
  expect(episodeNearCompletion(100, 600, false)).toBe(false);
});
