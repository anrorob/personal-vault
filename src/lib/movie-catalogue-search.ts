type SearchableMovie = {
  id: string;
  title: string;
  original_title?: string | null;
  year: number | null;
};
type SearchableFranchise = { name: string; member_ids: string[] };

/** Filters loaded catalogue state without changing membership or ordering. */
export function filterMovieCatalogue<M extends SearchableMovie, F extends SearchableFranchise>(
  movies: M[],
  franchises: F[],
  query: string,
) {
  const term = query.trim().toLocaleLowerCase();
  if (!term) return { movies, franchises };
  const franchiseMembers = new Set(
    franchises
      .filter((franchise) => franchise.name.toLocaleLowerCase().includes(term))
      .flatMap((franchise) => franchise.member_ids),
  );
  return {
    movies: movies.filter(
      (movie) =>
        [movie.title, movie.original_title, movie.year?.toString()].some((field) =>
          field?.toLocaleLowerCase().includes(term),
        ) || franchiseMembers.has(movie.id),
    ),
    franchises: franchises.filter((franchise) => franchise.name.toLocaleLowerCase().includes(term)),
  };
}
