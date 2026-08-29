export type ArrivalHallFile = {
  name: string;
  relative_path: string;
  folder: string | null;
  size: number;
  uploaded_at: string;
};

export type ArrivalHallListing = {
  files: ArrivalHallFile[];
  max_upload_bytes: number;
};

export type VaultMasterItem = {
  id: string;
  batch_id: string;
  source_kind: "incoming" | "inventory";
  source_path: string;
  relative_path: string;
  filename: string;
  size_bytes: number;
  mime_type: string;
  modified_at: string;
  sha256: string;
  state:
    | "inventoried"
    | "needs_review"
    | "approved"
    | "rejected"
    | "moved"
    | "move_failed"
    | "move_queued"
    | "moving"
    | "duplicate_kept"
    | "duplicate_removed"
    | "duplicate_remove_failed"
    | "arrival_removed";
  duplicate_of_id: string | null;
  proposed_category: string | null;
  proposed_destination: string | null;
  proposal_reason: string | null;
  proposal_confidence: "low" | "medium" | "high" | null;
  metadata: Record<string, unknown>;
  publication_audience: "vault-wide" | "private" | null;
  metadata_overrides: Partial<
    Record<
      | "display_title"
      | "captured_on"
      | "location"
      | "artist"
      | "album"
      | "album_artist"
      | "track_number"
      | "disc_number"
      | "release_year",
      string | number
    >
  >;
};

export type VaultMasterListing = {
  items: VaultMasterItem[];
};

export type PublicationBundle = {
  key: string;
  author: string;
  title: string;
  source_item_ids: string[];
  front_cover_item_ids: string[];
  back_cover_item_ids: string[];
  issues: string[];
  review_status: "review_required";
};

export type PublicationBundleListing = {
  bundles: PublicationBundle[];
  publication_rule: "owner_review_required";
};

export type PublicationReviewBlock = {
  id: string;
  block_type: string;
  locator: string;
  content_text: string | null;
  source_page: number | null;
  illustration_file_id: string | null;
};

export type PublicationReviewIssue = {
  id: string;
  issue_type: string;
  severity: "advisory" | "warning" | "critical";
  state: "open" | "accepted" | "resolved" | "rejected";
  detail: string;
  source_page: number | null;
};

export type PublicationReview = {
  source_item_id: string;
  state: "needs_review" | "deferred" | "rejected" | "ready_to_publish" | "published";
  revision: number;
  snapshot: {
    publication: {
      publication_type: string;
      reading_mode: "reflowable" | "fixed_layout" | "hybrid";
      language: string | null;
      detected: Record<string, unknown>;
      user_overrides: Record<string, unknown>;
      effective: Record<string, unknown>;
    };
    blocks: PublicationReviewBlock[];
    issues: PublicationReviewIssue[];
  };
};

export type IngestionAiJob = {
  id: string;
  item_id: string;
  status: "queued" | "processing" | "completed" | "failed";
  error: string | null;
};

export type IngestionAiEvidence = {
  id: string;
  item_id: string;
  content_type:
    | "personal_photo"
    | "receipt"
    | "financial_document"
    | "general_document"
    | "screenshot"
    | "artwork"
    | "publication_cover"
    | "unknown";
  caption: string;
  ocr_text: string;
  confidence: number;
  reasons: string[];
  model_id: string;
  model_revision: string;
  task_version: string;
  processing_ms: number;
  recommended_destination: string | null;
  decision_score: number;
  routing_band: "automatic_eligible" | "batch_review" | "individual_review";
  confidence_components: Record<string, number>;
  conflicts: string[];
  automatic_disqualifiers: string[];
  decision_model_version: string;
  created_at: string;
};

export type IngestionAiItemEvidence = {
  item_id: string;
  jobs: IngestionAiJob[];
  evidence: IngestionAiEvidence[];
};

export type IngestionAiEvidenceListing = {
  items: IngestionAiItemEvidence[];
  publication_rule: "private_review_evidence_only";
};

export type IngestionAnalysisGroup = {
  destination: string | null;
  content_type: IngestionAiEvidence["content_type"];
  routing_band: IngestionAiEvidence["routing_band"];
  explanation: string;
  item_ids: string[];
  count: number;
};

export type IngestionAnalysisBatch = {
  id: string;
  status: "running" | "paused" | "completed" | "completed_with_failures";
  total_items: number;
  queued_items: number;
  processing_items: number;
  completed_items: number;
  failed_items: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  groups: IngestionAnalysisGroup[];
};

export type IngestionAnalysisBatchListing = {
  batches: IngestionAnalysisBatch[];
};

export type RoutingMemoryRule = {
  id: string;
  feature_signature: string;
  features: Record<string, string>;
  destination: string;
  example_count: number;
  contradiction_count: number;
  confidence: number;
  maturity: "evidence" | "suggestion" | "review" | "established";
  status: "enabled" | "disabled";
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
  affected_item_ids: string[];
};

export type RoutingMemoryListing = { rules: RoutingMemoryRule[] };

export type AutopilotPolicy = {
  id: string;
  source: "arrival_hall";
  content_type:
    | "personal_photo"
    | "receipt"
    | "financial_document"
    | "general_document"
    | "artwork";
  destination: "Gallery" | "Documents" | "Ledger" | "Archives";
  threshold: number;
  max_items: number;
  max_failures: number;
  max_failure_percent: number;
  status: "enabled" | "paused" | "disabled";
  policy_version: string;
  created_at: string;
  updated_at: string;
};

