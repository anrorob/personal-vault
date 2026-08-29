import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Expand,
  Info,
  Pencil,
  Share2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  formatPhotoDate,
  getPhotoTitle,
  parseGalleryFilter,
  parseGallerySortOrder,
  type GalleryIntelligenceTerm,
  type GalleryFaceDetection,
  type GalleryImageDetails,
  type GalleryLocalAnnotation,
  type GallerySortOrder,
} from "@/lib/gallery";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ActionProgress } from "@/components/pv/ActionProgress";

type GalleryIntelligenceJobStatus = {
  id: string;
  status: "queued" | "processing" | "completed" | "failed";
  error: string | null;
};

const galleryIntelligenceStatusLabel = (job: GalleryIntelligenceJobStatus) =>
  ({
    queued: "Queued",
    processing: "Analysing…",
    completed: "Completed",
    failed: "Failed",
  })[job.status];

const peopleAnalysisStatusLabel = (job: GalleryIntelligenceJobStatus) =>
  ({
    queued: "Queued",
    processing: "Analysing faces",
    completed: "Completed",
    failed: "Failed",
  })[job.status];

export const Route = createFileRoute("/app/gallery/$photoId")({
  validateSearch: (search: Record<string, unknown>) => ({
    sort: parseGallerySortOrder(search.sort),
    photo_type: parseGalleryFilter(search.photo_type),
    content_tag: parseGalleryFilter(search.content_tag),
    person: parseGalleryFilter(search.person),
  }),
  component: PhotoViewerPage,
});

