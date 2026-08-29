import { ImagePlus, UserRound, X } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  defaultProfileFrame,
  emptyPersonDraft,
  galleryProfileAssets,
  normaliseProfileFrame,
  profileImageCropStyle,
  type GalleryProfileAsset,
  type PersonDraft,
} from "@/lib/people";

type Props = {
  initial?: PersonDraft;
  submitLabel: string;
  busy?: boolean;
  onSubmit: (draft: PersonDraft) => Promise<void>;
  onCancel?: () => void;
};

export function PersonForm({ initial, submitLabel, busy = false, onSubmit, onCancel }: Props) {
  const [draft, setDraft] = useState<PersonDraft>(initial ?? emptyPersonDraft());
  const [assets, setAssets] = useState<GalleryProfileAsset[]>([]);
  const [assetsError, setAssetsError] = useState(false);
  const cropFrameRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    frame: PersonDraft["profile_frame"];
  } | null>(null);

  useEffect(() => {
    void galleryProfileAssets()
      .then(setAssets)
      .catch(() => setAssetsError(true));
  }, []);

  const selectedAsset = assets.find((asset) => asset.asset_id === draft.profile_asset_id);
  const set = (field: keyof PersonDraft, value: string | null) =>
    setDraft((current) => ({ ...current, [field]: value }));
  const setFrame = (field: "scale" | "x" | "y", value: number) =>
    setDraft((current) => ({
      ...current,
      profile_frame: normaliseProfileFrame({ ...current.profile_frame, [field]: value }),
    }));
  const setProfileAsset = (assetId: string | null) =>
    setDraft((current) => ({
      ...current,
      profile_asset_id: assetId,
      profile_frame:
        assetId === current.profile_asset_id ? current.profile_frame : defaultProfileFrame(),
    }));
  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (draft.profile_frame.scale === 1) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      frame: draft.profile_frame,
    };
  };
  const drag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const active = dragRef.current;
    const bounds = cropFrameRef.current?.getBoundingClientRect();
    if (!active || active.pointerId !== event.pointerId || !bounds) return;
    const movableWidth = (active.frame.scale - 1) * bounds.width;
    const movableHeight = (active.frame.scale - 1) * bounds.height;
    setDraft((current) => ({
      ...current,
      profile_frame: normaliseProfileFrame({
        scale: active.frame.scale,
        x: active.frame.x - ((event.clientX - active.startX) / movableWidth) * 100,
        y: active.frame.y - ((event.clientY - active.startY) / movableHeight) * 100,
      }),
    }));
  };
  const stopDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    await onSubmit(draft);
  };

  return (
    <form className="space-y-5" onSubmit={(event) => void submit(event)}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Full name" required>
          <input
            className="pv-input w-full"
            value={draft.full_name}
            required
            onChange={(event) => set("full_name", event.target.value)}
          />
        </Field>
        <Field label="Preferred name">
          <input
            className="pv-input w-full"
            value={draft.preferred_name}
            onChange={(event) => set("preferred_name", event.target.value)}
          />
        </Field>
        <Field label="Aliases" hint="Separate names with commas">
          <input
            className="pv-input w-full"
            value={draft.aliases}
            onChange={(event) => set("aliases", event.target.value)}
          />
        </Field>
        <Field label="Date of birth">
          <input
            className="pv-input w-full"
            type="date"
            value={draft.date_of_birth}
            onChange={(event) => set("date_of_birth", event.target.value)}
          />
        </Field>
      </div>

      <Field label="Relationship to you" hint="For example: friend, sister, colleague">
        <input
          className="pv-input w-full"
          value={draft.relationship_label}
          onChange={(event) => set("relationship_label", event.target.value)}
        />
      </Field>

      <fieldset className="space-y-3">
        <legend className="text-sm" style={{ color: "var(--pv-silver)" }}>
          Profile picture
        </legend>
        <p className="text-xs" style={{ color: "var(--pv-text-dim)" }}>
          Choose one of your existing Gallery images. Nothing is copied.
        </p>
        {selectedAsset ? (
          <div
            className="flex items-center gap-3 rounded-lg border p-2"
            style={{ borderColor: "var(--pv-border)" }}
          >
            <div className="relative h-14 w-14 shrink-0 overflow-hidden rounded-md">
              <img
                src={selectedAsset.thumbnail_url}
                alt="Selected profile"
                className="absolute max-w-none object-cover"
                style={profileImageCropStyle(draft.profile_frame)}
              />
            </div>
            <span className="min-w-0 flex-1 truncate text-xs" style={{ color: "var(--pv-silver)" }}>
              {selectedAsset.display_title ?? selectedAsset.name}
            </span>
            <button
              type="button"
              className="pv-btn-ghost !p-2"
              onClick={() => setProfileAsset(null)}
              aria-label="Remove profile picture"
            >
              <X size={16} />
            </button>
          </div>
        ) : (
          <div
            className="flex h-16 items-center gap-3 rounded-lg border px-3 text-xs"
            style={{ borderColor: "var(--pv-border)", color: "var(--pv-text-dim)" }}
          >
            <UserRound size={20} /> No profile picture selected
          </div>
        )}
        {selectedAsset && (
          <div
            className="space-y-3 rounded-lg border p-3"
            style={{ borderColor: "var(--pv-border)" }}
          >
            <div className="flex flex-wrap items-center gap-4">
              <div
                ref={cropFrameRef}
                className="relative aspect-square w-40 shrink-0 touch-none overflow-hidden rounded-md border cursor-grab active:cursor-grabbing"
                style={{ borderColor: "var(--pv-gold)", background: "var(--pv-panel)" }}
                onPointerDown={startDrag}
                onPointerMove={drag}
                onPointerUp={stopDrag}
                onPointerCancel={stopDrag}
                aria-label="Profile picture crop frame. Drag the image to position it."
              >
                <img
                  src={selectedAsset.thumbnail_url}
                  alt="Profile picture crop preview"
                  className="absolute max-w-none select-none object-cover"
                  style={profileImageCropStyle(draft.profile_frame)}
                  draggable={false}
                />
              </div>
              <p className="max-w-sm text-xs" style={{ color: "var(--pv-text-dim)" }}>
                Drag the image to position it inside this fixed profile frame. Zoom changes the
                image behind the frame; the original Gallery image is never changed.
              </p>
            </div>
            <div className="flex items-end gap-2">
              <div className="min-w-0 flex-1">
                <FrameControl
                  label="Zoom"
                  value={draft.profile_frame.scale}
                  min={1}
                  max={3}
                  step={0.05}
                  onChange={(value) =>
                    setDraft((current) => ({
                      ...current,
                      profile_frame: normaliseProfileFrame({
                        ...current.profile_frame,
                        scale: value,
                      }),
                    }))
                  }
                />
              </div>
              <button
                type="button"
                className="pv-btn-ghost px-2 py-1 text-xs"
                onClick={() =>
                  setFrame(
                    "scale",
                    Math.max(1, Number((draft.profile_frame.scale - 0.1).toFixed(2))),
                  )
                }
              >
                −
              </button>
              <button
                type="button"
                className="pv-btn-ghost px-2 py-1 text-xs"
                onClick={() =>
                  setFrame(
                    "scale",
                    Math.min(3, Number((draft.profile_frame.scale + 0.1).toFixed(2))),
                  )
                }
              >
                +
              </button>
            </div>
            <FrameControl
              label="Horizontal position"
              value={draft.profile_frame.x}
              min={0}
              max={100}
              step={1}
              onChange={(value) => setFrame("x", value)}
            />
            <FrameControl
              label="Vertical position"
              value={draft.profile_frame.y}
              min={0}
              max={100}
              step={1}
              onChange={(value) => setFrame("y", value)}
            />
            <button
              type="button"
              className="pv-btn-ghost px-3 py-1.5 text-xs"
              onClick={() =>
                setDraft((current) => ({ ...current, profile_frame: defaultProfileFrame() }))
              }
            >
              Reset framing
            </button>
          </div>
        )}
        {assetsError ? (
          <p className="pv-status-error text-xs">Gallery images are unavailable right now.</p>
        ) : (
          <div className="grid grid-cols-4 gap-2 sm:grid-cols-6">
            {assets.map((asset) => (
              <button
                key={asset.asset_id}
                type="button"
                className="aspect-square overflow-hidden rounded-md border focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--pv-gold)]"
                style={{
                  borderColor:
                    asset.asset_id === draft.profile_asset_id
                      ? "var(--pv-gold)"
                      : "var(--pv-border)",
                }}
                onClick={() => setProfileAsset(asset.asset_id)}
                aria-label={`Use ${asset.display_title ?? asset.name} as profile picture`}
              >
                <img src={asset.thumbnail_url} alt="" className="h-full w-full object-cover" />
              </button>
            ))}
            {!assets.length && (
              <span
                className="col-span-full flex items-center gap-2 text-xs"
                style={{ color: "var(--pv-text-dim)" }}
              >
                <ImagePlus size={16} /> No Gallery images available.
              </span>
            )}
          </div>
        )}
      </fieldset>

      <div
        className="flex flex-wrap justify-end gap-3 border-t pt-5"
        style={{ borderColor: "var(--pv-border)" }}
      >
        {onCancel && (
          <button type="button" className="pv-btn-ghost px-4 py-2 text-sm" onClick={onCancel}>
            Cancel
          </button>
        )}
        <button className="pv-btn-primary px-4 py-2 text-sm" disabled={busy}>
          {busy ? "Saving…" : submitLabel}
        </button>
      </div>
    </form>
  );
}

function FrameControl({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="block text-xs" style={{ color: "var(--pv-silver)" }}>
      <span className="flex justify-between gap-3">
        <span>{label}</span>
        <span>{label === "Zoom" ? `${value.toFixed(2)}×` : `${Math.round(value)}%`}</span>
      </span>
      <input
        className="mt-1.5 w-full accent-[var(--pv-gold)]"
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function Field({
  label,
  hint,
  required,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1.5 text-sm" style={{ color: "var(--pv-silver)" }}>
      <span>
        {label}
        {required ? " *" : ""}
      </span>
      {children}
      {hint && (
        <span className="block text-xs" style={{ color: "var(--pv-text-dim)" }}>
          {hint}
        </span>
      )}
    </label>
  );
}
