import { getFileTitle } from "@/lib/vault-libraries";

export function HomeVideoCardMetadata({
  name,
  display_title,
  location,
}: {
  name: string;
  display_title: string | null;
  location: string | null;
}) {
  return (
    <span className="block p-4">
      <span className="block text-sm font-medium truncate" style={{ color: "var(--pv-silver)" }}>
        {display_title?.trim() || getFileTitle(name)}
      </span>
      {location?.trim() && (
        <span className="block text-xs mt-1 truncate" style={{ color: "var(--pv-text-dim)" }}>
          {location}
        </span>
      )}
    </span>
  );
}
