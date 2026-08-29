export type VaultFileKind =
  | "video"
  | "image"
  | "audio"
  | "pdf"
  | "document"
  | "archive"
  | "software"
  | "other";

export type VaultLibraryFile = {
  id: string;
  name: string;
  directory: string | null;
  size: number;
  modified_at: string;
  kind: VaultFileKind;
  opens_inline: boolean;
  open_url: string;
  display_title: string | null;
  captured_on: string | null;
  location: string | null;
  metadata_provenance: Record<string, string>;
};

export function getFileTitle(filename: string): string {
  const lastDot = filename.lastIndexOf(".");
  const stem = lastDot > 0 ? filename.slice(0, lastDot) : filename;
  return stem.replaceAll("_", " ");
}
