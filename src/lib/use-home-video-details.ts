import { useCallback, useEffect, useMemo, useState } from "react";

export function useHomeVideoDetails<T extends { file_id: string; asset_id: string }>(
  fileId: string | null,
) {
  const [state, setState] = useState<{ fileId: string; value: T } | null>(null);
  const scope = useMemo(
    () => ({ fileId, controller: new AbortController(), sequence: 0 }),
    [fileId],
  );
  const reload = useCallback(async () => {
    if (!fileId || scope.controller.signal.aborted) return;
    const sequence = ++scope.sequence;
    try {
      const response = await fetch(`/api/personal-videos/${fileId}/details`, {
        credentials: "include",
        cache: "no-store",
        signal: scope.controller.signal,
      });
      if (!response.ok) throw new Error("Video details unavailable");
      const value = (await response.json()) as T;
      if (scope.controller.signal.aborted || sequence !== scope.sequence) return;
      if (value.file_id !== fileId || !value.asset_id) throw new Error("Video identity mismatch");
      setState({ fileId, value });
    } catch {
      if (!scope.controller.signal.aborted && sequence === scope.sequence) setState(null);
    }
  }, [fileId, scope]);
  useEffect(() => {
    if (scope.controller.signal.aborted) scope.controller = new AbortController();
    void reload();
    return () => scope.controller.abort();
  }, [reload, scope]);
  // Reject stale state during the selection render, before effects run.
  return { details: state?.fileId === fileId ? state.value : null, reload };
}
