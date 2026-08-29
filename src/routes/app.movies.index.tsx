import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowRight, Film } from "lucide-react";
import { useEffect, useState } from "react";

export const Route = createFileRoute("/app/movies/")({
  component: MoviesPage,
});

type Movie = {
  id: string;
  title: string;
  year: number | null;
  poster_url: string | null;
  is_exclusive_movie: boolean;
};

type MovieView = "all" | "exclusive";

function MoviesPage() {
  const navigate = useNavigate();
  const [movies, setMovies] = useState<Movie[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<MovieView>("all");

  useEffect(() => {
    const controller = new AbortController();

    const loadMovies = async () => {
      try {
        const response = await fetch(`/api/movies?view=${view}`, {
          credentials: "include",
          headers: {
            Accept: "application/json",
          },
          signal: controller.signal,
        });

        if (response.status === 401) {
          await navigate({ to: "/login" });
          return;
        }

        if (!response.ok) {
          throw new Error("Movie library request failed");
        }

        setMovies((await response.json()) as Movie[]);
      } catch (requestError) {
        if (requestError instanceof DOMException && requestError.name === "AbortError") {
          return;
        }

        setError("The movie library is currently unavailable.");
      }
    };

    void loadMovies();

    return () => controller.abort();
  }, [navigate, view]);

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <div>
          <h3 className="pv-content-title text-xl">Movies</h3>
          <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
            {movies === null
              ? "Loading library..."
              : `${movies.length} ${movies.length === 1 ? "movie" : "movies"}`}
          </p>
        </div>
      </div>

      <div
        className="inline-flex rounded-md border p-1"
        style={{ borderColor: "var(--pv-border)", background: "rgba(255,255,255,0.03)" }}
        aria-label="Movie library filter"
      >
        <button
          type="button"
          className="rounded px-3 py-1.5 text-sm transition-colors"
          onClick={() => setView("all")}
          aria-pressed={view === "all"}
          style={
            view === "all"
              ? { background: "var(--pv-gold)", color: "#09090b" }
              : { color: "var(--pv-text-dim)" }
          }
        >
          All Movies
        </button>
        <button
          type="button"
          className="rounded px-3 py-1.5 text-sm transition-colors"
          onClick={() => setView("exclusive")}
          aria-pressed={view === "exclusive"}
          style={
            view === "exclusive"
              ? { background: "var(--pv-gold)", color: "#09090b" }
              : { color: "var(--pv-text-dim)" }
          }
        >
          Exclusive Movies
        </button>
      </div>

      {error && <div className="pv-panel p-6 text-sm text-center text-red-300">{error}</div>}

      {!error && movies?.length === 0 && (
        <div className="pv-panel p-10 text-sm text-center" style={{ color: "var(--pv-text-dim)" }}>
          {view === "exclusive"
            ? "No titles have been selected as Exclusive Movies."
            : "No movie files were found."}
        </div>
      )}

      {!error && movies && movies.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {movies.map((movie) => (
            <Link
              key={movie.id}
              className="pv-panel pv-panel-hover overflow-hidden text-left group"
              to="/app/movies/$movieId"
              params={{ movieId: movie.id }}
              aria-label={`View details for ${movie.title}`}
            >
              <div
                className="aspect-[2/3] flex items-center justify-center relative"
                style={{
                  background: "linear-gradient(160deg, #1c1d22 0%, #101014 60%, #0a0a0c 100%)",
                  borderBottom: "1px solid var(--pv-border)",
                }}
              >
                {movie.poster_url ? (
                  <img
                    src={movie.poster_url}
                    alt={`${movie.title} poster`}
                    loading="lazy"
                    className="absolute inset-0 h-full w-full object-cover"
                  />
                ) : (
                  <Film size={40} style={{ color: "var(--pv-silver-dim)" }} />
                )}
                {movie.is_exclusive_movie && (
                  <span
                    className="absolute left-3 top-3 rounded-full px-2 py-1 text-[11px] font-semibold"
                    style={{
                      background: "rgba(9,9,11,0.8)",
                      border: "1px solid var(--pv-gold)",
                      color: "var(--pv-gold)",
                    }}
                  >
                    Exclusive
                  </span>
                )}
                <span
                  className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100 transition-opacity"
                  style={{ background: "rgba(0, 0, 0, 0.5)" }}
                >
                  <span
                    className="h-12 w-12 rounded-full flex items-center justify-center"
                    style={{
                      background: "var(--pv-gold)",
                      color: "#0a0a0c",
                    }}
                  >
                    <ArrowRight size={22} />
                  </span>
                </span>
              </div>
              <div className="p-4">
                <h3 className="text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
                  {movie.title}
                </h3>
                {movie.year && (
                  <p className="text-xs mt-0.5" style={{ color: "var(--pv-text-dim)" }}>
                    {movie.year}
                  </p>
                )}
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
