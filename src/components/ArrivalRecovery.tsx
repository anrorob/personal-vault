import { useCallback, useEffect, useState } from "react";

type RecoveryItem = {
  item_id: string;
  filename: string;
  state: string;
  status: string;
  message: string;
  can_remove: boolean;
  can_retry: boolean;
};

export function ArrivalRecovery({
  onChanged,
  revision = "",
}: {
  onChanged: () => Promise<void>;
  revision?: string;
}) {
  const [items, setItems] = useState<RecoveryItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const load = useCallback(async () => {
    const response = await fetch("/api/vault-master/recovery", {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok)
      throw new Error("Recovery checks are unavailable. Your staged files are unchanged.");
    const result = await response.json();
    setItems(result.items);
  }, []);
  useEffect(() => {
    void load().catch((error: Error) => setMessage(error.message));
  }, [load, revision]);

  async function recover(action: "remove" | "recheck" | "retry", ids: string[]) {
    if (
      action === "remove" &&
      !window.confirm(
        "Remove the selected unpublished files from Arrival Hall? Published or uncertain items will be skipped. This cannot be undone.",
      )
    )
      return;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/vault-master/recovery", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action,
          item_ids: ids,
          ...(action === "remove" ? { confirmation: "REMOVE FROM ARRIVAL HALL" } : {}),
        }),
      });
      if (!response.ok) throw new Error("Recovery could not complete. Recheck the selected items.");
      const result = (await response.json()) as {
        outcomes: { item_id: string; processed: boolean; message: string }[];
      };
      const skipped = result.outcomes.filter((item) => !item.processed);
      setMessage(
        `${result.outcomes.length - skipped.length} processed; ${skipped.length} need attention.${skipped.map((item) => ` ${items.find((entry) => entry.item_id === item.item_id)?.filename ?? "Item"}: ${item.message}`).join("")}`,
      );
      setSelected(new Set(skipped.map((item) => item.item_id)));
      await load();
      await onChanged();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Recovery could not complete. Recheck.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pv-panel space-y-3 p-5" aria-label="Arrival Hall recovery">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">Intake recovery</h2>
        <button
          className="pv-btn-secondary px-3 py-2 text-xs"
          disabled={busy}
          onClick={() => void load().catch((error: Error) => setMessage(error.message))}
        >
          Recheck availability
        </button>
      </div>
      <p className="text-xs">
        Review, retry or remove unpublished staging. Published content is never deleted here.
      </p>
      {message && (
        <p role="status" className="break-words text-xs">
          {message}
        </p>
      )}
      {selected.size > 0 && (
        <div className="flex flex-wrap gap-2">
          <span className="text-xs">{selected.size} selected</span>
          <button
            className="pv-btn-secondary px-3 py-2 text-xs"
            disabled={busy}
            onClick={() => void recover("recheck", [...selected])}
          >
            Recheck selected
          </button>
          <button
            className="pv-btn-danger px-3 py-2 text-xs"
            disabled={busy}
            onClick={() => void recover("remove", [...selected])}
          >
            Remove selected staging
          </button>
          <button
            className="pv-btn-secondary px-3 py-2 text-xs"
            disabled={busy}
            onClick={() => setSelected(new Set())}
          >
            Clear
          </button>
        </div>
      )}
      {items.map((item) => (
        <div
          key={item.item_id}
          className="space-y-2 border-t py-3"
          style={{ borderColor: "var(--pv-border)" }}
        >
          <label className="flex min-w-0 items-start gap-2 text-xs">
            <input
              type="checkbox"
              disabled={busy}
              checked={selected.has(item.item_id)}
              onChange={() =>
                setSelected((current) => {
                  const next = new Set(current);
                  if (next.has(item.item_id)) next.delete(item.item_id);
                  else next.add(item.item_id);
                  return next;
                })
              }
            />
            <span className="min-w-0 break-words">{item.filename}</span>
          </label>
          {item.status === "needs_recovery" && (
            <p className="text-xs font-semibold">Needs recovery</p>
          )}
          <p className="break-words text-xs">{item.message}</p>
          <div className="flex flex-wrap gap-2">
            <button
              className="pv-btn-secondary px-3 py-2 text-xs"
              disabled={busy}
              onClick={() => void recover("recheck", [item.item_id])}
            >
              Recheck / reconcile
            </button>
            {item.can_retry && (
              <button
                className="pv-btn-primary px-3 py-2 text-xs"
                disabled={busy}
                onClick={() => void recover("retry", [item.item_id])}
              >
                Retry safe move
              </button>
            )}
            {item.can_remove && (
              <button
                className="pv-btn-danger px-3 py-2 text-xs"
                disabled={busy}
                onClick={() => void recover("remove", [item.item_id])}
              >
                Remove from Arrival Hall
              </button>
            )}
          </div>
        </div>
      ))}
      {items.length === 0 && <p className="text-xs">No unfinished intake records to recover.</p>}
    </section>
  );
}
