import { useEffect, useState } from "react";

const sections: Record<string, [string, string]> = {
  Gallery: ["gallery", "photos"],
  "Home Videos": ["home_videos", "videos"],
  Theatre: ["movies", "movies"],
};

export function SectionHeading({
  title,
  section,
  pathname,
}: {
  title: string;
  section: string;
  pathname: string;
}) {
  const [counts, setCounts] = useState<Record<string, number>>({});
  const counted = sections[title];
  const routePath = pathname.replace(/\/$/, "");
  useEffect(() => {
    if (!sections[title]) return;
    const controller = new AbortController();
    const refresh = () => {
      void fetch("/api/section-counts", { credentials: "include", signal: controller.signal })
        .then(async (response) => {
          if (!response.ok) throw new Error("Counts unavailable");
          const value = await response.json();
          if (!controller.signal.aborted) setCounts(value);
        })
        .catch(() => {
          if (!controller.signal.aborted) setCounts({});
        });
    };
    refresh();
    window.addEventListener("focus", refresh);
    window.addEventListener("pv-section-counts-changed", refresh);
    const timer = window.setInterval(refresh, 60_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("pv-section-counts-changed", refresh);
    };
  }, [title, routePath]);
  const count = counted ? counts[counted[0]] : undefined;
  return (
    <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
      <h1
        className={`pv-page-title truncate text-base ${!counted && title !== "Music" && section === "Library" ? "hidden md:block" : ""}`}
      >
        {title}
      </h1>
      {counted ? (
        <span
          className="text-xs"
          style={{ color: "var(--pv-text-dim)" }}
          aria-label={`${title} total`}
        >
          {Number.isSafeInteger(count) && count >= 0
            ? `· ${count.toLocaleString("en-GB")} ${count === 1 ? counted[1].slice(0, -1) : counted[1]}`
            : ""}
        </span>
      ) : (
        section &&
        title !== "Music" && (
          <span
            className={`text-xs uppercase tracking-widest ${section === "Library" ? "" : "hidden md:inline"}`}
            style={{ color: "var(--pv-text-dim)" }}
          >
            {section}
          </span>
        )
      )}
    </div>
  );
}
