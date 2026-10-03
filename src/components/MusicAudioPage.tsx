import { Link, useNavigate } from "@tanstack/react-router";
import { RefreshCw, Search, X } from "lucide-react";
import { useEffect, useState } from "react";

import { MusicLibrary } from "../components/MusicLibrary";

import { useMusicPlayer } from "../hooks/useMusicPlayer";
import type { MusicTrack } from "../lib/music-types";

type AlbumCandidate = {
  release_group_id?: string | null;
  disambiguation?: string | null;
  formats?: string[];
  barcode?: string | null;
  catalog_numbers?: string[];
  release_id: string;
  title: string;
  artist: string;
  date: string | null;
  country: string | null;
  track_count: number;
  score: number;
  cover_art_available: boolean;
};

type LocalMatch = {
  asset_id: string;
  filename: string;
  title: string;
  proposed_track: string | null;
  suggested_track: string | null;
  status: string;
  score: number;
  reason: string;
  warning?: string | null;
  local_duration?: number | null;
  provider_duration?: number | null;
};

type AlbumPreview = {
  review_revision?: string;
  local_matches?: LocalMatch[];
  order_ready?: boolean;
  identity_conflicts?: string[];
  folder: string;
  release_id: string;
  title: string;
  artist: string;
  date: string | null;
  country: string | null;
  genres: string[];
  cover_art_available: boolean;
  local_track_count: number;
  matched_track_count: number;
  unmatched_local_files: string[];
  tracks: Array<{
    asset_id: string | null;
    filename: string | null;
    disc_number: number;
    track_number: number;
    title: string;
    artist: string;
    matched: boolean;
    duration_seconds?: number | null;
  }>;
};

