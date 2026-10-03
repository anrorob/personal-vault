export type TheatreSource = {
  container: string;
  bitrate?: number;
  video: {
    Codec?: string;
    Profile?: string;
    Level?: number;
    Width?: number;
    Height?: number;
    BitRate?: number;
    AverageFrameRate?: number;
    BitDepth?: number;
    VideoRange?: string;
  };
  audio_tracks: {
    Index?: number;
    Codec?: string;
    Profile?: string;
    Channels?: number;
    SampleRate?: number;
    BitRate?: number;
    IsDefault?: boolean;
  }[];
};

export function videoCodecString(video: TheatreSource["video"]): string | null {
  if (video.Codec === "h264") {
    const profiles: Record<string, string> = {
      Baseline: "42",
      "Constrained Baseline": "42",
      Main: "4D",
      High: "64",
      "High 10": "6E",
    };
    const profile = profiles[video.Profile || ""];
    if (!profile || !video.Level) return null;
    return `avc1.${profile}00${Math.round(video.Level).toString(16).padStart(2, "0")}`;
  }
  if (video.Codec === "hevc" && ["Main", "Main 10"].includes(video.Profile || "") && video.Level) {
    return `hvc1.${video.Profile === "Main 10" ? "2" : "1"}.6.L${video.Level}.B0`;
  }
  if (video.Codec === "vc1") return "wvc1";
  // Unknown profile/level must not be presented as verified compatibility.
  return null;
}

export async function theatreCapabilities(
  source: TheatreSource,
  video: HTMLVideoElement,
  audioIndex?: number | null,
) {
  const audio =
    source.audio_tracks.find((a) => a.Index === audioIndex) ||
    source.audio_tracks.find((a) => a.IsDefault) ||
    source.audio_tracks[0];
  const audioCodecs: Record<string, string> = {
    aac: "mp4a.40.2",
    mp3: "mp4a.40.34",
    ac3: "ac-3",
    eac3: "ec-3",
    opus: "opus",
    flac: "flac",
    alac: "alac",
  };
  const vcodec = videoCodecString(source.video);
  const acodec = audioCodecs[audio?.Codec || ""];
  const nativeHls = Boolean(video.canPlayType("application/vnd.apple.mpegurl"));
  const supported = (mime: string, native: boolean) =>
    native
      ? Boolean(video.canPlayType(mime))
      : typeof MediaSource !== "undefined" && MediaSource.isTypeSupported(mime);
  const vmime = vcodec ? `video/mp4; codecs="${vcodec}"` : "";
  const amime = acodec ? `audio/mp4; codecs="${acodec}"` : "";
  const hdr = source.video.VideoRange && source.video.VideoRange !== "SDR";
  const checkVideo = async (mime: string, type: "file" | "media-source") => {
    if (!mime || !supported(mime, type === "file" || nativeHls)) return false;
    if (!navigator.mediaCapabilities || hdr) return false;
    try {
      const result = await navigator.mediaCapabilities.decodingInfo({
        type,
        video: {
          contentType: mime,
          width: source.video.Width || 0,
          height: source.video.Height || 0,
          bitrate: source.video.BitRate || source.bitrate || 0,
          framerate: source.video.AverageFrameRate || 0,
        },
      });
      return result.supported;
    } catch {
      return false;
    }
  };
  const container = (
    { mp4: "video/mp4", m4v: "video/mp4", webm: "video/webm", mkv: "video/x-matroska" } as Record<
      string,
      string
    >
  )[source.container];
  const directMime =
    container && vcodec && (!audio || acodec)
      ? `${container}; codecs="${[vcodec, ...(audio ? [acodec] : [])].join(",")}"`
      : "";
  const videoCopy = await checkVideo(vmime, nativeHls ? "file" : "media-source");
  const audioCopy = !audio || Boolean(amime && supported(amime, nativeHls));
  return {
    direct: Boolean(
      directMime &&
      supported(directMime, true) &&
      (await checkVideo(vmime, "file")) &&
      (!audio || (amime && supported(amime, true))),
    ),
    video_copy: videoCopy,
    audio_copy: audioCopy,
    h264: supported('video/mp4; codecs="avc1.640028"', nativeHls),
    aac: supported('audio/mp4; codecs="mp4a.40.2"', nativeHls),
  };
}
