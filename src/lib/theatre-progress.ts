import { useCallback, useEffect, useRef, useState } from "react";

export type PlaybackState = {
  position_seconds: number;
  duration_seconds: number;
  completed: boolean;
  state: "unwatched" | "in_progress" | "watched";
};
export function resumePosition(progress?: PlaybackState) {
  return progress?.state === "in_progress" ? progress.position_seconds : 0;
}

/** Uses the existing PV per-viewer records. Writes are ordered across progress/manual actions. */
export function useTheatreProgress(collection: "movies" | "tv-episodes") {
  const [progress, setProgress] = useState<Record<string, PlaybackState>>({});
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const active = useRef(true);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  useEffect(() => {
    active.current = true;
    const controller = new AbortController();
    void fetch(`/api/user-state/${collection}`, {
      credentials: "include",
      signal: controller.signal,
    })
      .then(async (r) => {
        if (!r.ok) throw new Error();
        return r.json() as Promise<Record<string, PlaybackState>>;
      })
      .then((value) => {
        if (active.current && !controller.signal.aborted) {
          setProgress((current) => ({ ...value, ...current }));
          setLoaded(true);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted && active.current)
          setError("Playback state could not be loaded.");
      });
    return () => {
      active.current = false;
      controller.abort();
    };
  }, [collection]);
  const write = useCallback(
    (id: string, body: object, manual = false) => {
      if (manual) setPending((count) => count + 1);
      const request = queue.current
        .then(async () => {
          const response = await fetch(
            `/api/user-state/${collection}/${id}${manual ? "/watched" : ""}`,
            {
              method: "PUT",
              credentials: "include",
              keepalive: true,
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            },
          );
          if (!response.ok) throw new Error("Playback state could not be saved.");
          const value = (await response.json()) as PlaybackState;
          if (active.current) {
            setProgress((current) => ({ ...current, [id]: value }));
            setError(null);
          }
        })
        .catch(() => {
          if (active.current) setError("Playback state could not be saved.");
        })
        .finally(() => {
          if (manual && active.current) setPending((count) => count - 1);
        });
      queue.current = request;
      return request;
    },
    [collection],
  );
  const save = useCallback(
    (id: string, position_seconds: number, duration_seconds: number, completed: boolean) => {
      void write(id, { position_seconds, duration_seconds, completed });
    },
    [write],
  );
  const mark = useCallback((id: string, watched: boolean) => write(id, { watched }, true), [write]);
  return { progress, loaded, error, pending: pending > 0, save, mark };
}
