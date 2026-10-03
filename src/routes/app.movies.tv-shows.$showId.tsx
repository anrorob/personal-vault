import { Link, createFileRoute } from "@tanstack/react-router";
import { Play } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { PlaybackStateIndicator, WatchedAction } from "@/components/pv/PlaybackState";
import { useTheatreProgress, resumePosition } from "@/lib/theatre-progress";
import { MoviePlayer } from "@/components/pv/MoviePlayer";
import {
  continueEpisode,
  episodeIdentity,
  episodeNearCompletion,
  nextEpisode,
  orderedEpisodes,
  type SeriesEpisode,
  type TvShow,
} from "@/lib/theatre-episodes";
import type { QualityMode } from "@/lib/theatre-quality";
type Playback = { subtitles: { index: number; label: string }[] };

export const Route = createFileRoute("/app/movies/tv-shows/$showId")({
  component: TvShowDetail,
});

function TvShowDetail() {
  const { showId } = Route.useParams();
  const {
    progress,
    loaded: progressLoaded,
    error: progressError,
    pending: progressPending,
    save,
    mark,
  } = useTheatreProgress("tv-episodes");
  const [show, setShow] = useState<TvShow | null>(null);
  const [error, setError] = useState(false);
  const [selectedSeason, setSelectedSeason] = useState<number>(0);
  const [playing, setPlaying] = useState<SeriesEpisode | null>(null);
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(null);
  const [startingId, setStartingId] = useState<string | null>(null);
  const [nearEndId, setNearEndId] = useState<string | null>(null);
  const [qualityMode, setQualityMode] = useState<QualityMode>("Auto");
  const playbackRequest = useRef<AbortController | null>(null);
  const [resumeSeconds, setResumeSeconds] = useState(0);
  const [playback, setPlayback] = useState<Playback | null>(null);
  const [selectedSubtitleIndex, setSelectedSubtitleIndex] = useState<number | null>(null);
  const [playbackError, setPlaybackError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setShow(null);
    setError(false);
    setPlaying(null);
    setSelectedEpisodeId(null);
    setSelectedSeason(0);
    setStartingId(null);
    setNearEndId(null);
    setPlaybackError(null);
    fetch(`/api/tv-shows/${showId}`, { credentials: "include" })
      .then(async (response) => {
        if (!response.ok) throw new Error("TV Show was not available");
        return (await response.json()) as TvShow;
      })
      .then((value) => !cancelled && setShow(value))
      .catch(() => !cancelled && setError(true));
    return () => {
      cancelled = true;
      playbackRequest.current?.abort();
    };
  }, [showId]);

  const savePlaybackProgress = useCallback(
    (position: number, duration: number, completed: boolean) => {
      if (playing) {
        save(playing.id, position, duration, completed);
        setNearEndId(episodeNearCompletion(position, duration, completed) ? playing.id : null);
      }
    },
    [playing, save],
  );
  const startEpisode = async (episode: SeriesEpisode) => {
    playbackRequest.current?.abort();
    const controller = new AbortController();
    playbackRequest.current = controller;
    setSelectedEpisodeId(episode.id);
    setStartingId(episode.id);
    setSelectedSeason(
      show?.seasons.findIndex((season) => season.season_number === episode.season_number) ?? 0,
    );
    setPlaybackError(null);
    try {
      const response = await fetch(`/api/tv-shows/episodes/${episode.id}/playback`, {
        credentials: "include",
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error("Episode playback is not available.");
      }
      const state = await fetch(`/api/user-state/tv-episodes/${episode.id}`, {
        credentials: "include",
        signal: controller.signal,
      });
      if (!state.ok) throw new Error("Playback state is unavailable");
      const resume = resumePosition(await state.json());
      const nextPlayback = (await response.json()) as Playback;
      if (controller.signal.aborted) return;
      const subtitleLabel = playback?.subtitles.find(
        (track) => track.index === selectedSubtitleIndex,
      )?.label;
      setResumeSeconds(resume);
      setPlayback(nextPlayback);
      setSelectedSubtitleIndex(
        nextPlayback.subtitles.find((track) => track.label === subtitleLabel)?.index ?? null,
      );
      setNearEndId(null);
      setPlaying(episode);
    } catch {
      if (!controller.signal.aborted) setPlaybackError("Episode playback is not available.");
    } finally {
      if (!controller.signal.aborted) setStartingId(null);
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

  const episodes = orderedEpisodes(show);
  const continuation = progressLoaded ? continueEpisode(episodes, progress) : undefined;
  const upNext =
    playing && nearEndId === playing.id ? nextEpisode(episodes, playing.id) : undefined;

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
        {continuation && (
          <button
            type="button"
            disabled={startingId !== null || progressPending}
            onClick={() => void startEpisode(continuation)}
            className="mt-4 min-h-11 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            Continue — Season {continuation.season_number} · Episode {continuation.episode_number}
          </button>
        )}
      </header>
      {playing && (
        <section className="mt-6" aria-label="Current episode">
          <div className="mb-3" aria-live="polite">
            <p className="font-medium">{show.title}</p>
            <p className="text-sm text-muted-foreground">{episodeIdentity(playing)}</p>
          </div>
          <div className="aspect-video overflow-hidden rounded-lg bg-black">
            <MoviePlayer
              bufferPlayback
              key={playing.id}
              source={`/api/tv-shows/episodes/${playing.id}/hls/master.m3u8${selectedSubtitleIndex === null ? "" : `?subtitle_index=${selectedSubtitleIndex}`}`}
              startSeconds={resumeSeconds}
              onPlaybackError={() => {
                setPlaybackError("Episode playback is not available.");
                setPlaying(null);
              }}
              onProgress={savePlaybackProgress}
              subtitleTracks={playback?.subtitles ?? []}
              selectedSubtitleIndex={selectedSubtitleIndex}
              onSubtitleChange={setSelectedSubtitleIndex}
              initialQualityMode={qualityMode}
              onQualityModeChange={setQualityMode}
              playbackPlanUrl={`/api/tv-shows/episodes/${playing.id}/playback-plan${selectedSubtitleIndex === null ? "" : `?subtitle_index=${selectedSubtitleIndex}`}`}
            />
          </div>
          {upNext && (
            <div className="pv-panel mt-3 p-4" aria-label="Up next">
              <p className="text-sm font-medium">Up next</p>
              <p className="mt-1 text-sm text-muted-foreground">{episodeIdentity(upNext)}</p>
              <button
                type="button"
                disabled={startingId !== null}
                onClick={() => void startEpisode(upNext)}
                className="mt-3 min-h-11 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
              >
                Play Next Episode
              </button>
            </div>
          )}
        </section>
      )}
      {progressError && (
        <p role="alert" className="mt-4 text-sm text-red-300">
          {progressError}
        </p>
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
                <div
                  key={episode.id}
                  className={`pv-panel overflow-hidden ${selectedEpisodeId === episode.id || playing?.id === episode.id ? "ring-2 ring-amber-400" : ""}`}
                >
                  <button
                    type="button"
                    aria-pressed={selectedEpisodeId === episode.id}
                    aria-current={playing?.id === episode.id ? "true" : undefined}
                    onClick={() =>
                      void startEpisode({
                        ...episode,
                        season_number: show.seasons[selectedSeason].season_number,
                      })
                    }
                    className="w-full pv-panel-hover overflow-hidden text-left group"
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
                      {(selectedEpisodeId === episode.id || playing?.id === episode.id) && (
                        <p className="mt-1 text-xs text-amber-400">
                          {startingId === episode.id
                            ? "Loading episode…"
                            : playing?.id === episode.id
                              ? "Current episode"
                              : "Selected"}
                        </p>
                      )}
                      {episode.runtime_minutes && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          {episode.runtime_minutes} min
                        </p>
                      )}
                    </div>
                  </button>
                  <div className="px-3 pb-3 space-y-2">
                    <PlaybackStateIndicator progress={progress[episode.id]} />
                    <WatchedAction
                      progress={progress[episode.id]}
                      pending={progressPending}
                      onChange={(watched) => void mark(episode.id, watched)}
                    />
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}
      </div>
    </main>
  );
}
