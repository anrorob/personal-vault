import { HomeVideoMove } from "@/components/pv/HomeVideoMove";
import { HomeVideoCaptureDate, HomeVideoPrivateTags } from "@/components/pv/HomeVideoAnnotations";
import { HomeVideoControls } from "@/components/pv/HomeVideoControls";
import { homeVideoSearch, initialHomeVideoQuery } from "@/lib/home-video-controls";
import { authorizeHiddenVideos } from "@/lib/passkeys";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Clapperboard, Pencil, Play, Plus } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { HomeVideoCardMetadata } from "@/components/pv/HomeVideoCardMetadata";
import { getFileTitle, type VaultLibraryFile } from "@/lib/vault-libraries";
import { ActionProgress, type ActionProgressState } from "@/components/pv/ActionProgress";
import { KenAnalysis } from "@/components/pv/KenAnalysis";
import { useHomeVideoDetails } from "@/lib/use-home-video-details";

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
  file_id: string;
  asset_id: string;
  name: string;
  display_title: string | null;
  analysis: VideoJob | null;
  narrative: string | null;
  narrative_source: "user" | "ken_adjusted" | "ken_generated" | "vault_master" | "none";
  people: Person[];
  content_tags: Term[];
  captured_on: string | null;
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

function VideoThumbnail({ url }: { url?: string | null }) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  if (!url || failed) return null;
  return (
    <img
      src={url}
      alt=""
      loading="lazy"
      className={`absolute inset-0 h-full w-full object-cover ${loaded ? "" : "opacity-0"}`}
      onLoad={() => setLoaded(true)}
      onError={() => setFailed(true)}
    />
  );
}

type Playback = {
  status: "direct" | "ready" | "preparing" | "failed";
  playback_url: string | null;
};

function HomeVideoPlayer({ fileId }: { fileId: string }) {
  const [playback, setPlayback] = useState<Playback | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [mediaError, setMediaError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async (retry: boolean) => {
      try {
        const response = await fetch(
          `/api/personal-videos/${fileId}/playback${retry ? "?retry=true" : ""}`,
          { credentials: "include", signal: controller.signal },
        );
        if (!response.ok) throw new Error("Playback unavailable");
        const result = (await response.json()) as Playback;
        if (controller.signal.aborted) return;
        setPlayback(result);
        if (result.status === "preparing") timer = setTimeout(() => void poll(false), 1500);
      } catch {
        if (!controller.signal.aborted) setPlayback({ status: "failed", playback_url: null });
      }
    };
    void poll(attempt > 0);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [fileId, attempt]);
  if (mediaError || playback?.status === "failed") {
    return (
      <ActionProgress
        state="failed"
        label={
          mediaError
            ? "This recording could not be played by the browser."
            : "Playback could not be prepared for this recording."
        }
        onRetry={() => {
          setPlayback(null);
          setMediaError(false);
          setAttempt((value) => value + 1);
        }}
      />
    );
  }
  if (!playback?.playback_url) {
    return <ActionProgress state="running" label="Preparing playback..." />;
  }
  return (
    <video
      src={playback.playback_url}
      className="h-full w-full"
      controls
      autoPlay
      playsInline
      onError={() => setMediaError(true)}
    />
  );
}

