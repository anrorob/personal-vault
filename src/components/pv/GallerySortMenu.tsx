import { useState } from "react";
import { Check, ChevronDown, ChevronRight } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { GalleryPeriod, GallerySortOrder } from "@/lib/gallery";

export function GallerySortMenu({
  sort,
  onSort,
  periods,
  onJump,
}: {
  sort: GallerySortOrder;
  onSort: (sort: GallerySortOrder) => void;
  periods: GalleryPeriod[];
  onJump: (start: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [year, setYear] = useState<number>();
  const years = [...new Set(periods.map((period) => period.year))];
  const jump = (start: string) => {
    setOpen(false);
    onJump(start);
  };
  return (
    <div className="flex items-center gap-2 text-xs" style={{ color: "var(--pv-text-dim)" }}>
      <span>Sort</span>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            className="pv-input !w-auto inline-flex min-h-11 items-center gap-2 !py-2 text-sm"
            aria-label="Sort Gallery photos"
          >
            {sort === "oldest" ? "Oldest first" : "Newest first"}
            <ChevronDown size={14} />
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="end"
          collisionPadding={12}
          aria-label="Gallery sort and dates"
          className="pv-dialog w-64 max-w-[calc(100vw-env(safe-area-inset-left)-env(safe-area-inset-right)-2rem)] max-h-[min(70dvh,calc(var(--radix-popover-content-available-height)-env(safe-area-inset-bottom)-0.5rem))] overflow-y-auto overscroll-contain p-1"
          style={{
            background: "var(--pv-panel)",
            color: "var(--pv-silver)",
            borderColor: "var(--pv-border)",
          }}
        >
          {(["newest", "oldest"] as const).map((value) => (
            <button
              type="button"
              key={value}
              aria-pressed={sort === value}
              className="flex min-h-11 w-full items-center gap-2 rounded px-3 text-left text-sm hover:bg-white/10"
              onClick={() => {
                setOpen(false);
                onSort(value);
              }}
            >
              <Check size={14} className={sort === value ? "" : "invisible"} />
              {value === "newest" ? "Newest first" : "Oldest first"}
            </button>
          ))}
          {years.length > 0 && (
            <nav
              aria-label="Gallery dates"
              className="mt-1 border-t pt-1"
              style={{ borderColor: "var(--pv-border)" }}
            >
              {years.map((value) =>
                value === null ? (
                  <button
                    type="button"
                    key="undated"
                    className="min-h-11 w-full rounded px-3 text-left text-sm hover:bg-white/10"
                    onClick={() => jump(periods.find((period) => period.year === null)!.start)}
                  >
                    No date
                  </button>
                ) : (
                  <div key={value}>
                    <button
                      type="button"
                      aria-expanded={year === value}
                      aria-controls={`gallery-months-${value}`}
                      className="flex min-h-11 w-full items-center justify-between rounded px-3 text-sm hover:bg-white/10"
                      onClick={() => setYear(year === value ? undefined : value)}
                    >
                      {value}
                      <ChevronRight size={14} className={year === value ? "rotate-90" : ""} />
                    </button>
                    {year === value && (
                      <div
                        id={`gallery-months-${value}`}
                        aria-label={`Months in ${value}`}
                        className="pl-4"
                      >
                        {periods
                          .filter((period) => period.year === value)
                          .map((period) => (
                            <button
                              type="button"
                              key={period.month}
                              className="block min-h-11 w-full rounded px-3 text-left text-sm hover:bg-white/10"
                              onClick={() => jump(period.start)}
                            >
                              {new Intl.DateTimeFormat("en-GB", { month: "long" }).format(
                                new Date(2000, period.month! - 1, 1),
                              )}
                            </button>
                          ))}
                      </div>
                    )}
                  </div>
                ),
              )}
            </nav>
          )}
        </PopoverContent>
      </Popover>
    </div>
  );
}
