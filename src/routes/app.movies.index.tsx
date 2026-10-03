import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowRight, Ellipsis, Film, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTheatreProgress } from "@/lib/theatre-progress";
import { PlaybackStateIndicator } from "@/components/pv/PlaybackState";
import { FranchiseEditor, MovieFranchisePanel } from "@/components/pv/MovieFranchises";
import { franchiseRequest, type MovieFranchise } from "@/lib/movie-franchises";
import { filterMovieCatalogue } from "@/lib/movie-catalogue-search";
import { memberPosters, ungroupedMovies } from "@/lib/movie-franchise-presentation";
import { FranchisePoster } from "@/components/pv/FranchisePoster";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

export const Route = createFileRoute("/app/movies/")({
  validateSearch: (search: Record<string, unknown>): { franchise?: string } => ({
    ...(typeof search.franchise === "string" ? { franchise: search.franchise } : {}),
  }),
  component: MoviesPage,
});

type Movie = {
  id: string;
  title: string;
  original_title?: string | null;
  year: number | null;
  poster_url: string | null;
  is_exclusive_movie: boolean;
};

type MovieView = "all" | "exclusive";

function MoviesPage() {
  const navigate = useNavigate();
  const { progress, error: progressError } = useTheatreProgress("movies");
  const [movies, setMovies] = useState<Movie[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<MovieView>("all");
  const { franchise } = Route.useSearch();
  const [franchises, setFranchises] = useState<MovieFranchise[]>([]);
  const [franchiseError, setFranchiseError] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [franchiseEditorOpen, setFranchiseEditorOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const searchInput = useRef<HTMLInputElement>(null);
  const menuDestination = useRef<"search" | "franchise" | null>(null);
  const toggleSearch = () => {
    setSearchOpen((current) => !current);
    setQuery("");
  };
  const searching = query.trim().length > 0;
  const visible = useMemo(
    () => filterMovieCatalogue(movies ?? [], franchises, query),
    [movies, franchises, query],
  );
  const standalone = useMemo(
    () =>
      view === "all" && !searching ? ungroupedMovies(visible.movies, franchises) : visible.movies,
    [view, searching, visible.movies, franchises],
  );

  useEffect(() => {
    let active = true;
    if (!franchise) {
      void franchiseRequest<MovieFranchise[]>()
        .then((items) => {
          if (active) {
            setFranchises(items);
            setFranchiseError(null);
          }
        })
        .catch((reason: Error) => {
          if (active) setFranchiseError(reason.message);
        });
    }
    return () => {
      active = false;
    };
  }, [franchise]);

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

  if (franchise)
    return (
      <MovieFranchisePanel
        key={franchise}
        id={franchise}
        onDeleted={() => void navigate({ to: "/app/movies", search: {} })}
      />
    );

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
        <FranchiseEditor
          open={franchiseEditorOpen}
          onOpenChange={setFranchiseEditorOpen}
          showTrigger={false}
          onSaved={(item) => setFranchises((current) => [...current, item])}
        />
      </div>

      <div className="flex items-center gap-2">
        <div
          className="inline-flex rounded-md border p-1"
          style={{ borderColor: "var(--pv-border)", background: "rgba(255,255,255,0.03)" }}
          aria-label="Movie library filter"
        >
          <button
            type="button"
            className="rounded px-2 sm:px-3 py-1.5 text-sm transition-colors"
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
            className="rounded px-2 sm:px-3 py-1.5 text-sm transition-colors"
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
        <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen} modal={false}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              aria-label="Movies actions"
              className="inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md border"
              style={{ borderColor: "var(--pv-border)" }}
              // Activate on click (including touch-generated clicks), not pointerdown.
              // Suppress Radix's pointer toggle so one gesture cannot toggle twice.
              onPointerDown={(event) => event.preventDefault()}
              onClick={() => setMenuOpen((current) => !current)}
            >
              <Ellipsis size={20} aria-hidden="true" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            collisionPadding={8}
            className="pv-dialog min-w-44 max-w-[calc(100vw-1rem)] border-[var(--pv-border)] bg-[var(--pv-bg)] text-[var(--pv-silver)]"
            onCloseAutoFocus={(event) => {
              if (menuDestination.current) {
                event.preventDefault();
                if (menuDestination.current === "search") searchInput.current?.focus();
                menuDestination.current = null;
              }
            }}
          >
            <DropdownMenuItem
              className="min-h-11"
              onSelect={() => {
                menuDestination.current = !searchOpen ? "search" : null;
                toggleSearch();
              }}
            >
              Search
            </DropdownMenuItem>
            <DropdownMenuItem
              className="min-h-11"
              onSelect={() => {
                menuDestination.current = "franchise";
                setFranchiseEditorOpen(true);
              }}
            >
              Create franchise
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {searchOpen && (
        <div id="movies-search" role="search" className="flex items-center gap-2 max-w-md">
          <input
            ref={searchInput}
            type="search"
            aria-label="Search movies"
            placeholder="Title, original title, year or franchise"
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="min-h-11 min-w-0 flex-1 rounded-md border bg-transparent px-3 text-sm"
            style={{ borderColor: "var(--pv-border)" }}
          />
          <button
            type="button"
            aria-label="Clear movie search"
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md border"
            style={{ borderColor: "var(--pv-border)" }}
            onClick={() => setQuery("")}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </div>
      )}

      {progressError && (
        <p role="alert" className="text-sm text-red-300">
          {progressError}
        </p>
      )}
      {error && <div className="pv-panel p-6 text-sm text-center text-red-300">{error}</div>}
      {franchiseError && (
        <p role="alert" className="text-sm text-red-300">
          {franchiseError}
        </p>
      )}

      {view === "all" && visible.franchises.length > 0 && (
        <div className="pv-card-grid">
          {visible.franchises.map((item) => (
            <Link
              key={item.id}
              to="/app/movies"
              search={{ franchise: item.id }}
              className="pv-panel pv-panel-hover min-w-0 overflow-hidden"
              aria-label={`Open franchise ${item.name}`}
            >
              <FranchisePoster
                name={item.name}
                posters={memberPosters(movies ?? [], item.member_ids)}
              />
              <div className="p-4">
                <h3 className="text-sm font-semibold [overflow-wrap:anywhere]">{item.name}</h3>
                <p className="text-xs mt-1 text-[var(--pv-text-dim)]">
                  Franchise · {item.member_count} movies
                </p>
              </div>
            </Link>
          ))}
        </div>
      )}

      {!error &&
        searching &&
        movies !== null &&
        visible.movies.length === 0 &&
        (view !== "all" || visible.franchises.length === 0) && (
          <p role="status" className="pv-panel p-10 text-sm text-center text-[var(--pv-text-dim)]">
            No movies or franchises match your search.
          </p>
        )}
      {!error && !searching && movies?.length === 0 && (
        <div className="pv-panel p-10 text-sm text-center" style={{ color: "var(--pv-text-dim)" }}>
          {view === "exclusive"
            ? "No titles have been selected as Exclusive Movies."
            : "No movie files were found."}
        </div>
      )}

      {!error && movies && standalone.length > 0 && (
        <div className="pv-card-grid">
          {standalone.map((movie) => (
            <Link
              key={movie.id}
              className="pv-panel pv-panel-hover min-w-0 overflow-hidden text-left group"
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
                <h3
                  className="text-sm font-semibold [overflow-wrap:anywhere]"
                  style={{ color: "var(--pv-silver)" }}
                >
                  {movie.title}
                </h3>
                <PlaybackStateIndicator progress={progress[movie.id]} />
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
