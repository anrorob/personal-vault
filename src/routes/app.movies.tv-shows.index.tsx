import { Link, createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";

export const Route = createFileRoute("/app/movies/tv-shows/")({
  component: TvShowsPage,
});

type TvShow = { id: string; title: string; season_count: number; poster_url: string | null };

function TvShowsPage() {
  const [shows, setShows] = useState<TvShow[] | null>(null);

  useEffect(() => {
    void fetch("/api/tv-shows", { credentials: "include" })
      .then((response) => (response.ok ? (response.json() as Promise<TvShow[]>) : []))
      .then(setShows)
      .catch(() => setShows([]));
  }, []);

  if (shows === null)
    return <div className="pv-panel p-10 text-center text-sm">Loading TV Shows…</div>;

  if (!shows.length)
    return (
      <div className="pv-panel p-10 text-center text-sm">
        TV Shows will appear here once a reviewed Show has been published.
      </div>
    );

  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
      {shows.map((show) => (
        <Link
          key={show.id}
          to="/app/movies/tv-shows/$showId"
          params={{ showId: show.id }}
          className="pv-panel pv-panel-hover block overflow-hidden text-left"
        >
          <div className="aspect-[2/3] bg-black">
            {show.poster_url && (
              <img
                src={show.poster_url}
                alt={`${show.title} poster`}
                className="h-full w-full object-cover"
              />
            )}
          </div>
          <div className="p-4">
            <h3 className="pv-content-title">{show.title}</h3>
            <p className="text-sm mt-1">
              {show.season_count} {show.season_count === 1 ? "Season" : "Seasons"}
            </p>
          </div>
        </Link>
      ))}
    </div>
  );
}
