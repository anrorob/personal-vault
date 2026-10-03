import { createFileRoute, Link } from "@tanstack/react-router";
export const Route = createFileRoute("/app/music_/playlists")({
  component: () => (
    <div className="mx-auto max-w-5xl min-w-0 space-y-5">
      <nav aria-label="Music navigation" className="flex min-h-11 items-center">
        <Link to="/app/music" className="pv-btn-ghost">
          Back to Music
        </Link>
      </nav>
      <h1 className="pv-content-title text-2xl">Playlists</h1>
      <p className="text-[var(--pv-text-dim)]">Playlists are coming later.</p>
    </div>
  ),
});
