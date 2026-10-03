import { Link } from "@tanstack/react-router";
import { Disc3, Music2, Play } from "lucide-react";

import { orderedAlbum, type LibraryTrack } from "../lib/music-album-order";
import { MusicSongRow } from "./MusicSongRow";
export type { LibraryTrack } from "../lib/music-album-order";

export function MusicLibrary<T extends LibraryTrack>({
  tracks,
  query,
  playAlbum,
  playSong,
  identify,
  activeTrackId,
  playing = false,
  onSongSaved,
  section,
  showHeading = true,
}: {
  tracks: T[];
  query: string;
  playAlbum: (tracks: T[]) => void;
  playSong: (track: T) => void;
  identify: (track: T) => void;
  activeTrackId?: string;
  playing?: boolean;
  onSongSaved?: () => Promise<void>;
  section: "albums" | "songs";
  showHeading?: boolean;
}) {
  const groups = new Map<string, T[]>();
  for (const track of tracks) {
    if (track.album_group_id)
      groups.set(track.album_group_id, [...(groups.get(track.album_group_id) ?? []), track]);
  }
  const match = (track: T) =>
    `${track.title} ${track.artist} ${track.album_group_id ? track.album : ""}`
      .toLocaleLowerCase()
      .includes(query.trim().toLocaleLowerCase());
  const albums = [...groups.entries()].filter(([, members]) => members.some(match));
  const songs = tracks.filter((track) => !track.album_group_id && match(track));
  const empty =
    "rounded-xl border border-[var(--pv-border)] bg-white/[0.015] px-6 py-10 text-center text-sm text-[var(--pv-text-dim)]";
  return (
    <div className="space-y-10">
      {section === "albums" && (
        <section
          id="music-albums"
          aria-labelledby="music-albums-heading"
          className="scroll-mt-24 space-y-4"
        >
          {showHeading && (
            <h2 id="music-albums-heading" className="pv-content-title text-xl">
              Albums
            </h2>
          )}
          {!albums.length ? (
            <div className={empty}>
              <Disc3 aria-hidden="true" className="mx-auto mb-3" />
              {query ? "No matching albums." : "No albums yet."}
            </div>
          ) : (
            <div className="pv-card-grid">
              {albums.map(([id, members]) => {
                const first = members[0];
                const ordered = orderedAlbum(members);
                const artwork = members.find((track) => track.artwork_url)?.artwork_url;
                return (
                  <article
                    key={id}
                    className="min-w-0 rounded-xl border border-[var(--pv-border)] bg-white/[0.02]"
                  >
                    <div className="relative aspect-square overflow-hidden rounded-t-xl bg-black/30">
                      <Link
                        to="/app/music/albums/$albumId"
                        params={{ albumId: id }}
                        aria-label={`Open ${first.album}`}
                        className="flex h-full items-center justify-center focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--pv-gold)]"
                      >
                        {artwork ? (
                          <img
                            src={artwork}
                            alt=""
                            className="h-full w-full object-cover"
                            loading="lazy"
                          />
                        ) : (
                          <Disc3
                            size={64}
                            aria-hidden="true"
                            className="text-[var(--pv-gold-dim)]"
                          />
                        )}
                      </Link>
                      <button
                        type="button"
                        aria-label={`Play ${first.album}`}
                        disabled={!ordered}
                        aria-describedby={!ordered ? `order-${id}` : undefined}
                        onClick={() => ordered && playAlbum(ordered)}
                        className="absolute left-1/2 top-1/2 flex h-11 w-11 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-white/25 bg-black/65 text-white shadow-md hover:bg-black/85 focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--pv-gold)] disabled:opacity-35"
                      >
                        <Play size={18} aria-hidden="true" />
                      </button>
                    </div>
                    <div className="space-y-1 p-3">
                      <Link
                        to="/app/music/albums/$albumId"
                        params={{ albumId: id }}
                        className="block break-words font-semibold text-[var(--pv-silver)]"
                      >
                        {first.album}
                      </Link>
                      <p className="break-words text-sm text-[var(--pv-text-dim)]">
                        {first.album_artist ?? first.artist}
                        {first.release_year ? ` · ${first.release_year}` : ""}
                      </p>
                      {!ordered && (
                        <p id={`order-${id}`} className="text-xs text-[var(--pv-text-dim)]">
                          Track order needs attention.
                        </p>
                      )}
                      {first.can_edit && (
                        <button
                          type="button"
                          onClick={() => identify(first)}
                          className="pv-btn-ghost min-h-11 !px-0 text-xs"
                        >
                          Identify album
                        </button>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      )}
      {section === "songs" && (
        <section
          id="music-songs"
          aria-labelledby="music-songs-heading"
          className="scroll-mt-24 space-y-4"
        >
          {showHeading && (
            <h2 id="music-songs-heading" className="pv-content-title text-xl">
              Songs
            </h2>
          )}
          {!songs.length ? (
            <div className={empty}>
              <Music2 aria-hidden="true" className="mx-auto mb-3" />
              {query ? "No matching songs." : "No standalone songs yet."}
            </div>
          ) : (
            <div className="divide-y divide-[var(--pv-border)] rounded-xl border border-[var(--pv-border)]">
              {songs.map((track) => (
                <MusicSongRow
                  key={track.asset_id}
                  track={track}
                  play={() => playSong(track)}
                  identify={() => identify(track)}
                  active={activeTrackId === track.id}
                  playing={playing}
                  onSaved={onSongSaved}
                />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
