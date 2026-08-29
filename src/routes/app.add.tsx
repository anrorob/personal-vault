import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import {
  Check,
  CircleAlert,
  Clock3,
  FolderInput,
  Inbox,
  LoaderCircle,
  RotateCcw,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { type ArrivalHallListing, formatBytes, UploadError, uploadFile } from "@/lib/incoming";

export const Route = createFileRoute("/app/add")({
  component: AddToVault,
});

type QueueStatus = "waiting" | "uploading" | "staged" | "failed";

type QueueItem = {
  id: string;
  file: File;
  status: QueueStatus;
  progress: number;
  storedName: string | null;
  error: string | null;
};

function AddToVault() {
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const nextId = useRef(0);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [maxUploadBytes, setMaxUploadBytes] = useState<number | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    const loadLimit = async () => {
      try {
        const response = await fetch("/api/arrival-hall", {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });

        if (response.status === 401) {
          await navigate({ to: "/login" });
          return;
        }

        if (response.ok) {
          const listing = (await response.json()) as ArrivalHallListing;
          setMaxUploadBytes(listing.max_upload_bytes);
        }
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          console.error("Unable to load the upload limit", error);
        }
      }
    };

    void loadLimit();
    return () => controller.abort();
  }, [navigate]);

  const addFiles = useCallback(
    (files: FileList | File[]) => {
      const additions = Array.from(files).map((file) => {
        nextId.current += 1;
        const overLimit = maxUploadBytes !== null && file.size > maxUploadBytes;

        return {
          id: `${file.name}-${file.size}-${file.lastModified}-${nextId.current}`,
          file,
          status: overLimit ? ("failed" as const) : ("waiting" as const),
          progress: 0,
          storedName: null,
          error: overLimit
            ? `This file exceeds the ${formatBytes(maxUploadBytes)} upload limit.`
            : null,
        };
      });

      setQueue((current) => [...current, ...additions]);
    },
    [maxUploadBytes],
  );

  useEffect(() => {
    if (uploading) {
      return;
    }

    const nextItem = queue.find((item) => item.status === "waiting");
    if (!nextItem) {
      return;
    }

    setUploading(true);
    setQueue((current) =>
      current.map((item) =>
        item.id === nextItem.id ? { ...item, status: "uploading", progress: 0, error: null } : item,
      ),
    );

    void uploadFile(nextItem.file, (progress) => {
      setQueue((current) =>
        current.map((item) => (item.id === nextItem.id ? { ...item, progress } : item)),
      );
    })
      .then((result) => {
        setQueue((current) =>
          current.map((item) =>
            item.id === nextItem.id
              ? {
                  ...item,
                  status: "staged",
                  progress: 100,
                  storedName: result.stored_name,
                }
              : item,
          ),
        );
      })
      .catch((error: unknown) => {
        if (error instanceof UploadError && error.status === 401) {
          void navigate({ to: "/login" });
        }

        setQueue((current) =>
          current.map((item) =>
            item.id === nextItem.id
              ? {
                  ...item,
                  status: "failed",
                  error:
                    error instanceof Error ? error.message : "The upload could not be completed.",
                }
              : item,
          ),
        );
      })
      .finally(() => setUploading(false));
  }, [navigate, queue, uploading]);

  const stagedCount = queue.filter((item) => item.status === "staged").length;

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div>
        <h2 className="pv-content-title text-xl">Add to Vault</h2>
        <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
          Files are staged securely in the Arrival Hall before you move them to a permanent library.
        </p>
      </div>

      <div
        className="rounded-xl p-10 md:p-16 flex flex-col items-center text-center transition-colors"
        style={{
          border: `1.5px dashed ${dragActive ? "var(--pv-gold)" : "var(--pv-border-strong)"}`,
          background: dragActive
            ? "rgba(201,169,97,0.08)"
            : "radial-gradient(ellipse at top, rgba(201,169,97,0.05), transparent 60%), var(--pv-panel)",
        }}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragActive(true);
        }}
        onDragOver={(event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
        }}
        onDragLeave={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
            setDragActive(false);
          }
        }}
        onDrop={(event) => {
          event.preventDefault();
          setDragActive(false);
          addFiles(event.dataTransfer.files);
        }}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          className="sr-only"
          onChange={(event) => {
            if (event.target.files) {
              addFiles(event.target.files);
            }
            event.target.value = "";
          }}
        />
        <div
          className="h-14 w-14 rounded-full flex items-center justify-center mb-4"
          style={{ border: "1px solid var(--pv-border)", color: "var(--pv-gold)" }}
        >
          <Upload size={22} />
        </div>
        <h3 className="text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
          Drop files here
        </h3>
        <p className="mt-2 text-sm max-w-md" style={{ color: "var(--pv-text-dim)" }}>
          Select one or more files. Each upload is transferred directly into the Vault’s inert
          staging area.
        </p>
        <button
          type="button"
          className="pv-btn-primary mt-6"
          onClick={() => inputRef.current?.click()}
        >
          Browse Files
        </button>
        <div
          className="mt-6 inline-flex items-center gap-2 text-xs px-3 py-1.5 rounded-full"
          style={{ border: "1px solid var(--pv-border)", color: "var(--pv-silver-dim)" }}
        >
          <FolderInput size={13} />
          Destination: Arrival Hall
          {maxUploadBytes !== null && ` · Up to ${formatBytes(maxUploadBytes)} per file`}
        </div>
      </div>

      <div className="pv-panel p-5">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <div>
            <h4 className="text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
              Upload queue
            </h4>
            <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
              Keep this page open while files are uploading.
            </p>
          </div>
          <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            {queue.length} {queue.length === 1 ? "file" : "files"}
          </span>
        </div>

        {queue.length === 0 ? (
          <div className="mt-4 py-8 text-center text-sm" style={{ color: "var(--pv-text-dim)" }}>
            The queue is empty.
          </div>
        ) : (
          <div className="mt-5 space-y-3">
            {queue.map((item) => (
              <div
                key={item.id}
                className="rounded-lg p-4"
                style={{
                  background: "rgba(255,255,255,0.025)",
                  border: "1px solid var(--pv-border)",
                }}
              >
                <div className="flex items-start gap-3">
                  <QueueIcon status={item.status} />
                  <div className="min-w-0 flex-1">
                    <div className="flex justify-between gap-4">
                      <div className="min-w-0">
                        <p
                          className="text-sm font-medium truncate"
                          style={{ color: "var(--pv-silver)" }}
                        >
                          {item.file.name}
                        </p>
                        <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
                          {formatBytes(item.file.size)} · {getStatusLabel(item)}
                        </p>
                      </div>
                      {item.status === "failed" && (
                        <button
                          type="button"
                          className="shrink-0 inline-flex items-center gap-1.5 text-xs"
                          style={{ color: "var(--pv-gold)" }}
                          onClick={() =>
                            setQueue((current) =>
                              current.map((candidate) =>
                                candidate.id === item.id
                                  ? {
                                      ...candidate,
                                      status: "waiting",
                                      progress: 0,
                                      error: null,
                                    }
                                  : candidate,
                              ),
                            )
                          }
                        >
                          <RotateCcw size={13} />
                          Retry
                        </button>
                      )}
                    </div>
                    {item.status === "uploading" && (
                      <div
                        className="h-1.5 rounded-full overflow-hidden mt-3"
                        style={{ background: "rgba(255,255,255,0.07)" }}
                      >
                        <div
                          className="h-full rounded-full transition-[width]"
                          style={{
                            width: `${item.progress}%`,
                            background: "var(--pv-gold)",
                          }}
                        />
                      </div>
                    )}
                    {item.error && <p className="text-xs text-red-300 mt-2">{item.error}</p>}
                    {item.storedName && item.storedName !== item.file.name && (
                      <p className="text-xs mt-2" style={{ color: "var(--pv-text-dim)" }}>
                        Stored safely as {item.storedName} because that filename already existed.
                      </p>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {stagedCount > 0 && (
          <div
            className="mt-5 pt-5 flex flex-wrap items-center justify-between gap-3"
            style={{ borderTop: "1px solid var(--pv-border)" }}
          >
            <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              {stagedCount} {stagedCount === 1 ? "file is" : "files are"} now staged and inert.
            </p>
            <Link
              to="/app/arrival-hall"
              className="inline-flex items-center gap-2 text-sm"
              style={{ color: "var(--pv-gold)" }}
            >
              <Inbox size={15} />
              View Arrival Hall
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}

function QueueIcon({ status }: { status: QueueStatus }) {
  if (status === "uploading") {
    return <LoaderCircle size={18} className="animate-spin" style={{ color: "var(--pv-gold)" }} />;
  }
  if (status === "staged") {
    return <Check size={18} className="text-emerald-300" />;
  }
  if (status === "failed") {
    return <CircleAlert size={18} className="text-red-300" />;
  }
  return <Clock3 size={18} style={{ color: "var(--pv-text-dim)" }} />;
}

function getStatusLabel(item: QueueItem): string {
  if (item.status === "uploading") {
    return `Uploading ${item.progress}%`;
  }
  if (item.status === "staged") {
    return "Staged in Arrival Hall";
  }
  if (item.status === "failed") {
    return "Upload failed";
  }
  return "Waiting";
}
