import { expect, test } from "bun:test";
import {
  attachTheatreBufferGate,
  playableAhead,
  theatreBufferPolicy,
} from "../src/lib/theatre-buffering";

const ranges = (items: [number, number][]): TimeRanges => ({
  length: items.length,
  start: (i) => items[i][0],
  end: (i) => items[i][1],
});

class Media extends EventTarget {
  currentTime = 0;
  duration = 300;
  readyState = 3;
  paused = true;
  seeking = false;
  ended = false;
  buffered = ranges([]);
  playCalls = 0;
  frameCallback: (() => void) | null = null;
  play() {
    this.playCalls++;
    this.paused = false;
    this.dispatchEvent(new Event("play"));
    if (!this.paused) this.dispatchEvent(new Event("playing"));
    return Promise.resolve();
  }
  pause() {
    this.paused = true;
    this.dispatchEvent(new Event("pause"));
  }
  requestVideoFrameCallback(callback: () => void) {
    this.frameCallback = callback;
    return 1;
  }
  cancelVideoFrameCallback() {
    this.frameCallback = null;
  }
  emit(event: string) {
    this.dispatchEvent(new Event(event));
  }
}

function setup() {
  const media = new Media();
  let now = 0;
  let failures = 0;
  const gate = attachTheatreBufferGate(media as unknown as HTMLVideoElement, {
    policy: theatreBufferPolicy(),
    now: () => now,
    onView: () => {},
    onFailure: () => failures++,
  });
  return {
    media,
    gate,
    advance: (ms: number) => {
      now += ms;
      gate.tick();
    },
    failures: () => failures,
  };
}

test("time limits also enforce the estimated forward payload budget at high bitrate", () => {
  for (const rate of [8_000_000, 20_000_000, 80_000_000]) {
    for (const memory of [2, 8]) {
      const p = theatreBufferPolicy(rate, memory);
      expect(p.hls.maxMaxBufferLength).toBeLessThanOrEqual(90);
      expect(((p.hls.maxMaxBufferLength + 3) * rate) / 8).toBeLessThanOrEqual(p.hls.maxBufferSize);
      expect(p.startupSeconds).toBeLessThan(p.hls.maxMaxBufferLength);
      expect(p.seekSeconds).toBeLessThanOrEqual(6);
      expect(p.seekSeconds).toBeLessThan(p.hls.maxMaxBufferLength);
      expect(p.hls.backBufferLength).toBe(10);
      expect(p.hls.frontBufferFlushThreshold).toBe(p.hls.maxMaxBufferLength);
      expect(p.hls.autoStartLoad).toBe(true);
    }
  }
  expect(theatreBufferPolicy(20_000_000).hls.maxBufferLength).toBeGreaterThan(60);
  expect(theatreBufferPolicy(8_000_000).hls.maxBufferLength).toBe(90);
});

test("forward-buffer measurement excludes disconnected ranges", () => {
  expect(
    playableAhead(
      ranges([
        [0, 10],
        [100, 200],
      ]),
      8,
    ),
  ).toBe(2);
  expect(playableAhead(ranges([[100, 200]]), 8)).toBe(0);
});

test("startup waits for actual 24-second playable range, not elapsed time", () => {
  const { media, gate, advance } = setup();
  gate.start();
  expect(gate.snapshot().required_resume_buffer_seconds).toBe(24);
  media.buffered = ranges([[0, 3]]);
  advance(5_000);
  expect(media.playCalls).toBe(0);
  media.buffered = ranges([[0, 24]]);
  media.emit("progress");
  expect(media.playCalls).toBe(1);
  expect(gate.snapshot().buffered_seconds_at_start).toBe(24);
  advance(30);
  media.frameCallback?.();
  expect(gate.snapshot().startup_seconds).toBe(5.03);
  gate.dispose();
});

test("native Play cannot bypass the startup buffer gate", async () => {
  const { media, gate } = setup();
  gate.start();
  await media.play();
  expect(media.paused).toBe(true);
  expect(gate.snapshot().buffered_seconds_at_start).toBeNull();
  gate.dispose();
});

test("autoplay denial shows a manual Play prompt without a restart", async () => {
  const { media, gate } = setup();
  media.buffered = ranges([[0, 25]]);
  media.play = () =>
    Promise.reject(Object.assign(new Error("blocked"), { name: "NotAllowedError" }));
  gate.start();
  await Promise.resolve();
  expect(gate.snapshot().phase).toBe("blocked");
  gate.dispose();
});

