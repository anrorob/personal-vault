import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import {
  Film,
  Clapperboard,
  Images,
  Inbox,
  Upload,
  ArrowRight,
  Music2,
  BookOpenText,
  Share2,
} from "lucide-react";
import { useEffect, useState } from "react";
import type { GalleryImage } from "@/lib/gallery";
import type { ArrivalHallListing } from "@/lib/incoming";
import type { VaultLibraryFile } from "@/lib/vault-libraries";

type Movie = {
  id: string;
  poster_url: string | null;
};

type MusicTrack = {
  id: string;
  artist: string;
  album: string;
  album_artist: string | null;
  album_folder: string;
  artwork_url: string | null;
  enrichment_status: "identified" | "needs_review";
};

type ReadingRoomPublication = {
  id: string;
  cover_url: string | null;
};

type CommonsAsset = {
  asset_id: string;
  preview_url: string | null;
};

export const Route = createFileRoute("/app/")({
  component: HomePage,
});

type CardProps = {
  to: string;
  title: string;
  count: string;
  icon: React.ComponentType<{ size?: number; className?: string }>;
  previews?: CardPreview[];
};

type CardPreview = {
  id: string;
  kind: "image" | "video";
  url: string;
};

function BigCard({ to, title, count, icon: Icon, previews = [] }: CardProps) {
  return (
    <Link
      to={to}
      className="pv-panel pv-panel-hover p-6 flex flex-col justify-between min-h-[180px] group relative overflow-hidden"
    >
      {previews.length > 0 && <CardPreviews previews={previews} title={title} />}
      <div className="flex items-start justify-between">
        <Icon size={26} className="pv-card-icon relative z-10" />
      </div>
      <div className="relative z-10">
        <h3 className="text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
          {title}
        </h3>
        <p className="mt-1 text-sm" style={{ color: "var(--pv-text-dim)" }}>
          {count}
        </p>
      </div>
    </Link>
  );
}

function CardPreviews({ previews, title }: { previews: CardPreview[]; title: string }) {
  return (
    <div
      aria-hidden="true"
      className="absolute right-4 bottom-4 h-16 w-[70%] opacity-70 group-hover:opacity-90 transition-opacity"
      style={{
        maskImage: "linear-gradient(90deg, transparent, black 18%)",
        WebkitMaskImage: "linear-gradient(90deg, transparent, black 18%)",
      }}
    >
      {previews.map((preview, index) => (
        <span
          key={preview.id}
          className="absolute overflow-hidden rounded-lg border"
          style={{
            right: index === 0 ? "4%" : index === 1 ? "35%" : "67%",
            bottom: index === 0 ? "2%" : index === 1 ? "22%" : "0",
            width: index === 0 ? "48%" : "36%",
            height: index === 0 ? "94%" : "78%",
            zIndex: previews.length - index,
            borderColor: "var(--pv-border)",
            background: "#111217",
            transform: `rotate(${index === 1 ? "3deg" : index === 2 ? "-2deg" : "0deg"})`,
          }}
        >
          {preview.kind === "image" ? (
            <img src={preview.url} alt="" className="h-full w-full object-cover" loading="lazy" />
          ) : (
            <video
              src={preview.url}
              className="h-full w-full object-cover"
              muted
              playsInline
              preload="metadata"
              aria-label={`${title} preview`}
              onLoadedMetadata={(event) => {
                event.currentTarget.currentTime = Math.min(0.5, event.currentTarget.duration / 10);
              }}
            />
          )}
        </span>
      ))}
    </div>
  );
}

