import { Link, createFileRoute } from "@tanstack/react-router";
import { Play } from "lucide-react";
import { useEffect, useState } from "react";
import { MoviePlayer } from "@/components/pv/MoviePlayer";

type Episode = {
  id: string;
  episode_number: number;
  title: string;
  runtime_minutes: number | null;
  artwork_url: string | null;
};

type Season = { id: string; season_number: number; poster_url: string | null; episodes: Episode[] };
type Show = { id: string; title: string; poster_url: string | null; seasons: Season[] };
type Playback = { subtitles: { index: number; label: string }[] };

export const Route = createFileRoute("/app/movies/tv-shows/$showId")({
  component: TvShowDetail,
});

function TvShowDetail() {
  const { showId } = Route.useParams();
  const [show, setShow] = useState<Show | null>(null);
  const [error, setError] = useState(false);
  const [selectedSeason, setSelectedSeason] = useState<number>(0);
  const [playing, setPlaying] = useState<Episode | null>(null);
  const [resumeSeconds, setResumeSeconds] = useState(0);
  const [playback, setPlayback] = useState<Playback | null>(null);
  const [selectedSubtitleIndex, setSelectedSubtitleIndex] = useState<number | null>(null);
  const [playbackError, setPlaybackError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/tv-shows/${showId}`, { credentials: "include" })
      .then(async (response) => {
        if (!response.ok) throw new Error("TV Show was not available");
        return (await response.json()) as Show;
      })
      .then((value) => !cancelled && setShow(value))
      .catch(() => !cancelled && setError(true));
    return () => {
      cancelled = true;
    };
  }, [showId]);

  useEffect(() => {
    if (!playing) return;
    void fetch(`/api/user-state/tv-episodes/${playing.id}`, { credentials: "include" })
      .then((response) => (response.ok ? response.json() : null))
      .then((progress) => setResumeSeconds(progress?.position_seconds ?? 0))
      .catch(() => setResumeSeconds(0));
  }, [playing]);
  const startEpisode = async (episode: Episode) => {
    setPlayback(null);
    setPlaybackError(null);
    try {
      const response = await fetch(`/api/tv-shows/episodes/${episode.id}/playback`, {
        credentials: "include",
      });
      if (!response.ok) {
        throw new Error("Episode playback is not available.");
      }
      setPlayback((await response.json()) as Playback);
      setSelectedSubtitleIndex(null);
      setPlaying(episode);
    } catch {
      setPlaybackError("Episode playback is not available.");
    }
  };

  if (error) {
    return (
      <main className="min-h-full px-4 py-8 sm:px-6 lg:px-10">
        <Link
          to="/app/movies/tv-shows"
          className="pv-text-link inline-flex rounded-md text-sm underline-offset-4 hover:underline"
        >
          Back to TV Shows
        </Link>
        <p className="mt-6 text-muted-foreground">This TV Show is not available.</p>
      </main>
    );
  }

  if (!show) {
    return (
      <main className="min-h-full px-4 py-8 sm:px-6 lg:px-10 text-muted-foreground">
        Loading TV Show…
      </main>
    );
  }

  return (
    <main className="min-h-full px-4 py-8 sm:px-6 lg:px-10">
      <Link
        to="/app/movies/tv-shows"
        className="pv-text-link inline-flex rounded-md text-sm underline-offset-4 hover:underline"
      >
        Back to TV Shows
      </Link>
      <header className="mt-4">
        <h1 className="text-3xl font-semibold tracking-tight">{show.title}</h1>
      </header>
      {playing && (
        <section className="mt-6 aspect-video overflow-hidden rounded-lg bg-black">
          <MoviePlayer
            key={playing.id}
            source={`/api/tv-shows/episodes/${playing.id}/hls/master.m3u8${selectedSubtitleIndex === null ? "" : `?subtitle_index=${selectedSubtitleIndex}`}`}
            startSeconds={resumeSeconds}
            onPlaybackError={() => setPlaying(null)}
            onProgress={(position_seconds, duration_seconds, completed) => {
              void fetch(`/api/user-state/tv-episodes/${playing.id}`, {
                method: "PUT",
                credentials: "include",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ position_seconds, duration_seconds, completed }),
              });
            }}
            subtitleTracks={playback?.subtitles ?? []}
            selectedSubtitleIndex={selectedSubtitleIndex}
            onSubtitleChange={setSelectedSubtitleIndex}
          />
        </section>
      )}
      {playbackError && <p className="mt-4 text-sm text-destructive">{playbackError}</p>}
      <div className="mt-8">
        <div className="flex gap-4 overflow-x-auto pb-2">
          {show.seasons.map((season, index) => (
            <button
              type="button"
              key={season.id}
              onClick={() => setSelectedSeason(index)}
              className={`pv-panel w-32 min-w-32 overflow-hidden text-left ${selectedSeason === index ? "ring-2 ring-amber-400" : ""}`}
            >
              <div className="aspect-[2/3] bg-black">
                {season.poster_url && (
                  <img
                    src={season.poster_url}
                    alt={`Season ${season.season_number} poster`}
                    className="h-full w-full object-cover"
                  />
                )}
              </div>
              <p className="p-3 text-sm">Season {season.season_number}</p>
            </button>
          ))}
        </div>
        {show.seasons[selectedSeason] && (
          <section className="mt-6">
            <h2 className="text-xl font-medium">
              Season {show.seasons[selectedSeason].season_number}
            </h2>
            <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {show.seasons[selectedSeason].episodes.map((episode) => (
                <button
                  type="button"
                  key={episode.id}
                  onClick={() => void startEpisode(episode)}
                  className="pv-panel pv-panel-hover overflow-hidden text-left group"
                >
                  <div className="relative aspect-video bg-black">
                    {episode.artwork_url && (
                      <img
                        src={episode.artwork_url}
                        alt={`Episode ${episode.episode_number}: ${episode.title}`}
                        className="h-full w-full object-cover"
                      />
                    )}
                    <span className="absolute inset-0 flex items-center justify-center bg-black/20 group-hover:bg-black/35">
                      <Play
                        className="h-10 w-10 text-white"
                        fill="currentColor"
                        aria-hidden="true"
                      />
                    </span>
                  </div>
                  <div className="p-3">
                    <p className="text-sm font-semibold">
                      {episode.episode_number}. {episode.title}
                    </p>
                    {episode.runtime_minutes && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        {episode.runtime_minutes} min
                      </p>
                    )}
                  </div>
                </button>
              ))}
            </div>
          </section>
        )}
      </div>
    </main>
  );
}
