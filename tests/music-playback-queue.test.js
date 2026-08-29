import { expect, test } from "bun:test";

import { MUSIC_INTER_TRACK_DELAY_MS, nextAlbumTrack } from "../src/lib/music-playback-queue";

const albumTracks = [{ id: "disc-1-track-1" }, { id: "disc-1-track-2" }, { id: "disc-2-track-1" }];

test("advances an album in its supplied canonical order", () => {
  expect(nextAlbumTrack(albumTracks, "disc-1-track-1")?.id).toBe("disc-1-track-2");
  expect(nextAlbumTrack(albumTracks, "disc-1-track-2")?.id).toBe("disc-2-track-1");
});

test("starts continuation from a manually selected later album track", () => {
  expect(nextAlbumTrack(albumTracks, "disc-1-track-2")?.id).toBe("disc-2-track-1");
});

test("stops after the final album track", () => {
  expect(nextAlbumTrack(albumTracks, "disc-2-track-1")).toBeNull();
});

test("retains the established two-second inter-track delay", () => {
  expect(MUSIC_INTER_TRACK_DELAY_MS).toBe(2_000);
});
