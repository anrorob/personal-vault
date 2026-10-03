export type TvResolverTrack = {
  id: string;
  arrival_item_id: string | null;
  original_filename: string;
  runtime_seconds: number | null;
  disc_number: number | null;
  track_number: number | null;
  classification: string;
  proposed_season_number: number | null;
  proposed_episode_number: number | null;
  canonical_destination: string | null;
  confidence: string | null;
  evidence: string[];
  publication_state: string;
  failure_detail: string | null;
};

export type TvResolverSeason = {
  season_number: number;
  episode_candidate_count: number;
  extra_count: number;
  unresolved_count: number;
};
export type TvResolverBatch = {
  id: string;
  status: string;
  proposed_show_title: string | null;
  confidence: string | null;
  seasons: TvResolverSeason[];
  tracks: TvResolverTrack[];
};
type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as RecordValue)
    : null;
const string = (value: unknown, fallback = "") => (typeof value === "string" ? value : fallback);
const nullableString = (value: unknown) => (typeof value === "string" ? value : null);
const nullableNumber = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

function track(value: unknown): TvResolverTrack | null {
  const source = record(value);
  if (!source || !string(source.id) || !string(source.original_filename)) return null;
  return {
    id: string(source.id),
    arrival_item_id: nullableString(source.arrival_item_id),
    original_filename: string(source.original_filename),
    runtime_seconds: nullableNumber(source.runtime_seconds),
    disc_number: nullableNumber(source.disc_number),
    track_number: nullableNumber(source.track_number),
    classification: string(source.classification, "unresolved"),
    proposed_season_number: nullableNumber(source.proposed_season_number),
    proposed_episode_number: nullableNumber(source.proposed_episode_number),
    canonical_destination: nullableString(source.canonical_destination),
    confidence: nullableString(source.confidence),
    evidence: Array.isArray(source.evidence)
      ? source.evidence.filter((entry): entry is string => typeof entry === "string")
      : [],
    publication_state: string(source.publication_state, "proposed"),
    failure_detail: nullableString(source.failure_detail),
  };
}

export function normalizeTvResolverBatches(payload: unknown): {
  batches: TvResolverBatch[];
  dropped: number;
} {
  const response = record(payload);
  if (!response || !Array.isArray(response.batches))
    throw new Error("TV Resolver response has no batch list");
  let dropped = 0;
  const batches = response.batches.flatMap((value) => {
    const source = record(value);
    if (!source || !string(source.id) || source.status === "complete") {
      dropped += source?.status === "complete" ? 0 : 1;
      return [];
    }
    const tracks = Array.isArray(source.tracks)
      ? source.tracks.flatMap((entry) => {
          const normalized = track(entry);
          if (!normalized) dropped += 1;
          return normalized ? [normalized] : [];
        })
      : [];
    const seasons = Array.isArray(source.seasons)
      ? source.seasons.flatMap((entry) => {
          const season = record(entry);
          if (!season || !Number.isInteger(season.season_number)) {
            dropped += 1;
            return [];
          }
          return [
            {
              season_number: season.season_number as number,
              episode_candidate_count: nullableNumber(season.episode_candidate_count) ?? 0,
              extra_count: nullableNumber(season.extra_count) ?? 0,
              unresolved_count: nullableNumber(season.unresolved_count) ?? 0,
            },
          ];
        })
      : [];
    return [
      {
        id: string(source.id),
        status: string(source.status, "needs_review"),
        proposed_show_title: nullableString(source.proposed_show_title),
        confidence: nullableString(source.confidence),
        seasons,
        tracks,
      },
    ];
  });
  return { batches, dropped };
}

export function tvCounts(batch: TvResolverBatch) {
  const episodes = batch.tracks.filter((track) => track.classification === "likely_episode");
  return {
    episodes: episodes.length,
    extras: batch.tracks.filter((track) => track.classification === "likely_extra").length,
    unresolved: batch.tracks.filter((track) => track.classification === "unresolved").length,
    failed: batch.tracks.filter((track) => track.publication_state === "failed").length,
    extrasRemaining: batch.tracks.filter(
      (track) => track.classification === "likely_extra" && track.publication_state !== "published",
    ).length,
    published: episodes.filter((track) => track.publication_state === "published").length,
  };
}

/** Episode members already have the group's review action; unresolved files retain individual review. */
export function tvGroupedEpisodeIds(batches: TvResolverBatch[]): Set<string> {
  return new Set(
    batches
      .filter((batch) => !["complete", "superseded"].includes(batch.status))
      .flatMap((batch) =>
        batch.tracks.flatMap((track) =>
          track.classification === "likely_episode" &&
          track.publication_state !== "cancelled" &&
          track.arrival_item_id
            ? [track.arrival_item_id]
            : [],
        ),
      ),
  );
}

export function tvBatchActions(batch: TvResolverBatch) {
  const counts = tvCounts(batch);
  return {
    approve: ["proposed", "needs_review"].includes(batch.status) && counts.published === 0,
    publishExtras:
      batch.status === "published" &&
      counts.episodes > 0 &&
      counts.published === counts.episodes &&
      counts.extrasRemaining > 0 &&
      counts.failed === 0,
    retry: batch.status === "failed" && counts.failed > 0,
  };
}
