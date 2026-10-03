import { useEffect, useRef, useState } from "react";
import { MoreHorizontal, Video } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "./ui/dialog";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "./ui/dropdown-menu";
import { MoviePlayer } from "./pv/MoviePlayer";

type MusicVideo = {
  asset_id: string;
  artist: string | null;
  title: string | null;
  can_edit: boolean;
  favorite?: boolean;
  playback_url: string;
  thumbnail_url?: string;
};

function VideoPlayback({ endpoint }: { endpoint: string }) {
  const [source, setSource] = useState<string | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const check = async () => {
      try {
        const response = await fetch(endpoint, {
          credentials: "include",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error();
        const state = await response.json();
        if (controller.signal.aborted) return;
        if (state.status === "preparing") timer = setTimeout(() => void check(), 2000);
        else if (state.playback_url) setSource(state.playback_url);
        else throw new Error();
      } catch {
        if (!controller.signal.aborted) setError(true);
      }
    };
    void check();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [endpoint]);
  if (error) return <p role="alert">This video could not be played.</p>;
  if (!source) return <p role="status">Preparing video...</p>;
  return (
    <div className="aspect-video w-full">
      <MoviePlayer sourceType="file" source={source} onPlaybackError={() => setError(true)} />
    </div>
  );
}

function VideoCard({
  video,
  onSaved,
  beforePlay,
  onFavorite,
}: {
  video: MusicVideo;
  onSaved: () => Promise<void>;
  onFavorite: (value: boolean) => void;
  beforePlay?: () => void;
}) {
  const menuTrigger = useRef<HTMLButtonElement>(null);
  const [favoriteBusy, setFavoriteBusy] = useState(false);
  const [favoriteError, setFavoriteError] = useState(false);
  const toggleFavorite = async () => {
    setFavoriteBusy(true);
    setFavoriteError(false);
    try {
      const response = await fetch(`/api/music-videos/${video.asset_id}/favorite`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ favorite: !video.favorite }),
      });
      if (!response.ok) throw new Error();
      const result = await response.json();
      onFavorite(result.favorite);
    } catch {
      setFavoriteError(true);
    } finally {
      setFavoriteBusy(false);
    }
  };
  const [editing, setEditing] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [artist, setArtist] = useState("");
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [imageFailed, setImageFailed] = useState(false);
  const label = video.title || "Untitled music video";
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/music-videos/${video.asset_id}/metadata`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ artist: artist.trim() || null, title: title.trim() || null }),
      });
      if (!response.ok) throw new Error();
      await onSaved();
      setEditing(false);
    } catch {
      setError("The metadata could not be saved or refreshed.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <article className="relative min-w-0 rounded-xl border border-[var(--pv-border)] bg-white/[0.02]">
      <Dialog
        open={playing}
        onOpenChange={(open) => {
          if (open) beforePlay?.();
          setPlaying(open);
        }}
      >
        <DialogTrigger asChild>
          <button
            type="button"
            aria-label={`Play ${label}`}
            className="w-full text-left focus-visible:ring-2 focus-visible:ring-[var(--pv-gold)]"
          >
            <div className="aspect-video flex items-center justify-center overflow-hidden rounded-t-xl bg-black/30">
              {video.thumbnail_url && !imageFailed ? (
                <img
                  src={video.thumbnail_url}
                  alt=""
                  loading="lazy"
                  onError={() => setImageFailed(true)}
                  className="h-full w-full object-cover"
                />
              ) : (
                <Video size={48} aria-hidden="true" />
              )}
            </div>
            <div className="space-y-1 p-3 pr-14">
              <p className="break-words font-semibold">{label}</p>
              <p className="break-words text-sm text-[var(--pv-text-dim)]">
                {video.artist || "Artist / Band not set"}
              </p>
            </div>
          </button>
        </DialogTrigger>
        <DialogContent className="pv-dialog max-w-5xl max-h-[92dvh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{label}</DialogTitle>
            <DialogDescription>{video.artist || "Music Video"}</DialogDescription>
          </DialogHeader>
          {playing && <VideoPlayback endpoint={video.playback_url} />}
        </DialogContent>
      </Dialog>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            ref={menuTrigger}
            type="button"
            aria-label={`Options for ${label}`}
            onClick={(event) => event.stopPropagation()}
            className="absolute bottom-1 right-1 flex h-11 w-11 items-center justify-center rounded-md text-[var(--pv-text-dim)] hover:text-[var(--pv-silver)] focus-visible:ring-2"
          >
            <MoreHorizontal size={20} aria-hidden="true" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          onClick={(event) => event.stopPropagation()}
          onCloseAutoFocus={(event) => {
            if (editing) event.preventDefault();
          }}
        >
          <DropdownMenuItem disabled={favoriteBusy} onSelect={() => void toggleFavorite()}>
            {video.favorite ? "Remove from Favorites" : "Add to Favorites"}
          </DropdownMenuItem>
          <DropdownMenuItem
            disabled={!video.can_edit}
            onSelect={() => {
              setArtist(video.artist || "");
              setTitle(video.title || "");
              setError(null);
              setEditing(true);
            }}
          >
            Edit metadata
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {favoriteError && (
        <p role="alert" className="px-3 pb-12 text-sm">
          Favorites could not be updated. Please try again.
        </p>
      )}
      {video.can_edit && (
        <Dialog
          open={editing}
          onOpenChange={(open) => {
            setEditing(open);
            if (open) {
              setArtist(video.artist || "");
              setTitle(video.title || "");
              setError(null);
            }
          }}
        >
          <DialogContent
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              menuTrigger.current?.focus();
            }}
            className="pv-dialog max-h-[90dvh] overflow-y-auto"
          >
            <DialogHeader>
              <DialogTitle>Edit Music Video metadata</DialogTitle>
              <DialogDescription>
                Artist / Band and Title are optional. Your corrections take precedence.
              </DialogDescription>
            </DialogHeader>
            <form onSubmit={(event) => void save(event)} className="space-y-4">
              <label className="block">
                Artist / Band
                <input
                  value={artist}
                  onChange={(event) => setArtist(event.target.value)}
                  maxLength={240}
                  className="pv-input w-full"
                />
              </label>
              <label className="block">
                Title
                <input
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  maxLength={240}
                  className="pv-input w-full"
                />
              </label>
              {error && <p role="alert">{error}</p>}
              <div className="flex flex-wrap gap-2">
                <button type="submit" disabled={busy} className="pv-btn-primary min-h-11">
                  {busy ? "Saving..." : "Save"}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setEditing(false)}
                  className="pv-btn-ghost min-h-11"
                >
                  Cancel
                </button>
              </div>
            </form>
          </DialogContent>
        </Dialog>
      )}
    </article>
  );
}

export function MusicVideos({ beforePlay }: { beforePlay?: () => void }) {
  const [items, setItems] = useState<MusicVideo[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(false);
  const [artist, setArtist] = useState("all");
  const reload = async (signal?: AbortSignal) => {
    const response = await fetch("/api/music-videos", { credentials: "include", signal });
    if (!response.ok) throw new Error();
    const values = await response.json();
    if (!Array.isArray(values)) throw new Error("Invalid Music Video response");
    if (!signal?.aborted) {
      setItems(values);
      setLoaded(true);
      setError(false);
    }
  };
  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal).catch(() => {
      if (!controller.signal.aborted) setError(true);
    });
    return () => controller.abort();
  }, []);
  const artists = [
    ...new Set(items.map((item) => item.artist).filter((value): value is string => !!value)),
  ].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  const visible = items.filter(
    (item) =>
      artist === "all" ||
      (artist === "favorites"
        ? item.favorite
        : artist === "missing"
          ? !item.artist
          : item.artist === artist.slice(7)),
  );
  return (
    <section
      id="music-music-videos"
      aria-labelledby="music-videos-heading"
      className="scroll-mt-24 space-y-4"
    >
      <h2 id="music-videos-heading" className="pv-content-title text-xl">
        Music Videos
      </h2>
      <label className="flex flex-wrap items-center gap-2">
        Artist / Band
        <select
          value={artist}
          onChange={(event) => setArtist(event.target.value)}
          className="pv-input min-h-11 max-w-full"
        >
          <option value="favorites">Favorites</option>
          <option value="all">All</option>
          <option value="missing">Not set</option>
          {artists.map((value) => (
            <option key={value} value={`artist:${value}`}>
              {value}
            </option>
          ))}
        </select>
      </label>
      {error && <p role="alert">Music Videos is currently unavailable.</p>}
      {!loaded && !error && <p role="status">Loading Music Videos...</p>}
      {loaded && !visible.length && (
        <p className="pv-panel p-6 text-sm">
          {artist === "favorites"
            ? "No favorite music videos yet."
            : items.length
              ? "No matching music videos."
              : "No music videos yet."}
        </p>
      )}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {visible.map((video) => (
          <VideoCard
            key={video.asset_id}
            video={video}
            beforePlay={beforePlay}
            onSaved={() => reload()}
            onFavorite={(favorite) =>
              setItems((current) =>
                current.map((item) =>
                  item.asset_id === video.asset_id ? { ...item, favorite } : item,
                ),
              )
            }
          />
        ))}
      </div>
    </section>
  );
}
