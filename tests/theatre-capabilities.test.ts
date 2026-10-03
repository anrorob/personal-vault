import { afterEach, expect, test } from "bun:test";
import { theatreCapabilities, videoCodecString } from "../src/lib/theatre-capabilities";

const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
const originalMediaSource = Object.getOwnPropertyDescriptor(globalThis, "MediaSource");
afterEach(() => {
  for (const [name, descriptor] of [
    ["navigator", originalNavigator],
    ["MediaSource", originalMediaSource],
  ] as const) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else Reflect.deleteProperty(globalThis, name);
  }
});

test("probe reflects exact H264 profile and level rather than baseline support", () => {
  expect(videoCodecString({ Codec: "h264", Profile: "High", Level: 41 })).toBe("avc1.640029");
  expect(videoCodecString({ Codec: "h264" })).toBeNull();
  expect(videoCodecString({ Codec: "vc1", Profile: "Advanced" })).toBe("wvc1");
});

test("VC1 cannot be copied when browser supports H264 but rejects VC1", async () => {
  const canPlayType = (mime: string) =>
    mime.includes("avc1") || mime.includes("mp4a") ? "probably" : "";
  Object.defineProperty(globalThis, "MediaSource", {
    configurable: true,
    value: { isTypeSupported: (mime: string) => Boolean(canPlayType(mime)) },
  });
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { mediaCapabilities: { decodingInfo: async () => ({ supported: true }) } },
  });
  const result = await theatreCapabilities(
    {
      container: "mkv",
      video: { Codec: "vc1", Width: 1920, Height: 1080, BitRate: 20000000, AverageFrameRate: 24 },
      audio_tracks: [{ Codec: "ac3" }],
    },
    { canPlayType } as HTMLVideoElement,
  );
  expect(result).toEqual({
    direct: false,
    video_copy: false,
    audio_copy: false,
    h264: true,
    aac: true,
  });
});

test("compatible source can direct play while MKV can preserve its H264 video", async () => {
  const canPlayType = (mime: string) =>
    mime.startsWith("video/mp4") || mime.startsWith("audio/mp4") ? "probably" : "";
  Object.defineProperty(globalThis, "MediaSource", {
    configurable: true,
    value: { isTypeSupported: () => true },
  });
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { mediaCapabilities: { decodingInfo: async () => ({ supported: true }) } },
  });
  const source = {
    container: "mp4",
    video: {
      Codec: "h264",
      Profile: "High",
      Level: 41,
      Width: 1920,
      Height: 1080,
      BitRate: 20000000,
      AverageFrameRate: 24,
    },
    audio_tracks: [{ Codec: "aac" }],
  };
  expect((await theatreCapabilities(source, { canPlayType } as HTMLVideoElement)).direct).toBe(
    true,
  );
  const remux = await theatreCapabilities({ ...source, container: "mkv" }, {
    canPlayType,
  } as HTMLVideoElement);
  expect(remux.direct).toBe(false);
  expect(remux.video_copy).toBe(true);
  expect(remux.audio_copy).toBe(true);
});