export type AutopilotRun = {
  id: string;
  policy_id: string;
  status: "running" | "queued" | "completed" | "stopped";
  item_ids: string[];
  outcomes: Record<string, string>;
  stop_reason: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};

export type AutopilotListing = {
  policies: AutopilotPolicy[];
  runs: AutopilotRun[];
};

export type VaultMasterJob = {
  id: string;
  source_kind: "incoming" | "inventory";
  status: "queued" | "scanning" | "completed" | "failed";
  item_count: number;
  error: string | null;
  created_at: string | null;
  completed_at: string | null;
};

export type VaultMasterJobListing = {
  jobs: VaultMasterJob[];
};

export type VaultMasterActivityEntry = {
  id: string;
  batch_id: string | null;
  item_id: string | null;
  source_kind: "incoming" | "inventory" | null;
  filename: string | null;
  action: string;
  username: string | null;
  detail: string;
  succeeded: boolean;
  created_at: string;
};

export type VaultMasterActivityListing = {
  events: VaultMasterActivityEntry[];
};

export type VaultAsset = {
  id: string;
  asset_type: string;
  display_title: string;
  captured_on: string | null;
  location: string | null;
  vault_path: string;
  filename: string;
  size_bytes: number;
  mime_type: string;
  sha256: string;
  metadata: Record<string, unknown>;
  metadata_provenance: Record<string, string>;
};

export type VaultAssetSearchResult = {
  assets: VaultAsset[];
};

export type AssetRelationshipCandidate = {
  classification: "exact_duplicate" | "probable_duplicate" | "alternate_version" | "related_file";
  confidence: "certain" | "high" | "medium" | "low";
  evidence: string[];
  affected_files: Array<{
    asset_id: string;
    vault_path: string;
    filename: string;
    size_bytes: number;
    mime_type: string;
    sha256: string;
  }>;
};

export type AssetRelationshipCandidateListing = {
  candidates: AssetRelationshipCandidate[];
};

export type CanonicalAssetRelationship = {
  relationship_type: "duplicate" | "alternate_version" | "related_file";
  confidence: "certain" | "high" | "medium" | "low";
  evidence: string[];
  created_by: string;
  created_at: string;
  affected_files: AssetRelationshipCandidate["affected_files"];
};

export type CanonicalAssetRelationshipListing = {
  relationships: CanonicalAssetRelationship[];
};

export type VaultAssetHistoryEntry = {
  id: string;
  asset_id: string;
  action: string;
  username: string;
  previous_values: Record<string, string | null>;
  current_values: Record<string, string | null>;
  created_at: string;
};

export type VaultAssetHistory = {
  entries: VaultAssetHistoryEntry[];
};

export type QuarantinePreflight = {
  ready: boolean;
  source_path: string;
  proposed_quarantine_path: string | null;
  checksum_verified: boolean;
  reason: string | null;
};

export type PermanentDeletionPreflight = {
  ready: boolean;
  source_path: string;
  proposed_permanent_deletion_path: string | null;
  checksum_verified: boolean;
  quarantined_at: string | null;
  eligible_at: string | null;
  reason: string | null;
};

export type SidecarRecoveryCandidate = {
  sidecar_name: string;
  status:
    | "current"
    | "hidden"
    | "recoverable"
    | "intentionally_deleted"
    | "media_missing"
    | "restorable"
    | "conflict"
    | "path_conflict"
    | "invalid"
    | "unsupported";
  detail: string;
  asset_id: string | null;
  display_title: string | null;
  vault_path: string | null;
  filename: string | null;
};

export type SidecarRecoveryAssessment = {
  discovered: number;
  valid: number;
  invalid: number;
  unsupported: number;
  current: number;
  hidden: number;
  recoverable: number;
  intentionally_deleted: number;
  media_missing: number;
  restorable: number;
  conflicting: number;
  path_conflicts: number;
  candidates: SidecarRecoveryCandidate[];
};

export type UploadResult = {
  status: "uploaded";
  original_name: string;
  stored_name: string;
  size: number;
};

export class UploadError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export function formatBytes(bytes: number): string {
  if (bytes === 0) {
    return "0 B";
  }

  const units = ["B", "KB", "MB", "GB", "TB"];
  const unitIndex = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** unitIndex;

  return `${value.toFixed(unitIndex === 0 || value >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

export function encodeArrivalHallPath(relativePath: string): string {
  return relativePath.split("/").map(encodeURIComponent).join("/");
}

function getErrorMessage(xhr: XMLHttpRequest): string {
  try {
    const body = JSON.parse(xhr.responseText) as { detail?: unknown };
    if (typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // The generic message below intentionally hides unexpected server output.
  }

  return "The upload could not be completed.";
}

export function uploadFile(
  file: File,
  onProgress: (progress: number) => void,
): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/arrival-hall");
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-PV-Upload", "1");
    xhr.setRequestHeader("X-PV-Filename", encodeURIComponent(file.name));
    xhr.setRequestHeader("X-PV-Last-Modified", String(file.lastModified));

    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText) as UploadResult);
        return;
      }

      reject(new UploadError(getErrorMessage(xhr), xhr.status));
    });
    xhr.addEventListener("error", () => {
      reject(new UploadError("The connection was interrupted.", 0));
    });
    xhr.addEventListener("abort", () => {
      reject(new UploadError("The upload was cancelled.", 0));
    });

    xhr.send(file);
  });
}