export function MusicAudioPage({ section }: { section: "albums" | "songs" }) {
  const navigate = useNavigate();
  const [tracks, setTracks] = useState<MusicTrack[] | null>(null);
  const player = useMusicPlayer((track) => void showLyrics(track));
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lyrics, setLyrics] = useState<{ trackId: string; text: string } | null>(null);
  const [reviewGroup, setReviewGroup] = useState<string | null>(null);
  const [reviewFolder, setReviewFolder] = useState<string | null>(null);
  const [identityArtist, setIdentityArtist] = useState("");
  const [identityAlbum, setIdentityAlbum] = useState("");
  const [candidates, setCandidates] = useState<AlbumCandidate[]>([]);
  const [albumPreview, setAlbumPreview] = useState<AlbumPreview | null>(null);
  const [mapping, setMapping] = useState<Record<string, string | null>>({});
  const [applyOrder, setApplyOrder] = useState(false);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/music", {
      credentials: "include",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 401) {
          await navigate({ to: "/login" });
          return null;
        }
        if (!response.ok) throw new Error("Music request failed");
        return (await response.json()) as MusicTrack[];
      })
      .then((items) => items && setTracks(items))
      .catch((requestError) => {
        if (!(requestError instanceof DOMException && requestError.name === "AbortError")) {
          setError("Music is currently unavailable.");
        }
      });
    return () => controller.abort();
  }, [navigate]);

  const refreshMetadata = async () => {
    setRefreshing(true);
    setError(null);
    try {
      const response = await fetch("/api/music/refresh", {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) throw new Error("Music refresh failed");
      const updated = await fetch("/api/music", {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!updated.ok) throw new Error("Music reload failed");
      setTracks((await updated.json()) as MusicTrack[]);
    } catch {
      setError("Music information could not be refreshed from the playback service.");
    } finally {
      setRefreshing(false);
    }
  };

  const showLyrics = async (track: MusicTrack) => {
    setError(null);
    try {
      const response = await fetch(`/api/music/${track.id}/lyrics`, {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Lyrics request failed");
      const result = (await response.json()) as { text: string };
      setLyrics({ trackId: track.id, text: result.text });
    } catch {
      setError("Lyrics are not available for this track.");
    }
  };

  const startAlbumReview = (track: MusicTrack) => {
    setReviewFolder(track.album_folder);
    setReviewGroup(track.album_group_id ?? null);
    setIdentityArtist(track.artist.toLowerCase().startsWith("unknown") ? "" : track.artist);
    setIdentityAlbum(
      track.album.toLowerCase().startsWith("unknown")
        ? (track.album_folder.split("/").at(-1)?.split(" - ").slice(1).join(" - ") ?? "")
        : track.album,
    );
    setCandidates([]);
    setAlbumPreview(null);
    setReviewError(null);
  };

  const closeAlbumReview = () => {
    setReviewFolder(null);
    setCandidates([]);
    setAlbumPreview(null);
    setReviewError(null);
  };

  const searchAlbum = async () => {
    if (!reviewFolder || !identityArtist.trim() || !identityAlbum.trim()) return;
    setReviewBusy(true);
    setReviewError(null);
    setAlbumPreview(null);
    setCandidates([]);
    try {
      const response = await fetch("/api/vault-master/music/albums/search", {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          folder: reviewFolder,
          ...(reviewGroup ? { album_group_id: reviewGroup } : {}),
          artist: identityArtist.trim(),
          album: identityAlbum.trim(),
        }),
      });
      if (response.status === 422) {
        const body = await response.json();
        setReviewError(
          typeof body.detail === "string"
            ? body.detail
            : "The album details are invalid. Check the search fields.",
        );
        return;
      }
      if (!response.ok) {
        const messages: Record<number, string> = {
          401: "Your session has expired. Sign in again to search for this album.",
          403: "You do not have permission to search for this album.",
          503: "The online music catalogue is temporarily unavailable. Please try again shortly.",
          502: "The online music catalogue returned a technical error. Please try again later.",
        };
        setReviewError(
          messages[response.status] ??
            "The album search could not be completed because of a server error.",
        );
        return;
      }
      const result = (await response.json()) as { candidates: AlbumCandidate[] };
      setCandidates(result.candidates);
      if (!result.candidates.length) {
        setReviewError("No matching releases were found. Check the artist and album names.");
      }
    } catch {
      setReviewError("The album search connection failed. Check your connection and try again.");
    } finally {
      setReviewBusy(false);
    }
  };

  const previewAlbum = async (releaseId: string) => {
    if (!reviewFolder) return;
    setReviewBusy(true);
    setReviewError(null);
    try {
      const response = await fetch("/api/vault-master/music/albums/preview", {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          folder: reviewFolder,
          ...(reviewGroup ? { album_group_id: reviewGroup } : {}),
          release_id: releaseId,
        }),
      });
      if (response.status === 422) {
        const body = await response.json();
        setReviewError(
          typeof body.detail === "string"
            ? body.detail
            : "The album details are invalid. Check the search fields.",
        );
        return;
      }
      if (!response.ok) {
        const body = await response.json();
        setReviewError(
          typeof body.detail === "string"
            ? body.detail
            : "The selected release could not be reviewed.",
        );
        return;
      }
      const preview = (await response.json()) as AlbumPreview;
      setAlbumPreview(preview);
      setMapping(
        Object.fromEntries(
          (preview.local_matches ?? []).map((row) => [row.asset_id, row.proposed_track]),
        ),
      );
      setApplyOrder(!preview.order_ready);
    } catch {
      setReviewError("The selected release could not be reviewed.");
    } finally {
      setReviewBusy(false);
    }
  };

  const approveAlbum = async () => {
    if (!reviewFolder || !albumPreview) return;
    setReviewBusy(true);
    setReviewError(null);
    try {
      const response = await fetch("/api/vault-master/music/albums/approve", {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({
          folder: reviewFolder,
          ...(reviewGroup ? { album_group_id: reviewGroup } : {}),
          release_id: albumPreview.release_id,
          ...(albumPreview.review_revision
            ? {
                review_revision: albumPreview.review_revision,
                mapping: Object.entries(mapping).map(([asset_id, provider_track]) => ({
                  asset_id,
                  provider_track,
                })),
                apply_order: applyOrder,
              }
            : {}),
        }),
      });
      if (!response.ok) {
        const body = await response.json();
        setReviewError(
          typeof body.detail === "string"
            ? body.detail
            : "Approval failed. Review the mapping again.",
        );
        return;
      }
      const approval = await response.json();
      if (approval.sidecars_exported === false) {
        setReviewError(
          "Mapping saved in the catalogue. Portable metadata export needs attention; please review again before retrying approval.",
        );
        return;
      }
      const updated = await fetch("/api/music", {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!updated.ok) throw new Error("Music reload failed");
      setTracks((await updated.json()) as MusicTrack[]);
      closeAlbumReview();
    } catch {
      setReviewError("The reviewed album information could not be retained.");
    } finally {
      setReviewBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-7xl min-w-0 space-y-5 pb-8">
      <nav aria-label="Music navigation" className="flex min-h-11 items-center">
        <Link to="/app/music" className="pv-btn-ghost">
          Back to Music
        </Link>
      </nav>
      <header className="min-w-0 space-y-1">
        <h1 id={`music-${section}-heading`} className="pv-content-title text-xl">
          {section === "albums" ? "Albums" : "Songs"}
        </h1>
        <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
          {tracks === null
            ? "Opening your music library..."
            : section === "albums"
              ? `${new Set(tracks.filter((track) => track.album_group_id).map((track) => track.album_group_id)).size} albums`
              : `${tracks.filter((track) => !track.album_group_id).length} songs`}
        </p>
      </header>
      <div className="flex flex-wrap justify-end gap-4">
        <button
          type="button"
          onClick={() => void refreshMetadata()}
          disabled={refreshing}
          className="pv-btn-ghost flex items-center gap-2"
        >
          <RefreshCw size={15} className={refreshing ? "animate-spin" : ""} />
          {refreshing ? "Refreshing" : "Refresh music information"}
        </button>
      </div>

      {(tracks?.length ?? 0) > 0 && (
        <label className="pv-panel flex items-center gap-3 px-4 py-3">
          <Search size={16} style={{ color: "var(--pv-text-dim)" }} />
          <span className="sr-only">Search {section}</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={
              section === "albums" ? "Search albums or artists" : "Search songs or artists"
            }
            className="w-full bg-transparent text-sm outline-none"
            style={{ color: "var(--pv-silver)" }}
          />
        </label>
      )}

      {error && <div className="pv-panel p-6 text-center text-sm text-red-300">{error}</div>}
      {tracks && (
        <MusicLibrary
          section={section}
          showHeading={false}
          tracks={tracks}
          query={query}
          playSong={(track) => void player.play(track, [track])}
          playAlbum={player.startAlbum}
          identify={startAlbumReview}
          activeTrackId={player.active?.id}
          playing={player.playing}
          onSongSaved={async () => {
            const response = await fetch("/api/music", {
              credentials: "include",
              headers: { Accept: "application/json" },
            });
            if (!response.ok) throw new Error("Music refresh failed");
            setTracks((await response.json()) as MusicTrack[]);
          }}
        />
      )}

      {player.controls}

      {reviewFolder && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4">
          <section
            className="max-h-[90vh] w-full max-w-3xl overflow-auto rounded-lg border p-6 shadow-2xl"
            style={{ background: "var(--pv-bg-elev)", borderColor: "var(--pv-border)" }}
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <p
                  className="text-xs uppercase tracking-widest"
                  style={{ color: "var(--pv-gold)" }}
                >
                  Vault Master album review
                </p>
                <h3 className="mt-2 text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
                  Identify {identityArtist} · {identityAlbum}
                </h3>
                <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  Search results are proposals. Nothing is published until you approve an exact
                  release and track match.
                </p>
              </div>
              <button
                type="button"
                onClick={closeAlbumReview}
                className="pv-btn-ghost !p-2"
                aria-label="Close album review"
              >
                <X size={16} />
              </button>
            </div>

            <div className="mt-6 grid gap-4 sm:grid-cols-2">
              <label className="space-y-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                Artist or band
                <input
                  value={identityArtist}
                  onChange={(event) => setIdentityArtist(event.target.value)}
                  className="pv-input w-full"
                  placeholder="Example Artist"
                />
              </label>
              <label className="space-y-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                Album
                <input
                  value={identityAlbum}
                  onChange={(event) => setIdentityAlbum(event.target.value)}
                  className="pv-input w-full"
                  placeholder="Example Album – Act 1"
                />
              </label>
            </div>
            <div className="mt-4 flex justify-end">
              <button
                type="button"
                onClick={() => void searchAlbum()}
                disabled={reviewBusy || !identityArtist.trim() || !identityAlbum.trim()}
                className="pv-btn-gold flex items-center gap-2"
              >
                <Search size={15} /> {reviewBusy ? "Searching" : "Find releases"}
              </button>
            </div>

            {reviewError && <p className="mt-4 text-sm text-red-300">{reviewError}</p>}

            {!albumPreview && candidates.length > 0 && (
              <div className="mt-6 space-y-3">
                <h4 className="text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
                  Possible releases
                </h4>
                {candidates.map((candidate) => (
                  <div
                    key={candidate.release_id}
                    className="flex items-center justify-between gap-4 rounded-md border p-4"
                    style={{ borderColor: "var(--pv-border)" }}
                  >
                    <div className="min-w-0">
                      <p className="break-words text-sm" style={{ color: "var(--pv-silver)" }}>
                        {candidate.title}
                        {candidate.disambiguation ? ` (${candidate.disambiguation})` : ""}
                      </p>
                      <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                        {candidate.artist}
                        {candidate.date ? ` · ${candidate.date}` : ""}
                        {candidate.country ? ` · ${candidate.country}` : ""} ·{" "}
                        {candidate.track_count} tracks
                        {candidate.formats?.length ? ` · ${candidate.formats.join(" / ")}` : ""}
                        {candidate.cover_art_available ? " · cover available" : ""}
                      </p>
                      {(candidate.barcode || candidate.catalog_numbers?.length) && (
                        <p
                          className="mt-1 break-words text-xs"
                          style={{ color: "var(--pv-text-dim)" }}
                        >
                          {candidate.barcode ? `Barcode: ${candidate.barcode}` : ""}
                          {candidate.barcode && candidate.catalog_numbers?.length ? " · " : ""}
                          {candidate.catalog_numbers?.length
                            ? `Catalog: ${candidate.catalog_numbers.join(", ")}`
                            : ""}
                        </p>
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => void previewAlbum(candidate.release_id)}
                      disabled={reviewBusy}
                      className="pv-btn-ghost shrink-0"
                    >
                      Review match
                    </button>
                  </div>
                ))}
              </div>
            )}

            {albumPreview?.identity_conflicts?.map((conflict) => (
              <p key={conflict} role="status" className="text-sm text-amber-700">
                {conflict}
              </p>
            ))}
            {albumPreview && (
              <div className="mt-6 space-y-4">
                <div>
                  <h4 className="text-base font-semibold" style={{ color: "var(--pv-silver)" }}>
                    {albumPreview.artist} — {albumPreview.title}
                  </h4>
                  <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                    {albumPreview.review_revision
                      ? Object.values(mapping).filter(Boolean).length
                      : albumPreview.matched_track_count}{" "}
                    of {albumPreview.local_track_count} tracks proposed
                    {albumPreview.cover_art_available ? " · front cover available" : ""}
                  </p>
                </div>
                {albumPreview.local_matches && albumPreview.local_matches.length > 0 && (
                  <div className="space-y-3" aria-label="Review track mapping">
                    <div className="flex flex-wrap gap-3">
                      <button
                        type="button"
                        className="pv-btn-ghost"
                        disabled={reviewBusy}
                        onClick={() =>
                          setMapping(
                            Object.fromEntries(
                              albumPreview.local_matches!.map((row) => [
                                row.asset_id,
                                row.proposed_track,
                              ]),
                            ),
                          )
                        }
                      >
                        Use confident proposals
                      </button>
                      <button
                        type="button"
                        className="pv-btn-ghost"
                        disabled={reviewBusy}
                        onClick={() =>
                          setMapping(
                            Object.fromEntries(
                              albumPreview.local_matches!.map((row) => [row.asset_id, null]),
                            ),
                          )
                        }
                      >
                        Leave all unmatched
                      </button>
                    </div>
                    {albumPreview.local_matches.map((row) => (
                      <div
                        key={row.asset_id}
                        className="rounded-md border p-3 space-y-2"
                        style={{ borderColor: "var(--pv-border)" }}
                      >
                        <label className="block text-sm">
                          <span className="block break-words mb-2">{row.filename}</span>
                          <select
                            aria-label={`Match ${row.filename}`}
                            className="w-full rounded-md p-2 bg-neutral-900 text-white"
                            disabled={reviewBusy}
                            value={mapping[row.asset_id] ?? ""}
                            onChange={(event) =>
                              setMapping((previous) => ({
                                ...previous,
                                [row.asset_id]: event.target.value || null,
                              }))
                            }
                          >
                            <option value="">Unmatched / local bonus track</option>
                            {albumPreview.tracks.map((track) => {
                              const key = `${track.disc_number}:${track.track_number}`;
                              return (
                                <option
                                  key={key}
                                  value={key}
                                  disabled={Object.entries(mapping).some(
                                    ([id, value]) => id !== row.asset_id && value === key,
                                  )}
                                >
                                  {track.disc_number}.{track.track_number} {track.title}
                                  {track.duration_seconds != null
                                    ? ` (${track.duration_seconds.toFixed(1)} s)`
                                    : ""}
                                </option>
                              );
                            })}
                          </select>
                        </label>
                        <p className="text-xs">
                          {mapping[row.asset_id] !== row.proposed_track
                            ? "Owner selection"
                            : `${row.status.replaceAll("_", " ")} · ${row.reason} (score ${row.score})`}
                        </p>
                        <p className="text-xs">
                          Local duration:{" "}
                          {row.local_duration == null
                            ? "unknown"
                            : `${row.local_duration.toFixed(1)} s`}
                          {mapping[row.asset_id] === row.proposed_track &&
                          row.provider_duration != null
                            ? ` · Proposed duration: ${row.provider_duration.toFixed(1)} s`
                            : ""}
                        </p>
                        {row.warning && <p className="text-xs text-amber-300">{row.warning}</p>}
                      </div>
                    ))}
                    <label className="flex gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={applyOrder}
                        disabled={reviewBusy}
                        onChange={(event) => setApplyOrder(event.target.checked)}
                      />
                      Use this approved mapping for album playback order. Incomplete mappings keep
                      album Play disabled.
                    </label>
                    {albumPreview.order_ready && (
                      <p className="text-xs">
                        An existing playback order is preserved unless you select the checkbox.
                      </p>
                    )}
                  </div>
                )}
                <div
                  className="max-h-72 divide-y overflow-auto rounded-md border"
                  style={{ borderColor: "var(--pv-border)" }}
                >
                  {albumPreview.tracks.map((track) => (
                    <div
                      key={`${track.disc_number}:${track.track_number}`}
                      className="flex items-center gap-3 px-4 py-3 text-sm"
                    >
                      <span className="w-10 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                        {track.disc_number}.{track.track_number}
                      </span>
                      <span
                        className="min-w-0 flex-1 truncate"
                        style={{ color: "var(--pv-silver)" }}
                      >
                        {track.title}
                      </span>
                      <span className={track.matched ? "text-emerald-300" : "text-amber-300"}>
                        {albumPreview.review_revision
                          ? (albumPreview.local_matches?.find(
                              (row) =>
                                mapping[row.asset_id] ===
                                `${track.disc_number}:${track.track_number}`,
                            )?.filename ?? "Missing locally / unmatched")
                          : track.matched
                            ? track.filename
                            : "No matching track"}
                      </span>
                    </div>
                  ))}
                </div>
                {!albumPreview.review_revision && albumPreview.unmatched_local_files.length > 0 && (
                  <div className="rounded-md border border-amber-400/30 bg-amber-400/5 p-4">
                    <p className="text-xs font-semibold text-amber-200">Files without a match</p>
                    <p className="mt-2 text-xs text-amber-100/80">
                      {albumPreview.unmatched_local_files.join(", ")}
                    </p>
                  </div>
                )}
                <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  {reviewGroup
                    ? "Approval retains release information and available cover for this album. Only matched tracks receive track information. Your album identity and manual corrections are preserved. Playback order changes only when explicitly selected above."
                    : "Approval stores the selected titles, artist, album identity and available cover inside Vault Master."}{" "}
                  Original audio files are never renamed or retagged.
                </p>
                <div className="flex justify-between gap-3">
                  <button
                    type="button"
                    onClick={() => setAlbumPreview(null)}
                    disabled={reviewBusy}
                    className="pv-btn-ghost"
                  >
                    Back to results
                  </button>
                  <button
                    type="button"
                    onClick={() => void approveAlbum()}
                    disabled={
                      reviewBusy || (!reviewGroup && albumPreview.matched_track_count === 0)
                    }
                    className="pv-btn-gold"
                  >
                    {reviewBusy
                      ? "Retaining"
                      : albumPreview.review_revision
                        ? "Approve release and mapping"
                        : "Approve album information"}
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>
      )}

      {lyrics && (
        <aside
          className="fixed bottom-20 right-5 z-40 max-h-[60vh] w-[min(28rem,calc(100vw-2.5rem))] overflow-auto rounded-lg border p-5 shadow-2xl"
          style={{ background: "var(--pv-bg-elev)", borderColor: "var(--pv-border)" }}
        >
          <div className="mb-4 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
              Lyrics
            </h3>
            <button
              type="button"
              onClick={() => setLyrics(null)}
              className="pv-btn-ghost !p-2"
              aria-label="Close lyrics"
            >
              <X size={15} />
            </button>
          </div>
          <p className="whitespace-pre-line text-sm leading-7" style={{ color: "var(--pv-text)" }}>
            {lyrics.text}
          </p>
        </aside>
      )}
    </div>
  );
}
