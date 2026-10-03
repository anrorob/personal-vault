export type LibraryTrack = {
  id: string;
  asset_id: string;
  can_edit?: boolean;
  title: string;
  artist: string;
  album: string;
  album_artist: string | null;
  album_group_id?: string | null;
  album_position?: number | null;
  album_order_state?: string | null;
  album_member_count?: number | null;
  release_year: number | null;
  artwork_url: string | null;
  duration_seconds: number | null;
  enrichment_status: string;
};

export function orderedAlbum<T extends LibraryTrack>(tracks: T[]): T[] | null {
  if (!tracks.length) return null;
  const group = tracks[0].album_group_id;
  if (
    !group ||
    tracks.some(
      (track) =>
        track.album_group_id !== group ||
        track.album_order_state !== "ready" ||
        track.album_member_count !== tracks.length,
    )
  )
    return null;
  const ordered = [...tracks].sort((a, b) => (a.album_position ?? 0) - (b.album_position ?? 0));
  if (
    new Set(ordered.map((track) => track.asset_id)).size !== ordered.length ||
    ordered.some((track, index) => track.album_position !== index + 1)
  )
    return null;
  return ordered;
}
