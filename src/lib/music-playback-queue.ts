export const MUSIC_INTER_TRACK_DELAY_MS = 2_000;

type QueueTrack = { id: string };

export function nextAlbumTrack<T extends QueueTrack>(
  albumTracks: readonly T[],
  currentTrackId: string,
): T | null {
  const currentIndex = albumTracks.findIndex((track) => track.id === currentTrackId);
  return currentIndex >= 0 ? (albumTracks[currentIndex + 1] ?? null) : null;
}