function HomePage() {
  const navigate = useNavigate();
  const [counts, setCounts] = useState<{
    movies: number | null;
    musicAlbums: number | null;
    musicTracks: number | null;
    readingRoom: number | null;
    personalVideos: number | null;
    gallery: number | null;
    incoming: number | null;
  }>({
    movies: null,
    musicAlbums: null,
    musicTracks: null,
    readingRoom: null,
    personalVideos: null,
    gallery: null,
    incoming: null,
  });
  const [previews, setPreviews] = useState<Record<string, CardPreview[]>>({});

  useEffect(() => {
    const controller = new AbortController();

    const loadCounts = async () => {
      const responses = await Promise.all([
        fetch("/api/movies", {
          credentials: "include",
          signal: controller.signal,
        }),
        fetch("/api/music", {
          credentials: "include",
          signal: controller.signal,
        }),
        fetch("/api/reading-room/publications", {
          credentials: "include",
          signal: controller.signal,
        }),
        fetch("/api/gallery", {
          credentials: "include",
          signal: controller.signal,
        }),
        fetch("/api/personal-videos", {
          credentials: "include",
          signal: controller.signal,
        }),
        fetch("/api/vault-master/commons/shared-with-me?category=gallery", {
          credentials: "include",
          signal: controller.signal,
        }),
        fetch("/api/arrival-hall", {
          credentials: "include",
          signal: controller.signal,
        }),
      ]);

      if (responses.some((response) => response.status === 401)) {
        await navigate({ to: "/login" });
        return;
      }

      const [
        moviesResponse,
        musicResponse,
        readingRoomResponse,
        galleryResponse,
        personalVideosResponse,
        commonsResponse,
        incomingResponse,
      ] = responses;
      const movies = moviesResponse.ok ? ((await moviesResponse.json()) as unknown[]) : null;
      const music = musicResponse.ok ? ((await musicResponse.json()) as MusicTrack[]) : null;
      const readingRoom = readingRoomResponse.ok
        ? ((await readingRoomResponse.json()) as ReadingRoomPublication[])
        : null;
      const gallery = galleryResponse.ok
        ? ((await galleryResponse.json()) as GalleryImage[])
        : null;
      const personalVideos = personalVideosResponse.ok
        ? ((await personalVideosResponse.json()) as unknown[])
        : null;
      const commons = commonsResponse.ok
        ? ((await commonsResponse.json()) as { assets: CommonsAsset[] }).assets
        : null;
      const incoming = incomingResponse.ok
        ? ((await incomingResponse.json()) as ArrivalHallListing)
        : null;

      setCounts({
        movies: movies?.length ?? null,
        musicAlbums: music ? musicAlbumKeys(music).size : null,
        musicTracks: music?.length ?? null,
        readingRoom: readingRoom?.length ?? null,
        personalVideos: personalVideos?.length ?? null,
        gallery: gallery?.length ?? null,
        incoming: incoming?.files.length ?? null,
      });

      setPreviews({
        movies: randomPreviews(
          (movies ?? [])
            .filter((movie): movie is Movie => isMovie(movie) && Boolean(movie.poster_url))
            .map((movie) => ({ id: movie.id, kind: "image" as const, url: movie.poster_url! })),
        ),
        music: randomPreviews(musicAlbumPreviews(music ?? [])),
        readingRoom: randomPreviews(
          (readingRoom ?? [])
            .filter((publication) => Boolean(publication.cover_url))
            .map((publication) => ({
              id: publication.id,
              kind: "image" as const,
              url: publication.cover_url!,
            })),
        ),
        gallery: randomPreviews(
          (gallery ?? []).map((image) => ({
            id: image.id,
            kind: "image" as const,
            url: image.thumbnail_url,
          })),
        ),
        personalVideos: randomPreviews(
          (personalVideos ?? [])
            .filter((file): file is VaultLibraryFile => isVaultFile(file) && file.kind === "video")
            .map((file) => ({ id: file.id, kind: "video" as const, url: file.open_url })),
        ),
        commons: randomPreviews(
          (commons ?? [])
            .filter((asset) => Boolean(asset.preview_url))
            .map((asset) => ({
              id: asset.asset_id,
              kind: "image" as const,
              url: asset.preview_url!,
            })),
        ),
      });
    };

    void loadCounts().catch((error: unknown) => {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        console.error("Unable to load Vault counts", error);
      }
    });

    return () => controller.abort();
  }, [navigate]);

  return (
    <div className="max-w-6xl mx-auto space-y-10">
      <section>
        <h2 className="pv-display-title text-2xl md:text-3xl tracking-tight">
          Greetings, Vault Hunter.
        </h2>

        <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
          Your Personal Vault is ready.
        </p>
      </section>

      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        <BigCard
          to="/app/gallery"
          title="Gallery"
          count={formatCount(counts.gallery, "photo")}
          icon={Images}
          previews={previews.gallery}
        />
        <BigCard
          to="/app/music"
          title="Music"
          count={formatMusicCount(counts.musicAlbums, counts.musicTracks)}
          icon={Music2}
          previews={previews.music}
        />
        <BigCard
          to="/app/movies"
          title="Theatre"
          count={formatCount(counts.movies, "movie")}
          icon={Film}
          previews={previews.movies}
        />
        <BigCard
          to="/app/personal-videos"
          title="Home Videos"
          count={formatCount(counts.personalVideos, "video")}
          icon={Clapperboard}
          previews={previews.personalVideos}
        />
        <BigCard
          to="/app/reading-room"
          title="Reading Room"
          count={formatCount(counts.readingRoom, "publication")}
          icon={BookOpenText}
          previews={previews.readingRoom}
        />
        <BigCard
          to="/app/commons"
          title="Vault Commons"
          count="Shared content"
          icon={Share2}
          previews={previews.commons}
        />
      </section>

      <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Link
          to="/app/arrival-hall"
          className="pv-panel pv-panel-dark pv-panel-hover p-5 rounded-xl flex items-center gap-4"
          style={{
            border: "1px solid var(--pv-gold-dim)",
            background:
              "linear-gradient(145deg, rgba(215,185,104,0.08), transparent 46%), linear-gradient(180deg, #121214, #0e0e10)",
          }}
        >
          <div
            className="h-11 w-11 rounded-lg flex items-center justify-center"
            style={{
              background: "linear-gradient(180deg, #d4b56b, #a5893f)",
              color: "#1a1608",
            }}
          >
            <Inbox size={20} />
          </div>
          <div className="flex-1">
            <div className="text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
              Arrival Hall
            </div>
            <div className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              {formatCount(counts.incoming, "file")}
            </div>
          </div>
          <ArrowRight size={16} style={{ color: "var(--pv-gold)" }} />
        </Link>

        <Link
          to="/app/add"
          className="pv-panel pv-panel-dark pv-panel-hover p-5 rounded-xl flex items-center gap-4"
          style={{
            border: "1px solid var(--pv-gold-dim)",
            background:
              "linear-gradient(145deg, rgba(215,185,104,0.08), transparent 46%), linear-gradient(180deg, #121214, #0e0e10)",
          }}
        >
          <div
            className="h-11 w-11 rounded-lg flex items-center justify-center"
            style={{
              background: "linear-gradient(180deg, #d4b56b, #a5893f)",
              color: "#1a1608",
            }}
          >
            <Upload size={20} />
          </div>
          <div className="flex-1">
            <div className="text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
              Add to Vault
            </div>
            <div className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              Upload new files to the Arrival Hall
            </div>
          </div>
          <ArrowRight size={16} style={{ color: "var(--pv-gold)" }} />
        </Link>
      </section>
    </div>
  );
}

