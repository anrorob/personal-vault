import { useEffect, useId, useRef, useState } from "react";

export function PlayerMenu({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => {
    ref.current?.dispatchEvent(new Event("pvcontrolmenuchange", { bubbles: true }));
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("focusin", outside, true);
    window.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("focusin", outside, true);
      window.removeEventListener("keydown", escape, true);
    };
  }, [open]);
  return (
    <div
      ref={ref}
      className={`pv-player-menu min-w-0 ${label === "Subtitles" ? "flex-1" : "shrink-0"}`}
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.preventDefault();
          event.stopPropagation();
          setOpen(false);
          trigger.current?.focus();
        }
        if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          setOpen(true);
          requestAnimationFrame(() => {
            const items = [
              ...ref.current!.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]'),
            ];
            const index = items.indexOf(document.activeElement as HTMLButtonElement);
            const next =
              event.key === "Home"
                ? 0
                : event.key === "End"
                  ? items.length - 1
                  : (index + (event.key === "ArrowUp" ? -1 : 1) + items.length) % items.length;
            items[next]?.focus();
          });
        }
      }}
    >
      <button
        ref={trigger}
        type="button"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={id}
        className="min-h-11 min-w-11 w-full truncate rounded px-2 hover:bg-white/10 focus-visible:outline-white"
        onClick={() => setOpen(!open)}
      >
        {label === "Quality"
          ? value
          : `Subtitles: ${options.find((option) => option.value === value)?.label ?? "Off"}`}
      </button>
      {open && (
        <div
          id={id}
          role="menu"
          aria-label={label}
          className="pv-player-menu-options flex flex-wrap overflow-y-auto rounded bg-neutral-900"
        >
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              role="menuitemradio"
              aria-checked={value === option.value}
              className="min-h-11 max-w-full truncate rounded px-3 hover:bg-white/15 focus-visible:outline-white"
              onClick={(event) => {
                onChange(option.value);
                setOpen(false);
                // Touch must not retain a focus hold after the menu closes.
                if (event.detail === 0) trigger.current?.focus();
                else event.currentTarget.blur();
              }}
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
