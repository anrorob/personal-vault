export type GalleryImage = {
  id: string;
  asset_id: string;
  name: string;
  size: number;
  added_at: string;
  captured_on: string | null;
  captured_at?: string | null;
  date_source: "embedded" | "filename" | "file_modified" | "user_override" | "unavailable";
  location: string | null;
  display_title: string | null;
  description?: string | null;
  thumbnail_url: string;
  media_type: string;
  content_type: string;
  photo_display: boolean;
  warning: string | null;
  owner_display_name?: string | null;
};

export type GallerySortOrder = "newest" | "oldest";

export type GalleryIntelligenceTerm = {
  namespace: "photo_type" | "content_tag";
  slug: string;
  display_name: string;
};

export type GalleryIntelligenceOwnerTerm = GalleryIntelligenceTerm & { source: string };
export type GalleryPerson = { id: string; display_name: string; active: boolean; source?: string };
export type GalleryFaceDetection = {
  id: string;
  bounding_box: { x: number; y: number; w: number; h: number };
  person_id?: string;
  person_name?: string;
  user_confirmed?: boolean;
};

export type GalleryLocalAnnotation = {
  note: string | null;
  tags: string[];
  people: GalleryPerson[];
};

export const DEFAULT_GALLERY_SORT: GallerySortOrder = "newest";

export function parseGallerySortOrder(value: unknown): GallerySortOrder {
  return value === "oldest" ? "oldest" : DEFAULT_GALLERY_SORT;
}

export function parseGalleryFilter(value: unknown): string[] {
  const values = Array.isArray(value) ? value : typeof value === "string" ? [value] : [];
  return [...new Set(values.filter((item) => typeof item === "string" && item.length > 0))];
}

export type GalleryImageDetails = GalleryImage & {
  can_edit: boolean;
  asset_id?: string;
  lifecycle_state?: "active" | "hidden";
  vault_path?: string;
  mime_type?: string;
  sha256?: string;
  metadata_provenance?: Record<string, string>;
  image_url: string;
  previous_id: string | null;
  next_id: string | null;
  intelligence: GalleryIntelligenceTerm[];
  intelligence_provenance?: GalleryIntelligenceOwnerTerm[];
  people?: GalleryPerson[];
  origin_people?: GalleryPerson[];
  local_annotation?: GalleryLocalAnnotation;
  unknown_people_count?: number;
  unresolved_person_presence?: boolean;
  face_detections?: GalleryFaceDetection[];
};

export function getPhotoTitle(filename: string): string {
  const lastDot = filename.lastIndexOf(".");
  const stem = lastDot > 0 ? filename.slice(0, lastDot) : filename;
  return stem.replaceAll("_", " ");
}

export function formatPhotoDate(capturedOn: string | null): string {
  if (!capturedOn) {
    return "Date not recorded";
  }
  const [year, month, day] = capturedOn.slice(0, 10).split("-").map(Number);
  const date = new Date(year, month - 1, day);

  if (Number.isNaN(date.getTime())) {
    return capturedOn;
  }

  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(date);
}
