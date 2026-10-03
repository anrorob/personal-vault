import { GalleryControls } from "@/components/pv/GalleryControls";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { Filter, Image as ImageIcon, RefreshCw } from "lucide-react";
import { GallerySortMenu } from "@/components/pv/GallerySortMenu";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  galleryReturnPosition,
  rememberGalleryPosition,
  type GalleryPeriod,
  DEFAULT_GALLERY_SORT,
  formatPhotoDate,
  getPhotoTitle,
  parseGalleryFilter,
  parseGallerySortOrder,
  type GalleryIntelligenceTerm,
  type GalleryCustomTag,
  type GalleryImage,
  type GallerySortOrder,
} from "@/lib/gallery";
import { ActionProgress } from "@/components/pv/ActionProgress";
import { authorizeHiddenPhotos } from "@/lib/passkeys";

export const Route = createFileRoute("/app/gallery/")({
  validateSearch: (search: Record<string, unknown>) => ({
    sort: parseGallerySortOrder(search.sort),
    photo_type: parseGalleryFilter(search.photo_type),
    content_tag: parseGalleryFilter(search.content_tag),
    person: parseGalleryFilter(search.person),
    private_tag: parseGalleryFilter(search.private_tag),
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
  const { sort, photo_type, content_tag, person, private_tag } = Route.useSearch();
  const [people, setPeople] = useState<
    Array<{ id: string; display_name: string; active: boolean }>
  >([]);
  const [images, setImages] = useState<GalleryImage[] | null>(null);
  const [returnPosition] = useState(galleryReturnPosition);
  const restorePending = useRef(returnPosition);
  const [seek, setSeek] = useState<string | null>(null);
  const seekQuery = useRef("");
  const [periods, setPeriods] = useState<GalleryPeriod[]>([]);
  const [previousCursor, setPreviousCursor] = useState<string | null>(null);
  const previousSentinel = useRef<HTMLDivElement | null>(null);
  const prependPosition = useRef<{ id: string; offset: number } | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingNext, setLoadingNext] = useState(false);
  const galleryQuery = useRef("");
  const galleryGeneration = useRef(0);
  const continuationRequest = useRef<AbortController | null>(null);
  const continuationBusy = useRef(false);
  const continuationSentinel = useRef<HTMLDivElement | null>(null);
  const galleryGrid = useRef<HTMLDivElement | null>(null);
  const [gridWidth, setGridWidth] = useState(0);
  const [gridTop, setGridTop] = useState(0);
  const [viewport, setViewport] = useState(() => ({
    top: typeof window === "undefined" ? 0 : window.scrollY,
    height: typeof window === "undefined" ? 900 : window.innerHeight,
    width: typeof window === "undefined" ? 1200 : window.innerWidth,
  }));
  const [privateTags, setPrivateTags] = useState<GalleryCustomTag[]>([]);
  const [terms, setTerms] = useState<GalleryIntelligenceTerm[]>([]);
  const [canBackfill, setCanBackfill] = useState(false);
  const [backfillStatus, setBackfillStatus] = useState<string | null>(null);
  const [bulkProgress, setBulkProgress] = useState<GalleryIntelligenceBulkProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stateReady, setStateReady] = useState(false);
  const [selectionMode, setSelectionModeState] = useState(false);
  const setSelectionMode = (active: boolean) => {
    const { scrollX, scrollY } = window;
    setSelectionModeState(active);
    window.requestAnimationFrame(() =>
      window.scrollTo({ left: scrollX, top: scrollY, behavior: "instant" }),
    );
  };
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [shareOpen, setShareOpen] = useState(false);
  const shareTrigger = useRef<HTMLButtonElement>(null);
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
  const [lifecycleBusy, setLifecycleBusy] = useState(false);
  const [shareError, setShareError] = useState<string | null>(null);
  const [includeSharedPhotos, setIncludeSharedPhotos] = useState(false);
  const [includeHidden, setIncludeHidden] = useState(returnPosition?.hidden ?? false);
  const [galleryVersion, setGalleryVersion] = useState(0);
  const [sharedCollections, setSharedCollections] = useState<
    Array<{ collection_id: string; name: string; owner_display_name: string; included: boolean }>
  >([]);
  const openingPhoto = useRef(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

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
        // Saved server state retains the sort preference; detail-return position
        // is scoped to this navigation session instead of stale prior visits.
        if (!new URLSearchParams(window.location.search).has("sort") && state.sort !== sort) {
          await navigate({
            to: "/app/gallery",
            search: {
              sort: state.sort,
              photo_type: [],
              content_tag: [],
              person: [],
              private_tag: [],
            },
            replace: true,
          });
        }
      })
      .catch(() => setError("Your Gallery preferences could not be opened."))
      .finally(() => setStateReady(true));
  }, [navigate, sort]);

  const refreshPrivateTags = useCallback(async () => {
    try {
      const response = await fetch("/api/gallery/custom-tags", {
        credentials: "include",
        cache: "no-store",
      });
      if (!response.ok) throw new Error();
      setPrivateTags((await response.json()) as GalleryCustomTag[]);
    } catch {
      setPrivateTags([]);
      setError("Your private tags could not be loaded.");
    }
  }, []);

  useEffect(() => {
    void refreshPrivateTags();
    const refresh = () => void refreshPrivateTags();
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [refreshPrivateTags]);

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
    const generation = ++galleryGeneration.current;
    continuationRequest.current?.abort();
    continuationBusy.current = false;
    prependPosition.current = null;
    setNextCursor(null);
    setPreviousCursor(null);
    setLoadingNext(false);

    const loadGallery = async () => {
      try {
        const query = new URLSearchParams({ sort });
        if (includeHidden) query.set("include_hidden", "true");
        photo_type.forEach((value) => query.append("photo_type", value));
        content_tag.forEach((value) => query.append("content_tag", value));
        person.forEach((value) => query.append("person", value));
        private_tag.forEach((value) => query.append("private_tag", value));
        if (galleryQuery.current !== query.toString()) setPeriods([]);
        galleryQuery.current = query.toString();
        const position = restorePending.current;
        if (position && position.query !== galleryQuery.current) restorePending.current = null;
        void fetch(`/api/gallery/chronology?${galleryQuery.current}`, {
          credentials: "include",
          signal: controller.signal,
        })
          .then(async (response) => {
            if (!response.ok) throw new Error();
            const result = (await response.json()) as GalleryPeriod[];
            if (!controller.signal.aborted && generation === galleryGeneration.current)
              setPeriods(result);
          })
          .catch(() => {
            if (!controller.signal.aborted && generation === galleryGeneration.current)
              setPeriods([]);
          });
        if (restorePending.current) query.set("anchor_asset_id", restorePending.current.assetId);
        else if (seek && seekQuery.current === galleryQuery.current) query.set("start", seek);
        const response = await fetch(`/api/gallery/pages?${query}`, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });

        if (response.status === 401) {
          await navigate({ to: "/login" });
          return;
        }

        if (response.status === 404 && restorePending.current) {
          restorePending.current = null;
          setGalleryVersion((version) => version + 1);
          return;
        }
        if (!response.ok) {
          throw new Error("Gallery request failed");
        }

        const page = (await response.json()) as {
          items: GalleryImage[];
          next_cursor: string | null;
          previous_cursor?: string | null;
          seek_anchor_id?: string | null;
        };
        if (controller.signal.aborted || generation !== galleryGeneration.current) return;
        if (
          restorePending.current &&
          !page.items.some((item) => item.id === restorePending.current!.photoId)
        ) {
          restorePending.current = null;
        }
        setImages(page.items);
        setNextCursor(page.next_cursor);
        setPreviousCursor(page.previous_cursor ?? null);
        if (!restorePending.current && page.seek_anchor_id) {
          const anchor =
            page.items.find((item) => item.id === page.seek_anchor_id) ?? page.items[0];
          prependPosition.current = anchor ? { id: anchor.id, offset: 80 } : null;
        } else if (!restorePending.current) window.scrollTo({ top: 0, behavior: "instant" });
        setError(null);
      } catch (requestError) {
        if (controller.signal.aborted || generation !== galleryGeneration.current) return;
        if (requestError instanceof DOMException && requestError.name === "AbortError") {
          return;
        }

        setError("The Gallery is currently unavailable.");
      }
    };

    void loadGallery();
    return () => {
      controller.abort();
      continuationRequest.current?.abort();
    };
  }, [
    content_tag,
    galleryVersion,
    includeHidden,
    navigate,
    person,
    photo_type,
    private_tag,
    sort,
    stateReady,
    seek,
  ]);

  useEffect(() => {
    const sentinel = continuationSentinel.current;
    if (!sentinel || !nextCursor || loadingNext || !images) return;
    const generation = galleryGeneration.current;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (
          !entry.isIntersecting ||
          continuationBusy.current ||
          restorePending.current ||
          prependPosition.current
        )
          return;
        observer.disconnect();
        const controller = new AbortController();
        continuationRequest.current = controller;
        continuationBusy.current = true;
        setLoadingNext(true);
        const query = new URLSearchParams(galleryQuery.current);
        query.set("cursor", nextCursor);
        void fetch(`/api/gallery/pages?${query}`, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        })
          .then(async (response) => {
            if (!response.ok) throw new Error("Gallery continuation failed");
            return (await response.json()) as {
              items: GalleryImage[];
              next_cursor: string | null;
            };
          })
          .then((page) => {
            if (controller.signal.aborted || generation !== galleryGeneration.current) return;
            setImages((current) => {
              const seen = new Set(current?.map((image) => image.id));
              return [...(current ?? []), ...page.items.filter((image) => !seen.has(image.id))];
            });
            setNextCursor(page.next_cursor === nextCursor ? null : page.next_cursor);
          })
          .catch((reason) => {
            if (reason instanceof DOMException && reason.name === "AbortError") return;
            if (generation !== galleryGeneration.current) return;
            setNextCursor(null);
            setError("The Gallery is currently unavailable.");
          })
          .finally(() => {
            if (generation === galleryGeneration.current) {
              continuationBusy.current = false;
              setLoadingNext(false);
            }
          });
      },
      { rootMargin: "700px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [images, loadingNext, nextCursor]);

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

  useEffect(() => {
    const sentinel = previousSentinel.current;
    if (!sentinel || !previousCursor || loadingNext || !images) return;
    const generation = galleryGeneration.current;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (
          !entry.isIntersecting ||
          window.scrollY < 1 ||
          continuationBusy.current ||
          restorePending.current ||
          prependPosition.current
        )
          return;
        observer.disconnect();
        const controller = new AbortController();
        continuationRequest.current = controller;
        continuationBusy.current = true;
        setLoadingNext(true);
        const query = new URLSearchParams(galleryQuery.current);
        query.set("before", previousCursor);
        const anchor = currentAnchor();
        void fetch(`/api/gallery/pages?${query}`, {
          credentials: "include",
          signal: controller.signal,
        })
          .then(async (response) => {
            if (!response.ok) throw new Error();
            return (await response.json()) as {
              items: GalleryImage[];
              previous_cursor: string | null;
            };
          })
          .then((page) => {
            if (controller.signal.aborted || generation !== galleryGeneration.current) return;
            if (anchor.anchor_id)
              prependPosition.current = { id: anchor.anchor_id, offset: anchor.anchor_offset };
            setImages((current) => {
              const seen = new Set(current?.map((image) => image.id));
              return [...page.items.filter((image) => !seen.has(image.id)), ...(current ?? [])];
            });
            setPreviousCursor(
              page.previous_cursor === previousCursor ? null : page.previous_cursor,
            );
          })
          .catch(() => {
            if (!controller.signal.aborted && generation === galleryGeneration.current)
              setError("The Gallery is currently unavailable.");
          })
          .finally(() => {
            if (generation === galleryGeneration.current) {
              continuationBusy.current = false;
              setLoadingNext(false);
            }
          });
      },
      { rootMargin: "0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [images, previousCursor, loadingNext, currentAnchor]);

  useEffect(() => {
    if (!images?.length || !galleryGrid.current) return;
    const root = galleryGrid.current;
    let frame = 0;
    const measure = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        setGridWidth(root.clientWidth);
        setGridTop(window.scrollY + root.getBoundingClientRect().top);
        setViewport({ top: window.scrollY, height: window.innerHeight, width: window.innerWidth });
      });
    };
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    observer?.observe(root);
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, { passive: true });
    measure();
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure);
      window.cancelAnimationFrame(frame);
    };
  }, [images?.length]);

  const gridColumns = viewport.width >= 1280 ? 4 : viewport.width >= 768 ? 3 : 2;
  const rowLayout = useMemo(() => {
    const count = Math.ceil((images?.length ?? 0) / gridColumns);
    const cardWidth = gridWidth > 0 ? (gridWidth - (gridColumns - 1) * 16) / gridColumns : 300;
    const heights: number[] = [];
    const offsets = [0];
    for (let row = 0; row < count; row++) {
      const cards = images!.slice(row * gridColumns, (row + 1) * gridColumns);
      const extraLines = Math.max(
        0,
        ...cards.map(
          (card) => Number(Boolean(card.owner_display_name)) + Number(Boolean(card.location)),
        ),
      );
      const height = cardWidth + 46 + extraLines * 20;
      heights.push(height);
      offsets.push(offsets.at(-1)! + height + 16);
    }
    return { heights, offsets, total: count ? offsets[count] - 16 : 0 };
  }, [images, gridColumns, gridWidth]);

  const rowAt = useCallback(
    (position: number) => {
      let low = 0;
      let high = rowLayout.heights.length;
      while (low < high) {
        const middle = (low + high) >> 1;
        if (rowLayout.offsets[middle + 1] < position) low = middle + 1;
        else high = middle;
      }
      return Math.min(low, Math.max(0, rowLayout.heights.length - 1));
    },
    [rowLayout],
  );
  const visibleStartRow = Math.max(0, rowAt(viewport.top - gridTop) - 5);
  const visibleEndRow = Math.min(
    rowLayout.heights.length,
    rowAt(viewport.top - gridTop + viewport.height) + 6,
  );
  const visibleStartIndex = visibleStartRow * gridColumns;
  const visibleImages = images?.slice(visibleStartIndex, visibleEndRow * gridColumns) ?? [];
  const topSpacer = rowLayout.offsets[visibleStartRow] ?? 0;
  const bottomSpacer = Math.max(
    0,
    rowLayout.total - (rowLayout.offsets[visibleEndRow] ?? 0) + (visibleEndRow ? 16 : 0),
  );

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
    const target = restorePending.current
      ? { id: restorePending.current.photoId, offset: restorePending.current.offset }
      : prependPosition.current;
    if (!target || !images?.length || !gridWidth || !galleryGrid.current) return;
    const index = images.findIndex((image) => image.id === target.id);
    if (index < 0) return;
    const top =
      window.scrollY +
      galleryGrid.current.getBoundingClientRect().top +
      rowLayout.offsets[Math.floor(index / gridColumns)] -
      target.offset;
    // Scroll the virtual row into the mounted window, then correct to the actual
    // card after layout and router scroll handling have both completed.
    window.scrollTo({ top: Math.max(0, top), behavior: "instant" });
    let second = 0;
    const frame = window.requestAnimationFrame(() => {
      second = window.requestAnimationFrame(() => {
        const card = document.querySelector<HTMLElement>(
          `[data-gallery-id="${CSS.escape(target.id)}"]`,
        );
        if (!card) return;
        window.scrollBy(0, card.getBoundingClientRect().top - target.offset);
        restorePending.current = null;
        prependPosition.current = null;
      });
    });
    return () => {
      window.cancelAnimationFrame(frame);
      window.cancelAnimationFrame(second);
    };
  }, [images, gridWidth, gridColumns, rowLayout, viewport.top]);

  const jumpToPeriod = (start: string) => {
    restorePending.current = null;
    prependPosition.current = null;
    setSelectedIds([]);
    seekQuery.current = galleryQuery.current;
    setSeek(start);
    setGalleryVersion((version) => version + 1);
  };

  const changeSort = async (nextSort: GallerySortOrder) => {
    if (nextSort === sort) return;

    restorePending.current = null;
    setSeek(null);
    window.scrollTo({ top: 0 });
    if (saveTimer.current) clearTimeout(saveTimer.current);
    await fetch("/api/user-state/gallery", {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sort: nextSort, anchor_id: null, anchor_offset: 0 }),
    });
    await navigate({
      to: "/app/gallery",
      search: { sort: nextSort, photo_type, content_tag, person, private_tag },
    });
  };

  const changeFilters = async (
    nextPhotoTypes: string[],
    nextContentTags: string[],
    nextPeople = person,
    nextPrivateTags = private_tag,
  ) => {
    restorePending.current = null;
    setSeek(null);
    window.scrollTo({ top: 0 });
    await navigate({
      to: "/app/gallery",
      search: {
        sort,
        photo_type: nextPhotoTypes,
        content_tag: nextContentTags,
        person: nextPeople,
        private_tag: nextPrivateTags,
      },
    });
  };

  const setSharedPreference = async (enabled: boolean) => {
    try {
      const response = await fetch("/api/gallery/shared-preference", {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ include_shared_photos: enabled }),
      });
      if (!response.ok) throw new Error("Shared photo preferences could not be saved.");
      setIncludeSharedPhotos(enabled);
      restorePending.current = null;
      setSeek(null);
      setGalleryVersion((version) => version + 1);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Shared photo preferences could not be saved.",
      );
    }
  };

  const setCollectionInclusion = async (collectionId: string, included: boolean) => {
    try {
      const response = await fetch(`/api/gallery/shared-collections/${collectionId}/inclusion`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ included }),
      });
      if (!response.ok) throw new Error("Shared collection preference could not be saved.");
      setSharedCollections((current) =>
        current.map((collection) =>
          collection.collection_id === collectionId ? { ...collection, included } : collection,
        ),
      );
      restorePending.current = null;
      setSeek(null);
      setGalleryVersion((version) => version + 1);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Shared collection preference could not be saved.",
      );
    }
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
    if (selectedIds.length === 0 || lifecycleBusy) return;
    setLifecycleBusy(true);
    setError(null);
    const results = await Promise.allSettled(
      selectedIds.map(async (assetId) => {
        const response = await fetch(`/api/vault-master/assets/${assetId}/lifecycle/${action}`, {
          method: "POST",
          credentials: "include",
        });
        if (!response.ok) {
          const body = (await response.json().catch(() => ({}))) as { detail?: string };
          throw new Error(body.detail ?? "Only the owner can change this item.");
        }
        return assetId;
      }),
    );
    const successfulIds = new Set(
      results.flatMap((result) => (result.status === "fulfilled" ? [result.value] : [])),
    );
    // Both Hide and Restore remove successes from their originating lifecycle scope.
    setImages(
      (current) =>
        current?.filter((image) => !image.asset_id || !successfulIds.has(image.asset_id)) ??
        current,
    );
    setSelectedIds((current) => current.filter((id) => !successfulIds.has(id)));
    const failure = results.find((result) => result.status === "rejected");
    if (failure?.status === "rejected") {
      setError(
        failure.reason instanceof Error
          ? failure.reason.message
          : "Gallery visibility could not be changed.",
      );
    } else {
      setSelectionMode(false);
    }
    setLifecycleBusy(false);
  };

  return (
    <div className="pv-layout-container max-w-7xl mx-auto space-y-6">
      <GalleryControls
        actions={
          <>
            <GalleryFilters
              onOpen={() => void refreshPrivateTags()}
              terms={terms}
              photoTypes={photo_type}
              contentTags={content_tag}
              people={people}
              selectedPeople={person}
              privateTags={privateTags}
              selectedPrivateTags={private_tag}
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
                  <div
                    className="space-y-2 border-t pt-3"
                    style={{ borderColor: "var(--pv-border)" }}
                  >
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
                            void setCollectionInclusion(
                              collection.collection_id,
                              event.target.checked,
                            )
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
                setSelectedIds([]);
                setSelectionMode(false);
                if (includeHidden) {
                  setImages(null);
                  setError(null);
                  restorePending.current = null;
                  setSeek(null);
                  setIncludeHidden(false);
                  return;
                }
                void authorizeHiddenPhotos()
                  .then(() => {
                    setImages(null);
                    setError(null);
                    restorePending.current = null;
                    setSeek(null);
                    setIncludeHidden(true);
                  })
                  .catch((reason) =>
                    setError(
                      reason instanceof Error
                        ? reason.message
                        : "Hidden Photos identity confirmation failed.",
                    ),
                  );
              }}
            >
              {includeHidden ? "Hidden content" : "View Hidden"}
            </button>
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
          </>
        }
        sort={
          <GallerySortMenu
            key={galleryQuery.current}
            sort={sort}
            onSort={(value) => void changeSort(value)}
            periods={periods}
            onJump={jumpToPeriod}
          />
        }
        selection={
          selectionMode ? (
            <div
              className="flex min-h-11 w-full flex-wrap items-center gap-2"
              aria-label="Gallery selection controls"
            >
              <span className="mr-auto text-xs">{selectedIds.length} selected</span>
              <button
                className="pv-btn-ghost min-h-11 !px-2 text-xs"
                onClick={() => setSelectedIds([])}
              >
                Clear
              </button>
              <button
                className="pv-btn-secondary min-h-11 !px-2 text-xs"
                ref={shareTrigger}
                disabled={selectedIds.length < 2}
                onClick={() => void openShare()}
              >
                Share
              </button>
              <button
                className="pv-btn-secondary min-h-11 !px-2 text-xs"
                disabled={!selectedIds.length || lifecycleBusy}
                onClick={() => void changeLifecycle(includeHidden ? "unhide" : "hide")}
              >
                {includeHidden ? "Restore selected" : "Hide selected"}
              </button>
              <button
                className="pv-btn-ghost min-h-11 !px-2 text-xs"
                onClick={() => {
                  setSelectionMode(false);
                  setSelectedIds([]);
                  setShareOpen(false);
                }}
              >
                Done
              </button>
            </div>
          ) : undefined
        }
      />
      <div className="flex flex-wrap items-center gap-3">
        {images &&
          (photo_type.length + content_tag.length + person.length + private_tag.length > 0 ||
            includeHidden) && (
            <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              {images.length} results
            </span>
          )}
        <ActiveGalleryFilters
          terms={terms}
          photoTypes={photo_type}
          contentTags={content_tag}
          people={people}
          selectedPeople={person}
          privateTags={privateTags}
          selectedPrivateTags={private_tag}
          onChange={changeFilters}
        />
        {(photo_type.length > 0 ||
          content_tag.length > 0 ||
          person.length > 0 ||
          private_tag.length > 0) && (
          <button
            type="button"
            className="text-xs"
            style={{ color: "var(--pv-gold)" }}
            onClick={() => void changeFilters([], [], [], [])}
          >
            Clear filters
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

      <Dialog open={shareOpen} onOpenChange={setShareOpen}>
        <DialogContent
          className="max-h-[calc(100dvh-2rem)] overflow-y-auto"
          aria-describedby={undefined}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            shareTrigger.current?.focus({ preventScroll: true });
          }}
        >
          <div>
            <DialogTitle>Share {selectedIds.length} selected items</DialogTitle>
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
        </DialogContent>
      </Dialog>

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
            {includeHidden
              ? "No hidden photos"
              : photo_type.length + content_tag.length + person.length + private_tag.length > 0
                ? "No photos match these filters"
                : "Gallery is empty"}
          </h3>
          <p className="text-xs mt-2" style={{ color: "var(--pv-text-dim)" }}>
            {includeHidden
              ? "Hidden photos will appear here."
              : photo_type.length + content_tag.length + person.length + private_tag.length > 0
                ? "Try changing or clearing your filters."
                : "Move staged images from the Arrival Hall into the Gallery library to display them here."}
          </p>
        </div>
      )}

      {images && images.length > 0 && (
        <div ref={galleryGrid} data-gallery-grid>
          {previousCursor && <div ref={previousSentinel} aria-hidden="true" />}
          {topSpacer > 0 && <div aria-hidden="true" style={{ height: topSpacer }} />}
          <div className="pv-card-grid">
            {visibleImages.map((image, localIndex) => (
              <Link
                key={image.id}
                to="/app/gallery/$photoId"
                params={{ photoId: image.id }}
                search={{
                  sort,
                  photo_type,
                  content_tag,
                  person,
                  private_tag,
                  hidden: includeHidden,
                }}
                onClick={(event) => {
                  if (selectionMode && image.asset_id) {
                    event.preventDefault();
                    setSelectedIds((current) =>
                      current.includes(image.asset_id)
                        ? current.filter((id) => id !== image.asset_id)
                        : [...current, image.asset_id],
                    );
                    return;
                  }
                  // Router navigation resets the document scroll position. Lock the
                  // clicked position so that reset cannot overwrite our return point.
                  rememberGalleryPosition({
                    assetId: image.asset_id,
                    photoId: image.id,
                    query: galleryQuery.current,
                    hidden: includeHidden,
                    offset: Math.round(event.currentTarget.getBoundingClientRect().top),
                  });
                  openingPhoto.current = true;
                  persistState(sort, true);
                }}
                data-gallery-id={image.id}
                data-gallery-index={visibleStartIndex + localIndex}
                className="pv-panel pv-panel-hover relative overflow-hidden group"
                style={{
                  minHeight:
                    rowLayout.heights[Math.floor((visibleStartIndex + localIndex) / gridColumns)],
                }}
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
                  {image.media_type.startsWith("image/") ||
                  image.media_type === "application/pdf" ? (
                    <img
                      src={image.thumbnail_url}
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
          {bottomSpacer > 0 && <div aria-hidden="true" style={{ height: bottomSpacer }} />}
        </div>
      )}

      {images && nextCursor && <div ref={continuationSentinel} aria-hidden="true" />}
    </div>
  );
}

function ActiveGalleryFilters({
  terms,
  photoTypes,
  contentTags,
  people,
  selectedPeople,
  privateTags,
  selectedPrivateTags,
  onChange,
}: {
  terms: GalleryIntelligenceTerm[];
  photoTypes: string[];
  contentTags: string[];
  people: Array<{ id: string; display_name: string }>;
  selectedPeople: string[];
  privateTags: GalleryCustomTag[];
  selectedPrivateTags: string[];
  onChange: (
    photoTypes: string[],
    contentTags: string[],
    people?: string[],
    privateTags?: string[],
  ) => Promise<void>;
}) {
  const active = [
    ...photoTypes.map((slug) => ["photo_type", slug] as const),
    ...contentTags.map((slug) => ["content_tag", slug] as const),
    ...selectedPeople.map((id) => ["person", id] as const),
    ...selectedPrivateTags.map((id) => ["private_tag", id] as const),
  ];
  return active.map(([namespace, slug]) => {
    const label =
      (namespace === "private_tag"
        ? `Private: ${privateTags.find((tag) => tag.id === slug)?.display_name ?? "Unavailable tag"}`
        : namespace === "person"
          ? people.find((person) => person.id === slug)?.display_name
          : terms.find((term) => term.namespace === namespace && term.slug === slug)
              ?.display_name) ?? slug;
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
            namespace === "private_tag"
              ? selectedPrivateTags.filter((id) => id !== slug)
              : selectedPrivateTags,
          )
        }
      >
        {label} ×
      </button>
    );
  });
}

