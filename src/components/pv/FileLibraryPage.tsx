import { Archive, File, FileAudio, FileImage, FileText, Film, Package } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { formatBytes } from "@/lib/incoming";
import { getFileTitle, type VaultFileKind, type VaultLibraryFile } from "@/lib/vault-libraries";

export function FileLibraryPage({
  apiPath,
  title,
  description,
  emptyTitle,
  emptyDescription,
}: {
  apiPath: "documents" | "archives";
  title: string;
  description: string;
  emptyTitle: string;
  emptyDescription: string;
}) {
  const navigate = useNavigate();
  const [files, setFiles] = useState<VaultLibraryFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    const loadFiles = async () => {
      try {
        const response = await fetch(`/api/${apiPath}`, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });

        if (response.status === 401) {
          await navigate({ to: "/login" });
          return;
        }

        if (!response.ok) {
          throw new Error("Library request failed");
        }

        setFiles((await response.json()) as VaultLibraryFile[]);
      } catch (requestError) {
        if (requestError instanceof DOMException && requestError.name === "AbortError") {
          return;
        }

        setError(`${title} is currently unavailable.`);
      }
    };

    void loadFiles();
    return () => controller.abort();
  }, [apiPath, navigate, title]);

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div>
        <h2 className="pv-content-title text-xl">{title}</h2>
        <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
          {files === null
            ? `Opening ${title.toLowerCase()}...`
            : `${files.length} ${files.length === 1 ? "file" : "files"} · ${description}`}
        </p>
      </div>

      {error && <div className="pv-panel p-6 text-sm text-center text-red-300">{error}</div>}

      {!error && files?.length === 0 && (
        <div className="pv-panel p-10 text-center">
          <span
            className="mx-auto h-12 w-12 rounded-full flex items-center justify-center"
            style={{
              border: "1px solid var(--pv-border)",
              color: "var(--pv-gold)",
            }}
          >
            {apiPath === "archives" ? <Archive size={20} /> : <FileText size={20} />}
          </span>
          <h3 className="text-sm font-semibold mt-4" style={{ color: "var(--pv-silver)" }}>
            {emptyTitle}
          </h3>
          <p className="text-xs mt-2" style={{ color: "var(--pv-text-dim)" }}>
            {emptyDescription}
          </p>
        </div>
      )}

      {!error && files && files.length > 0 && (
        <div
          className="pv-panel overflow-hidden divide-y"
          style={{ borderColor: "var(--pv-border)" }}
        >
          {files.map((file) => (
            <a
              key={file.id}
              href={file.open_url}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-4 p-4 md:px-5 transition-colors hover:bg-white/[0.025]"
              aria-label={`Open ${file.name}`}
            >
              <span
                className="h-11 w-11 shrink-0 rounded-lg flex items-center justify-center"
                style={{
                  border: "1px solid var(--pv-border)",
                  color: "var(--pv-gold)",
                  background: "var(--pv-panel)",
                }}
              >
                <FileKindIcon kind={file.kind} />
              </span>
              <span className="min-w-0 flex-1">
                <span
                  className="block text-sm font-medium truncate"
                  style={{ color: "var(--pv-silver)" }}
                >
                  {file.display_title ?? getFileTitle(file.name)}
                </span>
                <span
                  className="block text-xs mt-1 truncate"
                  style={{ color: "var(--pv-text-dim)" }}
                >
                  {[
                    file.directory,
                    file.name,
                    formatBytes(file.size),
                    file.captured_on,
                    file.location,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
              </span>
              <span
                className="text-[10px] uppercase tracking-widest"
                style={{ color: "var(--pv-text-dim)" }}
              >
                {file.opens_inline ? "Open" : "Download"}
              </span>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

function FileKindIcon({ kind }: { kind: VaultFileKind }) {
  const props = { size: 19 };

  switch (kind) {
    case "video":
      return <Film {...props} />;
    case "image":
      return <FileImage {...props} />;
    case "audio":
      return <FileAudio {...props} />;
    case "pdf":
    case "document":
      return <FileText {...props} />;
    case "archive":
      return <Archive {...props} />;
    case "software":
      return <Package {...props} />;
    default:
      return <File {...props} />;
  }
}
