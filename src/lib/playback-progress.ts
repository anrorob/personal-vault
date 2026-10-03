/** Reports viewer progress independently of timeline direction and callback renders. */
export function attachPlaybackProgress(
  video: HTMLVideoElement,
  send: (position: number, duration: number, completed: boolean) => void,
  now = () => performance.now(),
) {
  let lastTime = -Infinity;
  let lastSent = "";
  let latest: [number, number, boolean] | null = null;
  const capture = (completed = false) => {
    if (Number.isFinite(video.duration) && video.duration > 0 && Number.isFinite(video.currentTime))
      latest = [video.currentTime, video.duration, completed || video.ended];
  };
  const flush = () => {
    if (!latest || JSON.stringify(latest) === lastSent) return;
    lastSent = JSON.stringify(latest);
    lastTime = now();
    send(...latest);
  };
  const timed = () => {
    capture();
    if (now() - lastTime >= 10_000) flush();
  };
  const immediate = () => {
    capture();
    flush();
  };
  const ended = () => {
    capture(true);
    flush();
  };
  const events: [string, EventListener][] = [
    ["timeupdate", timed],
    ["pause", immediate],
    ["seeked", immediate],
    ["ended", ended],
  ];
  events.forEach(([name, listener]) => video.addEventListener(name, listener));
  window.addEventListener("pagehide", immediate);
  return () => {
    // Stream teardown may already have reset duration/currentTime. Keep the last valid snapshot.
    flush();
    events.forEach(([name, listener]) => video.removeEventListener(name, listener));
    window.removeEventListener("pagehide", immediate);
  };
}
