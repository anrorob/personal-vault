import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { Disc3, Play } from "lucide-react";
import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "../components/ui/dialog";
import { orderedAlbum } from "../lib/music-album-order";
import type { MusicTrack } from "../lib/music-types";
import { useMusicPlayer } from "../hooks/useMusicPlayer";
import { MusicAlbumPlayer } from "../components/MusicAlbumPlayer";

export const Route = createFileRoute("/app/music_/albums/$albumId")({ component: AlbumPage });
type Album = {
  id: string;
  title: string;
  artist: string;
  can_edit: boolean;
  order_state: string;
  tracks: MusicTrack[];
  artwork_url: string | null;
  release_year: number | null;
};

function AlbumPage() {
  const { albumId } = Route.useParams();
  return <AlbumDetails key={albumId} albumId={albumId} />;
}

function AlbumDetails({ albumId }: { albumId: string }) {
  const navigate = useNavigate();
  const [album, setAlbum] = useState<Album | null>(null);
  const [status, setStatus] = useState("Opening album�");
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [artist, setArtist] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [artFailed, setArtFailed] = useState(false);
  const player = useMusicPlayer(undefined, true);
  useEffect(() => {
    const controller = new AbortController();
    void fetch(`/api/music/albums/${encodeURIComponent(albumId)}`, {
      credentials: "include",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 401) {
          await navigate({ to: "/login" });
          return;
        }
        if (!response.ok) throw new Error("Album unavailable");
        const result = (await response.json()) as Album;
        if (!controller.signal.aborted) setAlbum(result);
      })
      .catch(() => {
        if (!controller.signal.aborted) setStatus("This album is not available.");
      });
    return () => controller.abort();
  }, [albumId, navigate]);
  const sequence = album?.order_state === "ready" ? orderedAlbum(album.tracks) : null;
  const tracks = sequence ?? album?.tracks ?? [];
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setSaveError(null);
    let saved = false;
    try {
      const response = await fetch(
        `/api/vault-master/music/albums/groups/${encodeURIComponent(albumId)}`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ album_title: title.trim(), artist_name: artist.trim() }),
        },
      );
      if (!response.ok) throw new Error("Correction failed");
      saved = true;
      const refreshed = await fetch(`/api/music/albums/${encodeURIComponent(albumId)}`, {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!refreshed.ok) throw new Error("Album refresh failed");
      setAlbum((await refreshed.json()) as Album);
      setEditing(false);
    } catch {
      setSaveError(
        saved
          ? "Your correction was saved, but the album could not be refreshed. Close and reopen the album to see it."
          : "Album information could not be saved. Your changes are still here.",
      );
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="mx-auto min-w-0 max-w-5xl space-y-6 pb-8">
      <Link
        to="/app/music/albums"
        hash="music-albums"
        className="pv-btn-ghost inline-flex min-h-11 items-center"
      >
        Back to Albums
      </Link>
      {!album ? (
        <p role="status">{status}</p>
      ) : (
        <>
          <header className="pv-panel flex min-w-0 flex-col gap-6 p-5 sm:flex-row sm:p-8">
            <div className="mx-auto flex aspect-square w-48 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-white/[0.04] sm:mx-0 sm:w-56">
              {album.artwork_url && !artFailed ? (
                <img
                  src={album.artwork_url}
                  alt={`${album.title} artwork`}
                  className="h-full w-full object-cover"
                  onError={() => setArtFailed(true)}
                />
              ) : (
                <Disc3 size={72} aria-label="No album artwork" className="text-[var(--pv-gold)]" />
              )}
            </div>
            <div className="min-w-0 flex-1 space-y-4 self-center">
              <h2 className="pv-content-title break-words text-3xl">
                {album.title || "Untitled album"}
              </h2>
              {album.artist && (
                <p className="break-words text-lg text-[var(--pv-text-dim)]">{album.artist}</p>
              )}
              {album.release_year && <p className="text-sm">{album.release_year}</p>}
              <p className="text-sm text-[var(--pv-text-dim)]">
                {tracks.length} {tracks.length === 1 ? "track" : "tracks"}
              </p>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className="pv-btn-primary inline-flex min-h-11 items-center gap-2"
                  disabled={!sequence}
                  aria-describedby={!sequence ? "album-order" : undefined}
                  onClick={() => sequence && player.startAlbum(sequence)}
                >
                  <Play size={18} />
                  Play album
                </button>
                {album.can_edit && (
                  <Dialog
                    open={editing}
                    onOpenChange={(open) => {
                      setEditing(open);
                      if (open) {
                        setTitle(album.title);
                        setArtist(album.artist);
                        setSaveError(null);
                      }
                    }}
                  >
                    <DialogTrigger asChild>
                      <button type="button" className="pv-btn-ghost min-h-11">
                        Edit metadata
                      </button>
                    </DialogTrigger>
                    <DialogContent className="pv-dialog max-h-[90dvh] overflow-y-auto">
                      <DialogHeader>
                        <DialogTitle>Edit album metadata</DialogTitle>
                        <DialogDescription>
                          Correct the album title and artist. Tracks, files and playback order stay
                          the same.
                        </DialogDescription>
                      </DialogHeader>
                      <form onSubmit={(event) => void save(event)} className="space-y-4">
                        <label className="block">
                          Album title
                          <input
                            className="pv-input mt-2 w-full"
                            required
                            maxLength={300}
                            value={title}
                            onChange={(e) => setTitle(e.target.value)}
                          />
                        </label>
                        <label className="block">
                          Artist
                          <input
                            className="pv-input mt-2 w-full"
                            required
                            maxLength={300}
                            value={artist}
                            onChange={(e) => setArtist(e.target.value)}
                          />
                        </label>
                        {saveError && <p role="alert">{saveError}</p>}
                        <div className="flex flex-wrap gap-2">
                          <button
                            type="submit"
                            className="pv-btn-primary min-h-11"
                            disabled={saving || !title.trim() || !artist.trim()}
                          >
                            {saving ? "Saving�" : "Save correction"}
                          </button>
                          <button
                            type="button"
                            className="pv-btn-ghost min-h-11"
                            onClick={() => setEditing(false)}
                          >
                            Cancel
                          </button>
                        </div>
                      </form>
                    </DialogContent>
                  </Dialog>
                )}
              </div>
              {!sequence && (
                <p id="album-order" role="status" className="text-sm text-[var(--pv-text-dim)]">
                  {tracks.length
                    ? "Album order has not yet been resolved. You can play individual tracks."
                    : "This album has no available tracks yet."}
                </p>
              )}
            </div>
          </header>
          <MusicAlbumPlayer
            player={player}
            sequence={sequence}
            tracks={tracks}
            title={album.title}
            artist={album.artist}
          />
        </>
      )}
    </div>
  );
}
