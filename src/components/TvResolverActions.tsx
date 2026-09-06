import { tvBatchActions, tvCounts, type TvResolverBatch } from "@/lib/tv-resolver";

export type TvResolverAction = "approve" | "publish-extras" | "retry";

export function TvResolverActions({
  batch,
  busy,
  onAction,
}: {
  batch: TvResolverBatch;
  busy: boolean;
  onAction: (action: TvResolverAction) => void;
}) {
  const actions = tvBatchActions(batch);
  const counts = tvCounts(batch);
  return (
    <>
      {actions.approve && (
        <button
          type="button"
          className="pv-btn-primary"
          disabled={busy || counts.unresolved > 0 || counts.failed > 0}
          onClick={() => onAction("approve")}
        >
          {busy ? "Working…" : "Approve batch"}
        </button>
      )}
      {actions.publishExtras && (
        <button
          type="button"
          className="pv-btn-primary"
          disabled={busy}
          onClick={() => onAction("publish-extras")}
        >
          {busy ? "Working…" : "Publish extras"}
        </button>
      )}
      {actions.retry && (
        <button
          type="button"
          className="pv-btn-secondary"
          disabled={busy}
          onClick={() => onAction("retry")}
        >
          Retry failed
        </button>
      )}
    </>
  );
}
