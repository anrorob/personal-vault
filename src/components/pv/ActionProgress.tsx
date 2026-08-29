import { LoaderCircle } from "lucide-react";
import type { ReactNode } from "react";

export type ActionProgressState =
  | "idle"
  | "queued"
  | "running"
  | "completed"
  | "completed_with_warnings"
  | "failed";

export function ActionProgress({
  state,
  label,
  current,
  total,
  percent,
  detail,
  onRetry,
  retryLabel = "Retry",
  emphasis = "plain",
  showProgressBar = true,
}: {
  state: ActionProgressState;
  label: string;
  current?: number;
  total?: number;
  percent?: number;
  detail?: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  emphasis?: "plain" | "badge";
  showProgressBar?: boolean;
}) {
  const running = state === "queued" || state === "running";
  const hasCount = typeof current === "number" && typeof total === "number" && total > 0;
  const progress = hasCount
    ? Math.max(0, Math.min(100, ((current ?? 0) / (total ?? 1)) * 100))
    : typeof percent === "number"
      ? Math.max(0, Math.min(100, percent))
      : null;
  const color =
    state === "failed"
      ? "#fca5a5"
      : state === "completed_with_warnings" || running
        ? "var(--pv-gold)"
        : "var(--pv-text-dim)";

  return (
    <div
      className="inline-flex max-w-full flex-col gap-1 text-xs"
      style={
        emphasis === "badge"
          ? {
              color,
              background: "rgba(201,169,97,0.10)",
              border: "1px solid var(--pv-gold-dim)",
              borderRadius: "9999px",
              padding: "0.25rem 0.5rem",
            }
          : { color }
      }
    >
      <div className="inline-flex flex-wrap items-center gap-1.5" aria-live="polite" role="status">
        {running ? (
          <LoaderCircle aria-hidden="true" className="pv-action-spinner size-3 animate-spin" />
        ) : null}
        <span>
          {label}
          {hasCount
            ? ` · ${current} of ${total}`
            : typeof percent === "number"
              ? ` · ${percent}%`
              : ""}
        </span>
        {state === "failed" && onRetry ? (
          <button
            type="button"
            className="text-xs"
            style={{ color: "var(--pv-gold)" }}
            onClick={onRetry}
          >
            {retryLabel}
          </button>
        ) : null}
      </div>
      {progress !== null && showProgressBar ? (
        <div
          aria-label={`${label} progress`}
          aria-valuemax={100}
          aria-valuemin={0}
          aria-valuenow={Math.round(progress)}
          className="h-1 overflow-hidden rounded-full"
          role="progressbar"
          style={{ background: "var(--pv-border)" }}
        >
          <div
            className="h-full rounded-full"
            style={{ background: "var(--pv-gold)", width: `${progress}%` }}
          />
        </div>
      ) : null}
      {detail ? <span>{detail}</span> : null}
    </div>
  );
}
