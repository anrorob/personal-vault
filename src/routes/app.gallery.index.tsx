import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { CalendarDays, Filter, Image as ImageIcon, RefreshCw } from "lucide-react";
import { createPortal } from "react-dom";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DEFAULT_GALLERY_SORT,
  formatPhotoDate,
  getPhotoTitle,
  parseGalleryFilter,
  parseGallerySortOrder,
  type GalleryIntelligenceTerm,
  type GalleryImage,
  type GallerySortOrder,
} from "@/lib/gallery";
import { ActionProgress } from "@/components/pv/ActionProgress";

export const Route = createFileRoute("/app/gallery/")({
  validateSearch: (search: Record<string, unknown>) => ({
    sort: parseGallerySortOrder(search.sort),
    photo_type: parseGalleryFilter(search.photo_type),
    content_tag: parseGalleryFilter(search.content_tag),
    person: parseGalleryFilter(search.person),
  }),
  component: GalleryPage,
});

type GalleryViewState = {
  sort: GallerySortOrder;
  anchor_id: string | null;
  anchor_offset: number;
};

type GalleryIntelligenceBulkProgress = {
  id: string;
  total: number;
  completed: number;
  processing: number;
  queued: number;
  failed: number;
};

function GalleryPage() {
  const navigate = useNavigate();
  const { sort, photo_type, content_tag, person } = Route.useSearch();
  const [people, setPeople] = useState<
    Array<{ id: string; display_name: string; active: boolean }>
  >([]);
  const [images, setImages] = useState<GalleryImage[] | null>(null);
  const [terms, setTerms] = useState<GalleryIntelligenceTerm[]>([]);
  const [canBackfill, setCanBackfill] = useState(false);
  const [backfillStatus, setBackfillStatus] = useState<string | null>(null);
  const [bulkProgress, setBulkProgress] = useState<GalleryIntelligenceBulkProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savedState, setSavedState] = useState<GalleryViewState | null>(null);
  const [stateReady, setStateReady] = useState(false);
  const [timelineIndex, setTimelineIndex] = useState(0);
  const [scrubbing, setScrubbing] = useState(false);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [shareOpen, setShareOpen] = useState(false);
  const [shareKind, setShareKind] = useState<"individual" | "collection">("individual");
  const [collectionName, setCollectionName] = useState("");
  const [shareMode, setShareMode] = useState<"quick" | "standard">("quick");
  const [shareTarget, setShareTarget] = useState<"everyone" | "specific">("specific");
  const [shareDestination, setShareDestination] = useState<"local" | "vault">("local");
  const [pairedVaults, setPairedVaults] = useState<
    Array<{ remote_vault_id: string; display_label: string }>
  >([]);
  const [targetVaultId, setTargetVaultId] = useState("");
  const [recipients, setRecipients] = useState<Array<{ user_id: string; display_name: string }>>(
    [],
  );
  const [recipientIds, setRecipientIds] = useState<string[]>([]);
  const [shareBusy, setShareBusy] = useState(false);
  const [shareError, setShareError] = useState<string | null>(null);
  const [includeSharedPhotos, setIncludeSharedPhotos] = useState(false);
  const [includeHidden, setIncludeHidden] = useState(false);
  const [galleryVersion, setGalleryVersion] = useState(0);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [purgeReason, setPurgeReason] = useState("");
  const [purging, setPurging] = useState(false);
  const [sharedCollections, setSharedCollections] = useState<
    Array<{ collection_id: string; name: string; owner_display_name: string; included: boolean }>
  >([]);
  const restoredSort = useRef<GallerySortOrder | null>(null);
  const openingPhoto = useRef(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const timelineTrack = useRef<HTMLDivElement | null>(null);

  const loadBulkProgress = useCallback(async () => {
    const response = await fetch("/api/gallery/intelligence/backfill/latest", {
      credentials: "include",
    });
    if (!response.ok) return;
    const result = (await response.json()) as { run: GalleryIntelligenceBulkProgress | null };
    setBulkProgress(result.run);
  }, []);

  useEffect(() => {
    void fetch("/api/user-state/gallery", { credentials: "include" })
      .then(async (response) => {
        if (response.status === 401) return navigate({ to: "/login" });
        if (!response.ok) throw new Error();
        const state = (await response.json()) as GalleryViewState;
        setSavedState(state);
        if (!new URLSearchParams(window.location.search).has("sort") && state.sort !== sort) {
          await navigate({
            to: "/app/gallery",
            search: { sort: state.sort, photo_type: [], content_tag: [], person: [] },
            replace: true,
          });
        }
      })
      .catch(() => setError("Your Gallery preferences could not be opened."))
      .finally(() => setStateReady(true));
  }, [navigate, sort]);

  useEffect(() => {
    void fetch("/api/gallery/intelligence/terms", { credentials: "include" })
      .then((response) => (response.ok ? response.json() : []))
      .then((value) => setTerms(value as GalleryIntelligenceTerm[]));
    void fetch("/api/gallery/people", { credentials: "include" })
      .then((response) => (response.ok ? response.json() : []))
      .then(setPeople);
    void fetch("/api/gallery/intelligence/backfill/status", { credentials: "include" }).then(
      (response) => {
        setCanBackfill(response.ok);
        if (response.ok) void loadBulkProgress();
      },
    );
    void fetch("/api/gallery/shared-preference", { credentials: "include" })
      .then((response) => (response.ok ? response.json() : null))
      .then((value: { include_shared_photos: boolean } | null) => {
        if (value) setIncludeSharedPhotos(value.include_shared_photos);
      });
    void fetch("/api/gallery/shared-collections", { credentials: "include" })
      .then((response) => (response.ok ? response.json() : []))
      .then((value) => setSharedCollections(value as typeof sharedCollections));
  }, [loadBulkProgress]);

  useEffect(() => {
    if (!bulkProgress || bulkProgress.queued + bulkProgress.processing === 0) return;
    const timer = window.setInterval(() => void loadBulkProgress(), 2000);
    return () => window.clearInterval(timer);
  }, [bulkProgress, loadBulkProgress]);

  useEffect(() => {
    if (!bulkProgress || bulkProgress.queued + bulkProgress.processing > 0) return;
    const timer = window.setTimeout(() => setBulkProgress(null), 12_000);
    return () => window.clearTimeout(timer);
  }, [bulkProgress]);

  useEffect(() => {
    if (!stateReady) return;
    const controller = new AbortController();

    const loadGallery = async () => {
      try {
        const query = new URLSearchParams({ sort });
        if (includeHidden) query.set("include_hidden", "true");
        photo_type.forEach((value) => query.append("photo_type", value));
        content_tag.forEach((value) => query.append("content_tag", value));
        person.forEach((value) => query.append("person", value));
        const response = await fetch(`/api/gallery?${query}`, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });

        if (response.status === 401) {
          await navigate({ to: "/login" });
          return;
        }

        if (!response.ok) {
          throw new Error("Gallery request failed");
        }

        setImages((await response.json()) as GalleryImage[]);
      } catch (requestError) {
        if (requestError instanceof DOMException && requestError.name === "AbortError") {
          return;
        }

        setError("The Gallery is currently unavailable.");
      }
    };

    void loadGallery();
    return () => controller.abort();
  }, [content_tag, galleryVersion, includeHidden, navigate, person, photo_type, sort, stateReady]);

  const currentAnchor = useCallback(() => {
    const cards = [...document.querySelectorAll<HTMLElement>("[data-gallery-id]")];
    const card = cards.find((item) => item.getBoundingClientRect().bottom > 80) ?? cards.at(-1);
    return card
      ? {
          anchor_id: card.dataset.galleryId ?? null,
          anchor_offset: Math.round(card.getBoundingClientRect().top),
        }
      : { anchor_id: null, anchor_offset: 0 };
  }, []);

  const persistState = useCallback(
    (nextSort = sort, immediate = false) => {
      const anchor = currentAnchor();
      const save = () => {
        void fetch("/api/user-state/gallery", {
          method: "PUT",
          credentials: "include",
          keepalive: immediate,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sort: nextSort, ...anchor }),
        });
      };
      if (saveTimer.current) clearTimeout(saveTimer.current);
      if (immediate) save();
      else saveTimer.current = setTimeout(save, 500);
    },
    [currentAnchor, sort],
  );

  useEffect(() => {
    const save = () => {
      if (!openingPhoto.current) {
        persistState();
        const cards = [...document.querySelectorAll<HTMLElement>("[data-gallery-id]")];
        const index = cards.findIndex((item) => item.getBoundingClientRect().bottom > 100);
        if (index >= 0) setTimelineIndex(index);
      }
    };
    window.addEventListener("scroll", save, { passive: true });
    window.addEventListener("pagehide", save);
    return () => {
      if (!openingPhoto.current) persistState(sort, true);
      window.removeEventListener("scroll", save);
      window.removeEventListener("pagehide", save);
    };
  }, [persistState, sort]);

  useEffect(() => {
    if (!images || restoredSort.current === sort) return;

    const state = savedState;
    restoredSort.current = sort;
    if (!state || state.sort !== sort) return;

    let restoreFrame: number | undefined;
    const frame = window.requestAnimationFrame(() => {
      // TanStack Router completes its own scroll handling after route content
      // has rendered. Restore after that cycle, not merely after the fetch.
      restoreFrame = window.requestAnimationFrame(() => {
        const anchor = state.anchor_id
          ? document.querySelector<HTMLElement>(
              `[data-gallery-id="${CSS.escape(state.anchor_id)}"]`,
            )
          : null;
        if (anchor) {
          anchor.scrollIntoView({ block: "start" });
          window.scrollBy(0, state.anchor_offset);
        }
      });
    });
    return () => {
      window.cancelAnimationFrame(frame);
      if (restoreFrame !== undefined) window.cancelAnimationFrame(restoreFrame);
    };
  }, [images, savedState, sort]);

  const changeSort = async (nextSort: GallerySortOrder) => {
    if (nextSort === sort) return;

    window.scrollTo({ top: 0 });
    if (saveTimer.current) clearTimeout(saveTimer.current);
    await fetch("/api/user-state/gallery", {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sort: nextSort, anchor_id: null, anchor_offset: 0 }),
    });
    setSavedState({ sort: nextSort, anchor_id: null, anchor_offset: 0 });
    await navigate({
      to: "/app/gallery",
      search: { sort: nextSort, photo_type, content_tag, person },
    });
  };

  const changeFilters = async (
    nextPhotoTypes: string[],
    nextContentTags: string[],
    nextPeople = person,
  ) => {
    window.scrollTo({ top: 0 });
    await navigate({
      to: "/app/gallery",
      search: {
        sort,
        photo_type: nextPhotoTypes,
        content_tag: nextContentTags,
        person: nextPeople,
      },
    });
  };

  const setSharedPreference = async (enabled: boolean) => {
    const response = await fetch("/api/gallery/shared-preference", {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ include_shared_photos: enabled }),
    });
    if (!response.ok) return;
    setIncludeSharedPhotos(enabled);
    setImages(null);
    const query = new URLSearchParams({ sort });
    photo_type.forEach((value) => query.append("photo_type", value));
    content_tag.forEach((value) => query.append("content_tag", value));
    person.forEach((value) => query.append("person", value));
    const refreshed = await fetch(`/api/gallery?${query}`, { credentials: "include" });
    if (refreshed.ok) setImages((await refreshed.json()) as GalleryImage[]);
  };

  const setCollectionInclusion = async (collectionId: string, included: boolean) => {
    const response = await fetch(`/api/gallery/shared-collections/${collectionId}/inclusion`, {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ included }),
    });
    if (!response.ok) return;
    setSharedCollections((current) =>
      current.map((collection) =>
        collection.collection_id === collectionId ? { ...collection, included } : collection,
      ),
    );
    void setSharedPreference(includeSharedPhotos);
  };

  const runBackfill = async (reanalyse = false) => {
    setBackfillStatus(null);
    const response = await fetch(
      `/api/gallery/intelligence/backfill?limit=50&reanalyse=${reanalyse}`,
      {
        method: "POST",
        credentials: "include",
      },
    );
    if (!response.ok) {
      setBackfillStatus("Analysis could not be queued.");
      return;
    }
    const result = (await response.json()) as {
      queued: number;
      run: GalleryIntelligenceBulkProgress | null;
    };
    setBulkProgress(result.run);
    setBackfillStatus(
      result.queued
        ? `${result.queued} photos queued for analysis.`
        : "No eligible photos need analysis.",
    );
  };

  const timelineLabel = useMemo(() => {
    const image = images?.[timelineIndex];
    if (!image?.captured_on) return "Undated";
    const date = new Date(`${image.captured_on.slice(0, 10)}T12:00:00`);
    return new Intl.DateTimeFormat("en-GB", { month: "short", year: "numeric" }).format(date);
  }, [images, timelineIndex]);

  const scrubTo = useCallback(
    (index: number) => {
      if (!images?.length) return;
      const nextIndex = Math.max(0, Math.min(images.length - 1, index));
      setTimelineIndex(nextIndex);
      const target = document.querySelector<HTMLElement>(`[data-gallery-index="${nextIndex}"]`);
      if (!target) return;

      // Use a document-level scroll target instead of scrollIntoView. This is
      // reliable inside the application shell and leaves the sticky header clear.
      const top = window.scrollY + target.getBoundingClientRect().top - 80;
      window.scrollTo({ top: Math.max(0, top), behavior: "auto" });
    },
    [images],
  );

  const scrubFromPointer = useCallback(
    (clientY: number) => {
      if (!images?.length || !timelineTrack.current) return;
      const bounds = timelineTrack.current.getBoundingClientRect();
      const progress = Math.max(0, Math.min(1, (clientY - bounds.top) / bounds.height));
      scrubTo(Math.round(progress * (images.length - 1)));
    },
    [images, scrubTo],
  );

  const openShare = async () => {
    if (selectedIds.length < 2) return;
    setShareOpen(true);
    setShareError(null);
    setShareKind("individual");
    setShareDestination("local");
    setShareMode(selectedIds.length > 10 ? "standard" : "quick");
    const response = await fetch(`/api/vault-master/assets/${selectedIds[0]}/sharing`, {
      credentials: "include",
    });
    if (!response.ok) {
      setShareError("Sharing choices could not be opened.");
      return;
    }
    const body = (await response.json()) as {
      eligible_users: Array<{ user_id: string; display_name: string }>;
    };
    setRecipients(body.eligible_users);
    setRecipientIds([]);
    void fetch("/api/vault-master/federation/peers", { credentials: "include" })
      .then((value) => (value.ok ? value.json() : []))
      .then((value) => setPairedVaults(value as typeof pairedVaults));
  };
  const submitShare = async () => {
    setShareError(null);
    if (shareDestination === "local" && shareTarget === "specific" && recipientIds.length === 0) {
      setShareError("Select at least one person.");
      return;
    }
    if (shareDestination === "vault" && !targetVaultId) {
      setShareError("Select a paired Vault.");
      return;
    }
    if (shareKind === "collection" && !collectionName.trim()) {
      setShareError("A collection name is required.");
      return;
    }
    setShareBusy(true);
    try {
      if (shareDestination === "vault") {
        if (shareKind === "collection") {
          const created = await fetch("/api/vault-master/shared-collections", {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: collectionName.trim(), asset_ids: selectedIds }),
          });
          const collection = (await created.json()) as { collection_id?: string; detail?: string };
          if (!created.ok || !collection.collection_id)
            throw new Error(collection.detail ?? "Collection could not be created.");
          const shared = await fetch(
            `/api/vault-master/shared-collections/${collection.collection_id}/federation`,
            {
              method: "POST",
              credentials: "include",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ target_vault_id: targetVaultId, share_mode: shareMode }),
            },
          );
          if (!shared.ok)
            throw new Error(
              ((await shared.json()) as { detail?: string }).detail ??
                "Collection could not be shared with that Vault.",
            );
        } else {
          const shared = await fetch("/api/vault-master/federation/outgoing", {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              asset_ids: selectedIds,
              target_vault_id: targetVaultId,
              share_mode: shareMode,
            }),
          });
          if (!shared.ok)
            throw new Error(
              ((await shared.json()) as { detail?: string }).detail ??
                "Selected items could not be shared with that Vault.",
            );
        }
      } else if (shareKind === "collection") {
        const created = await fetch("/api/vault-master/shared-collections", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: collectionName.trim(), asset_ids: selectedIds }),
        });
        const collection = (await created.json()) as { collection_id?: string; detail?: string };
        if (!created.ok || !collection.collection_id)
          throw new Error(collection.detail ?? "Collection could not be created.");
        const shared = await fetch(
          `/api/vault-master/shared-collections/${collection.collection_id}/share`,
          {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              mode: shareTarget,
              recipient_user_ids: recipientIds,
              share_mode: shareMode,
            }),
          },
        );
        if (!shared.ok)
          throw new Error(
            ((await shared.json()) as { detail?: string }).detail ??
              "Collection could not be shared.",
          );
      } else {
        const shared = await fetch("/api/vault-master/assets/sharing/bulk", {
          method: "PUT",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            asset_ids: selectedIds,
            mode: shareTarget,
            recipient_user_ids: recipientIds,
            share_mode: shareMode,
          }),
        });
        if (!shared.ok) {
          const failure = (await shared.json()) as {
            detail?: string | { message?: string; asset_id?: string; asset_title?: string };
          };
          const detail = failure.detail;
          if (typeof detail === "object" && detail?.message) {
            const item =
              detail.asset_title ??
              images?.find((image) => image.asset_id === detail.asset_id)?.display_title ??
              "Selected item";
            throw new Error(`${item}: ${detail.message}`);
          }
          throw new Error(
            typeof detail === "string" ? detail : "Selected items could not be shared.",
          );
        }
      }
      setSelectedIds([]);
      setShareOpen(false);
    } catch (reason) {
      setShareError(reason instanceof Error ? reason.message : "Share could not be saved.");
    } finally {
      setShareBusy(false);
    }
  };

  const changeLifecycle = async (action: "hide" | "unhide") => {
    if (selectedIds.length === 0) return;
    setError(null);
    try {
      const results = await Promise.all(
        selectedIds.map(async (assetId) => {
          const response = await fetch(`/api/vault-master/assets/${assetId}/lifecycle/${action}`, {
            method: "POST",
            credentials: "include",
          });
          if (!response.ok) {
            const body = (await response.json().catch(() => ({}))) as { detail?: string };
            throw new Error(body.detail ?? "Only the owner can change this item.");
          }
        }),
      );
      void results;
      setSelectedIds([]);
      setSelectionMode(false);
      setGalleryVersion((current) => current + 1);
      setIncludeHidden(action === "unhide");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Gallery visibility could not be changed.",
      );
    }
  };

  const permanentlyDeleteSelected = async () => {
    const assetId = selectedIds[0];
    if (!assetId || !purgeReason.trim()) return;
    setPurging(true);
    setError(null);
    try {
      const review = await fetch(
        `/api/vault-master/assets/${assetId}/lifecycle/permanent-deletion-review`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: purgeReason.trim() }),
        },
      );
      if (!review.ok) throw new Error((await review.json()).detail ?? "Purge review failed.");
      const confirmed = await fetch(
        `/api/vault-master/assets/${assetId}/lifecycle/permanent-deletion-confirm`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: true }),
        },
      );
      if (!confirmed.ok)
        throw new Error((await confirmed.json()).detail ?? "Purge confirmation failed.");
      const executed = await fetch(
        `/api/vault-master/assets/${assetId}/lifecycle/permanent-deletion-execute`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ execute: true }),
        },
      );
      if (!executed.ok)
        throw new Error((await executed.json()).detail ?? "Permanent deletion failed.");
      setPurgeOpen(false);
      setPurgeReason("");
      setSelectedIds([]);
      setSelectionMode(false);
      setGalleryVersion((current) => current + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Permanent deletion failed.");
    } finally {
      setPurging(false);
    }
  };

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="pv-content-title text-xl">Gallery</h2>
          <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
            {images === null
              ? "Opening the image archive..."
              : `${images.length} ${images.length === 1 ? "photo" : "photos"}`}
          </p>
        </div>
        <label className="flex items-center gap-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Sort photos
          <select
            className="pv-input !w-auto !py-2 text-sm"
            value={sort}
            onChange={(event) => void changeSort(parseGallerySortOrder(event.target.value))}
            aria-label="Sort Gallery photos"
          >
            <option value={DEFAULT_GALLERY_SORT}>Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <GalleryFilters
          terms={terms}
          photoTypes={photo_type}
          contentTags={content_tag}
          people={people}
          selectedPeople={person}
          onChange={changeFilters}
        />
        <details className="relative">
          <summary
            className="inline-flex cursor-pointer list-none items-center gap-2 rounded-md px-3 py-2 text-xs"
            style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
          >
            Shared photos
          </summary>
          <div
            className="absolute left-0 z-30 mt-2 w-72 space-y-3 rounded-md p-4 shadow-xl"
            style={{ background: "var(--pv-panel)", border: "1px solid var(--pv-border)" }}
          >
            <label
              className="flex items-center justify-between gap-3 text-xs"
              style={{ color: "var(--pv-silver)" }}
            >
              Include individually shared photos
              <input
                type="checkbox"
                checked={includeSharedPhotos}
                onChange={(event) => void setSharedPreference(event.target.checked)}
              />
            </label>
            {sharedCollections.length > 0 && (
              <div className="space-y-2 border-t pt-3" style={{ borderColor: "var(--pv-border)" }}>
                <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  Shared collections
                </p>
                {sharedCollections.map((collection) => (
                  <label
                    key={collection.collection_id}
                    className="flex items-start gap-2 text-xs"
                    style={{ color: "var(--pv-silver)" }}
                  >
                    <input
                      type="checkbox"
                      checked={collection.included}
                      onChange={(event) =>
                        void setCollectionInclusion(collection.collection_id, event.target.checked)
                      }
                    />
                    <span>
                      {collection.name}
                      <span className="block" style={{ color: "var(--pv-text-dim)" }}>
                        Shared by {collection.owner_display_name}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>
        </details>
        <button
          type="button"
          className="inline-flex items-center gap-2 rounded-md px-3 py-2 text-xs"
          style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
          aria-pressed={selectionMode}
          onClick={() => {
            if (selectionMode) {
              setSelectionMode(false);
              setSelectedIds([]);
              setShareOpen(false);
            } else {
              setSelectionMode(true);
            }
          }}
        >
          {selectionMode ? "Done" : "Select"}
        </button>
        <button
          type="button"
          className="inline-flex items-center gap-2 rounded-md px-3 py-2 text-xs"
          style={{
            color: includeHidden ? "var(--pv-gold)" : "var(--pv-silver)",
            border: "1px solid var(--pv-border)",
          }}
          aria-pressed={includeHidden}
          onClick={() => {
            setIncludeHidden((current) => !current);
            setSelectedIds([]);
            setSelectionMode(false);
          }}
        >
          {includeHidden ? "Hidden content" : "View Hidden"}
        </button>
        <ActiveGalleryFilters
          terms={terms}
          photoTypes={photo_type}
          contentTags={content_tag}
          people={people}
          selectedPeople={person}
          onChange={changeFilters}
        />
        {selectionMode && selectedIds.length > 0 && (
          <button
            type="button"
            className="pv-btn-secondary px-3 py-2 text-xs"
            onClick={() => void changeLifecycle(includeHidden ? "unhide" : "hide")}
          >
            {includeHidden
              ? `Restore selected (${selectedIds.length})`
              : `Hide selected (${selectedIds.length})`}
          </button>
        )}
        {selectionMode && selectedIds.length === 1 && (
          <button
            type="button"
            className="px-3 py-2 text-xs"
            style={{ color: "#f1b4b4", border: "1px solid #8f4040", borderRadius: "0.375rem" }}
            onClick={() => setPurgeOpen(true)}
          >
            Permanently delete
          </button>
        )}
        {(photo_type.length > 0 || content_tag.length > 0 || person.length > 0) && (
          <button
            type="button"
            className="text-xs"
            style={{ color: "var(--pv-gold)" }}
            onClick={() => void changeFilters([], [], [])}
          >
            Clear filters
          </button>
        )}
        {canBackfill && (
          <button
            type="button"
            className="inline-flex items-center gap-2 rounded-md px-3 py-2 text-xs"
            style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
            onClick={() => void runBackfill(false)}
          >
            <RefreshCw size={13} />
            Analyse existing photos
          </button>
        )}
        {backfillStatus && (
          <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            {backfillStatus}
          </span>
        )}
        {bulkProgress && (
          <ActionProgress
            state={
              bulkProgress.queued + bulkProgress.processing > 0
                ? bulkProgress.queued > 0 && bulkProgress.processing === 0
                  ? "queued"
                  : "running"
                : bulkProgress.failed > 0
                  ? "completed_with_warnings"
                  : "completed"
            }
            label={
              bulkProgress.queued + bulkProgress.processing > 0
                ? "Analysing"
                : "Bulk analysis complete"
            }
            current={bulkProgress.completed + bulkProgress.failed}
            total={bulkProgress.total}
            detail={
              bulkProgress.processing || bulkProgress.queued || bulkProgress.failed
                ? `${bulkProgress.processing} processing · ${bulkProgress.queued} queued · ${bulkProgress.failed} failed`
                : undefined
            }
          />
        )}
      </div>

      {purgeOpen && (
        <section
          className="space-y-3 rounded-md p-4"
          style={{ background: "var(--pv-panel)", border: "1px solid #8f4040" }}
        >
          <div>
            <h3 className="text-sm font-medium" style={{ color: "#f1b4b4" }}>
              Permanently delete this file?
            </h3>
            <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
              This removes the canonical bytes to reclaim storage. It cannot be undone; hidden
              content will not be restorable. Internal deletion history remains to prevent
              accidental recovery.
            </p>
          </div>
          <label className="block text-xs" style={{ color: "var(--pv-silver)" }}>
            Reason for permanent deletion
            <input
              className="pv-input mt-1"
              value={purgeReason}
              onChange={(event) => setPurgeReason(event.target.value)}
            />
          </label>
          <div className="flex gap-2">
            <button
              type="button"
              className="pv-btn-secondary px-3 py-2 text-xs"
              disabled={purging}
              onClick={() => setPurgeOpen(false)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="px-3 py-2 text-xs"
              disabled={purging || !purgeReason.trim()}
              style={{ color: "#f1b4b4", border: "1px solid #8f4040", borderRadius: "0.375rem" }}
              onClick={() => void permanentlyDeleteSelected()}
            >
              {purging ? "Deleting…" : "Permanently delete file"}
            </button>
          </div>
        </section>
      )}

      {selectionMode && selectedIds.length > 0 && (
        <div className="pv-panel flex flex-wrap items-center justify-between gap-3 p-3 text-sm">
          <span style={{ color: "var(--pv-silver)" }}>{selectedIds.length} selected</span>
          <div className="flex gap-2">
            <button className="pv-btn-ghost px-3 py-2 text-xs" onClick={() => setSelectedIds([])}>
              Clear
            </button>
            <button
              className="pv-btn-primary px-3 py-2 text-xs"
              disabled={selectedIds.length < 2}
              onClick={() => void openShare()}
            >
              Share
            </button>
          </div>
        </div>
      )}

      {shareOpen && (
        <section className="pv-panel space-y-4 p-5" aria-label="Share selected items">
          <div>
            <h3 className="text-sm" style={{ color: "var(--pv-silver)" }}>
              Share {selectedIds.length} selected items
            </h3>
            <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
              Choose whether to share each item or keep them together as a logical collection.
            </p>
          </div>
          <fieldset className="space-y-2">
            <legend className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              Destination
            </legend>
            <label className="mr-4 text-sm">
              <input
                type="radio"
                checked={shareDestination === "local"}
                onChange={() => setShareDestination("local")}
              />{" "}
              Share in my Vault
            </label>
            <label className="text-sm">
              <input
                type="radio"
                checked={shareDestination === "vault"}
                onChange={() => setShareDestination("vault")}
              />{" "}
              Share with another Vault
            </label>
            {shareDestination === "vault" && (
              <select
                className="pv-input mt-2 block w-full"
                value={targetVaultId}
                onChange={(event) => setTargetVaultId(event.target.value)}
              >
                <option value="">Select paired Vault</option>
                {pairedVaults.map((vault) => (
                  <option key={vault.remote_vault_id} value={vault.remote_vault_id}>
                    {vault.display_label}
                  </option>
                ))}
              </select>
            )}
          </fieldset>
          {shareDestination === "local" && (
            <>
              <label className="flex gap-2 text-sm">
                <input
                  type="radio"
                  checked={shareKind === "individual"}
                  onChange={() => setShareKind("individual")}
                />{" "}
                Share selected items individually
              </label>
              <label className="flex gap-2 text-sm">
                <input
                  type="radio"
                  checked={shareKind === "collection"}
                  onChange={() => setShareKind("collection")}
                />{" "}
                Share as collection
              </label>
              {shareKind === "collection" && (
                <label className="block text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  Collection name
                  <input
                    className="pv-input mt-1 block w-full"
                    value={collectionName}
                    onChange={(event) => setCollectionName(event.target.value)}
                    placeholder="Athens Trip"
                  />
                </label>
              )}
              <fieldset className="space-y-2">
                <legend className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  Share in my Vault
                </legend>
                <label className="mr-4 text-sm">
                  <input
                    type="radio"
                    checked={shareTarget === "everyone"}
                    onChange={() => setShareTarget("everyone")}
                  />{" "}
                  Everyone
                </label>
                <label className="text-sm">
                  <input
                    type="radio"
                    checked={shareTarget === "specific"}
                    onChange={() => setShareTarget("specific")}
                  />{" "}
                  Specific people
                </label>
                {shareTarget === "specific" &&
                  recipients.map((person) => (
                    <label key={person.user_id} className="mt-2 flex gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={recipientIds.includes(person.user_id)}
                        onChange={() =>
                          setRecipientIds((current) =>
                            current.includes(person.user_id)
                              ? current.filter((id) => id !== person.user_id)
                              : [...current, person.user_id],
                          )
                        }
                      />{" "}
                      {person.display_name}
                    </label>
                  ))}
              </fieldset>
            </>
          )}
          <label className="block text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Sharing mode
            <select
              className="pv-input mt-1 block w-full"
              value={shareMode}
              onChange={(event) => setShareMode(event.target.value as "quick" | "standard")}
            >
              <option value="quick">Quick Share — available now</option>
              <option value="standard">Standard Share — review for 3 minutes</option>
            </select>
          </label>
          {shareError && <p className="text-sm text-red-300">{shareError}</p>}
          <div className="flex gap-2">
            <button className="pv-btn-ghost px-3 py-2 text-xs" onClick={() => setShareOpen(false)}>
              Cancel
            </button>
            <button
              className="pv-btn-primary px-3 py-2 text-xs"
              disabled={shareBusy}
              onClick={() => void submitShare()}
            >
              {shareBusy ? "Sharing…" : "Continue"}
            </button>
          </div>
        </section>
      )}

      {error && <div className="pv-panel p-6 text-sm text-center text-red-300">{error}</div>}

      {!error && images?.length === 0 && (
        <div className="pv-panel p-10 text-center">
          <span
            className="mx-auto h-12 w-12 rounded-full flex items-center justify-center"
            style={{ border: "1px solid var(--pv-border)", color: "var(--pv-gold)" }}
          >
            <ImageIcon size={20} />
          </span>
          <h3 className="text-sm font-semibold mt-4" style={{ color: "var(--pv-silver)" }}>
            Gallery is empty
          </h3>
          <p className="text-xs mt-2" style={{ color: "var(--pv-text-dim)" }}>
            Move staged images from the Arrival Hall into the Gallery library to display them here.
          </p>
        </div>
      )}

      {!error && images && images.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-4">
          {images.map((image) => (
            <Link
              key={image.id}
              to="/app/gallery/$photoId"
              params={{ photoId: image.id }}
              search={{ sort, photo_type, content_tag, person }}
              onClick={() => {
                // Router navigation resets the document scroll position. Lock the
                // clicked position so that reset cannot overwrite our return point.
                openingPhoto.current = true;
                persistState(sort, true);
              }}
              data-gallery-id={image.id}
              data-gallery-index={images.indexOf(image)}
              className="pv-panel pv-panel-hover relative overflow-hidden group"
              aria-label={`Open photo from ${formatPhotoDate(image.captured_on)}`}
            >
              {selectionMode && image.asset_id && (
                <label
                  className="absolute z-10 m-2 rounded bg-black/70 p-1"
                  onClick={(event) => event.stopPropagation()}
                >
                  <input
                    type="checkbox"
                    checked={selectedIds.includes(image.asset_id)}
                    onChange={() =>
                      setSelectedIds((current) =>
                        current.includes(image.asset_id)
                          ? current.filter((id) => id !== image.asset_id)
                          : [...current, image.asset_id],
                      )
                    }
                    aria-label={`Select ${image.display_title ?? image.name}`}
                  />
                </label>
              )}
              <div className="aspect-square overflow-hidden" style={{ background: "#101014" }}>
                {image.media_type.startsWith("image/") || image.media_type === "application/pdf" ? (
                  <img
                    src={
                      image.owner_display_name && image.asset_id
                        ? `/api/vault-master/commons/shared-with-me/${image.asset_id}/preview`
                        : image.thumbnail_url
                    }
                    alt={image.display_title ?? getPhotoTitle(image.name)}
                    loading="lazy"
                    className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.025]"
                  />
                ) : (
                  <div
                    className="flex h-full items-center justify-center text-xs"
                    style={{ color: "var(--pv-text-dim)" }}
                  >
                    No preview
                  </div>
                )}
              </div>
              <div className="p-3">
                <p className="text-sm font-medium truncate" style={{ color: "var(--pv-silver)" }}>
                  {formatPhotoDate(image.captured_on)}
                </p>
                {image.owner_display_name && (
                  <p className="text-xs mt-1 truncate" style={{ color: "var(--pv-text-dim)" }}>
                    Shared by {image.owner_display_name}
                  </p>
                )}
                {image.location && (
                  <p className="text-xs mt-1 truncate" style={{ color: "var(--pv-text-dim)" }}>
                    {image.location}
                  </p>
                )}
              </div>
            </Link>
          ))}
        </div>
      )}

      {!error &&
        images &&
        images.length > 1 &&
        typeof document !== "undefined" &&
        createPortal(
          <aside
            className={`fixed right-1 top-1/2 z-[60] -translate-y-1/2 touch-none select-none rounded-full border shadow-2xl backdrop-blur-xl transition-[width,padding] duration-200 md:right-4 ${scrubbing ? "w-28 px-4 py-4" : "w-12 px-2 py-3"}`}
            style={{
              color: "var(--pv-gold)",
              borderColor: "rgba(215,185,104,0.36)",
              background: "linear-gradient(145deg, rgba(255,255,255,0.13), rgba(12,12,16,0.62))",
              boxShadow: "0 12px 40px rgba(0,0,0,0.42), inset 0 1px rgba(255,255,255,0.16)",
            }}
            aria-label="Gallery timeline"
          >
            <div className="mb-2 flex items-center justify-center gap-1 text-[10px] whitespace-nowrap">
              <CalendarDays size={12} />
              <span className={scrubbing ? "block" : "sr-only"}>{timelineLabel}</span>
            </div>
            <div
              ref={timelineTrack}
              role="slider"
              tabIndex={0}
              aria-valuemin={0}
              aria-valuemax={images.length - 1}
              aria-valuenow={timelineIndex}
              aria-valuetext={timelineLabel}
              aria-label={`Gallery timeline, ${timelineLabel}`}
              className="relative mx-auto h-48 w-5 cursor-ns-resize rounded-full outline-none focus-visible:ring-2 focus-visible:ring-[var(--pv-gold)]"
              onPointerDown={(event) => {
                event.currentTarget.setPointerCapture(event.pointerId);
                setScrubbing(true);
                scrubFromPointer(event.clientY);
              }}
              onPointerMove={(event) => {
                if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                  scrubFromPointer(event.clientY);
                }
              }}
              onPointerUp={(event) => {
                scrubFromPointer(event.clientY);
                event.currentTarget.releasePointerCapture(event.pointerId);
                setScrubbing(false);
              }}
              onPointerCancel={() => setScrubbing(false)}
              onKeyDown={(event) => {
                if (event.key === "ArrowUp") scrubTo(timelineIndex - 1);
                else if (event.key === "ArrowDown") scrubTo(timelineIndex + 1);
                else if (event.key === "Home") scrubTo(0);
                else if (event.key === "End") scrubTo(images.length - 1);
                else return;
                event.preventDefault();
              }}
            >
              <span
                className="absolute inset-x-[8px] inset-y-0 rounded-full"
                style={{ background: "rgba(255,255,255,0.18)" }}
              />
              <span
                className="absolute left-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border"
                style={{
                  top: `${(timelineIndex / (images.length - 1)) * 100}%`,
                  background: "var(--pv-gold)",
                  borderColor: "rgba(255,255,255,0.55)",
                  boxShadow: "0 0 12px rgba(215,185,104,0.7)",
                }}
              />
            </div>
          </aside>,
          document.body,
        )}
    </div>
  );
}

function ActiveGalleryFilters({
  terms,
  photoTypes,
  contentTags,
  people,
  selectedPeople,
  onChange,
}: {
  terms: GalleryIntelligenceTerm[];
  photoTypes: string[];
  contentTags: string[];
  people: Array<{ id: string; display_name: string }>;
  selectedPeople: string[];
  onChange: (photoTypes: string[], contentTags: string[], people?: string[]) => Promise<void>;
}) {
  const active = [
    ...photoTypes.map((slug) => ["photo_type", slug] as const),
    ...contentTags.map((slug) => ["content_tag", slug] as const),
    ...selectedPeople.map((id) => ["person", id] as const),
  ];
  return active.map(([namespace, slug]) => {
    const label =
      (namespace === "person"
        ? people.find((person) => person.id === slug)?.display_name
        : terms.find((term) => term.namespace === namespace && term.slug === slug)?.display_name) ??
      slug;
    return (
      <button
        key={`${namespace}:${slug}`}
        type="button"
        className="rounded-full px-2 py-1 text-xs"
        style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
        onClick={() =>
          void onChange(
            namespace === "photo_type" ? photoTypes.filter((value) => value !== slug) : photoTypes,
            namespace === "content_tag"
              ? contentTags.filter((value) => value !== slug)
              : contentTags,
            namespace === "person"
              ? selectedPeople.filter((value) => value !== slug)
              : selectedPeople,
          )
        }
      >
        {label} ×
      </button>
    );
  });
}

function GalleryFilters({
  terms,
  photoTypes,
  contentTags,
  people,
  selectedPeople,
  onChange,
}: {
  terms: GalleryIntelligenceTerm[];
  photoTypes: string[];
  contentTags: string[];
  people: Array<{ id: string; display_name: string; active: boolean }>;
  selectedPeople: string[];
  onChange: (photoTypes: string[], contentTags: string[], people?: string[]) => Promise<void>;
}) {
  const [peopleOpen, setPeopleOpen] = useState(false);
  const [personSearch, setPersonSearch] = useState("");
  const toggle = (namespace: "photo_type" | "content_tag", slug: string, checked: boolean) => {
    const selected = namespace === "photo_type" ? photoTypes : contentTags;
    const next = checked ? [...selected, slug] : selected.filter((value) => value !== slug);
    void onChange(
      namespace === "photo_type" ? next : photoTypes,
      namespace === "content_tag" ? next : contentTags,
    );
  };
  const groups: Array<["photo_type" | "content_tag", string]> = [
    ["photo_type", "Photo type"],
    ["content_tag", "Tags"],
  ];

  return (
    <details className="relative">
      <summary
        className="inline-flex cursor-pointer list-none items-center gap-2 rounded-md px-3 py-2 text-xs"
        style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
      >
        <Filter size={13} />
        Filter
        {photoTypes.length + contentTags.length + selectedPeople.length
          ? ` (${photoTypes.length + contentTags.length + selectedPeople.length})`
          : ""}
      </summary>
      <div
        className="absolute left-0 z-30 mt-2 w-72 space-y-4 rounded-md p-4 shadow-xl"
        style={{ background: "var(--pv-panel)", border: "1px solid var(--pv-border)" }}
      >
        {groups.map(([namespace, label]) => (
          <fieldset key={namespace} className="space-y-2">
            <legend className="text-xs font-medium" style={{ color: "var(--pv-silver)" }}>
              {label}
            </legend>
            <div className="grid grid-cols-2 gap-2">
              {terms
                .filter((term) => term.namespace === namespace)
                .map((term) => {
                  const selected = (namespace === "photo_type" ? photoTypes : contentTags).includes(
                    term.slug,
                  );
                  return (
                    <label
                      key={term.slug}
                      className="flex items-center gap-2 text-xs"
                      style={{ color: "var(--pv-text-dim)" }}
                    >
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={(event) => toggle(namespace, term.slug, event.target.checked)}
                      />
                      {term.display_name}
                    </label>
                  );
                })}
            </div>
          </fieldset>
        ))}
        <div className="space-y-2">
          <button
            type="button"
            className="flex w-full items-center justify-between text-xs font-medium"
            style={{ color: "var(--pv-silver)" }}
            aria-expanded={peopleOpen}
            onClick={() => setPeopleOpen((open) => !open)}
          >
            People
            <span style={{ color: "var(--pv-text-dim)" }}>{selectedPeople.length || "Select"}</span>
          </button>
          {peopleOpen ? (
            <div
              className="space-y-2 rounded-md p-2"
              style={{ border: "1px solid var(--pv-border)" }}
            >
              <input
                aria-label="Search People"
                className="pv-input !w-full !py-2 text-xs"
                placeholder="Search People"
                value={personSearch}
                onChange={(event) => setPersonSearch(event.target.value)}
              />
              <div className="max-h-48 space-y-1 overflow-y-auto">
                {people
                  .filter((entry) => entry.active)
                  .filter((entry) =>
                    entry.display_name
                      .toLocaleLowerCase()
                      .includes(personSearch.toLocaleLowerCase()),
                  )
                  .sort((left, right) => left.display_name.localeCompare(right.display_name))
                  .map((entry) => (
                    <label
                      key={entry.id}
                      className="flex items-center gap-2 text-xs"
                      style={{ color: "var(--pv-text-dim)" }}
                    >
                      <input
                        type="checkbox"
                        checked={selectedPeople.includes(entry.id)}
                        onChange={(event) =>
                          void onChange(
                            photoTypes,
                            contentTags,
                            event.target.checked
                              ? [...selectedPeople, entry.id]
                              : selectedPeople.filter((id) => id !== entry.id),
                          )
                        }
                      />
                      {entry.display_name}
                    </label>
                  ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </details>
  );
}