test("buffered bytes without future decoded media cannot release the gate", () => {
  const { media, gate } = setup();
  media.buffered = ranges([[0, 30]]);
  media.readyState = 2;
  gate.start();
  expect(media.playCalls).toBe(0);
  media.readyState = 3;
  media.emit("canplay");
  expect(media.playCalls).toBe(1);
  gate.dispose();
});

test("a waiting event with ample buffer does not create a pause/play loop", () => {
  const { media, gate } = setup();
  media.buffered = ranges([[0, 30]]);
  gate.start();
  media.emit("waiting");
  media.emit("waiting");
  expect(media.playCalls).toBe(1);
  expect(gate.snapshot().rebuffer_count).toBe(0);
  expect(gate.snapshot().waiting_events).toBe(2);
  gate.dispose();
});

test("short media starts when its entire remaining range is playable", () => {
  const { media, gate } = setup();
  media.duration = 8;
  media.buffered = ranges([[0, 8]]);
  gate.start();
  expect(media.paused).toBe(false);
  gate.dispose();
});

test("a stall holds position and rebuilds 12 seconds without repeated resume attempts", () => {
  const { media, gate } = setup();
  media.buffered = ranges([[0, 25]]);
  gate.start();
  media.currentTime = 24.8;
  media.emit("waiting");
  media.emit("waiting");
  expect(media.paused).toBe(true);
  expect(gate.snapshot().rebuffer_count).toBe(1);
  expect(gate.snapshot().required_resume_buffer_seconds).toBe(12);
  media.buffered = ranges([[0, 30]]);
  media.emit("progress");
  expect(media.playCalls).toBe(1);
  media.buffered = ranges([[0, 40]]);
  media.emit("progress");
  expect(media.playCalls).toBe(2);
  expect(media.currentTime).toBe(24.8);
  gate.dispose();
});

test.each([20, 150, 5])("seek to %s uses six seconds at the new position", (position) => {
  const { media, gate } = setup();
  media.buffered = ranges([[0, 30]]);
  gate.start();
  media.currentTime = 15;
  media.currentTime = position;
  media.seeking = true;
  media.emit("seeking");
  expect(gate.snapshot().phase).toBe("seeking");
  expect(gate.snapshot().required_resume_buffer_seconds).toBe(6);
  media.buffered = ranges([
    [position, position + 3],
    [250, 290],
  ]);
  media.seeking = false;
  media.emit("seeked");
  expect(media.paused).toBe(true);
  media.buffered = ranges([[position, position + 6]]);
  media.emit("progress");
  expect(media.paused).toBe(false);
  expect(media.currentTime).toBe(position);
  expect(gate.snapshot().required_resume_buffer_seconds).toBeNull();
  expect(gate.snapshot().rebuffer_count).toBe(0);
  gate.dispose();
});

test("quick scene jumps release only the final position then allow normal refill", () => {
  const { media, gate, failures } = setup();
  media.buffered = ranges([[0, 30]]);
  gate.start();
  for (const position of [50, 200, 80, 170]) {
    media.currentTime = position;
    media.seeking = true;
    media.emit("seeking");
    media.buffered = ranges([[position, position + 6]]);
    media.emit("progress");
    expect(media.playCalls).toBe(1);
  }
  // An obsolete request can complete after the last jump; it cannot release it.
  media.buffered = ranges([[80, 140]]);
  media.seeking = false;
  media.emit("seeked");
  expect(media.playCalls).toBe(1);
  media.buffered = ranges([[170, 176]]);
  media.emit("progress");
  expect(media.playCalls).toBe(2);
  expect(gate.snapshot().phase).toBe("ready");
  media.buffered = ranges([[170, 235]]);
  media.emit("progress");
  expect(gate.snapshot().buffered_seconds_ahead).toBe(65);
  expect(media.playCalls).toBe(2);
  expect(failures()).toBe(0);
  // A later spontaneous stall must still rebuild the conservative target.
  media.currentTime = 234.8;
  media.emit("waiting");
  expect(gate.snapshot().phase).toBe("rebuffering");
  expect(gate.snapshot().required_resume_buffer_seconds).toBe(12);
  media.buffered = ranges([[234, 241]]);
  media.emit("progress");
  expect(media.playCalls).toBe(2);
  media.buffered = ranges([[234, 247]]);
  media.emit("progress");
  expect(media.playCalls).toBe(3);
  gate.dispose();
});

