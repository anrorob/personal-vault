import { createFileRoute, Link } from "@tanstack/react-router";
import { MusicVideos } from "../components/MusicVideos";
export const Route = createFileRoute("/app/music_/videos")({
  component: () => (
    <div className="mx-auto max-w-7xl min-w-0 space-y-5 pb-8">
      <nav aria-label="Music navigation" className="flex min-h-11 items-center">
        <Link to="/app/music" className="pv-btn-ghost">
          Back to Music
        </Link>
      </nav>
      <MusicVideos />
    </div>
  ),
});