function PersonalVideosPage() {
  const navigate = useNavigate();
  const [query, setQuery] = useState(initialHomeVideoQuery);
  const [selecting, setSelecting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const setSelectionMode = (value: boolean) => {
    const { scrollX, scrollY } = window;
    setSelecting(value);
    window.requestAnimationFrame(() =>
      window.scrollTo({ left: scrollX, top: scrollY, behavior: "instant" }),
    );
  };
  const [videos, setVideos] = useState<VaultLibraryFile[] | null>(null);
  const [selected, setSelected] = useState<VaultLibraryFile | null>(null);
  const { details, reload } = useHomeVideoDetails<VideoDetails>(selected?.id ?? null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void fetch(`/api/personal-videos?${homeVideoSearch(query)}`, {
      credentials: "include",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 401) {
          setVideos(null);
          setSelected(null);
          return navigate({ to: "/login" });
        }
        if (response.status === 403) {
          setVideos([]);
          setSelected(null);
        }
        if (!response.ok) throw new Error();
        const rows = (await response.json()) as VaultLibraryFile[];
        if (controller.signal.aborted) return;
        setVideos(rows);
        setSelectedIds((ids) => ids.filter((id) => rows.some((row) => row.asset_id === id)));
        setError(null);
      })
      .catch((cause) => {
        if (!(cause instanceof DOMException && cause.name === "AbortError"))
          setError("Home Videos is currently unavailable.");
      });
    return () => controller.abort();
  }, [navigate, query, revision]);
  useEffect(() => {
    if (!details?.analysis || !active.has(details.analysis.status)) return;
    const timer = window.setTimeout(() => void reload(), 1500);
    return () => window.clearTimeout(timer);
  }, [details?.analysis, reload]);
  return (
    <div className="pv-layout-container max-w-7xl mx-auto space-y-6">
      <div>
        <h2 className="pv-content-title text-xl">Home Videos</h2>
        <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
          {videos === null
            ? "Opening your recordings..."
            : `${videos.length} ${videos.length === 1 ? "video" : "videos"}`}
        </p>
      </div>
      <HomeVideoControls
        query={query}
        onQuery={setQuery}
        selecting={selecting}
        selectedIds={selectedIds}
        onSelect={setSelectionMode}
        onClear={() => setSelectedIds([])}
        onError={setError}
        onHidden={async () => {
          if (!query.include_hidden) {
            try {
              await authorizeHiddenVideos();
            } catch (reason) {
              setError(
                reason instanceof Error ? reason.message : "Hidden Videos could not be unlocked.",
              );
              return;
            }
          }
          // Conceal the previous privacy scope and close its player synchronously.
          setSelected(null);
          setVideos(null);
          setSelectedIds([]);
          setSelectionMode(false);
          setError(null);
          setQuery((current) => ({ ...current, include_hidden: !current.include_hidden }));
        }}
        onHideComplete={(ids, all) => {
          setVideos(
            (rows) =>
              rows?.filter((video) => !video.asset_id || !ids.includes(video.asset_id)) ?? null,
          );
          setSelectedIds((current) => current.filter((id) => !ids.includes(id)));
          if (all) setSelectionMode(false);
        }}
      />
      {error && <div className="pv-panel p-6 text-sm text-center text-red-300">{error}</div>}
      {videos?.length === 0 && (
        <div className="pv-panel p-10 text-center">
          <Clapperboard className="mx-auto" style={{ color: "var(--pv-gold)" }} />
          <h3 className="text-sm font-semibold mt-4">
            {query.include_hidden
              ? "No hidden videos match this view"
              : "No videos match this view"}
          </h3>
        </div>
      )}
      {videos && videos.length > 0 && (
        <div className="pv-card-grid pv-card-grid--wide">
          {videos.map((video) => (
            <button
              key={video.id}
              type="button"
              className="pv-panel pv-panel-hover overflow-hidden text-left group"
              aria-pressed={
                selecting
                  ? Boolean(video.asset_id && selectedIds.includes(video.asset_id))
                  : undefined
              }
              aria-label={selecting ? `Select ${video.display_title || video.name}` : undefined}
              onClick={() => {
                if (selecting) {
                  if (video.asset_id)
                    setSelectedIds((ids) =>
                      ids.includes(video.asset_id!)
                        ? ids.filter((id) => id !== video.asset_id)
                        : [...ids, video.asset_id!],
                    );
                  return;
                }
                setSelected(video);
              }}
            >
              <span
                className="aspect-video flex items-center justify-center relative overflow-hidden"
                style={{ background: "#09090b", color: "var(--pv-gold)" }}
              >
                <VideoThumbnail
                  key={`${video.id}:${video.modified_at}`}
                  url={video.thumbnail_url}
                />
                <span
                  className="relative h-12 w-12 rounded-full flex items-center justify-center"
                  style={{
                    background: "rgba(201,169,97,0.12)",
                    border: "1px solid var(--pv-gold-dim)",
                  }}
                >
                  {selecting ? (
                    <span aria-hidden="true">
                      {video.asset_id && selectedIds.includes(video.asset_id) ? "✓" : "○"}
                    </span>
                  ) : (
                    <Play size={20} fill="currentColor" />
                  )}
                </span>
              </span>
              <HomeVideoCardMetadata {...video} />
            </button>
          ))}
        </div>
      )}
      <Dialog
        open={selected !== null}
        onOpenChange={(open) => {
          if (!open) {
            setSelected(null);
          }
        }}
      >
        <DialogContent
          className="pv-dialog max-w-5xl max-h-[92vh] overflow-y-auto p-0"
          onEscapeKeyDown={(event) => {
            // The local date editor handles Escape without closing the player.
            if (
              event.target instanceof Element &&
              event.target.closest("[data-home-video-date-editor]")
            )
              event.preventDefault();
          }}
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
              {selected && <HomeVideoPlayer key={selected.id} fileId={selected.id} />}
            </div>
            {selected?.can_edit ? (
              <VideoIntelligence
                key={details?.asset_id ?? "loading"}
                details={details}
                reload={reload}
                annotations={
                  details ? (
                    <>
                      <HomeVideoMove
                        assetId={details.asset_id}
                        onMoved={() => {
                          setSelected(null);
                          setRevision((value) => value + 1);
                        }}
                      />
                      <HomeVideoCaptureDate
                        assetId={details.asset_id}
                        capturedOn={details.captured_on}
                        onSaved={async () => {
                          setRevision((value) => value + 1);
                          await reload();
                        }}
                      />
                      <HomeVideoPrivateTags
                        assetId={details.asset_id}
                        onChange={() => setRevision((value) => value + 1)}
                      />
                    </>
                  ) : null
                }
                ken={
                  details ? (
                    <KenAnalysis
                      key={details.asset_id}
                      assetId={details.asset_id}
                      onMetadataChange={reload}
                      onTitleChange={(title) => {
                        setSelected((current) =>
                          current ? { ...current, display_title: title } : current,
                        );
                        setVideos(
                          (current) =>
                            current?.map((video) =>
                              video.id === selected?.id
                                ? { ...video, display_title: title }
                                : video,
                            ) ?? null,
                        );
                        void reload();
                      }}
                    />
                  ) : null
                }
              />
            ) : selected?.asset_id ? (
              <HomeVideoPrivateTags
                key={selected.asset_id}
                assetId={selected.asset_id}
                onChange={() => setRevision((value) => value + 1)}
              />
            ) : null}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function VideoIntelligence({
  details,
  reload,
  ken,
  annotations,
}: {
  details: VideoDetails | null;
  reload: () => Promise<void>;
  ken: ReactNode;
  annotations: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [people, setPeople] = useState<Person[]>([]);
  const [terms, setTerms] = useState<Term[]>([]);
  const [personQuery, setPersonQuery] = useState("");
  const [personId, setPersonId] = useState("");
  const [tagId, setTagId] = useState("");
  const [tagQuery, setTagQuery] = useState("");
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
      <h2 className="font-medium text-sm">Home Video details</h2>
      <details className="text-xs">
        <summary className="cursor-pointer">Legacy Video Intelligence (Florence)</summary>
        <div
          className="flex flex-wrap items-center justify-between gap-3 pb-3"
          style={{ borderBottom: "1px solid var(--pv-border)" }}
        >
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-medium text-sm" style={{ color: "var(--pv-silver)" }}>
              Florence Video Intelligence
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
      </details>
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
        {details.narrative_source.startsWith("ken_") && (
          <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            {details.narrative_source === "ken_adjusted"
              ? "User-adjusted KEN description"
              : "KEN description"}
          </p>
        )}
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
                  Use generated description
                </button>
              )}
            </div>
          </div>
        )}
      </Block>
      {ken}
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
              <input
                className="pv-input !w-40 !py-2 text-xs"
                aria-label="Search system content tags"
                placeholder="Search content tags"
                maxLength={64}
                value={tagQuery}
                onChange={(event) => {
                  setTagQuery(event.target.value);
                  setTagId("");
                }}
              />
              <select
                className="pv-input !w-auto !py-2 text-xs"
                aria-label="Add content tag"
                value={tagId}
                onChange={(event) => setTagId(event.target.value)}
              >
                <option value="">Add content tag…</option>
                {terms
                  .filter((term) =>
                    term.display_name.toLowerCase().includes(tagQuery.toLowerCase()),
                  )
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
        {annotations}
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
