import { createFileRoute, Link, Outlet, useNavigate } from "@tanstack/react-router";
import { BookOpenText, LibraryBig, Search, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

export const Route = createFileRoute("/app/reading-room")({ component: ReadingRoomPage });

type ReadingProgress = {
  locator: string;
  character_offset: number;
  completed: boolean;
  percent: number;
};

type Publication = {
  id: string;
  title: string;
  author: string;
  publication_type: string;
  edition: string | null;
  language: string | null;
  description: string | null;
  publisher: string | null;
  isbn: string | null;
  publication_details: string | null;
  cover_url: string | null;
  chapter_count: number;
  progress: ReadingProgress | null;
};

type PublicationDetail = Publication & {
  chapters: Array<{ locator: string; title: string; level: number }>;
};

type SearchResult = {
  publication_id: string;
  title: string;
  author: string;
  language: string | null;
  locator: string;
  block_type: string;
  snippet: string;
  rank: number;
};

const languageName = (language: string | null) =>
  language === "pl" ? "Polish" : language === "en" ? "English" : (language?.toUpperCase() ?? null);

const typeName = (value: string) => `${value.charAt(0).toUpperCase()}${value.slice(1)}`;

function ReadingRoomPage() {
  const navigate = useNavigate();
  const [publications, setPublications] = useState<Publication[] | null>(null);
  const [selected, setSelected] = useState<PublicationDetail | null>(null);
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const searchRequest = useRef(0);
  const detailHeading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/reading-room/publications", {
      credentials: "include",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (response.status === 401) {
          await navigate({ to: "/login" });
          return null;
        }
        if (!response.ok) throw new Error("Reading Room request failed");
        return (await response.json()) as Publication[];
      })
      .then((items) => items && setPublications(items))
      .catch((requestError) => {
        if (!(requestError instanceof DOMException && requestError.name === "AbortError")) {
          setError("Reading Room is currently unavailable.");
        }
      });
    return () => controller.abort();
  }, [navigate]);

  useEffect(() => {
    const cleaned = query.trim();
    const requestNumber = ++searchRequest.current;
    if (cleaned.length < 2) {
      setSearchResults(null);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setSearching(true);
      void fetch(`/api/reading-room/search?q=${encodeURIComponent(cleaned)}`, {
        credentials: "include",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      })
        .then(async (response) => {
          if (response.status === 401) {
            await navigate({ to: "/login" });
            return null;
          }
          if (!response.ok) throw new Error("Reading Room search failed");
          return (await response.json()) as SearchResult[];
        })
        .then((results) => {
          if (results && requestNumber === searchRequest.current) setSearchResults(results);
        })
        .catch((requestError) => {
          if (!(requestError instanceof DOMException && requestError.name === "AbortError")) {
            if (requestNumber === searchRequest.current)
              setError("Reading Room search is currently unavailable.");
          }
        })
        .finally(() => {
          if (requestNumber === searchRequest.current) setSearching(false);
        });
    }, 300);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [navigate, query]);

  useEffect(() => {
    if (selected) detailHeading.current?.focus();
  }, [selected]);

  const openDetails = async (publication: Publication) => {
    setError(null);
    const response = await fetch(`/api/reading-room/publications/${publication.id}`, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (response.status === 401) {
      await navigate({ to: "/login" });
      return;
    }
    if (!response.ok) {
      setError("Publication details are currently unavailable.");
      return;
    }
    setSelected((await response.json()) as PublicationDetail);
  };

  return (
    <div className="max-w-6xl mx-auto space-y-7">
      <section className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="pv-page-title text-3xl md:text-4xl">Reading Room</h1>
          <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Your approved books, magazines, comics and journals.
          </p>
        </div>
        <label className="relative block w-full sm:w-72">
          <span className="sr-only">Search Reading Room</span>
          <Search
            size={16}
            className="absolute left-3 top-1/2 -translate-y-1/2"
            style={{ color: "var(--pv-silver-dim)" }}
          />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search every publication"
            className="w-full rounded-lg border bg-transparent py-2.5 pl-9 pr-3 text-sm outline-none focus:ring-2"
            style={{ borderColor: "var(--pv-border)", color: "var(--pv-text)" }}
          />
        </label>
      </section>

      {error && (
        <div className="pv-panel p-4 text-sm" role="alert" style={{ color: "#efb0a9" }}>
          {error}
        </div>
      )}

      {query.trim().length >= 2 ? (
        <section aria-live="polite" aria-busy={searching}>
          {searching && searchResults === null ? (
            <div className="pv-panel p-10 text-center" style={{ color: "var(--pv-text-dim)" }}>
              Searching the Reading Room…
            </div>
          ) : searchResults?.length ? (
            <ol className="space-y-3">
              {searchResults.map((result, index) => (
                <li key={`${result.publication_id}-${result.locator}-${index}`}>
                  <button
                    type="button"
                    onClick={() =>
                      void navigate({
                        to: "/app/reading-room/$publicationId/read",
                        params: { publicationId: result.publication_id },
                        search: { locator: result.locator },
                      })
                    }
                    className="pv-panel pv-panel-hover w-full p-4 text-left focus-visible:outline-none"
                  >
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <h2 className="font-semibold" style={{ color: "var(--pv-silver)" }}>
                        {result.title}
                      </h2>
                      <span className="text-xs uppercase" style={{ color: "var(--pv-gold-dim)" }}>
                        {result.block_type.replace("_", " ")}
                      </span>
                    </div>
                    <p className="mt-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                      {result.author} · {languageName(result.language) ?? "Language not recorded"}
                    </p>
                    <SearchSnippet value={result.snippet} />
                  </button>
                </li>
              ))}
            </ol>
          ) : (
            <div className="pv-panel p-10 text-center">
              <Search size={30} className="mx-auto pv-card-icon" />
              <h2 className="mt-4 text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
                No matching passages
              </h2>
              <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
                Try another English or Polish word or phrase.
              </p>
            </div>
          )}
        </section>
      ) : publications === null && !error ? (
        <div className="pv-panel p-10 text-center" style={{ color: "var(--pv-text-dim)" }}>
          Opening the Reading Room…
        </div>
      ) : (publications?.length ?? 0) === 0 ? (
        <div className="pv-panel p-10 text-center">
          <LibraryBig size={32} className="mx-auto pv-card-icon" />
          <h2 className="mt-4 text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
            The Reading Room is empty
          </h2>
          <p className="mt-2 text-sm" style={{ color: "var(--pv-text-dim)" }}>
            Approved publications from Arrival Hall will appear here.
          </p>
        </div>
      ) : (
        <section className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {(publications ?? []).map((publication) => (
            <button
              key={publication.id}
              type="button"
              onClick={() => void openDetails(publication)}
              className="pv-panel pv-panel-hover overflow-hidden text-left focus-visible:outline-none"
              aria-label={`View ${publication.title} by ${publication.author}`}
            >
              <div
                className="aspect-[2/3] flex items-center justify-center overflow-hidden"
                style={{ background: "linear-gradient(145deg, #22201a, #101013)" }}
              >
                {publication.cover_url ? (
                  <img
                    src={publication.cover_url}
                    alt={`Cover of ${publication.title}`}
                    className="h-full w-full object-cover"
                    loading="lazy"
                  />
                ) : (
                  <BookOpenText size={42} className="pv-card-icon" aria-hidden="true" />
                )}
              </div>
              <div className="p-3">
                <h2
                  className="line-clamp-2 text-sm font-semibold"
                  style={{ color: "var(--pv-silver)" }}
                >
                  {publication.title}
                </h2>
                <p className="mt-1 line-clamp-1 text-xs" style={{ color: "var(--pv-text-dim)" }}>
                  {publication.author}
                </p>
                {publication.progress && (
                  <div className="mt-3">
                    <div
                      className="h-1 overflow-hidden rounded-full"
                      style={{ background: "rgba(255,255,255,0.08)" }}
                    >
                      <span
                        className="block h-full rounded-full"
                        style={{
                          width: `${publication.progress.percent}%`,
                          background: "var(--pv-gold)",
                        }}
                      />
                    </div>
                    <span
                      className="mt-1 block text-[10px]"
                      style={{ color: "var(--pv-silver-dim)" }}
                    >
                      {publication.progress.completed
                        ? "Completed"
                        : `${publication.progress.percent}% read`}
                    </span>
                  </div>
                )}
              </div>
            </button>
          ))}
        </section>
      )}

      {selected && (
        <section className="pv-panel p-5 md:p-7" aria-labelledby="publication-detail-title">
          <div className="flex items-start justify-between gap-4">
            <div>
              <p
                className="text-xs uppercase tracking-[0.18em]"
                style={{ color: "var(--pv-gold)" }}
              >
                {typeName(selected.publication_type)} details
              </p>
              <h2
                ref={detailHeading}
                tabIndex={-1}
                id="publication-detail-title"
                className="mt-2 text-2xl font-semibold outline-none"
                style={{ color: "var(--pv-silver)" }}
              >
                {selected.title}
              </h2>
              <p className="mt-1" style={{ color: "var(--pv-text-dim)" }}>
                {selected.author}
              </p>
            </div>
            <button
              type="button"
              onClick={() => setSelected(null)}
              aria-label="Close publication details"
            >
              <X size={20} />
            </button>
          </div>

          <div className="mt-6 max-w-2xl space-y-4 text-sm">
            {selected.description && (
              <p style={{ color: "var(--pv-text-dim)" }}>{selected.description}</p>
            )}
            <dl className="grid grid-cols-[7rem_1fr] gap-x-3 gap-y-2">
              <dt style={{ color: "var(--pv-silver-dim)" }}>Edition</dt>
              <dd>{selected.edition ?? "Not recorded"}</dd>
              <dt style={{ color: "var(--pv-silver-dim)" }}>Language</dt>
              <dd>{languageName(selected.language) ?? "Not recorded"}</dd>
              <dt style={{ color: "var(--pv-silver-dim)" }}>Publisher</dt>
              <dd>{selected.publisher ?? "Not recorded"}</dd>
              <dt style={{ color: "var(--pv-silver-dim)" }}>ISBN</dt>
              <dd>{selected.isbn ?? "Not recorded"}</dd>
            </dl>
            {selected.publication_details && (
              <p style={{ color: "var(--pv-text-dim)" }}>{selected.publication_details}</p>
            )}
            <Link
              to="/app/reading-room/$publicationId/read"
              params={{ publicationId: selected.id }}
              className="inline-flex items-center gap-2 rounded-lg border px-4 py-2 text-sm"
              style={{ borderColor: "var(--pv-gold-dim)", color: "var(--pv-gold-bright)" }}
            >
              <BookOpenText size={16} /> Open reader
            </Link>
          </div>
        </section>
      )}
      <Outlet />
    </div>
  );
}

function SearchSnippet({ value }: { value: string }) {
  const parts = value.split(/(<<|>>)/);
  let highlighted = false;
  return (
    <p className="mt-3 text-sm leading-relaxed" style={{ color: "var(--pv-text-dim)" }}>
      {parts.map((part, index) => {
        if (part === "<<") {
          highlighted = true;
          return null;
        }
        if (part === ">>") {
          highlighted = false;
          return null;
        }
        return highlighted ? <mark key={index}>{part}</mark> : <span key={index}>{part}</span>;
      })}
    </p>
  );
}