export function GalleryFilters({
  onOpen,
  terms,
  photoTypes,
  contentTags,
  people,
  selectedPeople,
  privateTags,
  selectedPrivateTags,
  onChange,
}: {
  onOpen?: () => void;
  terms: GalleryIntelligenceTerm[];
  photoTypes: string[];
  contentTags: string[];
  people: Array<{ id: string; display_name: string; active: boolean }>;
  selectedPeople: string[];
  privateTags: GalleryCustomTag[];
  selectedPrivateTags: string[];
  onChange: (
    photoTypes: string[],
    contentTags: string[],
    people?: string[],
    privateTags?: string[],
  ) => Promise<void>;
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
    <details
      className="relative"
      onToggle={(event) => {
        if (event.currentTarget.open) onOpen?.();
      }}
    >
      <summary
        className="inline-flex cursor-pointer list-none items-center gap-2 rounded-md px-3 py-2 text-xs"
        style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
      >
        <Filter size={13} />
        Filter
        {photoTypes.length + contentTags.length + selectedPeople.length + selectedPrivateTags.length
          ? ` (${photoTypes.length + contentTags.length + selectedPeople.length + selectedPrivateTags.length})`
          : ""}
      </summary>
      <div
        className="absolute left-0 z-30 mt-2 max-h-[calc(100dvh-10rem)] w-72 space-y-4 overflow-x-hidden overflow-y-auto rounded-md p-4 shadow-xl"
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
        <fieldset className="space-y-2">
          <legend className="text-xs font-medium" style={{ color: "var(--pv-silver)" }}>
            My private tags
          </legend>
          <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Visible only to you.
          </p>
          {privateTags.map((tag) => (
            <label
              key={tag.id}
              className="flex items-center gap-2 text-xs"
              style={{ color: "var(--pv-text-dim)" }}
            >
              <input
                type="checkbox"
                checked={selectedPrivateTags.includes(tag.id)}
                onChange={(event) =>
                  void onChange(
                    photoTypes,
                    contentTags,
                    selectedPeople,
                    event.target.checked
                      ? [...selectedPrivateTags, tag.id]
                      : selectedPrivateTags.filter((id) => id !== tag.id),
                  )
                }
              />
              {tag.display_name}
            </label>
          ))}
          {!privateTags.length && (
            <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              No private tags yet.
            </p>
          )}
        </fieldset>
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
              <div className="space-y-1">
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
