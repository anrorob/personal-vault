import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import {
  BrainCircuit,
  Database,
  File,
  Folder,
  Inbox,
  Pencil,
  Pause,
  Play,
  BookOpen,
  RefreshCw,
  Search,
  Trash2,
  Undo2,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { formatPhotoDate } from "@/lib/gallery";
import { getAuthSession } from "@/lib/auth";
import {
  encodeArrivalHallPath,
  formatBytes,
  type AssetRelationshipCandidateListing,
  type ArrivalHallListing,
  type AutopilotListing,
  type CanonicalAssetRelationshipListing,
  type IngestionAiEvidenceListing,
  type IngestionAiItemEvidence,
  type IngestionAnalysisBatchListing,
  type PermanentDeletionPreflight,
  type PublicationBundleListing,
  type PublicationReview,
  type SidecarRecoveryAssessment,
  type QuarantinePreflight,
  type VaultAsset,
  type VaultAssetHistory,
  type VaultAssetHistoryEntry,
  type VaultAssetSearchResult,
  type VaultMasterItem,
  type VaultMasterListing,
} from "@/lib/incoming";

export const Route = createFileRoute("/app/arrival-hall")({
  component: ArrivalHallPage,
});

type MetadataDraft = {
  display_title: string;
  captured_on: string;
  location: string;
  artist: string;
  album: string;
  album_artist: string;
  track_number: string;
  disc_number: string;
  release_year: string;
};

const GENERAL_METADATA_FIELDS = ["display_title", "captured_on", "location"] as const;
const MUSIC_METADATA_FIELDS = [
  "display_title",
  "artist",
  "album",
  "album_artist",
  "track_number",
  "disc_number",
  "release_year",
] as const;

const AUTOPILOT_POLICY_OPTIONS = [
  { content_type: "personal_photo", destination: "Gallery", label: "Personal photos to Gallery" },
  { content_type: "receipt", destination: "Documents", label: "Receipts to Documents" },
  {
    content_type: "financial_document",
    destination: "Ledger",
    label: "Financial documents to Ledger",
  },
  { content_type: "general_document", destination: "Documents", label: "Documents to Documents" },
  { content_type: "artwork", destination: "Archives", label: "Artwork to Archives" },
] as const;

function supportsSemanticAssessment(item: VaultMasterItem) {
  return item.mime_type.startsWith("image/") || item.mime_type === "application/pdf";
}

type EditableMetadataField = keyof MetadataDraft;
type ItemAction = "approving" | "rejecting" | "moving";
type BulkAction = "approve" | "reject" | "move";

type BulkActionProgress = {
  action: BulkAction;
  itemIds: string[];
};

const METADATA_OVERRIDE_LABELS: Record<EditableMetadataField, string> = {
  display_title: "Title",
  captured_on: "Date",
  location: "Location",
  artist: "Artist or band",
  album: "Album",
  album_artist: "Album artist",
  track_number: "Track",
  disc_number: "Disc",
  release_year: "Release year",
};

function correctedMetadataSummary(item: VaultMasterItem) {
  return (
    Object.entries(item.metadata_overrides) as [EditableMetadataField, string | number][]
  ).map(
    ([field, value]) =>
      `${METADATA_OVERRIDE_LABELS[field]}: ${field === "captured_on" ? formatPhotoDate(String(value)) : value}`,
  );
}

function ArrivalHallPage() {
  const navigate = useNavigate();
  const [listing, setListing] = useState<ArrivalHallListing | null>(null);
  const [vaultMaster, setVaultMaster] = useState<VaultMasterListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [savingDecision, setSavingDecision] = useState<string | null>(null);
  const [itemActions, setItemActions] = useState<Record<string, ItemAction>>({});
  const [itemActionErrors, setItemActionErrors] = useState<Record<string, string>>({});
  const [bulkActionProgress, setBulkActionProgress] = useState<BulkActionProgress | null>(null);
  const itemActionsRef = useRef(new Set<string>());
  const proposalUpdatesRef = useRef(new Set<string>());
  const loadVersionRef = useRef(0);
  const [selectedItems, setSelectedItems] = useState<Set<string>>(new Set());
  const [editingMetadata, setEditingMetadata] = useState<string | null>(null);
  const [metadataDraft, setMetadataDraft] = useState<MetadataDraft | null>(null);
  const [aiEvidence, setAiEvidence] = useState<IngestionAiEvidenceListing>({
    items: [],
    publication_rule: "private_review_evidence_only",
  });
  const [queueingAi, setQueueingAi] = useState<string | null>(null);
  const [analysisBatches, setAnalysisBatches] = useState<IngestionAnalysisBatchListing>({
    batches: [],
  });
  const [bulkAnalysisBusy, setBulkAnalysisBusy] = useState(false);
  const [autopilot, setAutopilot] = useState<AutopilotListing>({ policies: [], runs: [] });
  const [autopilotBusy, setAutopilotBusy] = useState(false);
  const [publicationBundles, setPublicationBundles] = useState<PublicationBundleListing>({
    bundles: [],
    publication_rule: "owner_review_required",
  });
  const [publicationReviews, setPublicationReviews] = useState<Record<string, PublicationReview>>(
    {},
  );
  const [publicationBusy, setPublicationBusy] = useState<string | null>(null);
  const [publicationPreparing, setPublicationPreparing] = useState<string | null>(null);
  const [reviewFilter, setReviewFilter] = useState<
    "all" | "automatic_eligible" | "batch_review" | "individual_review" | "conflicts" | "duplicates"
  >("all");
  const [isAdministrator, setIsAdministrator] = useState(false);

  useEffect(() => {
    void getAuthSession()
      .then((session) => setIsAdministrator(session.role === "administrator"))
      .catch(() => setIsAdministrator(false));
  }, []);

  function replaceVaultMasterItem(updated: VaultMasterListing["items"][number]) {
    setVaultMaster((current) =>
      current
        ? {
            items: current.items.map((item) => (item.id === updated.id ? updated : item)),
          }
        : current,
    );
  }

  const loadArrivalHall = useCallback(async () => {
    const loadVersion = ++loadVersionRef.current;
    setRefreshing(true);
    setError(null);

    try {
      const [response, vaultMasterResponse, aiResponse, batchesResponse, controlResponses] =
        await Promise.all([
          fetch("/api/arrival-hall", {
            credentials: "include",
            headers: { Accept: "application/json" },
          }),
          fetch("/api/vault-master/items", {
            credentials: "include",
            headers: { Accept: "application/json" },
          }),
          fetch("/api/vault-master/items/ai", {
            credentials: "include",
            headers: { Accept: "application/json" },
          }),
          fetch("/api/vault-master/items/ai/batches", {
            credentials: "include",
            headers: { Accept: "application/json" },
          }),
          Promise.all([
            fetch("/api/vault-master/autopilot", {
              credentials: "include",
              headers: { Accept: "application/json" },
            }),
            isAdministrator
              ? fetch("/api/vault-master/publication-bundles", {
                  credentials: "include",
                  headers: { Accept: "application/json" },
                })
              : Promise.resolve(null),
          ]),
        ]);
      const [autopilotResponse, publicationResponse] = controlResponses;

      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }

      if (
        !response.ok ||
        !vaultMasterResponse.ok ||
        !aiResponse.ok ||
        !batchesResponse.ok ||
        !autopilotResponse.ok ||
        (isAdministrator && !publicationResponse?.ok)
      ) {
        throw new Error("Arrival Hall request failed");
      }

      const nextListing = (await response.json()) as ArrivalHallListing;
      const nextVaultMaster = (await vaultMasterResponse.json()) as VaultMasterListing;
      const nextAiEvidence = (await aiResponse.json()) as IngestionAiEvidenceListing;
      const nextAnalysisBatches = (await batchesResponse.json()) as IngestionAnalysisBatchListing;
      let publications: PublicationBundleListing = {
        bundles: [],
        publication_rule: "owner_review_required",
      };
      const nextAutopilot = (await autopilotResponse.json()) as AutopilotListing;
      if (publicationResponse) {
        publications = (await publicationResponse.json()) as PublicationBundleListing;
      }
      const reviewEntries = await Promise.all(
        publications.bundles
          .flatMap((bundle) => bundle.source_item_ids)
          .map(async (sourceId) => {
            const reviewResponse = await fetch(
              `/api/vault-master/publication-bundles/${sourceId}/review`,
              { credentials: "include", headers: { Accept: "application/json" } },
            );
            return reviewResponse.ok
              ? ([sourceId, (await reviewResponse.json()) as PublicationReview] as const)
              : null;
          }),
      );
      if (loadVersion !== loadVersionRef.current) return;
      setListing(nextListing);
      setVaultMaster(nextVaultMaster);
      setAiEvidence(nextAiEvidence);
      setAnalysisBatches(nextAnalysisBatches);
      setAutopilot(nextAutopilot);
      setPublicationBundles(publications);
      setPublicationReviews(
        Object.fromEntries(
          reviewEntries.filter(
            (entry): entry is readonly [string, PublicationReview] => entry !== null,
          ),
        ),
      );
    } catch {
      if (loadVersion === loadVersionRef.current) {
        setError("Arrival Hall is currently unavailable.");
      }
    } finally {
      setRefreshing(false);
    }
  }, [isAdministrator, navigate]);

  function beginItemAction(itemId: string, action: ItemAction) {
    if (itemActionsRef.current.has(itemId)) return false;
    itemActionsRef.current.add(itemId);
    loadVersionRef.current += 1;
    setItemActions((current) => ({ ...current, [itemId]: action }));
    setItemActionErrors((current) => {
      const next = { ...current };
      delete next[itemId];
      return next;
    });
    return true;
  }

  function finishItemAction(itemId: string) {
    itemActionsRef.current.delete(itemId);
    loadVersionRef.current += 1;
    setItemActions((current) => {
      const next = { ...current };
      delete next[itemId];
      return next;
    });
  }

  async function responseError(response: Response, fallback: string) {
    try {
      const body = (await response.json()) as { detail?: string };
      return body.detail?.trim() || fallback;
    } catch {
      return fallback;
    }
  }

  async function extractPublication(sourceId: string) {
    setPublicationBusy(sourceId);
    setPublicationPreparing(sourceId);
    setError(null);
    try {
      const response = await fetch(`/api/vault-master/publication-bundles/${sourceId}/extract`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ max_florence_pages: 8 }),
      });
      const body = (await response.json()) as { detail?: string; pending_pages?: number[] };
      if (!response.ok) throw new Error(body.detail ?? "Publication extraction failed");
      setNotice(
        body.pending_pages?.length
          ? `Book preparation has started; ${body.pending_pages.length} pages remain.`
          : "The book is ready for review.",
      );
      await loadArrivalHall();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Publication extraction failed.");
    } finally {
      setPublicationPreparing(null);
      setPublicationBusy(null);
    }
  }

  async function publicationAction(
    sourceId: string,
    action: "retry" | "defer" | "reject" | "publish",
  ) {
    setPublicationBusy(sourceId);
    setError(null);
    try {
      const response = await fetch(
        `/api/vault-master/publication-bundles/${sourceId}/review/action`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ action }),
        },
      );
      const body = (await response.json()) as PublicationReview & { detail?: string };
      if (!response.ok) throw new Error(body.detail ?? "Publication review action failed");
      setPublicationReviews((current) => ({ ...current, [sourceId]: body }));
      setNotice(
        action === "publish"
          ? "Publication was verified and published to the Library."
          : `Publication review ${action} recorded.`,
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Publication review action failed.");
    } finally {
      setPublicationBusy(null);
    }
  }

  async function resolvePublicationIssue(sourceId: string, issueId: string) {
    setPublicationBusy(sourceId);
    try {
      const response = await fetch(
        `/api/vault-master/publication-bundles/${sourceId}/review/issues/${issueId}`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ state: "resolved" }),
        },
      );
      const body = (await response.json()) as PublicationReview & { detail?: string };
      if (!response.ok) throw new Error(body.detail ?? "Issue resolution failed");
      setPublicationReviews((current) => ({ ...current, [sourceId]: body }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Issue resolution failed.");
    } finally {
      setPublicationBusy(null);
    }
  }

  async function editPublicationMetadata(sourceId: string, review: PublicationReview) {
    const detected = review.snapshot.publication.detected;
    const overrides = review.snapshot.publication.user_overrides;
    const author = window.prompt("Author", String(overrides.author ?? detected.author ?? ""));
    if (author === null) return;
    const title = window.prompt("Title", String(overrides.title ?? detected.title ?? ""));
    if (title === null) return;
    setPublicationBusy(sourceId);
    try {
      const response = await fetch(
        `/api/vault-master/publication-bundles/${sourceId}/review/metadata`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ values: { author, title } }),
        },
      );
      const body = (await response.json()) as PublicationReview & { detail?: string };
      if (!response.ok) throw new Error(body.detail ?? "Metadata correction failed");
      setPublicationReviews((current) => ({ ...current, [sourceId]: body }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Metadata correction failed.");
    } finally {
      setPublicationBusy(null);
    }
  }

  async function editPublicationBlock(sourceId: string, blockId: string, currentText: string) {
    const text = window.prompt("Corrected reading text", currentText);
    if (text === null) return;
    setPublicationBusy(sourceId);
    try {
      const response = await fetch(
        `/api/vault-master/publication-bundles/${sourceId}/review/blocks/${blockId}`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ text }),
        },
      );
      const body = (await response.json()) as PublicationReview & { detail?: string };
      if (!response.ok) throw new Error(body.detail ?? "Text correction failed");
      setPublicationReviews((current) => ({ ...current, [sourceId]: body }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Text correction failed.");
    } finally {
      setPublicationBusy(null);
    }
  }

  async function rotatePublicationPage(sourceId: string, review: PublicationReview, page: number) {
    const pageCount = Number(review.snapshot.publication.detected.page_count ?? 0);
    const existingOrder = review.snapshot.publication.user_overrides.page_order;
    const pageOrder = Array.isArray(existingOrder)
      ? existingOrder
      : Array.from({ length: pageCount }, (_, index) => index + 1);
    const existingRotations = review.snapshot.publication.user_overrides.page_rotations;
    const rotations =
      existingRotations && typeof existingRotations === "object"
        ? { ...(existingRotations as Record<string, number>) }
        : {};
    rotations[String(page)] = ((rotations[String(page)] ?? 0) + 90) % 360;
    setPublicationBusy(sourceId);
    try {
      const response = await fetch(
        `/api/vault-master/publication-bundles/${sourceId}/review/pages`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ page_order: pageOrder, rotations }),
        },
      );
      const body = (await response.json()) as PublicationReview & { detail?: string };
      if (!response.ok) throw new Error(body.detail ?? "Page correction failed");
      setPublicationReviews((current) => ({ ...current, [sourceId]: body }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Page correction failed.");
    } finally {
      setPublicationBusy(null);
    }
  }

  async function captionPublicationIllustration(sourceId: string, blockId: string) {
    const caption = window.prompt("Illustration caption");
    if (caption === null) return;
    setPublicationBusy(sourceId);
    try {
      const response = await fetch(
        `/api/vault-master/publication-bundles/${sourceId}/review/illustrations/${blockId}`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ caption }),
        },
      );
      const body = (await response.json()) as PublicationReview & { detail?: string };
      if (!response.ok) throw new Error(body.detail ?? "Caption correction failed");
      setPublicationReviews((current) => ({ ...current, [sourceId]: body }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Caption correction failed.");
    } finally {
      setPublicationBusy(null);
    }
  }

  async function queueSemanticAnalysis(itemId: string) {
    setQueueingAi(itemId);
    setError(null);
    try {
      const response = await fetch(`/api/vault-master/items/${itemId}/ai/analyse`, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Semantic analysis queue failed");
      await loadArrivalHall();
    } catch {
      setError("The file analysis could not be queued.");
    } finally {
      setQueueingAi(null);
    }
  }

  async function queueBulkAnalysis() {
    const selectedEligible = [...selectedItems].filter((itemId) => {
      const item = vaultMaster?.items.find((candidate) => candidate.id === itemId);
      return (
        item?.source_kind === "incoming" &&
        supportsSemanticAssessment(item) &&
        ["inventoried", "needs_review"].includes(item.state)
      );
    });
    const allEligible =
      vaultMaster?.items
        .filter(
          (item) =>
            item.source_kind === "incoming" &&
            supportsSemanticAssessment(item) &&
            ["inventoried", "needs_review"].includes(item.state),
        )
        .map((item) => item.id) ?? [];
    const itemIds = selectedItems.size > 0 ? selectedEligible : allEligible;
    if (itemIds.length === 0) return;
    setBulkAnalysisBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/vault-master/items/ai/batches", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ item_ids: itemIds }),
      });
      if (!response.ok) throw new Error("Bulk analysis queue failed");
      await loadArrivalHall();
    } catch {
      setError("The selected files could not be queued for bulk analysis.");
    } finally {
      setBulkAnalysisBusy(false);
    }
  }

  async function controlAnalysisBatch(batchId: string, command: "pause" | "resume" | "retry") {
    setBulkAnalysisBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/vault-master/items/ai/batches/${batchId}/${command}`, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Batch control failed");
      await loadArrivalHall();
    } catch {
      setError("The bulk analysis batch could not be updated.");
    } finally {
      setBulkAnalysisBusy(false);
    }
  }

  async function configureAutopilot(
    contentType: (typeof AUTOPILOT_POLICY_OPTIONS)[number]["content_type"],
    destination: (typeof AUTOPILOT_POLICY_OPTIONS)[number]["destination"],
  ) {
    setAutopilotBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/vault-master/autopilot/policy", {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          content_type: contentType,
          destination,
          threshold: 80,
          max_items: 50,
          max_failures: 2,
          max_failure_percent: 5,
        }),
      });
      if (!response.ok) throw new Error("Auto-pilot configuration failed");
      await loadArrivalHall();
    } catch {
      setError("The auto-pilot policy could not be configured.");
    } finally {
      setAutopilotBusy(false);
    }
  }

  async function setAutopilotStatus(policyId: string, status: "enabled" | "paused" | "disabled") {
    setAutopilotBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/vault-master/autopilot/policy/${policyId}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ status }),
      });
      if (!response.ok) throw new Error("Auto-pilot status update failed");
      await loadArrivalHall();
    } catch {
      setError("The auto-pilot policy could not be updated.");
    } finally {
      setAutopilotBusy(false);
    }
  }

  async function runAutopilotNow() {
    setAutopilotBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/vault-master/autopilot/run", {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Auto-pilot run failed");
      setAutopilot((await response.json()) as AutopilotListing);
    } catch {
      setError("The auto-pilot run could not be started.");
    } finally {
      setAutopilotBusy(false);
    }
  }

  useEffect(() => {
    void loadArrivalHall();
    const refresh = window.setInterval(() => {
      void loadArrivalHall();
    }, 5000);
    return () => window.clearInterval(refresh);
  }, [loadArrivalHall]);

  async function updateProposal(
    itemId: string,
    category: string,
    publicationAudience?: "vault-wide" | "private",
  ) {
    if (proposalUpdatesRef.current.has(itemId)) return;
    proposalUpdatesRef.current.add(itemId);
    loadVersionRef.current += 1;
    try {
      const response = await fetch(`/api/vault-master/items/${itemId}/proposal`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ category, publication_audience: publicationAudience }),
      });
      if (!response.ok) {
        throw new Error(await responseError(response, "The proposal could not be updated."));
      }
      const updated = (await response.json()) as VaultMasterItem;
      // A periodic list request may have started after this PATCH began but before
      // the backend committed it. Its snapshot must not overwrite this response.
      loadVersionRef.current += 1;
      replaceVaultMasterItem(updated);
      setError(null);
    } catch (proposalError) {
      setError(
        proposalError instanceof Error
          ? proposalError.message
          : "The proposal could not be updated.",
      );
    } finally {
      proposalUpdatesRef.current.delete(itemId);
    }
  }

  function detectedMetadata(item: VaultMasterItem): MetadataDraft {
    const filenameWithoutExtension =
      item.filename.lastIndexOf(".") > 0
        ? item.filename.slice(0, item.filename.lastIndexOf("."))
        : item.filename;
    const text = (field: string) => {
      const value = item.metadata[field];
      return typeof value === "string" || typeof value === "number" ? String(value) : "";
    };
    return {
      display_title: text("display_title") || filenameWithoutExtension,
      captured_on:
        typeof item.metadata.captured_at === "string" ? item.metadata.captured_at.slice(0, 10) : "",
      location: typeof item.metadata.location === "string" ? item.metadata.location : "",
      artist: text("artist"),
      album: text("album"),
      album_artist: text("album_artist"),
      track_number: text("track_number"),
      disc_number: text("disc_number"),
      release_year: text("release_year"),
    };
  }

  function isMusicItem(item: VaultMasterItem) {
    return item.proposed_category === "Music" || item.mime_type.startsWith("audio/");
  }

  function editableMetadataFields(item: VaultMasterItem): readonly EditableMetadataField[] {
    return isMusicItem(item) ? MUSIC_METADATA_FIELDS : GENERAL_METADATA_FIELDS;
  }

  function beginMetadataEdit(item: VaultMasterItem) {
    const detected = detectedMetadata(item);
    const draft = (value: string | number | undefined, fallback: string) =>
      value === undefined ? fallback : String(value);
    setMetadataDraft({
      display_title: draft(item.metadata_overrides.display_title, detected.display_title),
      captured_on: draft(item.metadata_overrides.captured_on, detected.captured_on),
      location: draft(item.metadata_overrides.location, detected.location),
      artist: draft(item.metadata_overrides.artist, detected.artist),
      album: draft(item.metadata_overrides.album, detected.album),
      album_artist: draft(item.metadata_overrides.album_artist, detected.album_artist),
      track_number: draft(item.metadata_overrides.track_number, detected.track_number),
      disc_number: draft(item.metadata_overrides.disc_number, detected.disc_number),
      release_year: draft(item.metadata_overrides.release_year, detected.release_year),
    });
    setEditingMetadata(item.id);
  }

  function cancelMetadataEdit() {
    setEditingMetadata(null);
    setMetadataDraft(null);
  }

  async function saveMetadata(item: VaultMasterItem) {
    if (!metadataDraft) return;
    const detected = detectedMetadata(item);
    const changes: Partial<Record<EditableMetadataField, string | number | null>> = {};

    editableMetadataFields(item).forEach((field) => {
      const draftValue = metadataDraft[field].trim();
      const currentValue = String(item.metadata_overrides[field] ?? detected[field]);
      if (draftValue === currentValue) return;
      if (!draftValue || draftValue === detected[field]) {
        changes[field] = null;
      } else if (["track_number", "disc_number", "release_year"].includes(field)) {
        changes[field] = Number.parseInt(draftValue, 10);
      } else {
        changes[field] = draftValue;
      }
    });
    if (Object.keys(changes).length === 0) {
      cancelMetadataEdit();
      return;
    }

    setSavingDecision(item.id);
    setError(null);
    try {
      const response = await fetch(`/api/vault-master/items/${item.id}/metadata`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(changes),
      });
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Metadata update failed");
      }
      replaceVaultMasterItem((await response.json()) as VaultMasterItem);
      cancelMetadataEdit();
    } catch (metadataError) {
      setError(
        metadataError instanceof Error
          ? metadataError.message
          : "The metadata corrections could not be saved.",
      );
    } finally {
      setSavingDecision(null);
    }
  }

  async function decide(itemId: string, decision: "approve" | "reject") {
    if (!beginItemAction(itemId, decision === "approve" ? "approving" : "rejecting")) return;
    try {
      const response = await fetch(`/api/vault-master/items/${itemId}/${decision}`, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error(await responseError(response, "The decision could not be recorded."));
      }
      replaceVaultMasterItem((await response.json()) as VaultMasterListing["items"][number]);
    } catch (decisionError) {
      setItemActionErrors((current) => ({
        ...current,
        [itemId]:
          decisionError instanceof Error
            ? decisionError.message
            : "The decision could not be recorded.",
      }));
    } finally {
      finishItemAction(itemId);
    }
  }

  async function returnToReview(itemId: string) {
    setSavingDecision(itemId);
    setError(null);
    try {
      const response = await fetch(`/api/vault-master/items/${itemId}/return-to-review`, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Return to review failed");
      replaceVaultMasterItem((await response.json()) as VaultMasterItem);
    } catch {
      setError("The rejected file could not be returned to review.");
    } finally {
      setSavingDecision(null);
    }
  }

  async function removeRejected(item: VaultMasterItem) {
    const confirmation = window.prompt(
      `Permanently remove ${item.relative_path} from the Arrival Hall?\n\nType REMOVE FROM ARRIVAL HALL to confirm.`,
    );
    if (confirmation !== "REMOVE FROM ARRIVAL HALL") return;
    setSavingDecision(item.id);
    setError(null);
    try {
      const response = await fetch(`/api/vault-master/items/${item.id}/rejected/remove`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ confirmation }),
      });
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Removal failed");
      }
      await loadArrivalHall();
    } catch (removeError) {
      setError(
        removeError instanceof Error
          ? removeError.message
          : "The rejected file could not be removed.",
      );
    } finally {
      setSavingDecision(null);
    }
  }

  async function moveApproved(itemId: string) {
    if (!beginItemAction(itemId, "moving")) return;
    try {
      const response = await fetch(`/api/vault-master/items/${itemId}/move`, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Move failed");
      }
      replaceVaultMasterItem((await response.json()) as VaultMasterListing["items"][number]);
      setNotice(null);
    } catch (moveError) {
      setItemActionErrors((current) => ({
        ...current,
        [itemId]:
          moveError instanceof Error ? moveError.message : "The approved file could not be moved.",
      }));
    } finally {
      finishItemAction(itemId);
    }
  }

  async function decideDuplicate(itemId: string, decision: "keep" | "remove") {
    setSavingDecision(itemId);
    setError(null);
    try {
      const response = await fetch(`/api/vault-master/items/${itemId}/duplicate/${decision}`, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Duplicate decision failed");
      }
      await loadArrivalHall();
    } catch (duplicateError) {
      setError(
        duplicateError instanceof Error
          ? duplicateError.message
          : "The duplicate decision could not be completed.",
      );
    } finally {
      setSavingDecision(null);
    }
  }

  async function runBulkAction(action: "approve" | "reject" | "move") {
    const selected = [...selectedItems];
    const eligible = selected.filter((itemId) => {
      const item = vaultMaster?.items.find((candidate) => candidate.id === itemId);
      return action === "approve"
        ? item?.state === "needs_review" && !item.duplicate_of_id
        : action === "reject"
          ? item?.state === "needs_review" || item?.state === "approved"
          : item?.state === "approved";
    });
    if (eligible.length === 0 || (action === "approve" && eligible.length !== selected.length)) {
      return;
    }

    const filenames = eligible.map((itemId) => itemsById.get(itemId)?.relative_path ?? itemId);
    const description = `${action === "move" ? "Queue for movement" : action === "approve" ? "Approve" : "Reject"} these ${eligible.length} files?\n\n${filenames.join("\n")}`;
    if (!window.confirm(description)) return;

    const itemAction: ItemAction =
      action === "approve" ? "approving" : action === "reject" ? "rejecting" : "moving";
    setSavingDecision("bulk");
    setBulkActionProgress({ action, itemIds: eligible });
    loadVersionRef.current += 1;
    setItemActions((current) => ({
      ...current,
      ...Object.fromEntries(eligible.map((itemId) => [itemId, itemAction])),
    }));
    setItemActionErrors((current) => {
      const next = { ...current };
      eligible.forEach((itemId) => delete next[itemId]);
      return next;
    });
    setError(null);
    try {
      const response = await fetch(
        action === "approve"
          ? "/api/vault-master/bulk/approve"
          : "/api/vault-master/items/ai/review-batches",
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(
            action === "approve" ? { item_ids: eligible } : { action, item_ids: eligible },
          ),
        },
      );
      if (!response.ok) {
        throw new Error(await responseError(response, "The selected files could not be updated."));
      }
      const result = (await response.json()) as {
        items: VaultMasterListing["items"];
        outcomes?: Record<string, string>;
      };
      loadVersionRef.current += 1;
      result.items.forEach(replaceVaultMasterItem);
      const updatedIds = new Set(result.items.map((item) => item.id));
      const failedItemIds = eligible.filter((itemId) => !updatedIds.has(itemId));
      if (failedItemIds.length > 0) {
        setItemActionErrors((current) => ({
          ...current,
          ...Object.fromEntries(
            failedItemIds.map((itemId) => [
              itemId,
              result.outcomes?.[itemId]?.replaceAll("_", " ") ??
                "The selected action was not completed.",
            ]),
          ),
        }));
        setError(
          `${failedItemIds.length} selected ${failedItemIds.length === 1 ? "file was" : "files were"} not updated.`,
        );
      }
      setSelectedItems(new Set());
    } catch (bulkError) {
      const message =
        bulkError instanceof Error ? bulkError.message : "The selected files could not be updated.";
      setItemActionErrors((current) => ({
        ...current,
        ...Object.fromEntries(eligible.map((itemId) => [itemId, message])),
      }));
      setError(message);
    } finally {
      loadVersionRef.current += 1;
      setItemActions((current) => {
        const next = { ...current };
        eligible.forEach((itemId) => delete next[itemId]);
        return next;
      });
      setBulkActionProgress(null);
      setSavingDecision(null);
    }
  }

  const incomingAnalysis = new Map(
    vaultMaster?.items
      .filter((item) => item.source_kind === "incoming")
      .map((item) => [item.relative_path, item]) ?? [],
  );
  const itemsById = new Map(vaultMaster?.items.map((item) => [item.id, item]) ?? []);
  const aiEvidenceByItem = new Map<string, IngestionAiItemEvidence>(
    aiEvidence.items.map((item) => [item.item_id, item]),
  );
  const confidentReviewItems =
    vaultMaster?.items.filter((item) => {
      const evidence = aiEvidenceByItem.get(item.id)?.evidence[0];
      return (
        item.source_kind === "incoming" &&
        item.state === "needs_review" &&
        !item.duplicate_of_id &&
        evidence !== undefined &&
        evidence.routing_band !== "individual_review"
      );
    }) ?? [];
  const selectedReviewCount = [...selectedItems].filter((id) => {
    const item = itemsById.get(id);
    return (
      item?.source_kind === "incoming" && item.state === "needs_review" && !item.duplicate_of_id
    );
  }).length;
  const selectedApprovalEligible =
    selectedItems.size > 0 && selectedReviewCount === selectedItems.size;
  const selectedApprovedCount = [...selectedItems].filter(
    (id) => itemsById.get(id)?.state === "approved",
  ).length;
  const latestAnalysisBatch = analysisBatches.batches[0];
  const bulkSemanticCount =
    vaultMaster?.items.filter(
      (item) =>
        item.source_kind === "incoming" &&
        supportsSemanticAssessment(item) &&
        ["inventoried", "needs_review"].includes(item.state),
    ).length ?? 0;
  const selectedBulkSemanticCount = [...selectedItems].filter((itemId) => {
    const item = itemsById.get(itemId);
    return (
      item?.source_kind === "incoming" &&
      supportsSemanticAssessment(item) &&
      ["inventoried", "needs_review"].includes(item.state)
    );
  }).length;
  const visibleFiles =
    listing?.files.filter((file) => {
      const publicationItemIds = new Set(
        publicationBundles.bundles.flatMap((bundle) => [
          ...bundle.source_item_ids,
          ...bundle.front_cover_item_ids,
          ...bundle.back_cover_item_ids,
        ]),
      );
      const publicationItem = incomingAnalysis.get(file.relative_path);
      if (publicationItem && publicationItemIds.has(publicationItem.id)) return false;
      if (reviewFilter === "all") return true;
      const item = incomingAnalysis.get(file.relative_path);
      if (reviewFilter === "duplicates") return Boolean(item?.duplicate_of_id);
      const evidence = item ? aiEvidenceByItem.get(item.id)?.evidence[0] : undefined;
      if (reviewFilter === "conflicts") return (evidence?.conflicts.length ?? 0) > 0;
      return evidence?.routing_band === reviewFilter;
    }) ?? [];
  const visibleSelectableItemIds = visibleFiles.flatMap((file) => {
    const item = incomingAnalysis.get(file.relative_path);
    return item && !item.duplicate_of_id && ["needs_review", "approved"].includes(item.state)
      ? [item.id]
      : [];
  });
  const allVisibleSelectableItemsSelected =
    visibleSelectableItemIds.length > 0 &&
    visibleSelectableItemIds.every((itemId) => selectedItems.has(itemId));

  function toggleVisibleSelection() {
    setSelectedItems((current) => {
      const next = new Set(current);
      if (allVisibleSelectableItemsSelected) {
        visibleSelectableItemIds.forEach((itemId) => next.delete(itemId));
      } else {
        visibleSelectableItemIds.forEach((itemId) => next.add(itemId));
      }
      return next;
    });
  }
  const stagedFolderCount = new Set(
    listing?.files.flatMap((file) => (file.folder ? [file.folder] : [])) ?? [],
  ).size;
  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="pv-display-title text-2xl tracking-tight md:text-3xl">
            The Vault Master presents
          </p>
          <h2 className="pv-content-title mt-2 text-xl">Arrival Hall</h2>
          <p className="mt-1 max-w-xl text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Secure staging for newly uploaded files. Only an explicitly enabled, safety-gated
            auto-pilot policy may move proven low-risk files without individual approval.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Link
            to="/app/routing-memory"
            className="pv-btn-secondary inline-flex items-center gap-2"
          >
            <BrainCircuit size={15} />
            Routing Memory
          </Link>
          <button
            type="button"
            className="inline-flex items-center gap-2 rounded-md px-3 py-2 text-xs"
            style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
            disabled={refreshing}
            onClick={() => void loadArrivalHall()}
          >
            <RefreshCw size={14} className={refreshing ? "animate-spin" : ""} />
            Refresh
          </button>
          <Link to="/app/add" className="pv-btn-primary inline-flex items-center gap-2">
            <Upload size={15} />
            Add files
          </Link>
        </div>
      </div>

      {error && <div className="pv-panel p-6 text-sm text-center text-red-300">{error}</div>}
      {isAdministrator && publicationBundles.bundles.length > 0 && (
        <section className="pv-panel p-5 space-y-4" aria-labelledby="publication-review-title">
          <div className="flex items-start gap-3">
            <span
              className="mt-0.5 flex h-9 w-9 items-center justify-center rounded-full"
              style={{ color: "var(--pv-gold)", border: "1px solid var(--pv-border)" }}
            >
              <BookOpen size={17} />
            </span>
            <div>
              <p
                className="text-[11px] uppercase tracking-[0.2em]"
                style={{ color: "var(--pv-gold)" }}
              >
                Reading Room
              </p>
              <h3
                id="publication-review-title"
                className="mt-1 text-sm font-semibold"
                style={{ color: "var(--pv-silver)" }}
              >
                Publication review
              </h3>
              <p className="mt-1 max-w-2xl text-xs" style={{ color: "var(--pv-text-dim)" }}>
                Check the detected book details, prepared text, illustrations and page order before
                adding the edition to the Reading Room.
              </p>
            </div>
          </div>

          <div className="space-y-3">
            {publicationBundles.bundles.map((bundle) => {
              const sourceId = bundle.source_item_ids[0];
              const review = sourceId ? publicationReviews[sourceId] : undefined;
              const frontId = bundle.front_cover_item_ids[0];
              const frontItem = vaultMaster?.items.find((item) => item.id === frontId);
              const backId = bundle.back_cover_item_ids[0];
              const backItem = vaultMaster?.items.find((item) => item.id === backId);
              const openIssues =
                review?.snapshot.issues.filter(
                  (issue) => issue.state === "open" || issue.state === "accepted",
                ) ?? [];
              const chapters =
                review?.snapshot.blocks.filter(
                  (block) => block.block_type === "chapter" || block.block_type === "heading",
                ) ?? [];
              const textBlocks =
                review?.snapshot.blocks.filter((block) =>
                  ["part", "chapter", "heading", "paragraph", "footnote", "caption"].includes(
                    block.block_type,
                  ),
                ) ?? [];
              const illustrations =
                review?.snapshot.blocks.filter((block) => block.block_type === "illustration") ??
                [];
              return (
                <article
                  key={bundle.key}
                  className="rounded-lg border p-4"
                  style={{ borderColor: "var(--pv-border)", background: "var(--pv-surface-soft)" }}
                >
                  <div className="flex flex-col gap-4 md:flex-row">
                    {(frontItem || backItem) && (
                      <div className="flex gap-2">
                        {frontItem && (
                          <img
                            className="h-32 w-24 rounded object-cover"
                            src={`/api/arrival-hall/${encodeArrivalHallPath(frontItem.relative_path)}/preview`}
                            alt={`Front cover of ${bundle.title}`}
                          />
                        )}
                        {backItem && (
                          <img
                            className="h-32 w-24 rounded object-cover"
                            src={`/api/arrival-hall/${encodeArrivalHallPath(backItem.relative_path)}/preview`}
                            alt={`Back cover of ${bundle.title}`}
                          />
                        )}
                      </div>
                    )}
                    <div className="min-w-0 flex-1 space-y-3">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <h4
                            className="text-sm font-semibold"
                            style={{ color: "var(--pv-silver)" }}
                          >
                            {bundle.title}
                          </h4>
                          <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                            {bundle.author}
                            {review
                              ? ` · ${review.snapshot.publication.language ?? "language uncertain"} · ${review.snapshot.publication.reading_mode}`
                              : " · awaiting extraction"}
                          </p>
                        </div>
                        <span
                          className="rounded-full px-3 py-1 text-[10px] uppercase tracking-wider"
                          style={{ color: "var(--pv-gold)", border: "1px solid var(--pv-border)" }}
                        >
                          {review?.state.replaceAll("_", " ") ?? "review required"}
                        </span>
                      </div>

                      {bundle.issues.length > 0 && (
                        <p className="text-xs text-amber-200">
                          Bundle checks: {bundle.issues.join(", ").replaceAll("_", " ")}
                        </p>
                      )}

                      {!review && sourceId && (
                        <button
                          type="button"
                          className="pv-btn-primary inline-flex items-center gap-2"
                          disabled={publicationBusy === sourceId || bundle.issues.length > 0}
                          onClick={() => void extractPublication(sourceId)}
                        >
                          {publicationPreparing === sourceId ? (
                            <>
                              <RefreshCw size={16} className="animate-spin" aria-hidden="true" />
                              Preparing book…
                            </>
                          ) : (
                            "Prepare book for review"
                          )}
                        </button>
                      )}

                      {review && sourceId && (
                        <>
                          <div className="flex flex-wrap gap-2">
                            <button
                              type="button"
                              className="pv-btn-secondary"
                              disabled={publicationBusy === sourceId}
                              onClick={() => void editPublicationMetadata(sourceId, review)}
                            >
                              Correct metadata
                            </button>
                            <a
                              className="pv-btn-secondary"
                              href={`/api/vault-master/publication-bundles/${sourceId}/pages/1`}
                              target="_blank"
                              rel="noreferrer"
                            >
                              View exact page 1
                            </a>
                            <button
                              type="button"
                              className="pv-btn-secondary inline-flex items-center gap-2"
                              disabled={publicationBusy === sourceId}
                              onClick={() => void extractPublication(sourceId)}
                            >
                              {publicationPreparing === sourceId ? (
                                <>
                                  <RefreshCw
                                    size={15}
                                    className="animate-spin"
                                    aria-hidden="true"
                                  />
                                  Preparing book…
                                </>
                              ) : (
                                "Prepare again"
                              )}
                            </button>
                          </div>

                          {chapters.length > 0 && (
                            <details className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                              <summary className="cursor-pointer">
                                Chapter navigation · {chapters.length} headings
                              </summary>
                              <ul className="mt-2 space-y-1">
                                {chapters.map((block) => (
                                  <li
                                    key={block.id}
                                    className="flex items-center justify-between gap-3"
                                  >
                                    <span>
                                      {block.content_text ?? block.locator} · page{" "}
                                      {block.source_page ?? "?"}
                                    </span>
                                    <button
                                      type="button"
                                      className="text-[11px] underline"
                                      onClick={() =>
                                        void editPublicationBlock(
                                          sourceId,
                                          block.id,
                                          block.content_text ?? "",
                                        )
                                      }
                                    >
                                      Correct
                                    </button>
                                  </li>
                                ))}
                              </ul>
                            </details>
                          )}

                          {textBlocks.length > 0 && (
                            <details className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                              <summary className="cursor-pointer">
                                Reading text corrections · {textBlocks.length} blocks
                              </summary>
                              <ul className="mt-2 max-h-64 space-y-2 overflow-y-auto pr-2">
                                {textBlocks.map((block) => (
                                  <li
                                    key={block.id}
                                    className="flex items-start justify-between gap-3"
                                  >
                                    <span className="line-clamp-2">
                                      {block.block_type} · page {block.source_page ?? "?"} ·{" "}
                                      {block.content_text}
                                    </span>
                                    <button
                                      type="button"
                                      className="shrink-0 text-[11px] underline"
                                      onClick={() =>
                                        void editPublicationBlock(
                                          sourceId,
                                          block.id,
                                          block.content_text ?? "",
                                        )
                                      }
                                    >
                                      Correct
                                    </button>
                                  </li>
                                ))}
                              </ul>
                            </details>
                          )}

                          {illustrations.length > 0 && (
                            <details className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                              <summary className="cursor-pointer">
                                Illustration placement and captions · {illustrations.length}
                              </summary>
                              <ul className="mt-2 space-y-2">
                                {illustrations.map((block) => (
                                  <li
                                    key={block.id}
                                    className="flex items-center justify-between gap-3"
                                  >
                                    <span>
                                      Illustration at source page {block.source_page ?? "?"}
                                    </span>
                                    <button
                                      type="button"
                                      className="text-[11px] underline"
                                      onClick={() =>
                                        void captionPublicationIllustration(sourceId, block.id)
                                      }
                                    >
                                      Set caption
                                    </button>
                                  </li>
                                ))}
                              </ul>
                            </details>
                          )}

                          {openIssues.length > 0 && (
                            <div className="space-y-2">
                              {openIssues.map((issue) => (
                                <div
                                  key={issue.id}
                                  className="flex flex-wrap items-center justify-between gap-2 rounded border px-3 py-2 text-xs"
                                  style={{ borderColor: "var(--pv-border)" }}
                                >
                                  <span
                                    style={{
                                      color:
                                        issue.severity === "critical"
                                          ? "#fca5a5"
                                          : "var(--pv-text-dim)",
                                    }}
                                  >
                                    {issue.severity} · {issue.detail}
                                    {issue.source_page ? ` · page ${issue.source_page}` : ""}
                                  </span>
                                  <button
                                    type="button"
                                    className="pv-btn-secondary"
                                    disabled={publicationBusy === sourceId}
                                    onClick={() => void resolvePublicationIssue(sourceId, issue.id)}
                                  >
                                    Mark resolved
                                  </button>
                                  {issue.issue_type === "incorrect_rotation" &&
                                    issue.source_page && (
                                      <button
                                        type="button"
                                        className="pv-btn-secondary"
                                        disabled={publicationBusy === sourceId}
                                        onClick={() =>
                                          void rotatePublicationPage(
                                            sourceId,
                                            review,
                                            issue.source_page!,
                                          )
                                        }
                                      >
                                        Rotate 90°
                                      </button>
                                    )}
                                  {issue.source_page && (
                                    <a
                                      className="text-[11px] underline"
                                      href={`/api/vault-master/publication-bundles/${sourceId}/pages/${issue.source_page}`}
                                      target="_blank"
                                      rel="noreferrer"
                                    >
                                      Verify exact page
                                    </a>
                                  )}
                                </div>
                              ))}
                            </div>
                          )}

                          <div
                            className="flex flex-wrap gap-2 border-t pt-3"
                            style={{ borderColor: "var(--pv-border)" }}
                          >
                            <button
                              type="button"
                              className="pv-btn-secondary"
                              disabled={publicationBusy === sourceId}
                              onClick={() => void publicationAction(sourceId, "defer")}
                            >
                              Defer
                            </button>
                            <button
                              type="button"
                              className="pv-btn-secondary"
                              disabled={publicationBusy === sourceId}
                              onClick={() => void publicationAction(sourceId, "reject")}
                            >
                              Reject
                            </button>
                            <button
                              type="button"
                              className="pv-btn-primary"
                              disabled={
                                publicationBusy === sourceId || review.state === "ready_to_publish"
                              }
                              onClick={() => void publicationAction(sourceId, "publish")}
                            >
                              Approve publication
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}

      <section className="pv-panel p-5 space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p
              className="text-[11px] uppercase tracking-[0.2em]"
              style={{ color: "var(--pv-gold)" }}
            >
              Owner-controlled auto-pilot
            </p>
            <h3 className="mt-1 text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
              Ordinary Vault files with 80-point evidence
            </h3>
            <p className="mt-1 max-w-2xl text-xs" style={{ color: "var(--pv-text-dim)" }}>
              Opt-in, owner-scoped and disabled by default. The 80-point threshold applies to every
              supported ordinary category, but movement still needs its matching enabled policy.
              Duplicates, conflicts, screenshots, Reading Room candidates, changed checksums,
              collisions, mount problems and movement failures stay out of auto-pilot.
            </p>
          </div>
        </div>
        <div className="space-y-3 border-t pt-4" style={{ borderColor: "var(--pv-border)" }}>
          {AUTOPILOT_POLICY_OPTIONS.map((option) => {
            const policy = autopilot.policies.find(
              (candidate) =>
                candidate.content_type === option.content_type &&
                candidate.destination === option.destination,
            );
            return (
              <div
                key={option.content_type}
                className="flex flex-wrap items-center justify-between gap-3"
              >
                <div>
                  <p className="text-xs" style={{ color: "var(--pv-silver)" }}>
                    {option.label}
                  </p>
                  <p className="mt-1 text-[11px]" style={{ color: "var(--pv-text-dim)" }}>
                    {policy
                      ? `Threshold ${policy.threshold} - maximum ${policy.max_items} files - stop at ${policy.max_failures} failures or ${policy.max_failure_percent}%`
                      : "No owner policy yet; eligible files remain staged."}
                  </p>
                </div>
                {!policy ? (
                  <button
                    type="button"
                    className="pv-btn-secondary"
                    disabled={autopilotBusy}
                    onClick={() => void configureAutopilot(option.content_type, option.destination)}
                  >
                    Create disabled policy
                  </button>
                ) : (
                  <div className="flex flex-wrap items-center gap-2">
                    <span
                      className="rounded-full px-3 py-1 text-[10px] uppercase tracking-wider"
                      style={{ color: "var(--pv-gold)", border: "1px solid var(--pv-border)" }}
                    >
                      {policy.status}
                    </span>
                    {policy.status === "enabled" ? (
                      <button
                        type="button"
                        className="pv-btn-secondary"
                        disabled={autopilotBusy}
                        onClick={() => void setAutopilotStatus(policy.id, "paused")}
                      >
                        Stop and pause
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="pv-btn-secondary"
                        disabled={autopilotBusy}
                        onClick={() => void setAutopilotStatus(policy.id, "enabled")}
                      >
                        {policy.status === "paused" ? "Explicitly resume" : "Enable policy"}
                      </button>
                    )}
                    <button
                      type="button"
                      className="pv-btn-primary"
                      disabled={autopilotBusy || policy.status !== "enabled"}
                      onClick={() => void runAutopilotNow()}
                    >
                      Process eligible now
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        {autopilot.runs[0] && (
          <details
            className="border-t pt-4 text-xs"
            style={{ borderColor: "var(--pv-border)", color: "var(--pv-text-dim)" }}
          >
            <summary className="cursor-pointer select-none">
              Latest run: {autopilot.runs[0].status} · {autopilot.runs[0].item_ids.length} exact
              files
              {autopilot.runs[0].stop_reason ? ` · ${autopilot.runs[0].stop_reason}` : ""}
            </summary>
            <ul className="mt-3 space-y-1 font-mono">
              {autopilot.runs[0].item_ids.map((itemId) => (
                <li key={itemId}>
                  {itemId} · {autopilot.runs[0].outcomes[itemId] ?? "pending"}
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>

      {listing?.files.length === 0 && (
        <div className="pv-panel p-10 text-center">
          <span
            className="mx-auto h-12 w-12 rounded-full flex items-center justify-center"
            style={{ border: "1px solid var(--pv-border)", color: "var(--pv-gold)" }}
          >
            <Inbox size={20} />
          </span>
          <h3 className="text-sm font-semibold mt-4" style={{ color: "var(--pv-silver)" }}>
            Arrival Hall is empty
          </h3>
          <p className="text-xs mt-2" style={{ color: "var(--pv-text-dim)" }}>
            Files uploaded to Personal Vault will appear here.
          </p>
        </div>
      )}

      {listing && listing.files.length > 0 && (
        <div className="space-y-3">
          <div className="pv-panel p-5 space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
                  Automatic semantic analysis
                </p>
                <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  Eligible images and PDFs are analysed automatically after inventory.
                </p>
              </div>
              <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                {bulkSemanticCount > 0
                  ? "Waiting for automatic analysis"
                  : "No files awaiting analysis"}
              </p>
            </div>
            {latestAnalysisBatch && (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-3 text-[11px]">
                      <span style={{ color: "var(--pv-silver)" }}>
                        {latestAnalysisBatch.completed_items + latestAnalysisBatch.failed_items} of{" "}
                        {latestAnalysisBatch.total_items} complete
                      </span>
                      <span
                        className="uppercase tracking-wider"
                        style={{ color: "var(--pv-gold)" }}
                      >
                        {latestAnalysisBatch.status.replaceAll("_", " ")}
                      </span>
                    </div>
                    <div
                      className="mt-2 h-1.5 overflow-hidden rounded-full"
                      style={{ background: "rgba(255,255,255,0.07)" }}
                    >
                      <div
                        className="h-full rounded-full transition-all"
                        style={{
                          width: `${Math.round(((latestAnalysisBatch.completed_items + latestAnalysisBatch.failed_items) / latestAnalysisBatch.total_items) * 100)}%`,
                          background: "var(--pv-gold)",
                        }}
                      />
                    </div>
                  </div>
                  {latestAnalysisBatch.status === "running" && (
                    <button
                      type="button"
                      className="pv-btn-secondary inline-flex items-center gap-1.5 px-3 py-2 text-xs"
                      disabled={bulkAnalysisBusy}
                      onClick={() => void controlAnalysisBatch(latestAnalysisBatch.id, "pause")}
                    >
                      <Pause size={13} /> Pause
                    </button>
                  )}
                  {latestAnalysisBatch.status === "paused" && (
                    <button
                      type="button"
                      className="pv-btn-secondary inline-flex items-center gap-1.5 px-3 py-2 text-xs"
                      disabled={bulkAnalysisBusy}
                      onClick={() => void controlAnalysisBatch(latestAnalysisBatch.id, "resume")}
                    >
                      <Play size={13} /> Resume
                    </button>
                  )}
                  {latestAnalysisBatch.failed_items > 0 && (
                    <button
                      type="button"
                      className="pv-btn-secondary px-3 py-2 text-xs"
                      disabled={bulkAnalysisBusy}
                      onClick={() => void controlAnalysisBatch(latestAnalysisBatch.id, "retry")}
                    >
                      Retry {latestAnalysisBatch.failed_items} failed
                    </button>
                  )}
                </div>
                {latestAnalysisBatch.groups.length > 0 && (
                  <div className="grid gap-2 md:grid-cols-2">
                    {latestAnalysisBatch.groups.map((group) => (
                      <button
                        key={`${group.destination}-${group.content_type}-${group.routing_band}-${group.explanation}`}
                        type="button"
                        className="rounded-lg p-3 text-left transition-colors hover:bg-white/[0.03]"
                        style={{ border: "1px solid var(--pv-border)" }}
                        onClick={() => setSelectedItems(new Set(group.item_ids))}
                      >
                        <span
                          className="text-xs font-semibold"
                          style={{ color: "var(--pv-silver)" }}
                        >
                          {group.count} {group.content_type.replaceAll("_", " ")} →{" "}
                          {group.destination ?? "Review"}
                        </span>
                        <span
                          className="mt-1 block text-[10px] uppercase tracking-wider"
                          style={{ color: "var(--pv-gold)" }}
                        >
                          {group.routing_band.replaceAll("_", " ")}
                        </span>
                        <span
                          className="mt-1 block text-[11px]"
                          style={{ color: "var(--pv-text-dim)" }}
                        >
                          {group.explanation}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <button
              type="button"
              className="rounded-md px-3 py-2 text-xs"
              style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
              disabled={confidentReviewItems.length === 0 || savingDecision === "bulk"}
              onClick={() => setSelectedItems(new Set(confidentReviewItems.map((item) => item.id)))}
            >
              Select confident proposals ({confidentReviewItems.length})
            </button>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="rounded-md px-3 py-2 text-xs"
                style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
                disabled={visibleSelectableItemIds.length === 0 || savingDecision === "bulk"}
                onClick={toggleVisibleSelection}
              >
                {allVisibleSelectableItemsSelected ? "Deselect all" : "Select all"}
              </button>
              <button
                type="button"
                className="rounded-md px-3 py-2 text-xs"
                style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
                disabled={!selectedApprovalEligible || savingDecision === "bulk"}
                onClick={() => void runBulkAction("approve")}
              >
                {bulkActionProgress?.action === "approve"
                  ? `Approving ${bulkActionProgress.itemIds.length} selected…`
                  : `Approve selected (${selectedItems.size})`}
              </button>
              <button
                type="button"
                className="rounded-md px-3 py-2 text-xs"
                style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
                disabled={selectedItems.size === 0 || savingDecision === "bulk"}
                onClick={() => void runBulkAction("reject")}
              >
                Reject selected ({selectedItems.size})
              </button>
              <button
                type="button"
                className="pv-btn-primary px-3 py-2 text-xs"
                disabled={selectedApprovedCount === 0 || savingDecision === "bulk"}
                onClick={() => void runBulkAction("move")}
              >
                Move selected ({selectedApprovedCount})
              </button>
            </div>
          </div>
          {bulkActionProgress && (
            <p className="text-xs" role="status" style={{ color: "var(--pv-gold)" }}>
              {bulkActionProgress.action === "approve"
                ? `Approving ${bulkActionProgress.itemIds.length} selected files…`
                : bulkActionProgress.action === "reject"
                  ? `Rejecting ${bulkActionProgress.itemIds.length} selected files…`
                  : `Queueing movement for ${bulkActionProgress.itemIds.length} selected files…`}
            </p>
          )}
          <div className="flex flex-wrap gap-2" aria-label="Filter ingestion review">
            {(
              [
                ["all", "All"],
                ["automatic_eligible", "Automatic eligible"],
                ["batch_review", "Batch review"],
                ["individual_review", "Individual review"],
                ["conflicts", "Conflicts"],
                ["duplicates", "Duplicates"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                className="rounded-full px-3 py-1.5 text-[11px] transition-colors"
                style={{
                  color: reviewFilter === value ? "var(--pv-ink)" : "var(--pv-silver)",
                  background: reviewFilter === value ? "var(--pv-gold)" : "transparent",
                  border: "1px solid var(--pv-border)",
                }}
                onClick={() => setReviewFilter(value)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="pv-panel overflow-hidden">
            <div
              className="px-5 py-4 flex items-center justify-between"
              style={{ borderBottom: "1px solid var(--pv-border)" }}
            >
              <p className="text-sm font-semibold" style={{ color: "var(--pv-silver)" }}>
                Staged files
              </p>
              <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                {listing.files.length} {listing.files.length === 1 ? "file" : "files"}
                {stagedFolderCount > 0 &&
                  ` in ${stagedFolderCount} ${stagedFolderCount === 1 ? "folder" : "folders"}`}{" "}
                · {formatBytes(listing.files.reduce((total, file) => total + file.size, 0))}
              </p>
            </div>
            <div className="divide-y" style={{ borderColor: "var(--pv-border)" }}>
              {visibleFiles.map((file) => {
                const analysis = incomingAnalysis.get(file.relative_path);
                const itemAction = analysis ? itemActions[analysis.id] : undefined;
                const itemBusy = Boolean(
                  analysis && (itemAction || savingDecision === analysis.id),
                );
                const duplicate = analysis?.duplicate_of_id
                  ? itemsById.get(analysis.duplicate_of_id)
                  : undefined;
                const itemAi = analysis ? aiEvidenceByItem.get(analysis.id) : undefined;
                const latestEvidence = itemAi?.evidence[0];
                const latestAiJob = itemAi?.jobs[0];
                const isPdf = analysis?.mime_type === "application/pdf";
                const pipelineState =
                  itemAction === "approving"
                    ? "Approving…"
                    : itemAction === "rejecting"
                      ? "Rejecting…"
                      : itemAction === "moving"
                        ? "Queueing move…"
                        : !analysis
                          ? "Awaiting scan"
                          : latestAiJob?.status === "queued"
                            ? "Awaiting analysis"
                            : latestAiJob?.status === "processing"
                              ? "Analysing"
                              : analysis.state === "move_queued"
                                ? "Move queued"
                                : analysis.state === "moving"
                                  ? "Moving"
                                  : analysis.state === "moved"
                                    ? "Moved"
                                    : latestEvidence?.routing_band === "automatic_eligible"
                                      ? "Ready for auto-pilot"
                                      : latestEvidence
                                        ? "Awaiting review"
                                        : analysis.mime_type.startsWith("image/")
                                          ? "Awaiting analysis"
                                          : analysis.state === "approved"
                                            ? "Approved · not moved"
                                            : analysis.state === "move_failed"
                                              ? "Move failed"
                                              : analysis.state === "duplicate_kept"
                                                ? "Duplicate kept"
                                                : analysis.state === "duplicate_remove_failed"
                                                  ? "Removal failed"
                                                  : analysis.state === "rejected"
                                                    ? "Rejected"
                                                    : "Needs review";

                return (
                  <div key={file.relative_path} className="px-5 py-4 flex items-start gap-4">
                    {analysis &&
                      !analysis.duplicate_of_id &&
                      ["needs_review", "approved"].includes(analysis.state) && (
                        <input
                          type="checkbox"
                          aria-label={`Select ${file.relative_path}`}
                          className="mt-5 h-4 w-4 accent-amber-400"
                          checked={selectedItems.has(analysis.id)}
                          disabled={savingDecision === "bulk"}
                          onChange={(event) =>
                            setSelectedItems((current) => {
                              const next = new Set(current);
                              if (event.target.checked) next.add(analysis.id);
                              else next.delete(analysis.id);
                              return next;
                            })
                          }
                        />
                      )}
                    <span
                      className="h-16 w-16 shrink-0 rounded-md flex items-center justify-center overflow-hidden"
                      style={{
                        background: "rgba(255,255,255,0.035)",
                        border: "1px solid var(--pv-border)",
                        color: "var(--pv-gold)",
                      }}
                    >
                      {analysis?.mime_type.startsWith("image/") ? (
                        <img
                          src={`/api/arrival-hall/${encodeArrivalHallPath(file.relative_path)}/preview`}
                          alt=""
                          className="h-full w-full object-cover"
                          loading="lazy"
                        />
                      ) : analysis?.mime_type === "application/pdf" ? (
                        <span className="text-[10px] font-semibold uppercase">PDF</span>
                      ) : analysis?.mime_type.startsWith("video/") ? (
                        <video
                          src={`/api/arrival-hall/${encodeArrivalHallPath(file.relative_path)}/preview`}
                          className="h-full w-full object-cover"
                          muted
                          playsInline
                          preload="metadata"
                          onLoadedMetadata={(event) => {
                            event.currentTarget.currentTime = Math.min(
                              0.25,
                              event.currentTarget.duration / 10,
                            );
                          }}
                        />
                      ) : (
                        <File size={17} />
                      )}
                    </span>
                    <div className="min-w-0 flex-1">
                      {file.folder && (
                        <p
                          className="mb-1 flex items-center gap-1.5 text-[10px] uppercase tracking-wider"
                          style={{ color: "var(--pv-gold)" }}
                        >
                          <Folder size={12} />
                          {file.folder}
                        </p>
                      )}
                      <div className="flex flex-wrap items-center gap-2">
                        <p
                          className="text-sm font-medium truncate"
                          style={{ color: "var(--pv-silver)" }}
                        >
                          {analysis?.metadata_overrides.display_title ?? file.name}
                        </p>
                        <span
                          className="px-2 py-0.5 rounded-full text-[10px] uppercase tracking-wide"
                          style={{
                            color: "var(--pv-silver-dim)",
                            border: "1px solid var(--pv-border)",
                          }}
                        >
                          {pipelineState}
                        </span>
                      </div>
                      <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
                        {formatBytes(file.size)}
                        {analysis ? (
                          <>
                            {" "}
                            · {analysis.mime_type} · SHA-256{" "}
                            <span className="font-mono">{analysis.sha256.slice(0, 12)}…</span>
                          </>
                        ) : (
                          <>
                            {" "}
                            · Uploaded{" "}
                            {new Intl.DateTimeFormat("en-GB", {
                              dateStyle: "medium",
                              timeStyle: "short",
                            }).format(new Date(file.uploaded_at))}
                          </>
                        )}
                      </p>
                      {analysis &&
                        (typeof analysis.metadata.width === "number" ||
                          typeof analysis.metadata.camera_model === "string" ||
                          typeof analysis.metadata.captured_at === "string") && (
                          <p className="text-xs mt-1" style={{ color: "var(--pv-text-dim)" }}>
                            {typeof analysis.metadata.width === "number" &&
                              typeof analysis.metadata.height === "number" &&
                              `${analysis.metadata.width} × ${analysis.metadata.height}`}
                            {typeof analysis.metadata.camera_model === "string" &&
                              ` · Camera: ${analysis.metadata.camera_model}`}
                            {(analysis.metadata_overrides.captured_on ||
                              typeof analysis.metadata.captured_at === "string") &&
                              ` · ${
                                analysis.metadata_overrides.captured_on ? "Corrected date" : "Taken"
                              }: ${formatPhotoDate(
                                analysis.metadata_overrides.captured_on ??
                                  String(analysis.metadata.captured_at),
                              )}`}
                          </p>
                        )}
                      {analysis && Object.keys(analysis.metadata_overrides).length > 0 && (
                        <div className="mt-3 space-y-1">
                          <p
                            className="text-[10px] uppercase tracking-widest"
                            style={{ color: "var(--pv-gold)" }}
                          >
                            Corrected metadata
                          </p>
                          <p className="text-xs" style={{ color: "var(--pv-silver)" }}>
                            {correctedMetadataSummary(analysis).join(" · ")}
                          </p>
                        </div>
                      )}
                      {analysis && supportsSemanticAssessment(analysis) && !duplicate && (
                        <div
                          className="mt-3 rounded-lg border p-3"
                          style={{
                            borderColor: "rgba(var(--pv-gold-rgb), 0.22)",
                            background: "rgba(var(--pv-gold-rgb), 0.025)",
                          }}
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div>
                              <p
                                className="text-[10px] uppercase tracking-widest"
                                style={{ color: "var(--pv-gold)" }}
                              >
                                {isPdf ? "PDF evidence" : "Image evidence"}
                              </p>
                              <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                                {isPdf
                                  ? "Evidence stays private and review-only."
                                  : "Evidence stays private; only safe ordinary photos can continue to auto-pilot."}
                              </p>
                            </div>
                            <button
                              type="button"
                              className="pv-btn-ghost px-3 py-2 text-xs"
                              disabled={
                                queueingAi === analysis.id ||
                                latestAiJob?.status === "queued" ||
                                latestAiJob?.status === "processing"
                              }
                              onClick={() => void queueSemanticAnalysis(analysis.id)}
                            >
                              {latestAiJob?.status === "queued"
                                ? "Analysis queued"
                                : latestAiJob?.status === "processing"
                                  ? "Analysing"
                                  : latestAiJob?.status === "failed"
                                    ? "Retry analysis"
                                    : latestEvidence
                                      ? "Analyse again"
                                      : "Queue analysis now"}
                            </button>
                          </div>
                          {latestAiJob?.status === "failed" && (
                            <p className="mt-2 text-xs text-red-300">
                              Analysis failed: {latestAiJob.error}
                            </p>
                          )}
                          {latestEvidence && (
                            <div className="mt-3 space-y-2 text-xs">
                              <p style={{ color: "var(--pv-silver)" }}>
                                Classified as {latestEvidence.content_type.replaceAll("_", " ")} ·{" "}
                                {Math.round(latestEvidence.confidence * 100)}% evidence confidence
                              </p>
                              <p style={{ color: "var(--pv-text-dim)" }}>
                                {latestEvidence.reasons.join(" · ")}
                              </p>
                              <div
                                className="rounded-md border p-3"
                                style={{ borderColor: "var(--pv-border)" }}
                              >
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <p style={{ color: "var(--pv-silver)" }}>
                                    {latestEvidence.recommended_destination
                                      ? `Suggested destination: ${latestEvidence.recommended_destination}`
                                      : "No safe destination suggested"}
                                  </p>
                                  <span
                                    className="rounded-full border px-2 py-0.5 text-[10px]"
                                    style={{
                                      color: "var(--pv-gold)",
                                      borderColor: "rgba(var(--pv-gold-rgb), 0.32)",
                                    }}
                                  >
                                    {latestEvidence.routing_band.replaceAll("_", " ")} · score{" "}
                                    {latestEvidence.decision_score}/100
                                  </span>
                                </div>
                                <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 md:grid-cols-3">
                                  {Object.entries(latestEvidence.confidence_components)
                                    .filter(([name]) => name !== "learned_routing")
                                    .map(([name, value]) => (
                                      <p key={name} style={{ color: "var(--pv-text-dim)" }}>
                                        {name === "visual_classification" &&
                                        latestEvidence.content_type === "unknown"
                                          ? `category evidence: ${Math.round(value)} (insufficient)`
                                          : `${name.replaceAll("_", " ")}: ${Math.round(value)}`}
                                      </p>
                                    ))}
                                </div>
                                {latestEvidence.conflicts.map((conflict) => (
                                  <p key={conflict} className="mt-2 text-amber-300">
                                    Conflict: {conflict}
                                  </p>
                                ))}
                                {latestEvidence.automatic_disqualifiers.length > 0 ? (
                                  <div className="mt-2">
                                    <p style={{ color: "var(--pv-silver-dim)" }}>
                                      Requires review because:
                                    </p>
                                    <ul
                                      className="mt-1 list-disc space-y-1 pl-4"
                                      style={{ color: "var(--pv-text-dim)" }}
                                    >
                                      {latestEvidence.automatic_disqualifiers.map((reason) => (
                                        <li key={reason}>{reason}</li>
                                      ))}
                                    </ul>
                                  </div>
                                ) : (
                                  <p className="mt-2 text-emerald-300">
                                    Eligible for a future owner-enabled auto-pilot policy. No file
                                    will move automatically in this stage.
                                  </p>
                                )}
                              </div>
                              {latestEvidence.caption && (
                                <p style={{ color: "var(--pv-silver-dim)" }}>
                                  {"Visual hint (unverified): "}
                                  {latestEvidence.caption}
                                </p>
                              )}
                              {latestEvidence.ocr_text && (
                                <details>
                                  <summary
                                    className="cursor-pointer"
                                    style={{ color: "var(--pv-gold)" }}
                                  >
                                    Show private evidence text
                                  </summary>
                                  <p
                                    className="mt-2 whitespace-pre-wrap rounded-md p-2"
                                    style={{
                                      color: "var(--pv-silver-dim)",
                                      background: "rgba(0,0,0,0.24)",
                                    }}
                                  >
                                    {latestEvidence.ocr_text}
                                  </p>
                                </details>
                              )}
                            </div>
                          )}
                        </div>
                      )}
                      {analysis?.mime_type === "application/pdf" && !duplicate && (
                        <details
                          className="mt-3 rounded-lg border p-3"
                          style={{ borderColor: "var(--pv-border)" }}
                        >
                          <summary
                            className="cursor-pointer text-xs"
                            style={{ color: "var(--pv-gold)" }}
                          >
                            Preview PDF
                          </summary>
                          <iframe
                            className="mt-3 h-[32rem] w-full rounded border"
                            style={{ borderColor: "var(--pv-border)" }}
                            src={`/api/arrival-hall/${encodeArrivalHallPath(file.relative_path)}/preview`}
                            title={`Preview of ${file.name}`}
                          />
                        </details>
                      )}
                      {duplicate && (
                        <div className="mt-3 space-y-2">
                          <p className="text-xs text-amber-300">Exact duplicate detected.</p>
                          <p className="text-xs" style={{ color: "var(--pv-silver)" }}>
                            Matching Vault file:{" "}
                            {duplicate.source_path
                              .replace("/media/gallery", "/vault/Gallery")
                              .replace("/media/personal-videos", "/vault/Home Videos")
                              .replace("/media/documents", "/vault/Documents")
                              .replace("/media/archives", "/vault/Archives")
                              .replace("/media/movies", "/vault/Theatre/Movies")
                              .replace("/media/music", "/vault/Music")}
                          </p>
                          {analysis?.state !== "duplicate_kept" && (
                            <div className="flex flex-wrap gap-2">
                              <button
                                type="button"
                                className="rounded-md px-3 py-2 text-xs"
                                style={{
                                  color: "var(--pv-silver)",
                                  border: "1px solid var(--pv-border)",
                                }}
                                disabled={savingDecision === analysis?.id}
                                onClick={() =>
                                  analysis && void decideDuplicate(analysis.id, "keep")
                                }
                              >
                                Keep Arrival Hall copy
                              </button>
                              <button
                                type="button"
                                className="pv-btn-primary px-3 py-2 text-xs"
                                disabled={savingDecision === analysis?.id}
                                onClick={() =>
                                  analysis && void decideDuplicate(analysis.id, "remove")
                                }
                              >
                                Remove Arrival Hall duplicate
                              </button>
                            </div>
                          )}
                        </div>
                      )}
                      {analysis?.proposed_destination && (
                        <div className="mt-3 space-y-1">
                          <p className="text-xs" style={{ color: "var(--pv-silver)" }}>
                            Proposed: {analysis.proposed_destination}
                          </p>
                          <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                            {analysis.proposal_confidence} confidence · {analysis.proposal_reason}
                          </p>
                        </div>
                      )}
                      {analysis?.state === "rejected" && !duplicate && (
                        <div className="mt-4 flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            className="pv-btn-primary px-3 py-2 text-xs flex items-center gap-2"
                            disabled={savingDecision === analysis.id}
                            onClick={() => void returnToReview(analysis.id)}
                          >
                            <Undo2 size={13} />
                            Return to review
                          </button>
                          <button
                            type="button"
                            className="rounded-md px-3 py-2 text-xs flex items-center gap-2"
                            style={{
                              color: "rgb(252 165 165)",
                              border: "1px solid rgba(248,113,113,0.35)",
                            }}
                            disabled={savingDecision === analysis.id}
                            onClick={() => void removeRejected(analysis)}
                          >
                            <Trash2 size={13} />
                            Remove from Arrival Hall
                          </button>
                        </div>
                      )}
                      {analysis &&
                        !duplicate &&
                        !["rejected", "moved"].includes(analysis.state) && (
                          <div className="mt-4 flex flex-wrap items-center gap-2">
                            <select
                              aria-label={`Proposed category for ${file.relative_path}`}
                              className="rounded-md px-3 py-2 text-xs bg-transparent"
                              style={{
                                color: "var(--pv-silver)",
                                border: "1px solid var(--pv-border)",
                              }}
                              value={analysis.proposed_category ?? "Archives"}
                              disabled={itemBusy || analysis.state !== "needs_review"}
                              onChange={(event) =>
                                void updateProposal(analysis.id, event.target.value)
                              }
                            >
                              <option value="Gallery">Gallery</option>
                              <option value="Home Videos">Home Videos</option>
                              <option value="Movies">Theatre / Movies</option>
                              <option value="TV Shows">Theatre / TV Shows</option>
                              <option value="Documents">Documents</option>
                              <option value="Archives">Archives</option>
                              <option value="Music">Music</option>
                            </select>
                            {(analysis.proposed_category === "Movies" ||
                              analysis.proposed_category === "TV Shows") && (
                              <select
                                aria-label={`Theatre audience for ${file.relative_path}`}
                                className="rounded-md px-3 py-2 text-xs bg-transparent"
                                style={{
                                  color: "var(--pv-silver)",
                                  border: "1px solid var(--pv-border)",
                                }}
                                value={analysis.publication_audience ?? "vault-wide"}
                                disabled={itemBusy || analysis.state !== "needs_review"}
                                onChange={(event) =>
                                  void updateProposal(
                                    analysis.id,
                                    analysis.proposed_category,
                                    event.target.value as "vault-wide" | "private",
                                  )
                                }
                              >
                                <option value="vault-wide">All Vault users</option>
                                <option value="private">Only me</option>
                              </select>
                            )}
                            <button
                              type="button"
                              className="rounded-md px-3 py-2 text-xs flex items-center gap-2"
                              style={{
                                color: "var(--pv-silver)",
                                border: "1px solid var(--pv-border)",
                              }}
                              disabled={itemBusy}
                              onClick={() =>
                                editingMetadata === analysis.id
                                  ? cancelMetadataEdit()
                                  : beginMetadataEdit(analysis)
                              }
                            >
                              <Pencil size={13} />
                              {editingMetadata === analysis.id ? "Cancel editing" : "Edit metadata"}
                            </button>
                            {analysis.state === "needs_review" && (
                              <>
                                <button
                                  type="button"
                                  className="pv-btn-primary px-3 py-2 text-xs"
                                  disabled={itemBusy}
                                  onClick={() => void decide(analysis.id, "approve")}
                                >
                                  {itemAction === "approving" ? "Approving…" : "Approve proposal"}
                                </button>
                                <button
                                  type="button"
                                  className="rounded-md px-3 py-2 text-xs"
                                  style={{
                                    color: "var(--pv-silver)",
                                    border: "1px solid var(--pv-border)",
                                  }}
                                  disabled={itemBusy}
                                  onClick={() => void decide(analysis.id, "reject")}
                                >
                                  {itemAction === "rejecting" ? "Rejecting…" : "Reject"}
                                </button>
                              </>
                            )}
                            {analysis.state === "approved" && (
                              <button
                                type="button"
                                className="pv-btn-primary px-3 py-2 text-xs"
                                disabled={itemBusy}
                                onClick={() => void moveApproved(analysis.id)}
                              >
                                {itemAction === "moving" ? "Queueing move…" : "Move approved file"}
                              </button>
                            )}
                            {analysis.state === "move_failed" && (
                              <button
                                type="button"
                                className="pv-btn-primary px-3 py-2 text-xs"
                                disabled={itemBusy}
                                onClick={() => void moveApproved(analysis.id)}
                              >
                                {itemAction === "moving" ? "Retrying…" : "Retry safe move"}
                              </button>
                            )}
                            {analysis.state === "move_queued" && (
                              <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                                Vault Master will move this file in the background.
                              </span>
                            )}
                            {analysis.state === "moving" && (
                              <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
                                Verifying and moving in the background…
                              </span>
                            )}
                          </div>
                        )}
                      {analysis && itemActionErrors[analysis.id] && (
                        <p className="mt-3 text-xs text-red-300" role="alert">
                          {itemActionErrors[analysis.id]}
                        </p>
                      )}
                      {analysis &&
                        !duplicate &&
                        editingMetadata === analysis.id &&
                        metadataDraft && (
                          <form
                            className="mt-4 max-w-2xl rounded-lg p-4 space-y-4"
                            style={{
                              backgroundColor: "var(--pv-bg-elev)",
                              border: "1px solid var(--pv-border)",
                            }}
                            onSubmit={(event) => {
                              event.preventDefault();
                              void saveMetadata(analysis);
                            }}
                          >
                            <div>
                              <p
                                className="text-sm font-semibold"
                                style={{ color: "var(--pv-silver)" }}
                              >
                                Edit descriptive metadata
                              </p>
                              <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                                File facts remain unchanged. Restoring a detected value removes your
                                override.
                              </p>
                            </div>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                              <MetadataField
                                label={isMusicItem(analysis) ? "Track title" : "Display title"}
                                value={metadataDraft.display_title}
                                detectedValue={detectedMetadata(analysis).display_title}
                                onChange={(value) =>
                                  setMetadataDraft((current) =>
                                    current ? { ...current, display_title: value } : current,
                                  )
                                }
                                onRestore={() =>
                                  setMetadataDraft((current) =>
                                    current
                                      ? {
                                          ...current,
                                          display_title: detectedMetadata(analysis).display_title,
                                        }
                                      : current,
                                  )
                                }
                              />
                              {isMusicItem(analysis) ? (
                                <>
                                  <MetadataField
                                    label="Artist or band"
                                    value={metadataDraft.artist}
                                    detectedValue={detectedMetadata(analysis).artist}
                                    placeholder="Not detected"
                                    onChange={(artist) =>
                                      setMetadataDraft((current) =>
                                        current ? { ...current, artist } : current,
                                      )
                                    }
                                    onRestore={() =>
                                      setMetadataDraft((current) =>
                                        current
                                          ? {
                                              ...current,
                                              artist: detectedMetadata(analysis).artist,
                                            }
                                          : current,
                                      )
                                    }
                                  />
                                  <MetadataField
                                    label="Album"
                                    value={metadataDraft.album}
                                    detectedValue={detectedMetadata(analysis).album}
                                    placeholder="Not detected"
                                    onChange={(album) =>
                                      setMetadataDraft((current) =>
                                        current ? { ...current, album } : current,
                                      )
                                    }
                                    onRestore={() =>
                                      setMetadataDraft((current) =>
                                        current
                                          ? { ...current, album: detectedMetadata(analysis).album }
                                          : current,
                                      )
                                    }
                                  />
                                  <MetadataField
                                    label="Album artist"
                                    value={metadataDraft.album_artist}
                                    detectedValue={detectedMetadata(analysis).album_artist}
                                    placeholder="Not detected"
                                    onChange={(album_artist) =>
                                      setMetadataDraft((current) =>
                                        current ? { ...current, album_artist } : current,
                                      )
                                    }
                                    onRestore={() =>
                                      setMetadataDraft((current) =>
                                        current
                                          ? {
                                              ...current,
                                              album_artist: detectedMetadata(analysis).album_artist,
                                            }
                                          : current,
                                      )
                                    }
                                  />
                                  <MetadataField
                                    label="Track number"
                                    type="number"
                                    value={metadataDraft.track_number}
                                    detectedValue={detectedMetadata(analysis).track_number}
                                    placeholder="Not detected"
                                    onChange={(track_number) =>
                                      setMetadataDraft((current) =>
                                        current ? { ...current, track_number } : current,
                                      )
                                    }
                                    onRestore={() =>
                                      setMetadataDraft((current) =>
                                        current
                                          ? {
                                              ...current,
                                              track_number: detectedMetadata(analysis).track_number,
                                            }
                                          : current,
                                      )
                                    }
                                  />
                                  <MetadataField
                                    label="Disc number"
                                    type="number"
                                    value={metadataDraft.disc_number}
                                    detectedValue={detectedMetadata(analysis).disc_number}
                                    placeholder="Not detected"
                                    onChange={(disc_number) =>
                                      setMetadataDraft((current) =>
                                        current ? { ...current, disc_number } : current,
                                      )
                                    }
                                    onRestore={() =>
                                      setMetadataDraft((current) =>
                                        current
                                          ? {
                                              ...current,
                                              disc_number: detectedMetadata(analysis).disc_number,
                                            }
                                          : current,
                                      )
                                    }
                                  />
                                  <MetadataField
                                    label="Release year"
                                    type="number"
                                    value={metadataDraft.release_year}
                                    detectedValue={detectedMetadata(analysis).release_year}
                                    placeholder="YYYY"
                                    onChange={(release_year) =>
                                      setMetadataDraft((current) =>
                                        current ? { ...current, release_year } : current,
                                      )
                                    }
                                    onRestore={() =>
                                      setMetadataDraft((current) =>
                                        current
                                          ? {
                                              ...current,
                                              release_year: detectedMetadata(analysis).release_year,
                                            }
                                          : current,
                                      )
                                    }
                                  />
                                </>
                              ) : (
                                <>
                                  <MetadataField
                                    label="Capture or record date"
                                    type="date"
                                    value={metadataDraft.captured_on}
                                    detectedValue={detectedMetadata(analysis).captured_on}
                                    onChange={(captured_on) =>
                                      setMetadataDraft((current) =>
                                        current ? { ...current, captured_on } : current,
                                      )
                                    }
                                    onRestore={() =>
                                      setMetadataDraft((current) =>
                                        current
                                          ? {
                                              ...current,
                                              captured_on: detectedMetadata(analysis).captured_on,
                                            }
                                          : current,
                                      )
                                    }
                                  />
                                  <MetadataField
                                    label="Location"
                                    value={metadataDraft.location}
                                    detectedValue={detectedMetadata(analysis).location}
                                    placeholder="Not detected"
                                    onChange={(location) =>
                                      setMetadataDraft((current) =>
                                        current ? { ...current, location } : current,
                                      )
                                    }
                                    onRestore={() =>
                                      setMetadataDraft((current) =>
                                        current
                                          ? {
                                              ...current,
                                              location: detectedMetadata(analysis).location,
                                            }
                                          : current,
                                      )
                                    }
                                  />
                                </>
                              )}
                            </div>
                            <div className="flex flex-wrap justify-end gap-2">
                              <button
                                type="button"
                                className="rounded-md px-3 py-2 text-xs"
                                style={{
                                  color: "var(--pv-silver)",
                                  border: "1px solid var(--pv-border)",
                                }}
                                disabled={savingDecision === analysis.id}
                                onClick={cancelMetadataEdit}
                              >
                                Cancel
                              </button>
                              <button
                                type="submit"
                                className="pv-btn-primary px-3 py-2 text-xs"
                                disabled={savingDecision === analysis.id}
                              >
                                {savingDecision === analysis.id ? "Saving…" : "Save corrections"}
                              </button>
                            </div>
                          </form>
                        )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}
      <VaultHistory />
    </div>
  );
}

function VaultHistory() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<VaultAsset[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [expandedAsset, setExpandedAsset] = useState<string | null>(null);

  async function searchCatalogue(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const cleanedQuery = query.trim();
    if (!cleanedQuery) return;
    setSearching(true);
    setSearchError(null);
    setExpandedAsset(null);

    try {
      const response = await fetch(
        `/api/vault-master/assets/search?query=${encodeURIComponent(cleanedQuery)}`,
        {
          credentials: "include",
          headers: { Accept: "application/json" },
        },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) throw new Error("Catalogue search failed");
      setResults(((await response.json()) as VaultAssetSearchResult).assets);
    } catch {
      setSearchError("Vault history is currently unavailable.");
    } finally {
      setSearching(false);
    }
  }

  return (
    <section className="space-y-4 pt-4" aria-labelledby="vault-history-heading">
      <div className="flex items-start gap-3">
        <div
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
          style={{ color: "var(--pv-gold)", border: "1px solid var(--pv-border)" }}
        >
          <Database className="h-4 w-4" />
        </div>
        <div>
          <p
            className="text-[11px] uppercase tracking-[0.22em]"
            style={{ color: "var(--pv-gold)" }}
          >
            Vault Master history
          </p>
          <h3
            id="vault-history-heading"
            className="mt-1 text-lg font-semibold"
            style={{ color: "var(--pv-silver)" }}
          >
            Find an existing Vault asset
          </h3>
          <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Search the permanent catalogue by title, filename, location, or Vault path.
          </p>
        </div>
      </div>

      <form className="flex flex-col gap-2 sm:flex-row" onSubmit={searchCatalogue}>
        <label className="sr-only" htmlFor="vault-history-search">
          Search Vault history
        </label>
        <div className="relative flex-1">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2"
            style={{ color: "var(--pv-text-dim)" }}
          />
          <input
            id="vault-history-search"
            type="search"
            value={query}
            maxLength={240}
            placeholder="Title, filename, location, or /vault path"
            className="w-full rounded-md bg-transparent py-3 pl-10 pr-3 text-sm outline-none"
            style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <button
          type="submit"
          className="pv-btn-primary inline-flex items-center justify-center gap-2 px-5 py-3 text-sm"
          disabled={searching || !query.trim()}
        >
          <Search className={`h-4 w-4 ${searching ? "animate-pulse" : ""}`} />
          {searching ? "Searching…" : "Search history"}
        </button>
      </form>

      {searchError && (
        <p
          className="rounded-md px-4 py-3 text-xs"
          style={{ color: "#fca5a5", border: "1px solid rgba(248,113,113,0.3)" }}
        >
          {searchError}
        </p>
      )}

      {results !== null && (
        <div
          className="overflow-hidden rounded-lg"
          style={{ background: "var(--pv-panel)", border: "1px solid var(--pv-border)" }}
        >
          <div
            className="flex items-center justify-between px-4 py-3"
            style={{ borderBottom: results.length ? "1px solid var(--pv-border)" : undefined }}
          >
            <span className="text-sm font-medium" style={{ color: "var(--pv-silver)" }}>
              Search results
            </span>
            <span className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
              {results.length} {results.length === 1 ? "asset" : "assets"}
            </span>
          </div>
          {results.length === 0 ? (
            <p className="px-4 py-8 text-center text-sm" style={{ color: "var(--pv-text-dim)" }}>
              No matching Vault assets were found.
            </p>
          ) : (
            results.map((asset) => {
              const expanded = expandedAsset === asset.id;
              return (
                <article
                  key={`${asset.id}:${asset.vault_path}`}
                  className="px-4 py-4"
                  style={{ borderBottom: "1px solid var(--pv-border)" }}
                >
                  <button
                    type="button"
                    className="flex w-full items-start justify-between gap-4 text-left"
                    aria-expanded={expanded}
                    onClick={() => setExpandedAsset(expanded ? null : asset.id)}
                  >
                    <span>
                      <span
                        className="block text-sm font-medium"
                        style={{ color: "var(--pv-silver)" }}
                      >
                        {asset.display_title}
                      </span>
                      <span className="mt-1 block text-xs" style={{ color: "var(--pv-text-dim)" }}>
                        {asset.asset_type}
                        {asset.captured_on ? ` · ${formatPhotoDate(asset.captured_on)}` : ""}
                        {asset.location ? ` · ${asset.location}` : ""}
                      </span>
                    </span>
                    <span className="text-xs" style={{ color: "var(--pv-gold)" }}>
                      {expanded ? "Hide details" : "View details"}
                    </span>
                  </button>
                  {expanded && (
                    <VaultAssetDetails
                      asset={asset}
                      onUpdated={(updated) =>
                        setResults(
                          (current) =>
                            current?.map((candidate) =>
                              candidate.id === updated.id ? updated : candidate,
                            ) ?? current,
                        )
                      }
                      onDeleted={(assetId) => {
                        setResults(
                          (current) =>
                            current?.filter((candidate) => candidate.id !== assetId) ?? current,
                        );
                        setExpandedAsset(null);
                      }}
                    />
                  )}
                </article>
              );
            })
          )}
        </div>
      )}
    </section>
  );
}

function CatalogueRecovery() {
  const navigate = useNavigate();
  const [assessment, setAssessment] = useState<SidecarRecoveryAssessment | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const loadAssessment = useCallback(async () => {
    setLoading(true);
    setMessage(null);
    try {
      const response = await fetch("/api/vault-master/sidecars/recovery/assessment", {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) throw new Error("Recovery assessment failed");
      const result = (await response.json()) as SidecarRecoveryAssessment;
      setAssessment(result);
      setSelected(
        new Set(
          [...selected].filter((assetId) =>
            result.candidates.some(
              (candidate) => candidate.status === "restorable" && candidate.asset_id === assetId,
            ),
          ),
        ),
      );
    } catch {
      setMessage("Catalogue recovery assessment is currently unavailable.");
    } finally {
      setLoading(false);
    }
  }, [navigate, selected]);

  useEffect(() => {
    void loadAssessment();
    // Assessment is loaded once; the refresh control handles later checks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const restorable =
    assessment?.candidates.filter((candidate) => candidate.status === "restorable") ?? [];
  const attention =
    assessment?.candidates.filter((candidate) =>
      ["conflict", "path_conflict", "invalid", "unsupported"].includes(candidate.status),
    ) ?? [];

  async function restoreSelected() {
    setRestoring(true);
    setMessage(null);
    let restored = 0;
    let failed = 0;
    for (const assetId of selected) {
      try {
        const response = await fetch(
          `/api/vault-master/sidecars/recovery/${encodeURIComponent(assetId)}/restore`,
          {
            method: "POST",
            credentials: "include",
            headers: { Accept: "application/json", "Content-Type": "application/json" },
            body: JSON.stringify({ confirm: true }),
          },
        );
        if (response.status === 401) {
          await navigate({ to: "/login" });
          return;
        }
        if (!response.ok) throw new Error("Restore refused");
        restored += 1;
      } catch {
        failed += 1;
      }
    }
    setSelected(new Set());
    setConfirming(false);
    setRestoring(false);
    setMessage(
      failed
        ? `${restored} catalogue record(s) restored; ${failed} safely refused.`
        : `${restored} catalogue record(s) restored.`,
    );
    await loadAssessment();
  }

  return (
    <div
      className="rounded-lg p-4"
      style={{ background: "var(--pv-panel)", border: "1px solid var(--pv-border)" }}
    >
      <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
        <div>
          <h4 className="text-sm font-medium" style={{ color: "var(--pv-silver)" }}>
            Catalogue recovery
          </h4>
          <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
            Restore missing catalogue records from verified Vault Master sidecars. Files are never
            moved, replaced, or overwritten.
          </p>
        </div>
        <button
          type="button"
          className="pv-btn-secondary inline-flex items-center justify-center gap-2 px-3 py-2 text-xs"
          disabled={loading || restoring}
          onClick={() => void loadAssessment()}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          Assess recovery
        </button>
      </div>

      {assessment && (
        <div className="mt-4 space-y-3">
          <div className="flex flex-wrap gap-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
            <span>{assessment.current} current</span>
            <span>·</span>
            <span>{assessment.restorable} safely restorable</span>
            <span>·</span>
            <span>{assessment.conflicting + assessment.path_conflicts} conflicts</span>
            <span>·</span>
            <span>{assessment.invalid + assessment.unsupported} unreadable</span>
          </div>

          {restorable.map((candidate) => (
            <label
              key={candidate.sidecar_name}
              className="flex cursor-pointer items-start gap-3 rounded-md p-3"
              style={{ border: "1px solid var(--pv-border)" }}
            >
              <input
                type="checkbox"
                className="mt-1"
                checked={Boolean(candidate.asset_id && selected.has(candidate.asset_id))}
                onChange={(event) => {
                  if (!candidate.asset_id) return;
                  setSelected((current) => {
                    const next = new Set(current);
                    if (event.target.checked) next.add(candidate.asset_id!);
                    else next.delete(candidate.asset_id!);
                    return next;
                  });
                  setConfirming(false);
                }}
              />
              <span>
                <span className="block text-sm" style={{ color: "var(--pv-silver)" }}>
                  {candidate.display_title ?? candidate.filename ?? candidate.sidecar_name}
                </span>
                <span className="mt-1 block text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  {candidate.vault_path} · {candidate.detail}
                </span>
              </span>
            </label>
          ))}

          {attention.length > 0 && (
            <details>
              <summary className="cursor-pointer text-xs" style={{ color: "var(--pv-gold)" }}>
                Review {attention.length} sidecar(s) requiring attention
              </summary>
              <div className="mt-2 space-y-2">
                {attention.map((candidate) => (
                  <div
                    key={candidate.sidecar_name}
                    className="rounded-md p-3 text-xs"
                    style={{ color: "var(--pv-text-dim)", border: "1px solid var(--pv-border)" }}
                  >
                    <span className="font-medium" style={{ color: "var(--pv-silver)" }}>
                      {candidate.display_title ?? candidate.sidecar_name}
                    </span>
                    <span className="ml-2 uppercase">{candidate.status.replace("_", " ")}</span>
                    <p className="mt-1">{candidate.detail}</p>
                  </div>
                ))}
              </div>
            </details>
          )}

          {selected.size > 0 && !confirming && (
            <button
              type="button"
              className="pv-btn-primary px-4 py-2 text-xs"
              onClick={() => setConfirming(true)}
            >
              Review restore ({selected.size})
            </button>
          )}
          {confirming && (
            <div
              className="rounded-md p-3 text-xs"
              style={{ border: "1px solid var(--pv-gold)", color: "var(--pv-text-dim)" }}
            >
              <p>
                Restore {selected.size} missing catalogue record(s)? Each permanent file will be
                verified by size and SHA-256 before its record is restored.
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  className="pv-btn-secondary px-3 py-2"
                  disabled={restoring}
                  onClick={() => setConfirming(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="pv-btn-primary px-3 py-2"
                  disabled={restoring}
                  onClick={() => void restoreSelected()}
                >
                  {restoring ? "Restoring…" : "Confirm restore"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}
      {message && (
        <p className="mt-3 text-xs" style={{ color: "var(--pv-text-dim)" }}>
          {message}
        </p>
      )}
    </div>
  );
}

function VaultAssetDetails({
  asset,
  onUpdated,
  onDeleted,
}: {
  asset: VaultAsset;
  onUpdated: (asset: VaultAsset) => void;
  onDeleted: (assetId: string) => void;
}) {
  const navigate = useNavigate();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [showQuarantineRequest, setShowQuarantineRequest] = useState(false);
  const [quarantineReason, setQuarantineReason] = useState("");
  const [requestingQuarantine, setRequestingQuarantine] = useState(false);
  const [cancellingQuarantine, setCancellingQuarantine] = useState(false);
  const [quarantineError, setQuarantineError] = useState<string | null>(null);
  const [quarantineRequested, setQuarantineRequested] = useState(false);
  const [quarantinePreflight, setQuarantinePreflight] = useState<QuarantinePreflight | null>(null);
  const [checkingQuarantine, setCheckingQuarantine] = useState(false);
  const [confirmingQuarantine, setConfirmingQuarantine] = useState(false);
  const [deletionPreflight, setDeletionPreflight] = useState<PermanentDeletionPreflight | null>(
    null,
  );
  const [checkingDeletion, setCheckingDeletion] = useState(false);
  const [showDeletionRequest, setShowDeletionRequest] = useState(false);
  const [deletionReason, setDeletionReason] = useState("");
  const [requestingDeletion, setRequestingDeletion] = useState(false);
  const [cancellingDeletion, setCancellingDeletion] = useState(false);
  const [confirmingDeletion, setConfirmingDeletion] = useState(false);
  const [showDeletionExecution, setShowDeletionExecution] = useState(false);
  const [executionFilename, setExecutionFilename] = useState("");
  const [executingDeletion, setExecutingDeletion] = useState(false);
  const [deletionError, setDeletionError] = useState<string | null>(null);
  const [history, setHistory] = useState<VaultAssetHistoryEntry[] | null>(null);
  const [historyError, setHistoryError] = useState(false);
  const [historyVersion, setHistoryVersion] = useState(0);
  const [relationshipCandidates, setRelationshipCandidates] = useState<
    AssetRelationshipCandidateListing["candidates"] | null
  >(null);
  const [canonicalRelationships, setCanonicalRelationships] = useState<
    CanonicalAssetRelationshipListing["relationships"] | null
  >(null);
  const [relationshipVersion, setRelationshipVersion] = useState(0);
  const [relationshipError, setRelationshipError] = useState(false);
  const [requestingRelationship, setRequestingRelationship] = useState<string | null>(null);
  const [retainingRelationship, setRetainingRelationship] = useState<string | null>(null);
  const [linkingRelationship, setLinkingRelationship] = useState<string | null>(null);
  const [relationshipRequestError, setRelationshipRequestError] = useState<string | null>(null);
  const [draft, setDraft] = useState({
    display_title: asset.display_title,
    captured_on: asset.captured_on ?? "",
    location: asset.location ?? "",
  });
  const provenance = [
    ["Title", asset.metadata_provenance.display_title],
    ["Date", asset.metadata_provenance.captured_on],
    ["Location", asset.metadata_provenance.location],
  ].filter((entry): entry is [string, string] => Boolean(entry[1]));
  const latestQuarantineReview = history?.find(
    (entry) =>
      entry.action === "quarantine_review_requested" ||
      entry.action === "quarantine_review_cancelled" ||
      entry.action === "quarantined",
  );
  const hasQuarantineReview =
    quarantineRequested || latestQuarantineReview?.action === "quarantine_review_requested";
  const isQuarantined = asset.vault_path.startsWith("/vault/Quarantine/");
  const latestDeletionReview = history?.find(
    (entry) =>
      entry.action === "permanent_deletion_review_requested" ||
      entry.action === "permanent_deletion_review_cancelled" ||
      entry.action === "permanent_deletion_confirmed" ||
      entry.action === "permanently_deleted",
  );
  const hasDeletionReview = latestDeletionReview?.action === "permanent_deletion_review_requested";
  const deletionConfirmed = latestDeletionReview?.action === "permanent_deletion_confirmed";

  useEffect(() => {
    const controller = new AbortController();
    setHistory(null);
    setHistoryError(false);
    void fetch(`/api/vault-master/assets/${asset.id}/history`, {
      credentials: "include",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 401) {
          await navigate({ to: "/login" });
          return null;
        }
        if (!response.ok) throw new Error("History request failed");
        return (await response.json()) as VaultAssetHistory;
      })
      .then((body) => {
        if (body) setHistory(body.entries);
      })
      .catch((error) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setHistoryError(true);
        }
      });
    return () => controller.abort();
  }, [asset.id, historyVersion, navigate]);

  useEffect(() => {
    const controller = new AbortController();
    setRelationshipCandidates(null);
    setRelationshipError(false);
    setCanonicalRelationships(null);
    void Promise.all([
      fetch(`/api/vault-master/assets/${asset.id}/relationships/analysis`, {
        credentials: "include",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      }),
      fetch(`/api/vault-master/assets/${asset.id}/relationships`, {
        credentials: "include",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      }),
    ])
      .then(async ([analysisResponse, relationshipsResponse]) => {
        if (analysisResponse.status === 401 || relationshipsResponse.status === 401) {
          await navigate({ to: "/login" });
          return null;
        }
        if (!analysisResponse.ok || !relationshipsResponse.ok) {
          throw new Error("Relationship analysis request failed");
        }
        return {
          analysis: (await analysisResponse.json()) as AssetRelationshipCandidateListing,
          canonical: (await relationshipsResponse.json()) as CanonicalAssetRelationshipListing,
        };
      })
      .then((body) => {
        if (body) {
          setRelationshipCandidates(body.analysis.candidates);
          setCanonicalRelationships(body.canonical.relationships);
        }
      })
      .catch((error) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setRelationshipError(true);
        }
      });
    return () => controller.abort();
  }, [asset.id, navigate, relationshipVersion]);

  async function requestRelationshipReview(candidateAssetId: string) {
    setRequestingRelationship(candidateAssetId);
    setRelationshipRequestError(null);
    try {
      const response = await fetch(`/api/vault-master/assets/${asset.id}/relationships/review`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ candidate_asset_id: candidateAssetId }),
      });
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Relationship review request failed");
      }
      const entry = (await response.json()) as VaultAssetHistoryEntry;
      setHistory((current) => (current ? [entry, ...current] : [entry]));
      setHistoryVersion((current) => current + 1);
    } catch (requestError) {
      setRelationshipRequestError(
        requestError instanceof Error
          ? requestError.message
          : "The relationship review could not be requested.",
      );
    } finally {
      setRequestingRelationship(null);
    }
  }

  async function retainSeparateRelationship(candidateAssetId: string) {
    setRetainingRelationship(candidateAssetId);
    setRelationshipRequestError(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/relationships/review/retain`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ candidate_asset_id: candidateAssetId }),
        },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Relationship review resolution failed");
      }
      const entry = (await response.json()) as VaultAssetHistoryEntry;
      setHistory((current) => (current ? [entry, ...current] : [entry]));
      setHistoryVersion((current) => current + 1);
    } catch (resolutionError) {
      setRelationshipRequestError(
        resolutionError instanceof Error
          ? resolutionError.message
          : "The relationship review could not be resolved.",
      );
    } finally {
      setRetainingRelationship(null);
    }
  }

  async function linkCanonicalRelationship(candidateAssetId: string) {
    setLinkingRelationship(candidateAssetId);
    setRelationshipRequestError(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/relationships/review/link`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ candidate_asset_id: candidateAssetId }),
        },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Canonical relationship creation failed");
      }
      const entry = (await response.json()) as VaultAssetHistoryEntry;
      setHistory((current) => (current ? [entry, ...current] : [entry]));
      setHistoryVersion((current) => current + 1);
      setRelationshipVersion((current) => current + 1);
    } catch (linkError) {
      setRelationshipRequestError(
        linkError instanceof Error
          ? linkError.message
          : "The canonical relationship could not be created.",
      );
    } finally {
      setLinkingRelationship(null);
    }
  }

  async function saveChanges(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const changes: Record<string, string | null> = {};
    const current = {
      display_title: asset.display_title,
      captured_on: asset.captured_on ?? "",
      location: asset.location ?? "",
    };
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
      const response = await fetch(`/api/vault-master/assets/${asset.id}/metadata`, {
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
      const updated = (await response.json()) as VaultAsset;
      onUpdated(updated);
      setDraft({
        display_title: updated.display_title,
        captured_on: updated.captured_on ?? "",
        location: updated.location ?? "",
      });
      setHistoryVersion((current) => current + 1);
      setEditing(false);
    } catch (metadataError) {
      setEditError(
        metadataError instanceof Error
          ? metadataError.message
          : "The permanent metadata could not be updated.",
      );
    } finally {
      setSaving(false);
    }
  }

  async function requestQuarantineReview(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setRequestingQuarantine(true);
    setQuarantineError(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/lifecycle/quarantine-review`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ reason: quarantineReason.trim() || null }),
        },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Quarantine review request failed");
      }
      setQuarantineRequested(true);
      setShowQuarantineRequest(false);
      setHistoryVersion((current) => current + 1);
    } catch (requestError) {
      setQuarantineError(
        requestError instanceof Error
          ? requestError.message
          : "The quarantine review could not be requested.",
      );
    } finally {
      setRequestingQuarantine(false);
    }
  }

  async function cancelQuarantineReview() {
    setCancellingQuarantine(true);
    setQuarantineError(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/lifecycle/quarantine-review/cancel`,
        {
          method: "POST",
          credentials: "include",
          headers: { Accept: "application/json" },
        },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Quarantine review withdrawal failed");
      }
      setQuarantineRequested(false);
      setQuarantinePreflight(null);
      setHistoryVersion((current) => current + 1);
    } catch (requestError) {
      setQuarantineError(
        requestError instanceof Error
          ? requestError.message
          : "The quarantine review could not be withdrawn.",
      );
    } finally {
      setCancellingQuarantine(false);
    }
  }

  async function checkQuarantineMove() {
    setCheckingQuarantine(true);
    setQuarantineError(null);
    setQuarantinePreflight(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/lifecycle/quarantine-preflight`,
        { credentials: "include", headers: { Accept: "application/json" } },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Quarantine preflight failed");
      }
      setQuarantinePreflight((await response.json()) as QuarantinePreflight);
    } catch (preflightError) {
      setQuarantineError(
        preflightError instanceof Error
          ? preflightError.message
          : "The quarantine move could not be checked.",
      );
    } finally {
      setCheckingQuarantine(false);
    }
  }

  async function confirmQuarantineMove() {
    setConfirmingQuarantine(true);
    setQuarantineError(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/lifecycle/quarantine-confirm`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ confirm: true }),
        },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Quarantine confirmation failed");
      }
      const updated = (await response.json()) as VaultAsset;
      onUpdated(updated);
      setQuarantineRequested(false);
      setQuarantinePreflight(null);
      setHistoryVersion((current) => current + 1);
    } catch (confirmationError) {
      setQuarantineError(
        confirmationError instanceof Error
          ? confirmationError.message
          : "The file could not be moved to Quarantine.",
      );
    } finally {
      setConfirmingQuarantine(false);
    }
  }

  async function checkPermanentDeletion() {
    setCheckingDeletion(true);
    setDeletionError(null);
    setDeletionPreflight(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/lifecycle/permanent-deletion-preflight`,
        { credentials: "include", headers: { Accept: "application/json" } },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Permanent-deletion preflight failed");
      }
      setDeletionPreflight((await response.json()) as PermanentDeletionPreflight);
    } catch (preflightError) {
      setDeletionError(
        preflightError instanceof Error
          ? preflightError.message
          : "Permanent-deletion eligibility could not be checked.",
      );
    } finally {
      setCheckingDeletion(false);
    }
  }

  async function requestPermanentDeletion(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setRequestingDeletion(true);
    setDeletionError(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/lifecycle/permanent-deletion-review`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ reason: deletionReason.trim() }),
        },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Permanent-deletion review request failed");
      }
      setShowDeletionRequest(false);
      setDeletionReason("");
      setDeletionPreflight(null);
      setHistoryVersion((current) => current + 1);
    } catch (requestError) {
      setDeletionError(
        requestError instanceof Error
          ? requestError.message
          : "The permanent-deletion review could not be requested.",
      );
    } finally {
      setRequestingDeletion(false);
    }
  }

  async function cancelPermanentDeletionReview() {
    setCancellingDeletion(true);
    setDeletionError(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/lifecycle/permanent-deletion-review/cancel`,
        { method: "POST", credentials: "include", headers: { Accept: "application/json" } },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Permanent-deletion review withdrawal failed");
      }
      setHistoryVersion((current) => current + 1);
    } catch (requestError) {
      setDeletionError(
        requestError instanceof Error
          ? requestError.message
          : "The permanent-deletion review could not be withdrawn.",
      );
    } finally {
      setCancellingDeletion(false);
    }
  }

  async function confirmPermanentDeletionReview() {
    setConfirmingDeletion(true);
    setDeletionError(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/lifecycle/permanent-deletion-confirm`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ confirm: true }),
        },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Permanent-deletion confirmation failed");
      }
      setDeletionPreflight(null);
      setHistoryVersion((current) => current + 1);
    } catch (confirmationError) {
      setDeletionError(
        confirmationError instanceof Error
          ? confirmationError.message
          : "The permanent-deletion review could not be confirmed.",
      );
    } finally {
      setConfirmingDeletion(false);
    }
  }

  async function executePermanentDeletion(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (executionFilename !== asset.filename) return;
    setExecutingDeletion(true);
    setDeletionError(null);
    try {
      const response = await fetch(
        `/api/vault-master/assets/${asset.id}/lifecycle/permanent-deletion-execute`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ execute: true }),
        },
      );
      if (response.status === 401) {
        await navigate({ to: "/login" });
        return;
      }
      if (!response.ok) {
        const body = (await response.json()) as { detail?: string };
        throw new Error(body.detail ?? "Permanent deletion failed");
      }
      onDeleted(asset.id);
    } catch (executionError) {
      setDeletionError(
        executionError instanceof Error
          ? executionError.message
          : "The confirmed file could not be permanently deleted.",
      );
    } finally {
      setExecutingDeletion(false);
    }
  }

  return (
    <div
      className="mt-4 rounded-md p-4 text-xs"
      style={{ background: "var(--pv-bg)", border: "1px solid var(--pv-border)" }}
    >
      <VaultAssetPreview asset={asset} />
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <p className="font-medium" style={{ color: "var(--pv-silver)" }}>
            Permanent file
          </p>
          <p className="break-all" style={{ color: "var(--pv-text-dim)" }}>
            {asset.vault_path}
          </p>
          <p style={{ color: "var(--pv-text-dim)" }}>
            {asset.filename} · {formatBytes(asset.size_bytes)} · {asset.mime_type}
          </p>
          <p className="break-all font-mono text-[10px]" style={{ color: "var(--pv-text-dim)" }}>
            SHA-256 {asset.sha256}
          </p>
        </div>
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3">
            <p className="font-medium" style={{ color: "var(--pv-silver)" }}>
              Metadata provenance
            </p>
            {!editing && (
              <button
                type="button"
                className="inline-flex items-center gap-1"
                style={{ color: "var(--pv-gold)" }}
                onClick={() => {
                  setEditError(null);
                  setEditing(true);
                }}
              >
                <Pencil className="h-3 w-3" />
                Edit metadata
              </button>
            )}
          </div>
          {provenance.length ? (
            provenance.map(([label, source]) => (
              <p key={label} className="flex justify-between gap-4">
                <span style={{ color: "var(--pv-text-dim)" }}>{label}</span>
                <span style={{ color: "var(--pv-silver)" }}>{source.replaceAll("_", " ")}</span>
              </p>
            ))
          ) : (
            <p style={{ color: "var(--pv-text-dim)" }}>No provenance has been recorded.</p>
          )}
        </div>
      </div>

      {editing && (
        <form
          className="mt-4 space-y-4 pt-4"
          style={{ borderTop: "1px solid var(--pv-border)" }}
          onSubmit={saveChanges}
        >
          <p className="font-medium" style={{ color: "var(--pv-silver)" }}>
            Edit permanent descriptive metadata
          </p>
          <p style={{ color: "var(--pv-text-dim)" }}>
            The physical file and technical facts remain unchanged. This correction will be recorded
            in Vault history.
          </p>
          <div className="grid gap-4 md:grid-cols-3">
            <PermanentMetadataField
              label="Display title"
              value={draft.display_title}
              onChange={(value) => setDraft((current) => ({ ...current, display_title: value }))}
            />
            <PermanentMetadataField
              label="Capture or record date"
              type="date"
              value={draft.captured_on}
              onChange={(value) => setDraft((current) => ({ ...current, captured_on: value }))}
            />
            <PermanentMetadataField
              label="Location"
              value={draft.location}
              placeholder="Not recorded"
              onChange={(value) => setDraft((current) => ({ ...current, location: value }))}
            />
          </div>
          {editError && <p style={{ color: "#fca5a5" }}>{editError}</p>}
          <div className="flex flex-wrap justify-end gap-2">
            <button
              type="button"
              className="rounded-md px-3 py-2"
              style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
              disabled={saving}
              onClick={() => {
                setDraft({
                  display_title: asset.display_title,
                  captured_on: asset.captured_on ?? "",
                  location: asset.location ?? "",
                });
                setEditing(false);
                setEditError(null);
              }}
            >
              Cancel
            </button>
            <button type="submit" className="pv-btn-primary px-3 py-2" disabled={saving}>
              {saving ? "Saving…" : "Save correction"}
            </button>
          </div>
        </form>
      )}
      <section
        className="mt-4 space-y-3 pt-4"
        style={{ borderTop: "1px solid var(--pv-border)" }}
        aria-labelledby={`asset-relationships-${asset.id}`}
      >
        <div>
          <p
            id={`asset-relationships-${asset.id}`}
            className="font-medium"
            style={{ color: "var(--pv-silver)" }}
          >
            Possible duplicates and versions
          </p>
          <p className="mt-1" style={{ color: "var(--pv-text-dim)" }}>
            Vault Master analysis remains evidence until you explicitly retain both files or create
            a catalogue relationship. Neither choice changes either file.
          </p>
        </div>
        {relationshipCandidates === null && !relationshipError && (
          <p style={{ color: "var(--pv-text-dim)" }}>Checking this asset against your catalogue…</p>
        )}
        {relationshipError && (
          <p style={{ color: "#fca5a5" }}>Relationship analysis is currently unavailable.</p>
        )}
        {relationshipCandidates?.length === 0 && (
          <p style={{ color: "var(--pv-text-dim)" }}>No relationship candidates were found.</p>
        )}
        {canonicalRelationships && canonicalRelationships.length > 0 && (
          <div className="space-y-2">
            <p className="font-medium" style={{ color: "var(--pv-silver)" }}>
              Canonical relationships
            </p>
            {canonicalRelationships.map((relationship) => {
              const other = relationship.affected_files.find((file) => file.asset_id !== asset.id);
              return (
                <p
                  key={`${relationship.relationship_type}:${other?.asset_id ?? "unknown"}`}
                  className="rounded-md p-2"
                  style={{ border: "1px solid var(--pv-border)", color: "var(--pv-silver-dim)" }}
                >
                  <span className="capitalize" style={{ color: "var(--pv-gold)" }}>
                    {relationship.relationship_type.replaceAll("_", " ")}
                  </span>
                  {other ? ` · ${other.filename}` : ""} · {relationship.confidence} confidence
                </p>
              );
            })}
          </div>
        )}
        {relationshipRequestError && <p style={{ color: "#fca5a5" }}>{relationshipRequestError}</p>}
        {relationshipCandidates?.map((candidate) => {
          const candidateAsset = candidate.affected_files.find(
            (file) => file.asset_id !== asset.id,
          );
          if (!candidateAsset) return null;
          const latestReview = history?.find(
            (entry) =>
              (entry.action === "relationship_review_requested" ||
                entry.action === "relationship_review_retained" ||
                entry.action === "relationship_review_linked") &&
              entry.current_values.candidate_asset_id === candidateAsset.asset_id,
          );
          const reviewPending = latestReview?.action === "relationship_review_requested";
          const retainedSeparately = latestReview?.action === "relationship_review_retained";
          const canonicalRelationship = canonicalRelationships?.find((relationship) =>
            relationship.affected_files.some((file) => file.asset_id === candidateAsset.asset_id),
          );
          const relationshipLinked =
            latestReview?.action === "relationship_review_linked" ||
            canonicalRelationship !== undefined;
          return (
            <article
              key={`${candidate.classification}:${candidate.affected_files
                .map((file) => file.asset_id)
                .join(":")}`}
              className="rounded-md p-3"
              style={{ border: "1px solid var(--pv-border)", background: "var(--pv-panel)" }}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-medium capitalize" style={{ color: "var(--pv-gold)" }}>
                  {candidate.classification.replaceAll("_", " ")}
                </p>
                <p className="uppercase tracking-wide" style={{ color: "var(--pv-text-dim)" }}>
                  {candidate.confidence} confidence
                </p>
              </div>
              <ul
                className="mt-2 list-disc space-y-1 pl-4"
                style={{ color: "var(--pv-silver-dim)" }}
              >
                {candidate.evidence.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
              <div className="mt-3 grid gap-2 md:grid-cols-2">
                {candidate.affected_files.map((file) => (
                  <div
                    key={file.asset_id}
                    className="rounded p-2"
                    style={{ background: "var(--pv-bg)", border: "1px solid var(--pv-border)" }}
                  >
                    <p className="font-medium break-all" style={{ color: "var(--pv-silver)" }}>
                      {file.filename}
                    </p>
                    <p className="mt-1 break-all" style={{ color: "var(--pv-text-dim)" }}>
                      {file.vault_path}
                    </p>
                    <p className="mt-1" style={{ color: "var(--pv-text-dim)" }}>
                      {formatBytes(file.size_bytes)} · {file.mime_type}
                    </p>
                  </div>
                ))}
              </div>
              <div className="mt-3 flex flex-wrap justify-end gap-2">
                {reviewPending && !relationshipLinked && (
                  <button
                    type="button"
                    className="pv-btn-primary px-3 py-2"
                    disabled={linkingRelationship !== null || retainingRelationship !== null}
                    onClick={() => void linkCanonicalRelationship(candidateAsset.asset_id)}
                  >
                    {linkingRelationship === candidateAsset.asset_id
                      ? "Creating…"
                      : "Create relationship"}
                  </button>
                )}
                <button
                  type="button"
                  className="rounded-md px-3 py-2"
                  style={{
                    color:
                      retainedSeparately || relationshipLinked
                        ? "var(--pv-text-dim)"
                        : "var(--pv-gold)",
                    border: "1px solid var(--pv-border)",
                  }}
                  disabled={
                    Boolean(retainedSeparately) ||
                    relationshipLinked ||
                    requestingRelationship !== null ||
                    retainingRelationship !== null ||
                    linkingRelationship !== null ||
                    history === null
                  }
                  onClick={() =>
                    void (reviewPending
                      ? retainSeparateRelationship(candidateAsset.asset_id)
                      : requestRelationshipReview(candidateAsset.asset_id))
                  }
                >
                  {relationshipLinked
                    ? "Relationship created"
                    : retainedSeparately
                      ? "Retained separately"
                      : retainingRelationship === candidateAsset.asset_id
                        ? "Retaining…"
                        : reviewPending
                          ? "Retain both separately"
                          : requestingRelationship === candidateAsset.asset_id
                            ? "Requesting…"
                            : "Request relationship review"}
                </button>
              </div>
            </article>
          );
        })}
      </section>
      <section
        className="mt-4 space-y-3 pt-4"
        style={{ borderTop: "1px solid var(--pv-border)" }}
        aria-labelledby={`asset-lifecycle-${asset.id}`}
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p
              id={`asset-lifecycle-${asset.id}`}
              className="font-medium"
              style={{ color: "var(--pv-silver)" }}
            >
              File lifecycle
            </p>
            <p className="mt-1" style={{ color: "var(--pv-text-dim)" }}>
              A review request records the case for later action. It does not move, hide, or delete{" "}
              this file.
            </p>
          </div>
          {!showQuarantineRequest && !isQuarantined && (
            <div className="flex flex-wrap gap-2">
              {hasQuarantineReview && (
                <button
                  type="button"
                  className="rounded-md px-3 py-2"
                  style={{ color: "var(--pv-gold)", border: "1px solid var(--pv-border)" }}
                  disabled={checkingQuarantine || cancellingQuarantine}
                  onClick={() => void checkQuarantineMove()}
                >
                  {checkingQuarantine ? "Checking…" : "Check Bin move"}
                </button>
              )}
              <button
                type="button"
                className="rounded-md px-3 py-2"
                style={{ color: "var(--pv-gold)", border: "1px solid var(--pv-border)" }}
                disabled={cancellingQuarantine || checkingQuarantine}
                onClick={() => {
                  if (hasQuarantineReview) {
                    void cancelQuarantineReview();
                    return;
                  }
                  setQuarantineError(null);
                  setShowQuarantineRequest(true);
                }}
              >
                {hasQuarantineReview
                  ? cancellingQuarantine
                    ? "Withdrawing…"
                    : "Withdraw review request"
                  : "Request Bin review"}
              </button>
            </div>
          )}
        </div>
        {isQuarantined && (
          <p style={{ color: "var(--pv-gold)" }}>
            This file is in the recoverable Bin. Permanent deletion remains a separate review.
          </p>
        )}
        {quarantineError && !showQuarantineRequest && (
          <p style={{ color: "#fca5a5" }}>{quarantineError}</p>
        )}
        {quarantinePreflight && (
          <div
            className="space-y-2 rounded-md p-3"
            style={{ border: "1px solid var(--pv-border)", background: "var(--pv-surface)" }}
          >
            <p className="font-medium" style={{ color: "var(--pv-silver)" }}>
              {quarantinePreflight.ready ? "Bin move verified" : "Bin move blocked"}
            </p>
            {quarantinePreflight.proposed_quarantine_path && (
              <p className="break-all" style={{ color: "var(--pv-text-dim)" }}>
                Destination: {quarantinePreflight.proposed_quarantine_path}
              </p>
            )}
            <p style={{ color: "var(--pv-text-dim)" }}>
              Checksum {quarantinePreflight.checksum_verified ? "verified" : "not verified"}.
            </p>
            {quarantinePreflight.reason && (
              <p style={{ color: "#fca5a5" }}>{quarantinePreflight.reason}</p>
            )}
            {quarantinePreflight.ready && (
              <div className="flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  className="rounded-md px-3 py-2"
                  style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
                  disabled={confirmingQuarantine}
                  onClick={() => setQuarantinePreflight(null)}
                >
                  Keep in library
                </button>
                <button
                  type="button"
                  className="pv-btn-primary px-3 py-2"
                  disabled={confirmingQuarantine}
                  onClick={() => void confirmQuarantineMove()}
                >
                  {confirmingQuarantine ? "Moving…" : "Confirm recoverable move"}
                </button>
              </div>
            )}
          </div>
        )}
        {showQuarantineRequest && (
          <form className="space-y-3" onSubmit={requestQuarantineReview}>
            <label className="block space-y-1">
              <span style={{ color: "var(--pv-silver)" }}>Reason (optional)</span>
              <textarea
                className="min-h-20 w-full rounded-md px-3 py-2"
                maxLength={500}
                value={quarantineReason}
                onChange={(event) => setQuarantineReason(event.target.value)}
                style={{ background: "var(--pv-surface)", border: "1px solid var(--pv-border)" }}
              />
            </label>
            {quarantineError && <p style={{ color: "#fca5a5" }}>{quarantineError}</p>}
            <div className="flex flex-wrap justify-end gap-2">
              <button
                type="button"
                className="rounded-md px-3 py-2"
                style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
                disabled={requestingQuarantine}
                onClick={() => {
                  setShowQuarantineRequest(false);
                  setQuarantineReason("");
                  setQuarantineError(null);
                }}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="pv-btn-primary px-3 py-2"
                disabled={requestingQuarantine}
              >
                {requestingQuarantine ? "Requesting…" : "Record review request"}
              </button>
            </div>
          </form>
        )}
        {isQuarantined && (
          <div className="space-y-3 pt-4" style={{ borderTop: "1px solid var(--pv-border)" }}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="font-medium" style={{ color: "var(--pv-silver)" }}>
                  Permanent deletion
                </p>
                <p className="mt-1" style={{ color: "var(--pv-text-dim)" }}>
                  Eligibility requires the retention period and a fresh checksum check. A review
                  request still does not delete the file.
                </p>
              </div>
              {!showDeletionRequest && !deletionConfirmed && (
                <div className="flex flex-wrap gap-2">
                  {hasDeletionReview && (
                    <button
                      type="button"
                      className="rounded-md px-3 py-2"
                      style={{ color: "#fca5a5", border: "1px solid rgba(248,113,113,0.35)" }}
                      disabled={checkingDeletion || cancellingDeletion}
                      onClick={() => void checkPermanentDeletion()}
                    >
                      {checkingDeletion ? "Rechecking…" : "Review confirmation"}
                    </button>
                  )}
                  <button
                    type="button"
                    className="rounded-md px-3 py-2"
                    style={{ color: "#fca5a5", border: "1px solid rgba(248,113,113,0.35)" }}
                    disabled={checkingDeletion || cancellingDeletion}
                    onClick={() => {
                      if (hasDeletionReview) {
                        void cancelPermanentDeletionReview();
                        return;
                      }
                      void checkPermanentDeletion();
                    }}
                  >
                    {hasDeletionReview
                      ? cancellingDeletion
                        ? "Withdrawing…"
                        : "Withdraw deletion review"
                      : checkingDeletion
                        ? "Checking…"
                        : "Check deletion eligibility"}
                  </button>
                </div>
              )}
            </div>
            {hasDeletionReview && (
              <p style={{ color: "var(--pv-gold)" }}>
                Permanent-deletion review is pending. No content has been deleted.
              </p>
            )}
            {deletionConfirmed && (
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p style={{ color: "#fca5a5" }}>
                  Permanent deletion is confirmed. The file still exists until the final execution.
                </p>
                {!showDeletionExecution && (
                  <button
                    type="button"
                    className="rounded-md px-3 py-2"
                    style={{ color: "#fca5a5", border: "1px solid rgba(248,113,113,0.35)" }}
                    onClick={() => {
                      setExecutionFilename("");
                      setDeletionError(null);
                      setShowDeletionExecution(true);
                    }}
                  >
                    Review final execution
                  </button>
                )}
              </div>
            )}
            {deletionError && <p style={{ color: "#fca5a5" }}>{deletionError}</p>}
            {deletionConfirmed && showDeletionExecution && (
              <form
                className="space-y-3 rounded-md p-3"
                style={{
                  border: "1px solid rgba(248,113,113,0.35)",
                  background: "var(--pv-surface)",
                }}
                onSubmit={executePermanentDeletion}
              >
                <p className="font-medium" style={{ color: "#fca5a5" }}>
                  Final irreversible action
                </p>
                <p style={{ color: "var(--pv-text-dim)" }}>
                  Vault Master will revalidate the live path and checksum, remove the quarantined
                  content and canonical sidecar, and retain an audit tombstone. This cannot be
                  undone.
                </p>
                <p
                  className="break-all font-mono text-[10px]"
                  style={{ color: "var(--pv-silver)" }}
                >
                  {asset.vault_path}
                </p>
                <label className="block space-y-1">
                  <span style={{ color: "var(--pv-silver)" }}>
                    Type the exact filename to continue: {asset.filename}
                  </span>
                  <input
                    className="w-full rounded-md px-3 py-2"
                    autoComplete="off"
                    value={executionFilename}
                    onChange={(event) => setExecutionFilename(event.target.value)}
                    style={{ background: "var(--pv-bg)", border: "1px solid var(--pv-border)" }}
                  />
                </label>
                <div className="flex flex-wrap justify-end gap-2">
                  <button
                    type="button"
                    className="rounded-md px-3 py-2"
                    style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
                    disabled={executingDeletion}
                    onClick={() => {
                      setShowDeletionExecution(false);
                      setExecutionFilename("");
                      setDeletionError(null);
                    }}
                  >
                    Keep quarantined file
                  </button>
                  <button
                    type="submit"
                    className="rounded-md px-3 py-2"
                    style={{ color: "#fca5a5", border: "1px solid rgba(248,113,113,0.35)" }}
                    disabled={executingDeletion || executionFilename !== asset.filename}
                  >
                    {executingDeletion ? "Deleting…" : "Permanently delete file"}
                  </button>
                </div>
              </form>
            )}
            {deletionPreflight && !showDeletionRequest && (
              <div
                className="space-y-2 rounded-md p-3"
                style={{ border: "1px solid var(--pv-border)", background: "var(--pv-surface)" }}
              >
                <p className="font-medium" style={{ color: "var(--pv-silver)" }}>
                  {deletionPreflight.ready ? "Eligible for review" : "Not eligible for deletion"}
                </p>
                {deletionPreflight.eligible_at && (
                  <p style={{ color: "var(--pv-text-dim)" }}>
                    Eligible from{" "}
                    {new Intl.DateTimeFormat("en-GB", {
                      dateStyle: "medium",
                      timeStyle: "short",
                    }).format(new Date(deletionPreflight.eligible_at))}
                  </p>
                )}
                <p style={{ color: "var(--pv-text-dim)" }}>
                  Checksum {deletionPreflight.checksum_verified ? "verified" : "not verified"}.
                </p>
                {deletionPreflight.reason && (
                  <p style={{ color: "#fca5a5" }}>{deletionPreflight.reason}</p>
                )}
                {deletionPreflight.ready && (
                  <div className="flex justify-end">
                    <button
                      type="button"
                      className="rounded-md px-3 py-2"
                      style={{ color: "#fca5a5", border: "1px solid rgba(248,113,113,0.35)" }}
                      disabled={confirmingDeletion}
                      onClick={() => {
                        if (hasDeletionReview) {
                          void confirmPermanentDeletionReview();
                          return;
                        }
                        setShowDeletionRequest(true);
                      }}
                    >
                      {hasDeletionReview
                        ? confirmingDeletion
                          ? "Confirming…"
                          : "Confirm permanent deletion"
                        : "Continue to review request"}
                    </button>
                  </div>
                )}
              </div>
            )}
            {showDeletionRequest && (
              <form className="space-y-3" onSubmit={requestPermanentDeletion}>
                <label className="block space-y-1">
                  <span style={{ color: "var(--pv-silver)" }}>Reason (required)</span>
                  <textarea
                    className="min-h-20 w-full rounded-md px-3 py-2"
                    maxLength={500}
                    required
                    value={deletionReason}
                    onChange={(event) => setDeletionReason(event.target.value)}
                    style={{
                      background: "var(--pv-surface)",
                      border: "1px solid var(--pv-border)",
                    }}
                  />
                </label>
                <p style={{ color: "var(--pv-text-dim)" }}>
                  This records a review request only. Confirmation and execution remain separate.
                </p>
                <div className="flex flex-wrap justify-end gap-2">
                  <button
                    type="button"
                    className="rounded-md px-3 py-2"
                    style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
                    disabled={requestingDeletion}
                    onClick={() => {
                      setShowDeletionRequest(false);
                      setDeletionReason("");
                      setDeletionError(null);
                    }}
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="rounded-md px-3 py-2"
                    style={{ color: "#fca5a5", border: "1px solid rgba(248,113,113,0.35)" }}
                    disabled={requestingDeletion || !deletionReason.trim()}
                  >
                    {requestingDeletion ? "Requesting…" : "Request deletion review"}
                  </button>
                </div>
              </form>
            )}
          </div>
        )}
      </section>
      <VaultAssetAuditTrail entries={history} failed={historyError} />
    </div>
  );
}

function VaultAssetAuditTrail({
  entries,
  failed,
}: {
  entries: VaultAssetHistoryEntry[] | null;
  failed: boolean;
}) {
  const labels = {
    display_title: "Title",
    captured_on: "Date",
    location: "Location",
    reason: "Reason",
    state: "Review state",
    eligible_at: "Eligible from",
    checksum: "Verified checksum",
  } as const;

  return (
    <div className="mt-4 space-y-3 pt-4" style={{ borderTop: "1px solid var(--pv-border)" }}>
      <p className="font-medium" style={{ color: "var(--pv-silver)" }}>
        Vault history
      </p>
      {failed ? (
        <p style={{ color: "#fca5a5" }}>Vault history could not be loaded.</p>
      ) : entries === null ? (
        <p style={{ color: "var(--pv-text-dim)" }}>Loading Vault history...</p>
      ) : entries.length === 0 ? (
        <p style={{ color: "var(--pv-text-dim)" }}>No permanent asset actions recorded.</p>
      ) : (
        entries.map((entry) => (
          <div
            key={entry.id}
            className="rounded-md px-3 py-3"
            style={{ border: "1px solid var(--pv-border)" }}
          >
            <p style={{ color: "var(--pv-text-dim)" }}>
              {new Intl.DateTimeFormat("en-GB", {
                dateStyle: "medium",
                timeStyle: "short",
              }).format(new Date(entry.created_at))}
              {" · "}
              {entry.username}
            </p>
            {entry.action === "quarantine_review_requested" && (
              <p className="mt-2 font-medium" style={{ color: "var(--pv-silver)" }}>
                Quarantine review requested
              </p>
            )}
            {entry.action === "quarantine_review_cancelled" && (
              <p className="mt-2 font-medium" style={{ color: "var(--pv-silver)" }}>
                Quarantine review withdrawn
              </p>
            )}
            {entry.action === "quarantined" && (
              <p className="mt-2 font-medium" style={{ color: "var(--pv-silver)" }}>
                Moved to recoverable Quarantine
              </p>
            )}
            {entry.action === "permanent_deletion_review_requested" && (
              <p className="mt-2 font-medium" style={{ color: "#fca5a5" }}>
                Permanent-deletion review requested
              </p>
            )}
            {entry.action === "permanent_deletion_review_cancelled" && (
              <p className="mt-2 font-medium" style={{ color: "var(--pv-silver)" }}>
                Permanent-deletion review withdrawn
              </p>
            )}
            {entry.action === "permanent_deletion_confirmed" && (
              <p className="mt-2 font-medium" style={{ color: "#fca5a5" }}>
                Permanent deletion confirmed
              </p>
            )}
            {entry.action === "permanently_deleted" && (
              <p className="mt-2 font-medium" style={{ color: "#fca5a5" }}>
                Permanently deleted with audit tombstone retained
              </p>
            )}
            <div className="mt-2 space-y-1">
              {Object.keys(entry.current_values).map((field) => {
                const metadataField = field as keyof typeof labels;
                const label = labels[metadataField] ?? field.replaceAll("_", " ");
                const before = entry.previous_values[field] || "Not recorded";
                const after = entry.current_values[field] || "Not recorded";
                return (
                  <p key={field} className="flex flex-wrap gap-1">
                    <span style={{ color: "var(--pv-silver)" }}>{label}:</span>
                    <span style={{ color: "var(--pv-text-dim)" }}>{before}</span>
                    <span aria-hidden="true" style={{ color: "var(--pv-gold)" }}>
                      →
                    </span>
                    <span style={{ color: "var(--pv-silver)" }}>{after}</span>
                  </p>
                );
              })}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

function VaultAssetPreview({ asset }: { asset: VaultAsset }) {
  const previewUrl = `/api/vault-master/assets/${asset.id}/preview`;

  if (asset.mime_type.startsWith("image/")) {
    return (
      <div
        className="mb-4 overflow-hidden rounded-md"
        style={{ border: "1px solid var(--pv-border)" }}
      >
        <img
          src={previewUrl}
          alt={`Preview of ${asset.display_title}`}
          className="max-h-[28rem] w-full object-contain"
          style={{ background: "#050506" }}
        />
      </div>
    );
  }

  if (asset.mime_type.startsWith("video/")) {
    return (
      <div
        className="mb-4 overflow-hidden rounded-md"
        style={{ border: "1px solid var(--pv-border)" }}
      >
        <video
          src={previewUrl}
          controls
          preload="metadata"
          className="max-h-[28rem] w-full"
          aria-label={`Preview of ${asset.display_title}`}
          style={{ background: "#050506" }}
        />
      </div>
    );
  }

  if (asset.mime_type === "application/pdf") {
    return (
      <div className="mb-4 space-y-2">
        <iframe
          src={previewUrl}
          title={`Preview of ${asset.display_title}`}
          className="h-96 w-full rounded-md"
          style={{ background: "#fff", border: "1px solid var(--pv-border)" }}
        />
        <a
          href={previewUrl}
          target="_blank"
          rel="noreferrer"
          className="inline-block text-xs"
          style={{ color: "var(--pv-gold)" }}
        >
          Open PDF preview in a new tab
        </a>
      </div>
    );
  }

  return (
    <p
      className="mb-4 rounded-md px-3 py-3"
      style={{ color: "var(--pv-text-dim)", border: "1px solid var(--pv-border)" }}
    >
      Preview is not available for this file type.
    </p>
  );
}

function PermanentMetadataField({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: "text" | "date" | "number";
  placeholder?: string;
}) {
  return (
    <label className="space-y-2">
      <span className="block" style={{ color: "var(--pv-silver)" }}>
        {label}
      </span>
      <input
        type={type}
        value={value}
        maxLength={type === "text" ? 240 : undefined}
        max={type === "date" ? new Date().toISOString().slice(0, 10) : undefined}
        min={type === "number" ? 1 : undefined}
        step={type === "number" ? 1 : undefined}
        placeholder={placeholder}
        className="w-full rounded-md bg-transparent px-3 py-2 outline-none"
        style={{ color: "var(--pv-silver)", border: "1px solid var(--pv-border)" }}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function MetadataField({
  label,
  value,
  detectedValue,
  onChange,
  onRestore,
  type = "text",
  placeholder,
}: {
  label: string;
  value: string;
  detectedValue: string;
  onChange: (value: string) => void;
  onRestore: () => void;
  type?: "text" | "date";
  placeholder?: string;
}) {
  return (
    <div className="space-y-2">
      <span className="flex items-center justify-between gap-3 text-xs">
        <span style={{ color: "var(--pv-silver)" }}>{label}</span>
        <button
          type="button"
          className="text-[11px]"
          style={{ color: "var(--pv-gold)" }}
          disabled={value === detectedValue}
          onClick={onRestore}
        >
          Restore detected
        </button>
      </span>
      <input
        aria-label={label}
        type={type}
        value={value}
        maxLength={type === "text" ? 240 : undefined}
        max={type === "date" ? new Date().toISOString().slice(0, 10) : undefined}
        placeholder={placeholder}
        className="w-full rounded-md bg-transparent px-3 py-2 text-sm outline-none"
        style={{
          color: "var(--pv-silver)",
          border: "1px solid var(--pv-border)",
        }}
        onChange={(event) => onChange(event.target.value)}
      />
      <span className="block text-[11px]" style={{ color: "var(--pv-text-dim)" }}>
        Detected: {detectedValue || "Not available"}
      </span>
    </div>
  );
}