function isMovie(value: unknown): value is Movie {
  return typeof value === "object" && value !== null && "id" in value && "poster_url" in value;
}

function isVaultFile(value: unknown): value is VaultLibraryFile {
  return (
    typeof value === "object" &&
    value !== null &&
    "id" in value &&
    "open_url" in value &&
    "kind" in value
  );
}

function musicAlbumKey(track: MusicTrack): string {
  return track.enrichment_status === "identified"
    ? `${track.album_artist ?? track.artist}\u0000${track.album}`
    : `folder\u0000${track.album_folder}`;
}

function musicAlbumKeys(tracks: MusicTrack[]): Set<string> {
  return new Set(tracks.map(musicAlbumKey));
}

function musicAlbumPreviews(tracks: MusicTrack[]): CardPreview[] {
  const albums = new Map<string, CardPreview>();
  for (const track of tracks) {
    const key = musicAlbumKey(track);
    if (track.artwork_url && !albums.has(key)) {
      albums.set(key, { id: key, kind: "image", url: track.artwork_url });
    }
  }
  return [...albums.values()];
}

function randomPreviews<T>(values: T[], count = 3): T[] {
  return [...values].sort(() => Math.random() - 0.5).slice(0, count);
}

function formatCount(count: number | null, singular: string): string {
  if (count === null) {
    return "Loading...";
  }

  return `${count} ${count === 1 ? singular : `${singular}s`}`;
}

function formatMusicCount(albums: number | null, tracks: number | null): string {
  if (albums === null || tracks === null) {
    return "Loading...";
  }
  return `${albums} ${albums === 1 ? "album" : "albums"} · ${tracks} ${tracks === 1 ? "track" : "tracks"}`;
}