function PhotoViewerPage() {
  const { photoId } = Route.useParams();
  const { sort, photo_type, content_tag, person } = Route.useSearch();
  const navigate = useNavigate();
  const [photo, setPhoto] = useState<GalleryImageDetails | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fullScreen, setFullScreen] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [faceIdentificationMode, setFaceIdentificationMode] = useState(false);
  const [selectedFaceId, setSelectedFaceId] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setPhoto(null);
    setError(null);

    const loadPhoto = async () => {
      try {
        const query = new URLSearchParams({ sort });
        photo_type.forEach((value) => query.append("photo_type", value));
        content_tag.forEach((value) => query.append("content_tag", value));
        person.forEach((value) => query.append("person", value));
        const response = await fetch(`/api/gallery/${photoId}?${query}`, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });

        if (response.status === 401) {
          await navigate({ to: "/login" });
          return;
        }

        if (response.status === 404) {
          setError("This photo is no longer available in the Gallery.");
          return;
        }

        if (!response.ok) {
          throw new Error("Photo request failed");
        }

        setPhoto((await response.json()) as GalleryImageDetails);
      } catch (requestError) {
        if (requestError instanceof DOMException && requestError.name === "AbortError") {
          return;
        }

        setError("This photo could not be opened.");
      }
    };

    void loadPhoto();
    return () => controller.abort();
  }, [content_tag, navigate, person, photoId, photo_type, sort]);

  useEffect(() => {
    if (!photo) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && fullScreen) {
        setFullScreen(false);
      } else if (event.key === "ArrowLeft" && photo.previous_id) {
        void navigate({
          to: "/app/gallery/$photoId",
          params: { photoId: photo.previous_id },
          search: { sort, photo_type, content_tag, person },
        });
      }

      if (event.key === "ArrowRight" && photo.next_id) {
        void navigate({
          to: "/app/gallery/$photoId",
          params: { photoId: photo.next_id },
          search: { sort, photo_type, content_tag, person },
        });
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [content_tag, fullScreen, navigate, person, photo, photo_type, sort]);

  useEffect(() => {
    if (!fullScreen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [fullScreen]);

  if (error) {
    return (
      <div className="max-w-6xl mx-auto space-y-6">
        <BackToGallery
          sort={sort}
          photoTypes={photo_type}
          contentTags={content_tag}
          people={person}
        />
        <div className="pv-panel p-10 text-sm text-center text-red-300">{error}</div>
      </div>
    );
  }

  if (!photo) {
    return (
      <div className="max-w-6xl mx-auto">
        <div className="pv-panel p-10 text-sm text-center" style={{ color: "var(--pv-text-dim)" }}>
          Opening the archive image...
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-7xl mx-auto space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <BackToGallery
          sort={sort}
          photoTypes={photo_type}
          contentTags={content_tag}
          people={person}
        />
        <button
          type="button"
          className="inline-flex items-center gap-2 rounded-md px-3 py-2 text-xs"
          style={{
            color: "var(--pv-silver)",
            border: "1px solid var(--pv-border)",
          }}
          onClick={() => setFullScreen(true)}
        >
          <Expand size={14} />
          Full screen
        </button>
      </div>

      <PhotoStage
        photo={photo}
        sort={sort}
        faceIdentification={{
          active: faceIdentificationMode,
          selectedFaceId,
          onSelect: setSelectedFaceId,
        }}
      />

      {fullScreen && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black"
          role="dialog"
          aria-modal="true"
          aria-label={`Full-screen view of ${photo.display_title ?? getPhotoTitle(photo.name)}`}
        >
          <img
            src={
              (photo.media_type ?? photo.mime_type) === "application/pdf"
                ? photo.thumbnail_url
                : photo.image_url
            }
            alt={photo.display_title ?? getPhotoTitle(photo.name)}
            className="h-full w-full object-contain"
          />
          <PhotoNavigation direction="previous" photoId={photo.previous_id} sort={sort} />
          <PhotoNavigation direction="next" photoId={photo.next_id} sort={sort} />
          <button
            type="button"
            className="absolute right-4 z-10 inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm"
            style={{
              top: "max(1rem, env(safe-area-inset-top))",
              color: "var(--pv-silver)",
              background: "rgba(8,8,10,0.88)",
              border: "1px solid var(--pv-border-strong)",
            }}
            onClick={() => setFullScreen(false)}
            aria-label="Exit full screen"
          >
            <X size={18} />
            Exit full screen
          </button>
        </div>
      )}

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="pv-content-title text-lg">
            {photo.date_source === "user_override"
              ? "Corrected date"
              : photo.date_source === "embedded"
                ? "Photo taken"
                : photo.date_source === "filename"
                  ? "Date from filename"
                  : photo.date_source === "file_modified"
                    ? "File date"
                    : "Capture date"}
            : {formatPhotoDate(photo.captured_on)}
          </h1>
          {photo.location && (
            <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
              {photo.location}
            </p>
          )}
          {photo.owner_display_name && (
            <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
              Shared by {photo.owner_display_name}
            </p>
          )}
        </div>
        <div className="flex items-center gap-4">
          <button
            type="button"
            className="inline-flex items-center gap-2 rounded-md px-3 py-2 text-xs"
            style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
            onClick={() => setShowDetails((current) => !current)}
            aria-expanded={showDetails}
          >
            <Info size={14} />
            {showDetails ? "Hide details" : "Details"}
          </button>
          <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Use the arrow keys or viewer controls to move between photos.
          </p>
        </div>
      </div>
      {showDetails && (
        <PhotoDetails
          photo={photo}
          onUpdated={setPhoto}
          sort={sort}
          faceIdentificationMode={faceIdentificationMode}
          setFaceIdentificationMode={setFaceIdentificationMode}
          selectedFaceId={selectedFaceId}
          setSelectedFaceId={setSelectedFaceId}
        />
      )}
    </div>
  );
}

function PhotoDetails({
  photo,
  onUpdated,
  sort,
  faceIdentificationMode,
  setFaceIdentificationMode,
  selectedFaceId,
  setSelectedFaceId,
}: {
  photo: GalleryImageDetails;
  onUpdated: (photo: GalleryImageDetails) => void;
  sort: GallerySortOrder;
  faceIdentificationMode: boolean;
  setFaceIdentificationMode: (value: boolean) => void;
  selectedFaceId: string | null;
  setSelectedFaceId: (value: string | null) => void;
}) {
  const navigate = useNavigate();
  const [editing, setEditing] = useState(false);
  const [editingAccess, setEditingAccess] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [accessSaving, setAccessSaving] = useState(false);
  const [accessError, setAccessError] = useState<string | null>(null);
  const [access, setAccess] = useState<AssetSharingState | null>(null);
  const [aiEvidence, setAiEvidence] = useState<AiEvidence | null>(null);
  const [descriptionLoading, setDescriptionLoading] = useState(false);
  const [descriptionError, setDescriptionError] = useState<string | null>(null);
  const [intelligenceJob, setIntelligenceJob] = useState<GalleryIntelligenceJobStatus | null>(null);
  const [intelligenceQueueing, setIntelligenceQueueing] = useState(false);
  const [intelligenceError, setIntelligenceError] = useState<string | null>(null);
  const refreshedIntelligenceJobId = useRef<string | null>(null);
  const [accessDraft, setAccessDraft] = useState<{
    mode: "private" | "everyone" | "specific";
    recipientUserIds: string[];
    shareMode: "quick" | "standard";
  }>({ mode: "private", recipientUserIds: [], shareMode: "quick" });
  const [draft, setDraft] = useState({
    display_title: photo.display_title ?? getPhotoTitle(photo.name),
    captured_on: photo.captured_on ?? "",
    location: photo.location ?? "",
  });
  const provenance = [
    ["Title", photo.metadata_provenance?.display_title],
    ["Date", photo.metadata_provenance?.captured_on],
    ["Location", photo.metadata_provenance?.location],
  ].filter((entry): entry is [string, string] => Boolean(entry[1]));

  useEffect(() => {
    if (!photo.can_edit || !photo.asset_id) return;
    const controller = new AbortController();
    fetch(`/api/vault-master/assets/${photo.asset_id}/sharing`, {
      credentials: "include",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((policy: AssetSharingState | null) => {
        if (!policy) return;
        setAccess(policy);
        setAccessDraft({
          mode: policy.mode,
          recipientUserIds: policy.recipients.map((recipient) => recipient.user_id),
          shareMode: "quick",
        });
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [photo.asset_id, photo.can_edit]);

  const refreshGalleryIntelligenceMetadata = useCallback(async () => {
    const response = await fetch(`/api/gallery/${photo.id}?sort=${sort}`, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("Gallery Intelligence metadata could not be refreshed");
    const refreshed = (await response.json()) as GalleryImageDetails;
    onUpdated({
      ...photo,
      intelligence: refreshed.intelligence,
      intelligence_provenance: refreshed.intelligence_provenance,
    });
  }, [onUpdated, photo, sort]);

  const fetchGalleryIntelligenceStatus = useCallback(async () => {
    if (!photo.asset_id) return null;
    const response = await fetch(`/api/gallery/intelligence/assets/${photo.asset_id}/status`, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("Gallery Intelligence status could not be loaded");
    const body = (await response.json()) as { job: GalleryIntelligenceJobStatus | null };
    return body.job;
  }, [photo.asset_id]);

  useEffect(() => {
    if (!photo.can_edit || !photo.asset_id) {
      setIntelligenceJob(null);
      return;
    }
    let stopped = false;
    let timer: number | undefined;
    const sync = async () => {
      try {
        const job = await fetchGalleryIntelligenceStatus();
        if (stopped) return;
        setIntelligenceError(null);
        setIntelligenceJob(job);
        if (job?.status === "completed" && refreshedIntelligenceJobId.current !== job.id) {
          await refreshGalleryIntelligenceMetadata();
          refreshedIntelligenceJobId.current = job.id;
        }
        if (job && (job.status === "queued" || job.status === "processing")) {
          timer = window.setTimeout(() => void sync(), 1_500);
        }
      } catch {
        if (!stopped) setIntelligenceError("Gallery Intelligence status could not be loaded.");
      }
    };
    void sync();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [
    fetchGalleryIntelligenceStatus,
    intelligenceJob?.id,
    intelligenceJob?.status,
    photo.asset_id,
    photo.can_edit,
    refreshGalleryIntelligenceMetadata,
  ]);

  const queueGalleryIntelligence = useCallback(async () => {
    if (!photo.asset_id) return;
    setIntelligenceQueueing(true);
    setIntelligenceError(null);
    try {
      const response = await fetch(`/api/gallery/intelligence/assets/${photo.asset_id}/reanalyse`, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Gallery Intelligence analysis could not be queued");
      }
      setIntelligenceJob((await response.json()) as GalleryIntelligenceJobStatus);
    } catch (error) {
      setIntelligenceError(
        error instanceof Error
          ? error.message
          : "Gallery Intelligence analysis could not be queued.",
      );
      throw error;
    } finally {
      setIntelligenceQueueing(false);
    }
  }, [photo.asset_id]);

  useEffect(() => {
    if (!photo.can_edit || !photo.asset_id) {
      setAiEvidence(null);
      setDescriptionError(null);
      setDescriptionLoading(false);
      return;
    }

    const controller = new AbortController();
    setDescriptionLoading(true);
    setDescriptionError(null);

    void fetch(`/api/vault-master/assets/${photo.asset_id}/ai`, {
      credentials: "include",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 401) {
          await navigate({ to: "/login" });
          return null;
        }
        if (!response.ok) throw new Error("Visual description could not be loaded");
        return (await response.json()) as AiEvidence;
      })
      .then((evidence) => {
        if (evidence) setAiEvidence(evidence);
      })
      .catch((requestError: unknown) => {
        if (requestError instanceof DOMException && requestError.name === "AbortError") return;
        setDescriptionError("The stored visual description could not be loaded.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setDescriptionLoading(false);
      });

    return () => controller.abort();
  }, [navigate, photo.asset_id, photo.can_edit]);

  function resetDraft() {
    setDraft({
      display_title: photo.display_title ?? getPhotoTitle(photo.name),
      captured_on: photo.captured_on ?? "",
      location: photo.location ?? "",
    });
  }

  async function startAccessEditing() {
    if (!photo.asset_id) return;
    setAccessError(null);
    setAccessSaving(true);
    try {
      const response = await fetch(`/api/vault-master/assets/${photo.asset_id}/sharing`, {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Access details could not be loaded");
      }
      const policy = (await response.json()) as AssetSharingState;
      setAccess(policy);
      setAccessDraft({
        mode: policy.mode,
        recipientUserIds: policy.recipients.map((recipient) => recipient.user_id),
        shareMode: "quick",
      });
      setEditing(false);
      setEditingAccess(true);
    } catch (requestError) {
      setAccessError(
        requestError instanceof Error
          ? requestError.message
          : "Access details could not be loaded.",
      );
    } finally {
      setAccessSaving(false);
    }
  }

  async function saveAccessChanges(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!photo.asset_id) return;
    if (accessDraft.mode === "specific" && !accessDraft.recipientUserIds.length) {
      setAccessError("Select at least one person before sharing this item.");
      return;
    }

    setAccessSaving(true);
    setAccessError(null);
    try {
      const response = await fetch(`/api/vault-master/assets/${photo.asset_id}/sharing`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          mode: accessDraft.mode,
          recipient_user_ids: accessDraft.recipientUserIds,
          share_mode: accessDraft.shareMode,
        }),
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Access update failed");
      }
      const policy = (await response.json()) as AssetSharingState;
      setAccess(policy);
      setAccessDraft({
        mode: policy.mode,
        recipientUserIds: policy.recipients.map((recipient) => recipient.user_id),
      });
      setEditingAccess(false);
    } catch (requestError) {
      setAccessError(
        requestError instanceof Error
          ? requestError.message
          : "The access policy could not be updated.",
      );
    } finally {
      setAccessSaving(false);
    }
  }

  async function saveChanges(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const current = {
      display_title: photo.display_title ?? getPhotoTitle(photo.name),
      captured_on: photo.captured_on ?? "",
      location: photo.location ?? "",
    };
    const changes: Record<string, string | null> = {};
    (Object.keys(draft) as Array<keyof typeof draft>).forEach((field) => {
      const value = draft[field].trim();
      if (value !== current[field]) changes[field] = value || null;
    });
    if (!Object.keys(changes).length) {
      setEditing(false);
      return;
    }

    setSaving(true);
    setEditError(null);
    try {
      const response = await fetch(`/api/vault-master/assets/${photo.asset_id}/metadata`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(changes),
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Metadata update failed");
      }
      const updated = (await response.json()) as {
        display_title: string;
        captured_on: string | null;
        location: string | null;
        metadata_provenance: Record<string, string>;
      };
      const updatedPhoto = {
        ...photo,
        display_title: updated.display_title,
        captured_on: updated.captured_on,
        location: updated.location,
        date_source: updated.metadata_provenance.captured_on ?? photo.date_source,
        metadata_provenance: updated.metadata_provenance,
      } as GalleryImageDetails;
      onUpdated(updatedPhoto);
      setDraft({
        display_title: updated.display_title,
        captured_on: updated.captured_on ?? "",
        location: updated.location ?? "",
      });
      setEditing(false);
    } catch (requestError) {
      setEditError(
        requestError instanceof Error
          ? requestError.message
          : "The permanent metadata could not be updated.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="pv-panel p-5 text-sm" aria-label="Photo details">
      <div className="grid gap-5 md:grid-cols-2">
        <div className="space-y-2">
          <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
            Permanent file
          </h2>
          <p className="break-all" style={{ color: "var(--pv-text-dim)" }}>
            {photo.vault_path}
          </p>
          <p style={{ color: "var(--pv-text-dim)" }}>
            {photo.name} · {formatBytes(photo.size)} · {photo.mime_type}
          </p>
          <p className="break-all font-mono text-[10px]" style={{ color: "var(--pv-text-dim)" }}>
            SHA-256 {photo.sha256}
          </p>
        </div>
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3">
            <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
              Metadata provenance
            </h2>
            {access && access.mode !== "private" && (
              <span className="text-xs" style={{ color: "var(--pv-gold)" }}>
                {access.mode === "everyone"
                  ? "Shared with everyone"
                  : `Shared with ${access.recipients.length} ${access.recipients.length === 1 ? "person" : "people"}`}
              </span>
            )}
            {photo.can_edit && !editingAccess && (
              <div className="flex flex-wrap justify-end gap-x-4 gap-y-2">
                {!editing && (
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 text-xs"
                    style={{ color: "var(--pv-gold)" }}
                    onClick={() => {
                      resetDraft();
                      setEditError(null);
                      setEditing(true);
                    }}
                  >
                    <Pencil size={13} />
                    Edit metadata
                  </button>
                )}
                <button
                  type="button"
                  className="inline-flex items-center gap-1 text-xs"
                  style={{ color: "var(--pv-gold)" }}
                  disabled={accessSaving}
                  onClick={startAccessEditing}
                >
                  <Share2 size={13} />
                  {accessSaving ? "Loading…" : "Manage sharing"}
                </button>
              </div>
            )}
          </div>
          {provenance.length ? (
            provenance.map(([label, source]) => (
              <p key={label} className="flex justify-between gap-4 text-xs">
                <span style={{ color: "var(--pv-text-dim)" }}>{label}</span>
                <span style={{ color: "var(--pv-silver)" }}>{source.replaceAll("_", " ")}</span>
              </p>
            ))
          ) : (
            <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              No provenance has been recorded.
            </p>
          )}
        </div>
      </div>
      {photo.description || photo.captured_at ? (
        <div className="mt-4 space-y-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
          {photo.captured_at ? <p>Captured: {photo.captured_at}</p> : null}
          {photo.description ? <p>{photo.description}</p> : null}
        </div>
      ) : null}
      <GalleryIntelligenceMetadata
        photo={photo}
        onUpdated={onUpdated}
        job={intelligenceJob}
        queueing={intelligenceQueueing}
        analysisError={intelligenceError}
        onRetry={() => void queueGalleryIntelligence().catch(() => undefined)}
      />
      {!photo.can_edit && photo.local_annotation && (
        <SharedPhotoAnnotations photo={photo} onUpdated={onUpdated} />
      )}
      {photo.can_edit && photo.asset_id && (
        <GalleryPeopleSection
          photo={photo}
          onUpdated={onUpdated}
          sort={sort}
          faceIdentificationMode={faceIdentificationMode}
          setFaceIdentificationMode={setFaceIdentificationMode}
          selectedFaceId={selectedFaceId}
          setSelectedFaceId={setSelectedFaceId}
        />
      )}
      {photo.can_edit && photo.asset_id && (
        <div className="mt-5 space-y-2 pt-5" style={{ borderTop: "1px solid var(--pv-border)" }}>
          <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
            Florence visual description
          </h2>
          {descriptionLoading && (
            <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              Loading visual description…
            </p>
          )}
          {descriptionError && <p className="text-xs text-red-300">{descriptionError}</p>}
          {!descriptionLoading && !descriptionError && aiEvidence && (
            <GalleryVisualDescription evidence={aiEvidence} />
          )}
        </div>
      )}
      {editing && (
        <form
          className="mt-5 space-y-4 pt-5"
          style={{ borderTop: "1px solid var(--pv-border)" }}
          onSubmit={saveChanges}
        >
          <div>
            <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
              Edit permanent descriptive metadata
            </h2>
            <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
              The file itself is unchanged. This correction is saved by Vault Master and recorded in
              its history.
            </p>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            <MetadataInput
              label="Display title"
              value={draft.display_title}
              onChange={(value) => setDraft((current) => ({ ...current, display_title: value }))}
            />
            <MetadataInput
              label="Capture date"
              type="date"
              value={draft.captured_on}
              onChange={(value) => setDraft((current) => ({ ...current, captured_on: value }))}
            />
            <MetadataInput
              label="Location"
              value={draft.location}
              placeholder="Not recorded"
              onChange={(value) => setDraft((current) => ({ ...current, location: value }))}
            />
          </div>
          {editError && (
            <p className="text-xs" style={{ color: "#fca5a5" }}>
              {editError}
            </p>
          )}
          <div className="flex flex-wrap justify-end gap-2">
            <button
              type="button"
              className="rounded-md px-3 py-2 text-xs"
              style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
              disabled={saving}
              onClick={() => {
                resetDraft();
                setEditError(null);
                setEditing(false);
              }}
            >
              Cancel
            </button>
            <button type="submit" className="pv-btn-primary px-3 py-2 text-xs" disabled={saving}>
              {saving ? "Saving…" : "Save correction"}
            </button>
          </div>
        </form>
      )}
      {editingAccess && access && (
        <form
          className="mt-5 space-y-4 pt-5"
          style={{ borderTop: "1px solid var(--pv-border)" }}
          onSubmit={saveAccessChanges}
        >
          <div>
            <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
              Sharing
            </h2>
            <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
              This item remains owned by {access.owner_username}. Sharing never moves or copies its
              file.
            </p>
          </div>
          <fieldset className="space-y-3">
            <legend className="text-xs" style={{ color: "var(--pv-silver)" }}>
              Share this item
            </legend>
            {(
              [
                ["private", "Private", "Only you can see this item."],
                [
                  "everyone",
                  "Share in my Vault — Everyone",
                  "Everyone in this Vault can see this item.",
                ],
                [
                  "specific",
                  "Share in my Vault — Specific people",
                  "Choose the people who can see this item.",
                ],
              ] as const
            ).map(([mode, label, description]) => (
              <label
                key={mode}
                className="flex cursor-pointer gap-3 rounded-md p-3"
                style={{ border: "1px solid var(--pv-border)" }}
              >
                <input
                  type="radio"
                  name="asset-sharing-mode"
                  value={mode}
                  checked={accessDraft.mode === mode}
                  onChange={() => setAccessDraft((current) => ({ ...current, mode }))}
                />
                <span>
                  <span className="block text-xs" style={{ color: "var(--pv-silver)" }}>
                    {label}
                  </span>
                  <span className="block text-xs" style={{ color: "var(--pv-text-dim)" }}>
                    {description}
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
          {accessDraft.mode === "specific" && (
            <div className="space-y-2">
              <p className="text-xs" style={{ color: "var(--pv-silver)" }}>
                Specific people
              </p>
              {access.eligible_users.map((person) => {
                const selected = accessDraft.recipientUserIds.includes(person.user_id);
                return (
                  <label
                    key={person.user_id}
                    className="flex cursor-pointer items-center gap-3 rounded-md px-3 py-2"
                    style={{ border: "1px solid var(--pv-border)" }}
                  >
                    <input
                      type="checkbox"
                      checked={selected}
                      onChange={() =>
                        setAccessDraft((current) => ({
                          ...current,
                          recipientUserIds: selected
                            ? current.recipientUserIds.filter((id) => id !== person.user_id)
                            : [...current.recipientUserIds, person.user_id],
                        }))
                      }
                    />
                    <span
                      className="flex h-7 w-7 items-center justify-center rounded-full text-[10px]"
                      style={{ background: "var(--pv-border)", color: "var(--pv-silver)" }}
                    >
                      {person.avatar_label}
                    </span>
                    <span className="text-xs" style={{ color: "var(--pv-silver)" }}>
                      {person.display_name}
                    </span>
                  </label>
                );
              })}
            </div>
          )}
          {accessDraft.mode !== "private" && (
            <label className="block text-xs" style={{ color: "var(--pv-silver)" }}>
              Release
              <select
                className="ml-3 rounded-md px-2 py-1"
                value={accessDraft.shareMode}
                onChange={(event) =>
                  setAccessDraft((current) => ({
                    ...current,
                    shareMode: event.target.value as "quick" | "standard",
                  }))
                }
              >
                <option value="quick">Quick Share — available now</option>
                <option value="standard">Standard Share — review for 3 minutes</option>
              </select>
            </label>
          )}
          {accessError && (
            <p className="text-xs" style={{ color: "#fca5a5" }}>
              {accessError}
            </p>
          )}
          <div className="flex flex-wrap justify-end gap-2">
            <button
              type="button"
              className="rounded-md px-3 py-2 text-xs"
              style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
              disabled={accessSaving}
              onClick={() => {
                setAccessError(null);
                setEditingAccess(false);
              }}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="pv-btn-primary px-3 py-2 text-xs"
              disabled={accessSaving}
            >
              {accessSaving ? "Saving…" : "Save sharing"}
            </button>
          </div>
        </form>
      )}
      {photo.can_edit && photo.asset_id && (
        <GalleryOptions
          assetId={photo.asset_id}
          lifecycleState={photo.lifecycle_state ?? "active"}
          sort={sort}
          job={intelligenceJob}
          queueing={intelligenceQueueing}
          intelligence={photo.intelligence}
          onAnalysePhoto={queueGalleryIntelligence}
        />
      )}
    </section>
  );
}

function SharedPhotoAnnotations({
  photo,
  onUpdated,
}: {
  photo: GalleryImageDetails;
  onUpdated: (photo: GalleryImageDetails) => void;
}) {
  const [note, setNote] = useState(photo.local_annotation?.note ?? "");
  const [tags, setTags] = useState((photo.local_annotation?.tags ?? []).join(", "));
  const [personIds, setPersonIds] = useState<string[]>(
    (photo.local_annotation?.people ?? []).map((person) => person.id),
  );
  const [people, setPeople] = useState<
    Array<{ id: string; display_name: string; active: boolean }>
  >([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void fetch("/api/gallery/people", { credentials: "include" })
      .then((response) => (response.ok ? response.json() : []))
      .then((value) =>
        setPeople(value as Array<{ id: string; display_name: string; active: boolean }>),
      );
  }, []);

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`/api/gallery/${photo.id}/local-annotation`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          note,
          tags: tags
            .split(",")
            .map((tag) => tag.trim())
            .filter(Boolean),
          person_ids: personIds,
        }),
      });
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Personal annotations could not be saved");
      }
      onUpdated({ ...photo, local_annotation: (await response.json()) as GalleryLocalAnnotation });
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Personal annotations could not be saved.",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="mt-5 space-y-3 pt-5" style={{ borderTop: "1px solid var(--pv-border)" }}>
      <div>
        <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
          Shared photo
        </h2>
        <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Origin metadata is read-only. Your notes, tags, and People stay in your Vault view.
        </p>
      </div>
      {photo.origin_people?.length ? (
        <div className="flex flex-wrap gap-2">
          <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            People named by the owner:
          </span>
          {photo.origin_people.map((person) => (
            <span
              key={person.id}
              className="rounded-full px-2 py-1 text-xs"
              style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
            >
              {person.display_name}
            </span>
          ))}
        </div>
      ) : null}
      <form className="space-y-3" onSubmit={(event) => void save(event)}>
        <label className="block text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Personal note
          <textarea
            className="pv-input mt-1 min-h-20 w-full"
            value={note}
            maxLength={2000}
            onChange={(event) => setNote(event.target.value)}
          />
        </label>
        <label className="block text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Personal tags
          <input
            className="pv-input mt-1 w-full"
            value={tags}
            onChange={(event) => setTags(event.target.value)}
            placeholder="Separate tags with commas"
          />
        </label>
        <label className="block text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Your People
          <select
            className="pv-input mt-1 min-h-20 w-full"
            multiple
            value={personIds}
            onChange={(event) =>
              setPersonIds(
                Array.from(event.currentTarget.selectedOptions, (option) => option.value),
              )
            }
          >
            {people
              .filter((person) => person.active)
              .map((person) => (
                <option key={person.id} value={person.id}>
                  {person.display_name}
                </option>
              ))}
          </select>
        </label>
        {error ? <p className="text-xs text-red-300">{error}</p> : null}
        <button type="submit" className="pv-btn-secondary text-xs" disabled={saving}>
          {saving ? "Saving…" : "Save personal annotations"}
        </button>
      </form>
    </section>
  );
}

function GalleryPeopleSection({
  photo,
  onUpdated,
  sort,
  faceIdentificationMode,
  setFaceIdentificationMode,
  selectedFaceId,
  setSelectedFaceId,
}: {
  photo: GalleryImageDetails;
  onUpdated: (photo: GalleryImageDetails) => void;
  sort: GallerySortOrder;
  faceIdentificationMode: boolean;
  setFaceIdentificationMode: (value: boolean) => void;
  selectedFaceId: string | null;
  setSelectedFaceId: (value: string | null) => void;
}) {
  const [allPeople, setAllPeople] = useState<
    Array<{ id: string; display_name: string; active: boolean }>
  >([]);
  const [photoPersonId, setPhotoPersonId] = useState("");
  const [facePersonId, setFacePersonId] = useState("");
  const [photoPersonName, setPhotoPersonName] = useState("");
  const [facePersonName, setFacePersonName] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<GalleryIntelligenceJobStatus | null>(null);
  const refreshedPeopleJobId = useRef<string | null>(null);
  const peopleAnalysisRequestInFlight = useRef(false);
  const peopleAnalysisActive = status?.status === "queued" || status?.status === "processing";
  const refresh = useCallback(async () => {
    const response = await fetch(`/api/gallery/${photo.id}?sort=${sort}`, {
      credentials: "include",
    });
    if (!response.ok) throw new Error();
    const fresh = (await response.json()) as GalleryImageDetails;
    onUpdated({
      ...photo,
      people: fresh.people,
      unknown_people_count: fresh.unknown_people_count,
      unresolved_person_presence: fresh.unresolved_person_presence,
      face_detections: fresh.face_detections,
    });
  }, [onUpdated, photo, sort]);
  useEffect(() => {
    void fetch("/api/gallery/people", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then(setAllPeople);
  }, []);
  useEffect(() => {
    if (!photo.asset_id) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      const response = await fetch(`/api/gallery/people/assets/${photo.asset_id}/status`, {
        credentials: "include",
      });
      if (!response.ok || cancelled) return;
      const job = ((await response.json()) as { job: GalleryIntelligenceJobStatus | null }).job;
      setStatus(job);
      if (job?.status === "completed" && refreshedPeopleJobId.current !== job.id) {
        refreshedPeopleJobId.current = job.id;
        await refresh();
        setSelectedFaceId(null);
      }
      if (!cancelled && (job?.status === "queued" || job?.status === "processing"))
        timer = window.setTimeout(poll, 1500);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [photo.asset_id, refresh, setSelectedFaceId]);
  const assign = async (personId: string) => {
    if (!photo.asset_id || !personId) return;
    setBusy(true);
    try {
      const response = await fetch(`/api/gallery/people/assets/${photo.asset_id}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ person_id: personId, decision: "include" }),
      });
      if (!response.ok) throw new Error();
      await refresh();
      setPhotoPersonId("");
      setSelectedFaceId(null);
    } finally {
      setBusy(false);
    }
  };
  const remove = async (personId: string) => {
    if (!photo.asset_id) return;
    setBusy(true);
    try {
      await fetch(`/api/gallery/people/assets/${photo.asset_id}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ person_id: personId, decision: "exclude" }),
      });
      await refresh();
    } finally {
      setBusy(false);
    }
  };
  const rename = async (person: { id: string; display_name: string }) => {
    const displayName = window.prompt("Rename Person", person.display_name)?.trim();
    if (!displayName || displayName === person.display_name) return;
    setBusy(true);
    try {
      const response = await fetch(`/api/gallery/people/${person.id}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: displayName }),
      });
      if (!response.ok) throw new Error();
      const updated = (await response.json()) as {
        id: string;
        display_name: string;
        active: boolean;
      };
      setAllPeople((current) =>
        current.map((entry) => (entry.id === updated.id ? updated : entry)),
      );
      await refresh();
    } finally {
      setBusy(false);
    }
  };
  const createPerson = async (displayName: string) => {
    if (!displayName.trim()) return null;
    setBusy(true);
    try {
      const response = await fetch("/api/gallery/people", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: displayName.trim() }),
      });
      if (!response.ok) throw new Error();
      const person = (await response.json()) as {
        id: string;
        display_name: string;
        active: boolean;
      };
      setAllPeople((current) =>
        [...current, person].sort((a, b) => a.display_name.localeCompare(b.display_name)),
      );
      return person;
    } finally {
      setBusy(false);
    }
  };
  const identifyFace = async (personId: string, faceId = selectedFaceId) => {
    if (!photo.asset_id || !personId || !faceId) return;
    setBusy(true);
    try {
      const response = await fetch(
        `/api/gallery/people/assets/${photo.asset_id}/faces/${faceId}/identify`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ person_id: personId, decision: "include" }),
        },
      );
      if (!response.ok) throw new Error();
      await refresh();
      setSelectedFaceId(null);
      setFacePersonId("");
    } finally {
      setBusy(false);
    }
  };
  const clearFaceIdentity = async (faceId: string) => {
    if (!photo.asset_id) return;
    setBusy(true);
    try {
      const response = await fetch(
        `/api/gallery/people/assets/${photo.asset_id}/faces/${faceId}/identity`,
        {
          method: "DELETE",
          credentials: "include",
        },
      );
      if (!response.ok) throw new Error();
      await refresh();
    } finally {
      setBusy(false);
    }
  };
  const analyse = async () => {
    if (!photo.asset_id || peopleAnalysisActive || peopleAnalysisRequestInFlight.current) return;
    peopleAnalysisRequestInFlight.current = true;
    setBusy(true);
    try {
      const response = await fetch(`/api/gallery/people/assets/${photo.asset_id}/analyse`, {
        method: "POST",
        credentials: "include",
      });
      if (!response.ok) throw new Error();
      const job = (await response.json()) as GalleryIntelligenceJobStatus;
      refreshedPeopleJobId.current = null;
      setStatus(job);
    } catch {
      setStatus({ id: "", status: "failed", error: "People analysis could not be queued." });
    } finally {
      peopleAnalysisRequestInFlight.current = false;
      setBusy(false);
    }
  };
  return (
    <section className="mt-5 space-y-3 pt-5" style={{ borderTop: "1px solid var(--pv-border)" }}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
          People
        </h2>
        <button
          type="button"
          className="text-xs"
          style={{ color: "var(--pv-gold)" }}
          disabled={busy || peopleAnalysisActive}
          onClick={() => void analyse()}
        >
          Analyse people
        </button>
        {photo.face_detections?.length ? (
          <button
            type="button"
            className="text-xs"
            style={{ color: "var(--pv-gold)" }}
            disabled={busy}
            onClick={() => {
              setFaceIdentificationMode(!faceIdentificationMode);
              if (faceIdentificationMode) setSelectedFaceId(null);
            }}
          >
            {faceIdentificationMode ? "Close face identification" : "Identify a face"}
          </button>
        ) : null}
      </div>
      <div className="flex flex-wrap gap-2">
        {photo.people?.map((person) => (
          <span
            key={person.id}
            className="inline-flex items-center rounded-full py-1 pr-1 text-xs"
            style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
          >
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="rounded-full px-2 text-left hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-[var(--pv-gold)]"
                  aria-label={`Actions for ${person.display_name}`}
                  disabled={busy}
                >
                  {person.display_name}
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                <DropdownMenuItem disabled={busy} onSelect={() => void rename(person)}>
                  Rename
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <button
              type="button"
              aria-label={`Remove ${person.display_name}`}
              title="Remove from photo"
              className="rounded-full px-1 text-sm leading-none hover:text-red-300 focus:outline-none focus-visible:ring-1 focus-visible:ring-[var(--pv-gold)]"
              disabled={busy}
              onClick={() => void remove(person.id)}
            >
              ×
            </button>
          </span>
        ))}{" "}
        {!photo.people?.length && (
          <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            No People are associated yet.
          </span>
        )}
      </div>
      {photo.unknown_people_count ? (
        <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Unknown person
          {photo.unknown_people_count > 1 ? ` · ${photo.unknown_people_count} faces` : ""} — select
          it in face-identification mode to identify that specific face.
        </p>
      ) : null}
      {photo.unresolved_person_presence && !photo.unknown_people_count ? (
        <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Unidentified person present — use Add person to photo to confirm who is present.
        </p>
      ) : null}
      {faceIdentificationMode ? (
        <div className="space-y-2 rounded-md p-3" style={{ border: "1px solid var(--pv-border)" }}>
          <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Select a face to highlight its stored MediaPipe box. Face identity is separate from
            photo-level presence.
          </p>
          <div className="flex flex-wrap gap-2">
            {photo.face_detections?.map((face, index) => (
              <button
                key={face.id}
                type="button"
                className="rounded-md px-2 py-1 text-xs"
                style={{
                  color: face.id === selectedFaceId ? "var(--pv-gold)" : "var(--pv-silver)",
                  border: "1px solid var(--pv-border)",
                }}
                onClick={() => setSelectedFaceId(face.id)}
              >
                {face.person_name ?? `Face ${index + 1}`}
              </button>
            ))}
          </div>
          {selectedFaceId
            ? (() => {
                const selectedFace = photo.face_detections?.find(
                  (face) => face.id === selectedFaceId,
                );
                const selectedFaceIndex =
                  photo.face_detections?.findIndex((face) => face.id === selectedFaceId) ?? -1;
                const selectedFaceLabel =
                  selectedFace?.person_name ?? `Face ${selectedFaceIndex + 1}`;
                return (
                  <div
                    className="space-y-2 rounded-md p-3"
                    style={{ background: "rgba(215,185,104,0.06)" }}
                  >
                    <p className="text-sm" style={{ color: "var(--pv-silver)" }}>
                      Selected face: {selectedFaceLabel}
                    </p>
                    <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                      Identify selected face as...
                    </p>
                    <div className="flex flex-wrap items-center gap-2">
                      <select
                        aria-label="Identify selected face as"
                        className="pv-input !w-auto !py-2 text-xs"
                        value={facePersonId}
                        onChange={(event) => setFacePersonId(event.target.value)}
                      >
                        <option value="">Choose Person…</option>
                        {allPeople
                          .filter((person) => person.active)
                          .map((person) => (
                            <option key={person.id} value={person.id}>
                              {person.display_name}
                            </option>
                          ))}
                      </select>
                      <button
                        type="button"
                        className="rounded-md px-3 py-2 text-xs"
                        disabled={!facePersonId || busy}
                        onClick={() => void identifyFace(facePersonId)}
                      >
                        Identify face
                      </button>
                      {selectedFace?.user_confirmed ? (
                        <button
                          type="button"
                          className="text-xs"
                          style={{ color: "var(--pv-gold)" }}
                          disabled={busy}
                          onClick={() => void clearFaceIdentity(selectedFaceId)}
                        >
                          Remove face identity
                        </button>
                      ) : null}
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        className="pv-input !w-52 !py-2 text-xs"
                        value={facePersonName}
                        onChange={(event) => setFacePersonName(event.target.value)}
                        placeholder="Create Person for this face"
                      />
                      <button
                        type="button"
                        className="rounded-md px-3 py-2 text-xs"
                        disabled={!facePersonName.trim() || busy}
                        onClick={() =>
                          void createPerson(facePersonName).then((person) => {
                            if (!person) return;
                            setFacePersonName("");
                            return identifyFace(person.id);
                          })
                        }
                      >
                        Create and identify face
                      </button>
                    </div>
                  </div>
                );
              })()
            : null}
        </div>
      ) : null}
      <div className="space-y-2 rounded-md p-3" style={{ border: "1px solid var(--pv-border)" }}>
        <p className="text-sm" style={{ color: "var(--pv-silver)" }}>
          Add person to photo
        </p>
        <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Adds a Person as present in this photo. It does not identify a specific face.
        </p>
        <div className="flex flex-wrap gap-2">
          <select
            className="pv-input !w-auto !py-2 text-xs"
            aria-label="Add person to photo"
            value={photoPersonId}
            onChange={(event) => setPhotoPersonId(event.target.value)}
          >
            <option value="">Add person…</option>
            {allPeople
              .filter((person) => person.active)
              .map((person) => (
                <option key={person.id} value={person.id}>
                  {person.display_name}
                </option>
              ))}
          </select>
          <button
            type="button"
            className="rounded-md px-3 py-2 text-xs"
            disabled={!photoPersonId || busy}
            onClick={() => void assign(photoPersonId)}
          >
            Add person to photo
          </button>
          <input
            className="pv-input !w-48 !py-2 text-xs"
            value={photoPersonName}
            onChange={(event) => setPhotoPersonName(event.target.value)}
            placeholder="Create Person for this photo"
          />
          <button
            type="button"
            className="rounded-md px-3 py-2 text-xs"
            disabled={!photoPersonName.trim() || busy}
            onClick={() =>
              void createPerson(photoPersonName).then((person) => {
                if (!person) return;
                setPhotoPersonName("");
                return assign(person.id);
              })
            }
          >
            Create and add to photo
          </button>
        </div>
      </div>
      {status ? (
        <ActionProgress
          state={status.status === "processing" ? "running" : status.status}
          label={peopleAnalysisStatusLabel(status)}
          detail={
            status.status === "failed"
              ? "People analysis failed. This photo remains published."
              : undefined
          }
          onRetry={status.status === "failed" && !busy ? () => void analyse() : undefined}
          showProgressBar={false}
        />
      ) : (
        <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          People analysis not yet run
        </p>
      )}
    </section>
  );
}

function GalleryIntelligenceMetadata({
  photo,
  onUpdated,
  job,
  queueing,
  analysisError,
  onRetry,
}: {
  photo: GalleryImageDetails;
  onUpdated: (photo: GalleryImageDetails) => void;
  job: GalleryIntelligenceJobStatus | null;
  queueing: boolean;
  analysisError: string | null;
  onRetry: () => void;
}) {
  const [terms, setTerms] = useState<GalleryIntelligenceTerm[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!photo.can_edit) return;
    void fetch("/api/gallery/intelligence/terms", { credentials: "include" })
      .then((response) => (response.ok ? response.json() : []))
      .then((value) => setTerms(value as GalleryIntelligenceTerm[]));
  }, [photo.can_edit]);

  const update = async (
    namespace: "photo_type" | "content_tag",
    slug: string,
    decision: "include" | "exclude",
  ) => {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/gallery/${photo.id}/intelligence`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ namespace, slug, decision }),
      });
      if (!response.ok) throw new Error();
      onUpdated({ ...photo, intelligence: (await response.json()) as GalleryIntelligenceTerm[] });
    } catch {
      setError("The Gallery Intelligence correction could not be saved.");
    } finally {
      setBusy(false);
    }
  };

  const values = (namespace: "photo_type" | "content_tag") =>
    photo.intelligence.filter((term) => term.namespace === namespace);
  return (
    <section className="mt-5 space-y-3 pt-5" style={{ borderTop: "1px solid var(--pv-border)" }}>
      <div>
        <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
          Gallery Intelligence
        </h2>
        <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Descriptive metadata from Vault Master. It does not affect Arrival Hall routing or
          publication.
        </p>
        {job && (
          <div className="mt-2">
            <ActionProgress
              state={job.status === "processing" ? "running" : job.status}
              label={`Gallery Intelligence: ${galleryIntelligenceStatusLabel(job)}`}
              onRetry={job.status === "failed" && !queueing ? onRetry : undefined}
            />
            {job.status === "failed" && (
              <>
                <p className="mt-1 text-red-300">{job.error ?? "Analysis failed."}</p>
              </>
            )}
          </div>
        )}
        {analysisError && <p className="mt-2 text-xs text-red-300">{analysisError}</p>}
      </div>
      {(["photo_type", "content_tag"] as const).map((namespace) => (
        <div key={namespace} className="space-y-2">
          <h3 className="text-xs font-medium" style={{ color: "var(--pv-silver)" }}>
            {namespace === "photo_type" ? "Photo type" : "Content tags"}
          </h3>
          {values(namespace).length ? (
            <div className="flex flex-wrap gap-2">
              {values(namespace).map((term) => (
                <span
                  key={`${namespace}:${term.slug}`}
                  className="inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs"
                  style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
                >
                  {term.display_name}
                  {photo.can_edit && (
                    <button
                      type="button"
                      disabled={busy}
                      aria-label={`Remove ${term.display_name}`}
                      onClick={() => void update(namespace, term.slug, "exclude")}
                      style={{ color: "var(--pv-gold)" }}
                    >
                      ×
                    </button>
                  )}
                </span>
              ))}
            </div>
          ) : (
            <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              No {namespace === "photo_type" ? "photo type" : "detected tags"} yet.
            </p>
          )}
          {photo.can_edit && (
            <select
              className="pv-input !w-auto !py-2 text-xs"
              value=""
              disabled={busy}
              aria-label={`Add ${namespace === "photo_type" ? "photo type" : "content tag"}`}
              onChange={(event) => {
                if (event.target.value) void update(namespace, event.target.value, "include");
              }}
            >
              <option value="">Add {namespace === "photo_type" ? "photo type" : "tag"}…</option>
              {terms
                .filter(
                  (term) =>
                    term.namespace === namespace &&
                    !values(namespace).some((value) => value.slug === term.slug),
                )
                .map((term) => (
                  <option key={term.slug} value={term.slug}>
                    {term.display_name}
                  </option>
                ))}
            </select>
          )}
        </div>
      ))}
      {error && <p className="text-xs text-red-300">{error}</p>}
    </section>
  );
}

type AiJob = { id: string; status: string; error: string | null; created_at: string };

function GalleryVisualDescription({ evidence }: { evidence: AiEvidence }) {
  if (evidence.visual_description) {
    return (
      <p
        className="rounded-md p-3 text-sm leading-relaxed"
        style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
      >
        {evidence.visual_description.caption}
      </p>
    );
  }
  return (
    <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
      No Florence visual description was retained when this photo passed through the Arrival Hall.
    </p>
  );
}

type GalleryOptionDialog = "menu" | "move" | "ocr" | null;
type MovePreflight = { ready: boolean; destination_path: string | null; reason: string | null };
type MoveCategory = "Gallery" | "Home Videos" | "Documents" | "Archives" | "Music";
const MOVE_CATEGORIES: MoveCategory[] = [
  "Gallery",
  "Home Videos",
  "Documents",
  "Archives",
  "Music",
];

function GalleryOptions({
  assetId,
  lifecycleState,
  sort,
  job,
  queueing,
  intelligence,
  onAnalysePhoto,
}: {
  assetId: string;
  lifecycleState: "active" | "hidden";
  sort: GallerySortOrder;
  job: GalleryIntelligenceJobStatus | null;
  queueing: boolean;
  intelligence: GalleryIntelligenceTerm[];
  onAnalysePhoto: () => Promise<void>;
}) {
  const navigate = useNavigate();
  const [dialog, setDialog] = useState<GalleryOptionDialog>(null);
  const [destinations, setDestinations] = useState<string[]>([]);
  const [category, setCategory] = useState<MoveCategory>("Gallery");
  const [destination, setDestination] = useState("");
  const [preflight, setPreflight] = useState<MovePreflight | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const close = () => {
    if (!busy) {
      setDialog(null);
      setError(null);
      setPreflight(null);
    }
  };
  const request = async (url: string, options?: RequestInit) => {
    const response = await fetch(url, { credentials: "include", ...options });
    const body = (await response.json()) as MovePreflight & { detail?: string };
    if (!response.ok) throw new Error(body.detail ?? "Request failed");
    return body;
  };
  async function loadDestinations(selectedCategory: MoveCategory) {
    setBusy(true);
    setError(null);
    try {
      const body = await request(
        `/api/vault-master/lifecycle/move-destinations?category=${encodeURIComponent(selectedCategory)}`,
      );
      const folders = (body as unknown as { destinations: string[] }).destinations;
      setDestinations(folders);
      setDestination(folders[0] ?? "");
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Existing Vault folders could not be loaded",
      );
    } finally {
      setBusy(false);
    }
  }
  async function openMove() {
    setDialog("move");
    await loadDestinations(category);
  }
  async function checkMove() {
    setBusy(true);
    setError(null);
    try {
      setPreflight(
        await request(`/api/vault-master/assets/${assetId}/lifecycle/move-preflight`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            category,
            destination_folder: destination.replace(
              new RegExp(`^/vault/${category.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/?`),
              "",
            ),
          }),
        }),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Move preflight failed");
    } finally {
      setBusy(false);
    }
  }
  async function confirmMove() {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/vault-master/assets/${assetId}/lifecycle/move-confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          category,
          destination_folder: destination.replace(
            new RegExp(`^/vault/${category.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/?`),
            "",
          ),
          confirm: true,
        }),
      });
      void navigate({
        to: "/app/gallery",
        search: { sort, photo_type: [], content_tag: [], person: [] },
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Move was refused");
    } finally {
      setBusy(false);
    }
  }
  async function queueGalleryIntelligence() {
    setBusy(true);
    setError(null);
    try {
      await onAnalysePhoto();
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Gallery Intelligence analysis could not be queued",
      );
    } finally {
      setBusy(false);
    }
  }
  async function changeLifecycle() {
    setBusy(true);
    setError(null);
    try {
      const action = lifecycleState === "hidden" ? "unhide" : "hide";
      await request(`/api/vault-master/assets/${assetId}/lifecycle/${action}`, {
        method: "POST",
      });
      void navigate({
        to: "/app/gallery",
        search: { sort, photo_type: [], content_tag: [], person: [] },
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Gallery visibility could not be changed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div
      className="mt-5 flex justify-end"
      style={{ borderTop: "1px solid var(--pv-border)", paddingTop: "1.25rem" }}
    >
      <button
        type="button"
        className="rounded-md px-3 py-2 text-xs"
        style={{ color: "var(--pv-gold)", border: "1px solid var(--pv-border)" }}
        onClick={() => setDialog("menu")}
      >
        Options
      </button>
      {dialog && dialog !== "ocr" && (
        <div
          className="fixed inset-0 z-[90] flex items-center justify-center bg-black/70 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Gallery options"
        >
          <div className="pv-panel w-full max-w-md space-y-4 p-5">
            {dialog === "menu" && (
              <>
                <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
                  Options
                </h2>
                <div className="grid gap-2">
                  <button
                    type="button"
                    className="rounded-md px-3 py-2 text-left text-sm"
                    onClick={() => void openMove()}
                  >
                    Move
                  </button>
                  <button
                    type="button"
                    className="rounded-md px-3 py-2 text-left text-sm"
                    disabled={busy}
                    onClick={() => void changeLifecycle()}
                  >
                    {lifecycleState === "hidden" ? "Restore" : "Hide"}
                  </button>
                  <button
                    type="button"
                    className="rounded-md px-3 py-2 text-left text-sm"
                    disabled={busy || queueing}
                    onClick={() => void queueGalleryIntelligence()}
                  >
                    {busy || queueing ? "Queueing photo analysis…" : "Analyse photo"}
                  </button>
                  <button
                    type="button"
                    className="rounded-md px-3 py-2 text-left text-sm"
                    disabled={busy}
                    onClick={() => setDialog("ocr")}
                  >
                    Analyse text / OCR
                  </button>
                </div>
                {job && (
                  <div className="space-y-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                    <ActionProgress
                      state={job.status === "processing" ? "running" : job.status}
                      label={`Gallery Intelligence: ${galleryIntelligenceStatusLabel(job)}`}
                      onRetry={
                        job.status === "failed" && !busy && !queueing
                          ? () => void queueGalleryIntelligence()
                          : undefined
                      }
                    />
                    {job.status === "completed" && (
                      <GalleryIntelligenceResult intelligence={intelligence} />
                    )}
                    {job.status === "failed" && (
                      <>
                        <p className="text-red-300">
                          {job.error ?? "Gallery Intelligence analysis failed."}
                        </p>
                      </>
                    )}
                  </div>
                )}
              </>
            )}
            {dialog === "move" && (
              <>
                <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
                  Move file
                </h2>
                <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  Select an existing Vault-section folder. No folder is created and no file can be
                  overwritten.
                </p>
                <select
                  value={category}
                  className="w-full rounded-md bg-transparent px-3 py-2 text-xs"
                  onChange={(event) => {
                    const selectedCategory = event.target.value as MoveCategory;
                    setCategory(selectedCategory);
                    setPreflight(null);
                    void loadDestinations(selectedCategory);
                  }}
                >
                  {MOVE_CATEGORIES.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
                <select
                  value={destination}
                  className="w-full rounded-md bg-transparent px-3 py-2 text-xs"
                  onChange={(event) => {
                    setDestination(event.target.value);
                    setPreflight(null);
                  }}
                >
                  {destinations.map((path) => (
                    <option key={path} value={path}>
                      {path}
                    </option>
                  ))}
                </select>
                {preflight && (
                  <p className="text-xs">
                    {preflight.ready ? `Ready: ${preflight.destination_path}` : preflight.reason}
                  </p>
                )}
                <button
                  type="button"
                  className="rounded-md px-3 py-2 text-xs"
                  disabled={busy || !destination}
                  onClick={() => void checkMove()}
                >
                  Check move
                </button>
                {preflight?.ready && (
                  <button
                    type="button"
                    className="pv-btn-primary px-3 py-2 text-xs"
                    disabled={busy}
                    onClick={() => void confirmMove()}
                  >
                    Confirm move
                  </button>
                )}
              </>
            )}
            {error && <p className="text-xs text-red-300">{error}</p>}
            <div className="flex justify-end">
              <button
                type="button"
                className="rounded-md px-3 py-2 text-xs"
                disabled={busy}
                onClick={close}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
      {dialog === "ocr" && <AnalysisDialog assetId={assetId} onClose={close} />}
    </div>
  );
}

function GalleryIntelligenceResult({ intelligence }: { intelligence: GalleryIntelligenceTerm[] }) {
  const photoTypes = intelligence.filter((term) => term.namespace === "photo_type");
  const contentTags = intelligence.filter((term) => term.namespace === "content_tag");
  if (!photoTypes.length && !contentTags.length) {
    return <p>Analysis completed — no photo type or content tags were identified.</p>;
  }
  return (
    <div className="space-y-1">
      <p>Photo type: {photoTypes.map((term) => term.display_name).join(", ") || "None"}</p>
      <p>Content tags: {contentTags.map((term) => term.display_name).join(", ") || "None"}</p>
    </div>
  );
}

type AiSuggestion = {
  id: string;
  raw_value: string;
  reviewed_value: string | null;
  status: "pending" | "accepted" | "rejected" | "deferred";
  model_id: string;
  model_revision: string;
  task_version: string;
  processing_ms: number;
};
type VisualDescriptionEvidence = {
  caption: string;
  confidence: number;
  model_id: string;
  model_revision: string;
  task_version: string;
  created_at: string;
};
type AiEvidence = {
  jobs: AiJob[];
  suggestions: AiSuggestion[];
  visual_description: VisualDescriptionEvidence | null;
};

function AnalysisDialog({ assetId, onClose }: { assetId: string; onClose: () => void }) {
  const [evidence, setEvidence] = useState<AiEvidence>({
    jobs: [],
    suggestions: [],
    visual_description: null,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const loadEvidence = useCallback(async () => {
    const response = await fetch(`/api/vault-master/assets/${assetId}/ai`, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("Analysis could not be loaded");
    const loaded = (await response.json()) as AiEvidence;
    setEvidence(loaded);
    setDrafts((current) => ({
      ...Object.fromEntries(loaded.suggestions.map((item) => [item.id, item.raw_value])),
      ...current,
    }));
    return loaded;
  }, [assetId]);

  const queueAnalysis = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/vault-master/assets/${assetId}/ai/ocr`, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("OCR request failed");
      await loadEvidence();
    } catch {
      setError("Analysis could not be started.");
    } finally {
      setBusy(false);
    }
  }, [assetId, loadEvidence]);

  useEffect(() => {
    let cancelled = false;
    void loadEvidence()
      .then((loaded) => {
        const hasCompleted = loaded.jobs.some((job) => job.status === "completed");
        const hasActive = loaded.jobs.some((job) => ["queued", "processing"].includes(job.status));
        const hasFailed = loaded.jobs.some((job) => job.status === "failed");
        if (!cancelled && !hasCompleted && !hasActive && !hasFailed) {
          void queueAnalysis();
        }
      })
      .catch(() => setError("Analysis could not be loaded."));
    return () => {
      cancelled = true;
    };
  }, [loadEvidence, queueAnalysis]);

  async function review(id: string, status: "accepted" | "rejected" | "deferred") {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${assetId}/ai/suggestions/${id}/review`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ status, value: status === "accepted" ? drafts[id] : null }),
        },
      );
      if (!response.ok) throw new Error("Review failed");
      await loadEvidence();
    } catch {
      setError("The OCR review could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  const active = evidence.jobs.some((job) => ["queued", "processing"].includes(job.status));
  const completed = evidence.jobs.some((job) => job.status === "completed");
  const failedJob = evidence.jobs.find((job) => job.status === "failed");
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => {
      void loadEvidence().catch(() => setError("Analysis could not be loaded."));
    }, 1_500);
    return () => window.clearInterval(timer);
  }, [active, loadEvidence]);
  return (
    <div
      className="fixed inset-0 z-[90] flex items-center justify-center bg-black/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Analyse text / OCR"
    >
      <div className="pv-panel max-h-[85vh] w-full max-w-2xl space-y-4 overflow-y-auto p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-medium" style={{ color: "var(--pv-silver)" }}>
              Analyse text / OCR
            </h2>
          </div>
          {failedJob && (
            <button
              type="button"
              className="pv-btn-primary px-3 py-2 text-xs"
              disabled={busy}
              onClick={queueAnalysis}
            >
              {busy ? "Analysing…" : "Retry"}
            </button>
          )}
        </div>
        {error && <p className="text-xs text-red-300">{error}</p>}
        {active && (
          <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Analysing…
          </p>
        )}
        {failedJob && <p className="text-xs text-red-300">Analysis failed: {failedJob.error}</p>}
        {completed && evidence.suggestions.length === 0 && (
          <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            No text found.
          </p>
        )}
        {evidence.suggestions.map((suggestion) => (
          <div
            key={suggestion.id}
            className="space-y-3 rounded-md p-3"
            style={{ border: "1px solid var(--pv-border)" }}
          >
            <textarea
              className="min-h-28 w-full rounded-md bg-transparent p-3 text-sm outline-none"
              style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
              value={drafts[suggestion.id] ?? suggestion.raw_value}
              disabled={suggestion.status !== "pending" || busy}
              onChange={(event) =>
                setDrafts((current) => ({ ...current, [suggestion.id]: event.target.value }))
              }
            />
            <p className="text-[10px]" style={{ color: "var(--pv-text-dim)" }}>
              {suggestion.model_id} · {suggestion.task_version} · {suggestion.processing_ms} ms ·{" "}
              {suggestion.status}
            </p>
            {suggestion.status === "pending" && (
              <div className="flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  className="rounded-md px-3 py-2 text-xs"
                  disabled={busy}
                  onClick={() => review(suggestion.id, "deferred")}
                >
                  Later
                </button>
                <button
                  type="button"
                  className="rounded-md px-3 py-2 text-xs"
                  disabled={busy}
                  onClick={() => review(suggestion.id, "rejected")}
                >
                  Reject
                </button>
                <button
                  type="button"
                  className="pv-btn-primary px-3 py-2 text-xs"
                  disabled={busy}
                  onClick={() => review(suggestion.id, "accepted")}
                >
                  Accept text
                </button>
              </div>
            )}
          </div>
        ))}
        <div className="flex justify-end">
          <button
            type="button"
            className="rounded-md px-3 py-2 text-xs"
            disabled={busy}
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

type AssetSharingRecipient = {
  user_id: string;
  display_name: string;
  avatar_label: string;
};

type AssetSharingState = {
  owner_username: string;
  mode: "private" | "everyone" | "specific";
  recipients: AssetSharingRecipient[];
  eligible_users: AssetSharingRecipient[];
  pending: boolean;
};

function MetadataInput({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: "text" | "date";
  placeholder?: string;
}) {
  return (
    <label className="space-y-2 text-xs">
      <span className="block" style={{ color: "var(--pv-silver)" }}>
        {label}
      </span>
      <input
        type={type}
        value={value}
        maxLength={type === "text" ? 240 : undefined}
        max={type === "date" ? new Date().toISOString().slice(0, 10) : undefined}
        placeholder={placeholder}
        className="w-full rounded-md bg-transparent px-3 py-2 outline-none"
        style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[unitIndex]}`;
}

function PhotoStage({
  photo,
  sort,
  faceIdentification,
}: {
  photo: GalleryImageDetails;
  sort: GallerySortOrder;
  faceIdentification: {
    active: boolean;
    selectedFaceId: string | null;
    onSelect: (faceId: string) => void;
  };
}) {
  const imageRef = useRef<HTMLImageElement | null>(null);
  const [renderedImageSize, setRenderedImageSize] = useState({
    width: 0,
    height: 0,
    naturalWidth: 0,
    naturalHeight: 0,
  });
  const scaleX = renderedImageSize.naturalWidth
    ? renderedImageSize.width / renderedImageSize.naturalWidth
    : 1;
  const scaleY = renderedImageSize.naturalHeight
    ? renderedImageSize.height / renderedImageSize.naturalHeight
    : 1;
  return (
    <div
      className="pv-panel relative min-h-[55vh] md:min-h-[70vh] overflow-hidden flex items-center justify-center"
      style={{ background: "#050506" }}
    >
      {(photo.media_type ?? photo.mime_type ?? "").startsWith("image/") ? (
        <div className="relative inline-block max-h-[78vh] max-w-full">
          <img
            ref={imageRef}
            src={photo.image_url}
            alt={photo.display_title ?? getPhotoTitle(photo.name)}
            className="block max-h-[78vh] max-w-full object-contain"
            onLoad={(event) => {
              const image = event.currentTarget;
              setRenderedImageSize({
                width: image.clientWidth,
                height: image.clientHeight,
                naturalWidth: image.naturalWidth,
                naturalHeight: image.naturalHeight,
              });
            }}
          />
          {faceIdentification.active &&
            photo.face_detections?.map((face, index) => {
              const box = face.bounding_box;
              const left = `${Math.max(0, box.x * scaleX)}px`;
              const top = `${Math.max(0, box.y * scaleY)}px`;
              return (
                <button
                  key={face.id}
                  type="button"
                  aria-label={`Select ${face.person_name ?? `Unknown face ${index + 1}`}`}
                  className="absolute rounded-sm text-left text-[10px]"
                  style={{
                    left,
                    top,
                    width: `${Math.max(1, box.w * scaleX)}px`,
                    height: `${Math.max(1, box.h * scaleY)}px`,
                    border: `2px solid ${face.id === faceIdentification.selectedFaceId ? "var(--pv-gold)" : "rgba(255,255,255,0.72)"}`,
                    background: "rgba(215,185,104,0.08)",
                  }}
                  onClick={() => faceIdentification.onSelect(face.id)}
                >
                  <span
                    className="absolute -top-5 left-0 whitespace-nowrap rounded px-1"
                    style={{ background: "rgba(8,8,10,0.86)", color: "var(--pv-silver)" }}
                  >
                    {face.person_name ?? `Face ${index + 1}`}
                  </span>
                </button>
              );
            })}
        </div>
      ) : (photo.media_type ?? photo.mime_type) === "application/pdf" ? (
        <img
          src={photo.thumbnail_url}
          alt={`First-page preview of ${photo.name}`}
          className="max-h-[78vh] w-full object-contain"
        />
      ) : (
        <p className="text-sm" style={{ color: "var(--pv-text-dim)" }}>
          No preview is available for this file.
        </p>
      )}
      <PhotoNavigation direction="previous" photoId={photo.previous_id} sort={sort} />
      <PhotoNavigation direction="next" photoId={photo.next_id} sort={sort} />
    </div>
  );
}

function BackToGallery({
  sort,
  photoTypes = [],
  contentTags = [],
  people = [],
}: {
  sort: GallerySortOrder;
  photoTypes?: string[];
  contentTags?: string[];
  people?: string[];
}) {
  return (
    <Link
      to="/app/gallery"
      search={{ sort, photo_type: photoTypes, content_tag: contentTags, person: people }}
      resetScroll={false}
      className="pv-text-link inline-flex items-center gap-2 text-sm"
    >
      <ArrowLeft size={16} />
      Gallery
    </Link>
  );
}

function PhotoNavigation({
  direction,
  photoId,
  sort,
}: {
  direction: "previous" | "next";
  photoId: string | null;
  sort: GallerySortOrder;
}) {
  const isPrevious = direction === "previous";
  const className = `absolute top-1/2 -translate-y-1/2 ${
    isPrevious ? "left-4" : "right-4"
  } h-11 w-11 rounded-full items-center justify-center`;
  const style = {
    background: "rgba(8,8,10,0.78)",
    border: "1px solid var(--pv-border-strong)",
    color: "var(--pv-silver)",
  };

  if (!photoId) {
    return (
      <span className={`${className} hidden md:flex opacity-30`} style={style}>
        {isPrevious ? <ChevronLeft size={22} /> : <ChevronRight size={22} />}
      </span>
    );
  }

  return (
    <Link
      to="/app/gallery/$photoId"
      params={{ photoId }}
      search={{ sort, photo_type: [], content_tag: [], person: [] }}
      className={`${className} flex transition-transform hover:scale-105`}
      style={style}
      aria-label={isPrevious ? "Previous photo" : "Next photo"}
    >
      {isPrevious ? <ChevronLeft size={22} /> : <ChevronRight size={22} />}
    </Link>
  );
}
