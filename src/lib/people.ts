export type PersonAsset = {
  asset_id: string;
  display_title: string;
  asset_type: string;
  vault_path: string | null;
};

export type Person = {
  person_id: string;
  full_name: string;
  preferred_name: string | null;
  aliases: string[];
  date_of_birth: string | null;
  profile_asset_id: string | null;
  profile_frame: { scale: number; x: number; y: number } | null;
  is_me: boolean;
  relationship_label: string | null;
  active: boolean;
};

export type PersonDetail = Person & {
  associated_asset_count: number;
  associated_assets: PersonAsset[];
};

export type GalleryProfileAsset = {
  id: string;
  asset_id: string;
  name: string;
  display_title: string | null;
  thumbnail_url: string;
  media_type: string;
};

export type PersonDraft = {
  full_name: string;
  preferred_name: string;
  aliases: string;
  date_of_birth: string;
  profile_asset_id: string | null;
  relationship_label: string;
  profile_frame: { scale: number; x: number; y: number };
};

export type ProfileFrame = PersonDraft["profile_frame"];

export const defaultProfileFrame = (): ProfileFrame => ({ scale: 1, x: 50, y: 50 });

export function normaliseProfileFrame(frame?: Partial<ProfileFrame> | null): ProfileFrame {
  const numberAt = (value: unknown, fallback: number) =>
    Number.isFinite(Number(value)) ? Number(value) : fallback;
  const scale = Math.min(3, Math.max(1, numberAt(frame?.scale, 1)));
  return {
    scale,
    x: scale === 1 ? 50 : Math.min(100, Math.max(0, numberAt(frame?.x, 50))),
    y: scale === 1 ? 50 : Math.min(100, Math.max(0, numberAt(frame?.y, 50))),
  };
}

// The image grows inside an overflow-hidden square frame. x/y represent the
// visible source position from 0% (left/top) to 100% (right/bottom), so the
// frame can never expose blank space at any supported zoom level.
export function profileImageCropStyle(frame?: Partial<ProfileFrame> | null) {
  const { scale, x, y } = normaliseProfileFrame(frame);
  const overflow = scale - 1;
  return {
    width: `${scale * 100}%`,
    height: `${scale * 100}%`,
    left: `${-overflow * x}%`,
    top: `${-overflow * y}%`,
  };
}

export const emptyPersonDraft = (): PersonDraft => ({
  full_name: "",
  preferred_name: "",
  aliases: "",
  date_of_birth: "",
  profile_asset_id: null,
  relationship_label: "",
  profile_frame: defaultProfileFrame(),
});

export const personDraftFrom = (person: Person): PersonDraft => ({
  full_name: person.full_name,
  preferred_name: person.preferred_name ?? "",
  aliases: person.aliases.join(", "),
  date_of_birth: person.date_of_birth ?? "",
  profile_asset_id: person.profile_asset_id,
  relationship_label: person.relationship_label ?? "",
  profile_frame: normaliseProfileFrame(person.profile_frame),
});

export const personPayload = (draft: PersonDraft) => ({
  full_name: draft.full_name.trim(),
  preferred_name: draft.preferred_name.trim() || null,
  aliases: draft.aliases
    .split(",")
    .map((alias) => alias.trim())
    .filter(Boolean),
  date_of_birth: draft.date_of_birth || null,
  profile_asset_id: draft.profile_asset_id,
  relationship_label: draft.relationship_label.trim() || null,
  profile_frame: draft.profile_asset_id ? draft.profile_frame : null,
});

export async function peopleRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/people${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    const error = new Error(`People request failed: ${response.status}`) as Error & {
      status?: number;
    };
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export async function galleryProfileAssets(): Promise<GalleryProfileAsset[]> {
  const response = await fetch("/api/gallery", { credentials: "include" });
  if (!response.ok) {
    throw new Error("Gallery request failed");
  }
  const records = (await response.json()) as GalleryProfileAsset[];
  return records.filter((asset) => asset.asset_id && asset.media_type.startsWith("image/"));
}
