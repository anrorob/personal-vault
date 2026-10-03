import { videoCodecString, type TheatreSource } from "./theatre-capabilities";

export const qualityModes = ["Original", "Auto", "FHD", "720p"] as const;
export type QualityMode = (typeof qualityModes)[number];
export type QualityInfo = {
  selected_mode: QualityMode;
  source_resolution?: { width: number; height: number };
  target_resolution?: { width: number; height: number };
  fallback_reason?: string | null;
};

/** Include segment production/TTFB; hls.js's network EWMA subtracts it. */
export function measuredDeliveryThroughput(
  previous: number | null,
  bytes: number,
  started: number,
  completed: number,
) {
  const elapsed = completed - started;
  if (!Number.isFinite(bytes) || !Number.isFinite(elapsed) || bytes <= 0 || elapsed <= 0)
    return previous;
  const sample = (bytes * 8000) / elapsed;
  return previous === null ? sample : previous * 0.75 + sample * 0.25;
}

export function qualityDimensions(source: TheatreSource): [number, number][] {
  const width = source.video.Width || 0,
    height = source.video.Height || 0;
  if (!width || !height) return [];
  const targets = [
    [width, height],
    [1920, 1080],
    [1280, 720],
    [854, 480],
    [640, 360],
  ].map(([w, h]) => {
    const scale = Math.min(1, w / width, h / height);
    return scale === 1
      ? [width, height]
      : [
          Math.max(2, Math.floor((width * scale) / 2) * 2),
          Math.max(2, Math.floor((height * scale) / 2) * 2),
        ];
  });
  return targets.filter(([w, h], i) => targets.findIndex(([x, y]) => w === x && h === y) === i) as [
    number,
    number,
  ][];
}

export function h264TargetCodec(width: number, height: number, fps: number) {
  const blocks = Math.ceil(width / 16) * Math.ceil(height / 16);
  const level = [
    [31, 3600, 108000],
    [40, 8192, 245760],
    [42, 8704, 522240],
    [50, 22080, 589824],
    [51, 36864, 983040],
    [52, 36864, 2073600],
    [60, 139264, 4177920],
  ].find(([, maxBlocks, maxRate]) => blocks <= maxBlocks && blocks * fps <= maxRate)?.[0];
  return level ? `avc1.6400${level.toString(16).padStart(2, "0")}` : null;
}

export async function probeQualityTargets(
  source: TheatreSource,
  video: HTMLVideoElement,
  preserveVideo: boolean,
) {
  const nativeHls = Boolean(video.canPlayType("application/vnd.apple.mpegurl"));
  const fps = source.video.AverageFrameRate || 30;
  return Promise.all(
    qualityDimensions(source).map(async ([width, height], index) => {
      const codec =
        index === 0 && preserveVideo
          ? videoCodecString(source.video)
          : h264TargetCodec(width, height, fps);
      const result = {
        width,
        height,
        supported: null as boolean | null,
        smooth: null as boolean | null,
      };
      if (!codec) return result;
      const mime = `video/mp4; codecs="${codec}"`;
      const native =
        nativeHls || (index === 0 && preserveVideo && Boolean(video.canPlayType(mime)));
      const mimeSupported = native
        ? Boolean(video.canPlayType(mime))
        : typeof MediaSource !== "undefined" && MediaSource.isTypeSupported(mime);
      if (!mimeSupported) return { ...result, supported: false };
      if (!navigator.mediaCapabilities) return result;
      try {
        const probe = await navigator.mediaCapabilities.decodingInfo({
          type: native ? "file" : "media-source",
          video: {
            contentType: mime,
            width,
            height,
            bitrate: Math.max(
              source.video.BitRate || source.bitrate || 0,
              Math.round(width * height * Math.max(30, fps) * 0.24),
            ),
            framerate: fps,
          },
        });
        return { ...result, supported: probe.supported, smooth: probe.smooth ?? null };
      } catch {
        return result;
      }
    }),
  );
}

export function originalResolutionViolation(
  mode: QualityMode,
  target: QualityInfo["target_resolution"],
  width: number,
  height: number,
) {
  if (mode !== "Original" || !target || !width || !height) return null;
  return width < target.width || height < target.height
    ? "Delivered resolution is below the Original source target. Playback stopped; provider reduction is not accepted as Original."
    : null;
}
