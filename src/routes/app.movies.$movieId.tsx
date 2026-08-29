import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, Clock3, Download, Film, Play, RotateCcw, Star, UserRound } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { MoviePlayer } from "@/components/pv/MoviePlayer";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export const Route = createFileRoute("/app/movies/$movieId")({
  component: MovieDetailsPage,
});

type MoviePerson = {
  name: string;
  role: string | null;
  type: string | null;
  image_url: string | null;
};

type MovieExtra = {
  id: string;
  title: string;
  runtime_minutes: number | null;
  thumbnail_url: string | null;
  playback_available: boolean;
};

type MovieSubtitle = {
  title: string | null;
  language: string | null;
  codec: string | null;
  is_external: boolean;
};

type PlaybackSubtitleTrack = {
  index: number;
  title: string | null;
  display_title: string | null;
  language: string | null;
  codec: string | null;
  is_external: boolean;
  is_default: boolean;
  is_forced: boolean;
  is_hearing_impaired: boolean;
};

type MoviePlaybackReadiness = {
  subtitles: PlaybackSubtitleTrack[];
};

type MovieDetails = {
  id: string;
  title: string;
  year: number | null;
  official_rating: string | null;
  community_rating: number | null;
  runtime_minutes: number | null;
  overview: string | null;
  tagline: string | null;
  genres: string[];
  studios: string[];
  people: MoviePerson[];
  extras: MovieExtra[];
  trailers: MovieExtra[];
  edition: string | null;
  collections: string[];
  subtitles: MovieSubtitle[];
  provider_imported_at: string | null;
  poster_url: string | null;
  backdrop_url: string | null;
  container: string | null;
  video_codec: string | null;
  audio_codecs: string[];
  is_exclusive_movie: boolean;
};

function formatRuntime(minutes: number | null) {
  if (minutes === null) {
    return null;
  }

  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;

  return hours > 0 ? `${hours}h ${remainingMinutes}m` : `${minutes}m`;
}

function formatPlaybackTime(seconds: number) {
  const wholeMinutes = Math.floor(seconds / 60);
  return `${Math.floor(wholeMinutes / 60)}:${String(wholeMinutes % 60).padStart(2, "0")}`;
}

function subtitleLanguageName(language: string | null) {
  if (!language) return null;
  try {
    return (
      new Intl.DisplayNames(["en"], { type: "language" }).of(language) ?? language.toUpperCase()
    );
  } catch {
    return language.toUpperCase();
  }
}

function subtitleTrackLabel(track: PlaybackSubtitleTrack, tracks: PlaybackSubtitleTrack[]) {
  const language = subtitleLanguageName(track.language);
  const title = track.title?.trim() || null;
  const base =
    title && title.toLocaleLowerCase() !== language?.toLocaleLowerCase()
      ? language
        ? `${language} — ${title}`
        : title
      : (language ?? title ?? track.display_title?.trim() ?? `Subtitle ${track.index}`);
  const distinctions = [
    track.is_forced ? "Forced" : null,
    track.is_default ? "Default" : null,
    track.is_hearing_impaired ? "SDH" : null,
  ].filter((value): value is string => value !== null);
  const duplicateCount = tracks.filter((candidate) => {
    const candidateLanguage = subtitleLanguageName(candidate.language);
    const candidateTitle = candidate.title?.trim() || null;
    return candidateLanguage === language && candidateTitle === title;
  }).length;
  if (duplicateCount > 1) distinctions.push(`Track ${track.index}`);
  if (!language && !title && track.codec) distinctions.push(track.codec.toUpperCase());
  return distinctions.length > 0 ? `${base} (${distinctions.join(", ")})` : base;
}

