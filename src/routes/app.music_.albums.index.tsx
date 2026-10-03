import { createFileRoute } from "@tanstack/react-router";
import { MusicAudioPage } from "../components/MusicAudioPage";
export const Route = createFileRoute("/app/music_/albums/")({
  component: () => <MusicAudioPage section="albums" />,
});
