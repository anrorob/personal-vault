import type { PlaybackState } from "./theatre-progress";

export type TvEpisode = {
  id: string;
  episode_number: number;
  title: string;
  runtime_minutes: number | null;
  artwork_url: string | null;
};
export type TvSeason = {
  id: string;
  season_number: number;
  poster_url: string | null;
  episodes: TvEpisode[];
};
export type TvShow = { id: string; title: string; poster_url: string | null; seasons: TvSeason[] };
export type SeriesEpisode = TvEpisode & { season_number: number };

/** Only the authorized PV catalogue supplies identity and ordering. */
export function orderedEpisodes(show: TvShow): SeriesEpisode[] {
  return [...show.seasons]
    .sort((a, b) => a.season_number - b.season_number)
    .flatMap((season) =>
      [...season.episodes]
        .sort((a, b) => a.episode_number - b.episode_number)
        .map((episode) => ({ ...episode, season_number: season.season_number })),
    );
}

export function episodeIdentity(episode: SeriesEpisode) {
  return `Season ${episode.season_number} · Episode ${episode.episode_number} — ${episode.title}`;
}

export function continueEpisode(
  episodes: SeriesEpisode[],
  progress: Record<string, PlaybackState>,
) {
  // Existing records have no last-played timestamp: choose the earliest unfinished
  // episode in catalogue order when multiple episodes are In Progress.
  const unfinished = episodes.find((episode) => progress[episode.id]?.state === "in_progress");
  if (unfinished) return unfinished;
  let latestWatched = -1;
  episodes.forEach((episode, index) => {
    if (progress[episode.id]?.state === "watched") latestWatched = index;
  });
  return episodes
    .slice(latestWatched + 1)
    .find((episode) => progress[episode.id]?.state !== "watched");
}

export function nextEpisode(episodes: SeriesEpisode[], currentId: string) {
  const index = episodes.findIndex((episode) => episode.id === currentId);
  return index < 0 ? undefined : episodes[index + 1];
}

/** Matches PV's existing completion policy; sticky Watched alone is not a playback event. */
export function episodeNearCompletion(position: number, duration: number, ended: boolean) {
  return ended || (duration > 0 && position >= duration - Math.min(30, duration * 0.05));
}
