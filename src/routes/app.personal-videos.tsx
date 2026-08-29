import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Clapperboard, Pencil, Play, Plus } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatBytes } from "@/lib/incoming";
import { getFileTitle, type VaultLibraryFile } from "@/lib/vault-libraries";
import { ActionProgress, type ActionProgressState } from "@/components/pv/ActionProgress";

export const Route = createFileRoute("/app/personal-videos")({ component: PersonalVideosPage });

type VideoJob = {
  id: string;
  status: string;
  requested_reanalysis: boolean;
  total_frames: number;
  frames_completed: number;
  frames_failed: number;
  warning?: string | null;
  error?: string | null;
};
type Person = { id: string; display_name: string; source?: string };
type Term = { namespace: "content_tag"; slug: string; display_name: string; source?: string };
type VideoDetails = {
  asset_id: string;
  name: string;
  display_title: string | null;
  analysis: VideoJob | null;
  narrative: string | null;
  narrative_source: "user" | "vault_master" | "none";
  people: Person[];
  content_tags: Term[];
  warnings: string[];
};

const labels: Record<string, string> = {
  queued: "Queued",
  sampling: "Sampling",
  analysing: "Analysing",
  analysis_complete: "Reconciling",
  reconciling: "Reconciling",
  completed: "Completed",
  completed_with_warnings: "Completed with warnings",
  failed: "Failed",
};
const active = new Set(["queued", "sampling", "analysing", "analysis_complete", "reconciling"]);

