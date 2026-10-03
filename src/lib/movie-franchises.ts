export type FranchiseMovie = {
  id: string;
  title: string;
  year: number | null;
  poster_url: string | null;
};

export type MovieFranchise = {
  id: string;
  name: string;
  description: string;
  member_count: number;
  member_ids: string[];
  poster_url: string | null;
  movies: FranchiseMovie[];
  selected_order: "release" | "timeline" | null;
  effective_order: "release" | "timeline";
  timeline_available: boolean;
  chronology_source: string | null;
  chronology_url: string | null;
  chronology_version: string | null;
};

export async function franchiseRequest<T>(path = "", method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`/api/movie-franchises${path}`, {
    method,
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok)
    throw new Error(
      response.status === 401
        ? "Please sign in again to manage franchises."
        : "The franchise could not be updated. Please try again.",
    );
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}
