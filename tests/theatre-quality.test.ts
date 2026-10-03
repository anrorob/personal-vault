import { expect, test } from "bun:test";
import {
  qualityDimensions,
  qualityModes,
  h264TargetCodec,
  originalResolutionViolation,
} from "../src/lib/theatre-quality";

test("exact selector modes and native 4K candidates without upscaling", () => {
  expect([...qualityModes]).toEqual(["Original", "Auto", "FHD", "720p"]);
  expect(
    qualityDimensions({ container: "mkv", video: { Width: 3840, Height: 2160 }, audio_tracks: [] }),
  ).toContainEqual([3840, 2160]);
  expect(
    qualityDimensions({
      container: "mkv",
      video: { Width: 854, Height: 480 },
      audio_tracks: [],
    })[0],
  ).toEqual([854, 480]);
  expect(
    qualityDimensions({ container: "mkv", video: { Width: 3840, Height: 1600 }, audio_tracks: [] }),
  ).toContainEqual([1920, 800]);
});
test("4K decode probe uses an appropriate H264 level", () => {
  expect(h264TargetCodec(3840, 2160, 30)).toBe("avc1.640033");
  expect(h264TargetCodec(3840, 2160, 60)).toBe("avc1.640034");
});
test("Original refuses an observed downscale while Auto may adapt", () => {
  const target = { width: 1920, height: 1080 };
  expect(originalResolutionViolation("Original", target, 416, 234)).toContain("Playback stopped");
  expect(originalResolutionViolation("Original", target, 1920, 1080)).toBeNull();
  expect(originalResolutionViolation("Auto", target, 1280, 720)).toBeNull();
});

test("Auto delivery evidence includes slow segment production without inventing samples", async () => {
  const { measuredDeliveryThroughput } = await import("../src/lib/theatre-quality");
  expect(measuredDeliveryThroughput(null, 1_000_000, 1000, 5000)).toBe(2_000_000);
  expect(measuredDeliveryThroughput(2_000_000, 1_000_000, 0, 1000)).toBe(3_500_000);
  expect(measuredDeliveryThroughput(null, 0, 0, 1000)).toBeNull();
  expect(measuredDeliveryThroughput(null, 100, 1000, 1000)).toBeNull();
});