function PersonalVideosPage() {
  const navigate = useNavigate();
  const [videos, setVideos] = useState<VaultLibraryFile[] | null>(null);
  const [selected, setSelected] = useState<VaultLibraryFile | null>(null);
  const [details, setDetails] = useState<VideoDetails | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playbackError, setPlaybackError] = useState(false);
  const reload = useCallback(async () => {
    if (!selected) return;
    const response = await fetch(`/api/personal-videos/${selected.id}/details`, {
      credentials: "include",
    });
    if (!response.ok) throw new Error();
    setDetails((await response.json()) as VideoDetails);
  }, [selected]);
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/personal-videos", { credentials: "include", signal: controller.signal })
      .then(async (response) => {
        if (response.status === 401) return navigate({ to: "/login" });
        if (!response.ok) throw new Error();
        setVideos((await response.json()) as VaultLibraryFile[]);
      })
      .catch((cause) => {
        if (!(cause instanceof DOMException && cause.name === "AbortError"))
          setError("Home Videos is currently unavailable.");
      });
    return () => controller.abort();
  }, [navigate]);
  useEffect(() => {
    if (selected) void reload().catch(() => setDetails(null));
  }, [reload, selected]);
  useEffect(() => {
    if (!details?.analysis || !active.has(details.analysis.status)) return;
    const timer = window.setTimeout(() => void reload(), 1500);
    return () => window.clearTimeout(timer);
  }, [details?.analysis, reload]);
  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div>
        <h2 className="pv-content-title text-xl">Home Videos</h2>
        <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
          {videos === null
            ? "Opening your recordings..."
            : `${videos.length} ${videos.length === 1 ? "video" : "videos"}`}
        </p>
      </div>
      {error && <div className="pv-panel p-6 text-sm text-center text-red-300">{error}</div>}
      {!error && videos?.length === 0 && (
        <div className="pv-panel p-10 text-center">
          <Clapperboard className="mx-auto" style={{ color: "var(--pv-gold)" }} />
          <h3 className="text-sm font-semibold mt-4">Home Videos is empty</h3>
        </div>
      )}
      {!error && videos && videos.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
          {videos.map((video) => (
            <button
              key={video.id}
              type="button"
              className="pv-panel pv-panel-hover overflow-hidden text-left group"
              onClick={() => {
                setSelected(video);
                setDetails(null);
                setPlaybackError(false);
              }}
            >
              <span
                className="aspect-video flex items-center justify-center relative overflow-hidden"
                style={{ background: "#09090b", color: "var(--pv-gold)" }}
              >
                <video
                  src={video.open_url}
                  className="absolute inset-0 h-full w-full object-cover"
                  muted
                  playsInline
                  preload="metadata"
                />
                <span
                  className="relative h-12 w-12 rounded-full flex items-center justify-center"
                  style={{
                    background: "rgba(201,169,97,0.12)",
                    border: "1px solid var(--pv-gold-dim)",
                  }}
                >
                  <Play size={20} fill="currentColor" />
                </span>
              </span>
              <span className="block p-4">
                <span
                  className="block text-sm font-medium truncate"
                  style={{ color: "var(--pv-silver)" }}
                >
                  {video.display_title ?? getFileTitle(video.name)}
                </span>
                <span
                  className="block text-xs mt-1 truncate"
                  style={{ color: "var(--pv-text-dim)" }}
                >
                  {[video.directory, formatBytes(video.size)].filter(Boolean).join(" · ")}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}
      <Dialog
        open={selected !== null}
        onOpenChange={(open) => {
          if (!open) {
            setSelected(null);
            setDetails(null);
          }
        }}
      >
        <DialogContent
          className="pv-dialog max-w-5xl max-h-[92vh] overflow-y-auto p-0"
          style={{ background: "#08080a", borderColor: "var(--pv-border)" }}
        >
          <DialogHeader className="px-6 pt-6">
            <DialogTitle>
              {selected?.display_title ?? (selected ? getFileTitle(selected.name) : "Home Video")}
            </DialogTitle>
            <DialogDescription>{selected?.directory ?? "Home Videos"}</DialogDescription>
          </DialogHeader>
          <div className="px-6 pb-6 space-y-5">
            <div
              className="aspect-video overflow-hidden rounded-md bg-black flex items-center justify-center"
              style={{ border: "1px solid var(--pv-border)" }}
            >
              {selected && !playbackError && (
                <video
                  key={selected.id}
                  src={selected.open_url}
                  className="h-full w-full"
                  controls
                  autoPlay
                  playsInline
                  onError={() => setPlaybackError(true)}
                />
              )}
              {playbackError && (
                <p className="text-sm text-red-300">
                  This recording could not be played by the browser.
                </p>
              )}
            </div>
            <VideoIntelligence details={details} reload={reload} />
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function VideoIntelligence({
  details,
  reload,
}: {
  details: VideoDetails | null;
  reload: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [people, setPeople] = useState<Person[]>([]);
  const [terms, setTerms] = useState<Term[]>([]);
  const [personQuery, setPersonQuery] = useState("");
  const [personId, setPersonId] = useState("");
  const [tagId, setTagId] = useState("");
  const [showPeoplePicker, setShowPeoplePicker] = useState(false);
  const [showTagPicker, setShowTagPicker] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    void fetch("/api/gallery/people", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then((items) => setPeople(items as Person[]));
    void fetch("/api/personal-videos/intelligence/terms", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then((items) => setTerms(items as Term[]));
  }, []);
  useEffect(() => {
    if (!editing) setDraft(details?.narrative ?? "");
  }, [details?.narrative, editing]);
  const patch = async (path: string, body: object) => {
    setBusy(true);
    setMessage(null);
    try {
      const response = await fetch(path, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok)
        throw new Error(
          ((await response.json()) as { detail?: string }).detail ??
            "The change could not be saved.",
        );
      await reload();
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : "The change could not be saved.");
    } finally {
      setBusy(false);
    }
  };
  const queue = async (reanalyse: boolean) => {
    if (!details) return;
    setBusy(true);
    try {
      const response = await fetch("/api/personal-videos/intelligence/jobs", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset_id: details.asset_id, reanalyse }),
      });
      if (!response.ok) throw new Error();
      await reload();
    } catch {
      setMessage("Video Intelligence analysis could not be queued.");
    } finally {
      setBusy(false);
    }
  };
  if (!details)
    return (
      <section className="pv-panel p-5 text-sm" style={{ color: "var(--pv-text-dim)" }}>
        Loading Video Intelligence…
      </section>
    );
  const running = !!details.analysis && active.has(details.analysis.status);
  const visiblePeople = people.filter((person) =>
    person.display_name.toLowerCase().includes(personQuery.toLowerCase()),
  );
  return (
    <section className="pv-panel p-4 sm:p-5 space-y-4 text-sm" style={{ background: "#0c0c0f" }}>
      <div
        className="flex flex-wrap items-center justify-between gap-3 pb-3"
        style={{ borderBottom: "1px solid var(--pv-border)" }}
      >
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-medium text-sm" style={{ color: "var(--pv-silver)" }}>
            Video Intelligence
          </h2>
          <ActionProgress
            state={
              (details.analysis
                ? active.has(details.analysis.status)
                  ? details.analysis.status === "queued"
                    ? "queued"
                    : "running"
                  : details.analysis.status === "failed"
                    ? "failed"
                    : details.analysis.status === "completed_with_warnings"
                      ? "completed_with_warnings"
                      : "completed"
                : "idle") as ActionProgressState
            }
            label={
              !details.analysis ? "Not analysed" : (labels[details.analysis.status] ?? "Analysis")
            }
            current={
              details.analysis?.status === "analysing"
                ? details.analysis.frames_completed
                : undefined
            }
            total={
              details.analysis?.status === "analysing" ? details.analysis.total_frames : undefined
            }
            emphasis="badge"
            showProgressBar={false}
          />
        </div>
        <button
          type="button"
          className="rounded-md px-3 py-1.5 text-xs"
          style={{
            color: "var(--pv-gold)",
            border: "1px solid var(--pv-gold-dim)",
            background: "rgba(201,169,97,0.06)",
          }}
          disabled={busy || running}
          onClick={() => void queue(!!details.analysis)}
        >
          {busy
            ? "Queueing…"
            : details.analysis
              ? details.analysis.status === "failed"
                ? "Retry"
                : "Reanalyse"
              : "Analyse video"}
        </button>
      </div>
      {!details.analysis && (
        <p className="-mt-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Video Intelligence has not analysed this video yet.
        </p>
      )}
      {details.analysis?.status === "completed_with_warnings" && (
        <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Analysis completed with some missing information.
        </p>
      )}
      {details.analysis?.status === "failed" && (
        <p className="text-xs text-red-300">Analysis failed. Video playback remains available.</p>
      )}
      <Block title="Description">
        <div className="flex items-start justify-between gap-3">
          <p className="whitespace-pre-wrap leading-6" style={{ color: "var(--pv-silver)" }}>
            {details.narrative ?? "No description generated."}
          </p>
          <button
            type="button"
            className="text-xs shrink-0 rounded-md px-2 py-1"
            style={{ color: "var(--pv-gold)", border: "1px solid var(--pv-border)" }}
            onClick={() => setEditing(true)}
          >
            <Pencil className="mr-1 inline size-3" />
            Edit
          </button>
        </div>
        {details.narrative_source === "user" && (
          <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Your description remains authoritative after reanalysis.
          </p>
        )}
        {editing && (
          <div className="space-y-2">
            <textarea
              className="pv-input min-h-24 w-full"
              aria-label="Video description"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
            <div className="flex gap-3">
              <button
                type="button"
                className="rounded-md px-3 py-2 text-xs"
                disabled={busy}
                onClick={() => {
                  void patch(`/api/personal-videos/intelligence/${details.asset_id}/narrative`, {
                    narrative: draft || null,
                  });
                  setEditing(false);
                }}
              >
                Save
              </button>
              <button type="button" className="text-xs" onClick={() => setEditing(false)}>
                Cancel
              </button>
              {details.narrative_source === "user" && (
                <button
                  type="button"
                  className="text-xs"
                  style={{ color: "var(--pv-gold)" }}
                  onClick={() =>
                    void patch(`/api/personal-videos/intelligence/${details.asset_id}/narrative`, {
                      narrative: null,
                    })
                  }
                >
                  Reset to Vault Master
                </button>
              )}
            </div>
          </div>
        )}
      </Block>
      <div className="grid gap-4 md:grid-cols-2">
        <Block title="People">
          <Chips
            values={details.people}
            empty="No people identified."
            onRemove={(id) =>
              void patch(`/api/personal-videos/intelligence/${details.asset_id}/people`, {
                person_id: id,
                decision: "exclude",
              })
            }
          />
          {!showPeoplePicker ? (
            <button
              type="button"
              className="inline-flex items-center gap-1 text-xs"
              style={{ color: "var(--pv-gold)" }}
              onClick={() => setShowPeoplePicker(true)}
            >
              <Plus className="size-3" /> Add person
            </button>
          ) : (
            <div
              className="mt-2 rounded-md p-3 flex flex-wrap gap-2"
              style={{
                background: "rgba(255,255,255,0.025)",
                border: "1px solid var(--pv-border)",
              }}
            >
              <input
                className="pv-input !w-40 !py-2 text-xs"
                placeholder="Search People"
                value={personQuery}
                onChange={(event) => setPersonQuery(event.target.value)}
              />
              <select
                className="pv-input !w-auto !py-2 text-xs"
                aria-label="Add person to video"
                value={personId}
                onChange={(event) => setPersonId(event.target.value)}
              >
                <option value="">Add person…</option>
                {visiblePeople.map((person) => (
                  <option key={person.id} value={person.id}>
                    {person.display_name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="rounded-md px-3 py-2 text-xs"
                disabled={busy || !personId}
                onClick={() => {
                  void patch(`/api/personal-videos/intelligence/${details.asset_id}/people`, {
                    person_id: personId,
                    decision: "include",
                  });
                  setPersonId("");
                  setShowPeoplePicker(false);
                }}
              >
                Add person to video
              </button>
              <button
                type="button"
                className="text-xs"
                style={{ color: "var(--pv-text-dim)" }}
                onClick={() => setShowPeoplePicker(false)}
              >
                Cancel
              </button>
            </div>
          )}
        </Block>
        <Block title="Content tags">
          <Chips
            values={details.content_tags}
            empty="No content tags assigned."
            onRemove={(slug) =>
              void patch(`/api/personal-videos/intelligence/${details.asset_id}/tags`, {
                namespace: "content_tag",
                slug,
                decision: "exclude",
              })
            }
          />
          {!showTagPicker ? (
            <button
              type="button"
              className="inline-flex items-center gap-1 text-xs"
              style={{ color: "var(--pv-gold)" }}
              onClick={() => setShowTagPicker(true)}
            >
              <Plus className="size-3" /> Add tag
            </button>
          ) : (
            <div
              className="mt-2 rounded-md p-3 flex flex-wrap gap-2"
              style={{
                background: "rgba(255,255,255,0.025)",
                border: "1px solid var(--pv-border)",
              }}
            >
              <select
                className="pv-input !w-auto !py-2 text-xs"
                aria-label="Add content tag"
                value={tagId}
                onChange={(event) => setTagId(event.target.value)}
              >
                <option value="">Add content tag…</option>
                {terms
                  .filter((term) => !details.content_tags.some((value) => value.slug === term.slug))
                  .map((term) => (
                    <option key={term.slug} value={term.slug}>
                      {term.display_name}
                    </option>
                  ))}
              </select>
              <button
                type="button"
                className="rounded-md px-3 py-2 text-xs"
                disabled={busy || !tagId}
                onClick={() => {
                  void patch(`/api/personal-videos/intelligence/${details.asset_id}/tags`, {
                    namespace: "content_tag",
                    slug: tagId,
                    decision: "include",
                  });
                  setTagId("");
                  setShowTagPicker(false);
                }}
              >
                Add tag
              </button>
              <button
                type="button"
                className="text-xs"
                style={{ color: "var(--pv-text-dim)" }}
                onClick={() => setShowTagPicker(false)}
              >
                Cancel
              </button>
            </div>
          )}
        </Block>
      </div>
      {!!details.warnings.length && (
        <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Analysis completed with some missing information.
        </p>
      )}
      {message && <p className="text-xs text-red-300">{message}</p>}
    </section>
  );
}
function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-2 pt-3" style={{ borderTop: "1px solid var(--pv-border)" }}>
      <h3 className="font-medium text-sm" style={{ color: "var(--pv-silver)" }}>
        {title}
      </h3>
      {children}
    </div>
  );
}
function Chips({
  values,
  onRemove,
  empty,
}: {
  values: Array<{ id?: string; slug?: string; display_name: string }>;
  onRemove: (value: string) => void;
  empty: string;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {values.map((value) => {
        const identity = value.id ?? value.slug ?? "";
        return (
          <span
            key={identity}
            className="inline-flex items-center rounded-full py-1 pr-1 text-xs"
            style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
          >
            <span className="px-2">{value.display_name}</span>
            <button
              type="button"
              className="px-1 text-sm leading-none hover:text-red-300"
              aria-label={`Remove ${value.display_name}`}
              onClick={() => onRemove(identity)}
            >
              ×
            </button>
          </span>
        );
      })}
      {!values.length && (
        <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          {empty}
        </span>
      )}
    </div>
  );
}