test("a seek near the end needs only the remaining playable media", () => {
  const { media, gate } = setup();
  media.buffered = ranges([[0, 30]]);
  gate.start();
  media.currentTime = 297;
  media.emit("seeking");
  expect(gate.snapshot().required_resume_buffer_seconds).toBe(3);
  media.buffered = ranges([[297, 299]]);
  media.emit("seeked");
  expect(media.paused).toBe(true);
  media.buffered = ranges([[297, 300]]);
  media.emit("progress");
  expect(media.paused).toBe(false);
  expect(media.playCalls).toBe(2);
  gate.dispose();
});

test("seeking while user-paused never resumes automatically", () => {
  const { media, gate } = setup();
  media.buffered = ranges([[0, 30]]);
  gate.start();
  media.pause();
  media.currentTime = 100;
  media.emit("seeking");
  media.buffered = ranges([[100, 150]]);
  media.emit("seeked");
  expect(media.playCalls).toBe(1);
  expect(media.paused).toBe(true);
  gate.dispose();
});

test("resume position during initial load still requires startup threshold", () => {
  const { media, gate } = setup();
  media.currentTime = 100;
  gate.start();
  media.emit("seeking");
  media.buffered = ranges([[100, 114]]);
  media.emit("seeked");
  expect(media.playCalls).toBe(0);
  media.buffered = ranges([[100, 125]]);
  media.emit("progress");
  expect(media.playCalls).toBe(1);
  gate.dispose();
});

test("limited native buffering falls back only with measured playable media", () => {
  const { media, gate, advance } = setup();
  gate.start();
  media.buffered = ranges([[0, 8]]);
  advance(44_999);
  expect(media.playCalls).toBe(0);
  advance(1);
  expect(media.playCalls).toBe(1);
  expect(gate.snapshot().fallback_reason).not.toBeNull();
  gate.dispose();
});

test("an empty buffer times out visibly instead of hanging or starting empty", () => {
  const { media, gate, advance, failures } = setup();
  gate.start();
  advance(90_000);
  expect(media.playCalls).toBe(0);
  expect(failures()).toBe(1);
  expect(gate.snapshot().phase).toBe("failed");
  advance(10_000);
  expect(failures()).toBe(1);
  gate.dispose();
});

test("missing metadata also has a finite preparation deadline", () => {
  const { media, gate, advance, failures } = setup();
  advance(90_000);
  expect(media.playCalls).toBe(0);
  expect(gate.snapshot().phase).toBe("failed");
  expect(failures()).toBe(1);
  gate.dispose();
});

test("diagnostics retain 30s and 120s samples and disposal removes all listeners", () => {
  const { media, gate, advance } = setup();
  media.buffered = ranges([[0, 30]]);
  gate.start();
  media.currentTime = 30;
  media.buffered = ranges([[20, 100]]);
  advance(30_000);
  media.currentTime = 120;
  media.buffered = ranges([[110, 200]]);
  advance(90_000);
  expect(gate.snapshot().buffer_samples_after_start).toEqual({ "30": 70, "120": 80 });
  gate.dispose();
  media.emit("waiting");
  expect(gate.snapshot().rebuffer_count).toBe(0);
  expect(media.frameCallback).toBeNull();
});

test("quality renegotiation reuses the seek gate and preserves paused intent", () => {
  for (const wantsPlay of [true, false]) {
    const media = new Media();
    media.currentTime = 100;
    const gate = attachTheatreBufferGate(media as unknown as HTMLVideoElement, {
      policy: theatreBufferPolicy(),
      resume: true,
      wantsPlay,
      onView: () => {},
      onFailure: () => {},
    });
    gate.start();
    expect(gate.snapshot().required_resume_buffer_seconds).toBe(6);
    media.buffered = ranges([[100, 105]]);
    media.emit("progress");
    expect(media.playCalls).toBe(0);
    media.buffered = ranges([[100, 106]]);
    media.emit("progress");
    expect(media.playCalls).toBe(wantsPlay ? 1 : 0);
    expect(theatreBufferPolicy().hls.maxMaxBufferLength).toBeGreaterThan(6);
    gate.dispose();
  }
});
