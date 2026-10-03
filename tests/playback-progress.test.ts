import { expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
import { attachPlaybackProgress } from "../src/lib/playback-progress";

test("progress follows backward/near-end seeks, deduplicates, and survives media teardown", () => {
  const video = document.createElement("video");
  Object.defineProperty(video, "duration", { value: 600, configurable: true });
  const writes: [number, number, boolean][] = [];
  let now = 0;
  const dispose = attachPlaybackProgress(
    video,
    (...value) => writes.push(value),
    () => now,
  );
  video.currentTime = 120;
  video.dispatchEvent(new Event("timeupdate"));
  video.dispatchEvent(new Event("pause"));
  expect(writes).toEqual([[120, 600, false]]);
  now = 10000;
  video.currentTime = 125;
  video.dispatchEvent(new Event("timeupdate"));
  video.currentTime = 30;
  video.dispatchEvent(new Event("seeked"));
  now = 20000;
  video.currentTime = 40;
  video.dispatchEvent(new Event("timeupdate"));
  expect(writes.at(-1)).toEqual([40, 600, false]);
  video.currentTime = 575;
  video.dispatchEvent(new Event("seeked"));
  expect(writes.at(-1)).toEqual([575, 600, false]);
  video.currentTime = 576;
  video.dispatchEvent(new Event("timeupdate"));
  Object.defineProperty(video, "duration", { value: NaN });
  video.currentTime = 0;
  dispose();
  expect(writes.at(-1)).toEqual([576, 600, false]);
  const count = writes.length;
  video.dispatchEvent(new Event("pause"));
  expect(writes.length).toBe(count);
});
test("ended and page close flush current viewer progress", () => {
  const video = document.createElement("video");
  Object.defineProperty(video, "duration", { value: 600 });
  const writes: [number, number, boolean][] = [];
  const dispose = attachPlaybackProgress(video, (...value) => writes.push(value));
  video.currentTime = 600;
  video.dispatchEvent(new Event("ended"));
  expect(writes.at(-1)).toEqual([600, 600, true]);
  dispose();
});
