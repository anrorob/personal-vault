import { Ellipsis, X } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

/** One responsive control region, below the shell's 64px header. */
export function GalleryControls({
  actions,
  sort,
  selection,
  sectionName = "Gallery",
}: {
  sectionName?: string;
  actions: ReactNode;
  sort: ReactNode;
  selection?: ReactNode;
}) {
  const contextId = `${sectionName.toLowerCase().replaceAll(" ", "-")}-context-actions`;
  const [open, setOpen] = useState(false);
  const region = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const selecting = Boolean(selection);
  useEffect(() => {
    setOpen(false);
  }, [selecting]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!region.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus({ preventScroll: true });
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  return (
    <div
      ref={region}
      data-gallery-controls
      className="sticky top-16 z-10 border-b py-2"
      style={{ background: "var(--pv-bg, #0e0e11)", borderColor: "var(--pv-border)" }}
    >
      <div className="flex min-h-11 min-w-0 flex-wrap items-center justify-between gap-2">
        {selection || (
          <>
            <button
              ref={trigger}
              type="button"
              className="pv-gallery-context-toggle pv-btn-secondary inline-flex min-h-11 items-center gap-2 md:hidden"
              aria-label={`${sectionName} actions`}
              aria-expanded={open}
              aria-controls={contextId}
              onClick={() => setOpen(!open)}
            >
              {open ? <X size={18} /> : <Ellipsis size={18} />}{" "}
              <span className="sr-only sm:not-sr-only">{sectionName} actions</span>
            </button>
            <div
              id={contextId}
              data-context-open={open}
              aria-label={`${sectionName} actions`}
              className={`gallery-context-actions ${open ? "flex" : "hidden"} absolute inset-x-0 top-full max-h-[calc(100dvh-9rem)] flex-col gap-3 overflow-y-auto rounded-md border p-3 shadow-xl md:static md:flex md:max-h-none md:flex-1 md:flex-row md:flex-wrap md:items-center md:overflow-visible md:border-0 md:p-0 md:shadow-none`}
              style={{ background: "var(--pv-bg, #0e0e11)", borderColor: "var(--pv-border)" }}
            >
              {actions}
            </div>
          </>
        )}
        {!selection && (
          <div className="shrink-0" data-gallery-sort>
            {sort}
          </div>
        )}
      </div>
    </div>
  );
}
