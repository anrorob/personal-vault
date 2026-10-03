type Membership = { member_ids: string[] };
type PosterMovie = { id: string; poster_url: string | null };

export const FRANCHISE_POSTER_INTERVAL_MS = 60 * 60 * 1000;

/** Representation only: the caller retains the full catalogue for search/access. */
export function ungroupedMovies<M extends { id: string }>(movies: M[], franchises: Membership[]) {
  const grouped = new Set(franchises.flatMap((franchise) => franchise.member_ids));
  return movies.filter((movie) => !grouped.has(movie.id));
}

/** Existing authorized artwork only, ordered independently of franchise chronology. */
export function memberPosters(movies: PosterMovie[], memberIds: string[]) {
  const members = new Set(memberIds);
  return [
    ...new Set(
      movies
        .filter((movie) => members.has(movie.id))
        .sort((a, b) => a.id.localeCompare(b.id))
        .map((movie) => movie.poster_url?.trim())
        .filter((url): url is string => Boolean(url && /^(\/[^/]|https?:\/\/)/i.test(url))),
    ),
  ];
}

export function franchisePosterAt(posters: string[], time: number, failed: ReadonlySet<string>) {
  if (!posters.length) return null;
  const start = Math.floor(time / FRANCHISE_POSTER_INTERVAL_MS) % posters.length;
  for (let offset = 0; offset < posters.length; offset++) {
    const poster = posters[(start + offset) % posters.length];
    if (!failed.has(poster)) return poster;
  }
  return null;
}