function MovieDetailsPage() {
  const { movieId } = Route.useParams();
  const navigate = useNavigate();
  const [details, setDetails] = useState<MovieDetails | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playerOpen, setPlayerOpen] = useState(false);
  const [playingFeature, setPlayingFeature] = useState<MovieExtra | null>(null);
  const [playbackError, setPlaybackError] = useState(false);
  const [resumeSeconds, setResumeSeconds] = useState(0);
  const [resumeRequested, setResumeRequested] = useState(false);
  const [exclusiveError, setExclusiveError] = useState<string | null>(null);
  const [exclusiveSubmitting, setExclusiveSubmitting] = useState(false);
  const [playbackSubtitles, setPlaybackSubtitles] = useState<PlaybackSubtitleTrack[]>([]);
  const [selectedSubtitleIndex, setSelectedSubtitleIndex] = useState<number | null>(null);
  const handlePlaybackError = useCallback(() => setPlaybackError(true), []);

  useEffect(() => {
    const controller = new AbortController();

    const loadDetails = async () => {
      try {
        const response = await fetch(`/api/movies/${movieId}/details`, {
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

        if (response.status === 404) {
          setError("This movie is no longer available in the Vault.");
          return;
        }

        if (!response.ok) {
          throw new Error("Movie details request failed");
        }

        setDetails((await response.json()) as MovieDetails);
      } catch (requestError) {
        if (requestError instanceof DOMException && requestError.name === "AbortError") {
          return;
        }

        setError("Movie details are currently unavailable.");
      }
    };

    void loadDetails();

    return () => controller.abort();
  }, [movieId, navigate]);

  useEffect(() => {
    void fetch(`/api/user-state/movies/${movieId}`, { credentials: "include" })
      .then(async (response) => (response.ok ? response.json() : null))
      .then(
        (
          progress: {
            position_seconds: number;
            duration_seconds: number;
            completed: boolean;
          } | null,
        ) => {
          if (progress && !progress.completed && progress.position_seconds >= 30) {
            setResumeSeconds(progress.position_seconds);
          }
        },
      );
  }, [movieId]);

  useEffect(() => {
    if (!playerOpen || playingFeature) {
      setPlaybackSubtitles([]);
      setSelectedSubtitleIndex(null);
      return;
    }

    const controller = new AbortController();
    void fetch(`/api/movies/${movieId}/playback`, {
      credentials: "include",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 401) {
          await navigate({ to: "/login" });
          return null;
        }
        if (!response.ok) return null;
        return (await response.json()) as MoviePlaybackReadiness;
      })
      .then((readiness) => {
        if (!readiness) return;
        setPlaybackSubtitles(readiness.subtitles);
        setSelectedSubtitleIndex((current) =>
          readiness.subtitles.some((track) => track.index === current) ? current : null,
        );
      })
      .catch((requestError) => {
        if (!(requestError instanceof DOMException && requestError.name === "AbortError")) {
          setPlaybackSubtitles([]);
          setSelectedSubtitleIndex(null);
        }
      });

    return () => controller.abort();
  }, [movieId, navigate, playerOpen, playingFeature]);

  const savePlaybackProgress = useCallback(
    (positionSeconds: number, durationSeconds: number, completed: boolean) => {
      void fetch(`/api/user-state/movies/${movieId}`, {
        method: "PUT",
        credentials: "include",
        keepalive: true,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          position_seconds: positionSeconds,
          duration_seconds: durationSeconds,
          completed,
        }),
      });
      if (completed) setResumeSeconds(0);
      else if (positionSeconds >= 30) {
        setResumeSeconds(positionSeconds);
      }
    },
    [movieId],
  );

  const toggleExclusive = useCallback(async () => {
    setExclusiveSubmitting(true);
    setExclusiveError(null);
    try {
      const response = await fetch(`/api/movies/${movieId}/exclusive`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(body?.detail ?? "Exclusive Movies confirmation failed");
      }
      const state = (await response.json()) as {
        is_exclusive_movie: boolean;
      };
      setDetails((current) =>
        current
          ? {
              ...current,
              is_exclusive_movie: state.is_exclusive_movie,
            }
          : current,
      );
    } catch (requestError) {
      setExclusiveError(
        requestError instanceof Error
          ? requestError.message
          : "Exclusive Movies confirmation failed",
      );
    } finally {
      setExclusiveSubmitting(false);
    }
  }, [movieId, navigate]);

  const cast = useMemo(
    () => details?.people.filter((person) => person.type === "Actor") ?? [],
    [details],
  );
  const crew = useMemo(
    () => details?.people.filter((person) => person.type !== "Actor") ?? [],
    [details],
  );

  if (error) {
    return (
      <div className="max-w-6xl mx-auto space-y-6">
        <Link
          to="/app/movies"
          className="inline-flex items-center gap-2 text-sm"
          style={{ color: "var(--pv-text-dim)" }}
        >
          <ArrowLeft size={16} />
          Movies
        </Link>
        <div className="pv-panel p-10 text-sm text-center text-red-300">{error}</div>
      </div>
    );
  }

  if (!details) {
    return (
      <div className="max-w-6xl mx-auto">
        <div className="pv-panel p-10 text-sm text-center" style={{ color: "var(--pv-text-dim)" }}>
          Opening the archive record...
        </div>
      </div>
    );
  }

  const runtime = formatRuntime(details.runtime_minutes);

  return (
    <div className="max-w-7xl mx-auto space-y-8">
      <Link to="/app/movies" className="pv-text-link inline-flex items-center gap-2 text-sm">
        <ArrowLeft size={16} />
        Movies
      </Link>

      <section
        className="pv-panel relative overflow-hidden min-h-[520px]"
        style={{ background: "#09090b" }}
      >
        {details.backdrop_url && (
          <img
            src={details.backdrop_url}
            alt=""
            className="absolute inset-0 h-full w-full object-cover opacity-40"
          />
        )}
        <div
          className="absolute inset-0"
          style={{
            background:
              "linear-gradient(90deg, rgba(8,8,10,0.98) 0%, rgba(8,8,10,0.82) 48%, rgba(8,8,10,0.34) 100%), linear-gradient(0deg, #08080a 0%, transparent 55%)",
          }}
        />

        <div className="relative z-10 flex flex-col md:flex-row gap-8 items-start p-6 md:p-10 min-h-[520px]">
          <div
            className="w-44 sm:w-52 md:w-60 shrink-0 aspect-[2/3] overflow-hidden rounded-md flex items-center justify-center"
            style={{
              background: "#111116",
              border: "1px solid var(--pv-border)",
              boxShadow: "0 24px 70px rgba(0,0,0,0.45)",
            }}
          >
            {details.poster_url ? (
              <img
                src={details.poster_url}
                alt={`${details.title} poster`}
                className="h-full w-full object-cover"
              />
            ) : (
              <Film size={46} style={{ color: "var(--pv-silver-dim)" }} />
            )}
          </div>

          <div className="flex-1 self-end max-w-3xl py-2">
            {details.tagline && (
              <p
                className="text-xs uppercase tracking-[0.22em] mb-3"
                style={{ color: "var(--pv-gold)" }}
              >
                {details.tagline}
              </p>
            )}
            <h1 className="pv-content-title text-3xl tracking-tight sm:text-4xl md:text-5xl">
              {details.title}
            </h1>

            <div
              className="flex flex-wrap items-center gap-x-4 gap-y-2 mt-4 text-sm"
              style={{ color: "var(--pv-text-dim)" }}
            >
              {details.year && <span>{details.year}</span>}
              {details.edition && <span>{details.edition}</span>}
              {details.official_rating && (
                <span
                  className="px-2 py-0.5 rounded-sm text-xs"
                  style={{ border: "1px solid var(--pv-border)" }}
                >
                  {details.official_rating}
                </span>
              )}
              {runtime && (
                <span className="inline-flex items-center gap-1.5">
                  <Clock3 size={14} />
                  {runtime}
                </span>
              )}
              {details.community_rating !== null && (
                <span className="inline-flex items-center gap-1.5">
                  <Star size={14} fill="currentColor" style={{ color: "var(--pv-gold)" }} />
                  {details.community_rating.toFixed(1)}
                </span>
              )}
            </div>

            {details.genres.length > 0 && (
              <div className="flex flex-wrap gap-2 mt-4">
                {details.genres.map((genre) => (
                  <span
                    key={genre}
                    className="px-2.5 py-1 rounded-full text-xs"
                    style={{
                      color: "var(--pv-silver)",
                      background: "rgba(255,255,255,0.06)",
                      border: "1px solid var(--pv-border)",
                    }}
                  >
                    {genre}
                  </span>
                ))}
              </div>
            )}

            {details.overview && (
              <p
                className="mt-6 text-sm sm:text-base leading-7 max-w-2xl"
                style={{ color: "var(--pv-text)" }}
              >
                {details.overview}
              </p>
            )}

            <div className="mt-7 flex flex-wrap items-center gap-3">
              <button
                type="button"
                className="inline-flex items-center gap-2 rounded-md px-5 py-2.5 text-sm font-semibold transition-transform hover:scale-[1.02]"
                style={{ background: "var(--pv-gold)", color: "#09090b" }}
                onClick={() => {
                  setResumeRequested(false);
                  setPlaybackError(false);
                  setPlayingFeature(null);
                  setPlayerOpen(true);
                }}
              >
                <Play size={18} fill="currentColor" /> Play movie
              </button>
              {resumeSeconds >= 30 && (
                <button
                  type="button"
                  className="pv-btn-ghost inline-flex items-center gap-2"
                  onClick={() => {
                    setResumeRequested(true);
                    setPlaybackError(false);
                    setPlayingFeature(null);
                    setPlayerOpen(true);
                  }}
                >
                  <RotateCcw size={16} /> Continue at {formatPlaybackTime(resumeSeconds)}
                </button>
              )}
              <details className="relative">
                <summary className="pv-btn-ghost flex cursor-pointer list-none items-center gap-2">
                  <Download size={16} /> Download
                </summary>
                <div
                  className="absolute left-0 top-full z-20 mt-2 min-w-52 overflow-hidden rounded-md border bg-[#111115] p-1 shadow-2xl"
                  style={{ borderColor: "var(--pv-border)" }}
                >
                  <a
                    className="block rounded px-3 py-2 text-sm hover:bg-white/5"
                    href={`/api/movies/${movieId}/download/original`}
                  >
                    Original file
                  </a>
                  <a
                    className="block rounded px-3 py-2 text-sm hover:bg-white/5"
                    href={`/api/movies/${movieId}/download/compressed.mp4`}
                  >
                    Compressed MP4
                  </a>
                </div>
              </details>
              <button
                type="button"
                className="pv-btn-ghost"
                onClick={() => void toggleExclusive()}
                disabled={exclusiveSubmitting}
              >
                {exclusiveSubmitting
                  ? "Updating…"
                  : details.is_exclusive_movie
                    ? "Remove from Exclusive"
                    : "Mark as Exclusive"}
              </button>
            </div>
          </div>
        </div>
      </section>

      {cast.length > 0 && (
        <section className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
              Cast
            </h2>
            <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
              {cast.length} credited performers
            </p>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 lg:grid-cols-8 gap-3">
            {cast.map((person) => (
              <article key={`${person.name}-${person.role}`} className="pv-panel overflow-hidden">
                <div
                  className="aspect-[2/3] flex items-center justify-center"
                  style={{ background: "#111116" }}
                >
                  {person.image_url ? (
                    <img
                      src={person.image_url}
                      alt={person.name}
                      loading="lazy"
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    <UserRound size={30} style={{ color: "var(--pv-silver-dim)" }} />
                  )}
                </div>
                <div className="p-3">
                  <h3 className="text-xs font-semibold" style={{ color: "var(--pv-silver)" }}>
                    {person.name}
                  </h3>
                  {person.role && (
                    <p className="text-[11px] mt-1" style={{ color: "var(--pv-text-dim)" }}>
                      {person.role}
                    </p>
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      <div className="grid lg:grid-cols-[1.5fr_1fr] gap-6">
        {crew.length > 0 && (
          <section className="pv-panel p-6">
            <h2 className="text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
              Credits
            </h2>
            <div className="grid sm:grid-cols-2 gap-x-8 gap-y-4 mt-5">
              {crew.map((person, index) => (
                <div key={`${person.name}-${person.role}-${index}`}>
                  <p className="text-sm" style={{ color: "var(--pv-silver)" }}>
                    {person.name}
                  </p>
                  <p className="text-xs mt-0.5" style={{ color: "var(--pv-text-dim)" }}>
                    {person.role ?? person.type ?? "Crew"}
                  </p>
                </div>
              ))}
            </div>
          </section>
        )}

        <section className="pv-panel p-6 space-y-6">
          <div>
            <h2 className="text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
              Archive record
            </h2>
            <dl className="mt-5 space-y-3 text-sm">
              <div className="flex justify-between gap-5">
                <dt style={{ color: "var(--pv-text-dim)" }}>Container</dt>
                <dd className="uppercase" style={{ color: "var(--pv-silver)" }}>
                  {details.container ?? "Unknown"}
                </dd>
              </div>
              <div className="flex justify-between gap-5">
                <dt style={{ color: "var(--pv-text-dim)" }}>Video</dt>
                <dd className="uppercase" style={{ color: "var(--pv-silver)" }}>
                  {details.video_codec ?? "Unknown"}
                </dd>
              </div>
              <div className="flex justify-between gap-5">
                <dt style={{ color: "var(--pv-text-dim)" }}>Audio</dt>
                <dd className="uppercase text-right" style={{ color: "var(--pv-silver)" }}>
                  {details.audio_codecs.join(", ") || "Unknown"}
                </dd>
              </div>
              {details.subtitles.length > 0 && (
                <div className="flex justify-between gap-5">
                  <dt style={{ color: "var(--pv-text-dim)" }}>Subtitles</dt>
                  <dd className="text-right" style={{ color: "var(--pv-silver)" }}>
                    {details.subtitles
                      .map(
                        (subtitle) =>
                          subtitle.title ?? subtitle.language ?? subtitle.codec ?? "Subtitle track",
                      )
                      .join(", ")}
                  </dd>
                </div>
              )}
            </dl>
          </div>

          {details.collections.length > 0 && (
            <div>
              <h3
                className="text-xs uppercase tracking-wider"
                style={{ color: "var(--pv-text-dim)" }}
              >
                Collections
              </h3>
              <p className="text-sm leading-6 mt-2" style={{ color: "var(--pv-silver)" }}>
                {details.collections.join(" · ")}
              </p>
            </div>
          )}

          {details.studios.length > 0 && (
            <div>
              <h3
                className="text-xs uppercase tracking-wider"
                style={{ color: "var(--pv-text-dim)" }}
              >
                Studios
              </h3>
              <p className="text-sm leading-6 mt-2" style={{ color: "var(--pv-silver)" }}>
                {details.studios.join(" · ")}
              </p>
            </div>
          )}

          {details.provider_imported_at && (
            <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              Catalogue refreshed {new Date(details.provider_imported_at).toLocaleString("en-GB")}
            </p>
          )}
        </section>
      </div>

      <section className="space-y-4">
        {details.trailers.length > 0 && (
          <>
            <div>
              <h2 className="text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
                Trailers
              </h2>
              <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
                {details.trailers.length} {details.trailers.length === 1 ? "trailer" : "trailers"}{" "}
                indexed
              </p>
            </div>
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {details.trailers.map((trailer) => (
                <button
                  key={trailer.id}
                  type="button"
                  disabled={!trailer.playback_available}
                  className="pv-panel overflow-hidden text-left transition-transform enabled:hover:scale-[1.01] disabled:cursor-default"
                  onClick={() => {
                    setPlaybackError(false);
                    setPlayingFeature(trailer);
                    setPlayerOpen(true);
                  }}
                >
                  {trailer.thumbnail_url ? (
                    <div className="relative aspect-video bg-black">
                      <img
                        src={trailer.thumbnail_url}
                        alt=""
                        className="h-full w-full object-cover"
                      />
                      <Play
                        className="absolute inset-0 m-auto"
                        size={30}
                        fill="currentColor"
                        style={{ color: "var(--pv-gold)" }}
                      />
                    </div>
                  ) : (
                    <div className="p-4 flex items-center gap-3">
                      <span
                        className="h-10 w-10 shrink-0 rounded-md flex items-center justify-center"
                        style={{
                          background: "rgba(255,255,255,0.04)",
                          border: "1px solid var(--pv-border)",
                        }}
                      >
                        <Play size={17} style={{ color: "var(--pv-gold)" }} />
                      </span>
                      <div className="min-w-0">
                        <h3
                          className="text-sm font-medium truncate"
                          style={{ color: "var(--pv-silver)" }}
                        >
                          {trailer.title}
                        </h3>
                        <p className="text-xs mt-0.5" style={{ color: "var(--pv-text-dim)" }}>
                          {formatRuntime(trailer.runtime_minutes) ?? "Runtime unavailable"}
                        </p>
                      </div>
                    </div>
                  )}
                  {trailer.thumbnail_url && (
                    <div className="p-4">
                      <h3
                        className="text-sm font-medium truncate"
                        style={{ color: "var(--pv-silver)" }}
                      >
                        {trailer.title}
                      </h3>
                      <p className="text-xs mt-0.5" style={{ color: "var(--pv-text-dim)" }}>
                        {formatRuntime(trailer.runtime_minutes) ?? "Runtime unavailable"}
                      </p>
                    </div>
                  )}
                </button>
              ))}
            </div>
          </>
        )}

        <div>
          <h2 className="text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
            Special features
          </h2>
          <p className="text-xs mt-1 max-w-3xl" style={{ color: "var(--pv-text-dim)" }}>
            {details.extras.length > 0
              ? `${details.extras.length} special features are indexed. Their current disc-source names are preserved until Vault Master can identify and propose clearer titles.`
              : "No special features are currently indexed."}
          </p>
        </div>

        {details.extras.length > 0 && (
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {details.extras.map((extra) => (
              <button
                key={extra.id}
                type="button"
                disabled={!extra.playback_available}
                className="pv-panel overflow-hidden text-left transition-transform enabled:hover:scale-[1.01] disabled:cursor-default"
                onClick={() => {
                  setPlaybackError(false);
                  setPlayingFeature(extra);
                  setPlayerOpen(true);
                }}
              >
                {extra.thumbnail_url ? (
                  <div className="relative aspect-video bg-black">
                    <img src={extra.thumbnail_url} alt="" className="h-full w-full object-cover" />
                    <Play
                      className="absolute inset-0 m-auto"
                      size={30}
                      fill="currentColor"
                      style={{ color: "var(--pv-gold)" }}
                    />
                  </div>
                ) : (
                  <div className="p-4 flex items-center gap-3">
                    <span
                      className="h-10 w-10 shrink-0 rounded-md flex items-center justify-center"
                      style={{
                        background: "rgba(255,255,255,0.04)",
                        border: "1px solid var(--pv-border)",
                      }}
                    >
                      <Film size={18} style={{ color: "var(--pv-gold)" }} />
                    </span>
                    <div className="min-w-0">
                      <h3
                        className="text-sm font-medium truncate"
                        style={{ color: "var(--pv-silver)" }}
                      >
                        {extra.title}
                      </h3>
                      <p className="text-xs mt-0.5" style={{ color: "var(--pv-text-dim)" }}>
                        {formatRuntime(extra.runtime_minutes) ?? "Runtime unavailable"}
                      </p>
                    </div>
                  </div>
                )}
                {extra.thumbnail_url && (
                  <div className="p-4">
                    <h3
                      className="text-sm font-medium truncate"
                      style={{ color: "var(--pv-silver)" }}
                    >
                      {extra.title}
                    </h3>
                    <p className="text-xs mt-0.5" style={{ color: "var(--pv-text-dim)" }}>
                      {formatRuntime(extra.runtime_minutes) ?? "Runtime unavailable"}
                    </p>
                  </div>
                )}
              </button>
            ))}
          </div>
        )}
      </section>

      <Dialog
        open={playerOpen}
        onOpenChange={(open) => {
          setPlayerOpen(open);
          if (!open) {
            setPlaybackError(false);
            setPlaybackSubtitles([]);
            setSelectedSubtitleIndex(null);
          }
        }}
      >
        <DialogContent
          className="pv-dialog max-w-5xl p-0 overflow-hidden"
          style={{
            background: "#08080a",
            borderColor: "var(--pv-border)",
          }}
        >
          <DialogHeader className="px-6 pt-6">
            <DialogTitle style={{ color: "var(--pv-silver)" }}>
              {playingFeature?.title ?? details.title}
            </DialogTitle>
            <DialogDescription style={{ color: "var(--pv-text-dim)" }}>
              {details.year ?? "Personal Vault Theatre"}
            </DialogDescription>
          </DialogHeader>
          <div className="px-6 pb-6">
            <div
              className="aspect-video overflow-hidden rounded-md bg-black flex items-center justify-center"
              style={{ border: "1px solid var(--pv-border)" }}
            >
              {!playbackError ? (
                <MoviePlayer
                  key={playingFeature?.id ?? details.id}
                  source={
                    playingFeature
                      ? `/api/movies/${details.id}/features/${playingFeature.id}/hls/master.m3u8`
                      : `/api/movies/${details.id}/hls/master.m3u8${
                          selectedSubtitleIndex === null
                            ? ""
                            : `?subtitle_index=${selectedSubtitleIndex}`
                        }`
                  }
                  onPlaybackError={handlePlaybackError}
                  startSeconds={!playingFeature && resumeRequested ? resumeSeconds : 0}
                  onProgress={!playingFeature ? savePlaybackProgress : undefined}
                  subtitleTracks={
                    playingFeature
                      ? []
                      : playbackSubtitles.map((track) => ({
                          index: track.index,
                          label: subtitleTrackLabel(track, playbackSubtitles),
                        }))
                  }
                  selectedSubtitleIndex={playingFeature ? null : selectedSubtitleIndex}
                  onSubtitleChange={playingFeature ? undefined : setSelectedSubtitleIndex}
                />
              ) : (
                <div className="px-6 text-center">
                  <p className="text-sm text-red-300">Playback could not be started.</p>
                  <p className="text-xs mt-2" style={{ color: "var(--pv-text-dim)" }}>
                    Close the player and try again.
                  </p>
                </div>
              )}
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {exclusiveError && (
        <p className="rounded-md border border-red-900/70 bg-red-950/20 p-3 text-sm text-red-200">
          {exclusiveError}
        </p>
      )}
    </div>
  );
}
