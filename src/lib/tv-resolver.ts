export type TvResolverTrack = {
  id: string;
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

type JsonRecord = Record<string, unknown>;

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function string(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string")
    : [];
}

function normalizeTrack(value: unknown): TvResolverTrack | null {
  const source = record(value);
  if (!source || !string(source.id) || !string(source.original_filename)) return null;
  return {
    id: string(source.id),
    original_filename: string(source.original_filename),
    runtime_seconds: nullableNumber(source.runtime_seconds),
    disc_number: nullableNumber(source.disc_number),
    track_number: nullableNumber(source.track_number),
    classification: string(source.classification, "unresolved"),
    proposed_season_number: nullableNumber(source.proposed_season_number),
    proposed_episode_number: nullableNumber(source.proposed_episode_number),
    canonical_destination: nullableString(source.canonical_destination),
    confidence: nullableString(source.confidence),
    evidence: strings(source.evidence),
    publication_state: string(source.publication_state, "proposed"),
    failure_detail: nullableString(source.failure_detail),
  };
}

function normalizeSeason(value: unknown): TvResolverSeason | null {
  const source = record(value);
  if (!source || !Number.isInteger(source.season_number)) return null;
  return {
    season_number: source.season_number,
    episode_candidate_count: nullableNumber(source.episode_candidate_count) ?? 0,
    extra_count: nullableNumber(source.extra_count) ?? 0,
    unresolved_count: nullableNumber(source.unresolved_count) ?? 0,
  };
}

export function normalizeTvResolverBatches(payload: unknown): {
  batches: TvResolverBatch[];
  dropped: number;
} {
  const response = record(payload);
  if (!response || !Array.isArray(response.batches)) {
    throw new Error("TV Resolver response has no batch list");
  }
  let dropped = 0;
  const batches = response.batches.flatMap((value) => {
    const source = record(value);
    if (!source || !string(source.id)) {
      dropped += 1;
      return [];
    }
    const tracks = Array.isArray(source.tracks)
      ? source.tracks.flatMap((track) => {
          const normalized = normalizeTrack(track);
          if (!normalized) dropped += 1;
          return normalized ? [normalized] : [];
        })
      : [];
    const seasons = Array.isArray(source.seasons)
      ? source.seasons.flatMap((season) => {
          const normalized = normalizeSeason(season);
          if (!normalized) dropped += 1;
          return normalized ? [normalized] : [];
        })
      : [];
    if (source.status === "complete") return [];
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
