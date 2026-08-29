import { Link, Outlet, createFileRoute, useLocation } from "@tanstack/react-router";

export const Route = createFileRoute("/app/movies")({
  component: MoviesLayout,
});

function MoviesLayout() {
  const { pathname } = useLocation();
  const tvShows = pathname.startsWith("/app/movies/tv-shows");

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div className="space-y-3">
        <div>
          <h2 className="pv-content-title text-xl">Theatre</h2>
          <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
            Your Vault theatre library.
          </p>
        </div>
        <nav
          className="inline-flex rounded-md border p-1"
          style={{ borderColor: "var(--pv-border)", background: "rgba(255,255,255,0.03)" }}
          aria-label="Theatre library"
        >
          <Link to="/app/movies" className="pv-library-tab" data-active={!tvShows}>
            Movies
          </Link>
          <Link to="/app/movies/tv-shows" className="pv-library-tab" data-active={tvShows}>
            TV Shows
          </Link>
        </nav>
      </div>
      <Outlet />
    </div>
  );
}
