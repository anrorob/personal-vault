import { Fragment, useState, type CSSProperties } from "react";
import { Pause, Play, SkipBack, SkipForward, Square } from "lucide-react";
import type { useMusicPlayer } from "../hooks/useMusicPlayer";
import type { MusicTrack } from "../lib/music-types";
import "./MusicAlbumPlayer.css";

function time(seconds: number) {
  const value = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}

export function MusicAlbumPlayer({
  player,
  sequence,
  tracks,
  title,
  artist,
}: {
  player: ReturnType<typeof useMusicPlayer>;
  sequence: MusicTrack[] | null;
  tracks: MusicTrack[];
  title: string;
  artist: string;
}) {
  const [stopped, setStopped] = useState(false);
  const active = tracks.find((track) => track.id === player.active?.id);
  const index = sequence?.findIndex((track) => track.id === active?.id) ?? -1;
  const rawDuration = active ? player.playbackDuration || active.duration_seconds || 0 : 0;
  const duration = Number.isFinite(rawDuration) ? Math.max(0, rawDuration) : 0;
  const position =
    active && Number.isFinite(player.playbackPosition)
      ? Math.max(0, Math.min(player.playbackPosition, duration))
      : 0;
  const busy = !!active && (player.playbackLoading || player.waitingForNext);
  const playing = !!active && player.playing;
  const state = !active
    ? stopped
      ? "Stopped"
      : "Ready"
    : player.playbackLoading
      ? "Loading"
      : player.waitingForNext
        ? "Next track"
        : playing
          ? "Playing"
          : "Paused";
  const multiDisc =
    !!sequence &&
    new Set(tracks.map((track) => track.disc_number).filter((n) => n !== null)).size > 1;
  const choose = (track: MusicTrack) => {
    if (active?.id === track.id) void player.play(track);
    else player.startAlbum(sequence ?? [track], track);
  };

  return (
    <section aria-label="Now playing" className="pv-music-console" data-playing={playing}>
      <div className="pv-console-rail">
        <h3>
          Personal Vault <span>/ Music</span>
        </h3>
        <span className="pv-console-index">
          {index >= 0
            ? `${String(index + 1).padStart(2, "0")} / ${String(sequence!.length).padStart(2, "0")}`
            : `${tracks.length} tracks`}
        </span>
      </div>
      <div className="pv-console-deck">
        <div className="pv-console-clock">
          <span role="status" className="pv-console-state">
            <i aria-hidden="true" />
            {state}
          </span>
          <div aria-label="Playback time" className="pv-console-time">
            <strong>{time(position)}</strong>
            <span>/ {time(duration)}</span>
          </div>
        </div>
        <div className="pv-console-display">
          <p className="pv-console-track" title={active?.title || title}>
            {active?.title || title || "Untitled album"}
          </p>
          {(active?.artist || artist) && (
            <p className="pv-console-artist" title={active?.artist || artist}>
              {active?.artist || artist}
            </p>
          )}
          {active && (
            <p className="pv-console-album" title={title}>
              {title}
            </p>
          )}
          <div className="pv-console-activity" aria-hidden="true" data-playing={playing}>
            {Array.from({ length: 18 }, (_, i) => (
              <span
                key={i}
                style={
                  {
                    "--bar-height": `${25 + ((i * 29) % 75)}%`,
                    "--bar-delay": `${-i * 0.13}s`,
                    "--bar-speed": `${0.7 + (i % 5) * 0.14}s`,
                  } as CSSProperties
                }
              />
            ))}
          </div>
        </div>
        <div className="pv-console-transport" role="group" aria-label="Playback controls">
          <button
            type="button"
            aria-label="Previous track"
            disabled={index <= 0}
            onClick={() => sequence && player.startAlbum(sequence, sequence[index - 1])}
          >
            <SkipBack size={18} />
          </button>
          <button
            type="button"
            className="pv-console-play"
            aria-label={playing ? "Pause" : "Play"}
            disabled={busy || (!active && !sequence?.length)}
            onClick={() =>
              active ? void player.play(active) : sequence && player.startAlbum(sequence)
            }
          >
            {playing ? <Pause size={20} /> : <Play size={20} />}
          </button>
          <button
            type="button"
            aria-label="Next track"
            disabled={index < 0 || !sequence || index >= sequence.length - 1}
            onClick={() => sequence && player.startAlbum(sequence, sequence[index + 1])}
          >
            <SkipForward size={18} />
          </button>
          <button
            type="button"
            aria-label="Stop"
            disabled={!active}
            onClick={() => {
              player.stop();
              setStopped(true);
            }}
          >
            <Square size={15} />
          </button>
        </div>
        <div
          className="pv-console-seek"
          style={
            { "--progress": `${duration ? (position / duration) * 100 : 0}%` } as CSSProperties
          }
        >
          <input
            type="range"
            aria-label="Playback position"
            aria-valuetext={`${time(position)} of ${time(duration)}`}
            min={0}
            max={Math.max(duration, 1)}
            step={0.1}
            value={position}
            disabled={!active || !player.canSeek || busy || duration <= 0}
            onChange={(event) => player.seek(Number(event.target.value))}
          />
        </div>
      </div>
      <section aria-label="Album tracks" className="pv-console-playlist">
        <div className="pv-console-playlist-heading">
          <h4>Album sequence</h4>
          <span>{tracks.length} tracks</span>
        </div>
        <ul>
          {tracks.map((track, trackIndex) => {
            const current = active?.id === track.id;
            return (
              <Fragment key={track.asset_id}>
                {multiDisc &&
                  track.disc_number !== null &&
                  (trackIndex === 0 ||
                    tracks[trackIndex - 1].disc_number !== track.disc_number) && (
                    <li className="pv-console-disc">Disc {track.disc_number}</li>
                  )}
                <li aria-current={current ? "true" : undefined}>
                  <button
                    type="button"
                    className="pv-console-row"
                    aria-label={`${current && playing ? "Pause" : "Play"} ${track.title || "track"}`}
                    onClick={() => choose(track)}
                  >
                    <span className="pv-console-row-icon" aria-hidden="true">
                      {current && playing ? <Pause size={13} /> : <Play size={13} />}
                    </span>
                    <span className="pv-console-row-number">
                      {sequence
                        ? String(trackIndex + 1).padStart(2, "0")
                        : (track.track_number ?? "—")}
                    </span>
                    <span className="pv-console-row-title" title={track.title}>
                      {track.title || "Untitled track"}
                      {track.artist && track.artist !== artist && <small>{track.artist}</small>}
                    </span>
                    {current && <span className="sr-only">{playing ? "Playing" : "Paused"}</span>}
                    <span className="pv-console-row-duration">
                      {track.duration_seconds !== null && Number.isFinite(track.duration_seconds)
                        ? time(track.duration_seconds)
                        : "—"}
                    </span>
                  </button>
                </li>
              </Fragment>
            );
          })}
        </ul>
      </section>
      {player.controls}
    </section>
  );
}
